"""Making MCP writes revertible, using Blender's own undo stack.

Undo is the right substrate for this and snapshots are not. Undo restores a
tree *in place*, so objects keep pointing at it and its interface socket
identifiers survive. Restoring a snapshot builds a new tree beside the original
with freshly issued identifiers, so recovering a shared group means repointing
every modifier at the copy - which silently resets each one's saved input
values. That is the same damage the bad edit would have done, which makes a
snapshot a poor rollback and a good durable reference copy. Both tools exist;
only this one is the reverse gear.

Measured on Blender 5.1, because none of it is guessable from the docs:

  - The push *after* the mutation is what makes it revertible. Pushing only
    before the edit does not work - the edit survives the undo. So every write
    here pushes once the change is made, not before it. Blender's source gives
    the reason: memfile_undosys_step_encode() serialises bmain at push time, so
    a step holds the state as of its own push and undo lands on the previous
    one. (reference/blender-main/blender/source/blender/editors/undo/)

  - ``bpy.ops.ed.undo.poll()`` returns False in background mode while
    ``bpy.ops.ed.undo()`` itself succeeds and reverts correctly. poll is
    therefore useless as a guard and is not consulted anywhere.

  - Undo invalidates every Python reference to a datablock: touching one
    afterwards raises ReferenceError("StructRNA ... has been removed"). Nothing
    may hold a handle across an undo; re-fetch by name on the far side.

The push counter exists so that undoing can be bounded. Blender 5.1 exposes no
way to read the undo stack, so the only defence against walking back into the
user's own edit history is to refuse to take more steps than we contributed.

That is a version-scoped limitation, not a permanent one. Blender's main branch
adds rna_wm_undo.cc, giving `wm.undo_stack` with `.steps`, `.active_index` and a
`.name` per step. Where that exists the guard can be made exact rather than
merely bounded: read the name of the step an undo would take off and refuse
unless we pushed it, which closes the one hole documented on `undo` below - that
a user's UI edit made after ours is the step that comes off first. Checked
against 5.1 and it is genuinely absent there: `WindowManager.bl_rna.properties`
has no `undo_stack`, and `bpy.types.UndoStack` does not exist. Worth revisiting
on upgrade; a `getattr(wm, "undo_stack", None)` probe is enough to detect it.
"""

import bpy

# How many revert points this session has pushed and not yet consumed. Module
# level rather than per-connection: the undo stack is per-Blender, so a counter
# scoped to anything narrower would lose track across a reconnect.
_pushed = 0


def push(message, counts=True):
    """Record the current state as a revert point. True if Blender took it.

    Call this *after* the mutation - see the module docstring for why before
    does not work.

    `counts=False` is for the defensive push taken before an edit, which exists
    to isolate any earlier unpushed drift into its own step rather than to be
    a step anyone will deliberately return to. Counting it would let `undo`
    take two steps for one edit.
    """
    global _pushed
    try:
        bpy.ops.ed.undo_push(message=message)
    except (RuntimeError, AttributeError):
        # Some contexts have no undo stack at all. A write that cannot be
        # registered is still a write worth completing, so this is reported
        # rather than raised.
        return False
    if counts:
        _pushed += 1
    return True


def budget():
    """How many steps `undo` is currently willing to take."""
    return _pushed


def undo(steps=1):
    """Step back through revert points this session pushed.

    Refuses to exceed the push count. The counter is the only guard available,
    and it is a bound on damage rather than a guarantee of correctness: if the
    user edits in the Blender UI between an MCP write and this call, their
    action is the more recent step and it is the one that comes off first.
    Undoing right after writing is the only usage that is precisely targeted.
    """
    global _pushed
    if steps < 1:
        raise ValueError(f"steps must be at least 1, got {steps}")
    if _pushed < 1:
        raise ValueError(
            "Nothing to undo: this session has not pushed any revert points. "
            "Undoing anyway would step into edits made in Blender itself, "
            "which are the user's to unwind.")
    if steps > _pushed:
        raise ValueError(
            f"Asked to undo {steps} steps but only {_pushed} were pushed by "
            "this session. Refusing, so that the user's own edit history is "
            "not consumed.")

    taken = 0
    for _ in range(steps):
        try:
            bpy.ops.ed.undo()
        except (RuntimeError, AttributeError) as exc:
            return {
                "undone": taken,
                "requested": steps,
                "remaining_budget": _pushed,
                "error": f"{type(exc).__name__}: {exc}",
            }
        taken += 1
        _pushed -= 1

    return {
        "undone": taken,
        "requested": steps,
        "remaining_budget": _pushed,
        # Worth stating in the result: a caller that cached a tree or node from
        # before this call is now holding dead pointers.
        "note": ("References taken before this call are now invalid; "
                 "re-read the tree by name to see the reverted state."),
    }


def reset():
    """Forget the push count. For tests, which share one Blender session."""
    global _pushed
    _pushed = 0
