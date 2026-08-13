# Blender Dev MCP

An MCP server that drives a running Blender for addon development. Two halves in
separate processes, talking JSON over a local socket on port 9876:

- `src/blender_dev_mcp/server.py` — the MCP server. Tool definitions, and the
  client end of the socket (`BlenderConnection`).
- `blender_dev_mcp_addon/addon.py` — the Blender addon. `SocketTransport` owns
  sockets, threads and framing; `BlenderDevMCPServer` owns dispatch and the
  handlers, which are declared with `@command("name")`. Domain logic lives
  alongside in `geonodes.py`, `ntp_bridge.py` and `undo.py`.

One tool, `blender_docs`, needs no Blender at all — it reads the offline docs
described below, and is served entirely from `src/blender_dev_mcp/docs.py`.

Nothing at runtime checks the two halves agree on the wire protocol, so
`test_contract.py` does it statically by AST-scraping both files.

## Running the tests

```
python test_server.py                     # client half, no Blender needed
python test_contract.py                   # wire protocol, no Blender needed
python tools/headless.py test_addon.py    # addon half, needs Blender
```

`tools/headless.py` runs any script inside headless Blender with full stderr and
tracebacks — far better for iterating than the MCP connection, and it never
touches the user's running session.

## Where to look things up

Three sources, in the order they should be consulted. The ordering is the
important part — they disagree, and one of them is version-correct.

| question | use |
|---|---|
| Does this API exist? What is it called? What type / enum values? | **`blender_docs` MCP tool** — offline, version-stamped, no Blender needed |
| What is the value *right now* in this scene? | `execute_blender_code` MCP tool |
| Why does it behave like that internally? | `reference/blender-main` C++ source |

### The version trap

`reference/blender-main` is **Blender 5.3 alpha** (`BLENDER_VERSION 503`). The
Blender this tooling drives is **5.1**. The source tree therefore shows what
*will* exist, not what *does*.

This is not hypothetical. `undo.py` documents exactly this: `wm.undo_stack`
would make its undo guard exact instead of merely bounded, and
`source/blender/makesrna/intern/rna_wm_undo.cc` is right there in the reference
tree — but the class does not exist in 5.1. Reading the source alone leads
straight to a feature that cannot be used.

So: **`blender_docs` first, source second.** The docs say whether something is
there; the source says how it works.

## docs/ — offline Blender 5.1 documentation

The HTML Python API reference and user manual for 5.1, matching the Blender this
tooling drives. ~2 GB, so `docs/Blender Documentation/` is gitignored and only
exists on this machine. `docs/undo_redo_notes.md` beside it is hand-written and
*is* tracked.

Reach for these through the **`blender_docs` MCP tool** rather than opening the
HTML — it resolves symbols through Sphinx's `objects.inv` (25,659 entries, ~15 ms)
and strips theme chrome from the page it returns. Grepping 1.6 GB of HTML by hand
is the slow path the tool already falls back to.

Because it is an MCP tool, it is available in **every** repo this server is
configured for, not just this one — which is the point of keeping the docs here
rather than next to any single addon.

`src/blender_dev_mcp/docs.py` is the implementation. Set `BLENDER_DOCS_DIR` to
point at a copy elsewhere; the versioned directory names are matched by glob, so
re-downloading for a newer Blender needs no code change.

## Reference source

`reference/` holds four upstream repos, ~430 MB total. **It is gitignored** — it
exists only on this machine, is never committed, and may be absent elsewhere.

### reference/blender-main (287 MB, 20k files)

Never grep the whole tree — always scope to a subdirectory. Paths below are
relative to `reference/blender-main/`.

**`scripts/` is the highest-value part for addon work.** It is Blender's own
bundled Python, so it is real, working, idiomatic `bpy`:

| path | what it answers |
|---|---|
| `scripts/startup/bl_ui/` | how Blender builds its own panels, menus, layouts |
| `scripts/startup/bl_operators/` | operator implementations, modal patterns |
| `scripts/modules/bpy_extras/` | the helper modules addons are meant to use |
| `scripts/addons_core/` | full shipped addons — `node_wrangler`, `rigify`, `io_scene_gltf2`. Best-practice examples of the real thing |
| `scripts/templates_py/` | official addon boilerplate |

**`source/blender/makesrna/intern/rna_*.cc`** (123 files) is the authoritative
definition of the Python API: exact property names, defaults, ranges, and enum
item identifiers. When the docs are vague about what a property is called or
what an enum accepts, this is ground truth — it is the code that *generates*
`bpy.types`.

Other useful corners:

- `source/blender/editors/` — C implementations behind the operators.
  `editors/undo/` explains why an undo push must follow its mutation (cited in
  `undo.py`).
- `source/blender/nodes/` — geometry and shader node internals.
- `doc/python_api/rst/` — source of the official Python API docs.
- `tests/python/` — bpy usage that is known to work, as executable examples.

Skip `intern/`, `extern/`, `lib/`, and `build_files/` — they are dependencies
and build plumbing, not API surface.

### reference/geonodes-main (119 MB)

A Python library for scripting geometry and shader nodes. It ships docs written
*for LLMs* — start with `llms.txt` (API reference) and `claude.md` (architecture
map, in French). Read those before the source; they are far cheaper than
crawling `core/`.

### reference/NodeToPython-main (4.8 MB)

Generates Python that rebuilds a node tree. `blender_dev_mcp_addon/ntp_bridge.py`
is a bridge to it — consult this when changing snapshot or restore behaviour.

### reference/geometry-script-main (20 MB)

Another node-scripting DSL. Useful mainly as a second opinion on node-graph API
design; nothing here depends on it.
