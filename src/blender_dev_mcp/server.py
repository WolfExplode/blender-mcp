"""MCP server exposing a running Blender for addon development and debugging.

Talks to the companion Blender addon over a local socket: scene/object/
collection inspection, object search, arbitrary Python, geometry node
tooling, viewport and stderr capture.

Forked from blender-mcp by Siddharth Ahuja (github.com/ahujasid) - MIT.
"""

import json
import logging
import os
import socket
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict

from mcp.server.fastmcp import Context, FastMCP, Image

from . import docs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("BlenderDevMCP")

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876

# A heavy operator on a dense mesh can legitimately outlast the default, and
# there is no progress signal to wait on - so make the ceiling adjustable
# rather than making everyone pay for the worst case.
RECV_TIMEOUT = float(os.getenv("BLENDER_MCP_TIMEOUT", "180"))


class IncompleteResponse(Exception):
    """The peer stopped mid-response, so the stream position is unknown.

    Distinct from a bad-but-complete reply: the socket must be dropped, since
    whatever arrives next would be read as the answer to the wrong command.
    """


class ConnectionDropped(Exception):
    """The socket failed mid-exchange and has been discarded.

    Raised for every transport-level failure -- reset, aborted, closed early,
    truncated. The distinction that matters to the caller is not which of those
    happened but that no trustworthy reply arrived and the stream is gone.
    """


class NotConnected(ConnectionDropped):
    """No connection could be established in the first place.

    A subclass because the recovery is identical -- there is no usable socket
    either way -- but the wording must not blame a connection that never
    existed, and the address has to be named: the port is settable both in the
    addon panel and via BLENDER_PORT, so a bare "refused" is ambiguous between
    "Blender is closed" and "the two ends disagree on the port".
    """


# Commands with no effect on the blend file or on disk. Only these may be
# replayed automatically when a pooled socket turns out to be dead: a stale
# socket cannot prove whether the command reached Blender before the connection
# went away, and replaying a mutating command could apply it twice.
READ_ONLY_COMMANDS = frozenset({
    "get_scene_info",
    "get_object_info",
    "get_object_property",
    "get_viewport_screenshot",
    "get_stderr_log",
    "list_node_trees",
    "get_node_tree_outline",
    "get_node_detail",
    "validate_node_tree",
    "get_collection_tree",
    "find_objects",
    "get_object_tree",
    "get_bone_tree",
    "audit_names",
})


