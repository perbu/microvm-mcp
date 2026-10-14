import sqlite3
from pathlib import Path

import pytest
from test_service import Backend, image

from microvm_mcp import images
from microvm_mcp.catalog import tools
from microvm_mcp.errors import ServiceError
from microvm_mcp.firecracker import Firecracker
from microvm_mcp.service import Service
from microvm_mcp.store import Store

DIGESTS = {"rootfs_sha256": "a" * 64, "kernel_sha256": "b" * 64}
LEGACY = {
    "state_dir": "/state",
    "image_id": "debian-13-dev-v1",
    "rootfs": "/images/debian.ext4",
    "kernel": "/images/vmlinux",
    **DIGESTS,
}


def test_legacy_top_level_image_is_the_single_default():
    loaded, default = images.load(LEGACY)
    assert default == "debian-13-dev-v1"
    assert loaded[default] == images.Image(
        "debian-13-dev-v1", Path("/images/debian.ext4"), Path("/images/vmlinux"), *DIGESTS.values()
    )


def test_image_tables_default_to_installed_bundle_paths():
    loaded, default = images.load(
        {
            "state_dir": "/state",
            "images": {
                "dev": {**DIGESTS, "default": True},
                "re": {**DIGESTS, "min_memory_mib": 4096, "rootfs": "/other/re.ext4"},
            },
        }
    )
    assert default == "dev"
    assert loaded["dev"].rootfs == Path("/state/images/dev/rootfs.ext4")
    assert loaded["dev"].kernel == Path("/state/images/dev/vmlinux")
    assert loaded["re"].rootfs == Path("/other/re.ext4")
    # Omitting memory_mib on an image with a minimum uses that minimum.
    assert (loaded["re"].memory_mib, loaded["re"].min_memory_mib) == (4096, 4096)
    assert (loaded["dev"].memory_mib, loaded["dev"].min_memory_mib) == (1024, 256)


@pytest.mark.parametrize(
    "config,message",
    [
        ({**LEGACY, "images": {"x": DIGESTS}}, "not both"),
        ({"state_dir": "/s", "image_id": "x"}, "missing image configuration"),
        ({"state_dir": "/s", "images": {}}, "at least one"),
        ({"state_dir": "/s", "images": {"a": DIGESTS, "b": DIGESTS}}, "exactly one"),
        (
            {
                "state_dir": "/s",
                "images": {"a": {**DIGESTS, "default": True}, "b": {**DIGESTS, "default": True}},
            },
            "exactly one",
        ),
        ({"state_dir": "/s", "images": {"../x": DIGESTS}}, "invalid image ID"),
        ({"state_dir": "/s", "images": {"a": {**DIGESTS, "min_memory": 1}}}, "unknown keys"),
        ({"state_dir": "/s", "images": {"a": {"rootfs_sha256": "a" * 64}}}, "kernel_sha256"),
        ({"state_dir": "/s", "images": {"a": {**DIGESTS, "min_memory_mib": 100}}}, "between"),
        (
            {
                "state_dir": "/s",
                "images": {"a": {**DIGESTS, "memory_mib": 512, "min_memory_mib": 1024}},
            },
            "below",
        ),
        ({"state_dir": "/s", "images": {"a": {**DIGESTS, "default": "yes"}}}, "true or false"),
    ],
)
def test_invalid_image_configuration_fails_closed(config, message):
    with pytest.raises(ValueError, match=message):
        images.load(config)


class TwoImages(Backend):
    images = {
        "dev": image("dev"),
        "re": image("re", memory_mib=4096, min_memory_mib=2048),
    }
    default_image = "dev"


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "state")
    backend = TwoImages()
    return Service(store, backend, max_memory_mib=65536), store, backend


def create(env, token, **kw):
    service, _, _ = env
    return service.call(token, "vm_create", {"request_id": "c-" + str(kw), **kw})


def test_vm_records_chosen_or_default_image_and_image_memory(env):
    service, store, _ = env
    token = store.issue("alice", max_vms=4, max_memory_mib=8192)
    default = create(env, token)
    assert (default["image"], default["memory_mib"]) == ("dev", 1024)
    chosen = create(env, token, image="re")
    assert (chosen["image"], chosen["memory_mib"]) == ("re", 4096)
    assert service.vms[chosen["id"]]["image"] == "re"
    with pytest.raises(ServiceError, match="memory_mib >= 2048"):
        create(env, token, image="re", memory_mib=1024)


