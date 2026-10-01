#!/usr/bin/env python3
"""Explicit, confirmed frontend management; no scheduler or downloaded code execution."""

import argparse
import collections
import contextlib
import fcntl
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid


ASSET = "agh-dashboard-range.tar.gz"
UNIT = "AdGuardHome.service"
VERSION = r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?"
REVISION = r"[a-f0-9]{64}"
# patch_revision/"patch:" identify the built frontend bytes under build/static.
FRONTEND_REVISION_ALGORITHM = "agh-frontend-static-sha256-v1"
NOT_CHECKED = "Unknown (run agh-patcher check)"
TOOLING_UPDATE = "Download and verify the tools bundle from the compatible GitHub Release, then rerun its installer."


class Error(Exception):
    pass


Release = collections.namedtuple("Release", ("frontend", "tooling", "urls"))


class Paths:
    def __init__(self, root=Path("/")):
        self.root = root
        self.config = root / "etc/agh-patcher/config.json"
        self.state = root / "var/lib/agh-patcher"
        self.systemd = root / "etc/systemd/system"
        self.command = root / "usr/local/bin/agh-patcher"
        self.docker_launcher = root / "usr/local/lib/adguardhome-patcher/agh-launch.sh"


class Runner:
    def run(self, *args, check=True):
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=60)
        if check and result.returncode:
            raise Error(f"{' '.join(args)} failed: {result.stderr.strip()}")
        return result


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise Error("Refusing a non-HTTPS download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Network:
    def download(self, url, destination, limit):
        if not url.startswith("https://"):
            raise Error("Release downloads must use HTTPS")
        opener = urllib.request.build_opener(HTTPSRedirect())
        request = urllib.request.Request(url, headers={"User-Agent": "adguardhome-patcher"})
        total = 0
        deadline = time.monotonic() + 120
        try:
            with opener.open(request, timeout=30) as response, destination.open("wb") as out:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if time.monotonic() > deadline:
                        raise Error("Release download exceeded its total time limit")
                    if total > limit:
                        raise Error("Release download exceeds its size limit")
                    out.write(chunk)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise Error("No compatible release is available for this AdGuard Home version") from error
            raise Error(f"Release download failed: HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise Error(f"Release download failed: {error}") from error

    def read(self, url, limit):
        with tempfile.TemporaryDirectory(prefix="agh-patcher-check-") as directory:
            target = Path(directory) / "response"
            self.download(url, target, limit)
            return target.read_bytes()


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_mkdir(path, mode=0o755):
    if not path.exists():
        durable_mkdir(path.parent)
        path.mkdir(mode=mode)
        fsync_directory(path)
        fsync_directory(path.parent)


def fsync_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Error(f"Cannot durably stage a non-regular file: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_tree(path):
    """Flush file data, child directories, then the root's parent entry."""
    safe_directory(path)
    if not path.is_dir():
        raise Error(f"Expected a frontend directory to flush: {path}")
    def walk_error(error):
        raise error
    for directory, children, files in os.walk(path, topdown=False, followlinks=False, onerror=walk_error):
        parent = Path(directory)
        if any((parent / name).is_symlink() for name in (*children, *files)):
            raise Error("Refusing a symlink in a durably staged frontend")
        for name in files:
            fsync_file(parent / name)
        fsync_directory(parent)
    fsync_directory(path.parent)


def durable_replace(source, target):
    os.replace(source, target)
    fsync_directory(target.parent)
    if source.parent != target.parent:
        fsync_directory(source.parent)


def durable_remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    fsync_directory(path.parent)


def atomic_json(path, data):
    durable_mkdir(path.parent)
    temp = path.with_name(path.name + ".new")
    with temp.open("w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    durable_replace(temp, path)


def secure_path(path, owner=0):
    """Protect privileged writes from symlink/ancestor replacement by other users."""
    for entry in (path, *path.parents):
        if entry.is_symlink():
            raise Error(f"Refusing a symlink in a management path: {entry}")
        if entry.exists() and (entry.stat().st_uid != owner or entry.stat().st_mode & 0o022):
            raise Error(f"Management path must be root-owned and not group/other writable: {entry}")


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def frontend_static_hashes(static):
    """Return {relative path: SHA-256} for every regular file under build/static."""
    if static.is_symlink() or not static.is_dir():
        raise Error(f"Expected a frontend static directory: {static}")
    def walk_error(error):
        raise error
    hashes = {}
    for directory, children, files in os.walk(static, followlinks=False, onerror=walk_error):
        for name in (*children, *files):
            path = Path(directory) / name
            relative = path.relative_to(static).as_posix()
            try:
                relative.encode("utf-8")
            except UnicodeEncodeError as error:
                raise Error(f"Unexpected frontend path encoding: {relative!r}") from error
            if "\\" in relative or any(ord(character) < 32 or ord(character) == 127 for character in relative):
                raise Error(f"Unexpected frontend path: {relative!r}")
            mode = os.lstat(path).st_mode
            if stat.S_ISREG(mode):
                hashes[relative] = digest(path)
            elif not stat.S_ISDIR(mode):
                raise Error(f"Frontend contains a link or special file: {relative}")
    if not hashes:
        raise Error("Frontend static directory contains no files")
    return hashes


def artifact_revision(hashes):
    """One deterministic SHA-256 over sorted relative paths and their file SHA-256 values."""
    checksum = hashlib.sha256(FRONTEND_REVISION_ALGORITHM.encode() + b"\n")
    for name in sorted(hashes, key=lambda value: value.encode("utf-8")):
        if not isinstance(hashes[name], str) or not re.fullmatch(REVISION, hashes[name]):
            raise Error(f"Invalid frontend file checksum: {name}")
        checksum.update(name.encode("utf-8") + b"\0" + bytes.fromhex(hashes[name]))
    return checksum.hexdigest()


def frontend_artifact_revision(static):
    return artifact_revision(frontend_static_hashes(static))


def install_file(source, target):
    if target.is_symlink():
        raise Error(f"Refusing a symlinked tooling target: {target}")
    durable_mkdir(target.parent)
    descriptor, name = tempfile.mkstemp(prefix=".agh-patcher-tool-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as out, source.open("rb") as original:
            os.fchmod(out.fileno(), 0o755)
            shutil.copyfileobj(original, out)
            out.flush()
            os.fsync(out.fileno())
        durable_replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def safe_directory(path):
    if not path.is_absolute() or path == Path("/"):
        raise Error(f"Expected an absolute, non-root directory: {path}")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise Error(f"Refusing a directory through a symbolic link: {path}")


def read_config(paths):
    if not paths.config.is_file():
        raise Error("Patcher is not configured. Run the native or Docker installer first.")
    owner = 0 if paths.root == Path("/") else os.geteuid()
    if paths.config.is_symlink() or paths.config.stat().st_uid != owner or paths.config.stat().st_mode & 0o022:
        raise Error("Patcher configuration must be root-owned and not writable by group/others")
    config = json.loads(paths.config.read_text())
    validate_config(config)
    return config


def validate_config(config):
    if not isinstance(config, dict):
        raise Error("Invalid patcher configuration")
    if config.get("schema") != 1 or config.get("mode") not in ("native", "docker"):
        raise Error("Unsupported patcher configuration")
    if not isinstance(config.get("tooling_revision"), str) or not re.fullmatch(REVISION, config["tooling_revision"]):
        raise Error("Invalid installed tooling revision; rerun the released installer")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config.get("repository", "")):
        raise Error("Expected a public GitHub owner/repository")
    safe_directory(Path(config["ui_root"]))
    if config["mode"] == "native":
        safe_directory(Path(config["agh_dir"]))
    elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", config.get("container", "")):
        raise Error("Invalid Docker container name")


class IndexAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            source = dict(attrs).get("src")
            if source:
                self.scripts.append(source)


def validate_archive(archive, destination, version, revision):
    """Validate the entire tar inventory before manually writing regular files."""
    members = {}
    total = 0
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            name = member.name
            parts = PurePosixPath(name).parts
            if (name.startswith("/") or ".." in parts or "\\" in name
                    or str(PurePosixPath(name)) != name.rstrip("/") or name in members):
                raise Error(f"Unsafe or duplicate archive path: {name}")
            allowed = name in ("build", "build/static", "build/VERSION", "build/MANIFEST.json",
                               "build/LICENSE.txt", "build/NOTICE") or name.startswith("build/static/")
            if not allowed or not (member.isdir() or member.isfile()):
                raise Error(f"Unexpected archive member: {name}")
            if name in ("build", "build/static") and not member.isdir():
                raise Error("Archive build/static roots must be directories")
            if member.isdir() and name not in ("build", "build/static") and not name.startswith("build/static/"):
                raise Error(f"Unexpected archive directory: {name}")
            if member.mode & 0o7000:
                raise Error("Archive contains special permission bits")
            total += member.size
            members[name] = member
            if len(members) > 10000 or total > 256 * 1024 * 1024 or member.size > 64 * 1024 * 1024:
                raise Error("Archive exceeds its size/member limits")
        for name in ("build/VERSION", "build/MANIFEST.json", "build/static/index.html"):
            if name not in members or not members[name].isfile():
                raise Error(f"Archive is missing {name}")
        if members["build/MANIFEST.json"].size > 1024 * 1024 or members["build/VERSION"].size > 128:
            raise Error("Oversized archive metadata")
        stamp = bundle.extractfile(members["build/VERSION"]).read().decode().strip()
        manifest = json.load(bundle.extractfile(members["build/MANIFEST.json"]))
        if not isinstance(manifest, dict):
            raise Error("Invalid archive manifest")
        if stamp != version or manifest.get("adguard_version") != version:
            raise Error("Archive explicitly supports a different AdGuard Home version")
        if manifest.get("schema") != 1 or manifest.get("revision_algorithm") != FRONTEND_REVISION_ALGORITHM:
            raise Error("Archive manifest has no frontend artefact revision; rebuild it with current release tooling")
        if manifest.get("patch_revision") != revision:
            raise Error("Archive revision does not match release metadata")
        files = manifest.get("files")
        actual = {name.removeprefix("build/static/") for name, member in members.items()
                  if name.startswith("build/static/") and member.isfile()}
        if not isinstance(files, dict) or set(files) != actual or "index.html" not in files:
            raise Error("Manifest does not describe the complete frontend")
        if any(not isinstance(value, str) or not re.fullmatch(REVISION, value) for value in files.values()):
            raise Error("Invalid frontend file checksum")
        for name, member in members.items():
            target = destination / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, target.open("xb") as out:
                    shutil.copyfileobj(source, out)
                target.chmod(0o644)
        for name, expected in files.items():
            if digest(destination / "build/static" / name) != expected:
                raise Error(f"Frontend checksum mismatch: {name}")
        # Recompute the identity from the extracted bytes instead of trusting the manifest.
        computed = frontend_artifact_revision(destination / "build/static")
        if computed != manifest["patch_revision"]:
            raise Error("Archive manifest revision does not describe its frontend files")
        if computed != revision:
            raise Error("Release metadata revision does not describe the archive's frontend files")
        index = IndexAssets()
        index.feed((destination / "build/static/index.html").read_text())
        if not index.scripts or any(source.removeprefix("./") not in files for source in index.scripts):
            raise Error("Frontend index does not reference its packaged scripts")
    return manifest


class Patcher:
    def __init__(self, config, paths=None, runner=None, network=None, confirm=None):
        validate_config(config)
        self.config = config
        self.paths = paths or Paths()
        self.runner = runner or Runner()
        self.network = network or Network()
        self.confirm = confirm or (lambda message: input(message).strip().lower() == "yes")
        self.ui = Path(config["ui_root"])
        self.build = self.ui / "build"

    def version(self):
        if self.config["mode"] == "native":
            if self.paths.root == Path("/"):
                secure_path(Path(self.config["agh_dir"]) / "AdGuardHome")
            result = self.runner.run(str(Path(self.config["agh_dir"]) / "AdGuardHome"), "--version")
        else:
            result = self.runner.run("docker", "exec", self.config["container"],
                                     "/opt/adguardhome/AdGuardHome", "--version")
        match = re.search(r"\bversion (" + VERSION + r")(?:\s|$)", result.stdout)
        if not match:
            raise Error("Could not determine the installed AdGuard Home version")
        return match.group(1)

    def local(self, version):
        safe_directory(self.build)
        if self.build.is_symlink():
            raise Error("Refusing a symlinked frontend build")
        if not self.build.exists():
            return "none", "No patched frontend installed"
        manifest_path = self.build / "MANIFEST.json"
        if not manifest_path.is_file():
            return "unknown", "Missing frontend manifest"
        try:
            manifest = json.loads(manifest_path.read_text())
            recorded = manifest["patch_revision"]
            if manifest.get("schema") != 1 or not re.fullmatch(REVISION, recorded):
                raise ValueError("Invalid manifest revision")
            if manifest.get("revision_algorithm") != FRONTEND_REVISION_ALGORITHM:
                raise ValueError("Invalid frontend revision algorithm")
            if (manifest["adguard_version"] != version or (self.build / "VERSION").read_text().strip() != version):
                return recorded, "Installed patch is incompatible; launcher uses stock UI"
            if (self.build / "DISABLED").exists():
                return recorded, "Patched frontend disabled; launcher uses stock UI"
            files = manifest["files"]
            if not isinstance(files, dict) or not files or "index.html" not in files:
                raise ValueError("Incomplete frontend manifest")
            for name in files:
                relative = PurePosixPath(name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Unsafe manifest path")
            safe_directory(self.build / "static")
            try:
                actual = frontend_static_hashes(self.build / "static")
            except Error as error:
                return recorded, f"Missing or damaged frontend: {error}"
            for name, expected in sorted(files.items()):
                if actual.get(name) != expected:
                    return recorded, f"Missing or damaged frontend file: {name}"
            extra = sorted(set(actual) - set(files))
            if extra:
                return recorded, f"Unexpected frontend file: {extra[0]}"
            # The installed identity is always derived from the installed bytes.
            revision = artifact_revision(actual)
            if revision != recorded:
                return recorded, "Frontend manifest revision does not match its installed files"
            return revision, "Healthy"
        except (OSError, KeyError, ValueError, TypeError, Error) as error:
            return "unknown", f"Invalid or incomplete frontend: {error}"

    def available(self, version):
        repository = self.config["repository"]
        tag = "ui-" + version
        metadata = json.loads(self.network.read(
            f"https://api.github.com/repos/{repository}/releases/tags/{tag}", 2 * 1024 * 1024))
        if not isinstance(metadata, dict) or not isinstance(metadata.get("body"), str) or not isinstance(metadata.get("assets"), list):
            raise Error("Malformed release metadata")
        if metadata.get("tag_name") != tag or metadata.get("draft"):
            raise Error("Release metadata does not explicitly support the installed AdGuard Home version")
        match = re.search(r"^patch: ([a-f0-9]{64})$", metadata.get("body", ""), re.M)
        if not match:
            raise Error("Release has no explicit patch revision; rebuild it with current tooling")
        tooling = re.search(r"^tooling: ([a-f0-9]{64})$", metadata["body"], re.M)
        if not tooling:
            raise Error("Release has no tooling revision")
        urls = {}
        for name in (ASSET, ASSET + ".sha256"):
            assets = [asset for asset in metadata["assets"] if isinstance(asset, dict) and asset.get("name") == name]
            expected = f"https://github.com/{repository}/releases/download/{tag}/{name}"
            if len(assets) != 1 or assets[0].get("browser_download_url") != expected:
                raise Error(f"Missing or unexpected release asset: {name}")
            urls[name] = expected
        return Release(match.group(1), tooling.group(1), urls)

    def tooling_comparison(self, available):
        """Describe management tooling separately; it never selects a frontend update."""
        installed = self.config["tooling_revision"]
        if installed == available:
            return "tooling up to date", None
        return "tooling update available", TOOLING_UPDATE

    def status(self, remote=False):
        version = self.version()
        revision, health = self.local(version)
        available = available_tooling = NOT_CHECKED
        status = "Installed; availability not checked" if health == "Healthy" else health
        actions = []
        if remote:
            try:
                release = self.available(version)
            except Error as error:
                self.print_status(version, revision, "Unavailable", "Unavailable", str(error))
                raise
            available = release.frontend
            available_tooling = release.tooling
            if revision == available and health == "Healthy":
                status = "Frontend up to date"
            else:
                status = "Frontend update available"
                actions.append("Run sudo agh-patcher update to review and install the frontend.")
                if health not in ("Healthy", "No patched frontend installed"):
                    status += "; " + health
            tooling_status, tooling_action = self.tooling_comparison(release.tooling)
            status += "; " + tooling_status
            if tooling_action:
                actions.append(tooling_action)
        if (self.paths.state / "transaction.json").exists():
            status += "; interrupted update (retained backup requires recovery)"
        self.print_status(version, revision, available, available_tooling, status, actions)

    def print_status(self, version, revision, available, available_tooling, status, actions=()):
        tooling = self.config["tooling_revision"]
        rows = [("AdGuard Home", version), ("Frontend revision", revision), ("Available frontend", available),
                ("Tooling revision", tooling), ("Available tooling", available_tooling), ("Status", status),
                *(("Next action", action) for action in actions)]
        print("\n".join(f"{label + ':':<21}{value}" for label, value in rows))

    @contextlib.contextmanager
    def lock(self):
        if self.paths.root == Path("/"):
            secure_path(self.paths.state)
            secure_path(self.paths.config)
            secure_path(self.ui)
            secure_path(self.paths.systemd)
            secure_path(self.paths.command)
            secure_path(self.paths.docker_launcher)
        durable_mkdir(self.paths.state)
        if (self.paths.state / "lock").is_symlink():
            raise Error("Refusing a symlinked operation lock")
        with (self.paths.state / "lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Error("Another patcher operation is running") from error
            yield

    def restart_and_verify(self):
        if self.config["mode"] == "native":
            self.runner.run("systemctl", "restart", UNIT)
            command = ("systemctl", "is-active", "--quiet", UNIT)
        else:
            self.runner.run("docker", "restart", self.config["container"])
            command = ("docker", "inspect", "--format", "{{json .State}}", self.config["container"])
        consecutive = 0
        for _ in range(15):
            result = self.runner.run(*command, check=False)
            healthy = result.returncode == 0
            if healthy and self.config["mode"] == "docker":
                state = json.loads(result.stdout)
                healthy = state.get("Running") is True and state.get("Health", {}).get("Status", "healthy") == "healthy"
            consecutive = consecutive + 1 if healthy else 0
            if consecutive == 3:
                return
            time.sleep(1)
        raise Error("AdGuard Home did not remain running after restart")

    def update(self, yes=False):
        version = self.version()
        previous, health = self.local(version)
        release = self.available(version)
        revision, urls = release.frontend, release.urls
        tooling_status, tooling_action = self.tooling_comparison(release.tooling)
        if tooling_action:
            # Reported only: update installs frontend files, never host management tooling.
            print(f"Note: {tooling_status}. {tooling_action}")
        if (self.paths.state / "transaction.json").exists():
            raise Error("An interrupted update has a retained backup; recover it before another update (see docs/manual-updates.md)")
        if previous == revision and health == "Healthy":
            print(f"Up to date: {revision}. No files/services changed.")
            return
        if self.config["mode"] == "docker":
            mounts = json.loads(self.runner.run("docker", "inspect", "--format", "{{json .Mounts}}", self.config["container"]).stdout)
            if not any(mount.get("Destination") == "/opt/adguardhome/ui" and Path(mount.get("Source", "")) == self.ui for mount in mounts):
                raise Error("Apply the Docker dashboard override with the configured parent UI mount before updating")
        with tempfile.TemporaryDirectory(prefix="agh-patcher-preflight-") as directory:
            temp = Path(directory)
            checksum = self.network.read(urls[ASSET + ".sha256"], 2048).decode()
            match = re.fullmatch(r"([a-f0-9]{64})  " + re.escape(ASSET) + r"\s*", checksum)
            if not match:
                raise Error("Malformed published checksum")
            archive = temp / ASSET
            self.network.download(urls[ASSET], archive, 64 * 1024 * 1024)
            archive_sha = digest(archive)
            if archive_sha != match.group(1):
                raise Error("Archive checksum mismatch; installation unchanged")
            extracted = temp / "verified"
            manifest = validate_archive(archive, extracted, version, revision)
            print(f"AdGuard Home {version}: replace frontend {previous} with {revision}.\n"
                  f"Validated compatibility, archive checksum and all frontend files.\n"
                  f"Destination: {self.build}\nAdGuard Home will restart; configuration/data are kept.\n"
                  "Previous files will be retained for rollback.")
            if not yes and not self.confirm("Type yes to apply this update: "):
                print("Update declined. No installation or services changed.")
                return
            with self.lock():
                if self.paths.root == Path("/") and read_config(self.paths) != self.config:
                    raise Error("Patcher configuration changed during preflight; run update again")
                # Recheck after user think-time/locking; do not race a binary or patch change.
                if self.version() != version or self.local(version) != (previous, health):
                    raise Error("Installation changed during preflight; run update again")
                self.install_verified(extracted / "build", manifest, archive_sha)

    def install_verified(self, verified, manifest, archive_sha):
        safe_directory(self.ui)
        durable_mkdir(self.ui)
        stage = self.ui / (".agh-patcher-stage-" + uuid.uuid4().hex)
        backup = self.ui / (".agh-patcher-backup-" + uuid.uuid4().hex)
        journal = self.paths.state / "transaction.json"
        last_update = self.paths.state / "last-update.json"
        previous_record = json.loads(last_update.read_text()) if last_update.exists() else {}
        previous_record_exists = last_update.exists()
        had_previous = self.build.exists()
        installed = False
        commit_attempted = False
        transaction = {"schema": 1, "phase": "prepared", "backup": str(backup), "stage": str(stage),
                       "build": str(self.build), "had_previous": had_previous,
                       "frontend_revision": manifest["patch_revision"]}
        try:
            shutil.copytree(verified, stage)
            (stage / "SHA256").write_text(archive_sha + "\n")
            atomic_json(stage / "INSTALL.json", {"schema": 1, "patch_revision": manifest["patch_revision"],
                                                  "archive_sha256": archive_sha})
            sync_tree(stage)
            if had_previous:
                sync_tree(self.build)
            # The staged data and recovery record must be durable before any live rename.
            atomic_json(journal, transaction)
            if had_previous:
                durable_replace(self.build, backup)
                transaction["phase"] = "backup_saved"
                atomic_json(journal, transaction)
            durable_replace(stage, self.build)
            installed = True
            transaction["phase"] = "installed"
            atomic_json(journal, transaction)
            self.restart_and_verify()
            if self.version() != manifest["adguard_version"]:
                raise Error("AdGuard Home version changed during the update")
            if self.local(manifest["adguard_version"]) != (manifest["patch_revision"], "Healthy"):
                raise Error("Installed frontend failed its file/compatibility check")
            commit_attempted = True
            atomic_json(last_update, {"backup": str(backup) if had_previous else None,
                                     "revision": manifest["patch_revision"], "archive_sha256": archive_sha})
            transaction["phase"] = "committed"
            atomic_json(journal, transaction)
        except BaseException as error:
            try:
                # A rename may have succeeded before its directory fsync failed.
                installed = installed or (not stage.exists() and self.build.exists() and journal.exists())
                if journal.exists():
                    transaction["phase"] = "rolling_back"
                    atomic_json(journal, transaction)
                if installed and self.build.exists():
                    durable_remove(self.build)
                if backup.exists():
                    durable_replace(backup, self.build)
                if commit_attempted:
                    if previous_record_exists:
                        atomic_json(last_update, previous_record)
                    else:
                        durable_remove(last_update)
                if installed:
                    self.restart_and_verify()
                if stage.exists():
                    durable_remove(stage)
                if journal.exists():
                    transaction["phase"] = "rolled_back"
                    atomic_json(journal, transaction)
                    durable_remove(journal)
            except BaseException as recovery:
                raise Error(f"Update failed ({error}); recovery needs administrator attention ({recovery}). "
                            f"Inspect transaction/recovery paths: {journal}") from error
            raise Error(f"Update failed; previous frontend restored (or stock UI retained): {error}") from error
        old_backup = previous_record.get("backup")
        if old_backup:
            remove_recorded_backup(Path(old_backup), self.ui)
        # Commit/obsolete-backup cleanup must reach stable storage before recovery metadata is removed.
        durable_remove(journal)
        print(f"Installed patch {manifest['patch_revision']}; AdGuard Home is running. Automatic updates are disabled.")

    def uninstall(self, yes=False):
        print("Remove patcher tooling/frontend and return AdGuard Home to the stock UI. Configuration/data are kept.")
        if self.config["mode"] == "docker":
            mounts = json.loads(self.runner.run("docker", "inspect", "--format", "{{json .Mounts}}", self.config["container"]).stdout)
            if any(mount.get("Destination") in ("/opt/adguardhome/ui", "/opt/adguardhome-patcher/agh-launch.sh") for mount in mounts):
                raise Error("Recreate the Docker container with its base Compose file only before uninstalling")
        if not yes and not self.confirm("Type yes to uninstall: "):
            print("Uninstall declined. No installation or services changed.")
            return
        with self.lock():
            if self.config["mode"] == "native":
                override = self.paths.systemd / (UNIT + ".d/dashboard-range.conf")
                override.unlink(missing_ok=True)
                try:
                    override.parent.rmdir()
                except OSError:
                    pass
                (Path(self.config["agh_dir"]) / "agh-launch.sh").unlink(missing_ok=True)
                self.runner.run("systemctl", "daemon-reload")
                self.restart_and_verify()
            if self.build.is_symlink():
                raise Error("Refusing removal of a symlinked frontend")
            owned = (self.build / "INSTALL.json").is_file()
            if owned:
                shutil.rmtree(self.build)
            elif self.build.exists():
                print(f"Unmanaged frontend directory retained: {self.build}")
            for record in ("last-update.json", "transaction.json"):
                file = self.paths.state / record
                if file.is_file():
                    data = json.loads(file.read_text())
                    if data.get("backup"):
                        remove_recorded_backup(Path(data["backup"]), self.ui)
                    if data.get("stage"):
                        remove_recorded_backup(Path(data["stage"]), self.ui, ".agh-patcher-stage-")
            self.paths.command.unlink(missing_ok=True)
            self.paths.docker_launcher.unlink(missing_ok=True)
            shutil.rmtree(self.paths.state)
            if self.paths.config.parent.exists():
                shutil.rmtree(self.paths.config.parent)
            try:
                self.paths.docker_launcher.parent.rmdir()
            except OSError:
                pass
        print("Patcher removed. Automatic updates disabled; AdGuard Home configuration/data kept.")


def remove_recorded_backup(path, ui, prefix=".agh-patcher-backup-"):
    if path.parent != ui or not re.fullmatch(re.escape(prefix) + r"[a-f0-9]{32}", path.name) or path.is_symlink():
        raise Error("Refusing an unsafe recorded rollback path")
    if path.exists():
        durable_remove(path)


def setup(mode, repository, container, source, paths=None, runner=None, environ=None):
    paths = paths or Paths()
    runner = runner or Runner()
    environ = environ if environ is not None else os.environ
    if not all((source / "scripts" / name).is_file() for name in ("agh-patcher.py", "agh-launch.sh")):
        raise Error("Run setup through the released tools bundle's install/native or install/docker installer")
    spec = importlib.util.spec_from_file_location("release_identity", source / "scripts/release-manifest.py")
    identity = importlib.util.module_from_spec(spec)
    # Import verification code without writing a cache into an unverified bundle.
    bytecode_setting = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(identity)
    finally:
        sys.dont_write_bytecode = bytecode_setting
    try:
        released = identity.verify_tools(source)
    except (OSError, ValueError) as error:
        raise Error(f"Invalid released tools bundle: {error}") from error
    if paths.root == Path("/"):
        for name in (*identity.TOOLS_FILES, identity.TOOLS_MANIFEST):
            secure_path(source / name)
    old = read_config(paths) if paths.config.exists() else {}
    if old and old["mode"] != mode:
        raise Error("Uninstall the existing patcher mode before changing native/Docker mode")
    agh_dir = environ.get("AGH_DIR", old.get("agh_dir", "/opt/AdGuardHome"))
    config = {"schema": 1, "mode": mode,
              "tooling_revision": released["tooling_revision"],
              "repository": repository or old.get("repository", "rdna897/adguardhome-patcher"),
              "ui_root": agh_dir if mode == "native" else environ.get("AGH_UI_ROOT", old.get("ui_root", "/opt/adguardhome-patcher/ui"))}
    if mode == "native":
        config["agh_dir"] = agh_dir
    else:
        config["container"] = container or old.get("container", "adguardhome")
    patcher = Patcher(config, paths, runner)
    version = patcher.version()
    if released["adguard_version"] != version:
        raise Error(f"Tools bundle supports {released['adguard_version']}, but installed AdGuard Home is {version}")
    if mode == "native":
        content = runner.run("systemctl", "cat", UNIT).stdout
        lines = [line.split("=", 1)[1] for line in content.splitlines() if line.startswith("ExecStart=") and line != "ExecStart="]
        if not lines:
            raise Error("Cannot determine AdGuardHome.service startup arguments")
        command = lines[-1]
        binary, launcher = str(Path(agh_dir) / "AdGuardHome"), str(Path(agh_dir) / "agh-launch.sh")
        if binary in command:
            command = command.replace(binary, launcher, 1)
        elif launcher not in command:
            raise Error("Unexpected AdGuardHome.service startup command")
        if "\n" in agh_dir or "\r" in agh_dir:
            raise Error("Invalid AdGuard Home path")
    with patcher.lock():
        install_file(source / "scripts/agh-patcher.py", paths.command)
        target = Path(agh_dir) / "agh-launch.sh" if mode == "native" else paths.docker_launcher
        if target.is_symlink():
            raise Error("Refusing a symlinked launcher target")
        install_file(source / "scripts/agh-launch.sh", target)
        if mode == "docker":
            durable_mkdir(patcher.ui)
        atomic_json(paths.config, config)
        paths.config.chmod(0o644)
        if mode == "native":
            override = paths.systemd / (UNIT + ".d/dashboard-range.conf")
            if paths.root == Path("/"):
                secure_path(override)
            override.parent.mkdir(parents=True, exist_ok=True)
            override.write_text("# Added by adguardhome-patcher.\n[Service]\nExecStart=\nExecStart=" + command + "\n")
            runner.run("systemctl", "daemon-reload")
    revision, _ = patcher.local(version)
    print(f"AdGuard Home: {version}\nFrontend revision: {revision}\nTooling revision: {config['tooling_revision']}\n"
          "Automatic updates: disabled; no timer installed.\n"
          "Status: sudo agh-patcher status\nCheck: sudo agh-patcher check\n"
          "Install/update frontend: sudo agh-patcher update\nUninstall: sudo agh-patcher uninstall")


def main():
    parser = argparse.ArgumentParser(description="Manual AdGuard Home frontend patch management (no automatic updates)")
    parser.add_argument("command", choices=("status", "check", "update", "uninstall", "install-native", "install-docker"))
    parser.add_argument("arguments", nargs="*")
    parser.add_argument("--yes", action="store_true", help="explicitly approve update/uninstall without an interactive prompt")
    args = parser.parse_args()
    paths = Paths()
    try:
        if args.command not in ("status", "check") and os.geteuid() != 0:
            raise Error("Run this administrator action with sudo/root")
        if args.yes and args.command not in ("update", "uninstall"):
            raise Error("--yes is only for explicitly requested update/uninstall")
        if args.command.startswith("install-"):
            source = Path(__file__).resolve().parent.parent
            mode = args.command.removeprefix("install-")
            if len(args.arguments) > (1 if mode == "native" else 2):
                raise Error("Unexpected installer arguments")
            setup(mode, args.arguments[-1] if args.arguments and (mode == "native" or len(args.arguments) > 1) else None,
                  args.arguments[0] if mode == "docker" and args.arguments else None, source, paths)
            return
        if args.arguments:
            raise Error("Unexpected positional arguments")
        config = read_config(paths)
        patcher = Patcher(config, paths)
        if args.command in ("status", "check"):
            patcher.status(remote=args.command == "check")
        elif args.command == "update":
            patcher.update(args.yes)
        else:
            patcher.uninstall(args.yes)
    except (Error, OSError, ValueError, EOFError, tarfile.TarError) as error:
        print(f"agh-patcher: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
