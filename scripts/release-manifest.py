#!/usr/bin/env python3
"""Generate the same content-derived revision for CI release notes and UI manifests."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile


def revision(root):
    files = [root / "README.md", root / "LICENSE", root / "docs/manual-updates.md"]
    for directory in ("patch", "scripts", "install"):
        files.extend(file for file in (root / directory).rglob("*")
                     if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc")
    checksum = hashlib.sha256()
    for file in sorted((file for file in files if file.is_file()), key=lambda file: file.relative_to(root).as_posix()):
        checksum.update(file.relative_to(root).as_posix().encode() + b"\0")
        checksum.update(hashlib.sha256(file.read_bytes()).digest())
    return checksum.hexdigest()


def manifest(root, build, version):
    files = {file.relative_to(build / "static").as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
             for file in sorted((build / "static").rglob("*")) if file.is_file()}
    result = {"schema": 1, "adguard_version": version, "patch_revision": revision(root), "files": files}
    (build / "MANIFEST.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def verify_release(root, out, version):
    spec = importlib.util.spec_from_file_location("patcher", root / "scripts/agh-patcher.py")
    patcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patcher)
    with tempfile.TemporaryDirectory(prefix="agh-release-verify-") as directory:
        patcher.validate_archive(out / patcher.ASSET, Path(directory), version, revision(root))
    with tarfile.open(out / "agh-dashboard-range-source.tar.gz", "r:gz") as bundle:
        for name in ("scripts/agh-patcher.py", "scripts/agh-launch.sh", "scripts/release-manifest.py",
                     "scripts/tests/test-patcher.py", "install/native/install.sh", "install/native/uninstall.sh",
                     "install/docker/install.sh", "install/docker/uninstall.sh", "docs/manual-updates.md"):
            if bundle.extractfile("./patcher/" + name).read() != (root / name).read_bytes():
                raise ValueError("Source release tooling differs: " + name)
        if any(member.name.startswith("./patcher/install/systemd/") and
               member.name.endswith((".timer", ".service")) for member in bundle):
            raise ValueError("Source release still includes automatic updater units")
    print("Release manifest validated by the updater; source archive contains matching manual tooling and no updater units.")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    if len(sys.argv) == 1:
        print(revision(root))
    elif len(sys.argv) == 4 and sys.argv[1] == "--verify":
        verify_release(root, Path(sys.argv[3]), sys.argv[2])
    elif len(sys.argv) == 3:
        manifest(root, Path(sys.argv[2]), sys.argv[1])
    else:
        sys.exit("usage: release-manifest.py [AdGuard-version build-directory | --verify AdGuard-version release-directory]")
