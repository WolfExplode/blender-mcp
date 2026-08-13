"""Tests for the Blender Dev MCP addon.

Needs Blender's interpreter (addon.py imports bpy), so run through the headless
runner rather than plain pytest:

    python tools/headless.py test_addon.py
"""
import importlib.util
import json
import os
import pathlib
import sys

ADDON = pathlib.Path(__file__).with_name("blender_dev_mcp_addon") / "addon.py"


def _load():
    spec = importlib.util.spec_from_file_location("blender_dev_mcp_addon_test", ADDON)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeClient:
    """Socket stand-in that replays scripted recv() chunks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.sent = []
        self.closed = False
        self.shutdown_called = False

    def recv(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, _t):
        pass

    def shutdown(self, _how):
        self.shutdown_called = True

    def close(self):
        self.closed = True


def _drain(m, chunks):
    """Run the transport's read loop over scripted chunks, returning commands."""
    seen = []
    transport = m.SocketTransport(lambda client, command: seen.append(command))
    transport.running = True
    transport._handle_client(FakeClient(chunks))
    return seen


# ---------------------------------------------------------------- framing

def test_single_command(m):
    seen = _drain(m, [b'{"type": "get_scene_info", "params": {}}'])
    assert seen == [{"type": "get_scene_info", "params": {}}], seen


def test_command_split_across_recvs(m):
    seen = _drain(m, [b'{"type": "get_sce', b'ne_info", "params": {}}'])
    assert seen == [{"type": "get_scene_info", "params": {}}], seen


def test_two_commands_in_one_recv(m):
    # The old buffer-wide json.loads raised "Extra data" here, read that as
    # "incomplete", and wedged the connection forever.
    seen = _drain(m, [b'{"type": "a", "params": {}}{"type": "b", "params": {}}'])
    assert [c["type"] for c in seen] == ["a", "b"], seen


def test_pipelined_and_split(m):
    seen = _drain(m, [b'{"type": "a"}{"type": "b', b'"}{"type": "c"}'])
    assert [c["type"] for c in seen] == ["a", "b", "c"], seen


def test_whitespace_between_commands(m):
    seen = _drain(m, [b'{"type": "a"}\n  \n{"type": "b"}\n'])
    assert [c["type"] for c in seen] == ["a", "b"], seen


def test_multibyte_split_across_recvs(m):
    # A single command whose UTF-8 is split mid-character.
    seen = _drain(m, [b'{"type": "\xe6\x97', b'\xa5"}'])
    assert [c["type"] for c in seen] == ["日"], seen


def test_multibyte_split_after_pipelined_command(m):
    # Decoding the buffer with errors="replace" turned the partial character
    # into U+FFFD; re-encoding the remainder then made that loss permanent, so
    # the *second* command arrived corrupted. Japanese shape key names hit this.
    seen = _drain(m, [b'{"type": "a"}{"type": "\xe6\x97', b'\xa5\xe6\x9c\xac"}'])
    assert [c["type"] for c in seen] == ["a", "日本"], seen


def test_garbage_does_not_dispatch_or_crash(m):
    seen = _drain(m, [b"this is not json at all"])
    assert seen == [], seen


def test_oversized_buffer_is_discarded(m):
    seen = []
    transport = m.SocketTransport(lambda client, command: seen.append(command))
    transport.running = True
    transport.MAX_BUFFER = 64
    # Never-parsing payload larger than the cap, then a valid command.
    transport._handle_client(FakeClient([b'{"junk": ' + b"x" * 200,
                                         b'{"type": "after"}']))
    assert [c["type"] for c in seen] == ["after"], seen


# ---------------------------------------------------------------- lifecycle
# Reloading an addon calls unregister() -> stop(). Before these guards, that
# closed only the listening socket: pooled client sockets stayed open, looked
# healthy to the MCP client, and swallowed one command each -- executing it on
# a dead server instance and sending the reply nowhere.

def test_stop_hangs_up_on_accepted_clients(m):
    transport = m.SocketTransport(lambda client, command: None)
    transport.running = True
    clients = [FakeClient([]), FakeClient([])]
    transport._clients.update(clients)

    transport.stop()

    for client in clients:
        assert client.shutdown_called, "peer must be told, not just our handle closed"
        assert client.closed, "accepted sockets must be closed by stop()"
    assert not transport._clients, "the client set must be emptied"


def test_shutdown_socket_errors_are_not_reported_as_faults(m):
    # stop() closes accepted sockets under their handler threads, so the OSError
    # that follows is the designed exit path. Printing it trains the user to
    # ignore the console, which is where real faults also go.
    import io
    from contextlib import redirect_stdout

    transport = m.SocketTransport(lambda client, command: None)
    transport.running = True

    class DiesAsIfClosed(FakeClient):
        def recv(self, _size):
            transport.running = False           # stop() has run
            raise OSError(10038, "not a socket")

    out = io.StringIO()
    with redirect_stdout(out):
        transport._handle_client(DiesAsIfClosed([]))
    assert "client handler error" not in out.getvalue(), out.getvalue()


def test_socket_errors_while_serving_are_still_reported(m):
    import io
    from contextlib import redirect_stdout

    transport = m.SocketTransport(lambda client, command: None)
    transport.running = True

    class Breaks(FakeClient):
        def recv(self, _size):
            raise OSError(9999, "something genuinely wrong")

    out = io.StringIO()
    with redirect_stdout(out):
        transport._handle_client(Breaks([]))
    assert "client handler error" in out.getvalue(), out.getvalue()


def test_handler_deregisters_its_client_on_exit(m):
    # Otherwise the set grows for the life of the session and stop() would
    # shutdown() sockets that closed long ago.
    transport = m.SocketTransport(lambda client, command: None)
    transport.running = True
    client = FakeClient([b'{"type": "get_scene_info"}'])
    transport._clients.add(client)

    transport._handle_client(client)

    assert not transport._clients, "handler must remove its own client when it exits"


def test_command_arriving_after_stop_is_not_executed(m):
    # The handler thread sits blocked in recv() across the stop, so `running`
    # has to be re-checked after the read, not only at the top of the loop.
    seen = []
    transport = m.SocketTransport(lambda client, command: seen.append(command))
    transport.running = True

    class StopsMidRecv(FakeClient):
        def recv(self, size):
            transport.running = False  # stop() lands while we are blocked here
            return super().recv(size)

    transport._handle_client(StopsMidRecv([b'{"type": "execute_code"}']))

    assert seen == [], "a stopped server must not execute what it happens to read"


def test_queued_command_is_discarded_if_the_server_stopped(m):
    # execute_command runs on a main-thread timer, so stop() can land between
    # queueing and firing. Running then would mutate the blend file for a client
    # that has already been hung up on.
    import bpy

    server = m.BlenderDevMCPServer()
    server.transport.running = True
    ran = []
    server.execute_command = lambda command: ran.append(command) or {"status": "success"}

    queued = []
    real_register = bpy.app.timers.register
    bpy.app.timers.register = lambda fn, **kw: queued.append(fn)
    try:
        client = FakeClient([])
        server._dispatch_on_main_thread(client, {"type": "execute_code", "params": {}})
    finally:
        bpy.app.timers.register = real_register

    assert queued, "expected the command to be queued on a timer"
    server.transport.running = False
    queued[0]()

    assert ran == [], "a command queued by a now-stopped server must not run"
    assert client.sent == [], "and nothing should be written to the closed client"


def test_queued_command_still_runs_while_the_server_is_up(m):
    # Guard against the check above turning into a blanket "never dispatch".
    import bpy

    server = m.BlenderDevMCPServer()
    server.transport.running = True
    ran = []
    server.execute_command = lambda command: ran.append(command) or {"status": "success"}

    queued = []
    real_register = bpy.app.timers.register
    bpy.app.timers.register = lambda fn, **kw: queued.append(fn)
    try:
        server._dispatch_on_main_thread(FakeClient([]), {"type": "get_scene_info"})
    finally:
        bpy.app.timers.register = real_register

    queued[0]()
    assert [c["type"] for c in ran] == ["get_scene_info"], ran


# ---------------------------------------------------------------- dispatch

