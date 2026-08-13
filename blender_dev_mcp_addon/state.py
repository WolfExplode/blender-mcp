"""What changed in the blend file, as a before/after comparison.

This exists so a write can be *previewed*. `execute_blender_code` takes
arbitrary Python, which cannot be statically analysed for what it will touch,
so the only honest way to answer "what would this do" is to do it, look, and
put it back. That is `dry_run` in addon.py; this module is the "look" half.

Identity is the whole trick. Comparing names alone cannot tell a rename from a
delete-plus-create, and renaming is the single most common bulk edit - so
datablocks are keyed by `as_pointer()` instead. Same pointer with a different
name is a rename, and that is reported exactly rather than as 251 deletions
next to 251 creations.

Two limits worth knowing, neither of them fixable from here:

  - `as_pointer()` is an allocation address, so a pointer freed during the
    edit may be handed straight back to something new. Within a single
    execution that is rare enough to accept, and the failure mode is a
    mislabelled line in a report, not a wrong edit.

  - Only the datablock namespace is compared - names, and what exists. Vertex
    coordinates, transforms and property values are not, because fingerprinting
    those on a production scene costs more than the whole edit. A dry run
    therefore proves what was *created, deleted or renamed*; it does not prove
    a value assignment happened.
"""

import bpy

# Collections worth watching. Not every one bpy.data offers: this is the set
# whose names people actually read and rename, kept short because the whole
# fingerprint is taken twice per dry run.
DATABLOCKS = (
    "objects", "meshes", "materials", "armatures", "actions", "images",
    "collections", "node_groups", "shape_keys", "curves", "cameras", "lights",
    "textures", "worlds", "texts", "scenes",
)

# Named things that live *inside* a datablock rather than in bpy.data. These
# matter more than their obscurity suggests: renaming a bone renames vertex
# groups across every bound mesh, which is exactly the blast radius someone
# would want to see before agreeing to it.
def _subitems():
    """{kind: {pointer: name}} for named sub-collections."""
    groups, bones, keys = {}, {}, {}
    for obj in bpy.data.objects:
        for vgroup in obj.vertex_groups:
            groups[vgroup.as_pointer()] = vgroup.name
    for armature in bpy.data.armatures:
        for bone in armature.bones:
            bones[bone.as_pointer()] = bone.name
    for shape_key in bpy.data.shape_keys:
        for block in shape_key.key_blocks:
            keys[block.as_pointer()] = block.name
    return {"vertex_groups": groups, "bones": bones, "shape_keys.key_blocks": keys}


def fingerprint(subitems=True):
    """{kind: {pointer: name}} for everything named in the file."""
    snapshot = {}
    for name in DATABLOCKS:
        collection = getattr(bpy.data, name, None)
        if collection is None:
            continue  # a build without this collection; not worth failing over
        snapshot[name] = {db.as_pointer(): db.name for db in collection}
    if subitems:
        snapshot.update(_subitems())
    return snapshot


def diff(before, after):
    """What changed between two fingerprints, as {kind: {created/deleted/renamed}}.

    Kinds that did not change are omitted entirely, so an edit that touched
    only bones reports only bones rather than a wall of zeroes.
    """
    changes = {}
    for kind in sorted(set(before) | set(after)):
        old, new = before.get(kind, {}), after.get(kind, {})

        created = sorted(new[p] for p in new.keys() - old.keys())
        deleted = sorted(old[p] for p in old.keys() - new.keys())
        renamed = sorted(
            (old[p], new[p]) for p in old.keys() & new.keys() if old[p] != new[p])

        if created or deleted or renamed:
            entry = {}
            if created:
                entry["created"] = created
            if deleted:
                entry["deleted"] = deleted
            if renamed:
                entry["renamed"] = [{"from": a, "to": b} for a, b in renamed]
            changes[kind] = entry
    return changes


def summarise(changes, max_items=100):
    """Trim a diff for reporting, and count what was trimmed.

    A bulk rename produces hundreds of near-identical lines whose shape is
    obvious from the first few. Returning all of them buries the one kind of
    change the caller did not expect, which is the entire reason to look at a
    diff at all - so each list is capped and the remainder is counted.

    The cap is deliberately high enough that a hand-authored edit is never cut.
    Truncation is for the 400-bone rename, not for a mesh's worth of shape
    keys, and a preview that hid a third of a 34-item change would be teaching
    the caller to distrust it.

    What is shown when the cap *does* bite is a head **and tail** sample, not
    the first N. The lists are sorted by name, so cutting only the tail drops
    whatever sorts last - and an unexpected `zzz_tmp` at the end of the
    alphabet is precisely the entry this function claims to protect. Sampling
    both ends costs nothing and cannot silently hide one extreme.

    max_items < 0 drops every item and keeps only "totals" - for a change
    spanning several hundred items across several kinds (a whole-file rename),
    even the head-and-tail sample multiplied across every kind can be wide
    enough to exceed a caller's own output budget before max_items=0 is ever
    reached. There is no item list to check by eye at that scale anyway; the
    counts are the useful signal, and a second labelled call with a narrow
    max_items (or a targeted read afterwards) covers spot-checking specific
    names.
    """
    trimmed, totals = {}, {}
    for kind, entry in changes.items():
        out = {}
        for action, items in entry.items():
            totals[f"{kind}.{action}"] = len(items)
            if max_items is not None and max_items < 0:
                out[f"{action}_omitted"] = len(items)
            elif max_items and len(items) > max_items:
                head = -(-max_items // 2)  # ceil, so odd caps favour the head
                tail = max_items - head
                out[action] = items[:head] + (items[-tail:] if tail else [])
                out[f"{action}_omitted"] = len(items) - max_items
            else:
                out[action] = items
        trimmed[kind] = out
    return {"changed": trimmed, "totals": totals} if trimmed else {"changed": {}}


def is_empty(changes):
    """True when nothing in the watched namespace moved."""
    return not changes
