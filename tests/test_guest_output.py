"""Exercise output framing only; never execute guest commands on the host."""

import base64
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def guest():
    path = Path(__file__).parents[1] / "guest/supervisor.py"
    spec = importlib.util.spec_from_file_location("guest_output", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def job(stdout=b"", stderr=b"", *, state="completed", eof=(), truncated=False):
    return {
        "stdout": bytearray(stdout),
        "stderr": bytearray(stderr),
        "state": state,
        "exit_code": 0 if state == "completed" else None,
        "truncated": truncated,
        "timed_out": False,
        "eof": set(eof),
    }


def test_separate_readable_output_and_idempotent_cursor(guest):
    guest.JOBS["test"] = job(b'{"answer": 42}\n', "warning: æøå\n".encode())
    result = guest.handle("job_output", {"job_id": "test"})
    assert result["stdout"] == '{"answer": 42}\n'
    assert result["stderr"] == "warning: æøå\n"
    assert result["decoding_errors"] == {"stdout": False, "stderr": False}
    assert result["exit_code"] == 0
    assert guest.handle("job_output", {"job_id": "test"}) == result
    tail = guest.handle("job_output", {"job_id": "test", "cursor": result["next_cursor"]})
    assert tail["stdout"] == tail["stderr"] == ""
    assert tail["next_cursor"] == result["next_cursor"]


@pytest.mark.parametrize("encoding", ["utf-8", "base64"])
def test_pagination_preserves_both_streams(guest, encoding):
    stdout = ("a" * (guest.OUTPUT_CHUNK - 1) + "😀\n").encode()
    stderr = ("é" * guest.OUTPUT_CHUNK + "\tend\n").encode()
    state = job(stdout, stderr)
    cursor = {}
    collected = {"stdout": b"", "stderr": b""}
    for _ in range(10):
        result = guest.read_output(state, {"cursor": cursor, "encoding": encoding})
        for stream in collected:
            if encoding == "base64":
                chunk = base64.b64decode(result[stream + "_base64"])
            else:
                chunk = result[stream].encode()
                assert not result["decoding_errors"][stream]
            collected[stream] += chunk
        if result["next_cursor"] == cursor:
            break
        cursor = result["next_cursor"]
    else:
        pytest.fail("output did not drain")
    assert collected == {"stdout": stdout, "stderr": stderr}
    assert cursor == {"stdout": len(stdout), "stderr": len(stderr)}


def test_polling_waits_for_split_utf8_without_losing_bytes(guest):
    state = job(b"hello \xf0\x9f", b"diagnostic\n", state="running")
    first = guest.read_output(state, {})
    assert first["stdout"] == "hello "
    assert first["stderr"] == "diagnostic\n"
    cursor = first["next_cursor"]
    pending = guest.read_output(state, {"cursor": cursor})
    assert pending["stdout"] == ""
    assert pending["next_cursor"] == cursor
    assert not pending["decoding_errors"]["stdout"]
    state["stdout"].extend(b"\x98\x80")
    result = guest.read_output(state, {"cursor": cursor})
    assert result["stdout"] == "😀"
    assert result["stderr"] == ""
    assert result["next_cursor"]["stdout"] == len(state["stdout"])


@pytest.mark.parametrize("finish", ["eof", "completed", "truncated"])
def test_incomplete_final_utf8_is_replaced_and_flagged(guest, finish):
    state = job(b"\xe2\x82", state="running")
    if finish == "eof":
        state["eof"].add("stdout")
    elif finish == "completed":
        state["state"] = "completed"
    else:
        state["truncated"] = True
    result = guest.read_output(state, {})
    assert result["stdout"] == "�"
    assert result["decoding_errors"] == {"stdout": True, "stderr": False}
    assert result["next_cursor"]["stdout"] == 2


def test_invalid_bytes_flagged_but_literal_replacement_character_is_valid(guest):
    state = job(b"\xff\x00\n", "�".encode())
    result = guest.read_output(state, {})
    assert result["stdout"] == "�\x00\n"
    assert result["stderr"] == "�"
    assert result["decoding_errors"] == {"stdout": True, "stderr": False}
    raw = guest.read_output(state, {"encoding": "base64"})
    assert base64.b64decode(raw["stdout_base64"]) == b"\xff\x00\n"
    assert base64.b64decode(raw["stderr_base64"]) == "�".encode()
    assert "decoding_errors" not in raw


@pytest.mark.parametrize("cursor", [{"stdout": -1}, {"stderr": 1}, {"stdout": True}])
def test_invalid_cursors_rejected(guest, cursor):
    with pytest.raises(ValueError, match="cursor"):
        guest.read_output(job(), {"cursor": cursor})
