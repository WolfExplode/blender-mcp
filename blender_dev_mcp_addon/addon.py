"""Blender side of Blender Dev MCP.

Listens on a socket for JSON commands and runs them on Blender's main thread.
Scope is deliberately small: inspect the scene, run Python, grab the viewport,
and read back stderr. No asset stores, no telemetry.

Forked from blender-mcp by Siddharth Ahuja (github.com/ahujasid) - MIT.
"""

import io
import json
import math
import os
import socket
import sys
import threading
import time
import traceback
from collections import deque
from contextlib import redirect_stdout, redirect_stderr, suppress

import bmesh
import bpy
import mathutils
from bpy.props import IntProperty

# Installed as an addon this is a package, so the plain relative import works.
# test_addon.py deliberately loads this file by path under its own module name
# with no package context, where a relative import raises ImportError - so fall
# back to loading the sibling by path. Keeping both paths working is what lets
# the addon tests run without installing the addon.
try:
    from . import geonodes, ntp_bridge, undo
except ImportError:
    import importlib.util
    import os

    def _load_sibling(filename, module_name):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    geonodes = _load_sibling("geonodes.py", "blender_dev_mcp_geonodes")
    ntp_bridge = _load_sibling("ntp_bridge.py", "blender_dev_mcp_ntp_bridge")
    # `undo` carries a live counter of how many revert points we pushed, so it
    # must exist exactly once. This file is the only importer for that reason -
    # geonodes and ntp_bridge take an undo_push=False flag and hand the push
    # back here instead, since loading by path would give each of them a
    # private module object and therefore a private, wrong count.
    undo = _load_sibling("undo.py", "blender_dev_mcp_undo")

# Blender registers add-ons under the top-level module name. This file is
# imported as the `addon` submodule of the package, so __name__ here is
# "<package>.addon" - take the first path component to get the name Blender
# actually registered.
ADDON_ID = __name__.partition(".")[0]

DEFAULT_PORT = 9876

# Blender writes operator tracebacks and addon errors to stderr, which only
# appears in the system console. Keep a bounded copy so a client can read back
# what went wrong before it connected - e.g. an error the user triggered by
# clicking in the UI. The tee always writes through, so the console is
# unchanged and a failure here can never suppress Blender's own output.
STDERR_LOG = deque(maxlen=2000)
_stderr_tee = None


class _StderrTee:
    """Wraps sys.stderr, recording writes into STDERR_LOG."""

    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        with suppress(Exception):
            STDERR_LOG.append(text)
        return self.stream.write(text)

    def __getattr__(self, name):
        # Delegate flush/isatty/encoding/fileno to the real stream.
        return getattr(self.stream, name)


def install_stderr_tee():
    global _stderr_tee
    if _stderr_tee is None and sys.stderr is not None:
        _stderr_tee = _StderrTee(sys.stderr)
        sys.stderr = _stderr_tee


def remove_stderr_tee():
    global _stderr_tee
    if _stderr_tee is not None:
        if sys.stderr is _stderr_tee:
            sys.stderr = _stderr_tee.stream
        _stderr_tee = None