def test_handler_table_is_built_from_the_decorators(m):
    decorated = {fn._command_name for fn in vars(m.BlenderDevMCPServer).values()
                 if callable(fn) and hasattr(fn, "_command_name")}
    assert decorated, "no @command handlers found - the table build is broken"
    assert set(m.BlenderDevMCPServer.HANDLERS) == decorated, \
        "HANDLERS drifted from the decorators that are supposed to define it"


def test_command_names_are_unique(m):
    # dict-building silently keeps the last duplicate, so a copy-pasted
    # decorator would make one handler unreachable with no error anywhere.
    names = [fn._command_name for fn in vars(m.BlenderDevMCPServer).values()
             if callable(fn) and hasattr(fn, "_command_name")]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate @command names: {dupes}"


def test_every_handler_is_callable_on_the_server(m):
    server = m.BlenderDevMCPServer()
    for name, attr in m.BlenderDevMCPServer.HANDLERS.items():
        assert callable(getattr(server, attr, None)), \
            f"{name} maps to {attr!r}, which is not a method"


def test_server_exposes_the_transport_lifecycle(m):
    # The panel and both operators drive the server object and never touch the
    # transport, so these passthroughs are load-bearing UI API.
    server = m.BlenderDevMCPServer(port=12345)
    assert server.port == 12345, server.port
    assert server.host == server.transport.host
    assert server.running is False, "a fresh server must not claim to be running"

    server.port = 12346
    assert server.transport.port == 12346, "the port setter must reach the transport"

    server.transport.running = True
    assert server.running is True, "running must reflect the transport, not a copy"


def test_start_and_stop_delegate_to_the_transport(m):
    server = m.BlenderDevMCPServer()
    calls = []
    server.transport.start = lambda: calls.append("start")
    server.transport.stop = lambda: calls.append("stop")

    server.start()
    server.stop()

    assert calls == ["start", "stop"], calls


def test_transport_needs_no_dispatcher(m):
    # The point of the split: framing is testable with no Blender command in
    # sight. If this ever needs a BlenderDevMCPServer, they have re-merged.
    seen = []
    transport = m.SocketTransport(lambda client, command: seen.append(command))
    transport.running = True
    transport._handle_client(FakeClient([b'{"type": "anything at all"}']))
    assert [c["type"] for c in seen] == ["anything at all"], seen


def test_unknown_command_lists_known(m):
    response = m.BlenderDevMCPServer().execute_command({"type": "nope"})
    assert response["status"] == "error", response
    assert "get_scene_info" in response["message"], response


def test_handler_error_becomes_error_status(m):
    response = m.BlenderDevMCPServer().execute_command(
        {"type": "get_object_info", "params": {"name": "does-not-exist"}})
    assert response["status"] == "error", response
    assert "does-not-exist" in response["message"], response


# ---------------------------------------------------------------- handlers

def test_scene_info(m):
    info = m.BlenderDevMCPServer().get_scene_info()
    for key in ("name", "object_count", "materials_count", "mode", "objects"):
        assert key in info, f"missing {key}: {info}"
    json.dumps(info)  # must be serialisable for the wire


def test_scene_info_truncates(m):
    info = m.BlenderDevMCPServer().get_scene_info(max_objects=1)
    assert len(info["objects"]) <= 1, info


def test_object_info_on_default_cube(m):
    import bpy
    name = next(o.name for o in bpy.data.objects if o.type == "MESH")
    info = m.BlenderDevMCPServer().get_object_info(name)
    assert info["mesh"]["vertices"] == 8, info
    assert len(info["world_bounding_box"]) == 2, info
    json.dumps(info)


def test_object_info_caps_long_name_lists(m):
    import bpy
    obj = next(o for o in bpy.data.objects if o.type == "MESH")
    for i in range(10):
        obj.vertex_groups.new(name=f"grp{i}")
    try:
        groups = m.BlenderDevMCPServer().get_object_info(obj.name, max_items=4)["vertex_groups"]
        assert groups["count"] == 10, groups
        assert len(groups["names"]) == 4, groups
        assert groups["showing"] == 4, groups
        # max_items=0 means "no cap"
        full = m.BlenderDevMCPServer().get_object_info(obj.name, max_items=0)["vertex_groups"]
        assert len(full["names"]) == 10, full
        assert "showing" not in full, full
    finally:
        for group in list(obj.vertex_groups):
            obj.vertex_groups.remove(group)


def test_object_info_reports_mode_and_selection_keys(m):
    import bpy
    obj = next(o for o in bpy.data.objects if o.type == "MESH")
    info = m.BlenderDevMCPServer().get_object_info(obj.name)
    assert info["mode"] == obj.mode, info
    assert info["selected"] == obj.select_get(), info
    # Not in Edit Mode, so no live selection block is reported.
    assert "selection" not in info["mesh"], info
    json.dumps(info)


def test_object_info_missing_raises(m):
    try:
        m.BlenderDevMCPServer().get_object_info("nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError for a missing object")


# ---------------------------------------------------------------- get_object_property

def _shape_key_object(name="__prop_obj__"):
    """A one-quad mesh with a Basis and one named shape key."""
    import bpy
    mesh = bpy.data.meshes.new(name + "_mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [],
                     [(0, 1, 2, 3)])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.shape_key_add(name="Basis")
    key = obj.shape_key_add(name="Smile")
    key.value = 0.5
    key.mute = True
    return obj, mesh


def test_property_reads_a_shape_key_by_name(m):
    import bpy
    obj, mesh = _shape_key_object()
    try:
        result = m.BlenderDevMCPServer().get_object_property(
            obj.name, 'data.shape_keys.key_blocks["Smile"]')
        assert result["type"] == "ShapeKey", result
        assert result["value"]["value"] == 0.5, result
        assert result["value"]["mute"] is True, result
        assert result["value"]["name"] == "Smile", result
        json.dumps(result)
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)


def test_property_reads_a_scalar_field_directly(m):
    import bpy
    obj, mesh = _shape_key_object()
    try:
        result = m.BlenderDevMCPServer().get_object_property(
            obj.name, 'data.shape_keys.key_blocks["Smile"].value')
        assert result["value"] == 0.5, result
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)


def test_property_collection_without_index_is_capped_names(m):
    import bpy
    obj, mesh = _shape_key_object()
    try:
        result = m.BlenderDevMCPServer().get_object_property(
            obj.name, "data.shape_keys.key_blocks")
        assert set(result["value"]["names"]) == {"Basis", "Smile"}, result
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)


def test_property_datablock_reference_is_named_not_recursed(m):
    """relative_key points back at the Basis ShapeKey - must not recurse into it."""
    import bpy
    obj, mesh = _shape_key_object()
    try:
        result = m.BlenderDevMCPServer().get_object_property(
            obj.name, 'data.shape_keys.key_blocks["Smile"]')
        rel = result["value"]["relative_key"]
        assert rel == {"type": "ShapeKey", "name": "Basis"}, rel
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)


def test_property_missing_index_raises_with_context(m):
    import bpy
    obj, mesh = _shape_key_object()
    try:
        try:
            m.BlenderDevMCPServer().get_object_property(
                obj.name, 'data.shape_keys.key_blocks["nope"]')
        except ValueError as exc:
            assert "nope" in str(exc), exc
        else:
            raise AssertionError("expected ValueError for a missing key")
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)


def test_property_rejects_arbitrary_expressions(m):
    """The path grammar is not an evaluator - underscored/dunder access must fail closed."""
    import bpy
    name = next(o.name for o in bpy.data.objects if o.type == "MESH")
    try:
        m.BlenderDevMCPServer().get_object_property(
            name, "modifiers.__class__")
    except ValueError:
        return
    raise AssertionError("expected ValueError for a non-identifier path segment")


def test_property_missing_object_raises(m):
    try:
        m.BlenderDevMCPServer().get_object_property("nope", "modifiers")
    except ValueError:
        return
    raise AssertionError("expected ValueError for a missing object")


def test_property_unnamed_collection_items_report_count_not_padding(m):
    """ShapeKey.data/.points hold thousands of unnamed ShapeKeyPoint structs.

    Regression: describing each item fell back to {"type": ..., "name": None},
    so a shape key's own detail carried 2 * max_items copies of that with no
    information in any of them. Only the count is meaningful there.
    """
    import bpy
    obj, mesh = _shape_key_object()
    try:
        result = m.BlenderDevMCPServer().get_object_property(
            obj.name, 'data.shape_keys.key_blocks["Smile"].data')
        assert result["value"] == {"count": 4}, result
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)


