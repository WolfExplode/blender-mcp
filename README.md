# Blender Dev MCP

Drive a running Blender from an MCP client, for addon development and debugging.

Twelve tools, no asset stores, no telemetry:

| Tool | What it does |
|---|---|
| `get_scene_info` | Scene name, counts, current frame, mode, active object, object list |
| `get_object_info` | Transform, mode, selection, materials, modifiers, vertex groups, shape keys, mesh counts, world AABB |
| `execute_blender_code` | Run Python in Blender; returns stdout **and** stderr, with a full traceback on failure |
| `get_viewport_screenshot` | Offscreen render of the 3D viewport (works when the window isn't focused) |
| `get_stderr_log` | Read back Blender's stderr this session — including tracebacks you triggered by clicking in the UI |
| `list_node_trees` | Every geometry node tree, with node/link/frame counts and how many things use it |
| `get_node_tree_outline` | One tree's interface, frames, zones and dependencies — without its nodes |
| `get_node_detail` | The nodes, settings and links of one frame |
| `validate_node_tree` | Invalid links, unpaired zones, Blender's node warnings, evaluated output geometry |
| `snapshot_node_tree` | Save a tree as runnable Python, so an edit can be undone |
| `restore_node_snapshot` | Rebuild trees from a snapshot, as new groups, validated |
| `annotate_node_tree` | Write labels and frames — the one edit that can't change what a tree computes |

### Geometry nodes

work in progress, will update docs when finished

### Notes on two of them

`get_object_info` returns vertex groups and shape keys as `{count, names}`,
capped at `max_items` (default 40, `0` for all) — a production rig with 300
vertex groups and 400 shape keys otherwise costs more context than every other
tool combined, and the count is usually the part you wanted. When the object is
in Edit Mode it also reports live selected vert/edge/face counts and the select
mode, which `mesh.vertices` cannot give you because the evaluated mesh is stale
until you leave Edit Mode.

`execute_blender_code` runs with `bpy`, `bmesh`, `mathutils` and `math` already
in scope, and with `__name__` set to `"<blender_dev_mcp>"` — defined, so
`if __name__ == "__main__"` does not raise NameError, but not `"__main__"`, so
loading an addon file here will not fire its entry-point block.

## Layout

```
blender_dev_mcp_addon/   the Blender addon (socket server, runs inside Blender)
  addon.py                 socket server, command dispatch, panel
  geonodes.py              read / validate / annotate node trees (pure bpy)
  ntp_bridge.py            snapshot + restore (optional: NodeToPython)
src/blender_dev_mcp/     the MCP server (stdio, talks to the addon on :9876)
docs/geometry-nodes.md   the geometry nodes tooling, in full
tools/headless.py        run scripts in headless Blender with full stderr
test_addon.py            addon tests   (need bpy -> headless runner)
test_server.py           MCP-side tests (pure python -> plain interpreter)
test_contract.py         checks the two halves agree on the wire protocol
```

The split is by what each half needs to import: `blender_dev_mcp_addon/` needs
`bpy` and can only run inside Blender; `src/blender_dev_mcp/` is plain Python
and never imports `bpy`. That is also why there are two test files run two
different ways.

Nothing at runtime checks that the params the server sends match the arguments
the addon handler takes — a mismatch shows up only as a `TypeError` inside
Blender, on the one call that uses it. `test_contract.py` closes that by
reading both files with `ast` and comparing the dispatch table against the
`send_command` calls, so it needs no Blender either.

## Setup

The addon is symlinked into Blender's addons directory, so editing this repo
edits the installed addon:

```
%APPDATA%\Blender Foundation\Blender\5.1\scripts\addons\blender_dev_mcp_addon
    -> C:\Users\WXP\Documents\GitHub\blender-mcp\blender_dev_mcp_addon
```

Enable **Blender Dev MCP** in Preferences → Add-ons. It auto-starts the socket
server on port 9876; the panel lives in View3D → Sidebar → Dev MCP.

Port and "Start on launch" are **addon preferences**, not per-file settings —
they live in `userpref.blend` and apply to every scene and every `.blend`. Set
them either in the panel or under Preferences → Add-ons → Blender Dev MCP, and
save preferences to make them stick. Changing the port while the server is
running does not move the live socket; the panel says so, and Stop then Start
rebinds onto the new one.

MCP client config:

```json
{ "blender": { "command": "uv",
               "args": ["--directory", "C:\\Users\\WXP\\Documents\\GitHub\\blender-mcp",
                        "run", "main.py"] } }
```

A Blender restart is required after editing addon code — the running instance
keeps the old module in memory.

Environment overrides, all optional:

| Variable | Default | Purpose |
|---|---|---|
| `BLENDER_HOST` | `localhost` | Where the addon is listening |
| `BLENDER_PORT` | `9876` | Must match the port in the Dev MCP panel |
| `BLENDER_MCP_TIMEOUT` | `180` | Seconds to wait for one command; raise it for heavy mesh operators |

## Testing

```
python test_server.py                      # MCP side, no Blender needed
python test_contract.py                    # wire protocol, no Blender needed
python tools/headless.py test_addon.py     # addon side, needs Blender's python
python tools/headless.py --list            # installed Blender versions
python tools/headless.py probe.py -v 4.2   # run a script against a specific version
```

`tools/headless.py` runs scripts in `blender --background --factory-startup`,
which shows full stderr and never touches your live session. Prefer it for
iteration; the MCP connection is for inspecting the actual scene you're working
in.

## Origin

Forked from [blender-mcp](https://github.com/ahujasid/blender-mcp) by Siddharth
Ahuja (MIT, see LICENSE). Stripped from 22 tools to 5: removed the PolyHaven,
Sketchfab, Hyper3D/Rodin and Hunyuan integrations and the telemetry layer
(~4,300 lines), and rewrote what remained.
