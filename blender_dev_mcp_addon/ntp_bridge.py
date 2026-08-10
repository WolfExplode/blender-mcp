"""Snapshot and restore geometry node trees, via NodeToPython.

A snapshot here is generated Python: the source that rebuilds a tree and every
group it depends on. That format is chosen for one property above all - it is
executable, so restoring needs no parser of ours, only exec.

It is emphatically not a reading format. Measured on this scene, a 72-node tree
is ~425 tokens as an outline and ~16,000 as a snapshot, roughly 85% of it
boilerplate property assignment. So snapshots are written to disk and the tool
returns a path, never the source. Reading a tree is what geonodes.py is for.

A snapshot is a durable reference copy, not a rollback, and the difference
matters. Restoring rebuilds the tree beside the original with freshly issued
interface socket identifiers, so putting a shared group back means repointing
every modifier at the copy - which resets each one's saved input values, the
same damage a bad edit would have done. Taking an edit back in place is
undo.py's job. What this module is for is keeping a known-good version of a
tree that outlives the session, the undo stack, and the .blend being reopened.

Restoring is safe by construction: the generated script always calls
bpy.data.node_groups.new(), so it creates a new tree beside the original rather
than overwriting it. Nothing here can destroy a tree that objects are using -
the danger is only in what someone does with the restored copy afterwards, and
that is a separate, deliberate act.

NodeToPython is an optional dependency. When it is missing these tools report
that clearly instead of failing at import, so the rest of the addon still works.
"""

import datetime
import os
import sys

import bpy

# Where to find a NodeToPython checkout. The environment variable wins so this
# is not hardcoded to one machine; the default is the usual clone location, and
# the installed extension is the last resort.
NTP_PATH_ENV = "BLENDER_MCP_NTP_PATH"
DEFAULT_NTP_PATHS = (
    os.path.expanduser(r"~\Documents\GitHub\NodeToPython"),
    os.path.expanduser("~/Documents/GitHub/NodeToPython"),
)


class NTPUnavailable(RuntimeError):
    """NodeToPython could not be imported, with the paths that were tried."""


def _ntp_roots():
    configured = os.environ.get(NTP_PATH_ENV)
    if configured:
        yield configured
    for path in DEFAULT_NTP_PATHS:
        yield path


def load_exporter():
    """Import NodeToPython's headless exporter, or say why it is unavailable.

    Imported lazily rather than at module load: a missing NodeToPython should
    cost the snapshot tools and nothing else, and an addon that fails to
    register because an optional dependency is absent is worse than one that
    reports the absence when asked.
    """
    tried = []
    for root in _ntp_roots():
        tried.append(root)
        if not os.path.isdir(os.path.join(root, "NodeToPython")):
            continue
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from NodeToPython.export.headless import export_to_string
            return export_to_string
        except ImportError as exc:
            tried[-1] = f"{root} (import failed: {exc})"

    # The extension install is a fallback, and only helps if that copy carries
    # the headless module - the stock one does not.
    try:
        from bl_ext.blender_org.node_to_python.export.headless import (
            export_to_string)
        return export_to_string
    except ImportError:
        tried.append("bl_ext.blender_org.node_to_python (no headless module)")

    raise NTPUnavailable(
        "NodeToPython's headless exporter was not found. Set the "
        f"{NTP_PATH_ENV} environment variable to a checkout containing "
        f"NodeToPython/export/headless.py. Tried: {tried}")


def snapshot_dir():
    """Where snapshots go: beside the .blend, or temp if it was never saved."""
    blend = bpy.data.filepath
    if blend:
        return os.path.join(os.path.dirname(blend), "node_snapshots")
    import tempfile
    return os.path.join(tempfile.gettempdir(), "blender_mcp_node_snapshots")


