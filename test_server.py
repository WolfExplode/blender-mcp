"""Tests for the MCP server side (src/blender_dev_mcp/server.py).

This half never imports bpy, so it runs under a plain interpreter - no Blender,
no headless runner:

    python test_server.py
"""
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from blender_dev_mcp.server import BlenderConnection  # noqa: E402


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
    except Exception as exc:
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
        srv.get_blender_connection()
    except Exception as exc:
        assert "9999" in str(exc), f"error should name the port it tried: {exc}"
        return
    finally:
        srv._blender_connection = saved_conn
        if saved_port is None:
            os.environ.pop("BLENDER_PORT", None)
        else:
            os.environ["BLENDER_PORT"] = saved_port
    raise AssertionError("connecting to a dead port should raise")


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