# ---------------------------------------------------------------- execute_code

def test_stdout_returned(m):
    result = m.BlenderDevMCPServer().execute_code("print('hello')")
    assert result == {"executed": True, "result": "hello\n"}, result


def test_common_modules_preimported(m):
    result = m.BlenderDevMCPServer().execute_code(
        "print(bmesh.__name__, mathutils.__name__, math.pi > 3)")
    assert result["result"].strip() == "bmesh mathutils True", result


def test_name_is_defined_but_not_main(m):
    result = m.BlenderDevMCPServer().execute_code(
        "print(__name__ == '__main__', __name__)")
    assert result["result"].startswith("False "), result


def test_stderr_captured_on_success(m):
    result = m.BlenderDevMCPServer().execute_code(
        "import sys; print('out'); print('ERRLINE', file=sys.stderr)")
    assert "out" in result["result"] and "ERRLINE" in result["result"], result


def test_failure_keeps_stdout_and_traceback(m):
    try:
        m.BlenderDevMCPServer().execute_code(
            "print('before crash')\nraise ValueError('boom')")
    except Exception as exc:
        got = str(exc)
    else:
        raise AssertionError("execute_code should raise when the snippet raises")
    assert "before crash" in got, f"stdout before the raise was lost:\n{got}"
    assert "ValueError: boom" in got, f"exception text was lost:\n{got}"
    assert "line 2" in got, f"traceback line number was lost:\n{got}"


# ---------------------------------------------------------------- stderr log

def test_stderr_log_records_and_writes_through(m):
    m.STDERR_LOG.clear()
    m.install_stderr_tee()
    try:
        print("TEE-WRITE-THROUGH-VISIBLE", file=sys.stderr)
        log = m.BlenderDevMCPServer().get_stderr_log()
        assert "TEE-WRITE-THROUGH-VISIBLE" in log["text"], log
        assert log["tee_active"] is True, log
    finally:
        m.remove_stderr_tee()


def test_stderr_log_truncates_and_clears(m):
    m.STDERR_LOG.clear()
    m.install_stderr_tee()
    try:
        print("x" * 500, file=sys.stderr)
        log = m.BlenderDevMCPServer().get_stderr_log(max_chars=100)
        assert len(log["text"]) == 100, len(log["text"])
        assert log["truncated"] is True, log
        m.BlenderDevMCPServer().get_stderr_log(clear=True)
        assert len(m.STDERR_LOG) == 0, "clear=True should empty the buffer"
    finally:
        m.remove_stderr_tee()


def test_stderr_log_reports_chunks_it_returned(m):
    # chunks used to be counted after the clear, so a clearing read always
    # claimed 0 chunks for text it had just handed back.
    m.STDERR_LOG.clear()
    m.install_stderr_tee()
    try:
        print("something", file=sys.stderr)
        log = m.BlenderDevMCPServer().get_stderr_log(clear=True)
        assert log["text"].strip(), log
        assert log["chunks"] > 0, log
    finally:
        m.remove_stderr_tee()


def test_tee_is_idempotent(m):
    m.install_stderr_tee()
    m.install_stderr_tee()
    try:
        assert sys.stderr.stream is not sys.stderr, "second install nested the tee"
    finally:
        m.remove_stderr_tee()
    m.remove_stderr_tee()  # removing twice must be harmless


# ---------------------------------------------------------------- registration

def test_register_cycle(m):
    import bpy
    # Patch on the class, so restore it on the way out: these used to leak, and
    # every later test that exercised the real start/stop was silently running
    # against a no-op lambda instead.
    real_start, real_stop = m.BlenderDevMCPServer.start, m.BlenderDevMCPServer.stop
    m.BlenderDevMCPServer.start = lambda self: None  # never bind a real port
    m.BlenderDevMCPServer.stop = lambda self: None
    m.register()
    try:
        assert isinstance(sys.stderr, m._StderrTee), "register did not install the tee"
        assert "BLENDERDEVMCP_PT_Panel" in dir(bpy.types), "panel not registered"
        # AddonPreferences is keyed by bl_idname internally rather than exposed
        # as a bpy.types attribute, so ask the class itself.
        assert m.BlenderDevMCPPreferences.is_registered, "prefs not registered"
        assert m.BlenderDevMCPPreferences.bl_idname == m.ADDON_ID, \
            "bl_idname must equal the addon module name or Blender cannot find the prefs"
    finally:
        m.unregister()
        m.BlenderDevMCPServer.start, m.BlenderDevMCPServer.stop = real_start, real_stop
    assert not isinstance(sys.stderr, m._StderrTee), "unregister did not restore stderr"
    assert "BLENDERDEVMCP_PT_Panel" not in dir(bpy.types), "panel leaked"
    # The old Scene properties must not come back - they were the bug.
    for prop in ("blender_dev_mcp_port", "blender_dev_mcp_auto_start"):
        assert not hasattr(bpy.types.Scene, prop), f"{prop} should no longer exist"


def test_settings_are_not_stored_on_the_scene(m):
    """Port and auto-start must not be per-.blend.

    A Scene property is unreadable at startup registration (bpy.context.scene is
    None under _RestrictContext) and is stored per scene, so saved values were
    silently ignored on every launch.
    """
    import bpy
    real_start, real_stop = m.BlenderDevMCPServer.start, m.BlenderDevMCPServer.stop
    m.BlenderDevMCPServer.start = lambda self: None
    m.BlenderDevMCPServer.stop = lambda self: None
    m.register()
    try:
        for prop in ("blender_dev_mcp_port", "blender_dev_mcp_auto_start"):
            assert not hasattr(bpy.types.Scene, prop), \
                f"{prop} is a Scene property again - it will not survive startup"
        annotations = m.BlenderDevMCPPreferences.__annotations__
        assert "port" in annotations and "auto_start" in annotations, annotations
    finally:
        m.unregister()
        m.BlenderDevMCPServer.start, m.BlenderDevMCPServer.stop = real_start, real_stop


def test_undo_budget_is_cleared_when_a_file_is_loaded(m):
    # Blender drops its undo stack on load; the push counter is module state and
    # would not, leaving undo_edit convinced it owns steps that no longer exist.
    # Spending that stale budget walks into the new file's own history.
    m.undo.reset()
    m.undo._pushed = [2, 2, 1, 1]
    m._reset_undo_budget_on_load(None)
    assert m.undo.budget() == 0, "a file load must void the budget"


def test_load_handler_is_registered_and_persistent(m):
    import bpy

    real_start, real_stop = m.BlenderDevMCPServer.start, m.BlenderDevMCPServer.stop
    m.BlenderDevMCPServer.start = lambda self: None
    m.BlenderDevMCPServer.stop = lambda self: None
    m.register()
    try:
        assert m._reset_undo_budget_on_load in bpy.app.handlers.load_post, \
            "register() must hook load_post or the budget goes stale"
        # Non-persistent handlers are removed by the very load they must
        # observe. Blender marks the decorated function by *setting* the
        # attribute, whose value is None - so test for presence, not truth.
        assert "_bpy_persistent" in m._reset_undo_budget_on_load.__dict__, \
            "the load handler must be @persistent"
    finally:
        m.unregister()
        m.BlenderDevMCPServer.start, m.BlenderDevMCPServer.stop = real_start, real_stop
    assert m._reset_undo_budget_on_load not in bpy.app.handlers.load_post, \
        "unregister() must remove the handler again"


def test_get_prefs_returns_none_when_addon_is_not_installed(m):
    """register() must survive prefs being unreachable, not raise.

    This module is loaded under a test-only name, so ADDON_ID matches no
    installed addon - the same shape as any unusual load path. get_prefs()
    returning None makes callers fall back to the defaults.
    """
    assert m.get_prefs() is None, "expected no prefs entry under the test module name"


def _geo_tree(name="__test_tree__"):
    """A small geometry node tree: two framed Math nodes and a paired zone."""
    import bpy
    tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
    frame = tree.nodes.new("NodeFrame")
    frame.label = "the frame"
    for _ in range(2):
        node = tree.nodes.new("ShaderNodeMath")
        node.parent = frame
    zone_in = tree.nodes.new("GeometryNodeRepeatInput")
    zone_out = tree.nodes.new("GeometryNodeRepeatOutput")
    zone_in.pair_with_output(zone_out)
    return tree


