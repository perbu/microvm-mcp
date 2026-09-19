from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from microvm_mcp.errors import ServiceError
from microvm_mcp.firecracker import LIFECYCLE_PROTOCOL, Firecracker


@pytest.fixture
def backend():
    # No VMM or host commands: inspect lifecycle ordering and failure handling.
    backend = object.__new__(Firecracker)
    backend.network = SimpleNamespace(create=Mock(), destroy=Mock())
    backend.launch = Mock()
    backend.stop = Mock()
    backend.api = Mock()
    backend.guest = Mock(return_value={"synchronized": True})
    backend.wait_guest = Mock()
    return backend


@pytest.mark.parametrize("previous", ["paused", "hibernated"])
def test_resume_synchronizes_before_returning(backend, previous):
    order = []
    backend.api.side_effect = lambda *a: order.append("vmm")
    backend.wait_guest.side_effect = lambda *a: order.append("ready")
    backend.guest.side_effect = lambda *a: order.append("sync") or {"synchronized": True}
    backend.resume({"lifecycle_protocol": LIFECYCLE_PROTOCOL}, previous)
    assert order == ["vmm", "ready", "sync"]
    assert backend.guest.call_args.args[1] == "sync_clock"
    assert type(backend.guest.call_args.args[2]["realtime_ns"]) is int
    if previous == "hibernated":
        assert backend.api.call_args.args[3]["clock_realtime"] is False


@pytest.mark.parametrize("previous", ["paused", "hibernated"])
def test_clock_failure_stops_vm_instead_of_reporting_ready(backend, previous):
    backend.guest.side_effect = ServiceError("guest_error", "clock failure")
    vm = {"lifecycle_protocol": LIFECYCLE_PROTOCOL}
    with pytest.raises(ServiceError):
        backend.resume(vm, previous)
    backend.stop.assert_called_once_with(vm)
    backend.network.destroy.assert_called_once_with(vm)


def test_old_checkpoint_is_rejected_before_launch(backend):
    with pytest.raises(ServiceError, match="Recreate"):
        backend.resume({}, "hibernated")
    backend.launch.assert_not_called()
    backend.api.assert_not_called()


def test_old_guest_image_is_rejected_without_retrying(backend):
    backend.guest.return_value = {"active": False, "jobs": {}}
    with pytest.raises(ServiceError, match="Rebuild"):
        Firecracker.wait_guest(backend, {})
    backend.guest.assert_called_once()


def test_pause_freezes_guest_jobs_before_stopping_vcpus(backend):
    order = []
    backend.guest.side_effect = lambda *a: order.append(a[1]) or {"frozen": True}
    backend.api.side_effect = lambda *a: order.append(a[3]["state"])
    backend.suspend({}, "pause", "running")
    assert order == ["prepare_suspend", "Paused"]


def test_failed_freeze_stops_vm_and_does_not_pause_vcpus(backend):
    backend.guest.return_value = {"frozen": False}
    vm = {}
    with pytest.raises(ServiceError, match="freeze"):
        backend.suspend(vm, "pause", "running")
    backend.api.assert_not_called()
    backend.stop.assert_called_once_with(vm)
    backend.network.destroy.assert_called_once_with(vm)
