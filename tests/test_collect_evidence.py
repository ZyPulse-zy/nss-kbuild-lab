# SPDX-License-Identifier: GPL-2.0-only
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from collect_evidence import collect


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nss-collection-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "workspace"
        self.work.mkdir()
        (self.work / "result.json").write_text('{"passed":false}')
        (self.work / "commands.json").write_text("[]")

    def test_export_excludes_unrecognized_json_and_hidden_context(self):
        (self.work / "credentials.json").write_text('{"private":"fixture"}')
        (self.work / ".nss-kbuild-lab.json").write_text('{"marker":"fixture"}')
        (self.work / "source-proof.json").write_text('{"public":true}')
        output = self.root / "evidence"
        self.assertEqual(collect(self.work, output), 0)
        self.assertFalse((output / "credentials.json").exists())
        self.assertFalse((output / ".nss-kbuild-lab.json").exists())
        self.assertTrue((output / "source-proof.json").is_file())
        for line in (output / "SHA256SUMS").read_text().splitlines():
            expected, name = line.split("  ", 1)
            self.assertFalse(Path(name).name.startswith("."))
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), expected)

    def test_changed_module_phase_log_is_rejected_before_export(self):
        module = self.work / "modules"
        module.mkdir()
        log = module / "compile.log"
        original = b"actual compiler failure\n"
        log.write_bytes(original)
        (module / "result.json").write_text(
            json.dumps(
                {
                    "passed": False,
                    "phases": [
                        {
                            "name": "compile",
                            "log": "compile.log",
                            "log_sha256": hashlib.sha256(original).hexdigest(),
                        }
                    ],
                }
            )
        )
        log.write_bytes(b"edited output\n")
        with self.assertRaisesRegex(ValueError, "Module command log changed"):
            collect(self.work, self.root / "evidence")


if __name__ == "__main__":
    unittest.main()