def test_outline_counts_nodes_inside_frames(m):
    """Regression: frame children were counted with `is` and always came to 0.

    Blender returns a fresh Python wrapper each time node.parent is read, so
    `node.parent is frame` is False for a node that really is in that frame.
    The outline reported every frame as empty while the tree plainly was not -
    wrong, and silent, which is the combination worth a test.
    """
    import bpy
    tree = _geo_tree()
    try:
        outline = m.geonodes.tree_outline(tree)
        frames = outline["frames"]
        assert len(frames) == 1, frames
        assert frames[0]["nodes"] == 2, frames
        assert frames[0]["label"] == "the frame", frames
        assert frames[0]["types"] == {"ShaderNodeMath": 2}, frames
        # The same number reached two different ways must agree.
        assert outline["annotation"]["framed_nodes"] == 2, outline["annotation"]
    finally:
        bpy.data.node_groups.remove(tree)


def test_outline_reports_zone_pairing(m):
    import bpy
    tree = _geo_tree()
    try:
        zones = m.geonodes.tree_outline(tree)["zones"]
        assert len(zones) == 1 and zones[0]["paired"] is True, zones

        # An unpaired zone is a broken tree Blender exposes no error flag for,
        # so the outline is the only thing that can report it.
        lone = bpy.data.node_groups.new("__lone__", "GeometryNodeTree")
        try:
            lone.nodes.new("GeometryNodeRepeatInput")
            zones = m.geonodes.tree_outline(lone)["zones"]
            assert zones[0]["paired"] is False, zones
        finally:
            bpy.data.node_groups.remove(lone)
    finally:
        bpy.data.node_groups.remove(tree)


def test_outline_reports_users_and_stays_json(m):
    import bpy
    tree = _geo_tree()
    obj = next(o for o in bpy.data.objects if o.type == "MESH")
    mod = obj.modifiers.new("__test_gn__", "NODES")
    mod.node_group = tree
    try:
        outline = m.geonodes.tree_outline(tree)
        assert obj.name in outline["users"]["objects"], outline["users"]
        # Everything crosses a socket as JSON; a stray bpy value breaks the call.
        json.dumps(outline)
    finally:
        obj.modifiers.remove(mod)
        bpy.data.node_groups.remove(tree)


def test_outline_rejects_non_geometry_trees(m):
    import bpy
    shader = bpy.data.node_groups.new("__shader__", "ShaderNodeTree")
    try:
        m.geonodes.tree_outline(shader)
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError for a ShaderNodeTree")
    finally:
        bpy.data.node_groups.remove(shader)


def test_outline_handler_names_known_trees_when_missing(m):
    tree = _geo_tree()
    try:
        m.BlenderDevMCPServer().get_node_tree_outline("__nope__")
    except ValueError as exc:
        assert "__test_tree__" in str(exc), exc
    else:
        raise AssertionError("expected ValueError naming the known trees")
    finally:
        import bpy
        bpy.data.node_groups.remove(tree)


def test_detail_disambiguates_repeated_socket_names(m):
    """Regression: a Math node's three "Value" sockets collapsed into one key.

    Keying by socket name alone made "A.Value -> B.Value" ambiguous and made
    the two unlinked Value inputs overwrite each other in the inputs dict. For
    SUBTRACT and DIVIDE the operand order is the entire meaning, so this did
    not just lose detail - it described a graph that could not be rebuilt.
    """
    import bpy
    tree = bpy.data.node_groups.new("__detail__", "GeometryNodeTree")
    try:
        a = tree.nodes.new("ShaderNodeMath")
        b = tree.nodes.new("ShaderNodeMath")
        b.operation = "SUBTRACT"
        b.inputs[1].default_value = 7.0
        tree.links.new(a.outputs[0], b.inputs[0])

        detail = m.geonodes.node_detail(tree)
        edge = next(l for l in detail["links"] if l["to"].startswith(b.name))
        assert edge["to"] == f"{b.name}.Value[0]", edge
        assert edge["from"] == f"{a.name}.Value", edge

        node = next(n for n in detail["nodes"] if n["name"] == b.name)
        assert node["inputs"] == {"Value[1]": 7.0}, node
        assert node["settings"]["operation"] == "SUBTRACT", node
    finally:
        bpy.data.node_groups.remove(tree)


def test_detail_elides_defaults_and_layout(m):
    """Untouched settings and canvas position are noise, not content."""
    import bpy
    tree = bpy.data.node_groups.new("__detail2__", "GeometryNodeTree")
    try:
        node = tree.nodes.new("ShaderNodeMath")
        node.location = (123.0, 456.0)
        detail = m.geonodes.node_detail(tree)
        entry = next(n for n in detail["nodes"] if n["name"] == node.name)
        settings = entry.get("settings", {})
        assert "use_clamp" not in settings, settings
        assert "location" not in settings, settings
        assert "location_absolute" not in settings, settings
        # ADD is the default operation, so it is elided too.
        assert "operation" not in settings, settings
        # ...but a chosen one survives.
        node.operation = "DIVIDE"
        entry = next(n for n in m.geonodes.node_detail(tree)["nodes"]
                     if n["name"] == node.name)
        assert entry["settings"]["operation"] == "DIVIDE", entry
    finally:
        bpy.data.node_groups.remove(tree)


def test_detail_drops_ui_selection_state(m):
    """A zone's active_index records a click, not a decision.

    Found in the reference scene, where one Repeat Output reported
    active_index=1 as though it were a setting. It survives default elision
    precisely because it differs from the default, so only the cosmetic list
    keeps it out.
    """
    import bpy
    tree = bpy.data.node_groups.new("__detail_ui__", "GeometryNodeTree")
    try:
        zone_in = tree.nodes.new("GeometryNodeRepeatInput")
        zone_out = tree.nodes.new("GeometryNodeRepeatOutput")
        zone_in.pair_with_output(zone_out)
        zone_out.repeat_items.new("GEOMETRY", "Geo")
        zone_out.repeat_items.new("FLOAT", "Val")
        zone_out.active_index = 1
        assert zone_out.active_index == 1, "fixture did not take"

        entry = next(n for n in m.geonodes.node_detail(tree)["nodes"]
                     if n["name"] == zone_out.name)
        assert "active_index" not in entry.get("settings", {}), entry
    finally:
        bpy.data.node_groups.remove(tree)


def test_detail_leaves_no_scratch_datablock(m):
    """Default elision builds a throwaway tree; it must not survive the read.

    Also holds when a read fails partway, which is the case that would
    otherwise leave junk in a user's file after an unrelated error.
    """
    import bpy
    tree = bpy.data.node_groups.new("__detail3__", "GeometryNodeTree")
    try:
        tree.nodes.new("ShaderNodeMath")
        before = {g.name for g in bpy.data.node_groups}
        m.geonodes.node_detail(tree)
        assert {g.name for g in bpy.data.node_groups} == before, "leaked scratch"
    finally:
        bpy.data.node_groups.remove(tree)


def test_detail_separates_boundary_links(m):
    """A frame's links to the outside are its inputs and outputs."""
    import bpy
    tree = bpy.data.node_groups.new("__detail4__", "GeometryNodeTree")
    try:
        frame = tree.nodes.new("NodeFrame")
        frame.label = "inner"
        a = tree.nodes.new("ShaderNodeMath")
        b = tree.nodes.new("ShaderNodeMath")
        a.parent = frame
        b.parent = frame
        outside = tree.nodes.new("ShaderNodeMath")
        tree.links.new(a.outputs[0], b.inputs[0])       # internal
        tree.links.new(outside.outputs[0], a.inputs[0])  # boundary

        detail = m.geonodes.node_detail(tree, "inner")
        assert len(detail["nodes"]) == 2, detail["nodes"]
        assert len(detail["links"]) == 1, detail["links"]
        assert len(detail["boundary_links"]) == 1, detail["boundary_links"]

        # frame=None reads what is outside every frame - the only way into a
        # tree with no frames at all.
        assert len(m.geonodes.node_detail(tree)["nodes"]) == 1
    finally:
        bpy.data.node_groups.remove(tree)


