# SPDX-License-Identifier: GPL-2.0-only
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from lab import verify_target_configuration


class TargetConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((ROOT / "pins/ipq807x.json").read_text())
        self.configuration = (ROOT / self.manifest["config"]).read_text()
        self.configuration += 'CONFIG_GCC_VERSION="14.3.0"\n'

    def test_both_pinned_public_profiles_select_the_expected_device(self):
        for profile in ("ipq60xx", "ipq807x"):
            manifest = json.loads((ROOT / "pins" / (profile + ".json")).read_text())
            config = (ROOT / manifest["config"]).read_text()
            selected = verify_target_configuration(
                manifest, config + 'CONFIG_GCC_VERSION="14.3.0"\n'
            )
            self.assertIn("_DEVICE_" + manifest["target"]["device"] + "=y", selected[-1])

    def test_default_device_fallback_is_rejected(self):
        config = self.configuration.replace("DEVICE_dynalink_dl-wrx36=y", "DEVICE_other=y")
        with self.assertRaisesRegex(ValueError, "DEVICE_dynalink_dl-wrx36"):
            verify_target_configuration(self.manifest, config)

    def test_subtarget_fallback_is_rejected(self):
        config = self.configuration.replace(
            "CONFIG_TARGET_qualcommax_ipq807x=y", "CONFIG_TARGET_qualcommax_ipq60xx=y"
        )
        with self.assertRaisesRegex(ValueError, "ipq807x=y"):
            verify_target_configuration(self.manifest, config)

    def test_disabled_nss_option_in_comment_is_rejected(self):
        config = self.configuration.replace(
            "CONFIG_ATH11K_NSS_SUPPORT=y", "# CONFIG_ATH11K_NSS_SUPPORT=y"
        )
        with self.assertRaisesRegex(ValueError, "ATH11K_NSS_SUPPORT"):
            verify_target_configuration(self.manifest, config)

    def test_wrong_toolchain_is_rejected(self):
        config = self.configuration.replace(
            'CONFIG_GCC_VERSION="14.3.0"', 'CONFIG_GCC_VERSION="14.4.0"'
        )
        with self.assertRaisesRegex(ValueError, "GCC_VERSION"):
            verify_target_configuration(self.manifest, config)


if __name__ == "__main__":
    unittest.main()
