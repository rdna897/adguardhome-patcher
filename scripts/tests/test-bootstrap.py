"""Root install.sh bootstrap tests with fake root, Docker and GitHub; no network or /opt writes."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[2]
REPOSITORY = "github.com/rdna897/adguardhome-patcher/releases/download/"
ASSET = "agh-patcher-tools.tar.gz"
SHELL = shutil.which("sh")
REAL_TOOLS = ("sh", "tar", "gzip", "sha256sum", "mktemp", "python3", "sed", "grep", "head",
              "cut", "wc", "mkdir", "rm", "cp")
FAKES = {
    "id": 'echo "${FAKE_UID:-0}"\n',
    # Serves https://<path> from $FAKE_WEB/<path> and records the requested URL.
    "curl": ('url= out=\nprintf \'%s\\n\' "$*" >>"$FAKE_LOG/curl-args"\n'
             'while [ $# -gt 0 ]; do case $1 in -o) out=$2; shift ;; https://*) url=$1 ;; esac; shift; done\n'
             'printf \'%s\\n\' "$url" >>"$FAKE_LOG/urls"\n'
             '[ -f "$FAKE_WEB/${url#https://}" ] || { echo "curl: (22) 404" >&2; exit 22; }\n'
             'cp "$FAKE_WEB/${url#https://}" "$out"\n'),
    "docker": ('printf \'%s\\n\' "$*" >>"$FAKE_LOG/docker"\n'
               '[ "$*" = "exec $FAKE_CONTAINER /opt/adguardhome/AdGuardHome --version" ] || '
               '{ echo "Error: No such container" >&2; exit 1; }\n'
               'printf \'%s\\n\' "$FAKE_VERSION_OUTPUT"\n'),
}
# The released installers record how the bootstrap handed off to them.
HANDOFF = ('printf \'%s\\n\' "{mode}" "AGH_DIR=${{AGH_DIR-unset}}" "$@" >"$FAKE_LOG/handoff"\n'
           'echo "{mode} installer output"\nexit "${{HANDOFF_STATUS:-0}}"\n')


def bundle():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in {"install/native/install.sh": HANDOFF.format(mode="native"),
                           "install/docker/install.sh": HANDOFF.format(mode="docker"),
                           "TOOLS.json": "{}\n"}.items():
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o755
            archive.addfile(member, io.BytesIO(data.encode()))
    return buffer.getvalue()


class Bootstrap(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tools = self.root / "opt/adguardhome-patcher"
        self.log, self.web, self.tmp, self.bin = (self.root / name for name in ("log", "web", "tmp", "bin"))
        for path in (self.log, self.web, self.tmp, self.bin):
            path.mkdir()
        script = (SOURCE / "install.sh").read_text()
        self.assertEqual(script.count("\ntools_dir=/opt/adguardhome-patcher\n"), 1)
        self.script = self.root / "install.sh"
        self.script.write_text(script.replace("\ntools_dir=/opt/adguardhome-patcher\n", f"\ntools_dir={self.tools}\n"))
        for name in REAL_TOOLS:
            (self.bin / name).symlink_to(shutil.which(name))
        for name, body in FAKES.items():
            self.fake(name, body)
        self.agh_dir = self.root / "custom/AdGuardHome"
        self.agh_dir.mkdir(parents=True)
        self.fake("AdGuardHome", 'printf \'%s\\n\' "$FAKE_VERSION_OUTPUT"\n', self.agh_dir)
        self.publish("v0.107.79")
        # Existing frontend and managed override state must survive extraction.
        (self.tools / "ui").mkdir(parents=True)
        (self.tools / "ui/marker").write_text("installed frontend\n")
        (self.tools / "compose.patcher.yaml").write_text("managed override\n")
        self.env = {"PATH": str(self.bin), "TMPDIR": str(self.tmp), "FAKE_LOG": str(self.log),
                    "FAKE_WEB": str(self.web), "FAKE_CONTAINER": "agh-container",
                    "FAKE_VERSION_OUTPUT": "AdGuard Home, version v0.107.79"}

    def fake(self, name, body, directory=None):
        path = (directory or self.bin) / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def publish(self, version, payload=None, checksum=None):
        release = self.web / (REPOSITORY + "ui-" + version)
        release.mkdir(parents=True)
        payload = bundle() if payload is None else payload
        (release / ASSET).write_bytes(payload)
        checksum = checksum or hashlib.sha256(payload).hexdigest() + "  " + ASSET + "\n"
        (release / (ASSET + ".sha256")).write_text(checksum)

    def run_bootstrap(self, *args, **env):
        result = subprocess.run([SHELL, str(self.script), *args], env={**self.env, **env},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(list(self.tmp.iterdir()), [], "temporary download directory left behind")
        return result

    def logged(self, name):
        path = self.log / name
        return path.read_text().splitlines() if path.exists() else []

    def assert_failed(self, result, message, status=1):
        self.assertEqual(result.returncode, status, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertEqual(self.logged("handoff"), [])
        self.assertFalse((self.tools / "install").exists())
        self.assertEqual((self.tools / "ui/marker").read_text(), "installed frontend\n")

    def assert_urls(self, version):
        base = "https://" + REPOSITORY + "ui-" + version + "/"
        self.assertEqual(self.logged("urls"), [base + ASSET, base + ASSET + ".sha256"])
        self.assertTrue(all(line.startswith("-fsSL --proto =https") and "--retry 3" in line
                            for line in self.logged("curl-args")))

    def assert_unsafe_destination(self, message):
        (self.bin / "tar").unlink()
        self.fake("tar", 'echo extraction >>"$FAKE_LOG/tar"\nexit 1\n')
        result = self.run_bootstrap("native", AGH_DIR=str(self.agh_dir))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertEqual(self.logged("tar"), [], "archive extraction was attempted")
        self.assertEqual(self.logged("handoff"), [])

    def test_symlinked_tools_directory_is_rejected_without_modifying_target(self):
        target = self.root / "redirected"
        self.tools.rename(target)
        self.tools.symlink_to(target, target_is_directory=True)
        before = {str(path.relative_to(target)): path.read_bytes() if path.is_file() else None
                  for path in target.rglob("*")}
        self.assert_unsafe_destination("refusing symlinked tools directory")
        self.assertTrue(self.tools.is_symlink())
        self.assertEqual(self.tools.readlink(), target)
        self.assertEqual({str(path.relative_to(target)): path.read_bytes() if path.is_file() else None
                          for path in target.rglob("*")}, before)

    def test_non_directory_tools_destination_is_rejected_without_modifying_file(self):
        shutil.rmtree(self.tools)
        self.tools.write_text("existing file\n")
        self.assert_unsafe_destination("tools path exists and is not a directory")
        self.assertTrue(self.tools.is_file())
        self.assertEqual(self.tools.read_text(), "existing file\n")

    def test_symlinked_tools_parent_is_rejected_without_modifying_target(self):
        parent = self.tools.parent
        target = self.root / "redirected-opt"
        parent.rename(target)
        parent.symlink_to(target, target_is_directory=True)
        before = {str(path.relative_to(target)): path.read_bytes() if path.is_file() else None
                  for path in target.rglob("*")}
        self.assert_unsafe_destination("refusing symlinked tools parent directory")
        self.assertTrue(parent.is_symlink())
        self.assertEqual(parent.readlink(), target)
        self.assertEqual({str(path.relative_to(target)): path.read_bytes() if path.is_file() else None
                          for path in target.rglob("*")}, before)

    def test_native_detects_version_and_hands_off_with_agh_dir(self):
        result = self.run_bootstrap("native", AGH_DIR=str(self.agh_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_urls("v0.107.79")
        self.assertEqual(self.logged("handoff"), ["native", f"AGH_DIR={self.agh_dir}"])
        self.assertEqual(self.logged("docker"), [])
        self.assertTrue((self.tools / "install/native/install.sh").is_file())
        self.assertEqual((self.tools / "ui/marker").read_text(), "installed frontend\n")
        self.assertEqual((self.tools / "compose.patcher.yaml").read_text(), "managed override\n")
        self.assertNotIn("Next:", result.stdout)

    def test_release_follows_detected_version(self):
        self.publish("v0.108.0-b.1")
        result = self.run_bootstrap("native", AGH_DIR=str(self.agh_dir),
                                    FAKE_VERSION_OUTPUT="AdGuard Home, version v0.108.0-b.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_urls("v0.108.0-b.1")

    def test_docker_detects_version_in_container_and_hands_off_names(self):
        for args in (("agh-container",), ("agh-container", "dns")):
            with self.subTest(args=args):
                (self.log / "docker").unlink(missing_ok=True)
                result = self.run_bootstrap("docker", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.logged("docker"), ["exec agh-container /opt/adguardhome/AdGuardHome --version"])
                self.assertEqual(self.logged("handoff"), ["docker", "AGH_DIR=unset", *args])
                self.assertIn("docker installer output", result.stdout)
                self.assertIn("Next: apply the managed override", result.stdout)

    def test_invalid_usage_fails_before_any_action(self):
        for args in ((), ("upgrade",), ("docker",), ("native", "extra"), ("docker", "a", "b", "c")):
            with self.subTest(args=args):
                result = self.run_bootstrap(*args)
                self.assert_failed(result, "usage: install.sh native", status=2)
        for args, message in ((("docker", "-x"), "invalid Docker container name"),
                              (("docker", "a/b"), "invalid Docker container name"),
                              (("docker", "agh-container", "dns;id"), "invalid Compose service name")):
            with self.subTest(args=args):
                self.assert_failed(self.run_bootstrap(*args), message)
        self.assertEqual(self.logged("docker") + self.logged("urls"), [])

    def test_environment_and_detection_failures_stop_before_download(self):
        cases = ((("native",), {"FAKE_UID": "1000"}, "run as root"),
                 (("native",), {"AGH_DIR": str(self.root / "missing")}, "AdGuard Home binary not found"),
                 (("native",), {"AGH_DIR": ""}, "AGH_DIR is set but empty"),
                 (("docker", "stopped"), {}, "cannot read the AdGuard Home version from running Docker container stopped"))
        for args, env, message in cases:
            with self.subTest(message=message):
                self.assert_failed(self.run_bootstrap(*args, **env), message)
        for output in ("AdGuard Home, version 0.107.79", "AdGuard Home, version v0.107.79;x", "AdGuard Home, version v0.107.79,",
                       "AdGuard Home, subversion v0.107.79", ""):
            with self.subTest(output=output):
                result = self.run_bootstrap("native", AGH_DIR=str(self.agh_dir), FAKE_VERSION_OUTPUT=output)
                self.assert_failed(result, "could not determine a valid installed AdGuard Home version")
        self.assertEqual(self.logged("urls"), [])

    def test_missing_dependencies_fail_before_download(self):
        for missing, mode in (("curl", ("native",)), ("sha256sum", ("native",)), ("docker", ("docker", "agh-container"))):
            with self.subTest(missing=missing):
                (self.bin / missing).rename(self.root / missing)
                result = self.run_bootstrap(*mode, AGH_DIR=str(self.agh_dir))
                (self.root / missing).rename(self.bin / missing)
                self.assert_failed(result, "required command not found: " + missing)
        self.assertEqual(self.logged("urls"), [])

    def test_unpublished_release_fails_without_installing(self):
        result = self.run_bootstrap("native", AGH_DIR=str(self.agh_dir),
                                    FAKE_VERSION_OUTPUT="AdGuard Home, version v0.107.80")
        self.assert_failed(result, "download failed")
        self.assertEqual(len(self.logged("urls")), 1)

    def test_checksum_mismatch_prevents_extraction_and_handoff(self):
        shutil.rmtree(self.web)
        self.publish("v0.107.79", checksum="0" * 64 + "  " + ASSET + "\n")
        self.assert_failed(self.run_bootstrap("docker", "agh-container"), "checksum mismatch")
        shutil.rmtree(self.web)
        self.publish("v0.107.79", checksum=hashlib.sha256(bundle()).hexdigest() + "  other.tar.gz\n")
        self.assert_failed(self.run_bootstrap("docker", "agh-container"), "invalid published checksum file")

    def test_extraction_failure_prevents_handoff(self):
        shutil.rmtree(self.web)
        self.publish("v0.107.79", payload=b"not a tar archive")
        self.assert_failed(self.run_bootstrap("native", AGH_DIR=str(self.agh_dir)), "extraction into")

    def test_released_installer_failure_is_reported(self):
        result = self.run_bootstrap("docker", "agh-container", "dns", HANDOFF_STATUS="3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("released installer failed", result.stderr)
        self.assertNotIn("Next:", result.stdout)


if __name__ == "__main__":
    unittest.main()
