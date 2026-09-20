#!/usr/bin/env python3
"""Prepare pinned vendor inputs online, then test/stage Debian packages offline."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".artifacts/debian"
INPUTS = WORK / "inputs"
PINS = ROOT / "packaging/debian/artifacts.json"
PACKAGES = ("microvm-mcp", "microvm-mcp-python", "microvm-mcp-firecracker-1.17")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_digest(path, expected):
    if digest(path) != expected:
        raise ValueError(f"SHA-256 mismatch: {path}")


def download(name, pin):
    directory = WORK / "downloads"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.tar.gz"
    if not path.exists():
        # Never leave a partial download under the reusable cache filename.
        with tempfile.NamedTemporaryFile(dir=directory) as temporary:
            with urllib.request.urlopen(pin["url"], timeout=120) as response:
                shutil.copyfileobj(response, temporary)
            temporary.flush()
            verify_digest(Path(temporary.name), pin["sha256"])
            shutil.copyfile(temporary.name, path)
    verify_digest(path, pin["sha256"])
    return path


def run(*args, **kwargs):
    subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def inventory(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        name = str(path.relative_to(directory))
        if path.is_symlink():
            result[name] = {"link": os.readlink(path)}
        elif path.is_file():
            result[name] = {"sha256": digest(path), "mode": path.stat().st_mode & 0o777}
    return result


def source_pins():
    return {
        str(p.relative_to(ROOT)): digest(p)
        for p in (PINS, ROOT / "uv.lock", ROOT / "pyproject.toml", Path(__file__).resolve())
    }


def prepare():
    pins = json.loads(PINS.read_text())
    archives = {name: download(name, pin) for name, pin in pins.items()}
    (WORK / "manifest.json").unlink(missing_ok=True)
    if INPUTS.exists():
        shutil.rmtree(INPUTS)
    INPUTS.mkdir(parents=True)
    for name, path in archives.items():
        with tarfile.open(path) as archive:
            archive.extractall(INPUTS / name, filter="data")
    python = INPUTS / "python/python/bin/python3.13"
    uv = INPUTS / "uv/uv-x86_64-unknown-linux-gnu/uv"
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UV_PYTHON_DOWNLOADS": "never"}
    for name, group in (("dependencies", "--no-dev"), ("test-dependencies", "--only-dev")):
        requirements = INPUTS / f"{name}.txt"
        run(
            str(uv),
            "export",
            "--locked",
            group,
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
            "--python",
            str(python),
            env=environment,
            stdout=subprocess.DEVNULL,
        )
        run(
            str(uv),
            "pip",
            "install",
            "--python",
            str(python),
            "--target",
            str(INPUTS / name),
            "--require-hashes",
            "--no-deps",
            "--only-binary",
            ":all:",
            "-r",
            str(requirements),
            env=environment,
        )
    # Installer tools are build-only; keep the deployed interpreter minimal.
    runtime = INPUTS / "python/python"
    for path in (runtime / "lib/python3.13/site-packages").glob("pip*"):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    for path in (runtime / "bin").glob("pip*"):
        path.unlink()
    # This is an application runtime, without GUI, development or installer tools.
    for path in (runtime / "bin").iterdir():
        if path.name not in ("python", "python3", "python3.13"):
            path.unlink()
    for pattern in (
        "include",
        "share",
        "lib/pkgconfig",
        "lib/itcl*",
        "lib/thread*",
        "lib/tcl*",
        "lib/tk*",
        "lib/libtcl*",
        "lib/python3.13/idlelib",
        "lib/python3.13/tkinter",
        "lib/python3.13/turtledemo",
        "lib/python3.13/ensurepip",
        "lib/python3.13/config-*",
        "lib/python3.13/lib-dynload/_tkinter*",
    ):
        for path in runtime.glob(pattern):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    for path in runtime.rglob("*.py"):
        path.chmod(0o644)
    for path in INPUTS.rglob("__pycache__"):
        shutil.rmtree(path)
    fc = INPUTS / "firecracker/release-v1.17.0-x86_64"
    for name in ("firecracker", "jailer"):
        verify_digest(fc / f"{name}-v1.17.0-x86_64", pins["firecracker"][f"{name}_sha256"])
    (WORK / "manifest.json").write_text(
        json.dumps({"sources": source_pins(), "files": inventory(INPUTS)}, indent=2) + "\n"
    )


def verify():
    manifest = json.loads((WORK / "manifest.json").read_text())
    if manifest["sources"] != source_pins() or manifest["files"] != inventory(INPUTS):
        raise ValueError("prepared inputs changed or are stale; rerun scripts/debian.py prepare")


def check():
    verify()
    python = str(INPUTS / "python/python/bin/python3.13")
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            str(p) for p in (ROOT / "src", INPUTS / "dependencies", INPUTS / "test-dependencies")
        ),
    }
    paths = ("src", "guest", "tests", "scripts", "examples", "packaging")
    run(python, "-m", "ruff", "check", *paths, env=environment)
    run(python, "-m", "ruff", "format", "--check", *paths, env=environment)
    run(python, "-m", "pytest", env=environment)


def copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def stage():
    verify()
    for package in PACKAGES:
        doc = ROOT / "debian" / package / "usr/share/doc" / package
        doc.mkdir(parents=True, exist_ok=True)
        copy(ROOT / "LICENSE", doc / "LICENSE")
        copy(PINS, doc / "artifacts.json")
    app = ROOT / "debian/microvm-mcp"
    private = app / "usr/lib/microvm-mcp"
    shutil.copytree(
        ROOT / "src/microvm_mcp",
        private / "app/microvm_mcp",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(INPUTS / "dependencies", private / "dependencies", symlinks=True)
    # Dependency command-line entry points have build-machine shebangs and are
    # not part of the service's supported interface.
    shutil.rmtree(private / "dependencies/bin", ignore_errors=True)
    (private / "dependencies/.lock").unlink(missing_ok=True)
    copy(ROOT / "packaging/debian/launcher.py", private / "launcher.py")
    for name, action in (("microvm-mcp", "cli"), ("microvm-mcp-install-image", "install-image")):
        launcher = app / "usr/bin" / name
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(
            "#!/bin/sh\nexec /usr/lib/microvm-mcp/python/bin/python3.13 -I -B "
            f'/usr/lib/microvm-mcp/launcher.py {action} "$@"\n'
        )
        launcher.chmod(0o755)
    config = (ROOT / "examples/service.toml").read_text()
    config = config.replace(
        "/opt/microvm-mcp/firecracker", "/usr/lib/microvm-mcp/firecracker/1.17.0/firecracker"
    )
    config = config.replace(
        "/opt/microvm-mcp/jailer", "/usr/lib/microvm-mcp/firecracker/1.17.0/jailer"
    )
    config = config.replace(
        "/opt/microvm-mcp/vmlinux", "/var/lib/microvm-mcp/images/debian-13-dev-v1/vmlinux"
    )
    config = config.replace(
        "/var/lib/microvm-images/debian-13.ext4",
        "/var/lib/microvm-mcp/images/debian-13-dev-v1/rootfs.ext4",
    )
    (app / "etc").mkdir()
    (app / "etc/microvm-mcp.toml").write_text(config)
    doc = app / "usr/share/doc/microvm-mcp"
    for source, name in (
        (ROOT / "README.md", "README.md"),
        (ROOT / "packaging/debian/README.md", "README.Debian"),
        (ROOT / "uv.lock", "uv.lock"),
        (INPUTS / "dependencies.txt", "requirements.txt"),
    ):
        copy(source, doc / name)

    runtime_package = ROOT / "debian/microvm-mcp-python"
    shutil.copytree(
        INPUTS / "python/python", runtime_package / "usr/lib/microvm-mcp/python", symlinks=True
    )
    notices = next((INPUTS / "python-notices").iterdir())
    for path in notices.glob("LICENSE*"):
        copy(path, runtime_package / "usr/share/doc/microvm-mcp-python/licenses" / path.name)

    fc_package = ROOT / "debian/microvm-mcp-firecracker-1.17"
    fc = INPUTS / "firecracker/release-v1.17.0-x86_64"
    for name in ("firecracker", "jailer"):
        copy(
            fc / f"{name}-v1.17.0-x86_64",
            fc_package / "usr/lib/microvm-mcp/firecracker/1.17.0" / name,
        )
    for name in ("LICENSE", "NOTICE", "THIRD-PARTY"):
        copy(fc / name, fc_package / "usr/share/doc/microvm-mcp-firecracker-1.17/upstream" / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify", "check", "stage", "version"))
    parser.add_argument("component", nargs="?", choices=("python", "firecracker"))
    args = parser.parse_args()
    if args.action == "version":
        if not args.component:
            parser.error("version requires a component")
        print(json.loads(PINS.read_text())[args.component]["version"])
    else:
        globals()[args.action]()


if __name__ == "__main__":
    main()