def test_detail_names_known_frames_when_missing(m):
    import bpy
    tree = bpy.data.node_groups.new("__detail5__", "GeometryNodeTree")
    try:
        frame = tree.nodes.new("NodeFrame")
        frame.label = "real frame"
        m.geonodes.node_detail(tree, "nope")
    except ValueError as exc:
        assert "real frame" in str(exc), exc
    else:
        raise AssertionError("expected ValueError naming the known frames")
    finally:
        bpy.data.node_groups.remove(tree)


def _mesh_object(name="__val_obj__"):
    """A one-quad mesh object, linked to the scene so it gets evaluated."""
    import bpy
    mesh = bpy.data.meshes.new(name + "_mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [],
                     [(0, 1, 2, 3)])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj, mesh


def _passthrough(tree):
    """Group Input -> Group Output, the minimum tree that validates clean."""
    tree.interface.new_socket("Geometry", in_out="INPUT",
                              socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT",
                              socket_type="NodeSocketGeometry")
    gi = tree.nodes.new("NodeGroupInput")
    go = tree.nodes.new("NodeGroupOutput")
    tree.links.new(gi.outputs[0], go.inputs[0])
    return gi, go


def test_validate_finds_static_faults(m):
    import bpy
    tree = bpy.data.node_groups.new("__val1__", "GeometryNodeTree")
    try:
        def kinds():
            return [p["kind"] for p in
                    m.geonodes.validate_tree(tree, evaluate=False)["problems"]]

        assert "no_group_output" in kinds(), kinds()

        tree.nodes.new("GeometryNodeRepeatInput")  # zone with no partner
        assert "unpaired_zone" in kinds(), kinds()

        out = tree.nodes.new("NodeGroupOutput")
        assert "group_output_unconnected" in kinds(), kinds()

        grid = tree.nodes.new("GeometryNodeMeshGrid")
        setpos = tree.nodes.new("GeometryNodeSetPosition")
        # Geometry into a boolean Selection socket: Blender keeps the link but
        # flags it invalid, which is its only static "broken" signal.
        tree.links.new(grid.outputs["Mesh"], setpos.inputs["Selection"])
        tree.links.new(setpos.outputs["Geometry"], out.inputs[0])
        assert "invalid_link" in kinds(), kinds()
    finally:
        bpy.data.node_groups.remove(tree)


def test_validate_passes_a_healthy_tree(m):
    """Guards against a checker that simply always complains."""
    import bpy
    tree = bpy.data.node_groups.new("__val2__", "GeometryNodeTree")
    try:
        _passthrough(tree)
        report = m.geonodes.validate_tree(tree, evaluate=False)
        assert report["ok"] is True, report
        assert report["problems"] == [], report
    finally:
        bpy.data.node_groups.remove(tree)


def test_validate_catches_silently_empty_output(m):
    """The failure with no error: evaluates fine, produces nothing.

    Blender raises no warning for this, so the evaluated geometry count is the
    only evidence that anything went wrong.
    """
    import bpy
    tree = bpy.data.node_groups.new("__val3__", "GeometryNodeTree")
    obj, mesh = _mesh_object("__val3_obj__")
    try:
        gi, go = _passthrough(tree)
        tree.links.clear()
        delete = tree.nodes.new("GeometryNodeDeleteGeometry")
        delete.inputs["Selection"].default_value = True
        tree.links.new(gi.outputs[0], delete.inputs["Geometry"])
        tree.links.new(delete.outputs["Geometry"], go.inputs[0])

        mod = obj.modifiers.new("GN", "NODES")
        mod.node_group = tree
        bpy.context.view_layer.update()

        report = m.geonodes.validate_tree(tree)
        assert report["ok"] is False, report
        assert any(p["kind"] == "empty_output" for p in report["problems"]), report
        assert report["evaluation"][0]["output"]["vertices"] == 0, report
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)
        bpy.data.node_groups.remove(tree)


def test_validate_counts_modifiers_not_just_objects(m):
    """One object can carry the same group twice, and values are per modifier.

    Counting objects understates what a rebuild would destroy, so the modifier
    count is reported separately and is the number to respect.
    """
    import bpy
    tree = bpy.data.node_groups.new("__val4__", "GeometryNodeTree")
    obj, mesh = _mesh_object("__val4_obj__")
    try:
        _passthrough(tree)
        for i in range(2):
            obj.modifiers.new(f"GN{i}", "NODES").node_group = tree
        users = m.geonodes.tree_users(tree)
        assert users["objects"] == [obj.name], users
        assert users["modifier_count"] == 2, users
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)
        bpy.data.node_groups.remove(tree)


def _structural_signature(tree):
    """Everything about a tree that a faithful rebuild must preserve.

    Deliberately not the generated source: two correct rebuilds can differ in
    variable names and node names while being the same graph. This compares
    what the graph *is*.
    """
    return {
        "nodes": len(tree.nodes),
        "links": len(tree.links),
        "types": sorted(n.bl_idname for n in tree.nodes),
        "interface": [(i.name, i.in_out, getattr(i, "socket_type", None))
                      for i in tree.interface.items_tree],
        "zones": sorted((n.bl_idname, n.paired_output is not None)
                        for n in tree.nodes if hasattr(n, "paired_output")),
        "framed": sum(1 for n in tree.nodes if n.parent),
        "labels": sorted(n.label for n in tree.nodes if n.label),
    }


def _snapshot_fixture(tree):
    """A tree exercising the parts a naive serialiser loses."""
    tree.interface.new_socket("Geometry", in_out="INPUT",
                              socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT",
                              socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Amount", in_out="INPUT",
                              socket_type="NodeSocketFloat")
    gi = tree.nodes.new("NodeGroupInput")
    go = tree.nodes.new("NodeGroupOutput")

    frame = tree.nodes.new("NodeFrame")
    frame.label = "framed region"
    math = tree.nodes.new("ShaderNodeMath")
    math.operation = "SUBTRACT"
    math.label = "a labelled node"
    math.parent = frame

    zone_in = tree.nodes.new("GeometryNodeRepeatInput")
    zone_out = tree.nodes.new("GeometryNodeRepeatOutput")
    zone_in.pair_with_output(zone_out)

    tree.links.new(gi.outputs[0], go.inputs[0])
    return tree


def _undo_baseline(m, name):
    """A tree with one node, and a revert point that undo can land on.

    Every undo test needs a step of its own to return to, otherwise undoing
    walks back into whatever the previously-run test left behind.
    """
    import bpy
    tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
    tree.nodes.new("NodeGroupInput")
    m.undo.reset()
    bpy.ops.ed.undo_push(message=f"baseline for {name}")
    return tree


def _drop_tree(name):
    """Remove a tree by name, tolerating it having been undone out of existence.

    Cleanup cannot hold the datablock it created: an undo in the body of the
    test invalidates every reference taken before it.
    """
    import bpy
    tree = bpy.data.node_groups.get(name)
    if tree is not None:
        bpy.data.node_groups.remove(tree)


def test_labelled_write_reports_its_diff_without_dry_run(m):
    """A real write is fingerprinted too, not only a preview.

    The blast radius of an edit - what it created, deleted or renamed - is
    reported every time something is actually labelled and kept, so a caller
    never has to run a snippet twice (once to preview, once to keep) just to
    see what it touched.
    """
    tree = _undo_baseline(m, "__real_write__")
    try:
        result = m.BlenderDevMCPServer().execute_code(
            "bpy.data.node_groups['__real_write__'].nodes.new('ShaderNodeMath')",
            undo_label="add a math node")
        assert "dry_run" not in result, result
        assert "reverted" not in result, result
        # Adding a node inside an existing tree touches no watched datablock
        # name, so the diff is empty - and the write is real, not undone.
        assert result["changed"] == {}, result
        import bpy
        assert len(bpy.data.node_groups["__real_write__"].nodes) == 2
    finally:
        _drop_tree("__real_write__")


def test_labelled_write_reports_a_rename_without_dry_run(m):
    tree = _undo_baseline(m, "__real_rename__")
    try:
        result = m.BlenderDevMCPServer().execute_code(
            "bpy.data.node_groups['__real_rename__'].name = '__real_renamed__'",
            undo_label="rename tree")
        renamed = result["changed"]["node_groups"]["renamed"]
        assert {"from": "__real_rename__", "to": "__real_renamed__"} in renamed, result
        import bpy
        assert "__real_renamed__" in bpy.data.node_groups
    finally:
        _drop_tree("__real_rename__")
        _drop_tree("__real_renamed__")


