#!/usr/bin/env python3
"""刷机进度指示的 mock 单测 (stdlib unittest)。

覆盖三层:
- flasher.execute_jobs 的 progress 回调事件序列 (task/phase/done/skip, 失败即停,
  progress=None 向后兼容);
- flash_gui._flash_phase 里程碑解析真值表 (各 kind + VLMU 百分比 + 未知行);
- headless GUI 队列落地 smoke (进度条 maximum/value, 阶段文字, 显示/隐藏)。

本机无板子、无显示环境: execute_jobs 用 mock 设备 + mock _run_job; GUI 用
withdrawn root, 不弹真窗。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flasher
import flash_gui


def _quiet(*_a, **_k):
    pass


# ---------------------------------------------------------------- execute_jobs 事件
class TestExecuteJobsProgress(unittest.TestCase):
    DEVS = [("SER_A", "SoC_A", "adb"),
            ("SER_B", "SoC_B", "adb"),
            ("SER_V", "SoC_VLMU", "adb")]

    def _run(self, jobs, run_ok=True, progress=None, devs=None):
        events = []
        cb = progress if progress is not None else (lambda ev: events.append(ev))
        with mock.patch("common.detect_devices", return_value=devs or self.DEVS), \
             mock.patch.object(flasher, "REBOOT_WAIT_SEC", 0), \
             mock.patch.object(flasher, "REBOOT_POLL_SEC", 0), \
             mock.patch.object(flasher, "_run_job", return_value=run_ok) as rj, \
             mock.patch.object(flasher, "_run_job_extracted", return_value=run_ok):
            ok = flasher.execute_jobs(jobs, log=_quiet, progress=cb)
        return ok, events, rj

    def test_happy_path_event_sequence(self):
        jobs = [("big", "xp5_a", "/p/X.tar.gz", False),
                ("small", "xp5_a", "/p/xpilot.tar.gz", False),
                ("mcu", "mcu", "/p/A.tar.gz", False),
                ("vlmu_mcu", "vlmu", "/p/V.zip", False)]
        ok, ev, _ = self._run(jobs)
        self.assertTrue(ok)
        tasks = [e for e in ev if e["type"] == "task"]
        dones = [e for e in ev if e["type"] == "done"]
        self.assertEqual([t["index"] for t in tasks], [1, 2, 3, 4])
        self.assertTrue(all(t["total"] == 4 for t in tasks))
        self.assertEqual([t["kind"] for t in tasks],
                         ["big", "small", "mcu", "vlmu_mcu"])
        self.assertEqual([t["target"] for t in tasks],
                         ["xp5_a", "xp5_a", "mcu", "vlmu"])
        self.assertEqual([t["name"] for t in tasks],
                         ["X.tar.gz", "xpilot.tar.gz", "A.tar.gz", "V.zip"])
        self.assertEqual([d["index"] for d in dones], [1, 2, 3, 4])
        self.assertTrue(all(d["ok"] for d in dones))
        # task 与 done 交替出现 (每个任务先 task 后 done)
        kinds = [e["type"] for e in ev if e["type"] in ("task", "done")]
        self.assertEqual(kinds, ["task", "done"] * 4)

    def test_failure_stops_after_done_false(self):
        jobs = [("big", "xp5_a", "/p/X.tar.gz", False),
                ("small", "xp5_a", "/p/xpilot.tar.gz", False)]
        ok, ev, rj = self._run(jobs, run_ok=False)
        self.assertFalse(ok)
        self.assertEqual(rj.call_count, 1)          # 失败即停, 第二个没跑
        tasks = [e for e in ev if e["type"] == "task"]
        dones = [e for e in ev if e["type"] == "done"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(dones, [{"type": "done", "index": 1, "ok": False}])

    def test_skip_emits_task_skipped_and_done(self):
        # fastboot-only 的 SoC_A 不能刷 mcu -> 跳过
        jobs = [("mcu", "mcu", "/p/A.tar.gz", False)]
        ok, ev, rj = self._run(jobs, devs=[("FB_A", "SoC_A", "fastboot")])
        self.assertFalse(ok)                        # 全跳过判失败
        rj.assert_not_called()
        tasks = [e for e in ev if e["type"] == "task"]
        dones = [e for e in ev if e["type"] == "done"]
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0].get("skipped"))
        self.assertEqual(tasks[0]["index"], 1)
        self.assertEqual(dones, [{"type": "done", "index": 1, "ok": True}])

    def test_progress_none_backward_compatible(self):
        jobs = [("mcu", "mcu", "/p/A.tar.gz", False)]
        # progress=None 时不应抛错, 结果照常
        with mock.patch("common.detect_devices", return_value=self.DEVS), \
             mock.patch.object(flasher, "_run_job", return_value=True):
            results = []
            ok = flasher.execute_jobs(jobs, log=_quiet, results_out=results)
        self.assertTrue(ok)
        self.assertEqual(results[0]["status"], "ok")

    def test_extracted_job_emits_events(self):
        jobs = [("mcu", "mcu", "/d/extracted", True)]
        ok, ev, _ = self._run(jobs)
        self.assertTrue(ok)
        tasks = [e for e in ev if e["type"] == "task"]
        self.assertEqual(tasks[0]["name"], "extracted")
        self.assertEqual(tasks[0]["kind"], "mcu")


# ---------------------------------------------------------------- 阶段解析真值表
class TestFlashPhase(unittest.TestCase):
    def test_big_milestones(self):
        cases = [
            ("waiting for device to enter fastboot mode... at t", "进入 fastboot 中"),
            ("board_type: A vehicle_did: 0x00000003", "已进入 fastboot"),
            ("fastboot_flashall at 2026", "刷 norflash/ufs 分区"),
            ("Sending 'xpilot_a'", "刷写分区中"),
            ("Flashing norflash and ufs finished successfully !!!", "norflash/ufs 完成"),
            ("Rebooting", "重启中"),
            ("Waiting 20s for device to boot...", "等待开机"),
            ("flash all done and fastboot continue at t", "大包完成"),
        ]
        for line, want in cases:
            self.assertEqual(flash_gui._flash_phase("big", line), want, line)

    def test_big_finished_before_generic_flashing(self):
        # "Flashing norflash ... finished" 含 "Flashing ", 必须命中更具体的完成态
        self.assertEqual(
            flash_gui._flash_phase("big", "Flashing norflash and ufs finished successfully"),
            "norflash/ufs 完成")

    def test_small_milestones(self):
        cases = [
            ("start to scp xp5 package", "推送文件中"),
            ("./xpilot/: 12584 files pushed", "推送文件中"),
            ("type is xp5_vlmu, copy libs to /xpilot/libs", "推送 libs"),
            ("begin reboot xpu process...", "重启中"),
            ("===== Copying SW apps Done =====", "小包完成"),
        ]
        for line, want in cases:
            self.assertEqual(flash_gui._flash_phase("small", line), want, line)

    def test_mcu_milestones(self):
        cases = [
            ("pushing E_APPA.hex 和 E_APPB.hex 到 /xdata/latest_aurix/", "推送 hex"),
            ("Currently Running on Zone: B", "读运行区"),
            ("Currently Flashing Zone: A", "刷 A 区"),
            ("Currently Flashing Zone: B", "刷 B 区"),
            ("aurix master upgrade success.", "该区刷写成功"),
            ("device SER offline, waiting for it to come back...", "等待设备重启"),
            ("Switch failed! Cannot Start Flash!", "切区失败"),
        ]
        for line, want in cases:
            self.assertEqual(flash_gui._flash_phase("mcu", line), want, line)

    def test_vlmu_percent(self):
        self.assertEqual(
            flash_gui._flash_phase("vlmu_mcu", "[INFO] 5s: Current upgrade progress 60%"),
            "OTA 升级 60%")
        self.assertEqual(
            flash_gui._flash_phase("vlmu_mcu", "[INFO] 9s: Upgrade progress 100% Success"),
            "OTA 升级 100%")

    def test_vlmu_milestones(self):
        cases = [
            ("[INFO] Step 1: killall -9 update_service", "准备升级"),
            ("[INFO] Step 3: Running update_service", "启动升级"),
            ("[INFO] Step 4: Monitoring upgrade progress", "监控升级进度"),
            ("[INFO] 12s: SwitchReboot", "升级完成, 重启中"),
            ("[OK] Upgrade completed successfully at 12s", "OTA 完成"),
            ("Fail!!!", "升级失败"),
        ]
        for line, want in cases:
            self.assertEqual(flash_gui._flash_phase("vlmu_mcu", line), want, line)

    def test_generic_extract(self):
        for kind in ("big", "small", "mcu", "vlmu_mcu"):
            self.assertEqual(flash_gui._flash_phase(kind, "[OK] ==> Extracting X.tar.gz"),
                             "解压中")
            self.assertEqual(flash_gui._flash_phase(kind, "[INFO] already extracted, reuse: /d"),
                             "复用已解压")

    def test_unknown_line_returns_empty(self):
        for kind in ("big", "small", "mcu", "vlmu_mcu", ""):
            self.assertEqual(flash_gui._flash_phase(kind, "some random vendor noise"), "")
        self.assertEqual(flash_gui._flash_phase("big", ""), "")

    def test_cross_kind_no_false_match(self):
        # 解析按当前 kind 选表: big 的 fastboot 标记不应被 small/mcu 表命中
        self.assertEqual(flash_gui._flash_phase("small", "fastboot_flashall at 2026"), "")
        self.assertEqual(flash_gui._flash_phase("mcu", "board_type: A vehicle_did: 0x3"), "")


# ---------------------------------------------------------------- headless GUI smoke
class TestGuiProgressSmoke(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.tk = tk
        for m in ("refresh_devices", "_reload_dir_suggestions", "_poll",
                  "_auto_refresh_tick"):
            setattr(flash_gui.App, m, lambda self, *a, **k: None)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = flash_gui.App(self.root)

    def tearDown(self):
        if getattr(self.app, "_log_fh", None):
            try:
                self.app._log_fh.close()
            except (OSError, ValueError):
                pass
            self.app._log_fh = None
        self.root.destroy()

    def _drain(self):
        self.app._poll_once()

    def test_start_task_phase_done_hide(self):
        a = self.app
        a._q.put(("flashprog", {"type": "start", "total": 3}))
        a._q.put(("flashprog", {"type": "task", "index": 1, "total": 3,
                                "kind": "big", "target": "xp5_a", "name": "X.tar.gz"}))
        self._drain()
        self.assertTrue(a._flash_active)
        self.assertEqual(a._flash_bar.cget("maximum"), 3.0)
        self.assertEqual(a._flash_bar.cget("value"), 0.0)
        self.assertIn("任务 1/3", a._flash_step_var.get())
        self.assertIn("大包", a._flash_step_var.get())
        self.assertEqual(a._flash_prog_frame.winfo_manager(), "pack")

        # 经 _job_log 喂里程碑行 -> 阶段事件 -> 文字更新
        a._job_log("[INFO] Flashing norflash slot a")
        self._drain()
        self.assertIn("刷写分区中", a._flash_step_var.get())
        self.assertIn("任务 1/3", a._flash_step_var.get())   # 前缀保留

        a._q.put(("flashprog", {"type": "done", "index": 1, "ok": True}))
        self._drain()
        self.assertEqual(a._flash_bar.cget("value"), 1.0)

        a._q.put(("flashprog", {"type": "task", "index": 2, "total": 3,
                                "kind": "mcu", "target": "mcu", "name": "A.tar.gz"}))
        self._drain()
        self.assertEqual(a._flash_bar.cget("value"), 1.0)   # idx-1
        self.assertIn("任务 2/3", a._flash_step_var.get())
        self.assertIn("MCU", a._flash_step_var.get())

        # 结束 -> 隐藏 + 复位
        a._set_busy(False)
        self.assertFalse(a._flash_active)
        self.assertEqual(a._flash_prog_frame.winfo_manager(), "")
        self.assertEqual(a._flash_bar.cget("value"), 0.0)

    def test_skip_advances_bar(self):
        a = self.app
        a._q.put(("flashprog", {"type": "start", "total": 2}))
        a._q.put(("flashprog", {"type": "task", "index": 1, "total": 2,
                                "kind": "mcu", "target": "mcu", "name": "A.tar.gz",
                                "skipped": True}))
        self._drain()
        self.assertEqual(a._flash_bar.cget("value"), 1.0)
        self.assertIn("跳过", a._flash_step_var.get())

    def test_idle_log_does_not_emit_phase(self):
        a = self.app
        self.assertFalse(a._flash_active)
        a._job_log("[INFO] Flashing norflash slot a")
        # 未刷机时不应产生 flashprog 事件
        kinds = []
        try:
            while True:
                k, _ = a._q.get_nowait()
                kinds.append(k)
        except Exception:
            pass
        self.assertNotIn("flashprog", kinds)


if __name__ == "__main__":
    unittest.main()