class BlenderDevMCPServer:
    # A command that never parses would otherwise grow the buffer forever.
    MAX_BUFFER = 32 * 1024 * 1024

    def __init__(self, host="localhost", port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if bpy.app.background:
            print("BlenderDevMCP: cannot serve in background mode (blender -b) - "
                  "commands would never execute; run Blender with a GUI")
            return

        if self.running:
            print("BlenderDevMCP: server already running")
            return

        self.running = True
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)

            self.server_thread = threading.Thread(target=self._server_loop, daemon=True)
            self.server_thread.start()
            print(f"BlenderDevMCP: listening on {self.host}:{self.port}")
        except Exception as exc:
            print(f"BlenderDevMCP: failed to start - {exc}")
            self.stop()

    def stop(self):
        self.running = False

        if self.socket:
            with suppress(Exception):
                self.socket.close()
            self.socket = None

        if self.server_thread:
            with suppress(Exception):
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            self.server_thread = None

        print("BlenderDevMCP: server stopped")

    def _server_loop(self):
        self.socket.settimeout(1.0)  # so self.running is checked regularly
        while self.running:
            try:
                client, address = self.socket.accept()
                print(f"BlenderDevMCP: client connected from {address}")
                threading.Thread(
                    target=self._handle_client, args=(client,), daemon=True
                ).start()
            except socket.timeout:
                continue
            except Exception as exc:
                if not self.running:
                    break
                print(f"BlenderDevMCP: accept failed - {exc}")
                time.sleep(0.5)

    def _handle_client(self, client):
        client.settimeout(None)
        buffer = b""
        decoder = json.JSONDecoder()

        try:
            while self.running:
                data = client.recv(8192)
                if not data:
                    break
                buffer += data

                # Decode as many whole commands as the buffer holds. Parsing the
                # entire buffer instead would treat a second pipelined command as
                # "Extra data", read that as an incomplete message, and wedge the
                # connection forever.
                while buffer:
                    buffer = buffer.lstrip()  # JSON whitespace is all ASCII
                    if not buffer:
                        break

                    # Decode strictly. errors="replace" would turn a character
                    # split across two recv() calls into U+FFFD, and re-encoding
                    # the remainder below would then bake that loss in - so a
                    # command pipelined behind another arrived corrupted. Treat
                    # an undecodable tail as "incomplete" and wait instead.
                    try:
                        text = buffer.decode("utf-8")
                    except UnicodeDecodeError:
                        if not self._discard_if_oversized(buffer):
                            break
                        buffer = b""
                        break

                    try:
                        command, end = decoder.raw_decode(text)
                    except json.JSONDecodeError:
                        # Genuinely incomplete - wait for the rest.
                        if not self._discard_if_oversized(buffer):
                            break
                        buffer = b""
                        break
                    buffer = text[end:].encode("utf-8")
                    self._dispatch_on_main_thread(client, command)
        except Exception as exc:
            print(f"BlenderDevMCP: client handler error - {exc}")
        finally:
            with suppress(Exception):
                client.close()

    def _discard_if_oversized(self, buffer):
        """True when `buffer` has grown past the cap and should be thrown away.

        Guards both the "incomplete JSON" and "undecodable tail" paths: neither
        can make progress, so without a cap a stream that never parses would
        grow without bound.
        """
        if len(buffer) > self.MAX_BUFFER:
            print("BlenderDevMCP: oversized command discarded")
            return True
        return False

    def _dispatch_on_main_thread(self, client, command):
        """Queue a command for Blender's main thread and reply when it is done."""

        def run():
            try:
                response = self.execute_command(command)
            except Exception as exc:
                traceback.print_exc()
                response = {"status": "error", "message": str(exc)}
            with suppress(Exception):
                client.sendall(json.dumps(response).encode("utf-8"))
            return None

        bpy.app.timers.register(run, first_interval=0.0)

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def execute_command(self, command):
        cmd_type = command.get("type")
        params = command.get("params", {})

        handlers = {
            "get_scene_info": self.get_scene_info,
            "get_object_info": self.get_object_info,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "execute_code": self.execute_code,
            "undo_edit": self.undo_edit,
            "get_stderr_log": self.get_stderr_log,
            "list_node_trees": self.list_node_trees,
            "get_node_tree_outline": self.get_node_tree_outline,
            "get_node_detail": self.get_node_detail,
            "validate_node_tree": self.validate_node_tree,
            "snapshot_node_tree": self.snapshot_node_tree,
            "restore_node_snapshot": self.restore_node_snapshot,
            "annotate_node_tree": self.annotate_node_tree,
        }

        handler = handlers.get(cmd_type)
        if handler is None:
            known = ", ".join(sorted(handlers))
            return {"status": "error",
                    "message": f"Unknown command type: {cmd_type}. Known: {known}"}

        try:
            return {"status": "success", "result": handler(**params)}
        except Exception as exc:
            traceback.print_exc()
            return {"status": "error", "message": str(exc)}

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------

    def get_scene_info(self, max_objects=10):
        scene = bpy.context.scene
        info = {
            "name": scene.name,
            "object_count": len(scene.objects),
            "materials_count": len(bpy.data.materials),
            "frame_current": scene.frame_current,
            "mode": bpy.context.mode,
            "active_object": getattr(bpy.context.view_layer.objects.active, "name", None),
            "objects": [],
        }
        for i, obj in enumerate(scene.objects):
            if i >= max_objects:
                break
            info["objects"].append({
                "name": obj.name,
                "type": obj.type,
                "location": [round(float(v), 4) for v in obj.location],
            })
        if len(scene.objects) > max_objects:
            info["truncated"] = f"showing {max_objects} of {len(scene.objects)} objects"
        return info

    @staticmethod
    def _get_aabb(obj):
        """World-space axis-aligned bounding box of a mesh object."""
        if obj.type != "MESH":
            raise TypeError("Object must be a mesh")
        corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
        return [
            [*mathutils.Vector(map(min, zip(*corners)))],
            [*mathutils.Vector(map(max, zip(*corners)))],
        ]

    @staticmethod
    def _capped(names, max_items):
        """A name list plus its true length, cut to `max_items`.

        Production rigs carry hundreds of vertex groups and shape keys. Dumping
        them whole made a single get_object_info call cost more than every other
        tool combined, and the count is almost always the part you wanted.
        Pass max_items=0 for the full list.
        """
        names = list(names)
        if max_items and len(names) > max_items:
            return {"count": len(names), "showing": max_items,
                    "names": names[:max_items]}
        return {"count": len(names), "names": names}

    def _edit_mode_selection(self, obj):
        """Selected vert/edge/face counts while `obj` is open in Edit Mode.

        The evaluated mesh is stale during Edit Mode, so mesh.vertices cannot
        answer this - the live state only exists in the edit BMesh.
        """
        bm = bmesh.from_edit_mesh(obj.data)
        return {
            "verts": sum(1 for v in bm.verts if v.select),
            "edges": sum(1 for e in bm.edges if e.select),
            "faces": sum(1 for f in bm.faces if f.select),
            "select_mode": list(bpy.context.tool_settings.mesh_select_mode),
        }

    def get_object_info(self, name, max_items=40):
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")

        info = {
            "name": obj.name,
            "type": obj.type,
            "mode": obj.mode,
            "location": [*obj.location],
            "rotation": [*obj.rotation_euler],
            "scale": [*obj.scale],
            "visible": obj.visible_get(),
            "selected": obj.select_get(),
            "materials": [s.material.name for s in obj.material_slots if s.material],
            "modifiers": [{"name": m.name, "type": m.type} for m in obj.modifiers],
            "vertex_groups": self._capped(
                (g.name for g in obj.vertex_groups), max_items),
        }

        if obj.type == "MESH":
            info["world_bounding_box"] = self._get_aabb(obj)
            if obj.data:
                mesh = obj.data
                info["mesh"] = {
                    "vertices": len(mesh.vertices),
                    "edges": len(mesh.edges),
                    "polygons": len(mesh.polygons),
                    "shape_keys": self._capped(
                        (k.name for k in mesh.shape_keys.key_blocks)
                        if mesh.shape_keys else (), max_items),
                }
                if obj.mode == "EDIT":
                    # Never let a selection read fail the whole call.
                    try:
                        info["mesh"]["selection"] = self._edit_mode_selection(obj)
                    except Exception as exc:
                        info["mesh"]["selection"] = {"error": str(exc)}
        return info

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png"):
        """Render the 3D viewport to `filepath`.

        screen.screenshot_area captures the OS window framebuffer, which is
        all-black whenever the Blender window is not composited in the
        foreground (the normal case when Blender is driven via MCP). Render with
        gpu.types.GPUOffScreen.draw_view3d instead, which is independent of
        window compositing state, and fall back to the window grab if offscreen
        rendering is unavailable (e.g. no GPU context). The response reports
        which path produced the image.
        """
        if not filepath:
            return {"error": "No filepath provided"}

        area = region = space = None
        for a in bpy.context.screen.areas:
            if a.type == "VIEW_3D":
                area = a
                space = a.spaces.active
                region = next((r for r in a.regions if r.type == "WINDOW"), None)
                break

        if not area or region is None or space is None:
            return {"error": "No 3D viewport found"}

        method = "offscreen"
        try:
            import gpu
            import numpy as np

            r3d = space.region_3d
            src_w, src_h = region.width, region.height
            if max(src_w, src_h) > max_size:
                s = max_size / max(src_w, src_h)
                width, height = max(1, int(src_w * s)), max(1, int(src_h * s))
            else:
                width, height = src_w, src_h

            offscreen = gpu.types.GPUOffScreen(width, height)
            try:
                offscreen.draw_view3d(
                    bpy.context.scene, bpy.context.view_layer, space, region,
                    r3d.view_matrix, r3d.window_matrix, do_color_management=True,
                )
                buf = offscreen.texture_color.read()
            finally:
                offscreen.free()

            buf.dimensions = width * height * 4
            pixels = np.asarray(buf, dtype=np.float32) / 255.0  # GPU buffer is 0..255

            # try/finally: if save() fails we fall back to the window grab, and
            # without this the scratch datablock stayed behind in the user's
            # file - a real edit to their scene caused purely by inspecting it.
            image = bpy.data.images.new("mcp_viewport", width, height, alpha=True)
            try:
                image.pixels.foreach_set(pixels.ravel())
                image.filepath_raw = filepath
                image.file_format = format.upper()
                image.save()
            finally:
                bpy.data.images.remove(image)

        except Exception as offscreen_err:
            print(f"BlenderDevMCP: offscreen capture failed ({offscreen_err}); "
                  "falling back to window grab", flush=True)
            method = "window_grab"
            with bpy.context.temp_override(area=area):
                bpy.ops.screen.screenshot_area(filepath=filepath)
            img = bpy.data.images.load(filepath)
            try:
                width, height = img.size
                if max(width, height) > max_size:
                    s = max_size / max(width, height)
                    width, height = int(width * s), int(height * s)
                    img.scale(width, height)
                    img.file_format = format.upper()
                    img.save()
            finally:
                bpy.data.images.remove(img)

        return {"success": True, "width": width, "height": height,
                "filepath": filepath, "method": method}

    def execute_code(self, code, undo_label=None):
        """Run Python in Blender and return everything it printed.

        Captures stdout *and* stderr, and on failure returns the output produced
        before the exception along with the traceback - otherwise the most
        useful diagnostics are exactly the ones that get thrown away.

        `undo_label` declares that this code changes something, and makes the
        change one step the user can Ctrl-Z and one step `undo_edit` can take
        back. It is opt-in because most code sent here only reads: pushing a
        revert point for every diagnostic print would bury the user's own edit
        history under our noise, and each push copies the whole file.
        """
        # Pre-import what almost every snippet needs. `__name__` is set to a
        # non-"__main__" value on purpose: without it `if __name__ ==
        # "__main__"` raises NameError, and setting it *to* "__main__" would
        # silently fire the entry-point block of any addon file loaded here.
        namespace = {
            "__name__": "<blender_dev_mcp>",
            "bpy": bpy,
            "bmesh": bmesh,
            "mathutils": mathutils,
            "math": math,
        }
        out_buffer = io.StringIO()
        err_buffer = io.StringIO()
        failed = False

        # Isolate anything an earlier unlabelled call left unpushed into its own
        # step, so undoing this edit reverts this edit and not also that drift.
        # Uncounted: it is a boundary, not somewhere anyone means to return to.
        if undo_label:
            undo.push(f"before MCP {undo_label}", counts=False)

        try:
            with redirect_stdout(out_buffer), redirect_stderr(err_buffer):
                exec(code, namespace)
        except Exception:
            failed = True
            err_buffer.write(traceback.format_exc())
        finally:
            # Pushed even when the code raised, and that is the important case:
            # a snippet that failed halfway has already changed the file, and
            # without a revert point that partial edit is the one thing that
            # could not be taken back.
            if undo_label:
                undo.push(f"MCP {undo_label}")

        output = out_buffer.getvalue()
        errors = err_buffer.getvalue()
        if errors:
            if output and not output.endswith("\n"):
                output += "\n"
            output += errors

        if failed:
            raise Exception(f"Code execution error:\n{output}")
        result = {"executed": True, "result": output}
        if undo_label:
            result["undo_budget"] = undo.budget()
        return result

    def list_node_trees(self):
        """Every geometry node tree in the file, with enough to pick one."""
        return geonodes.list_trees()

    def get_node_tree_outline(self, name):
        """Structural summary of one geometry node tree.

        Named separately from a full read because the full read is the
        expensive one: the reference tree here costs ~340 tokens as an outline
        and ~18,600 as a NodeToPython script. Reading the outline first and
        drilling into a single frame afterwards is the intended path, so the
        cheap call is the one with the obvious name.
        """
        tree = bpy.data.node_groups.get(name)
        if tree is None:
            known = [t.name for t in bpy.data.node_groups
                     if t.bl_idname == "GeometryNodeTree"]
            raise ValueError(
                f"No node group named {name!r}. Geometry node trees in this "
                f"file: {known}")
        return geonodes.tree_outline(tree)

    def get_node_detail(self, name, frame=None):
        """Nodes, settings and links for one frame of a geometry node tree.

        frame=None reads the nodes outside every frame, which is the only way
        into a tree that has no frames - half the trees in the reference scene
        are like that, so this is the common case rather than a fallback.
        """
        tree = bpy.data.node_groups.get(name)
        if tree is None:
            known = [t.name for t in bpy.data.node_groups
                     if t.bl_idname == "GeometryNodeTree"]
            raise ValueError(
                f"No node group named {name!r}. Geometry node trees in this "
                f"file: {known}")
        return geonodes.node_detail(tree, frame)

    def validate_node_tree(self, name, evaluate=True, max_objects=8):
        """Check a geometry node tree and report what is wrong with it."""
        tree = bpy.data.node_groups.get(name)
        if tree is None:
            known = [t.name for t in bpy.data.node_groups
                     if t.bl_idname == "GeometryNodeTree"]
            raise ValueError(
                f"No node group named {name!r}. Geometry node trees in this "
                f"file: {known}")
        return geonodes.validate_tree(
            tree, evaluate=evaluate, max_objects=max_objects)

    def snapshot_node_tree(self, name, path=None, keep_last=10):
        """Write the Python that rebuilds a tree to disk; return the path.

        The source is not returned. It is a build artifact - roughly forty
        times the size of the tree's outline, and mostly boilerplate - so
        handing it back would cost a great deal to say nothing new.
        """
        return ntp_bridge.snapshot_tree(name, path, keep_last=keep_last)

    def undo_edit(self, steps=1):
        """Take back writes this session made, using Blender's undo stack.

        This is the reverse gear for edits, and restore_node_snapshot is not:
        undo puts the tree back in place, keeping the identity that objects and
        modifier inputs are bound to, where a restore builds a copy alongside.
        """
        return undo.undo(steps)

    def restore_node_snapshot(self, path):
        """Run a snapshot file, recreating the trees it holds."""
        result = ntp_bridge.restore_snapshot(path, undo_push=False)
        undo.push(f"MCP restore {os.path.basename(path)}")
        # Validate what was just built rather than trusting that exec() not
        # raising means the graph works - which is the whole premise of having
        # a validator at all.
        reports = []
        for created in result["created"]:
            tree = bpy.data.node_groups.get(created)
            if tree is not None and tree.bl_idname == "GeometryNodeTree":
                reports.append(geonodes.validate_tree(tree, evaluate=False))
        result["validation"] = reports
        return result

    def annotate_node_tree(self, name, labels=None, frames=None):
        """Write labels and frames onto a geometry node tree."""
        tree = bpy.data.node_groups.get(name)
        if tree is None:
            known = [t.name for t in bpy.data.node_groups
                     if t.bl_idname == "GeometryNodeTree"]
            raise ValueError(
                f"No node group named {name!r}. Geometry node trees in this "
                f"file: {known}")
        result = geonodes.annotate_tree(
            tree, labels=labels, frames=frames, undo_push=False)
        undo.push(f"MCP annotate {name}")
        return result

    def get_stderr_log(self, max_chars=8000, clear=False):
        """Read back what Blender has written to stderr this session.

        Covers tracebacks the user triggered through the UI, which are otherwise
        only visible in the system console.
        """
        text = "".join(STDERR_LOG)
        truncated = len(text) > max_chars
        if truncated:
            text = text[-max_chars:]
        # Count before clearing, otherwise clear=True always reported 0 chunks
        # alongside the text it had just returned.
        chunks = len(STDERR_LOG)
        if clear:
            STDERR_LOG.clear()
        return {
            "text": text,
            "truncated": truncated,
            "chunks": chunks,
            "tee_active": _stderr_tee is not None,
        }


