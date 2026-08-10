"""MCP server exposing a running Blender for addon development and debugging.

Talks to the companion Blender addon over a local socket. Five tools: inspect
the scene, inspect an object, run Python, grab the viewport, read stderr.

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
                    raise Exception("Connection closed before receiving any data")
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
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Blender")

        command = {"type": command_type, "params": params or {}}
        try:
            logger.info(f"Sending command: {command_type}")
            self.sock.sendall(json.dumps(command).encode("utf-8"))
            self.sock.settimeout(RECV_TIMEOUT)
            response = json.loads(
                self.receive_full_response(self.sock).decode("utf-8"))

            if response.get("status") == "error":
                raise Exception(response.get("message", "Unknown error from Blender"))
            return response.get("result", {})

        except socket.timeout:
            # Invalidate the socket so the next call reconnects.
            self.sock = None
            raise Exception(
                f"Timeout after {RECV_TIMEOUT:g}s waiting for Blender. If Blender "
                "is running headless (blender -b), commands never execute - run "
                "it with a GUI. If the command is genuinely slow, raise "
                "BLENDER_MCP_TIMEOUT (seconds).")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as exc:
            self.sock = None
            raise Exception(f"Connection to Blender lost: {exc}")
        except json.JSONDecodeError as exc:
            self.sock = None
            raise Exception(f"Invalid response from Blender: {exc}")
        except IncompleteResponse as exc:
            # Previously this escaped uncaught, leaving self.sock in place. The
            # stream was already desynced, so every later command read the
            # wrong reply until the process restarted.
            self.sock = None
            raise Exception(f"Truncated response from Blender: {exc}")


_blender_connection = None


def get_blender_connection() -> BlenderConnection:
    """Get or create the persistent Blender connection."""
    global _blender_connection

    # Reuse the existing connection. We deliberately do NOT probe it with a
    # command here: that put two commands on the wire for every tool call, and
    # any overlap desynced the response stream until the socket timeout fired.
    # A dead socket is detected by the next real command and reconnected then.
    if _blender_connection is not None and _blender_connection.sock is not None:
        return _blender_connection

    if _blender_connection is None:
        host = os.getenv("BLENDER_HOST", DEFAULT_HOST)
        port = int(os.getenv("BLENDER_PORT", DEFAULT_PORT))
        _blender_connection = BlenderConnection(host=host, port=port)
        if not _blender_connection.connect():
            _blender_connection = None
            # Name the address: the port is settable both in the addon panel and
            # via BLENDER_PORT, so "refused" is otherwise ambiguous between
            # "Blender is closed" and "the two ends disagree on the port".
            raise Exception(
                f"Could not connect to Blender at {host}:{port}. Make sure "
                "Blender is running with the Blender Dev MCP addon enabled, and "
                "that the port matches the one in View3D > Sidebar > Dev MCP.")
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
    active object, and a truncated object list.

    Parameters:
    - max_objects: How many objects to list (default 10)
    """
    result = get_blender_connection().send_command(
        "get_scene_info", {"max_objects": max_objects})
    return json.dumps(result, indent=2)


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
def execute_blender_code(ctx: Context, code: str, undo_label: str = None) -> str:
    """Execute Python inside Blender and return everything it printed.

    Captures stdout and stderr. If the code raises, the error includes both the
    output produced before the exception and the full traceback.

    Parameters:
    - code: Python source to execute. `bpy`, `bmesh`, `mathutils` and `math`
      are already in scope. Runs with __name__ set to "<blender_dev_mcp>", so an
      `if __name__ == "__main__"` block will not fire on its own.
    - undo_label: Set this whenever the code changes anything, to a short
      description of the change ("add curve resample", "relink noise input").
      It registers the edit as one step, which makes it a single Ctrl-Z for the
      user and lets undo_edit take it back. Leave it unset for code that only
      reads - an unlabelled edit cannot be undone through this tooling.
    """
    result = get_blender_connection().send_command(
        "execute_code", {"code": code, "undo_label": undo_label})
    return result.get("result", "") or "(no output)"


@mcp.tool()
def undo_edit(ctx: Context, steps: int = 1) -> str:
    """Take back edits this session made to the blend file, in place.

    This is the reverse gear, and restore_node_snapshot is not: undo puts a
    tree back as it was, keeping the identity that objects and modifier inputs
    are bound to. Restoring builds a copy alongside, and repointing modifiers
    at that copy resets their saved values.

    Only steps this MCP session pushed can be taken back - it refuses rather
    than eating edits made in Blender itself. So it can undo an
    annotate_node_tree, a restore_node_snapshot, or an execute_blender_code
    that carried an undo_label, but never the user's own work.

    Targeting is only exact if you undo directly after writing. If the user
    edits in Blender in between, theirs is the more recent step and it comes
    off first. Re-read any tree afterwards rather than trusting an earlier read.

    Parameters:
    - steps: How many steps to take back. Defaults to 1, the last write.
    """
    result = get_blender_connection().send_command("undo_edit", {"steps": steps})
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
