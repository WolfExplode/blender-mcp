"""Tests for the MCP server side (src/blender_dev_mcp/server.py).

This half never imports bpy, so it runs under a plain interpreter - no Blender,
no headless runner:

    python test_server.py
"""
import json
import os
import socket
import sys
import threading
import time
from contextlib import suppress

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from blender_dev_mcp.server import (  # noqa: E402
    BlenderConnection, IncompleteResponse, NotConnected, READ_ONLY_COMMANDS,
)


class FakeSocket:
    """Socket stand-in that replays scripted recv() chunks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.sent = []
        self.recv_calls = 0

    def recv(self, _size):
        self.recv_calls += 1
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, _t):
        pass

    def close(self):
        pass


def _conn(chunks):
    conn = BlenderConnection(host="test", port=0)
    conn.sock = FakeSocket(chunks)
    return conn


def _chunked(payload, size):
    return [payload[i:i + size] for i in range(0, len(payload), size)]


# ---------------------------------------------------------------- reassembly

def test_whole_response_in_one_recv():
    conn = _conn([b'{"status": "success", "result": {"a": 1}}'])
    got = conn.receive_full_response(conn.sock)
    assert json.loads(got)["result"] == {"a": 1}, got


def test_response_split_across_recvs():
    conn = _conn([b'{"status": "success", "resu', b'lt": {"a": 1}}'])
    got = conn.receive_full_response(conn.sock)
    assert json.loads(got)["result"] == {"a": 1}, got


def test_multibyte_split_across_recvs():
    # Hardening, not a live bug: the addon replies via json.dumps with the
    # default ensure_ascii=True, so today the wire is pure ASCII and a
    # character can never straddle a chunk. If that ever changes, decode()
    # raises UnicodeDecodeError - which is not a JSONDecodeError, so it would
    # escape the retry loop and kill the call rather than reading on.
    payload = json.dumps({"status": "success", "result": "日本語のシェイプキー"},
                         ensure_ascii=False).encode("utf-8")
    conn = _conn(_chunked(payload, 7))
    got = conn.receive_full_response(conn.sock)
    assert json.loads(got)["result"] == "日本語のシェイプキー", got


def test_nested_braces_do_not_end_the_response_early():
    payload = json.dumps({"status": "success",
                          "result": {"outer": {"inner": {"deep": 1}}, "tail": 2}}).encode()
    conn = _conn(_chunked(payload, 5))
    got = json.loads(conn.receive_full_response(conn.sock))
    assert got["result"]["tail"] == 2, got


def test_brace_inside_a_string_does_not_end_the_response_early():
    payload = json.dumps({"status": "success", "result": "a } inside a string"}).encode()
    conn = _conn(_chunked(payload, 4))
    got = json.loads(conn.receive_full_response(conn.sock))
    assert got["result"] == "a } inside a string", got


def test_closed_connection_before_any_data_raises():
    conn = _conn([])
    try:
        conn.receive_full_response(conn.sock)
    except IncompleteResponse as exc:
        # Must be IncompleteResponse specifically, not a bare Exception: only
        # then does _attempt classify it as a transport failure and drop the
        # socket. As a bare Exception it escaped uncaught and the dead socket
        # stayed pooled, costing an extra failed call before recovery.
        assert "closed" in str(exc).lower(), exc
        return
    raise AssertionError("expected an error when the peer closes immediately")


def test_large_response_reassembly_is_not_quadratic():
    # A big execute_blender_code dump arrives as thousands of chunks. Parsing
    # the whole accumulated buffer after every one is O(n^2); this guards the
    # cheap "can't be complete yet" check that avoids it.
    payload = json.dumps({"status": "success", "result": "x" * 4_000_000}).encode()
    conn = _conn(_chunked(payload, 8192))
    start = time.time()
    got = conn.receive_full_response(conn.sock)
    elapsed = time.time() - start
    assert len(json.loads(got)["result"]) == 4_000_000
    assert elapsed < 5.0, f"reassembly took {elapsed:.1f}s - likely quadratic again"


# ---------------------------------------------------------------- send_command

def test_error_status_raises_with_message():
    conn = _conn([b'{"status": "error", "message": "no such object"}'])
    try:
        conn.send_command("get_object_info", {"name": "nope"})
    except Exception as exc:
        assert "no such object" in str(exc), exc
        return
    raise AssertionError("an error status should raise")


def test_success_returns_result_payload():
    conn = _conn([b'{"status": "success", "result": {"name": "Cube"}}'])
    assert conn.send_command("get_object_info") == {"name": "Cube"}


def test_command_is_sent_as_json():
    conn = _conn([b'{"status": "success", "result": {}}'])
    conn.send_command("execute_code", {"code": "print(1)"})
    sent = json.loads(conn.sock.sent[0])
    assert sent == {"type": "execute_code", "params": {"code": "print(1)"}}, sent


def test_missing_params_sends_empty_dict():
    conn = _conn([b'{"status": "success", "result": {}}'])
    conn.send_command("get_scene_info")
    assert json.loads(conn.sock.sent[0])["params"] == {}


def test_timeout_invalidates_the_socket():
    class TimingOutSocket(FakeSocket):
        def recv(self, _size):
            raise socket.timeout()

    conn = BlenderConnection(host="test", port=0)
    conn.sock = TimingOutSocket([])
    try:
        conn.send_command("get_scene_info")
    except Exception as exc:
        assert "Timeout" in str(exc), exc
        assert conn.sock is None, "a timed-out socket must be dropped so the next call reconnects"
        return
    raise AssertionError("expected a timeout error")


def test_invalid_json_response_invalidates_the_socket():
    # recv returns b"" after the garbage, so reassembly gives up.
    conn = _conn([b"not json"])
    try:
        conn.send_command("get_scene_info")
    except Exception:
        assert conn.sock is None, "an undecodable stream must drop the socket"
        return
    raise AssertionError("expected an error for a non-JSON response")


def test_timeout_message_names_the_override():
    class TimingOutSocket(FakeSocket):
        def recv(self, _size):
            raise socket.timeout()

    conn = BlenderConnection(host="test", port=0)
    conn.sock = TimingOutSocket([])
    try:
        conn.send_command("get_scene_info")
    except Exception as exc:
        assert "BLENDER_MCP_TIMEOUT" in str(exc), exc
        return
    raise AssertionError("expected a timeout error")


def test_connect_failure_names_the_address():
    import blender_dev_mcp.server as srv

    saved_conn, saved_port = srv._blender_connection, os.environ.get("BLENDER_PORT")
    srv._blender_connection = None
    os.environ["BLENDER_PORT"] = "9999"  # nothing listens here
    try:
        srv.get_blender_connection().send_command("get_scene_info")
    except Exception as exc:
        assert "9999" in str(exc), f"error should name the port it tried: {exc}"
        return
    finally:
        srv._blender_connection = saved_conn
        if saved_port is None:
            os.environ.pop("BLENDER_PORT", None)
        else:
            os.environ["BLENDER_PORT"] = saved_port
    raise AssertionError("commanding a dead port should raise")


def test_unreachable_blender_is_not_reported_as_a_lost_connection():
    conn = BlenderConnection(host="127.0.0.1", port=9999)  # nothing listens here
    try:
        conn.send_command("get_scene_info")
    except NotConnected as exc:
        assert "lost" not in str(exc).lower(), \
            f"a connection that never existed was not 'lost': {exc}"
        return
    raise AssertionError("expected NotConnected")


# ------------------------------------------------- stale pooled socket recovery

class StaleThenLiveConnection(BlenderConnection):
    """Pools a socket whose peer has already hung up, then reconnects to a live one.

    Models the real failure: reloading addons in Blender restarts the companion
    server and orphans every pooled socket, which still looks healthy until
    something is written to it.
    """

    def __init__(self, live_chunks, **kw):
        super().__init__(host="test", port=0, **kw)
        self._live_chunks = live_chunks
        self.connects = 0
        self.sock = FakeSocket([])  # recv returns b"" -> peer already gone

    def connect(self):
        self.connects += 1
        self.sock = FakeSocket(list(self._live_chunks))
        return True


def test_stale_pooled_socket_is_replaced_and_the_read_is_retried():
    conn = StaleThenLiveConnection([b'{"status": "success", "result": {"name": "Scene"}}'])
    assert conn.send_command("get_scene_info") == {"name": "Scene"}
    assert conn.connects == 1, "should have reconnected exactly once"


def test_stale_socket_retry_is_transparent_to_the_caller():
    # The whole point: one dead pooled socket must not surface as a tool error.
    conn = StaleThenLiveConnection([b'{"status": "success", "result": {"ok": 1}}'])
    conn.send_command("get_object_info", {"name": "Cube"})
    sent = json.loads(conn.sock.sent[0])
    assert sent == {"type": "get_object_info", "params": {"name": "Cube"}}, sent


def test_mutating_command_is_not_replayed_on_a_stale_socket():
    # Replaying execute_code could apply the same edit twice, because a dead
    # socket cannot prove whether Blender ran the command before hanging up.
    conn = StaleThenLiveConnection([b'{"status": "success", "result": {}}'])
    try:
        conn.send_command("execute_code", {"code": "bpy.ops.mesh.primitive_cube_add()"})
    except Exception as exc:
        assert conn.connects == 0, "a mutating command must not be retried"
        assert "not retried automatically" in str(exc), exc
        return
    raise AssertionError("expected a mutating command to refuse the replay")


def test_retry_happens_only_once():
    class AlwaysStale(StaleThenLiveConnection):
        def connect(self):
            self.connects += 1
            self.sock = FakeSocket([])  # still dead
            return True

    conn = AlwaysStale([])
    try:
        conn.send_command("get_scene_info")
    except Exception as exc:
        assert conn.connects == 1, f"expected one retry, got {conn.connects}"
        assert "lost" in str(exc).lower(), exc
        return
    raise AssertionError("expected the second failure to surface")


def test_fresh_connection_failure_is_not_retried():
    # sock is None, so the very first attempt is already on a new socket.
    # Retrying that would just double every genuine outage.
    class CountingConnect(BlenderConnection):
        def __init__(self):
            super().__init__(host="test", port=0)
            self.connects = 0

        def connect(self):
            self.connects += 1
            self.sock = FakeSocket([])
            return True

    conn = CountingConnect()
    try:
        conn.send_command("get_scene_info")
    except Exception:
        assert conn.connects == 1, f"expected no retry, got {conn.connects} connects"
        return
    raise AssertionError("expected the failure to surface")


def test_oserror_family_is_treated_as_a_dropped_socket():
    # WSAECONNABORTED (10053) arrives as a plain OSError on Windows and used to
    # slip past a handler that only listed ConnectionError/BrokenPipe/Reset.
    class AbortingSocket(FakeSocket):
        def sendall(self, _data):
            raise OSError(10053, "An established connection was aborted")

    conn = BlenderConnection(host="test", port=0)
    conn.sock = AbortingSocket([])
    try:
        conn.send_command("get_scene_info")
    except Exception:
        assert conn.sock is None, "an aborted socket must be dropped, not pooled"
        return
    raise AssertionError("expected an error")


def test_error_status_keeps_the_socket_pooled():
    # A well-formed {"status": "error"} means the transport is healthy; dropping
    # the socket would force a needless reconnect on every Blender-side error.
    conn = _conn([b'{"status": "error", "message": "no such object"}'])
    try:
        conn.send_command("get_object_info", {"name": "nope"})
    except Exception:
        assert conn.sock is not None, "a Blender-side error must not drop the socket"
        return
    raise AssertionError("an error status should raise")


def test_read_only_set_covers_only_side_effect_free_commands():
    mutating = {"execute_code", "undo_edit", "restore_node_snapshot",
                "annotate_node_tree", "snapshot_node_tree"}
    overlap = mutating & READ_ONLY_COMMANDS
    assert not overlap, f"these mutate and must never be auto-replayed: {overlap}"


# ------------------------------------- integration: real sockets, real restart

class FakeAddon:
    """The addon's socket server, reduced to its lifecycle over a real socket.

    Mirrors the fixed addon: stop() hangs up on accepted connections instead of
    leaving them open, and a handler that wakes to find the server stopped does
    not execute what it read.
    """

    def __init__(self):
        self.port = 0
        self.running = False
        self.served = []
        self._listener = None
        self._clients = []

    def start(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", self.port))
        self.port = self._listener.getsockname()[1]  # keep it across restarts
        self._listener.listen(5)
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        self._listener.settimeout(0.25)
        while self.running:
            try:
                client, _addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._clients.append(client)
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        while self.running:
            try:
                data = client.recv(8192)
            except OSError:
                break
            if not data or not self.running:
                break
            command = json.loads(data.decode("utf-8"))
            self.served.append(command["type"])
            with suppress(OSError):
                client.sendall(json.dumps(
                    {"status": "success", "result": {"ok": command["type"]}}
                ).encode("utf-8"))

    def stop(self):
        self.running = False
        for client in self._clients:
            with suppress(OSError):
                client.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                client.close()
        self._clients = []
        with suppress(OSError):
            self._listener.close()


def test_client_survives_an_addon_restart():
    """The bug this whole path exists for: reloading addons in Blender used to
    surface as two failed tool calls before anything worked again."""
    addon = FakeAddon()
    addon.start()
    try:
        conn = BlenderConnection(host="127.0.0.1", port=addon.port)
        assert conn.send_command("get_scene_info") == {"ok": "get_scene_info"}

        addon.stop()          # unregister()
        addon.start()         # register(), same port
        time.sleep(0.05)      # let the listener come up

        # No reconnect dance from the caller: this must just work.
        assert conn.send_command("get_scene_info") == {"ok": "get_scene_info"}
        assert addon.served == ["get_scene_info", "get_scene_info"], addon.served
    finally:
        addon.stop()


def test_mutating_command_is_not_replayed_across_an_addon_restart():
    addon = FakeAddon()
    addon.start()
    try:
        conn = BlenderConnection(host="127.0.0.1", port=addon.port)
        conn.send_command("get_scene_info")

        addon.stop()
        addon.start()
        time.sleep(0.05)

        try:
            conn.send_command("execute_code", {"code": "bpy.ops.object.delete()"})
        except Exception as exc:
            assert "not retried automatically" in str(exc), exc
            assert "execute_code" not in addon.served, \
                "a delete must never be replayed onto a reconnected Blender"
            return
        raise AssertionError("expected the mutating command to refuse the replay")
    finally:
        addon.stop()


def test_blender_going_away_entirely_is_reported():
    addon = FakeAddon()
    addon.start()
    conn = BlenderConnection(host="127.0.0.1", port=addon.port)
    conn.send_command("get_scene_info")
    addon.stop()  # and never comes back

    try:
        conn.send_command("get_scene_info")
    except Exception as exc:
        assert "9" in str(exc) or "connect" in str(exc).lower(), exc
        assert conn.sock is None, "no dead socket may be left pooled"
        return
    raise AssertionError("expected an error once Blender is gone")


# ------------------------------------------------------- offline Blender docs

def _docs_or_skip():
    """The docs are a gitignored ~2 GB download, so they may simply be absent."""
    from blender_dev_mcp import docs
    if docs.docs_root() is None:
        print("    (skipped: offline docs not installed)", end=" ")
        return None
    return docs


def test_docs_root_honours_the_env_override():
    from blender_dev_mcp import docs

    saved = os.environ.get(docs.DOCS_ENV)
    os.environ[docs.DOCS_ENV] = os.path.dirname(os.path.abspath(__file__))
    try:
        assert docs.docs_root() is not None, "an existing override dir must be used"
        os.environ[docs.DOCS_ENV] = os.path.join("no", "such", "place")
        assert docs.docs_root() is None, \
            "a bogus override must report absent, not silently fall back"
    finally:
        if saved is None:
            os.environ.pop(docs.DOCS_ENV, None)
        else:
            os.environ[docs.DOCS_ENV] = saved


def test_inventory_reports_the_blender_version_it_documents():
    docs = _docs_or_skip()
    if docs is None:
        return
    entries, version = docs.inventory()
    assert len(entries) > 1000, f"suspiciously small inventory: {len(entries)}"
    # The version stamp is the whole point: these docs must be attributable to a
    # release, unlike a source tree that always tracks main.
    assert version and "Blender" in version, version


def test_symbol_lookup_finds_an_attribute_by_bare_name():
    docs = _docs_or_skip()
    if docs is None:
        return
    hits = docs.find_symbols("set_inverse_pending")
    assert hits, "bare attribute names must resolve"
    names = [h[0] for h in hits]
    assert "bpy.types.ChildOfConstraint.set_inverse_pending" in names, names


def test_symbol_lookup_deduplicates():
    docs = _docs_or_skip()
    if docs is None:
        return
    hits = docs.find_symbols("bpy.types.ChildOfConstraint")
    names = [h[0] for h in hits]
    assert len(names) == len(set(names)), f"duplicate symbols returned: {names}"
    assert names[0] == "bpy.types.ChildOfConstraint", "exact match must rank first"


def test_page_text_drops_navigation_and_permalink_noise():
    docs = _docs_or_skip()
    if docs is None:
        return
    text = docs.page_text(docs.api_dir() / "bpy.types.ChildOfConstraint.html")
    assert "set_inverse_pending" in text, "the actual content must survive"
    assert "¶" not in text, "Sphinx permalink pilcrows must be stripped"
    assert "’" not in text and "“" not in text, \
        "curly quotes break copy-pasting an enum value into code"
    assert "Toggle table of contents" not in text, "theme chrome leaked in"
    assert "<div" not in text and "</p>" not in text, "raw markup leaked in"


def test_prose_search_finds_enum_values_absent_from_the_inventory():
    docs = _docs_or_skip()
    if docs is None:
        return
    # Enum members are page prose, not objects.inv entries - the fallback exists
    # precisely for them.
    assert not docs.find_symbols("TRACK_NEGATIVE_Z"), \
        "if this is indexed now, the fallback is no longer what is being tested"
    hits = docs.search_prose("TRACK_NEGATIVE_Z", limit=2)
    assert hits, "enum values must be findable somehow"
    assert any("Constraint" in path for path, _ in hits), hits


def test_prose_search_skips_generated_index_pages():
    docs = _docs_or_skip()
    if docs is None:
        return
    # genindex-all.html contains every term in the docs, so it matches anything
    # and explains nothing.
    hits = docs.search_prose("set_inverse_pending", limit=5)
    assert not any("genindex" in path for path, _ in hits), hits


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