def test_labelled_edit_is_revertible(m):
    """The whole point: a labelled write can be taken back in place."""
    import bpy
    tree = _undo_baseline(m, "__undo_ok__")
    try:
        server = m.BlenderDevMCPServer()
        result = server.execute_code(
            "bpy.data.node_groups['__undo_ok__'].nodes.new('ShaderNodeMath')",
            undo_label="add a math node")
        assert result["undo_budget"] == 1, result
        assert len(bpy.data.node_groups["__undo_ok__"].nodes) == 2

        report = server.undo_edit()
        assert report["undone"] == 1, report
        assert report["remaining_budget"] == 0, report
        # Re-fetched, not reused: the handle above is dead after the undo.
        assert len(bpy.data.node_groups["__undo_ok__"].nodes) == 1
    finally:
        _drop_tree("__undo_ok__")


def test_edit_that_raised_is_rolled_back(m):
    """A snippet that failed halfway has already changed the file.

    The revert point matters most here, so the push happens in a finally rather
    than only on the success path - and by default it is spent immediately.
    Partial state from a script that died is essentially never wanted, and
    leaving it applied only helps a caller who both noticed and knew to undo.
    """
    import bpy
    tree = _undo_baseline(m, "__undo_boom__")
    try:
        server = m.BlenderDevMCPServer()
        try:
            server.execute_code(
                "bpy.data.node_groups['__undo_boom__'].nodes.new('ShaderNodeMath')\n"
                "raise RuntimeError('halfway')",
                undo_label="edit that fails")
        except Exception as exc:
            assert "halfway" in str(exc), exc
            assert "rolled back" in str(exc), \
                f"the caller must be told the file was restored:\n{exc}"
        else:
            raise AssertionError("expected the snippet to raise")

        assert len(bpy.data.node_groups["__undo_boom__"].nodes) == 1, \
            "the half-applied edit should have been taken back"
        assert m.undo.budget() == 0, "the revert point was spent, not left"
    finally:
        _drop_tree("__undo_boom__")


def test_rollback_on_error_can_be_declined(m):
    """Opting out leaves the wreckage in place, with a revert point beside it."""
    import bpy
    tree = _undo_baseline(m, "__undo_keep__")
    try:
        server = m.BlenderDevMCPServer()
        try:
            server.execute_code(
                "bpy.data.node_groups['__undo_keep__'].nodes.new('ShaderNodeMath')\n"
                "raise RuntimeError('halfway')",
                undo_label="edit that fails", rollback_on_error=False)
        except Exception as exc:
            assert "still applied" in str(exc), exc
        else:
            raise AssertionError("expected the snippet to raise")

        assert len(bpy.data.node_groups["__undo_keep__"].nodes) == 2
        assert m.undo.budget() == 1
        server.undo_edit()
        assert len(bpy.data.node_groups["__undo_keep__"].nodes) == 1
    finally:
        _drop_tree("__undo_keep__")


def test_undo_all_steps_unwinds_the_whole_session(m):
    """The escape hatch after a bulk edit spread over several calls."""
    import bpy
    tree = _undo_baseline(m, "__undo_all__")
    try:
        server = m.BlenderDevMCPServer()
        for i in range(3):
            server.execute_code(
                "bpy.data.node_groups['__undo_all__'].nodes.new('ShaderNodeMath')",
                undo_label=f"add math node {i}")
        assert m.undo.budget() == 3
        assert len(bpy.data.node_groups["__undo_all__"].nodes) == 4

        report = server.undo_edit(all_steps=True)
        assert report["undone"] == 3, report
        assert report["remaining_budget"] == 0, report
        assert len(bpy.data.node_groups["__undo_all__"].nodes) == 1
    finally:
        _drop_tree("__undo_all__")


def test_undoing_several_edits_reverts_all_of_them(m):
    """One revert point is not one undo step.

    A labelled execute_code pushes a boundary entry as well as its own, so
    reverting K of them takes 2K-1 raw steps (measured on 5.1). Taking K
    instead lands between revert points and quietly leaves most of the edit
    applied, while reporting success - which is the worst way for a reverse
    gear to fail.
    """
    import bpy
    tree = _undo_baseline(m, "__undo_multi__")
    try:
        server = m.BlenderDevMCPServer()
        for i in range(3):
            server.execute_code(
                "bpy.data.node_groups['__undo_multi__'].nodes.new('ShaderNodeMath')",
                undo_label=f"add math node {i}")
        assert len(bpy.data.node_groups["__undo_multi__"].nodes) == 4

        report = server.undo_edit(steps=3)
        assert report["undone"] == 3, report
        assert report["raw_steps"] == 5, \
            f"expected 2*3-1 raw undo steps, got {report}"
        assert len(bpy.data.node_groups["__undo_multi__"].nodes) == 1, \
            "undoing every revert point must leave nothing of the edits"
    finally:
        _drop_tree("__undo_multi__")


def test_partial_revert_leaves_the_earlier_edits_alone(m):
    """Undoing one of three reverts exactly one."""
    import bpy
    tree = _undo_baseline(m, "__undo_partial__")
    try:
        server = m.BlenderDevMCPServer()
        for i in range(3):
            server.execute_code(
                "bpy.data.node_groups['__undo_partial__'].nodes.new('ShaderNodeMath')",
                undo_label=f"add math node {i}")
        server.undo_edit(steps=1)
        assert len(bpy.data.node_groups["__undo_partial__"].nodes) == 3, \
            "one revert point should take back exactly one edit"
        assert m.undo.budget() == 2
    finally:
        _drop_tree("__undo_partial__")


def test_undo_all_steps_on_an_empty_budget_is_not_an_error(m):
    # "put everything back" is a reasonable thing to ask when nothing is
    # outstanding, and refusing with an exception would make it unsafe to call
    # defensively - which is exactly when it would be called.
    m.undo.reset()
    report = m.BlenderDevMCPServer().undo_edit(all_steps=True)
    assert report["undone"] == 0, report


# ---------------------------------------------------------------- dry run

def test_dry_run_reports_the_change_and_keeps_none_of_it(m):
    import bpy
    tree = _undo_baseline(m, "__dry_make__")
    try:
        result = m.BlenderDevMCPServer().execute_code(
            "bpy.data.node_groups.new('__dry_spawned__', 'GeometryNodeTree')",
            dry_run=True)
        assert result["dry_run"] is True and result["reverted"] is True, result
        created = result["changed"]["node_groups"]["created"]
        assert "__dry_spawned__" in created, result
        assert bpy.data.node_groups.get("__dry_spawned__") is None, \
            "a dry run must not leave the datablock behind"
    finally:
        _drop_tree("__dry_make__")
        _drop_tree("__dry_spawned__")


def test_dry_run_reports_a_rename_as_a_rename(m):
    """Identity by pointer, not by name.

    Comparing name sets alone would call this one deletion and one creation,
    which for a bulk rename means a report twice the size that never says the
    word "renamed". This is the property the whole diff is built on.
    """
    import bpy
    tree = _undo_baseline(m, "__dry_rename__")
    try:
        result = m.BlenderDevMCPServer().execute_code(
            "bpy.data.node_groups['__dry_rename__'].name = '__dry_renamed__'",
            dry_run=True)
        renamed = result["changed"]["node_groups"]["renamed"]
        assert {"from": "__dry_rename__", "to": "__dry_renamed__"} in renamed, result
        assert "created" not in result["changed"]["node_groups"], \
            f"a rename must not be reported as a creation: {result}"
        assert bpy.data.node_groups.get("__dry_rename__") is not None, \
            "the original name should be back"
    finally:
        _drop_tree("__dry_rename__")
        _drop_tree("__dry_renamed__")


def test_dry_run_reverts_even_when_the_code_raises(m):
    """The half-applied preview is the one that most needs putting back."""
    import bpy
    tree = _undo_baseline(m, "__dry_boom__")
    try:
        try:
            m.BlenderDevMCPServer().execute_code(
                "bpy.data.node_groups.new('__dry_partial__', 'GeometryNodeTree')\n"
                "raise RuntimeError('halfway')",
                dry_run=True)
        except Exception as exc:
            assert "halfway" in str(exc), exc
            assert "Nothing in the blend file was kept" in str(exc), exc
            assert "__dry_partial__" in str(exc), \
                f"the report should say what it had done before failing:\n{exc}"
        else:
            raise AssertionError("expected the snippet to raise")
        assert bpy.data.node_groups.get("__dry_partial__") is None, \
            "a failed dry run must still be reverted"
    finally:
        _drop_tree("__dry_boom__")
        _drop_tree("__dry_partial__")


