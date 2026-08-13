"""Checks the two halves still agree on the wire protocol.

The MCP server sends `{"type": name, "params": {...}}`; the addon dispatches on
that name and calls a handler with those params as keyword arguments. Nothing
at runtime checks the two match - a renamed handler argument or a typo'd param
key surfaces only as a TypeError inside Blender, on the one call that uses it.

Both files are read with `ast` rather than imported, so this needs neither bpy
nor a running Blender:

    python test_contract.py
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(ROOT, "blender_dev_mcp_addon", "addon.py")
SERVER = os.path.join(ROOT, "src", "blender_dev_mcp", "server.py")


def _parse(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def addon_handlers():
    """{command name: (required args, all args)} from the @command decorators."""
    tree = _parse(ADDON)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "BlenderDevMCPServer")

    contract = {}
    for method in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
        for deco in method.decorator_list:
            # @command("get_scene_info")
            if not (isinstance(deco, ast.Call)
                    and getattr(deco.func, "id", None) == "command"):
                continue
            assert deco.args and isinstance(deco.args[0], ast.Constant), \
                f"{method.name}: @command needs a literal name"
            args = [a.arg for a in method.args.args if a.arg != "self"]
            required = args[:len(args) - len(method.args.defaults)]
            contract[deco.args[0].value] = (set(required), set(args))
    return contract


def server_calls():
    """{command name: set of param keys} for every send_command in the MCP server."""
    calls = {}
    for node in ast.walk(_parse(SERVER)):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send_command"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        name = node.args[0].value
        keys = set()
        if len(node.args) > 1 and isinstance(node.args[1], ast.Dict):
            for k in node.args[1].keys:
                assert isinstance(k, ast.Constant), f"non-literal param key in {name}"
                keys.add(k.value)
        calls[name] = keys
    return calls


def test_every_command_sent_has_a_handler():
    unknown = set(server_calls()) - set(addon_handlers())
    assert not unknown, f"server sends commands the addon cannot dispatch: {unknown}"


def test_params_sent_are_accepted_by_the_handler():
    handlers = addon_handlers()
    for name, sent in server_calls().items():
        _, accepted = handlers[name]
        extra = sent - accepted
        assert not extra, (
            f"{name}: server sends {sorted(extra)}, which the addon handler "
            f"does not accept (it takes {sorted(accepted)})")


def test_required_handler_args_are_always_sent():
    handlers = addon_handlers()
    for name, sent in server_calls().items():
        required, _ = handlers[name]
        missing = required - sent
        assert not missing, (
            f"{name}: addon handler requires {sorted(missing)}, which the "
            f"server never sends - this raises TypeError inside Blender")


def test_every_handler_is_reachable_from_a_tool():
    # A handler nothing calls is either dead code or a tool someone forgot to
    # wire up. Worth knowing either way.
    orphans = set(addon_handlers()) - set(server_calls())
    assert not orphans, f"addon handlers no tool ever calls: {orphans}"


def test_contract_is_not_vacuous():
    # Guards the ast scraping itself: if either parser silently returned {},
    # every assertion above would pass for the wrong reason.
    handlers, calls = addon_handlers(), server_calls()
    assert len(handlers) >= 5, handlers
    assert len(calls) >= 5, calls
    assert "max_items" in calls["get_object_info"], calls["get_object_info"]


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