# ----------------------------------------------------------------------
# settings
# ----------------------------------------------------------------------

class BlenderDevMCPPreferences(bpy.types.AddonPreferences):
    """Port and auto-start, stored per user rather than per .blend.

    These used to be bpy.types.Scene properties, which was wrong twice over.
    A Scene property lives inside the .blend, so every scene in every file
    carried its own opinion about a server there is only one of; and at startup
    register() runs before any file is loaded, when bpy.context is a
    _RestrictContext whose .scene is None - so the saved values could never be
    read at the one moment auto-start needed them, and every launch silently
    fell back to "port 9876, auto-start on" no matter what was saved.

    Preferences have neither problem: one copy per user, in userpref.blend, and
    bpy.context.preferences is readable from inside the restricted context.
    """

    bl_idname = ADDON_ID

    port: IntProperty(
        name="Port",
        description="Port the addon listens on. Must match BLENDER_PORT on the "
                    "MCP client side",
        default=DEFAULT_PORT, min=1024, max=65535,
    )
    auto_start: bpy.props.BoolProperty(
        name="Start on launch",
        description="Start the server automatically when Blender loads the addon",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "port")
        layout.prop(self, "auto_start")


def get_prefs():
    """This addon's preferences, or None if they are not reachable.

    None happens when the module is loaded outside a normal addon install -
    notably the test suite, which imports addon.py under its own module name so
    ADDON_ID does not match any registered addon. Callers fall back to the
    module defaults, so a missing prefs entry degrades to sane behaviour rather
    than an exception during register().
    """
    try:
        entry = bpy.context.preferences.addons.get(ADDON_ID)
    except Exception:
        return None
    return getattr(entry, "preferences", None)


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------

