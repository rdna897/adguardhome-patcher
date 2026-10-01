#!/usr/bin/env python3
"""Release identities: frontend artefact, frontend build inputs and host/release tooling.

- Frontend artefact revision (release "patch:", manifest patch_revision): SHA-256 of
  the exact built build/static bytes, computed by agh-patcher.py so installed hosts
  can recompute it.  Installed hosts compare only this value for frontend updates.
- Frontend input revision (release "frontend-input:"): fingerprint of the inputs
  that can change the built frontend.  CI uses it only to decide whether to rebuild.
- Tooling revision (release "tooling:"): host management and release tooling.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile


FRONTEND_INPUTS = ("patch/PATCH_BASE", "patch/dashboard-range.patch", "scripts/frontend-build.sh",
                   "scripts/build-release.sh", ".github/workflows/build.yml")
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


def frontend_input_revision(root):
    return content_revision(root, FRONTEND_INPUTS)


def tooling_revision(root):
    return content_revision(root, TOOLING_INPUTS)


def load_patcher(root):
    spec = importlib.util.spec_from_file_location("patcher", root / "scripts/agh-patcher.py")
    patcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patcher)
    return patcher


def manifest(root, build, version):
    """Write build/MANIFEST.json; it lives outside build/static, so it never affects the revision."""
    patcher = load_patcher(root)
    files = patcher.frontend_static_hashes(build / "static")
    result = {"schema": 1, "adguard_version": version,
              "revision_algorithm": patcher.FRONTEND_REVISION_ALGORITHM,
              "patch_revision": patcher.artifact_revision(files),
              # Provenance only: never compared by installed hosts.
              "frontend_input_revision": frontend_input_revision(root),
              "tooling_revision": tooling_revision(root), "files": files}
    (build / "MANIFEST.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def archive_revision(root, out, version):
    """Return the archive's frontend revision after the updater independently recomputes it."""
    patcher = load_patcher(root)
    archive = out / patcher.ASSET
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.getmember("build/MANIFEST.json")
        if not member.isfile() or member.size > 1024 * 1024:
            raise ValueError("Invalid release manifest member")
        claimed = json.load(bundle.extractfile(member)).get("patch_revision")
    with tempfile.TemporaryDirectory(prefix="agh-release-verify-") as directory:
        result = patcher.validate_archive(archive, Path(directory), version, claimed)
    return result


def verify_release(root, out, version):
    result = archive_revision(root, out, version)
    if result.get("tooling_revision") != tooling_revision(root):
        raise ValueError("Release tooling provenance differs")
    if result.get("frontend_input_revision") != frontend_input_revision(root):
        raise ValueError("Release frontend input provenance differs")
    with tarfile.open(out / "agh-dashboard-range-source.tar.gz", "r:gz") as bundle:
        for name in sorted(set((*FRONTEND_INPUTS, *TOOLING_INPUTS,
                                "scripts/tests/test-patcher.py", "docs/manual-updates.md"))):
            if bundle.extractfile("./patcher/" + name).read() != (root / name).read_bytes():
                raise ValueError("Source release tooling differs: " + name)
        if any(member.name.startswith("./patcher/install/systemd/") and
               member.name.endswith((".timer", ".service")) for member in bundle):
            raise ValueError("Source release still includes automatic updater units")
    print("Release manifest validated by the updater; frontend artefact revision "
          f"{result['patch_revision']} recomputed from build/static; source archive contains "
          "matching manual tooling and no updater units.")


USAGE = ("usage: release-manifest.py --frontend-input | --tooling | "
         "--artifact AdGuard-version release-directory | "
         "--verify AdGuard-version release-directory | AdGuard-version build-directory")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    arguments = sys.argv[1:]
    if arguments == ["--frontend-input"]:
        print(frontend_input_revision(root))
    elif arguments == ["--tooling"]:
        print(tooling_revision(root))
    elif len(arguments) == 3 and arguments[0] == "--artifact":
        print(archive_revision(root, Path(arguments[2]), arguments[1])["patch_revision"])
    elif len(arguments) == 3 and arguments[0] == "--verify":
        verify_release(root, Path(arguments[2]), arguments[1])
    elif len(arguments) == 2 and not arguments[0].startswith("-"):
        print(manifest(root, Path(arguments[1]), arguments[0])["patch_revision"])
    else:
        sys.exit(USAGE)
