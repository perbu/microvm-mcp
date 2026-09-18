import base64
import importlib.util
import os
from pathlib import Path

import pytest


@pytest.fixture
def guest(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "guest/supervisor.py"
    spec = importlib.util.spec_from_file_location("guest_files", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "WORKSPACE", tmp_path)
    return module, tmp_path


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", "a/../../escape"])
def test_reject_traversal(guest, path):
    module, _ = guest
    with pytest.raises(ValueError):
        module.open_workspace(path, os.O_RDONLY)


def test_reject_symlinks_in_every_component(guest, tmp_path):
    module, root = guest
    (root / "file").write_text("data")
    (root / "link").symlink_to(root / "file")
    (root / "dirlink").symlink_to(root, target_is_directory=True)
    for path in ("link", "dirlink/file"):
        with pytest.raises(OSError):
            module.open_workspace(path, os.O_RDONLY)


def test_fifo_is_refused_without_blocking(guest):
    module, root = guest
    os.mkfifo(root / "fifo")
    with pytest.raises(ValueError, match="regular files"):
        module.open_workspace("fifo", os.O_RDONLY)


def test_regular_read_and_limit(guest):
    module, root = guest
    (root / "file").write_bytes(b"hello\x00")
    assert (
        base64.b64decode(module.handle("read_file", {"path": "file"})["data_base64"])
        == b"hello\x00"
    )
    (root / "large").write_bytes(b"x" * (module.FILE_LIMIT + 1))
    with pytest.raises(ValueError, match="too large"):
        module.handle("read_file", {"path": "large"})