def _prune(directory, prefix, keep_last):
    """Delete all but the newest `keep_last` auto-named snapshots of one tree.

    Snapshots are meant to be taken before every risky edit, so without this
    they grow monotonically forever beside the user's .blend. Only files this
    module named itself are considered - the prefix is the sanitised tree name
    and the suffix is .py - so an explicit `path=` is never touched and neither
    is anything else that happens to live in the directory.
    """
    if not keep_last or keep_last < 1:
        return []
    try:
        candidates = sorted(
            entry for entry in os.listdir(directory)
            if entry.startswith(prefix + "-") and entry.endswith(".py"))
    except OSError:
        return []

    removed = []
    # Names end in a sortable -YYYYmmdd-HHMMSS stamp, so lexical order is
    # chronological and the tail is the newest.
    for entry in candidates[:-keep_last]:
        try:
            os.remove(os.path.join(directory, entry))
            removed.append(entry)
        except OSError:
            # A snapshot that will not delete is not worth failing the
            # snapshot that was actually asked for.
            pass
    return removed


def snapshot_tree(name, path=None, keep_last=10):
    """Write the Python that rebuilds `name` to disk; return where it went.

    The source is deliberately not returned. It is large, it is a build
    artifact, and a caller that wanted to understand the tree should be reading
    the outline instead.

    `keep_last` bounds how many auto-named snapshots of this tree survive;
    pass 0 to keep every one. It has no effect when `path` is given, since an
    explicitly named file is the caller's to manage.
    """
    export_to_string = load_exporter()

    tree = bpy.data.node_groups.get(name)
    if tree is None:
        known = [t.name for t in bpy.data.node_groups
                 if t.bl_idname == "GeometryNodeTree"]
        raise ValueError(
            f"No node group named {name!r}. Geometry node trees in this file: "
            f"{known}")

    source, messages = export_to_string(tree)

    pruned = []
    if path is None:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        directory = snapshot_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{safe}-{stamp}.py")
    else:
        path = bpy.path.abspath(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        safe = directory = None

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(source)

    # Pruned after the write, never before: losing an old snapshot is cheap,
    # but losing it and then failing to produce the new one is not.
    if directory is not None:
        pruned = _prune(directory, safe, keep_last)

    result = {
        "tree": tree.name,
        "path": path,
        "characters": len(source),
        "lines": source.count("\n") + 1,
        # Nested groups are emitted alongside, so a restore does not depend on
        # the rest of the file still being in the state it was captured from.
        "dependencies": sorted({
            n.node_tree.name for n in tree.nodes
            if getattr(n, "node_tree", None) is not None}),
        "exporter_messages": messages,
    }
    if pruned:
        result["pruned"] = pruned
    return result


def restore_snapshot(path, undo_push=True):
    """Run a snapshot, returning the trees it created.

    The script creates new node groups rather than overwriting existing ones,
    so this never touches a tree that objects are using. Blender resolves the
    name collision by suffixing, which is why the created names are reported
    back - they are usually not the names in the snapshot.

    That safety is also the limit of what this can do: it produces a copy, not
    a rollback. Undoing an edit in place is undo_edit's job. The addon passes
    undo_push=False and pushes through its own undo module so that this restore
    is counted as a step that can be taken back.
    """
    path = bpy.path.abspath(path)
    if not os.path.isfile(path):
        raise ValueError(f"No snapshot at {path!r}")

    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    before = {g.name for g in bpy.data.node_groups}
    # __name__ is set to "__main__" on purpose: the generated script guards its
    # entry point that way, and without it the file defines its functions and
    # then does nothing at all - a silent no-op that looks like success.
    namespace = {"__name__": "__main__"}
    exec(compile(source, path, "exec"), namespace)

    created = [g.name for g in bpy.data.node_groups if g.name not in before]

    if undo_push:
        # A restore adds real datablocks to the user's file, so it should be a
        # step they can Ctrl-Z like any other edit. Wrapped because not every
        # context has an undo stack - though background mode does, contrary to
        # what an earlier version of this comment claimed.
        try:
            bpy.ops.ed.undo_push(message=f"MCP restore {os.path.basename(path)}")
        except (RuntimeError, AttributeError):
            pass

    return {
        "path": path,
        "created": created,
        "note": "Restored as new node groups; nothing existing was replaced.",
    }
