#!/usr/bin/env python3
"""Keep frontend build inputs and host/release tooling identities independent."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile


FRONTEND_INPUTS = ("patch/PATCH_BASE", "patch/dashboard-range.patch", "scripts/frontend-build.sh")
TOOLING_INPUTS = ("scripts/agh-patcher.py", "scripts/agh-launch.sh", "scripts/agh-ui-sync.sh",
                  "scripts/release-manifest.py", "scripts/build-release.sh", ".github/workflows/build.yml",
                  "install/native/install.sh", "install/native/uninstall.sh", "install/docker/install.sh",
                  "install/docker/uninstall.sh", "install/docker/compose.override.yaml")


def content_revision(root, names):
    checksum = hashlib.sha256()
    for name in sorted(names):
        file = root / name
        checksum.update(name.encode() + b"\0")
        checksum.update(hashlib.sha256(file.read_bytes()).digest())
    return checksum.hexdigest()


def frontend_revision(root):
    return content_revision(root, FRONTEND_INPUTS)


def tooling_revision(root):
    return content_revision(root, TOOLING_INPUTS)


def manifest(root, build, version):
    files = {file.relative_to(build / "static").as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
             for file in sorted((build / "static").rglob("*")) if file.is_file()}
    # patch_revision is the frontend identity; tooling_revision is build provenance only.
    result = {"schema": 1, "adguard_version": version, "patch_revision": frontend_revision(root),
              "tooling_revision": tooling_revision(root), "files": files}
    (build / "MANIFEST.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def verify_release(root, out, version):
    spec = importlib.util.spec_from_file_location("patcher", root / "scripts/agh-patcher.py")
    patcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patcher)
    with tempfile.TemporaryDirectory(prefix="agh-release-verify-") as directory:
        result = patcher.validate_archive(out / patcher.ASSET, Path(directory), version, frontend_revision(root))
        if result.get("tooling_revision") != tooling_revision(root):
            raise ValueError("Release tooling provenance differs")
    with tarfile.open(out / "agh-dashboard-range-source.tar.gz", "r:gz") as bundle:
        for name in sorted(set((*FRONTEND_INPUTS, *TOOLING_INPUTS,
                                "scripts/tests/test-patcher.py", "docs/manual-updates.md"))):
            if bundle.extractfile("./patcher/" + name).read() != (root / name).read_bytes():
                raise ValueError("Source release tooling differs: " + name)
        if any(member.name.startswith("./patcher/install/systemd/") and
               member.name.endswith((".timer", ".service")) for member in bundle):
            raise ValueError("Source release still includes automatic updater units")
    print("Release manifest validated by the updater; source archive contains matching manual tooling and no updater units.")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    if len(sys.argv) == 1:
        print(frontend_revision(root))
    elif sys.argv[1:] == ["--tooling"]:
        print(tooling_revision(root))
    elif len(sys.argv) == 4 and sys.argv[1] == "--verify":
        verify_release(root, Path(sys.argv[3]), sys.argv[2])
    elif len(sys.argv) == 3:
        manifest(root, Path(sys.argv[2]), sys.argv[1])
    else:
        sys.exit("usage: release-manifest.py [--tooling | AdGuard-version build-directory | --verify AdGuard-version release-directory]")
