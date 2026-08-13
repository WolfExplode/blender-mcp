# Blender Undo/Redo — Personal Notes

Personal reference on how Blender's undo/redo system actually behaves, written up
after hitting the same class of bug more than once across different addons. Not
part of the official manual (this whole folder is gitignored) — just notes for
future-me.

## Mechanics

Blender's global undo ("memfile undo") is a full-state snapshot system, not a
delta/command log. Each undo step is a snapshot of the entire `bpy.data` state at
the moment the step was pushed; unchanged ID blocks are shared by reference with
the previous step, changed ones get a fresh copy. There is no per-property diffing
— if *any* field on an ID block changed since the last step, the whole block is
treated as new in the next snapshot.

**When a step gets pushed:** only when something explicitly triggers it —
an operator whose `bl_options` includes `'UNDO'` (or `'REGISTER'` for tool
operators), or an explicit `bpy.ops.ed.undo_push()` call. Plain Python property
assignment (`some_id.some_prop = value`) never pushes a step by itself.

**What counts as undo-tracked "ID data":** normal `bpy.data` blocks — `Scene`,
`Object`, `Mesh`, `Material`, etc. Properties on these are captured in the next
snapshot no matter how they were set (operator, modal loop, raw script).

**What does *not* get undo-tracked:** properties on `Screen`, `WindowManager`,
`Brush`, and `WorkSpace` data (this includes `View3DShading`, which lives on
`SpaceView3D`, itself owned by `Screen`). Verified empirically: continuously
writing `space.shading.studiolight_rotate_z` during a modal drag survives being
undone/redone unchanged, even when unrelated undoable operators run in between.
This is *why* viewport shading type, shading-popover settings, panel layout,
etc. are never part of Undo History in vanilla Blender.

**The bleed-through bug:** if you mutate a real ID property (e.g.
`scene.display.light_direction`) directly during a modal operator with no
`'UNDO'` option and no explicit push, the change sits "uncommitted" in the live
state. The *next* time anything else pushes an undo step, that push's snapshot
already includes your change (since it diffs the whole ID block, not just what
the pushing operator touched). Undo the *unrelated* action later, and Blender
reverts to the step *before* that snapshot — which predates your change too.
Net effect: undoing something else silently reverts your change as a side
effect, with no dedicated entry ever appearing in the Undo History to explain
it. Verified directly against a live Blender session: set
`scene.display.light_direction` via plain assignment, ran an unrelated
undoable operator, then undid it — the light direction reverted along with it.

**Two ways to stop that bleed, with different trade-offs:**
1. Give the change its own explicit `bpy.ops.ed.undo_push(message=...)` when it
   settles. Isolates it from bleeding into *other* steps, but it now shows up
   as its own entry in Undo History — undoing far enough back will revert it,
   and repeated triggers (e.g. per mouse-move) can spam the history if not
   bracketed carefully (see mio3_shape_keys entry below).
2. Never let the "true" value live only in undo-tracked ID data. Keep it
   in Python-side state (a module-level dict, not saved with the file) and
   register `undo_post`/`redo_post` handlers that re-apply the cached value
   to the ID property after *any* undo/redo fires. Zero History entries, and
   the value can't be reverted by unrelated undo/redo since it gets re-pinned
   immediately after. Downside: the addon must remember to re-seed the cache
   on file load (`load_post`), or a freshly opened file has nothing to re-pin
   from — should just fall through to whatever value was actually saved in
   the file, which is fine.

## Bug log

### hdri_maker — world/studio-light rotation dragging pushed unwanted undo steps
- Repo: `BlenderPlugins` (`git show 5d8679a619ec8e06427e5d8cb684ada6f96ef5ef`)
- The `HDRIMAKER_OT_RotateHDRI` modal operator had `bl_options = {"UNDO"}`,
  meaning every completed drag pushed its own undo step for world-Z rotation
  and studio-light rotation. Fix was a one-line change to
  `bl_options = {"INTERNAL"}`, removing the automatic push. This worked cleanly
  for those two rotation modes because their live values are written to
  `space.shading.studiolight_rotate_z` (Screen/space data — never undo-tracked
  anyway) and a custom scene property group used only to *remember* the value
  across shading-mode switches, not to drive the visual directly.

### hdri_maker — solid-shading shadow-direction dragging bled into unrelated undo/redo
- Repo: `BlenderPlugins`
- Status: fixed, uncommitted in working tree as of this writing (no commit hash yet)
- A later feature (commit `f5f5869`, "viewport shading solid mode shadow
  rotation") added a third drag mode that writes directly to
  `scene.display.light_direction` — genuine Scene ID data, unlike the
  Screen-data path the earlier two modes use. Because the operator already had
  `bl_options = {"INTERNAL"}` (no automatic push) from the fix above, the raw
  per-mouse-move writes had no undo step of their own, and got silently bundled
  into whatever undo step a later, completely unrelated operator created —
  undoing that unrelated action would also revert the shadow direction.
  Reproduced and confirmed via live Blender scripting before fixing.
  Fixed using approach 2 above: a module-level `_light_direction_cache` dict
  keyed by scene name, updated on every drag step and by ESC-cancel, with
  `undo_post`/`redo_post` handlers that re-pin `scene.display.light_direction`
  to the cached value after any undo/redo. Cache is seeded in `register()` and
  in `load_post` from the file's actual saved value.

### Bbrush — brush shelf dict desynced from mode transitions triggered by undo
- Repo: `Bbrush` (`git show e792f041a187f0757d53dff2bfb8976b11248513`)
- Different failure class — not ID-data bleed, but addon-owned Python state
  (`brush_shelf`, a plain global dict tracking per-mode brush shelves) going
  stale. Undo can put the user back into Sculpt mode without necessarily
  re-running the addon's normal mode-enter init path, while a live modal
  Ctrl-key handler immediately tries to index the now-cleared dict, raising
  `KeyError: 'MASK'`. Fix was defensive lazy-rebuild: both `set_brush_shelf()`
  and the modal handler now check whether the expected key exists and call
  `start_brush_shelf(context)` to rebuild before proceeding, instead of
  assuming the dict survived whatever undo/redo just happened.

### mio3_shape_keys — timer-triggered operator needed its own undo_push bracketing
- Repo: `mio3_shape_keys`
- Branch: `Morph_Brush` (`git show 0007f2c253a31fde0f2f222e8455180eac172816`)
- **Status: written, not merged/verified.** This branch's last commit is from
  2026-05-05; `master` has continued past it without merging, so treat this as
  an attempted fix, not a confirmed resolution.
- The addon's morph-apply operator already called
  `bpy.ops.ed.undo_push(message="Apply Morph")` at the top of its own
  `execute()`, which is correct when a user clicks a button. A new
  "auto-apply after sculpt stroke" feature drives the *same* operator from a
  `bpy.app.timers` polling loop instead, detecting stroke-end via
  `window.modal_operators`. Calling the operator this way from a background
  timer, still carrying its normal internal `undo_push`, created undo-step
  boundary problems around the auto-triggered write to real mesh/shape-key
  data. Attempted fix: added a `from_timer` flag (`SKIP_SAVE`, `HIDDEN`) so
  `execute()` skips its own internal push when timer-invoked, and the timer
  callback instead brackets the call manually —
  `undo_push()` → `mio3sk_morph_apply(..., from_timer=True)` → `undo_push()`
  — with reentrancy guards (`_morph_is_applying`, `_morph_was_stroking`) so the
  timer can't fire mid-apply or double-trigger.
