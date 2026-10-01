"""Regression checks for whitespace validation of staged and unstaged source."""

import pathlib
import subprocess
import tempfile
import unittest


CHECK = pathlib.Path(__file__).resolve().parents[1] / "check-applied-whitespace.sh"


class AppliedSourceWhitespace(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Validation test")
        self.git("config", "user.email", "validation@example.invalid")
        (self.root / ".gitattributes").write_text("patch/*.patch -whitespace\n")
        (self.root / "source.ts").write_text("const value = 1;\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def check_source(self):
        return subprocess.run(
            ["bash", str(CHECK), str(self.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).returncode

    def test_clean_applied_source_and_patch_context_pass(self):
        (self.root / "source.ts").write_text("const value = 2;\n")
        self.git("add", "source.ts")
        (self.root / "patch").mkdir()
        (self.root / "patch/change.patch").write_text("diff --git a/source.ts b/source.ts\n \n")
        self.git("add", "patch")
        self.assertEqual(self.check_source(), 0)

    def test_three_way_staged_malformed_source_is_rejected(self):
        # The artifact itself is exempt, but its added source contains trailing spaces.
        artifact = self.root / "patch/change.patch"
        artifact.parent.mkdir()
        artifact.write_text(
            "diff --git a/source.ts b/source.ts\n"
            "--- a/source.ts\n+++ b/source.ts\n@@ -1 +1 @@\n"
            "-const value = 1;\n+const value = 2;   \n"
        )
        self.git("add", "patch")
        self.git("commit", "-qm", "exempt patch artifact")
        self.git("apply", "--3way", "--whitespace=nowarn", str(artifact))
        self.assertEqual(self.git("diff").stdout, b"")
        self.assertNotEqual(self.check_source(), 0)

    def test_unstaged_malformed_source_is_rejected(self):
        (self.root / "source.ts").write_text("const value = 2;   \n")
        self.assertNotEqual(self.check_source(), 0)

    def test_new_staged_file_with_extra_blank_eof_is_rejected(self):
        (self.root / "added.ts").write_text("const added = true;\n\n")
        self.git("add", "added.ts")
        self.assertNotEqual(self.check_source(), 0)


if __name__ == "__main__":
    unittest.main()
