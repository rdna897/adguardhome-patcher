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
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


FRONTEND_INPUTS = ("patch/PATCH_BASE", "patch/dashboard-range.patch", "scripts/frontend-build.sh",
                   "scripts/build-release.sh", ".github/workflows/build.yml")
TOOLS_ASSET = "agh-patcher-tools.tar.gz"
TOOLS_MANIFEST = "TOOLS.json"
TOOLS_FILES = ("LICENSE", "install/native/install.sh", "install/docker/install.sh",
               "install/docker/compose.override.yaml", "scripts/agh-patcher.py",
               "scripts/agh-launch.sh", "scripts/release-manifest.py")
TOOLING_INPUTS = (*TOOLS_FILES, "scripts/build-release.sh", ".github/workflows/build.yml")


def verify_tools(root, version=None):
    """Validate an extracted release bundle without requiring development files."""
    for name in (*TOOLS_FILES, TOOLS_MANIFEST):
        path = root / name
        if any(parent.is_symlink() for parent in (path, *path.parents)) or not path.is_file():
            raise ValueError("Missing or unsafe tools file: " + name)
    if (root / TOOLS_MANIFEST).stat().st_size > 65536:
        raise ValueError("Oversized tools manifest")
    result = json.loads((root / TOOLS_MANIFEST).read_text())
    if (not isinstance(result, dict) or result.get("schema") != 1
            or not isinstance(result.get("tooling_revision"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", result["tooling_revision"])
            or not isinstance(result.get("source_commit"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", result["source_commit"])
            or not isinstance(result.get("adguard_version"), str)
            or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?", result["adguard_version"])):
        raise ValueError("Invalid tools manifest")
    if version is not None and result["adguard_version"] != version:
        raise ValueError("Tools bundle supports a different AdGuard Home version")
    files = result.get("files")
    if not isinstance(files, dict) or set(files) != set(TOOLS_FILES):
        raise ValueError("Tools manifest does not describe the required installation files")
    for name, expected in files.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Tools checksum mismatch: " + name)
    return result


def package_tools(root, out, version, source_commit):
    """Package only installation files and their release identity/integrity metadata."""
    out.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        raise ValueError("Invalid tools source commit")
    result = {"schema": 1, "adguard_version": version, "tooling_revision": tooling_revision(root),
              "source_commit": source_commit,
              "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in TOOLS_FILES}}
    data = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    asset = out / TOOLS_ASSET
    with tarfile.open(asset, "w:gz") as bundle:
        for name in TOOLS_FILES:
            path = root / name
            if any(parent.is_symlink() for parent in (path, *path.parents)) or not path.is_file():
                raise ValueError("Unsafe tools input: " + name)
            bundle.add(path, name, recursive=False)
        member = tarfile.TarInfo(TOOLS_MANIFEST)
        member.size, member.mode = len(data), 0o644
        bundle.addfile(member, io.BytesIO(data))
    (out / (TOOLS_ASSET + ".sha256")).write_text(hashlib.sha256(asset.read_bytes()).hexdigest() + "  " + TOOLS_ASSET + "\n")
    return result


def verify_tools_archive(root, out, version):
    """Check the full archive inventory and compare released tooling to the build source."""
    with tempfile.TemporaryDirectory(prefix="agh-tools-verify-") as directory:
        extracted = Path(directory)
        with tarfile.open(out / TOOLS_ASSET, "r:gz") as bundle:
            members = bundle.getmembers()
            expected = set((*TOOLS_FILES, TOOLS_MANIFEST))
            if (len(members) != len(expected) or {member.name for member in members} != expected
                    or any(not member.isfile() or member.mode & 0o7000 or member.size > 1024 * 1024 for member in members)):
                raise ValueError("Unsafe or unexpected tools archive inventory")
            for member in members:
                target = extracted / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.extractfile(member).read())
        result = verify_tools(extracted, version)
        if result["tooling_revision"] != tooling_revision(root):
            raise ValueError("Tools release revision differs from build source")
        for name in TOOLS_FILES:
            if (extracted / name).read_bytes() != (root / name).read_bytes():
                raise ValueError("Released tools differ from build source: " + name)
    return result


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
    for name in ("agh-dashboard-range.tar.gz", "agh-dashboard-range-source.tar.gz", TOOLS_ASSET):
        expected = hashlib.sha256((out / name).read_bytes()).hexdigest() + "  " + name + "\n"
        if (out / (name + ".sha256")).read_text() != expected:
            raise ValueError("Release checksum mismatch: " + name)
    result = archive_revision(root, out, version)
    if result.get("tooling_revision") != tooling_revision(root):
        raise ValueError("Release tooling provenance differs")
    if result.get("frontend_input_revision") != frontend_input_revision(root):
        raise ValueError("Release frontend input provenance differs")
    tools = verify_tools_archive(root, out, version)
    if tools["tooling_revision"] != result["tooling_revision"]:
        raise ValueError("Frontend and tools release identities differ")
    with tarfile.open(out / "agh-dashboard-range-source.tar.gz", "r:gz") as bundle:
        for name in sorted(set((*FRONTEND_INPUTS, *TOOLING_INPUTS,
                                "scripts/tests/test-patcher.py", "docs/safety-and-recovery.md"))):
            if bundle.extractfile("./patcher/" + name).read() != (root / name).read_bytes():
                raise ValueError("Source release tooling differs: " + name)
        if any(member.name.startswith("./patcher/") and
               member.name.endswith((".timer", ".service")) for member in bundle):
            raise ValueError("Source release includes host scheduler units")
    print("Release manifest validated by the updater; frontend artefact revision "
          f"{result['patch_revision']} recomputed from build/static; source archive contains "
          "matching manual tooling and no scheduler units; tools bundle and all three checksums verified.")


USAGE = ("usage: release-manifest.py --frontend-input | --tooling | "
         "--tools AdGuard-version release-directory | "
         "--artifact AdGuard-version release-directory | "
         "--verify AdGuard-version release-directory | AdGuard-version build-directory")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    arguments = sys.argv[1:]
    if arguments == ["--frontend-input"]:
        print(frontend_input_revision(root))
    elif arguments == ["--tooling"]:
        print(tooling_revision(root))
    elif len(arguments) == 3 and arguments[0] == "--tools":
        source_commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        print(package_tools(root, Path(arguments[2]), arguments[1], source_commit)["tooling_revision"])
    elif len(arguments) == 3 and arguments[0] == "--artifact":
        print(archive_revision(root, Path(arguments[2]), arguments[1])["patch_revision"])
    elif len(arguments) == 3 and arguments[0] == "--verify":
        verify_release(root, Path(arguments[2]), arguments[1])
    elif len(arguments) == 2 and not arguments[0].startswith("-"):
        print(manifest(root, Path(arguments[1]), arguments[0])["patch_revision"])
    else:
        sys.exit(USAGE)
