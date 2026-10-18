# SPDX-License-Identifier: GPL-2.0-only
import hashlib
import gc
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lab_io import Evidence, acquire, digest, extract_archive, tree_digest


class LabIoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nss-lab-io-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, names):
        path = self.root / "source.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            for name, contents in names:
                member = tarfile.TarInfo(name)
                member.size = len(contents)
                tar.addfile(member, io.BytesIO(contents))
        return path

    def test_extracts_one_root_and_preserves_contents(self):
        archive = self.archive([("root/a/b.txt", b"content\n")])
        output = self.root / "output"
        extract_archive(archive, output)
        self.assertEqual((output / "a/b.txt").read_bytes(), b"content\n")

    @unittest.skipUnless(shutil.which("zstd"), "requires the actual zstd executable")
    def test_zstd_archive_can_be_a_read_only_cache_symlink(self):
        raw = self.root / "source.tar"
        with tarfile.open(raw, "w") as tar:
            member = tarfile.TarInfo("root/data")
            member.size = 7
            tar.addfile(member, io.BytesIO(b"cached\n"))
        cached = self.root / "cache-object"
        subprocess.run(["zstd", "-q", str(raw), "-o", str(cached)], check=True)
        cached.chmod(0o444)
        original = cached.read_bytes()
        link = self.root / "input.tar.zst"
        link.symlink_to(cached)
        output = self.root / "output"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            extract_archive(link, output)
            gc.collect()
        self.assertFalse([item for item in caught if item.category is ResourceWarning])
        self.assertEqual((output / "data").read_bytes(), b"cached\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual(cached.read_bytes(), original)
        self.assertEqual(cached.stat().st_mode & 0o777, 0o444)

    def test_rejects_path_traversal_without_escape(self):
        archive = self.archive([("root/../escape", b"bad")])
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            extract_archive(archive, self.root / "output")
        self.assertFalse((self.root / "escape").exists())

    def test_rejects_absolute_archive_path(self):
        archive = self.archive([("/absolute/path", b"bad")])
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            extract_archive(archive, self.root / "output")

    def test_rejects_mixed_archive_roots(self):
        archive = self.archive([("one/a", b"a"), ("two/b", b"b")])
        with self.assertRaisesRegex(ValueError, "multiple roots"):
            extract_archive(archive, self.root / "output")

    def test_rejects_symlink_escape(self):
        archive = self.root / "symlink.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            link = tarfile.TarInfo("root/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../escape"
            tar.addfile(link)
        with self.assertRaises(tarfile.FilterError):
            extract_archive(archive, self.root / "output")
        self.assertFalse((self.root / "escape").exists())

    def test_cache_is_read_only_and_bad_cache_is_rejected(self):
        cache = self.root / "cache"
        cache.mkdir()
        value = b"pinned archive fixture"
        expected = hashlib.sha256(value).hexdigest()
        file = cache / expected
        file.write_bytes(value)
        workspace = self.root / "work"
        workspace.mkdir()
        spec = {"url": "https://example.invalid/unreachable", "sha256": expected}
        output = acquire(spec, "fixture.tar.gz", workspace, Evidence(workspace), cache)
        self.assertEqual(digest(output), expected)
        self.assertEqual(file.read_bytes(), value)
        file.write_bytes(b"corrupt")
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "cache hash mismatch"):
            acquire(spec, "fixture.tar.gz", other, Evidence(other), cache)
        self.assertEqual(file.read_bytes(), b"corrupt")

    def sdk_archive(self, target="/builder/python", name="staging_dir/host/bin/python"):
        archive = self.root / "sdk.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            link = tarfile.TarInfo("sdk/" + name)
            link.type = tarfile.SYMTYPE
            link.linkname = target
            tar.addfile(link)
        return archive

    def test_absolute_links_remain_rejected_by_default(self):
        with self.assertRaises(tarfile.FilterError):
            extract_archive(self.sdk_archive(), self.root / "output")

    def test_exact_sdk_host_link_is_omitted_and_recorded(self):
        expected = {"staging_dir/host/bin/python": "/builder/python"}
        output = self.root / "output"
        result = extract_archive(self.sdk_archive(), output, expected)
        self.assertEqual(result, expected)
        self.assertFalse((output / "staging_dir/host/bin/python").is_symlink())

    def test_changed_sdk_host_link_or_missing_pin_is_rejected(self):
        expected = {"staging_dir/host/bin/python": "/builder/python"}
        with self.assertRaisesRegex(ValueError, "pin mismatch"):
            extract_archive(self.sdk_archive("/other/python"), self.root / "changed", expected)
        with self.assertRaisesRegex(ValueError, "missing pinned"):
            extract_archive(self.archive([("root/a", b"file")]), self.root / "missing", expected)

    def test_allowlist_cannot_extend_beyond_sdk_host_commands(self):
        with self.assertRaisesRegex(ValueError, "Invalid SDK host-link pin"):
            extract_archive(
                self.sdk_archive(),
                self.root / "bad-pin",
                {"etc/config": "/builder/file"},
            )
        with self.assertRaises(tarfile.FilterError):
            extract_archive(
                self.sdk_archive(name="elsewhere/python"),
                self.root / "unknown",
                {"staging_dir/host/bin/python": "/builder/python"},
            )

    def test_source_fingerprint_detects_content_mode_and_symlink_changes(self):
        folder = self.root / "input"
        folder.mkdir()
        file = folder / "file"
        file.write_text("original")
        initial = tree_digest(folder)
        file.write_text("changed")
        self.assertNotEqual(tree_digest(folder), initial)
        file.write_text("original")
        self.assertEqual(tree_digest(folder), initial)
        file.chmod(0o755)
        self.assertNotEqual(tree_digest(folder), initial)
        file.chmod(0o644)
        (folder / "link").symlink_to("file")
        linked = tree_digest(folder)
        (folder / "link").unlink()
        (folder / "link").symlink_to("other")
        self.assertNotEqual(tree_digest(folder), linked)

    def test_failed_command_log_survives_next_attempt_and_tampering_is_rejected(self):
        evidence = Evidence(self.root)
        with self.assertRaisesRegex(RuntimeError, "failed"):
            evidence.run(
                "probe",
                [
                    sys.executable,
                    "-c",
                    'print("original failure"); raise SystemExit(3)',
                ],
            )
        failed = self.root / evidence.records[0]["log"]
        original = failed.read_bytes()
        evidence.run("probe", [sys.executable, "-c", 'print("success")'])
        self.assertEqual(failed.read_bytes(), original)
        self.assertEqual([r["exit"] for r in evidence.records], [3, 0])
        self.assertNotEqual(evidence.records[0]["log"], evidence.records[1]["log"])
        failed.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            Evidence(self.root)


if __name__ == "__main__":
    unittest.main()
