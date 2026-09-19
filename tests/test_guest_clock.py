"""Lifecycle helper tests mock clock syscalls; no guest execution on the host."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def guest(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "guest_clock", Path(__file__).parents[1] / "guest/supervisor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CGROUP", tmp_path)
    (tmp_path / "cgroup.events").write_text("populated 1\nfrozen 1\n")
    (tmp_path / "cgroup.freeze").write_text("0")
    return module


def test_resume_sets_wall_time_before_thaw_and_preserves_remaining_timeout(guest, monkeypatch):
    mono = [10.0]
    monkeypatch.setattr(guest.time, "monotonic", lambda: mono[0])
    guest.JOBS["running"] = {"state": "running", "deadline": 15.0}
    guest.JOBS["completed"] = {"state": "completed", "deadline": 9.0}
    guest.handle("prepare_suspend", {})
    assert (guest.CGROUP / "cgroup.freeze").read_text() == "1"
    mono[0] = 40.0  # Plain VMM pause advanced CLOCK_MONOTONIC by 30 seconds.
    calls = []

    def settime(clock, value):
        assert clock == guest.time.CLOCK_REALTIME
        assert (guest.CGROUP / "cgroup.freeze").read_text() == "1"
        calls.append(value)

    monkeypatch.setattr(guest.time, "clock_settime_ns", settime)
    assert guest.handle("sync_clock", {"realtime_ns": 123456789000}) == {"synchronized": True}
    assert calls == [123456789000]
    assert guest.JOBS["running"]["deadline"] - mono[0] == 5.0
    assert guest.JOBS["completed"]["deadline"] == 9.0
    assert guest.SUSPENDED_AT is None
    assert (guest.CGROUP / "cgroup.freeze").read_text() == "0"
    # A retried synchronization must not extend timeouts twice.
    monkeypatch.setattr(guest.time, "clock_settime_ns", lambda *_: None)
    guest.handle("sync_clock", {"realtime_ns": 123456789001})
    assert guest.JOBS["running"]["deadline"] == 45.0


def test_hibernate_with_stopped_monotonic_clock_does_not_add_host_downtime(guest, monkeypatch):
    monkeypatch.setattr(guest.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(guest.time, "clock_settime_ns", lambda *_: None)
    guest.JOBS["running"] = {"state": "running", "deadline": 15.0}
    guest.handle("prepare_suspend", {})
    guest.handle("sync_clock", {"realtime_ns": 9_000_000_000_000})
    assert guest.JOBS["running"]["deadline"] == 15.0


def test_failed_clock_correction_keeps_jobs_frozen(guest, monkeypatch):
    guest.handle("prepare_suspend", {})

    def fail(*_):
        raise PermissionError("clock syscall denied")

    monkeypatch.setattr(guest.time, "clock_settime_ns", fail)
    with pytest.raises(PermissionError):
        guest.handle("sync_clock", {"realtime_ns": 123})
    assert guest.SUSPENDED_AT is not None
    assert (guest.CGROUP / "cgroup.freeze").read_text() == "1"
    with pytest.raises(ValueError, match="frozen"):
        guest.handle("exec", {})


def test_repeated_prepare_does_not_lose_start_of_pause(guest, monkeypatch):
    mono = [10.0]
    monkeypatch.setattr(guest.time, "monotonic", lambda: mono[0])
    guest.handle("prepare_suspend", {})
    mono[0] = 20.0
    guest.handle("prepare_suspend", {})
    assert guest.SUSPENDED_AT == 10.0


@pytest.mark.parametrize("value", [True, -1, 1.5, "123", 2**63])
def test_invalid_host_time_is_rejected_before_syscall(guest, monkeypatch, value):
    monkeypatch.setattr(guest.time, "clock_settime_ns", lambda *_: pytest.fail("invalid syscall"))
    with pytest.raises(ValueError, match="invalid host time"):
        guest.handle("sync_clock", {"realtime_ns": value})
