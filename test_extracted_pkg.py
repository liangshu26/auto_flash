#!/usr/bin/env python3
"""已解压刷机包识别的 mock 单测 (stdlib unittest)。

重点守 MCU 包: hex 嵌套在 XPF/<build>/ 下, 顶层 markers 看不到, 曾被漏判
(导致解压后的 XPilot_AURIX_* 目录在刷机列表里识别不出来)。同时回归大包/小包/
VLMU MCU 的顶层标记识别与非包目录排除。
"""
import os
import shutil
import tempfile
import unittest

import common


class TestExtractedPackage(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _touch(self, rel):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("")
        return p

    def test_mcu_nested_hex_recognized(self):
        pkg = "XPilot_AURIX_E38BE_ENG_DEV_1.0.0-e38be-xos645-xp5-ultra-rc6"
        self._touch(f"{pkg}/XPF/E38BE000000000100_20261115_213440/E38BE000000000100_APPA.hex")
        self._touch(f"{pkg}/XPF/E38BE000000000100_20261115_213440/E38BE000000000100_APPB.hex")
        full = os.path.join(self.root, pkg)
        self.assertTrue(common._is_extracted_package(full))
        self.assertEqual([n for n, _ in common.scan_extracted_dir(self.root)["mcu"]], [pkg])

    def test_mcu_top_level_hex_still_recognized(self):
        pkg = "XPilot_AURIX_X"
        self._touch(f"{pkg}/E100_APPA.hex")
        self._touch(f"{pkg}/E100_APPB.hex")
        self.assertTrue(common._is_extracted_package(os.path.join(self.root, pkg)))

    def test_mcu_appb_only_recognized(self):
        pkg = "XPilot_AURIX_Y"
        self._touch(f"{pkg}/XPF/build/E100_APPB.hex")
        self.assertTrue(common._is_extracted_package(os.path.join(self.root, pkg)))

    def test_big_small_vlmu_markers(self):
        cases = {
            "XCCP_big": ("flash_tools/adc_flashall.sh", "xp5_big"),
            "xpilot_small": ("deploy_xpilot_by_adb.sh", "xp5_small"),
            "E29_VLMU_x": ("OTAPackage/VLMUMCU_x.zip", "vlmu_mcu"),
        }
        for name, (rel, cat) in cases.items():
            self._touch(f"{name}/{rel}")
            full = os.path.join(self.root, name)
            self.assertTrue(common._is_extracted_package(full), name)
            self.assertIn(name, [n for n, _ in common.scan_extracted_dir(self.root)[cat]], name)

    def test_non_package_dir_excluded(self):
        self._touch("random/sub/file.txt")
        self.assertFalse(common._is_extracted_package(os.path.join(self.root, "random")))

    def test_nested_hex_depth_limit(self):
        # 超过 max_depth(3) 的 hex 不应被搜到 (避免误扫无关深目录)
        pkg = "XPilot_AURIX_deep"
        self._touch(f"{pkg}/a/b/c/d/E100_APPA.hex")   # 深度 4 > 3
        self.assertFalse(common._has_nested_mcu_hex(os.path.join(self.root, pkg), max_depth=3))

    def test_missing_dir_returns_false(self):
        self.assertFalse(common._is_extracted_package(os.path.join(self.root, "nope")))


if __name__ == "__main__":
    unittest.main()