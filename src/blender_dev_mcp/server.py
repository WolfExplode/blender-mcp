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
    "get_viewport_screenshot",
    "get_stderr_log",
    "list_node_trees",
    "get_node_tree_outline",
    "get_node_detail",
    "validate_node_tree",
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
def execute_blender_code(ctx: Context, code: str, undo_label: str = None,
                         dry_run: bool = False, rollback_on_error: bool = True,
                         max_diff_items: int = 100) -> str:
    """Execute Python inside Blender and return everything it printed.

    Captures stdout and stderr. If the code raises, the error includes both the
    output produced before the exception and the full traceback.

    This is the general-purpose tool here, and deliberately so: the read tools
    answer fixed questions, and anything else is a few lines of bpy. Reach for
    it freely to read; use dry_run before it writes.

    Parameters:
    - code: Python source to execute. `bpy`, `bmesh`, `mathutils` and `math`
      are already in scope. Runs with __name__ set to "<blender_dev_mcp>", so an
      `if __name__ == "__main__"` block will not fire on its own.
    - undo_label: Set this whenever the code changes anything, to a short
      description of the change ("add curve resample", "relink noise input").
      It registers the edit as one step, which makes it a single Ctrl-Z for the
      user and lets undo_edit take it back. Leave it unset for code that only
      reads - an unlabelled edit cannot be undone through this tooling.
    - dry_run: Run the code, report what it changed, then put it back. Use this
      before any bulk write - it is the difference between proposing an edit and
      making one, and on someone's open unsaved file that difference is the
      whole game. What comes back is a diff of what was created, deleted and
      renamed. Three limits: it compares names and existence only, so a pure
      value assignment shows as no change; it watches bpy.data plus vertex
      groups, bones and shape keys, so state an addon keeps in its own
      PropertyGroup collections (mmd_root.vertex_morphs, rig metadata, modifier
      settings) is invisible - and when an addon binds its records to datablocks
      *by name*, a rename is exactly the edit whose risky half will not appear;
      and it cannot take back writes outside the blend file, so code that saves,
      exports or deletes on disk is NOT made safe by it. Print the post-state
      yourself for anything the diff cannot see.
    - rollback_on_error: When labelled code raises partway, take back what it
      already did (default true). Turn it off only to inspect the wreckage of a
      half-applied edit.
    - max_diff_items: Cap on entries listed per change kind (default 100; 0 for
      no cap). High enough that hand-authored edits are never cut. Past the cap
      you get a head-and-tail sample rather than the first N, because the lists
      are name-sorted and the unexpected entry is as likely to sort last as
      first. The full counts are always reported under "totals".
    """
    result = get_blender_connection().send_command(
        "execute_code", {"code": code, "undo_label": undo_label,
                         "dry_run": dry_run,
                         "rollback_on_error": rollback_on_error,
                         "max_diff_items": max_diff_items})

    output = result.get("result", "") or "(no output)"
    if not result.get("dry_run"):
        return output

    changed = result.get("changed") or {}
    report = ["[dry run - nothing was kept]" if result.get("reverted")
              else "[dry run - WARNING: could not be reverted, the change is "
                   f"still applied: {result.get('revert_error')}]"]
    if changed:
        report.append(json.dumps({"changed": changed,
                                  "totals": result.get("totals", {})},
                                 indent=2, ensure_ascii=False))
    else:
        report.append(
            "No datablock was created, deleted or renamed. If the code was "
            "meant to assign values rather than rename things, that is expected "
            "- a dry run cannot see it. Verify by reading the values back.")
    if output != "(no output)":
        report.append("--- output ---")
        report.append(output)
    return "\n".join(report)


@mcp.tool()
def undo_edit(ctx: Context, steps: int = 1, all_steps: bool = False) -> str:
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
    - all_steps: Take back everything this session pushed and still owns,
      ignoring `steps`. The escape hatch after a bulk edit spread over several
      calls, where undoing by hand means knowing what each call contributed and
      guessing low leaves the file half-reverted. The targeting caveat above
      applies more strongly the further back it walks.
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
