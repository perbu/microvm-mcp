import hashlib
import json
from pathlib import Path

import pytest

from microvm_mcp import provision


@pytest.fixture
def artifacts(tmp_path):
    rootfs = tmp_path / "source.ext4"
    kernel = tmp_path / "source.vmlinux"
    rootfs.write_bytes(b"disk" + b"\0" * (2 * 1024 * 1024))
    kernel.write_bytes(b"kernel")
    return {
        "state_dir": tmp_path / "state",
        "image_id": "debian-13-v1",
        "rootfs": rootfs,
        "kernel": kernel,
        "rootfs_sha256": hashlib.sha256(rootfs.read_bytes()).hexdigest(),
        "kernel_sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
    }


def test_install_pins_and_preserves_bytes(artifacts, monkeypatch):
    clones = []
    monkeypatch.setattr(provision, "clone", lambda source, target: clones.append((source, target)))
    manifest = provision.install(**artifacts)
    assert Path(manifest["rootfs"]).read_bytes() == artifacts["rootfs"].read_bytes()
    assert Path(manifest["kernel"]).read_bytes() == artifacts["kernel"].read_bytes()
    directory = Path(manifest["kernel"]).parent
    assert json.loads((directory / "manifest.json").read_text()) == manifest
    assert directory.stat().st_mode & 0o777 == 0o700
    assert Path(manifest["rootfs"]).stat().st_mode & 0o777 == 0o600
    assert clones[0][1].parent.parent == artifacts["state_dir"]
    with pytest.raises(FileExistsError):
        provision.install(**artifacts)
    assert Path(manifest["rootfs"]).read_bytes() == artifacts["rootfs"].read_bytes()


@pytest.mark.parametrize("key", ["rootfs_sha256", "kernel_sha256"])
def test_bad_digest_does_not_publish_bundle(artifacts, key):
    artifacts[key] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        provision.install(**artifacts)
    assert list((artifacts["state_dir"] / "images").iterdir()) == []


def test_reflink_failure_does_not_publish_bundle(artifacts, monkeypatch):
    def fail(source, destination):
        raise OSError("reflinks unavailable")

    monkeypatch.setattr(provision, "clone", fail)
    with pytest.raises(OSError, match="reflinks unavailable"):
        provision.install(**artifacts)
    assert list((artifacts["state_dir"] / "images").iterdir()) == []
    assert not list(artifacts["state_dir"].glob(".reflink-*"))


@pytest.mark.parametrize("image_id", ["../escape", "/absolute", ".", "..", "", "a" * 81])
def test_invalid_id_is_rejected_before_writing(artifacts, image_id):
    artifacts["image_id"] = image_id
    with pytest.raises(ValueError, match="image ID"):
        provision.install(**artifacts)
    assert not artifacts["state_dir"].exists()


def test_existing_symlink_is_not_followed(artifacts):
    images = artifacts["state_dir"] / "images"
    images.mkdir(parents=True)
    (images / artifacts["image_id"]).symlink_to("/does-not-exist")
    with pytest.raises(FileExistsError):
        provision.install(**artifacts)
