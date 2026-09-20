"""Local administrator installation of trusted, digest-pinned guest artifacts."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .firecracker import clone


def copy_verified(source, destination, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("SHA-256 must be 64 lowercase hexadecimal characters")
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as src, destination.open("xb") as dst:
        os.fchmod(dst.fileno(), 0o600)
        while chunk := src.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            # Preserve holes in raw ext4 images without invoking a host command.
            if not chunk.strip(b"\0"):
                dst.seek(len(chunk), os.SEEK_CUR)
            else:
                dst.write(chunk)
        dst.truncate(size)
        dst.flush()
        os.fsync(dst.fileno())
    if digest.hexdigest() != expected:
        raise ValueError(f"SHA-256 mismatch: {source}")


def install(state_dir, image_id, rootfs, kernel, rootfs_sha256, kernel_sha256):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", image_id):
        raise ValueError("image ID must be 1-80 letters, digits, dots, underscores or hyphens")
    state_dir = state_dir.resolve()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    images = state_dir / "images"
    images.mkdir(mode=0o700, exist_ok=True)
    destination = images / image_id
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"image bundle already exists: {destination}")
    with tempfile.TemporaryDirectory(prefix=".install-", dir=images) as directory:
        stage = Path(directory) / "bundle"
        stage.mkdir(mode=0o700)
        copy_verified(rootfs, stage / "rootfs.ext4", rootfs_sha256)
        copy_verified(kernel, stage / "vmlinux", kernel_sha256)
        # Probe from the staged image into the actual state directory, including
        # the case where images/ is a separate mount. No silent copy fallback.
        with tempfile.TemporaryDirectory(prefix=".reflink-", dir=state_dir) as probe:
            clone(stage / "rootfs.ext4", Path(probe) / "clone")
        manifest = {
            "image_id": image_id,
            "rootfs": str(destination / "rootfs.ext4"),
            "kernel": str(destination / "vmlinux"),
            "rootfs_sha256": rootfs_sha256,
            "kernel_sha256": kernel_sha256,
        }
        with (stage / "manifest.json").open("x") as output:
            json.dump(manifest, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        fsync_directory(stage)
        # Serialize publication by concurrent invocations of this installer.
        with (state_dir / ".image-install.lock").open("a") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"image bundle already exists: {destination}")
            stage.rename(destination)
            fsync_directory(images)
    return manifest


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/microvm-mcp"))
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--rootfs-sha256", required=True)
    parser.add_argument("--kernel-sha256", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("image installation requires root")
    os.umask(0o077)
    manifest = install(**vars(args))
    print("# Set these values in /etc/microvm-mcp.toml:")
    for key, value in manifest.items():
        print(f"{key} = {json.dumps(value)}")