class BLENDERDEVMCP_PT_Panel(bpy.types.Panel):
    bl_label = "Blender Dev MCP"
    bl_idname = "BLENDERDEVMCP_PT_Panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Dev MCP"

    def draw(self, context):
        layout = self.layout
        prefs = get_prefs()

        if prefs:
            layout.prop(prefs, "port")
            layout.prop(prefs, "auto_start")

        # Ask the server itself; never cache "am I running" anywhere else. The
        # old cached flag lived on the Scene and was wrong in exactly the case
        # that mattered - it read False at startup, so the panel offered "Start
        # server" for a server that was already listening.
        server = getattr(bpy.types, "blender_dev_mcp_server", None)
        if server is not None and server.running:
            layout.operator("blender_dev_mcp.stop_server", text="Stop server")
            layout.label(text=f"Running on port {server.port}", icon="CHECKMARK")
            # The prefs port is what the *next* start will use, so say so rather
            # than letting the field imply the live socket moved with it.
            if prefs and prefs.port != server.port:
                layout.label(text=f"Stop/Start to move to {prefs.port}", icon="INFO")
        else:
            layout.operator("blender_dev_mcp.start_server", text="Start server")


class BLENDERDEVMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blender_dev_mcp.start_server"
    bl_label = "Start Blender Dev MCP server"
    bl_description = "Start listening for MCP commands"

    def execute(self, context):
        prefs = get_prefs()
        port = prefs.port if prefs else DEFAULT_PORT

        server = getattr(bpy.types, "blender_dev_mcp_server", None)
        if server is None:
            server = bpy.types.blender_dev_mcp_server = BlenderDevMCPServer(port=port)
        elif server.port != port:
            # Editing the port then pressing Start used to do nothing at all:
            # the port was only read when the server object was constructed, and
            # auto-start had already constructed one. Rebind onto the new port.
            server.stop()
            server.port = port
        server.start()

        # start() reports failures by printing to the system console, which is
        # exactly where a GUI user will not look. Surface it in the UI.
        if not server.running:
            self.report({"ERROR"},
                        f"Could not listen on port {port} - see the system "
                        f"console. Is another Blender already using it?")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Blender Dev MCP listening on port {port}")
        return {"FINISHED"}


class BLENDERDEVMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blender_dev_mcp.stop_server"
    bl_label = "Stop Blender Dev MCP server"
    bl_description = "Stop listening for MCP commands"

    def execute(self, context):
        if getattr(bpy.types, "blender_dev_mcp_server", None):
            bpy.types.blender_dev_mcp_server.stop()
            del bpy.types.blender_dev_mcp_server
        return {"FINISHED"}


CLASSES = (
    BlenderDevMCPPreferences,
    BLENDERDEVMCP_PT_Panel,
    BLENDERDEVMCP_OT_StartServer,
    BLENDERDEVMCP_OT_StopServer,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)

    install_stderr_tee()

    # Auto-start so the MCP client can connect without manual UI interaction.
    # Wrapped because an exception escaping register() makes Blender abandon the
    # whole addon - it prints one line, "Exception in module register()", and
    # leaves it disabled. A port that will not bind would then take the panel
    # down with it, hiding the very button you need to recover. Keep the addon
    # loaded and let the user press Start.
    try:
        prefs = get_prefs()
        port = prefs.port if prefs else DEFAULT_PORT
        auto_start = prefs.auto_start if prefs else True

        if auto_start:
            server = getattr(bpy.types, "blender_dev_mcp_server", None)
            if server is None:
                server = bpy.types.blender_dev_mcp_server = BlenderDevMCPServer(
                    port=port)
            if not server.running:
                server.start()
    except Exception:
        traceback.print_exc()
        print("BlenderDevMCP: auto-start failed; addon is still loaded - "
              "use View3D > Sidebar > Dev MCP to start it manually")

    print("Blender Dev MCP addon registered")


def unregister():
    remove_stderr_tee()

    if getattr(bpy.types, "blender_dev_mcp_server", None):
        bpy.types.blender_dev_mcp_server.stop()
        del bpy.types.blender_dev_mcp_server

    for cls in reversed(CLASSES):
        with suppress(Exception):
            bpy.utils.unregister_class(cls)

    print("Blender Dev MCP addon unregistered")