def test_dry_run_of_read_only_code_reports_no_change(m):
    tree = _undo_baseline(m, "__dry_read__")
    try:
        result = m.BlenderDevMCPServer().execute_code(
            "print(len(bpy.data.objects))", dry_run=True)
        assert result["changed"] == {}, result
        assert result["result"].strip().isdigit(), result
    finally:
        _drop_tree("__dry_read__")


def test_dry_run_spends_its_own_revert_point(m):
    # A preview that leaves budget behind would make undo_edit walk back into
    # an edit the caller was told had already been undone.
    tree = _undo_baseline(m, "__dry_budget__")
    try:
        m.BlenderDevMCPServer().execute_code(
            "bpy.data.node_groups.new('__dry_budget_spawn__', 'GeometryNodeTree')",
            dry_run=True)
        assert m.undo.budget() == 0, "a reverted dry run should own no steps"
    finally:
        _drop_tree("__dry_budget__")
        _drop_tree("__dry_budget_spawn__")


def test_dry_run_sees_renames_of_subitems(m):
    """Bones and vertex groups are not datablocks, and get renamed constantly."""
    import bpy
    mesh = bpy.data.meshes.new("__dry_vg_mesh__")
    obj = bpy.data.objects.new("__dry_vg_obj__", mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.vertex_groups.new(name="original")
    m.undo.reset()
    bpy.ops.ed.undo_push(message="baseline for subitem dry run")
    try:
        result = m.BlenderDevMCPServer().execute_code(
            "bpy.data.objects['__dry_vg_obj__'].vertex_groups['original']"
            ".name = 'renamed'",
            dry_run=True)
        renamed = result["changed"]["vertex_groups"]["renamed"]
        assert {"from": "original", "to": "renamed"} in renamed, result
    finally:
        for name in ("__dry_vg_obj__",):
            leftover = bpy.data.objects.get(name)
            if leftover is not None:
                bpy.data.objects.remove(leftover)
        leftover = bpy.data.meshes.get("__dry_vg_mesh__")
        if leftover is not None:
            bpy.data.meshes.remove(leftover)


def test_dry_run_caps_long_lists_but_keeps_the_count(m):
    tree = _undo_baseline(m, "__dry_many__")
    try:
        result = m.BlenderDevMCPServer().execute_code(
            "for i in range(30):\n"
            "    bpy.data.node_groups.new(f'__dry_many_{i}__', 'GeometryNodeTree')",
            dry_run=True, max_diff_items=5)
        entry = result["changed"]["node_groups"]
        assert len(entry["created"]) == 5, entry
        assert entry["created_omitted"] == 25, entry
        assert result["totals"]["node_groups.created"] == 30, result
    finally:
        _drop_tree("__dry_many__")
        for i in range(30):
            _drop_tree(f"__dry_many_{i}__")


def test_dry_run_samples_both_ends_of_a_capped_list(m):
    """Truncation must not hide whatever sorts last.

    The lists are name-sorted, so a head-only cut drops the tail of the
    alphabet - and an unexpected entry is as likely to sort there as anywhere.
    """
    tree = _undo_baseline(m, "__dry_ends__")
    try:
        result = m.BlenderDevMCPServer().execute_code(
            "for i in range(30):\n"
            "    bpy.data.node_groups.new(f'__dry_ends_{i:02d}__', 'GeometryNodeTree')",
            dry_run=True, max_diff_items=6)
        created = result["changed"]["node_groups"]["created"]
        assert len(created) == 6, created
        assert created[0] == "__dry_ends_00__", created
        assert created[-1] == "__dry_ends_29__", created
        assert result["changed"]["node_groups"]["created_omitted"] == 24, result
        assert result["totals"]["node_groups.created"] == 30, result
    finally:
        _drop_tree("__dry_ends__")
        for i in range(30):
            _drop_tree(f"__dry_ends_{i:02d}__")


def test_dry_run_does_not_cap_an_ordinary_edit(m):
    """The default cap exists for bulk renames, not for a mesh's shape keys."""
    tree = _undo_baseline(m, "__dry_uncapped__")
    try:
        result = m.BlenderDevMCPServer().execute_code(
            "for i in range(34):\n"
            "    bpy.data.node_groups.new(f'__dry_uncapped_{i:02d}__', 'GeometryNodeTree')",
            dry_run=True)
        entry = result["changed"]["node_groups"]
        assert len(entry["created"]) == 34, entry
        assert "created_omitted" not in entry, entry
    finally:
        _drop_tree("__dry_uncapped__")
        for i in range(34):
            _drop_tree(f"__dry_uncapped_{i:02d}__")


def test_unlabelled_edit_is_not_undoable(m):
    """No label means no revert point - and the tool says so rather than guessing."""
    import bpy
    tree = _undo_baseline(m, "__undo_bare__")
    try:
        server = m.BlenderDevMCPServer()
        server.execute_code(
            "bpy.data.node_groups['__undo_bare__'].nodes.new('ShaderNodeMath')")
        assert m.undo.budget() == 0
        try:
            server.undo_edit()
        except ValueError as exc:
            assert "not pushed any revert points" in str(exc), exc
        else:
            raise AssertionError("undo should refuse with nothing pushed")
        # The edit is still there, which is the honest outcome: it was never
        # registered, so it is the user's to unwind, not ours.
        assert len(bpy.data.node_groups["__undo_bare__"].nodes) == 2
    finally:
        _drop_tree("__undo_bare__")


def test_undo_refuses_to_outrun_what_it_pushed(m):
    """The only guard against consuming the user's own edit history."""
    tree = _undo_baseline(m, "__undo_greedy__")
    try:
        server = m.BlenderDevMCPServer()
        server.execute_code(
            "bpy.data.node_groups['__undo_greedy__'].nodes.new('ShaderNodeMath')",
            undo_label="one edit")
        try:
            server.undo_edit(steps=5)
        except ValueError as exc:
            assert "only 1" in str(exc), exc
        else:
            raise AssertionError("undo should refuse to exceed its budget")
        assert m.undo.budget() == 1, "a refused undo must not spend budget"
    finally:
        server.undo_edit()
        _drop_tree("__undo_greedy__")


def test_annotating_is_revertible(m):
    """Annotation goes through the same counter, so it can be taken back too."""
    import bpy
    tree = _undo_baseline(m, "__undo_note__")
    try:
        node = tree.nodes[0]
        name = node.name
        server = m.BlenderDevMCPServer()
        server.annotate_node_tree("__undo_note__", labels={name: "the input"})
        assert bpy.data.node_groups["__undo_note__"].nodes[0].label == "the input"
        assert m.undo.budget() == 1

        server.undo_edit()
        assert bpy.data.node_groups["__undo_note__"].nodes[0].label == ""
    finally:
        _drop_tree("__undo_note__")


def test_references_do_not_survive_an_undo(m):
    """Measured Blender behaviour this design depends on, pinned as a test.

    If this ever stops raising, the re-fetch-by-name discipline in the undo
    tooling is no longer load-bearing - but until then, anything that caches a
    datablock across a revert is holding a dangling pointer.
    """
    tree = _undo_baseline(m, "__undo_stale__")
    try:
        server = m.BlenderDevMCPServer()
        server.execute_code(
            "bpy.data.node_groups['__undo_stale__'].nodes.new('ShaderNodeMath')",
            undo_label="add a node")
        server.undo_edit()
        try:
            len(tree.nodes)
        except ReferenceError:
            pass
        else:
            raise AssertionError(
                "a reference held across an undo unexpectedly still works")
    finally:
        _drop_tree("__undo_stale__")


def _skip_without_ntp(m):
    try:
        m.ntp_bridge.load_exporter()
        return False
    except m.ntp_bridge.NTPUnavailable:
        print("      (skipped: NodeToPython not found)")
        return True


def test_snapshot_round_trips_zones_frames_and_interface(m):
    """Export -> exec -> compare. The property the whole snapshot idea rests on.

    Zones, frames, labels and the group interface are exactly what a naive
    node/link serialiser drops, so a round trip that keeps them is the evidence
    that generated Python is a trustworthy snapshot format.
    """
    import bpy
    if _skip_without_ntp(m):
        return
    tree = _snapshot_fixture(
        bpy.data.node_groups.new("__snap__", "GeometryNodeTree"))
    before = {g.name for g in bpy.data.node_groups}
    path = None
    try:
        meta = m.ntp_bridge.snapshot_tree(tree.name)
        path = meta["path"]
        assert meta["characters"] > 0, meta

        result = m.ntp_bridge.restore_snapshot(path)
        assert result["created"], result
        clone = bpy.data.node_groups[result["created"][0]]
        assert _structural_signature(clone) == _structural_signature(tree)
    finally:
        for group in [g for g in bpy.data.node_groups if g.name not in before]:
            bpy.data.node_groups.remove(group)
        bpy.data.node_groups.remove(tree)
        if path and os.path.isfile(path):
            os.remove(path)


def test_snapshot_prunes_only_its_own_old_files(m):
    """keep_last bounds the snapshot directory without touching anything else.

    Snapshots are meant to be taken before every risky edit, so unbounded they
    accumulate forever beside the user's .blend. The risk in fixing that is
    deleting the wrong file, so this checks both halves: the oldest snapshots
    of this tree go, and a neighbouring file with a different name stays.
    """
    import bpy
    if _skip_without_ntp(m):
        return
    tree = _snapshot_fixture(
        bpy.data.node_groups.new("__prune__", "GeometryNodeTree"))
    directory = m.ntp_bridge.snapshot_dir()
    os.makedirs(directory, exist_ok=True)
    bystander = os.path.join(directory, "__prune__-keep-me.txt")
    written = []
    try:
        with open(bystander, "w", encoding="utf-8") as handle:
            handle.write("not a snapshot")

        # Names carry a one-second-resolution timestamp, so distinct paths are
        # passed explicitly here rather than racing the clock three times.
        for stamp in ("20260101-000001", "20260101-000002", "20260101-000003"):
            path = os.path.join(directory, f"__prune__-{stamp}.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# placeholder\n")
            written.append(path)

        meta = m.ntp_bridge.snapshot_tree(tree.name, keep_last=2)
        written.append(meta["path"])

        surviving = sorted(
            f for f in os.listdir(directory)
            if f.startswith("__prune__-") and f.endswith(".py"))
        assert len(surviving) == 2, surviving
        assert os.path.basename(meta["path"]) in surviving, surviving
        assert "__prune__-20260101-000001.py" not in surviving, surviving
        assert os.path.isfile(bystander), "pruning deleted an unrelated file"

        # An explicit path is the caller's to manage, so nothing is pruned.
        explicit = os.path.join(directory, "__prune__-explicit.py")
        written.append(explicit)
        assert "pruned" not in m.ntp_bridge.snapshot_tree(
            tree.name, path=explicit, keep_last=1)
    finally:
        bpy.data.node_groups.remove(tree)
        for path in written + [bystander]:
            if os.path.isfile(path):
                os.remove(path)


def test_restore_creates_rather_than_overwrites(m):
    """The safety property: a restore cannot clobber a tree objects are using."""
    import bpy
    if _skip_without_ntp(m):
        return
    tree = _snapshot_fixture(
        bpy.data.node_groups.new("__snap2__", "GeometryNodeTree"))
    before = {g.name for g in bpy.data.node_groups}
    path = None
    try:
        path = m.ntp_bridge.snapshot_tree(tree.name)["path"]
        created = m.ntp_bridge.restore_snapshot(path)["created"]
        assert tree.name in bpy.data.node_groups, "original was replaced"
        assert tree.name not in created, created
    finally:
        for group in [g for g in bpy.data.node_groups if g.name not in before]:
            bpy.data.node_groups.remove(group)
        bpy.data.node_groups.remove(tree)
        if path and os.path.isfile(path):
            os.remove(path)


def test_missing_node_to_python_is_reported_not_raised_bare(m):
    """An absent optional dependency should say what to do about it."""
    bridge = m.ntp_bridge
    real_env, real_defaults = bridge.NTP_PATH_ENV, bridge.DEFAULT_NTP_PATHS
    bridge.NTP_PATH_ENV = "__NO_SUCH_ENV__"
    bridge.DEFAULT_NTP_PATHS = ("/nonexistent/nodetopython",)
    try:
        bridge.load_exporter()
    except bridge.NTPUnavailable as exc:
        assert "headless" in str(exc), exc
    except ImportError:
        pass  # the installed-extension fallback is genuinely absent too
    finally:
        bridge.NTP_PATH_ENV, bridge.DEFAULT_NTP_PATHS = real_env, real_defaults


def test_annotate_sets_labels_and_reports_misses(m):
    """A label that hit no node must be reported, not silently dropped.

    Node names are autogenerated and shift as a tree is edited, so annotating
    against a stale name is the expected mistake rather than an exotic one.
    """
    import bpy
    tree = bpy.data.node_groups.new("__anno__", "GeometryNodeTree")
    try:
        node = tree.nodes.new("ShaderNodeMath")
        result = m.geonodes.annotate_tree(
            tree, labels={node.name: "seconds", "Math.999": "nope"},
            undo_push=False)
        assert node.label == "seconds", node.label
        assert len(result["applied"]) == 1, result
        assert result["skipped"][0]["target"] == "Math.999", result
        assert result["annotation"]["labelled_nodes"] == 1, result

        # None clears rather than writing the string "None".
        m.geonodes.annotate_tree(tree, labels={node.name: None},
                                 undo_push=False)
        assert node.label == "", repr(node.label)
    finally:
        bpy.data.node_groups.remove(tree)


def test_annotate_reuses_a_frame_with_the_same_label(m):
    """Re-running an annotation must not stack duplicate frames."""
    import bpy
    tree = bpy.data.node_groups.new("__anno2__", "GeometryNodeTree")
    try:
        a = tree.nodes.new("ShaderNodeMath")
        b = tree.nodes.new("ShaderNodeMath")
        spec = [{"label": "the group", "nodes": [a.name, b.name]}]

        first = m.geonodes.annotate_tree(tree, frames=spec, undo_push=False)
        assert first["applied"][0]["created"] is True, first
        frames = [n for n in tree.nodes if n.bl_idname == "NodeFrame"]
        assert len(frames) == 1, frames
        assert a.parent == frames[0] and b.parent == frames[0]

        second = m.geonodes.annotate_tree(tree, frames=spec, undo_push=False)
        assert second["applied"][0]["created"] is False, second
        assert len([n for n in tree.nodes
                    if n.bl_idname == "NodeFrame"]) == 1, "frame duplicated"
    finally:
        bpy.data.node_groups.remove(tree)


def test_annotate_leaves_no_empty_frame_when_nodes_are_missing(m):
    import bpy
    tree = bpy.data.node_groups.new("__anno3__", "GeometryNodeTree")
    try:
        result = m.geonodes.annotate_tree(
            tree, frames=[{"label": "ghost", "nodes": ["Math.999"]}],
            undo_push=False)
        assert not [n for n in tree.nodes
                    if n.bl_idname == "NodeFrame"], "left an empty frame"
        assert result["skipped"][0]["target"] == "ghost", result
    finally:
        bpy.data.node_groups.remove(tree)


def test_annotate_does_not_change_evaluation(m):
    """The safety claim: annotating is the write that cannot break a tree."""
    import bpy
    tree = bpy.data.node_groups.new("__anno4__", "GeometryNodeTree")
    obj, mesh = _mesh_object("__anno4_obj__")
    try:
        _passthrough(tree)
        mod = obj.modifiers.new("GN", "NODES")
        mod.node_group = tree
        bpy.context.view_layer.update()
        before = m.geonodes.validate_tree(tree)

        node = next(n for n in tree.nodes if n.bl_idname == "NodeGroupInput")
        m.geonodes.annotate_tree(
            tree, labels={node.name: "the input"},
            frames=[{"label": "everything", "nodes": [node.name]}],
            undo_push=False)

        after = m.geonodes.validate_tree(tree)
        assert after["ok"] is True, after
        assert after["evaluation"][0]["output"] == \
            before["evaluation"][0]["output"], (before, after)
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)
        bpy.data.node_groups.remove(tree)


def main():
    module = _load()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test(module)
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