def test_discovery_and_validation_list_only_configured_images(env):
    service, store, backend = env
    schema = service.by_name["vm_create"]["inputSchema"]["properties"]["image"]
    assert schema["enum"] == ["dev", "re"]
    # The static catalog used without a configuration accepts any well-formed ID.
    assert "pattern" in tools()[0]["inputSchema"]["properties"]["image"]
    token = store.issue("alice")
    with pytest.raises(ServiceError) as failure:
        create(env, token, image="other")
    assert failure.value.code == "invalid_arguments"
    assert not service.vms


def test_token_restricted_to_one_image_is_refused_for_the_other(env):
    service, store, _ = env
    token = store.issue("alice", max_vms=4, max_memory_mib=8192, images=["re"])
    with pytest.raises(ServiceError) as failure:
        create(env, token)
    assert (failure.value.code, failure.value.status) == ("forbidden", 403)
    assert create(env, token, image="re")["image"] == "re"
    # Rotation preserves the original restriction; it cannot widen it.
    rotated = store.issue("alice", images=["dev"])
    assert store.authenticate(rotated)["images"] == ["re"]
    with pytest.raises(ValueError, match="image restriction"):
        store.issue("bob", images=["../x"])


def test_removed_image_does_not_strand_hibernated_vm(env, tmp_path):
    service, store, backend = env
    token = store.issue("alice", max_memory_mib=8192)
    vm = create(env, token, image="re")["id"]
    service.call(token, "vm_suspend", {"vm_id": vm})

    class OnlyDev(TwoImages):
        images = {"dev": TwoImages.images["dev"]}

    restarted = Service(store, OnlyDev())
    assert restarted.call(token, "vm_resume", {"vm_id": vm})["state"] == "running"
    assert restarted.call(token, "vm_status", {"vm_id": vm})["image"] == "re"


def test_different_vmm_is_refused_on_resume(env):
    service, store, _ = env
    token = store.issue("alice")
    vm = create(env, token)["id"]
    service.call(token, "vm_suspend", {"vm_id": vm})

    class Upgraded(TwoImages):
        runtime_id = "other-runtime"

    with pytest.raises(ServiceError, match="original VMM"):
        Service(store, Upgraded()).call(token, "vm_resume", {"vm_id": vm})


def test_store_migrates_pre_image_workspaces(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    db = sqlite3.connect(directory / "state.sqlite")
    db.executescript("""
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY, max_vms INTEGER NOT NULL,
            max_memory_mib INTEGER NOT NULL, max_vcpus INTEGER NOT NULL,
            network INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO workspaces VALUES ('old', 1, 2048, 2, 0, 0);
    """)
    db.close()
    store = Store(directory)
    token = store.issue("old")
    assert store.authenticate(token)["images"] is None
    assert store.authenticate(store.issue("new", images=["b", "a", "b"]))["images"] == ["a", "b"]


def test_storage_reservation_uses_each_image_size(tmp_path):
    backend = object.__new__(Firecracker)
    backend.jails = tmp_path / "jails"
    backend.binary = tmp_path / "firecracker"
    small, large = tmp_path / "small.ext4", tmp_path / "large.ext4"
    small.write_bytes(b"\0" * 1024)
    large.write_bytes(b"\0" * 4096)
    backend.images = {
        "small": images.Image("small", small, tmp_path / "k", "", ""),
        "large": images.Image("large", large, tmp_path / "k", "", ""),
    }
    overhead = 2 * 256 * 1024**2 + 128 * 1024**2
    assert backend.storage_reservation({"image": "small", "memory_mib": 256}) == 1024 + overhead
    assert backend.storage_reservation({"image": "large", "memory_mib": 256}) == 4096 + overhead
    # Recorded at creation; independent of later configuration.
    vm = {"id": "a" * 32, "image": "gone", "memory_mib": 256, "disk_bytes": 2048}
    assert backend.storage_reservation(vm) == 2048 + overhead
    del vm["disk_bytes"]
    disk = backend.directory(vm) / "disk.ext4"
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"\0" * 512)
    assert backend.storage_reservation(vm) == 512 + overhead
