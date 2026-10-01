"""Filesystem mutation tests use only temporary installations and fake services/network."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("patcher", SOURCE / "scripts/agh-patcher.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
spec = importlib.util.spec_from_file_location("identity", SOURCE / "scripts/release-manifest.py")
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)
VERSION = "v0.107.79"


def static_files(label):
    return {"index.html": b'<html><script src="main.js"></script></html>', "main.js": b"// fixture " + label.encode()}


def revision_of(label):
    return p.artifact_revision({name: hashlib.sha256(value).hexdigest() for name, value in static_files(label).items()})


OLD, NEW = revision_of("old"), revision_of("new")
LABELS = {OLD: "old", NEW: "new"}


def frontend(revision=NEW, version=VERSION, claimed=None):
    """A UI archive tree whose manifest revision is computed from its static bytes unless claimed."""
    files = static_files(LABELS[revision])
    manifest = {"schema": 1, "adguard_version": version, "revision_algorithm": p.FRONTEND_REVISION_ALGORITHM,
                "patch_revision": claimed or revision,
                "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
    return {**{"build/static/" + name: value for name, value in files.items()},
            "build/VERSION": version.encode(), "build/MANIFEST.json": json.dumps(manifest).encode()}


def archive(files, extra=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, value in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            bundle.addfile(member, io.BytesIO(value))
        if extra:
            bundle.addfile(extra)
    return buffer.getvalue()


class FakeNetwork:
    def __init__(self, payload=None, revision=NEW, version=VERSION):
        self.payload = payload if payload is not None else archive(frontend(revision, version))
        self.revision, self.version = revision, version
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.calls = []
        self.fail_download = False
        self.tooling = "c" * 64

    def read(self, url, limit):
        self.calls.append(url)
        if url.endswith(".sha256"):
            return (self.sha + "  " + p.ASSET + "\n").encode()
        tag = "ui-" + self.version
        body = "patch: " + self.revision + ("\ntooling: " + self.tooling if self.tooling else "")
        return json.dumps({"tag_name": tag, "draft": False,
                           "body": body,
                           "assets": [{"name": name, "browser_download_url":
                                       f"https://github.com/rdna897/adguardhome-patcher/releases/download/{tag}/{name}"}
                                      for name in (p.ASSET, p.ASSET + ".sha256")]}).encode()

    def download(self, url, target, limit):
        self.calls.append(url)
        if self.fail_download:
            raise p.Error("failed download")
        target.write_bytes(self.payload)


class FakeRunner:
    def __init__(self, paths, agh):
        self.paths, self.agh = paths, agh
        self.calls = []
        self.fail_restarts = 0
        self.running = True
        self.unhealthy_restarts = 0
        self.mounts = []
        self.version = VERSION
        self.legacy_states = []

    def run(self, *args, check=True):
        self.calls.append(args)
        code, stdout = 0, ""
        if args[-1] == "--version":
            stdout = "AdGuard Home, version " + self.version + "\n"
        elif args[:2] == ("systemctl", "cat"):
            stdout = f'[Service]\nExecStart={self.agh}/AdGuardHome -c {self.agh}/AdGuardHome.yaml -w {self.agh}/work\n'
        elif args[:2] == ("systemctl", "show"):
            if "--property=ActiveState" in args:
                stdout = self.legacy_states.pop(0) if self.legacy_states else "inactive"
            else:
                stdout = "loaded" if (self.paths.systemd / args[-1]).exists() else "not-found"
        elif args[:2] in (("systemctl", "restart"), ("docker", "restart")):
            if self.fail_restarts:
                self.fail_restarts -= 1
                code = 1
            if self.unhealthy_restarts:
                self.unhealthy_restarts -= 1
                self.running = False
            else:
                self.running = True
        elif args[:2] == ("systemctl", "is-active"):
            code = 0 if self.running else 1
        elif args[:2] == ("docker", "inspect"):
            stdout = json.dumps(self.mounts) if args[3] == "{{json .Mounts}}" else json.dumps({"Running": self.running})
        result = SimpleNamespace(returncode=code, stdout=stdout, stderr="fixture failure" if code else "")
        if check and code:
            raise p.Error("restart failed")
        return result


class ManualPatcher(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.paths = p.Paths(self.root)
        self.agh = self.root / "opt/AdGuardHome"
        (self.agh / "work").mkdir(parents=True)
        (self.agh / "AdGuardHome").write_bytes(b"official binary fixture")
        (self.agh / "AdGuardHome.yaml").write_bytes(b"configuration must remain intact")
        (self.agh / "work/data.db").write_bytes(b"server query log/statistics")
        self.config = {"schema": 1, "mode": "native", "repository": "rdna897/adguardhome-patcher",
                       "agh_dir": str(self.agh), "ui_root": str(self.agh)}
        self.runner = FakeRunner(self.paths, self.agh)
        self.network = FakeNetwork()
        self.tool = p.Patcher(self.config, self.paths, self.runner, self.network, confirm=lambda _: False)
        self.patch_sleep = patch.object(p.time, "sleep")
        self.patch_sleep.start()
        self.addCleanup(self.patch_sleep.stop)

    def snapshot(self):
        return {file.relative_to(self.root).as_posix(): (file.read_bytes(), file.stat().st_mode & 0o777)
                for file in self.root.rglob("*") if file.is_file() and not file.is_symlink()}

    def install_old(self, legacy=False):
        for name, value in frontend(OLD).items():
            path = self.agh / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        (self.agh / "build/SHA256").write_text("f" * 64)
        if legacy:
            (self.agh / "build/MANIFEST.json").unlink()
        else:
            (self.agh / "build/INSTALL.json").write_text(json.dumps({"patch_revision": OLD}))

    def legacy(self):
        self.paths.systemd.mkdir(parents=True, exist_ok=True)
        for name in ("agh-ui-sync.timer", "agh-ui-sync.service"):
            (self.paths.systemd / name).write_text("obsolete unit")
        wants = self.paths.systemd / "timers.target.wants"
        wants.mkdir()
        (wants / "agh-ui-sync.timer").symlink_to(self.paths.systemd / "agh-ui-sync.timer")
        self.paths.legacy_config.parent.mkdir(parents=True, exist_ok=True)
        self.paths.legacy_config.write_text(f"MODE=native\nAGH_DIR='{self.agh}'\nGITHUB_REPO='rdna897/adguardhome-patcher'\n")
        self.paths.legacy_command.parent.mkdir(parents=True, exist_ok=True)
        self.paths.legacy_command.write_text("legacy script")

    def assert_no_restart(self):
        self.assertFalse(any("restart" in command for command in self.runner.calls))

    def test_fresh_install_has_no_scheduler_or_frontend_download_restart(self):
        p.setup("native", None, None, SOURCE, self.paths, self.runner, {"AGH_DIR": str(self.agh)})
        self.assertTrue(self.paths.command.is_file())
        self.assertTrue((self.agh / "agh-launch.sh").is_file())
        self.assertFalse((self.agh / "build").exists())
        self.assertFalse(list(self.paths.systemd.rglob("*.timer")))
        self.assertFalse(list(self.paths.systemd.rglob("*.service")))
        self.assertFalse(any("enable" in command for command in self.runner.calls))
        self.assert_no_restart()
        self.assertEqual(self.network.calls, [])
        self.assertIn(f'-c {self.agh}/AdGuardHome.yaml -w {self.agh}/work',
                      (self.paths.systemd / (p.UNIT + ".d/dashboard-range.conf")).read_text())

    def test_migration_stops_disables_and_removes_legacy_units_and_preserves_frontend(self):
        self.install_old(legacy=True)
        self.legacy()
        before = (self.agh / "build/static/main.js").read_bytes()
        p.setup("native", None, None, SOURCE, self.paths, self.runner, {})
        for name in ("agh-ui-sync.timer", "agh-ui-sync.service"):
            self.assertIn(("systemctl", "disable", "--now", name), self.runner.calls)
            self.assertFalse((self.paths.systemd / name).exists())
        self.assertFalse((self.paths.systemd / "timers.target.wants/agh-ui-sync.timer").is_symlink())
        self.assertFalse(self.paths.legacy_command.exists())
        self.assertFalse(self.paths.legacy_config.exists())
        self.assertEqual(before, (self.agh / "build/static/main.js").read_bytes())
        self.assert_no_restart()

    def test_reinstall_is_idempotent_and_preserves_custom_path(self):
        p.setup("native", None, None, SOURCE, self.paths, self.runner, {"AGH_DIR": str(self.agh)})
        before = self.snapshot()
        p.setup("native", None, None, SOURCE, self.paths, self.runner, {})
        self.assertEqual(before, self.snapshot())

    def test_migration_waits_for_inflight_legacy_swap_after_stopping_timer(self):
        self.legacy()
        self.runner.legacy_states = ["activating", "active", "inactive"]
        p.setup("native", None, None, SOURCE, self.paths, self.runner, {"AGH_DIR": str(self.agh)})
        stop_timer = self.runner.calls.index(("systemctl", "disable", "--now", "agh-ui-sync.timer"))
        wait_service = self.runner.calls.index(("systemctl", "show", "--property=ActiveState", "--value", "agh-ui-sync.service"))
        stop_service = self.runner.calls.index(("systemctl", "disable", "--now", "agh-ui-sync.service"))
        self.assertLess(stop_timer, wait_service)
        self.assertLess(wait_service, stop_service)

    def test_busy_legacy_updater_aborts_migration_without_killing_swap(self):
        self.legacy()
        self.runner.legacy_states = ["activating"] * 60
        with self.assertRaisesRegex(p.Error, "still running; timer disabled"):
            p.setup("native", None, None, SOURCE, self.paths, self.runner, {"AGH_DIR": str(self.agh)})
        self.assertNotIn(("systemctl", "disable", "--now", "agh-ui-sync.service"), self.runner.calls)
        self.assertTrue(self.paths.legacy_command.exists())
        self.assertFalse(self.paths.command.exists())

    def test_status_is_entirely_local_and_read_only(self):
        self.install_old()
        before = self.snapshot()
        self.tool.status()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.network.calls, [])
        self.assertEqual(self.runner.calls, [(str(self.agh / "AdGuardHome"), "--version")])

    def test_check_is_read_only_and_reports_new_revision(self):
        self.install_old()
        before = self.snapshot()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.tool.status(remote=True)
        self.assertIn("Frontend update available", output.getvalue())
        self.assertIn("Available frontend:  " + NEW, output.getvalue())
        self.assertEqual(before, self.snapshot())
        self.assert_no_restart()
        self.assertEqual(len(self.network.calls), 1)

    def test_status_reports_missing_frontend_files(self):
        self.install_old()
        (self.agh / "build/static/main.js").unlink()
        self.assertIn("Missing or damaged", self.tool.local(VERSION)[1])

    def test_same_revision_is_current_without_mutation_or_download(self):
        self.install_old()
        self.network.revision = OLD
        before = self.snapshot()
        self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.network.calls), 1)
        self.assert_no_restart()

    def test_update_refuses_incompatible_release_metadata(self):
        self.network.version = "v0.107.78"
        before = self.snapshot()
        with self.assertRaisesRegex(p.Error, "does not explicitly support"):
            self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())
        self.assert_no_restart()

    def test_current_revision_still_retires_legacy_updater_after_confirmation(self):
        self.install_old()
        self.network.revision = OLD
        self.legacy()
        frontend_before = (self.agh / "build/static/main.js").read_bytes()
        before = self.snapshot()
        self.tool.update()
        self.assertEqual(before, self.snapshot())
        self.assert_no_restart()
        self.tool.update(yes=True)
        self.assertFalse((self.paths.systemd / "agh-ui-sync.timer").exists())
        self.assertFalse(self.paths.legacy_command.exists())
        self.assertEqual(frontend_before, (self.agh / "build/static/main.js").read_bytes())
        self.assert_no_restart()
        self.assertTrue(all("api.github.com" in url for url in self.network.calls))

    def test_update_refuses_incompatible_archive_even_with_correct_checksum(self):
        self.network.payload = archive(frontend(version="v0.107.78"))
        self.network.sha = hashlib.sha256(self.network.payload).hexdigest()
        before = self.snapshot()
        with self.assertRaisesRegex(p.Error, "different AdGuard Home"):
            self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())

    def test_checksum_mismatch_aborts_before_mutation(self):
        self.install_old()
        self.legacy()
        before = self.snapshot()
        self.network.sha = "0" * 64
        with self.assertRaisesRegex(p.Error, "checksum mismatch"):
            self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())
        self.assert_no_restart()
        self.assertFalse(any("disable" in command for command in self.runner.calls))

    def test_failed_download_aborts_before_mutation(self):
        self.install_old()
        before = self.snapshot()
        self.network.fail_download = True
        with self.assertRaisesRegex(p.Error, "failed download"):
            self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())
        self.assert_no_restart()

    def test_declined_confirmation_keeps_frontend_and_legacy_scheduler_unchanged(self):
        self.install_old()
        self.legacy()
        before = self.snapshot()
        self.tool.update()
        self.assertEqual(before, self.snapshot())
        self.assert_no_restart()
        self.assertFalse(any("disable" in command for command in self.runner.calls))

    def test_success_records_revision_and_keeps_rollback_and_server_data(self):
        self.install_old()
        data = (self.agh / "work/data.db").read_bytes()
        config = (self.agh / "AdGuardHome.yaml").read_bytes()
        self.tool.update(yes=True)
        self.assertEqual(self.tool.local(VERSION), (NEW, "Healthy"))
        receipt = json.loads((self.agh / "build/INSTALL.json").read_text())
        self.assertEqual(receipt["patch_revision"], NEW)
        self.assertEqual(receipt["archive_sha256"], self.network.sha)
        state = json.loads((self.paths.state / "last-update.json").read_text())
        self.assertEqual(b"// fixture old", (Path(state["backup"]) / "static/main.js").read_bytes())
        self.assertFalse((self.paths.state / "transaction.json").exists())
        self.assertEqual((self.agh / "work/data.db").read_bytes(), data)
        self.assertEqual((self.agh / "AdGuardHome.yaml").read_bytes(), config)

    def test_update_migrates_legacy_timer_only_after_confirmation(self):
        self.legacy()
        self.tool.update(yes=True)
        self.assertFalse((self.paths.systemd / "agh-ui-sync.timer").exists())
        self.assertIn(("systemctl", "disable", "--now", "agh-ui-sync.timer"), self.runner.calls)

    def test_failed_restart_restores_previous_frontend_and_revision(self):
        self.install_old()
        original = {k: v for k, v in self.snapshot().items() if "/build/" in k}
        self.runner.fail_restarts = 1
        with self.assertRaisesRegex(p.Error, "previous frontend restored"):
            self.tool.update(yes=True)
        restored = {k: v for k, v in self.snapshot().items() if "/build/" in k}
        self.assertEqual(original, restored)
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))
        self.assertEqual(sum("restart" in command for command in self.runner.calls), 2)

    def test_failed_frontend_validation_rolls_back(self):
        self.install_old()
        local = self.tool.local
        def corrupt_after_install(version):
            result = local(version)
            return (NEW, "Missing or damaged frontend") if result[0] == NEW else result
        with patch.object(self.tool, "local", side_effect=corrupt_after_install):
            with self.assertRaisesRegex(p.Error, "previous frontend restored"):
                self.tool.update(yes=True)
        self.assertEqual(local(VERSION), (OLD, "Healthy"))

    def test_failed_service_recovery_retains_transaction_and_reports_failure(self):
        self.install_old()
        self.runner.fail_restarts = 2
        with self.assertRaisesRegex(p.Error, "recovery needs administrator attention"):
            self.tool.update(yes=True)
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))
        self.assertTrue((self.paths.state / "transaction.json").exists())
        with self.assertRaisesRegex(p.Error, "interrupted update"):
            self.tool.update(yes=True)

    def test_binary_change_during_confirmation_aborts_before_frontend_mutation(self):
        self.install_old()
        def confirm(_):
            self.runner.version = "v0.107.80"
            return True
        self.tool.confirm = confirm
        with self.assertRaisesRegex(p.Error, "changed during preflight"):
            self.tool.update()
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))
        self.assert_no_restart()

    def test_update_operation_lock_blocks_concurrent_mutation(self):
        self.install_old()
        with self.tool.lock():
            with self.assertRaisesRegex(p.Error, "operation is running"):
                self.tool.update(yes=True)
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))
        self.assert_no_restart()

    def test_eof_confirmation_is_read_only(self):
        self.install_old()
        def eof(_):
            raise EOFError()
        self.tool.confirm = eof
        before = self.snapshot()
        with self.assertRaises(EOFError):
            self.tool.update()
        self.assertEqual(before, self.snapshot())

    def test_failure_after_rename_restores_before_restart(self):
        self.install_old()
        replace = p.os.replace
        def fail_stage(source, destination):
            if Path(source).name.startswith(".agh-patcher-stage-"):
                raise OSError("simulated disk failure")
            return replace(source, destination)
        with patch.object(p.os, "replace", side_effect=fail_stage):
            with self.assertRaisesRegex(p.Error, "previous frontend restored"):
                self.tool.update(yes=True)
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))
        self.assert_no_restart()

    def test_unhealthy_service_rolls_back_and_verifies_recovery(self):
        self.install_old()
        self.runner.unhealthy_restarts = 1
        with self.assertRaisesRegex(p.Error, "previous frontend restored"):
            self.tool.update(yes=True)
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))
        self.assertTrue(self.runner.running)

    def test_first_install_failure_restores_stock_ui_without_partial_build(self):
        self.runner.fail_restarts = 1
        with self.assertRaises(p.Error):
            self.tool.update(yes=True)
        self.assertFalse((self.agh / "build").exists())
        self.assertTrue(self.runner.running)

    def test_uninstall_removes_tooling_units_ui_backups_but_preserves_data(self):
        p.setup("native", None, None, SOURCE, self.paths, self.runner, {"AGH_DIR": str(self.agh)})
        self.install_old()
        self.tool.update(yes=True)
        self.legacy()
        unrelated = self.paths.systemd / (p.UNIT + ".d/admin.conf")
        unrelated.write_text("independent service override")
        original = {name: (self.agh / name).read_bytes() for name in ("AdGuardHome", "AdGuardHome.yaml", "work/data.db")}
        self.tool.uninstall(yes=True)
        for path in (self.paths.command, self.paths.config, self.paths.state, self.paths.legacy_command,
                     self.paths.legacy_config, self.agh / "build", self.agh / "agh-launch.sh"):
            self.assertFalse(path.exists(), str(path))
        self.assertTrue(unrelated.is_file())
        self.assertFalse(list(self.agh.glob(".agh-patcher-*")))
        self.assertFalse(list(self.paths.systemd.rglob("agh-ui-sync.*")))
        for name, data in original.items():
            self.assertEqual((self.agh / name).read_bytes(), data)

    def test_legacy_uninstall_without_new_tooling_also_cleans_units_and_preserves_data(self):
        self.install_old(legacy=True)
        self.legacy()
        self.tool.uninstall(yes=True)
        self.assertFalse((self.agh / "build").exists())
        self.assertFalse(list(self.paths.systemd.rglob("agh-ui-sync.*")))
        self.assertTrue((self.agh / "AdGuardHome.yaml").is_file())
        self.assertTrue((self.agh / "work/data.db").is_file())

    def test_unsafe_archives_are_rejected_before_mutation(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("build/static/link", tarfile.SYMTYPE), ("build/static/hard", tarfile.LNKTYPE),
                           ("build/static/fifo", tarfile.FIFOTYPE), ("build/script.sh", tarfile.REGTYPE)):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "/etc/passwd" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
                self.network = FakeNetwork(archive(frontend(), member))
                self.tool.network = self.network
                before = self.snapshot()
                with self.assertRaises(p.Error):
                    self.tool.update(yes=True)
                self.assertEqual(before, self.snapshot())
                self.assert_no_restart()

    def test_revision_manifest_mismatch_and_missing_script_are_rejected(self):
        for files in (frontend(OLD), {k: v for k, v in frontend().items() if k != "build/static/main.js"}):
            self.tool.network = FakeNetwork(archive(files))
            before = self.snapshot()
            with self.assertRaises(p.Error):
                self.tool.update(yes=True)
            self.assertEqual(before, self.snapshot())

    def test_docker_setup_has_no_scheduler_and_uninstall_requires_stock_container(self):
        p.setup("docker", None, "adguardhome", SOURCE, self.paths, self.runner,
                {"AGH_UI_ROOT": str(self.root / "ui")})
        config = p.read_config(self.paths)
        tool = p.Patcher(config, self.paths, self.runner, self.network)
        self.assertFalse(list(self.paths.systemd.rglob("*.timer")))
        self.assert_no_restart()
        self.runner.mounts = [{"Destination": "/opt/adguardhome/ui"}]
        before = self.snapshot()
        with self.assertRaisesRegex(p.Error, "Recreate the Docker container"):
            tool.uninstall(yes=True)
        self.assertEqual(before, self.snapshot())
        self.runner.mounts = []
        tool.uninstall(yes=True)
        self.assertFalse(self.paths.command.exists())
        self.assertFalse(self.paths.docker_launcher.exists())

    def test_docker_update_checks_container_and_keeps_parent_mount_layout(self):
        self.config.update(mode="docker", container="adguardhome", ui_root=str(self.root / "ui"))
        tool = p.Patcher(self.config, self.paths, self.runner, self.network)
        self.runner.mounts = [{"Destination": "/opt/adguardhome/ui", "Source": str(self.root / "ui")}]
        tool.update(yes=True)
        self.assertEqual(tool.local(VERSION), (NEW, "Healthy"))
        self.assertIn(("docker", "restart", "adguardhome"), self.runner.calls)

    def test_retired_sync_entrypoint_is_inert(self):
        result = subprocess.run(["sh", str(SOURCE / "scripts/agh-ui-sync.sh")], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retired", result.stderr)

    def identity_tree(self):
        root = self.root / "identity"
        for name in (*identity.FRONTEND_INPUTS, *identity.TOOLING_INPUTS,
                     "README.md", "docs/manual-updates.md", "scripts/tests/test-patcher.py"):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / name, target)
        return root

    def static_tree(self, name, files, order=None):
        static = self.root / name / "static"
        for relative in order or files:
            target = static / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files[relative])
        return static

    BUILT = {"index.html": b'<html><script src="main.0123.js"></script><link href="main.a13f.css"></html>',
             "main.0123.js": b"bundle", "main.a13f.css": b"body{}", "assets/favicon.png": b"\x89PNG",
             "assets/deep/icon.svg": b"<svg/>"}

    def test_artifact_revision_ignores_creation_order_and_timestamps(self):
        first = self.static_tree("first", self.BUILT)
        second = self.static_tree("second", self.BUILT, order=sorted(self.BUILT, reverse=True))
        revision = p.frontend_artifact_revision(first)
        self.assertRegex(revision, "^[a-f0-9]{64}$")
        self.assertEqual(p.frontend_artifact_revision(second), revision)
        for path in (second, *second.rglob("*")):
            os.utime(path, (1, 1))
        self.assertEqual(p.frontend_artifact_revision(second), revision)

    def test_artifact_revision_changes_with_any_static_content_or_file_set(self):
        static = self.static_tree("built", self.BUILT)
        before = p.frontend_artifact_revision(static)
        changes = (
            lambda: (static / "assets/deep/icon.svg").write_bytes(b"<svg />"),
            lambda: (static / "late.js").write_bytes(b"added"),
            lambda: (static / "assets/favicon.png").unlink(),
            lambda: (static / "main.a13f.css").rename(static / "main.57ad.css"),
        )
        for change in changes:
            with self.subTest(change=change):
                shutil.rmtree(static.parent)
                static = self.static_tree("built", self.BUILT)
                self.assertEqual(p.frontend_artifact_revision(static), before)
                change()
                self.assertNotEqual(p.frontend_artifact_revision(static), before)

    def test_artifact_revision_rejects_links_special_files_and_unexpected_paths(self):
        cases = {
            "file link": lambda static: (static / "link.js").symlink_to(static / "main.0123.js"),
            "directory link": lambda static: (static / "linked").symlink_to(static / "assets", target_is_directory=True),
            "fifo": lambda static: os.mkfifo(static / "fifo"),
            "backslash": lambda static: (static / "odd\\name.js").write_bytes(b"x"),
            "control character": lambda static: (static / "odd\nname.js").write_bytes(b"x"),
        }
        for name, make in cases.items():
            with self.subTest(name=name):
                static = self.static_tree(name.replace(" ", "-"), self.BUILT)
                make(static)
                with self.assertRaises(p.Error):
                    p.frontend_artifact_revision(static)
        outside = self.static_tree("outside", self.BUILT)
        (self.root / "linked-static").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(p.Error):
            p.frontend_artifact_revision(self.root / "linked-static")

    def test_docs_tests_and_tooling_do_not_change_built_artifact_revision(self):
        root = self.identity_tree()
        build = self.static_tree("manifest-build", self.BUILT).parent
        first = identity.manifest(root, build, VERSION)
        for name in ("README.md", "docs/manual-updates.md", "scripts/tests/test-patcher.py",
                     "scripts/agh-patcher.py", "install/native/install.sh"):
            file = root / name
            file.write_text(file.read_text() + "\n# documentation/test/tooling change\n")
        second = identity.manifest(root, build, VERSION)
        self.assertEqual(second["patch_revision"], first["patch_revision"])
        self.assertEqual(second["patch_revision"], p.frontend_artifact_revision(build / "static"))
        self.assertEqual(second["files"], first["files"])
        self.assertEqual(second["frontend_input_revision"], first["frontend_input_revision"])
        self.assertNotEqual(second["tooling_revision"], first["tooling_revision"])
        self.assertNotIn("MANIFEST.json", second["files"])

    def test_build_environment_change_rebuilds_and_changed_output_has_new_revision(self):
        root = self.identity_tree()
        before = identity.frontend_input_revision(root)
        workflow = root / ".github/workflows/build.yml"
        self.assertIn("node-version: 22", workflow.read_text())
        workflow.write_text(workflow.read_text().replace("node-version: 22", "node-version: 24"))
        self.assertNotEqual(identity.frontend_input_revision(root), before)
        # Same CSS bytes under another webpack compilation hash is still a different frontend.
        renamed = {**self.BUILT, "index.html": self.BUILT["index.html"].replace(b"a13f", b"57ad")}
        renamed["main.57ad.css"] = renamed.pop("main.a13f.css")
        self.assertNotEqual(p.frontend_artifact_revision(self.static_tree("node22", self.BUILT)),
                            p.frontend_artifact_revision(self.static_tree("node24", renamed)))

    def test_frontend_input_fingerprint_tracks_build_inputs_but_not_timestamps(self):
        root = self.identity_tree()
        for name in identity.FRONTEND_INPUTS:
            with self.subTest(name=name):
                before = identity.frontend_input_revision(root)
                file = root / name
                os.utime(file, (1, 1))
                self.assertEqual(identity.frontend_input_revision(root), before)
                file.write_text(file.read_text() + "\n# frontend build change\n")
                self.assertNotEqual(identity.frontend_input_revision(root), before)

    def test_tooling_changes_independently_of_frontend_inputs(self):
        root = self.identity_tree()
        input_before, tooling_before = identity.frontend_input_revision(root), identity.tooling_revision(root)
        for name in ("scripts/agh-patcher.py", "install/native/install.sh", "install/docker/compose.override.yaml"):
            file = root / name
            file.write_text(file.read_text() + "\n# management change\n")
        self.assertEqual(identity.frontend_input_revision(root), input_before)
        self.assertNotEqual(identity.tooling_revision(root), tooling_before)

    def release_dir(self, root, claimed=None):
        build = self.root / "release/build"
        static = self.static_tree("release/build", static_files("new")).parent
        self.assertEqual(static, build)
        (build / "VERSION").write_text(VERSION + "\n")
        result = identity.manifest(root, build, VERSION)
        if claimed:
            result["patch_revision"] = claimed
            (build / "MANIFEST.json").write_text(json.dumps(result))
        out = self.root / "release/dist"
        out.mkdir(exist_ok=True)
        with tarfile.open(out / p.ASSET, "w:gz") as bundle:
            for name in ("build/static", "build/VERSION", "build/MANIFEST.json"):
                bundle.add(self.root / "release" / name, name)
        return out, result

    def test_release_revision_is_recomputed_from_the_packaged_frontend(self):
        root = self.identity_tree()
        out, result = self.release_dir(root)
        verified = identity.archive_revision(root, out, VERSION)
        self.assertEqual(verified["patch_revision"], result["patch_revision"])
        self.assertEqual(verified["patch_revision"], NEW)

    def test_release_with_false_manifest_revision_cannot_be_published(self):
        root = self.identity_tree()
        out, _ = self.release_dir(root, claimed=OLD)
        # release-manifest.py loads its own updater module, so match the updater's message.
        with self.assertRaisesRegex(Exception, "does not describe its frontend files"):
            identity.archive_revision(root, out, VERSION)

    def test_archive_with_false_manifest_revision_is_rejected_before_mutation(self):
        self.install_old()
        forged = "d" * 64
        self.tool.network = self.network = FakeNetwork(archive(frontend(NEW, claimed=forged)), revision=forged)
        before = self.snapshot()
        with self.assertRaisesRegex(p.Error, "does not describe its frontend files"):
            self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())
        self.assert_no_restart()

    def test_release_metadata_revision_must_match_archive_frontend(self):
        self.install_old()
        self.network.revision = "e" * 64
        before = self.snapshot()
        with self.assertRaisesRegex(p.Error, "does not match release metadata"):
            self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())
        self.assert_no_restart()
        target = self.root / "extracted"
        (self.root / "valid.tar.gz").write_bytes(archive(frontend(NEW)))
        with self.assertRaisesRegex(p.Error, "does not match release metadata"):
            p.validate_archive(self.root / "valid.tar.gz", target, VERSION, OLD)

    def test_archive_without_artifact_revision_must_be_rebuilt(self):
        files = frontend(NEW)
        manifest = json.loads(files["build/MANIFEST.json"])
        del manifest["revision_algorithm"]
        files["build/MANIFEST.json"] = json.dumps(manifest).encode()
        self.tool.network = FakeNetwork(archive(files))
        before = self.snapshot()
        with self.assertRaisesRegex(p.Error, "no frontend artefact revision"):
            self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())

    def check_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.tool.status(remote=True)
        return output.getvalue()

    def test_same_frontend_newer_tooling_never_offers_or_installs_frontend_update(self):
        root = self.identity_tree()
        installed_tooling = identity.tooling_revision(root)
        self.install_old()
        self.config["tooling_revision"] = installed_tooling
        manager = root / "scripts/agh-patcher.py"
        manager.write_text(manager.read_text() + "\n# newer management tooling\n")
        self.network.revision, self.network.tooling = OLD, identity.tooling_revision(root)
        self.assertNotEqual(self.network.tooling, installed_tooling)
        before = self.snapshot()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.tool.status(remote=True)
            self.tool.update(yes=True)
        text = output.getvalue()
        self.assertIn("Status:              Frontend up to date; tooling update available", text)
        self.assertIn("Tooling revision:    " + installed_tooling, text)
        self.assertIn("Available tooling:   " + self.network.tooling, text)
        self.assertIn("Next action:         " + p.TOOLING_UPDATE, text)
        self.assertIn("Up to date: " + OLD, text)
        self.assertNotIn("Frontend update available", text)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(all("api.github.com" in url for url in self.network.calls))
        self.assert_no_restart()

    def test_changed_frontend_bytes_offer_update_independently_of_tooling(self):
        self.install_old()
        self.config["tooling_revision"] = self.network.tooling
        text = self.check_output()
        self.assertIn("Status:              Frontend update available; tooling up to date", text)
        self.assertIn("Next action:         Run sudo agh-patcher update", text)
        self.assertNotIn(p.TOOLING_UPDATE, text)

    def test_check_with_unrecorded_tooling_keeps_frontend_current(self):
        self.install_old()
        self.network.revision = OLD
        text = self.check_output()
        self.assertIn("Tooling revision:    Unknown (configuration predates tooling identity", text)
        self.assertIn("Frontend up to date; installed tooling unknown", text)
        self.assertIn("rerun its installer", text)
        self.assertNotIn("Frontend update available", text)

    def test_status_is_offline_and_leaves_available_tooling_unknown(self):
        self.install_old()
        self.config["tooling_revision"] = "1" * 64
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.tool.status()
        self.assertIn("Tooling revision:    " + "1" * 64, output.getvalue())
        self.assertIn("Available tooling:   " + p.NOT_CHECKED, output.getvalue())
        self.assertNotIn("Next action", output.getvalue())
        self.assertEqual(self.network.calls, [])

    def test_release_without_tooling_identity_is_reported_without_frontend_update(self):
        self.install_old()
        self.network.revision, self.network.tooling = OLD, None
        self.config["tooling_revision"] = "1" * 64
        text = self.check_output()
        self.assertIn("Available tooling:   Not published by this release", text)
        self.assertIn("Frontend up to date; tooling revision not published", text)

    def test_extra_installed_static_file_is_damage_and_changes_identity(self):
        self.install_old()
        (self.agh / "build/static/extra.js").write_bytes(b"unexpected")
        self.assertEqual(self.tool.local(VERSION), (OLD, "Unexpected frontend file: extra.js"))
        self.network.revision = OLD
        self.assertIn("Frontend update available; Unexpected frontend file", self.check_output())

    def test_preartifact_manifest_is_identified_by_installed_bytes(self):
        self.install_old()
        manifest_path = self.agh / "build/MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        del manifest["revision_algorithm"]
        manifest["patch_revision"] = "a" * 64
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))
        self.network.revision = OLD
        before = self.snapshot()
        self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())
        self.assert_no_restart()

    def test_forged_installed_manifest_revision_is_reported(self):
        self.install_old()
        manifest_path = self.agh / "build/MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["patch_revision"] = NEW
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(self.tool.local(VERSION)[1], "Frontend manifest revision does not match its installed files")

    def test_installer_migration_removes_old_source_templates_and_updates_tooling_without_restart(self):
        root = self.identity_tree()
        directory = root / "install/systemd"
        directory.mkdir()
        for name in ("agh-ui-sync.timer", "agh-ui-sync.service"):
            (directory / name).write_text("old template")
        (directory / "administrator.service").write_text("keep unrelated template")
        (root / "ui").mkdir()
        (root / "ui/custom").write_text("preserve UI parent")
        self.install_old(legacy=True)
        self.legacy()
        frontend_before = {name: value for name, value in self.snapshot().items() if "/build/" in name}
        p.setup("native", None, None, root, self.paths, self.runner, {})
        self.assertFalse(list(directory.glob("agh-ui-sync.*")))
        self.assertTrue((directory / "administrator.service").exists())
        self.assertTrue((root / "ui/custom").exists())
        self.assertEqual(self.paths.command.read_bytes(), (root / "scripts/agh-patcher.py").read_bytes())
        self.assertEqual(p.read_config(self.paths)["tooling_revision"], identity.tooling_revision(root))
        self.assertEqual(frontend_before, {name: value for name, value in self.snapshot().items() if "/build/" in name})
        self.assertFalse(list(self.paths.systemd.rglob("agh-ui-sync.*")))
        self.assertFalse(self.paths.legacy_config.exists())
        self.assert_no_restart()

    def test_obsolete_source_cleanup_refuses_symlinked_parent(self):
        root = self.identity_tree()
        outside = self.root / "outside"
        outside.mkdir()
        target = outside / "agh-ui-sync.timer"
        target.write_text("must not remove outside source")
        (root / "install/systemd").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(p.Error, "symbolic link"):
            p.clean_legacy_source(root)
        self.assertTrue(target.exists())

    @contextlib.contextmanager
    def durability_trace(self):
        events = []
        original_replace, original_directory = p.os.replace, p.fsync_directory
        original_json, original_tree, original_remove = p.atomic_json, p.sync_tree, p.durable_remove
        def replace(source, target):
            original_replace(source, target)
            events.append(("rename", Path(source), Path(target)))
        def directory(path):
            original_directory(path)
            events.append(("directory", Path(path)))
        def commit_json(path, data):
            original_json(path, data)
            events.append(("json", path, data.get("phase")))
        def tree(path):
            original_tree(path)
            events.append(("tree", path))
        def remove(path):
            original_remove(path)
            events.append(("remove", path))
        with patch.object(p.os, "replace", side_effect=replace), \
             patch.object(p, "fsync_directory", side_effect=directory), \
             patch.object(p, "atomic_json", side_effect=commit_json), \
             patch.object(p, "sync_tree", side_effect=tree), \
             patch.object(p, "durable_remove", side_effect=remove):
            yield events

    def test_json_flushes_file_before_rename_then_parent_directory(self):
        events = []
        original_fsync, original_replace = p.os.fsync, p.os.replace
        def sync(descriptor):
            events.append("directory" if p.stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
            original_fsync(descriptor)
        def replace(source, target):
            events.append("rename")
            original_replace(source, target)
        with patch.object(p.os, "fsync", side_effect=sync), patch.object(p.os, "replace", side_effect=replace):
            p.atomic_json(self.root / "journal.json", {"phase": "prepared"})
        self.assertEqual(events, ["file", "rename", "directory"])

    def test_durable_file_flush_rejects_special_files_without_blocking(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(p.Error, "non-regular"):
            p.fsync_file(fifo)

    def test_transaction_data_journal_renames_commit_and_removal_are_durably_ordered(self):
        self.install_old()
        journal = self.paths.state / "transaction.json"
        with self.durability_trace() as events:
            self.tool.update(yes=True)
        prepared = events.index(("json", journal, "prepared"))
        backup_saved = events.index(("json", journal, "backup_saved"))
        installed = events.index(("json", journal, "installed"))
        committed = events.index(("json", journal, "committed"))
        receipt = events.index(("json", self.paths.state / "last-update.json", None))
        removed = events.index(("remove", journal))
        backup_move = next(i for i, event in enumerate(events) if event[0] == "rename" and event[1] == self.tool.build)
        live_move = next(i for i, event in enumerate(events) if event[0] == "rename" and event[2] == self.tool.build)
        stage_synced = next(i for i, event in enumerate(events) if event[0] == "tree" and event[1].name.startswith(".agh-patcher-stage-"))
        self.assertLess(stage_synced, prepared)
        self.assertLess(events.index(("tree", self.tool.build)), prepared)
        self.assertEqual(events[prepared - 1], ("directory", self.paths.state))
        self.assertEqual(events[backup_move + 1], ("directory", self.agh))
        self.assertEqual(events[live_move + 1], ("directory", self.agh))
        self.assertEqual(events[receipt - 1], ("directory", self.paths.state))
        self.assertEqual(events[removed - 1], ("directory", self.paths.state))
        self.assertEqual(sorted((prepared, backup_move, backup_saved, live_move, installed, receipt, committed, removed)),
                         [prepared, backup_move, backup_saved, live_move, installed, receipt, committed, removed])

    def test_rollback_rename_is_flushed_before_recovery_record_removal(self):
        self.install_old()
        self.runner.fail_restarts = 1
        journal = self.paths.state / "transaction.json"
        with self.durability_trace() as events:
            with self.assertRaisesRegex(p.Error, "previous frontend restored"):
                self.tool.update(yes=True)
        rollback = next(i for i, event in enumerate(events) if event[0] == "rename" and event[1].name.startswith(".agh-patcher-backup-"))
        self.assertLess(events.index(("json", journal, "rolling_back")), rollback)
        self.assertEqual(events[rollback + 1], ("directory", self.agh))
        self.assertLess(rollback, events.index(("json", journal, "rolled_back")))
        self.assertLess(events.index(("json", journal, "rolled_back")), events.index(("remove", journal)))
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))

    def test_staging_flush_failure_preserves_old_frontend_without_restart(self):
        self.install_old()
        with patch.object(p, "fsync_file", side_effect=OSError("storage cannot flush staged data")):
            with self.assertRaisesRegex(p.Error, "previous frontend restored"):
                self.tool.update(yes=True)
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))
        self.assertFalse((self.paths.state / "transaction.json").exists())
        self.assert_no_restart()

    def test_journal_flush_failure_occurs_before_any_live_rename(self):
        self.install_old()
        journal = self.paths.state / "transaction.json"
        original = p.fsync_directory
        def fail_prepared(path):
            if path == self.paths.state and journal.exists() and json.loads(journal.read_text())["phase"] == "prepared":
                raise OSError("journal parent fsync failed")
            original(path)
        with patch.object(p, "fsync_directory", side_effect=fail_prepared):
            with self.assertRaisesRegex(p.Error, "previous frontend restored"):
                self.tool.update(yes=True)
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))
        self.assert_no_restart()

    def test_failed_directory_flush_after_live_rename_still_restores_previous_build(self):
        self.install_old()
        original = p.fsync_directory
        failed = False
        def fail_live(path):
            nonlocal failed
            if path == self.agh and not failed and self.tool.local(VERSION)[0] == NEW:
                failed = True
                raise OSError("live rename directory fsync failed")
            original(path)
        with patch.object(p, "fsync_directory", side_effect=fail_live):
            with self.assertRaisesRegex(p.Error, "previous frontend restored"):
                self.tool.update(yes=True)
        self.assertTrue(failed)
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))
        self.assertTrue(self.runner.running)

    def test_failed_commit_restores_previous_build_and_success_receipt(self):
        self.install_old()
        last_update = self.paths.state / "last-update.json"
        previous = {"revision": OLD, "backup": None}
        p.atomic_json(last_update, previous)
        original = p.atomic_json
        def fail_commit(path, data):
            original(path, data)
            if path.name == "transaction.json" and data.get("phase") == "committed":
                raise OSError("commit durability failure")
        with patch.object(p, "atomic_json", side_effect=fail_commit):
            with self.assertRaisesRegex(p.Error, "previous frontend restored"):
                self.tool.update(yes=True)
        self.assertEqual(self.tool.local(VERSION), (OLD, "Healthy"))
        self.assertEqual(json.loads(last_update.read_text()), previous)

    def test_untrusted_config_and_privileged_path_symlinks_are_rejected(self):
        p.atomic_json(self.paths.config, self.config)
        self.paths.config.chmod(0o666)
        with self.assertRaisesRegex(p.Error, "not writable"):
            p.read_config(self.paths)
        link = self.root / "symlink"
        link.symlink_to(self.agh, target_is_directory=True)
        with self.assertRaises(p.Error):
            p.secure_path(link / "file")
        with self.assertRaises(p.Error):
            p.safe_directory(link)

    def test_checksum_filename_injection_and_duplicate_members_are_rejected(self):
        before = self.snapshot()
        read = self.network.read
        self.network.read = lambda url, limit: (("0" * 64 + "  ../../configuration\n").encode()
                                                if url.endswith(".sha256") else read(url, limit))
        with self.assertRaisesRegex(p.Error, "Malformed published checksum"):
            self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())
        duplicate = tarfile.TarInfo("build/VERSION")
        self.tool.network = FakeNetwork(archive(frontend(), duplicate))
        with self.assertRaisesRegex(p.Error, "duplicate"):
            self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())

    def test_https_redirect_downgrade_is_rejected(self):
        with self.assertRaisesRegex(p.Error, "non-HTTPS"):
            p.HTTPSRedirect().redirect_request(None, None, 302, "", {}, "http://unsafe.example/archive")

    def test_interrupted_transaction_refused_even_if_revision_matches(self):
        self.install_old()
        self.network.revision = OLD
        p.atomic_json(self.paths.state / "transaction.json", {"backup": "retained"})
        before = self.snapshot()
        with self.assertRaisesRegex(p.Error, "interrupted update"):
            self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())

    def test_unavailable_remote_check_never_mutates_installation(self):
        self.install_old()
        before = self.snapshot()
        with patch.object(self.network, "read", side_effect=p.Error("No compatible release is available")):
            with self.assertRaisesRegex(p.Error, "No compatible"):
                self.tool.status(remote=True)
        self.assertEqual(before, self.snapshot())
        self.assert_no_restart()

    def test_missing_legacy_frontend_is_reported(self):
        self.install_old(legacy=True)
        (self.agh / "build/static/index.html").unlink()
        self.assertEqual(self.tool.local(VERSION)[1], "Missing patched frontend index")

    def test_corrupt_tar_with_valid_checksum_never_mutates(self):
        self.tool.network = FakeNetwork(b"not a tar archive")
        before = self.snapshot()
        with self.assertRaises(tarfile.TarError):
            self.tool.update(yes=True)
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main()
