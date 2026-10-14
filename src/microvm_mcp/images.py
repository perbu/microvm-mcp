"""Operator-configured guest images. Only host configuration names a bootable image."""

import re
from dataclasses import dataclass
from pathlib import Path

IMAGE_ID = r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}"
LEGACY = ("image_id", "rootfs", "kernel", "rootfs_sha256", "kernel_sha256")
KEYS = {*LEGACY[1:], "default", "memory_mib", "min_memory_mib"}
DEFAULT_MEMORY_MIB = 1024


@dataclass(frozen=True)
class Image:
    id: str
    rootfs: Path
    kernel: Path
    rootfs_sha256: str
    kernel_sha256: str
    memory_mib: int = DEFAULT_MEMORY_MIB
    min_memory_mib: int = 256


def memory(value, name):
    if type(value) is not int or not 256 <= value <= 8192:
        raise ValueError(f"{name} must be an integer between 256 and 8192")
    return value


def load(config):
    """Return ({image_id: Image}, default_id). Never consults the filesystem."""
    tables = config.get("images")
    legacy = [key for key in LEGACY if key in config]
    if tables is not None and legacy:
        raise ValueError("configure [images.<id>] tables or top-level image_id, not both")
    if tables is None:
        # A pre-table configuration remains one implicit default image.
        missing = [key for key in LEGACY if key not in config]
        if missing:
            raise ValueError(f"missing image configuration: {', '.join(missing)}")
        tables = {config["image_id"]: {key: config[key] for key in LEGACY[1:]}}
    if not isinstance(tables, dict) or not tables:
        raise ValueError("configure at least one [images.<id>] table")
    state = Path(config["state_dir"])
    images, defaults = {}, []
    for image_id, entry in tables.items():
        if not re.fullmatch(IMAGE_ID, image_id):
            raise ValueError(f"invalid image ID: {image_id!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"[images.{image_id}] must be a table")
        unknown = set(entry) - KEYS
        if unknown:
            raise ValueError(f"[images.{image_id}] has unknown keys: {', '.join(sorted(unknown))}")
        for key in ("rootfs_sha256", "kernel_sha256"):
            if not isinstance(entry.get(key), str):
                raise ValueError(f"[images.{image_id}] requires {key}")
        minimum = memory(entry.get("min_memory_mib", 256), "min_memory_mib")
        default_memory = memory(
            entry.get("memory_mib", max(DEFAULT_MEMORY_MIB, minimum)), "memory_mib"
        )
        if default_memory < minimum:
            raise ValueError(f"[images.{image_id}] memory_mib is below min_memory_mib")
        if entry.get("default", False) not in (True, False):
            raise ValueError(f"[images.{image_id}] default must be true or false")
        if entry.get("default"):
            defaults.append(image_id)
        # Default paths are the layout written by microvm-mcp-install-image.
        bundle = state / "images" / image_id
        images[image_id] = Image(
            id=image_id,
            rootfs=Path(entry.get("rootfs", bundle / "rootfs.ext4")),
            kernel=Path(entry.get("kernel", bundle / "vmlinux")),
            rootfs_sha256=entry["rootfs_sha256"],
            kernel_sha256=entry["kernel_sha256"],
            memory_mib=default_memory,
            min_memory_mib=minimum,
        )
    if len(images) == 1 and not defaults:
        defaults = list(images)
    if len(defaults) != 1:
        raise ValueError("mark exactly one image with default = true")
    return images, defaults[0]