@dataclass
class BlenderConnection:
    host: str
    port: int
    sock: socket.socket = None
    # Serializes send+receive so two commands can never interleave on one socket.
    # Without this, a second command's response can be read as the first's, and
    # the stream stays desynced until the timeout fires.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def connect(self) -> bool:
        if self.sock:
            return True
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Blender at {self.host}:{self.port}")
            return True
        except Exception as exc:
            logger.error(f"Failed to connect to Blender: {exc}")
            self.sock = None
            return False

    def disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception as exc:
                logger.error(f"Error disconnecting from Blender: {exc}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192) -> bytes:
        """Read until the accumulated bytes parse as one JSON object."""
        chunks = []
        sock.settimeout(RECV_TIMEOUT)
        while True:
            chunk = sock.recv(buffer_size)
            if not chunk:
                if not chunks:
                    # Must be a type _attempt classifies as a transport failure.
                    # Anything else escapes its handlers with the dead socket
                    # still pooled, and the next call fails on it too.
                    raise IncompleteResponse(
                        "connection closed before any data arrived")
                break
            chunks.append(chunk)
            data = b"".join(chunks)

            # A complete response is one JSON object, so it always ends with
            # "}". Skipping the parse when it cannot possibly be complete keeps
            # a large execute_blender_code dump from being reassembled in
            # quadratic time - every 8 KB chunk otherwise re-parsed the whole
            # buffer so far.
            if not data.rstrip().endswith(b"}"):
                continue
            try:
                json.loads(data.decode("utf-8"))
                return data
            except UnicodeDecodeError:
                continue  # character split across chunks, keep reading
            except json.JSONDecodeError:
                continue  # incomplete, keep reading

        raise IncompleteResponse("Incomplete JSON response received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        # Hold the lock across send+receive: the response is matched to the
        # command purely by ordering on the stream, so overlapping calls would
        # hand each other's responses back.
        with self._lock:
            return self._send_command_locked(command_type, params)

    def _send_command_locked(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send one command, replacing a pooled socket that turns out to be dead.

        A socket handed back by the pool may have been closed by the peer since
        it was last used: reloading addons in Blender restarts the companion
        server and orphans every open connection. Nothing distinguishes that
        from a healthy socket until something is written to it, so the first
        attempt doubles as the liveness probe and a failure on a *reused* socket
        is treated as routine rather than as an error worth surfacing.
        """
        pooled = self.sock is not None
        try:
            return self._attempt(command_type, params)
        except NotConnected:
            raise
        except ConnectionDropped as exc:
            if not pooled:
                # Freshly connected and it still failed - Blender itself is the
                # problem, so reporting beats retrying.
                raise Exception(f"Connection to Blender lost: {exc}") from exc
            if command_type not in READ_ONLY_COMMANDS:
                raise Exception(
                    f"Connection to Blender was dropped before '{command_type}' "
                    f"was answered ({exc}). It may or may not have run, so it "
                    "was not retried automatically - check the result and "
                    "re-issue it if nothing happened."
                ) from exc
            logger.info("Pooled socket was stale; reconnecting to retry %s",
                        command_type)

        # Second and final attempt, on a socket known to be new.
        try:
            return self._attempt(command_type, params)
        except NotConnected:
            raise
        except ConnectionDropped as exc:
            raise Exception(f"Connection to Blender lost: {exc}") from exc

    def _attempt(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """One send/receive round trip. Any transport failure drops the socket."""
        if not self.sock and not self.connect():
            raise NotConnected(
                f"Could not connect to Blender at {self.host}:{self.port}. Make "
                "sure Blender is running with the Blender Dev MCP addon enabled, "
                "and that the port matches the one in View3D > Sidebar > Dev MCP.")

        command = {"type": command_type, "params": params or {}}
        try:
            logger.info(f"Sending command: {command_type}")
            self.sock.sendall(json.dumps(command).encode("utf-8"))
            self.sock.settimeout(RECV_TIMEOUT)
            response = json.loads(
                self.receive_full_response(self.sock).decode("utf-8"))
        except socket.timeout:
            # socket.timeout is an OSError, so it has to be classified before
            # the transport catch-all below. A timeout is not a dead socket -
            # Blender may simply be busy - but the stream is desynced either
            # way, so the socket still goes.
            self.disconnect()
            raise Exception(
                f"Timeout after {RECV_TIMEOUT:g}s waiting for Blender. If Blender "
                "is running headless (blender -b), commands never execute - run "
                "it with a GUI. If the command is genuinely slow, raise "
                "BLENDER_MCP_TIMEOUT (seconds).")
        except json.JSONDecodeError as exc:
            self.disconnect()
            raise Exception(f"Invalid response from Blender: {exc}") from exc
        except (OSError, IncompleteResponse) as exc:
            # Every transport failure lands here: refused, reset, aborted, closed
            # early, truncated. Catch the whole OSError family rather than named
            # subclasses - a platform errno that is not ConnectionError (Windows
            # sends WSAECONNABORTED as a plain OSError) must not slip through and
            # leave a dead socket pooled.
            self.disconnect()
            raise ConnectionDropped(str(exc) or type(exc).__name__) from exc
        except Exception:
            # Unclassified, so the stream state is unknown - never reuse it.
            self.disconnect()
            raise

        # Deliberately outside the try: a well-formed error reply means the
        # connection is healthy and should stay in the pool.
        if response.get("status") == "error":
            raise Exception(response.get("message", "Unknown error from Blender"))
        return response.get("result", {})


_blender_connection = None


def get_blender_connection() -> BlenderConnection:
    """Get the persistent Blender connection, creating the pool entry if needed.

    Connecting is left to the first command. We deliberately do NOT probe the
    socket here: that put two commands on the wire for every tool call, and any
    overlap desynced the response stream until the socket timeout fired. A dead
    socket is detected by the next real command and replaced there, which is
    also the only place that knows whether the command is safe to replay.
    """
    global _blender_connection

    if _blender_connection is None:
        host = os.getenv("BLENDER_HOST", DEFAULT_HOST)
        port = int(os.getenv("BLENDER_PORT", DEFAULT_PORT))
        _blender_connection = BlenderConnection(host=host, port=port)
        # Connect eagerly so startup can log reachability, but a failure here is
        # not fatal: Blender may simply not be up yet, and _attempt will name the
        # address if it is still unreachable when a command needs it.
        if _blender_connection.connect():
            logger.info("Created new persistent connection to Blender")

    return _blender_connection


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    logger.info("BlenderDevMCP starting up")
    try:
        try:
            get_blender_connection()
            logger.info("Connected to Blender on startup")
        except Exception as exc:
            logger.warning(f"Could not connect to Blender on startup: {exc}")
            logger.warning("Enable the Blender Dev MCP addon before using tools")
        yield {}
    finally:
        global _blender_connection
        if _blender_connection:
            _blender_connection.disconnect()
            _blender_connection = None
        logger.info("BlenderDevMCP shut down")


mcp = FastMCP("BlenderDevMCP", lifespan=server_lifespan)


# ----------------------------------------------------------------------
# tools
#
# These raise on failure rather than returning an "Error: ..." string, so the
# client sees a real MCP error instead of a successful result that happens to
# contain the word Error.
# ----------------------------------------------------------------------

@mcp.tool()
def get_scene_info(ctx: Context, max_objects: int = 10) -> str:
    """Summarise the current Blender scene.

    Returns scene name, object/material counts, current frame, interaction mode,
    active object, and an object list truncated at max_objects in scene order -
    fine for a small scene, close to useless for finding one object among
    hundreds. For that, use find_objects or get_collection_tree instead.

    Parameters:
    - max_objects: How many objects to list (default 10)
    """
    result = get_blender_connection().send_command(
        "get_scene_info", {"max_objects": max_objects})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_collection_tree(ctx: Context, max_items: int = 20) -> str:
    """Read the scene's collection hierarchy - the same tree the Outliner shows.

    Cheap and structural, the collection equivalent of get_node_tree_outline:
    names, nesting, per-collection visibility (excluded from the view layer,
    hidden in the viewport), and a capped list of the object names directly in
    each collection (not recursive - a child collection's objects appear under
    the child, not duplicated in the parent). Use this to see how the user
    organized the scene before searching it, or to answer "what collections
    are there" without wading through get_scene_info's object list.

    Parameters:
    - max_items: Cap on object names listed per collection (default 20; 0 for all)
    """
    result = get_blender_connection().send_command(
        "get_collection_tree", {"max_items": max_items})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def find_objects(ctx: Context, name_contains: str = None, type: str = None,
                 collection: str = None, visible_only: bool = False,
                 selected_only: bool = False, max_results: int = 50) -> str:
    """Search for objects instead of paging through get_scene_info's list.

    Filters bpy.context.scene.objects rather than truncating it in scene
    order, so it finds a needle in a 668-object file instead of getting lucky.
    Every filter given must match (AND, not OR); omit ones you don't need.

    Call it with no filters first - it does not dump "everything" in scene
    order. It tries the current selection, then what's visible in the
    viewport, then falls back to every object only if both of those are
    empty. The result's `scope` field says which one it used ("selected",
    "visible", or "all"), so build up from there: whatever the user is
    already looking at is usually the right starting point on a large file.

    Parameters:
    - name_contains: Case-insensitive substring match against the object name
    - type: Exact object type - 'MESH', 'ARMATURE', 'EMPTY', 'CAMERA', etc.
    - collection: Restrict to objects in this collection, including its nested
      sub-collections (Collection.all_objects). See get_collection_tree for
      names.
    - visible_only: Only objects currently visible in the viewport
      (obj.visible_get() - accounts for collection exclusion and hide toggles)
    - selected_only: Only currently selected objects
    - max_results: Cap on objects returned (default 50; 0 for all). The true
      match count (`total_matches`) is reported even when the list is capped.
    """
    result = get_blender_connection().send_command(
        "find_objects",
        {"name_contains": name_contains, "type": type, "collection": collection,
         "visible_only": visible_only, "selected_only": selected_only,
         "max_results": max_results})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def get_object_tree(ctx: Context, name: str = None, max_items: int = 25) -> str:
    """Read one level of the Outliner's object-parenting tree.

    This is a different hierarchy from get_collection_tree: the Outliner also
    nests objects under their parent object (Object.parent), independent of
    which collection they're in. mmd_tools rigs are the case this matters for
    - hundreds of rigidbody and joint empties parented under helper objects
    like "rigidbodies" rather than sorted into collections, invisible to
    get_collection_tree entirely.

    Returns one level at a time rather than the whole subtree, since a single
    parent can have hundreds of children on a rig like that. Omit `name` for
    the scene's root objects (no parent); pass an object name to expand its
    immediate children. Each child reports its own child_count so you know
    whether to drill into it next.

    Parameters:
    - name: Object to expand. Omit for the scene's parentless root objects.
    - max_items: Cap on children listed (default 25; 0 for all). The true
      count is always in total_children even when capped.
    """
    result = get_blender_connection().send_command(
        "get_object_tree", {"name": name, "max_items": max_items})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def get_bone_tree(ctx: Context, object_name: str, name: str = None,
                  max_items: int = 25) -> str:
    """Read one level of an armature's bone-parenting tree.

    get_object_tree's contract, one level down: Object.parent shows how
    objects nest, this shows how one armature's own Bone.parent/children
    nest. A production rig (mmd_tools physics chains especially) can carry
    300+ bones, and neither audit_names nor a plain bone-name list preserves
    that hierarchy - both return it flat. This is the tool for "does this
    bone still parent under the bone it should" after a bulk rename.

    Returns one level at a time rather than the whole subtree, same
    reasoning as get_object_tree. Omit `name` for the armature's root bones
    (no parent); pass a bone name to expand its immediate children. Each
    child reports its own child_count so you know whether to drill further.

    Parameters:
    - object_name: The armature object whose bones to walk (the object, not
      the armature datablock - same name get_object_info would take).
    - name: Bone to expand. Omit for the armature's parentless root bones.
    - max_items: Cap on children listed (default 25; 0 for all). The true
      count is always in total_children even when capped.
    """
    result = get_blender_connection().send_command(
        "get_bone_tree",
        {"object_name": object_name, "name": name, "max_items": max_items})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def get_object_info(ctx: Context, object_name: str, max_items: int = 40) -> str:
    """Inspect one object: transform, mode, selection, materials, modifiers,
    vertex groups, shape keys, mesh counts, and world-space bounding box.

    Vertex groups and shape keys are returned as {count, names} and capped, so a
    production rig with hundreds of each stays readable. When the object is in
    Edit Mode the live selected vert/edge/face counts are included.

    Parameters:
    - object_name: Name of the object to inspect
    - max_items: Cap on names listed per group (default 40; 0 for all)
    """
    result = get_blender_connection().send_command(
        "get_object_info", {"name": object_name, "max_items": max_items})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_object_property(ctx: Context, object_name: str, path: str,
                        max_items: int = 40) -> str:
    """Read one specific piece of data hanging off an object by RNA path.

    get_object_info answers fixed, shallow questions (what shape keys exist,
    how many modifiers) - names and counts only. This drills into one of
    them: a shape key's value/mute/interpolation, a modifier's actual
    settings, a constraint's target and influence. It is the same
    outline-then-detail shape as get_node_tree_outline/get_node_detail,
    applied to the rest of an object's data instead of just geometry nodes.

    A datablock reached along the path (a material, another object, a mesh)
    is named rather than expanded - point a fresh call at it if you need its
    own detail. A nested struct one level deep is fully expanded; a
    collection reached mid-path or as the result is a capped list of names.

    Parameters:
    - object_name: Object to start from (see get_object_info)
    - path: Dotted path from the object, e.g. 'modifiers["Subsurf"]',
      'data.shape_keys.key_blocks["Smile"]', 'constraints[0]',
      'data.shape_keys.key_blocks["Smile"].value'
    - max_items: Cap on names listed for any collection along the way
      (default 40; 0 for all)
    """
    result = get_blender_connection().send_command(
        "get_object_property",
        {"name": object_name, "path": path, "max_items": max_items})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def execute_blender_code(ctx: Context, code: str, undo_label: str = None,
                         dry_run: bool = False, rollback_on_error: bool = True,
                         max_diff_items: int = 100) -> str:
    """Execute Python inside Blender and return everything it printed.

    Captures stdout and stderr. If the code raises, the error includes both the
    output produced before the exception and the full traceback.

    The general-purpose tool here - read tools answer fixed questions, anything
    else is a few lines of bpy. A labelled write (dry run or not) also reports
    what it created, deleted and renamed.

    Parameters:
    - code: Python source. `bpy`, `bmesh`, `mathutils`, `math` already in scope.
      Runs with __name__ = "<blender_dev_mcp>", so `if __name__ == "__main__"`
      won't fire on its own.
    - undo_label: Set whenever the code changes anything, to a short
      description ("add curve resample"). Makes the edit one Ctrl-Z and lets
      undo_edit take it back, and fingerprints the diff. Leave unset for
      read-only code - an unlabelled edit can't be undone or diffed.
    - dry_run: Runs for real (no way to preview arbitrary Python otherwise) then
      puts the file back, keeping nothing. Use when the scope is unknown - an
      operator with implicit reach, a wildcard match, someone else's rig. Skip
      it when you already know the extent - a plain labelled write reports the
      same diff without a second execution. Can't undo writes outside the blend
      file (saves, exports, network calls already happened for real).
    - rollback_on_error: When labelled code raises partway, take back what it
      already did (default true). False keeps the wreckage for inspection.
    - max_diff_items: Cap per change kind (default 100, 0 = no cap, negative =
      totals only with no item list at all). Past the cap you get a
      head-and-tail sample, not the first N, since lists are name-sorted and
      an outlier is as likely to sort last as first. Full counts always in
      "totals" regardless of the cap - reach for negative on an edit spanning
      several kinds at hundreds of items each, where even the sampled lists
      can add up to more than the caller's own output budget.

    Diff blind spots regardless of dry_run: compares names/existence only, so
    a value assignment shows as no change; only watches bpy.data plus vertex
    groups, bones, shape keys, so addon-owned PropertyGroup state (rig
    metadata, custom collections) is invisible, and if that state is keyed by
    datablock name, a rename is the risky edit that won't show. Print
    post-state yourself for anything the diff can't see.

    The opposite surprise: renaming a bone (`armature.bones[i].name = ...`)
    cascades inside Blender itself, renaming the matching vertex group on
    every mesh with an Armature modifier pointing at that armature - before
    your code's next line runs. The diff reports this correctly because it
    fingerprints real before/after state, but a manual counter in your own
    code (`if vg.name == old: vg.name = new; count += 1`) will undercount,
    since Blender already did the rename for you and your check silently
    no-ops. When bone and vertex-group renames land in the same call, trust
    the returned diff's totals over anything your code printed - don't
    re-derive the count yourself.
    """
    result = get_blender_connection().send_command(
        "execute_code", {"code": code, "undo_label": undo_label,
                         "dry_run": dry_run,
                         "rollback_on_error": rollback_on_error,
                         "max_diff_items": max_diff_items})

    output = result.get("result", "") or "(no output)"
    # Only a labelled call (dry run or a real write) is fingerprinted, so only
    # those get a diff report - an unlabelled read-only call returns just its
    # printed output.
    if "changed" not in result:
        return output

    if result.get("dry_run"):
        header = ("[dry run - nothing in the blend file was kept]"
                  if result.get("reverted") else
                  "[dry run - WARNING: could not be reverted, the change is "
                  f"still applied: {result.get('revert_error')}]")
    else:
        header = "[written and undo-pushed]"
    report = [header]

    changed = result.get("changed") or {}
    if changed:
        report.append(json.dumps({"changed": changed,
                                  "totals": result.get("totals", {})},
                                 indent=2, ensure_ascii=False))
    else:
        report.append(
            "No datablock was created, deleted or renamed. If the code was "
            "meant to assign values rather than rename things, that is expected "
            "- this diff cannot see it. Verify by reading the values back.")
    if output != "(no output)":
        report.append("--- output ---")
        report.append(output)
    return "\n".join(report)


@mcp.tool()
def rename_items(ctx: Context, kind: str = None, targets: list = None,
                 renames: dict = None, substitutions: dict = None,
                 pattern_order: str = "longest_first", object_name: str = None,
                 dry_run: bool = False, undo_label: str = None,
                 max_diff_items: int = 100) -> str:
    """Rename a batch of same-kind items and report every knock-on change.

    Use this instead of writing your own rename loop in execute_blender_code,
    for any of the kinds listed below. The reason is not convenience: for
    bone, vertex_group, and shape_key specifically, the name is not just a
    label, it is the only link something else holds - a modifier's
    `vertex_group` field, an F-Curve's `pose.bones["Name"]` data path. Blender
    itself cascades a rename of one of these to every such reference
    *synchronously*, before the assignment statement that triggered it
    returns - most visibly, renaming a bone renames the matching vertex group
    on every mesh with an Armature modifier pointing at that armature. Code
    that renames things by hand and keeps its own counter will under- or
    over-count, because Blender already did part of the work the loop
    expected to do itself. This tool never counts renames from inside the
    loop; it reports the same pointer-based before/after diff
    execute_blender_code uses, so the number that comes back is what actually
    changed, not what the loop believed it did.

    Parameters:
    - kind: What's being renamed, for a single target. A bpy.data collection
      - "object", "mesh", "material", "armature", "action", "image",
      "collection", "node_group", "curve", "camera", "light", "texture",
      "world", "text", "scene" - or one of the three cascading sub-item
      kinds - "bone", "vertex_group", "shape_key" - which need object_name.
      Exactly one of kind or targets is required.
    - targets: [{"kind": ..., "object_name": ...}, ...] - rename across
      several kinds in one call instead of one rename_items call per kind,
      all sharing the one renames/substitutions given here, applied as one
      undo step with one before/after diff. Built for exactly the case that
      motivated substitutions in the first place: translating or rewording
      every object, mesh, material, armature, bone and shape key name in a
      file is naturally one dict applied across six-odd kinds, and that used
      to mean six-odd separate calls. object_name inside a target dict means
      what it means in the single-target form (only for the three
      contextual kinds); omit it for bpy.data kinds. A pattern that only
      applies to one target's kind (a hair-color term, say, in a target
      whose kind is "armature") simply matches nothing there at no cost -
      unused_patterns only flags a pattern that matched nothing across
      *every* target, not per target.

      Don't add a "vertex_group" target next to a "bone" target covering the
      same rig with the same renames/substitutions - the bone rename already
      cascades to the matching vertex groups within this same call, so by
      the time a vertex_group target ran those names would already be gone.
      Every one would come back "missing", harmlessly, but it's a wasted
      target. Leave vertex_group out and let the cascade do it.

      A failure partway through the list rolls back every target already
      applied in that call, not only the one that raised.
    - renames: {old_name: new_name}. A name missing from the collection is
      reported rather than raising, so one typo in a batch of 400 doesn't cost
      the rest. Exactly one of renames or substitutions is required.
    - substitutions: {pattern: replacement} - for renaming by rule instead of
      by an explicit list of names, e.g. translating every Japanese/Chinese
      bone, object and material name in a file to English in one call per
      kind. Every current name in the collection is read live and each
      pattern is applied to it in turn via plain substring replace; a name
      nothing matches is left alone. Any pattern that matched nothing at all
      comes back under "unused_patterns" - the substitutions equivalent of
      "missing" for renames, so a mistyped character in one entry out of a
      hundred doesn't have to be found by re-reading the whole result by eye.
    - pattern_order: "longest_first" (default) or "given". Substring patterns
      are inherently order-dependent: if one pattern's text is itself a
      substring of another's ("首"->"Neck" and "手首"->"Wrist"), whichever
      fires first consumes the shared characters and the other can never
      match as intended - dict order turned "手首" into "手Neck" instead of
      "Wrist" this way during this feature's own development, twice, once
      from raw ordering and once from trimming a dict down to a subset and
      losing a fallback pattern upstream entries had been depending on.
      "longest_first" removes that whole class of mistake for the common
      case (a translation/rewording glossary, where the longer pattern is
      essentially always the more specific one) by resolving the longest
      pattern touching a span first regardless of how the dict was written;
      equal-length patterns keep dict order as a tiebreak. Pass "given" only
      for the deliberate, rarer case of a true substitution chain, where one
      pattern's replacement text is meant to feed what a later, shorter
      pattern matches - forcing longest-first would break that chain.
    - object_name: With kind (single-target form): required for
      bone/vertex_group/shape_key - the armature (for bone) or mesh (for
      vertex_group/shape_key) that owns them. Ignored for bpy.data kinds.
      Not used with targets - put object_name inside each target dict.
    - dry_run: Apply for real, report the diff, then revert. Reach for this
      when the cascade radius of a rename isn't already known - a bone rename
      on a rig you didn't build, a vertex group shared across meshes you
      haven't all inspected - and always with substitutions, to check pattern
      ordering before it's kept. With targets, the whole list is applied and
      reverted together, so the diff is the combined effect of every target,
      cascades between them included, not one target at a time.
    - undo_label: Defaults to "rename N <kind>(s)" for a single target,
      "rename across N target(s)" for targets.
    - max_diff_items: Cap per change kind (default 100, 0 = no cap, negative =
      totals only, no item list); see execute_blender_code. A substitutions
      rename spanning several kinds at once (objects, bones, materials, shape
      keys...) each with hundreds of items is exactly the case where even a
      head-and-tail sample per kind adds up to more than a caller's own
      output budget - drop to a negative max_diff_items and, if a particular
      name needs checking, look it up with a targeted read afterwards.

    A rename can also collide: two old names mapping to the same new one, or a
    new name already taken in that collection. Blender resolves this itself by
    appending ".001" rather than raising - so every applied rename whose
    actual result differs from what was requested comes back under
    "collisions" in the report, not silently buried inside the diff as if it
    were the plain rename that was asked for.

    With targets, "missing" and "collisions" entries are tagged with which
    target (kind, object_name) they came from, and a per-target breakdown
    (kind, object_name, requested, applied) is always included - cheap even
    when every target succeeded cleanly, so there's no extra cost to reading
    a multi-target result that went exactly as intended.
    """
    result = get_blender_connection().send_command(
        "rename", {"kind": kind, "targets": targets, "renames": renames,
                   "substitutions": substitutions, "pattern_order": pattern_order,
                   "object_name": object_name, "dry_run": dry_run,
                   "undo_label": undo_label, "max_diff_items": max_diff_items})

    header = f"requested {result.get('requested', 0)}, applied {result.get('applied', 0)}"
    if result.get("dry_run"):
        header += (" [dry run - nothing kept]" if result.get("reverted") else
                   " [dry run - WARNING: could not be reverted, still applied]")
    else:
        header += " [written and undo-pushed]"
    report = [header]

    if result.get("targets"):
        report.append("targets: " + json.dumps(result["targets"], ensure_ascii=False))
    if result.get("missing"):
        report.append(f"missing (not found, skipped): {result['missing']}")
    if result.get("unused_patterns"):
        report.append("unused_patterns (matched nothing - check for a typo, or "
                      "for pattern_order='longest_first' having made a shorter "
                      f"pattern unreachable): {result['unused_patterns']}")
    if result.get("collisions"):
        report.append("collisions (Blender changed the requested name to "
                      f"avoid a clash): {json.dumps(result['collisions'], ensure_ascii=False)}")

    changed = result.get("changed") or {}
    if changed:
        report.append(json.dumps({"changed": changed,
                                  "totals": result.get("totals", {})},
                                 indent=2, ensure_ascii=False))
    else:
        report.append("No further datablocks were created, deleted or renamed as a side effect.")
    return "\n".join(report)


@mcp.tool()
def audit_names(ctx: Context, pattern: str = None, max_items: int = 50,
                kind: str = None, object_name: str = None) -> str:
    """Scan names for a leftover, without changing anything - the whole file
    by default, or one specific collection.

    Use this after a rename_items substitutions pass to confirm it actually
    got everything, instead of hand-writing a scan in execute_blender_code -
    a loop over objects, materials, every armature's bones, every mesh's
    vertex groups and shape keys, checking name.isascii() on each. That loop
    got written from scratch twice during one translation job before this
    tool existed to replace it.

    With kind omitted, it looks in the same namespace rename_items and
    execute_blender_code's dry_run diff watch - objects, meshes, materials,
    armatures, actions, images, collections, node_groups, shape_keys, curves,
    cameras, lights, textures, worlds, texts, scenes, plus bones, vertex
    groups and shape key blocks. A name this misses is a name a dry_run diff
    could not have reported as renamed either.

    With kind given, it looks in exactly one collection instead - the same
    one rename_items would target with that kind/object_name pair. This also
    doubles as the way to enumerate a collection rather than filter it: pass
    pattern="" (an explicit empty string) and every name matches, since every
    string contains "". This is the tool for "list every bone on this
    armature" or "list every shape key on this mesh" - there is no separate
    lister, because a filter with an always-true predicate already is one.

    Parameters:
    - pattern: A literal substring to search for. Omit it for the default
      check, "contains a non-ASCII character" - the leftover-CJK-after-
      translation case this exists for. Pass "" to match every name
      regardless of content (the enumeration case - pair it with kind, since
      the whole-file scan would otherwise dump the entire fingerprint). Pass
      a real string to check something narrower after a partial fix, e.g.
      "首" to confirm no bone, object or material anywhere still contains a
      character a substitutions pass was supposed to have replaced
      everywhere.
    - kind: Restrict the scan to one collection - the same vocabulary as
      rename_items' kind: a bpy.data collection ("object", "mesh",
      "material", "armature", ...) or one of "bone", "vertex_group",
      "shape_key", which need object_name.
    - object_name: Required with kind="bone"/"vertex_group"/"shape_key" -
      the armature or mesh that owns them, same meaning as in rename_items.
      Not meaningful without kind.
    - max_items: Cap on names listed per kind (default 50, 0 for all). Full
      counts are always in "count" per kind and total_matches regardless of
      the cap.

    A kind with no matches is left out of the report entirely rather than
    listed as zero.
    """
    result = get_blender_connection().send_command(
        "audit_names", {"pattern": pattern, "max_items": max_items,
                        "kind": kind, "object_name": object_name})

    total = result.get("total_matches", 0)
    if not total:
        if pattern:
            described = f'containing "{pattern}"'
        elif pattern == "":
            described = "at all"
        else:
            described = "with a non-ASCII character"
        scoped = f" in {kind} {object_name!r}" if kind and object_name else \
                 f" among {kind}s" if kind else ""
        return f"No names {described} found{scoped}."

    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def undo_edit(ctx: Context, steps: int = 1, all_steps: bool = False) -> str:
    """Take back edits this session made to the blend file, in place.

    The reverse gear, and restore_node_snapshot is not: undo puts a tree back
    as it was, keeping the identity objects and modifier inputs are bound to.
    Restoring builds a copy alongside, and repointing modifiers at it resets
    their saved values.

    Only steps this MCP session pushed can be taken back - it refuses rather
    than eating edits made in Blender itself. Can undo an annotate_node_tree,
    a restore_node_snapshot, or a labelled execute_blender_code, never the
    user's own work.

    Targeting is exact only right after writing - if the user edits in Blender
    first, theirs is the more recent step and comes off first. Re-read any tree
    afterwards rather than trusting an earlier read.

    Parameters:
    - steps: How many steps to take back. Defaults to 1, the last write.
    - all_steps: Take back everything this session still owns, ignoring
      `steps`. Escape hatch after a bulk edit spread over several calls, where
      undoing by hand means knowing what each call contributed. Same targeting
      caveat, more so the further back it walks.
    """
    result = get_blender_connection().send_command(
        "undo_edit", {"steps": steps, "all_steps": all_steps})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def list_node_trees(ctx: Context) -> str:
    """List every geometry node tree in the blend file.

    Returns name, node/link/frame counts and how many objects and other groups
    use each one. Start here, then call get_node_tree_outline on the one you
    care about.
    """
    result = get_blender_connection().send_command("list_node_trees")
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def get_node_tree_outline(ctx: Context, name: str) -> str:
    """Read the structure of a geometry node tree cheaply.

    Reports the group interface (with socket identifiers), the frames and what
    each contains, zone pairing, nested group dependencies, and how many
    objects use the tree. It does not report individual nodes or links - that
    detail is what makes a full read expensive, and the frame summary is
    usually the part you wanted.

    Frames matter more than they look: they carry the only human-written names
    in a typical tree, so they are the closest thing a node graph has to
    function names. Read the outline first and drill into one frame afterwards.

    Parameters:
    - name: Name of the geometry node group (see list_node_trees)
    """
    result = get_blender_connection().send_command(
        "get_node_tree_outline", {"name": name})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def get_node_detail(ctx: Context, name: str, frame: str = None) -> str:
    """Read the nodes and links of one region of a geometry node tree.

    This is the second half of the read: get_node_tree_outline says which
    frames exist and how big they are, and this opens one of them. Reading a
    whole tree this way is possible but is what the outline exists to avoid.

    Node settings and socket values left at their defaults are omitted, so what
    comes back is what someone actually chose. Sockets are referenced by name,
    with an index appended when the name is ambiguous - a Math node has three
    sockets called "Value", and for a SUBTRACT the operand order is the meaning,
    so "Value[0]" and "Value[1]" are distinguished on purpose.

    Parameters:
    - name: Name of the geometry node group (see list_node_trees)
    - frame: Frame label or node name to open. Omit to read the nodes that sit
      outside every frame - the only way into a tree that has no frames.
    """
    result = get_blender_connection().send_command(
        "get_node_detail", {"name": name, "frame": frame})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def validate_node_tree(ctx: Context, name: str, evaluate: bool = True,
                       max_objects: int = 8) -> str:
    """Check a geometry node tree and report everything wrong with it.

    Call this after anything that writes to a tree. A script that builds a
    graph reports success as long as it did not raise, which says nothing about
    whether the graph works - so this is the other half of every write.

    Reports invalid links, unpaired zones, a missing or unconnected Group
    Output, Blender's own per-node warnings (which live on the *modifier*, not
    the node), and the evaluated output geometry. That last one matters most: a
    tree that evaluates cleanly and emits nothing raises no warning at all, and
    zero vertices is the only evidence it went wrong.

    Also reports how many objects and modifiers use the tree. The modifier
    count is the one to respect before rebuilding anything - input values are
    stored per modifier, and an object can carry the same group twice.

    Parameters:
    - name: Name of the geometry node group
    - evaluate: Run the depsgraph to collect warnings and output counts. Turn
      off for a static-only check on a heavy scene.
    - max_objects: Cap on how many users are evaluated (default 8)
    """
    result = get_blender_connection().send_command(
        "validate_node_tree",
        {"name": name, "evaluate": evaluate, "max_objects": max_objects})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def snapshot_node_tree(ctx: Context, name: str, path: str = None,
                       keep_last: int = 10) -> str:
    """Save a geometry node tree as runnable Python, so a change can be undone.

    Writes the script that rebuilds the tree and every group it depends on, and
    returns where it was written - not the source. A snapshot is ~40x the size
    of the same tree's outline and is mostly boilerplate, so it is something to
    restore from, never something to read. Use get_node_tree_outline to
    understand a tree.

    Take one before editing anything you would mind losing.

    Parameters:
    - name: Name of the geometry node group
    - path: Where to write it. Defaults to a node_snapshots/ folder beside the
      .blend file, or the temp directory if the file was never saved.
    - keep_last: How many auto-named snapshots of this tree to keep, oldest
      deleted first (default 10; 0 keeps every one). Ignored when path is given.
    """
    result = get_blender_connection().send_command(
        "snapshot_node_tree",
        {"name": name, "path": path, "keep_last": keep_last})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def restore_node_snapshot(ctx: Context, path: str) -> str:
    """Rebuild geometry node trees from a snapshot file, and check the result.

    The trees come back as **new** node groups - Blender suffixes the names on
    collision - so this never overwrites a tree that objects are using. That
    also means restoring does not by itself undo an edit: it gives you a known
    good copy to compare against or to point a modifier at deliberately.

    Every restored tree is validated, so a snapshot that rebuilds into a broken
    graph says so rather than reporting success for having not raised.

    Parameters:
    - path: Path to a snapshot written by snapshot_node_tree
    """
    result = get_blender_connection().send_command(
        "restore_node_snapshot", {"path": path})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def annotate_node_tree(ctx: Context, name: str, labels: dict = None,
                       frames: list = None) -> str:
    """Write labels and frames onto a geometry node tree.

    Labels and frames do not affect evaluation, so this is the only write here
    that is safe on a group many objects depend on. It is also the highest
    value one: a typical tree has autogenerated names like Math.001 on most of
    its nodes, and frame labels are the closest thing a node graph has to
    function names.

    Annotations added this way are visible to whoever opens the .blend next, so
    this is not a private index - it is documentation, written where a person
    reading the canvas will find it.

    Every item is reported as applied or skipped. Node names shift as a tree is
    edited, so a stale name is likely rather than exotic, and a label that
    silently hit nothing is the failure worth catching.

    Parameters:
    - name: Name of the geometry node group
    - labels: {node_name: label}. A null label clears an existing one.
    - frames: [{"label": str, "nodes": [node_name, ...]}]. A frame whose label
      already exists is reused rather than duplicated, so re-running the same
      annotation is not destructive.
    """
    result = get_blender_connection().send_command(
        "annotate_node_tree",
        {"name": name, "labels": labels, "frames": frames})
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def get_viewport_screenshot(ctx: Context, max_size: int = 1000) -> Image:
    """Capture the active Blender 3D viewport as a PNG.

    Parameters:
    - max_size: Maximum pixel size of the largest dimension (default 1000)
    """
    blender = get_blender_connection()
    temp_path = os.path.join(
        tempfile.gettempdir(), f"blender_screenshot_{os.getpid()}.png")

    result = blender.send_command("get_viewport_screenshot", {
        "max_size": max_size,
        "filepath": temp_path,
        "format": "png",
    })
    if "error" in result:
        raise Exception(result["error"])
    if not os.path.exists(temp_path):
        raise Exception("Screenshot file was not created")

    try:
        with open(temp_path, "rb") as fh:
            image_bytes = fh.read()
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    return Image(data=image_bytes, format="png")


@mcp.tool()
def get_stderr_log(ctx: Context, max_chars: int = 8000, clear: bool = False) -> str:
    """Read what Blender has written to stderr this session.

    Blender sends operator tracebacks and addon errors to stderr, which normally
    only reaches the system console. Use this to see errors the user triggered
    by clicking in the UI, not just ones from execute_blender_code.

    Parameters:
    - max_chars: Return at most this many characters, newest last (default 8000)
    - clear: Empty the buffer after reading (default False)
    """
    result = get_blender_connection().send_command(
        "get_stderr_log", {"max_chars": max_chars, "clear": clear})

    text = result.get("text", "")
    if not text.strip():
        return "(nothing written to stderr this session)"

    header = f"[{result.get('chunks', 0)} chunks buffered"
    if result.get("truncated"):
        header += f", truncated to last {max_chars} chars"
    header += "]"
    return f"{header}\n{text}"


@mcp.tool()
def blender_docs(ctx: Context, query: str, max_chars: int = 6000) -> str:
    """Look something up in the offline Blender Python API reference and manual.

    Use this BEFORE guessing at an API, and before reading Blender's C++ source:
    these docs are version-stamped for the Blender this tooling drives, whereas
    source trees track main and describe APIs that do not exist yet. Needs no
    running Blender, so it also works when Blender is closed or busy.

    Answers "does this exist, what is it called, what type is it, what are the
    valid enum values". For a live value in the current scene, use
    execute_blender_code instead.

    Parameters:
    - query: A symbol (`bpy.types.ChildOfConstraint`, `set_inverse_pending`) or,
      failing that, a phrase to grep the manual prose for.
    - max_chars: Cap on the page text returned (default 6000)
    """
    if docs.docs_root() is None:
        raise Exception(
            "Offline Blender docs are not installed. Expected them under "
            "docs/Blender Documentation/ in the blender-mcp repo, or set "
            f"{docs.DOCS_ENV} to point at a copy.")

    _entries, version = docs.inventory()
    stamp = f"[{version}]" if version else "[offline Blender docs]"

    matches = docs.find_symbols(query)
    if matches:
        api = docs.api_dir()
        name, kind, page = matches[0]
        out = [f"{stamp} {name} ({kind})", ""]
        out.append(docs.page_text(api / page, max_chars=max_chars))
        if len(matches) > 1:
            out.append("")
            out.append(f"Other matches for {query!r}:")
            out.extend(f"  {n} ({k})" for n, k, _ in matches[1:15])
        return "\n".join(out)

    hits = docs.search_prose(query)
    if not hits:
        return (f"{stamp} nothing found for {query!r}. Symbol lookup is exact-ish "
                "-- try the unqualified name, or a phrase from the manual.")
    out = [f"{stamp} no symbol named {query!r}; found it in the prose:", ""]
    for path, snippet in hits:
        out.append(f"--- {path}")
        out.append(snippet)
        out.append("")
    return "\n".join(out)


def main():
    """Run the MCP server."""
    # When run by hand (stdin is a TTY) the server appears to "hang" while it
    # silently waits for an MCP client; log a hint so that state is obvious.
    # Launched by a client, stdin is a pipe so this is skipped, and logging goes
    # to stderr, never to the stdio protocol on stdout.
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if interactive:
        logger.info(
            "BlenderDevMCP is an MCP server, meant to be launched by an MCP "
            "client rather than run by hand. It will now wait silently for a "
            "client on stdin - that is normal, not a hang. Ctrl-C to exit.")
    mcp.run()


if __name__ == "__main__":
    main()
