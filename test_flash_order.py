#!/usr/bin/env python3
"""刷写顺序 (MCU -> 大包 -> 小包) 的 mock 单测 (stdlib unittest)。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flasher


class TestSortJobs(unittest.TestCase):
    def test_mcu_before_big_before_small(self):
        jobs = [("big", "xp5_a", "/p/X", False),
                ("small", "xp5_a", "/p/x", False),
                ("mcu", "mcu", "/p/A", False)]
        out = flasher.sort_jobs(jobs)
        self.assertEqual([j[0] for j in out], ["mcu", "big", "small"])

    def test_vlmu_mcu_grouped_with_mcu_first(self):
        jobs = [("big", "vlmu", "/p/V", False),
                ("vlmu_mcu", "vlmu", "/p/VM", False),
                ("small", "vlmu", "/p/v", False),
                ("mcu", "mcu", "/p/A", False)]
        out = flasher.sort_jobs(jobs)
        self.assertEqual([j[0] for j in out], ["vlmu_mcu", "mcu", "big", "small"])

    def test_stable_within_same_kind(self):
        # 同类保持输入相对顺序 (xp5_a 在 vlmu 前)
        jobs = [("big", "xp5_a", "/p/1", False),
                ("big", "vlmu", "/p/2", False),
                ("small", "xp5_a", "/p/3", False),
                ("small", "vlmu", "/p/4", False)]
        out = flasher.sort_jobs(jobs)
        self.assertEqual([(j[0], j[1]) for j in out],
                         [("big", "xp5_a"), ("big", "vlmu"),
                          ("small", "xp5_a"), ("small", "vlmu")])

    def test_unknown_kind_goes_last(self):
        jobs = [("weird", "t", "/p", False), ("mcu", "mcu", "/p/A", False)]
        out = flasher.sort_jobs(jobs)
        self.assertEqual([j[0] for j in out], ["mcu", "weird"])


class TestPlanJobsOrder(unittest.TestCase):
    def test_plan_jobs_mcu_first(self):
        names = {"mcu": "A.tar.gz", "xp5_big": "X.tar.gz", "xp5_small": "x.tar.gz"}
        with mock.patch("common.scan_dir", return_value={}), \
             mock.patch("common.newest_pkg", side_effect=lambda cats, c: names.get(c)):
            jobs = flasher.plan_jobs("/prep", ["xp5_a", "mcu"])
        self.assertEqual([j[0] for j in jobs], ["mcu", "big", "small"])
        self.assertEqual([j[1] for j in jobs], ["mcu", "xp5_a", "xp5_a"])

    def test_plan_jobs_multiple_targets(self):
        names = {"mcu": "A.tar.gz", "xp5_big": "X.tar.gz", "xp5_small": "x.tar.gz",
                 "vlmu_big": "V.tar.gz", "vlmu_small": "v.tar.gz"}
        with mock.patch("common.scan_dir", return_value={}), \
             mock.patch("common.newest_pkg", side_effect=lambda cats, c: names.get(c)):
            jobs = flasher.plan_jobs("/prep", ["vlmu", "xp5_a", "xp5_b", "mcu"])
        kinds = [j[0] for j in jobs]
        # mcu 最先, 其后全 big, 最后全 small
        self.assertEqual(kinds[0], "mcu")
        self.assertEqual(kinds, ["mcu"] + ["big"] * 3 + ["small"] * 3)


if __name__ == "__main__":
    unittest.main()