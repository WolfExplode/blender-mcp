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

The push bookkeeping exists so that undoing can be bounded. Blender 5.1 exposes
no way to read the undo stack, so the only defence against walking back into the
user's own edit history is to refuse to take more steps than we contributed -
and, because a revert point can occupy more than one stack entry, to know how
many each of ours cost.

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

# What this session pushed and has not yet consumed: one entry per revert point
# a caller can ask for, holding how many *raw* undo-stack entries that revert
# point occupies. Module level rather than per-connection, because the undo
# stack is per-Blender and anything narrower would lose track across a reconnect.
#
# A list rather than a count because the cost is not uniform. A labelled
# execute_code pushes a boundary step as well as its own, where annotate and
# restore push only one - so "undo three edits" is not "take three steps", and
# treating it as such silently under-reverts. Measured on Blender 5.1: reverting
# K labelled execute_code edits takes 2K-1 raw steps, not K.
_pushed = []

# Boundary pushes seen since the last counted one. They belong to the revert
# point that follows them, and are attributed to it when it arrives.
_pending = 0


def push(message, counts=True):
    """Record the current state as a revert point. True if Blender took it.

    Call this *after* the mutation - see the module docstring for why before
    does not work.

    `counts=False` is for the defensive push taken before an edit, which exists
    to isolate any earlier unpushed drift into its own step rather than to be
    a step anyone will deliberately return to. It is not a revert point of its
    own, but it does occupy a stack entry, so it is charged to the edit it
    precedes rather than ignored.
    """
    global _pushed, _pending
    try:
        bpy.ops.ed.undo_push(message=message)
    except (RuntimeError, AttributeError):
        # Some contexts have no undo stack at all. A write that cannot be
        # registered is still a write worth completing, so this is reported
        # rather than raised.
        return False
    if counts:
        _pushed.append(1 + _pending)
        _pending = 0
    else:
        _pending += 1
    return True


def budget():
    """How many revert points `undo` is currently willing to take back."""
    return len(_pushed)


def _raw_steps(count):
    """Raw undo steps needed to take back the newest `count` revert points.

    Every entry costs its own stack slots, except that the oldest one's
    boundary is the state being returned *to* - it is landed on, not stepped
    past. That is the -1, and without it a full revert overshoots into whatever
    came before, which on a user's file is their work.
    """
    entries = _pushed[-count:]
    raw = sum(entries)
    if entries[0] > 1:
        raw -= 1
    return raw


def undo(steps=1, all_steps=False):
    """Step back through revert points this session pushed.

    Refuses to exceed the push count. The counter is the only guard available,
    and it is a bound on damage rather than a guarantee of correctness: if the
    user edits in the Blender UI between an MCP write and this call, their
    action is the more recent step and it is the one that comes off first.
    Undoing right after writing is the only usage that is precisely targeted.

    `all_steps` takes back everything this session pushed and still owns. It is
    the "get me out of this" path: after a bulk edit spread over several calls,
    unwinding by hand means knowing how many steps each call contributed, and
    guessing low leaves a half-reverted file. The same targeting caveat applies,
    more so - the further back it walks, the likelier a user edit is in the way.
    """
    global _pushed, _pending
    held = len(_pushed)
    if all_steps:
        if held < 1:
            return {"undone": 0, "requested": 0, "remaining_budget": 0,
                    "note": "Nothing to undo: this session pushed no revert points."}
        steps = held
    if steps < 1:
        raise ValueError(f"steps must be at least 1, got {steps}")
    if held < 1:
        raise ValueError(
            "Nothing to undo: this session has not pushed any revert points. "
            "Undoing anyway would step into edits made in Blender itself, "
            "which are the user's to unwind.")
    if steps > held:
        raise ValueError(
            f"Asked to undo {steps} steps but only {held} were pushed by "
            "this session. Refusing, so that the user's own edit history is "
            "not consumed.")

    # One revert point is not one stack step, so the loop runs over raw steps
    # and the budget is settled afterwards from what actually landed.
    wanted_raw = _raw_steps(steps)
    taken_raw = 0
    error = None
    for _ in range(wanted_raw):
        try:
            bpy.ops.ed.undo()
        except (RuntimeError, AttributeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            break
        taken_raw += 1

    # Ownership is given up either way. On a clean run that is bookkeeping; on a
    # short one it is deliberate - the file then sits between revert points, the
    # entries no longer describe the stack, and a wrong count is worse than none
    # because it would send a later undo walking into the user's own history.
    undone = steps if taken_raw == wanted_raw else 0
    del _pushed[-steps:]
    _pending = 0

    result = {
        "undone": undone,
        "requested": steps,
        "raw_steps": taken_raw,
        "remaining_budget": len(_pushed),
        # Worth stating in the result: a caller that cached a tree or node from
        # before this call is now holding dead pointers.
        "note": ("References taken before this call are now invalid; "
                 "re-read the tree by name to see the reverted state."),
    }
    if error:
        result["error"] = error
        result["note"] = (
            f"Undo stopped after {taken_raw} of {wanted_raw} steps, so the file "
            "is part-way between revert points. This session has given up "
            "tracking them; check the state before writing again.")
    return result


def reset():
    """Forget what was pushed. For tests, and for a file load that voids it."""
    global _pushed, _pending
    _pushed = []
    _pending = 0
