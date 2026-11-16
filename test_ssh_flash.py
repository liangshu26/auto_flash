#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ssh 刷写 feature 的 mock 单测 (stdlib unittest)。

本机无板子、台架 ssh IP 不可达, 全部用 mock 验证:
  - adb 路径 argv 序列逐字不变 (主回归闸, 重构前录制)
  - ssh/scp argv 与 common._ssh_reachable 选项 parity
  - MCU / VLMU ssh 各成败场景与安全闸
  - 分发 (mixed snapshot / use_ssh 透传 / fastboot 跳过)
  - dry-run 文案与零 subprocess 调用

运行: python3 -m unittest discover -p "test_*.py"
"""

import os
import re
import types
import tempfile
import unittest
from unittest import mock

import common
import transport
import mcu
import flasher

# 在任何 mock 安装前抓住真实实现, 供选项 parity 测试对照
_REAL_SSH_REACHABLE = common._ssh_reachable


# ---------------------------------------------------------------- mock 台架
class FakeProc:
    """模拟 spawn 出来的远端升级进程。poll() 前 exits_after 次返回 None (仍在跑)。"""
    def __init__(self, rc=0, exits_after=None):
        self.returncode = rc
        self._exits_after = exits_after
        self._polls = 0

    def poll(self):
        self._polls += 1
        if self._exits_after is None or self._polls <= self._exits_after:
            return None
        return self.returncode


class Board:
    """可配置的假板子, 路由 transport 的 5 个底层入口并记录调用。"""
    def __init__(self):
        self.calls = []                 # (kind, argv)
        self.adb_online = True
        self.ssh_online = True
        self.capture_rules = []         # [(substring, str|callable(cmd)->str)]
        self.capture_default = ""
        self.rc_rules = []              # [(substring, int|callable(argv)->int)]
        self.rc_default = 0
        self.line_rules = []            # [(substring, [lines]|callable(argv)->[lines])]
        self.spawn_rc = 0
        self.spawn_exits_after = None
        self.sleeps = []

    # -- 配置助手
    def capture(self, substr, resp):
        self.capture_rules.append((substr, resp))
        return self

    def rc(self, substr, val):
        self.rc_rules.append((substr, val))
        return self

    def lines(self, substr, ls):
        self.line_rules.append((substr, ls))
        return self

    # -- 路由
    def _capture(self, cmd):
        for substr, resp in self.capture_rules:
            if substr in cmd:
                return resp(cmd) if callable(resp) else resp
        return self.capture_default

    def _rc(self, argv):
        cmd = argv[-1] if argv else ""
        for substr, val in self.rc_rules:
            if substr in cmd:
                return val(argv) if callable(val) else val
        return self.rc_default

    def _lines(self, argv):
        cmd = argv[-1] if argv else ""
        for substr, ls in self.line_rules:
            if substr in cmd:
                return ls(argv) if callable(ls) else ls
        return []

    def fake_run(self, argv, **kw):
        self.calls.append(("run", list(argv)))
        if "get-state" in argv:
            out = "device\n" if self.adb_online else ""
            return types.SimpleNamespace(stdout=out, stderr="", returncode=0)
        cmd = argv[-1] if argv else ""
        return types.SimpleNamespace(stdout=self._capture(cmd), stderr="", returncode=0)

    def fake_stream(self, argv, **kw):
        self.calls.append(("stream", list(argv)))
        log = kw.get("log")
        if log:
            for l in self._lines(argv):
                log(l)
        return self._rc(argv)

    def fake_popen(self, argv, **kw):
        self.calls.append(("popen", list(argv)))
        return FakeProc(self.spawn_rc, self.spawn_exits_after)

    def fake_reachable(self, ip, port=22, timeout=3):
        self.calls.append(("reachable", ip))
        return self.ssh_online

    def argv_of(self, kind):
        return [a for k, a in self.calls if k == kind]

    def stream_cmds(self):
        return [a[-1] for a in self.argv_of("stream")]


class TransportTest(unittest.TestCase):
    """安装全局 mock (subprocess / common / time), 提供 self.board。"""
    def setUp(self):
        self.board = Board()
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup_tmp)
        patchers = [
            mock.patch("subprocess.run", side_effect=self.board.fake_run),
            mock.patch("subprocess.Popen", side_effect=self.board.fake_popen),
            mock.patch("common.run_streaming", side_effect=self.board.fake_stream),
            mock.patch("common._ssh_reachable", side_effect=self.board.fake_reachable),
            mock.patch("common.track_proc", lambda p: None),
            mock.patch("common.untrack_proc", lambda p: None),
            mock.patch("time.sleep", side_effect=lambda s: self.board.sleeps.append(s)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _cleanup_tmp(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def hex_files(self, appa="x_APPA.hex", appb="x_APPB.hex", content_a="a", content_b="b"):
        pa = os.path.join(self._tmp, appa)
        pb = os.path.join(self._tmp, appb)
        with open(pa, "w") as f:
            f.write(content_a)
        with open(pb, "w") as f:
            f.write(content_b)
        return pa, pb

    def quiet(self, *a):
        pass


# ---------------------------------------------------------------- adb golden
class TestAdbGolden(TransportTest):
    """重构前录制的 adb argv 序列, 重构后必须逐字相等 (主回归闸)。"""

    def test_mcu_adb_sequence(self):
        appa, appb = self.hex_files()
        b = self.board
        vn = {"n": 0}

        def zone(cmd):
            vn["n"] += 1
            z = "A" if vn["n"] == 1 else "B"
            return f"Aurix active boot :{z}\n"
        b.capture("aurix_utility -v", zone)

        ok = mcu.flash_mcu(appa, appb, serial="SER_A", slave=False, log=self.quiet)
        self.assertTrue(ok)
        expected = [
            ["adb", "-s", "SER_A", "get-state"],
            ["adb", "-s", "SER_A", "shell", "chmod 777 /xdata"],
            ["adb", "-s", "SER_A", "shell", "rm -rf /xdata/latest_aurix"],
            ["adb", "-s", "SER_A", "shell", "mkdir -p /xdata/latest_aurix"],
            ["adb", "-s", "SER_A", "push", appa, "/xdata/latest_aurix/"],
            ["adb", "-s", "SER_A", "push", appb, "/xdata/latest_aurix/"],
            ["adb", "-s", "SER_A", "shell", "/xpilot/aurix_utility/aurix_utility -v"],
            ["adb", "-s", "SER_A", "shell",
             "/xpilot/aurix_utility/aurix_utility --update=/xdata/latest_aurix/x_APPB.hex,"
             "/xpilot/aurix_utility/FlashDrv/FlashDrv.hex --reboot=1"],
            ["adb", "-s", "SER_A", "get-state"],
            ["adb", "-s", "SER_A", "shell", "/xpilot/aurix_utility/aurix_utility -v"],
            ["adb", "-s", "SER_A", "shell",
             "/xpilot/aurix_utility/aurix_utility --update=/xdata/latest_aurix/x_APPA.hex,"
             "/xpilot/aurix_utility/FlashDrv/FlashDrv.hex --reboot=1"],
        ]
        actual = [a for k, a in b.calls if k in ("run", "stream")]
        self.assertEqual(actual, expected)
        self.assertEqual(b.sleeps, [30, 30])

    def test_vlmu_adb_sequence(self):
        b = self.board
        b.lines("grep 'Current upgrade progress'", ["Current upgrade progress 100%"])
        b.lines("grep -E 'Upgrade progress 100%|FlashExit|SwitchReboot'",
                ["Upgrade progress 100% Success"])
        b.lines("grep -E 'Upgrade progress 100%.*Success|FlashExit.*Success'",
                ["Upgrade progress 100% Success"])
        t = transport.for_device("SER_V")
        ok = flasher._run_vlmu_mcu_upgrade("VLMUMCU_x.zip", t, log=self.quiet)
        self.assertTrue(ok)
        # 临时脚本名随机, 用占位符归一
        norm = []
        for k, a in b.calls:
            aa = [re.sub(r".*/tmp\w+\.sh", "tmpSCRIPT.sh", x) for x in a]
            norm.append((k, aa))
        expected = [
            ("stream", ["adb", "-s", "SER_V", "shell", "killall -9 update_service"]),
            ("stream", ["adb", "-s", "SER_V", "push", "tmpSCRIPT.sh", "/tmp/start_vlmu_mcu.sh"]),
            ("stream", ["adb", "-s", "SER_V", "shell", "chmod +x /tmp/start_vlmu_mcu.sh"]),
            ("stream", ["adb", "-s", "SER_V", "shell", "> /xdata/log/latest/update_service.txt.log"]),
            ("popen", ["adb", "-s", "SER_V", "shell", "/tmp/start_vlmu_mcu.sh"]),
            ("stream", ["adb", "-s", "SER_V", "shell",
                        "grep 'Current upgrade progress' /xdata/log/latest/update_service.txt.log | tail -1"]),
            ("stream", ["adb", "-s", "SER_V", "shell",
                        "grep -E 'Upgrade progress 100%|FlashExit|SwitchReboot' "
                        "/xdata/log/latest/update_service.txt.log | tail -3"]),
            ("stream", ["adb", "-s", "SER_V", "shell",
                        "grep -E 'Upgrade progress 100%.*Success|FlashExit.*Success' "
                        "/xdata/log/latest/update_service.txt.log | tail -1"]),
        ]
        self.assertEqual(norm, expected)


# ---------------------------------------------------------------- argv parity
class TestArgvParity(TransportTest):
    def test_ssh_opts_match_reachable(self):
        # 真跑 common._ssh_reachable, 捕获它构造的 argv, 比对 -o 子序列
        captured = {}

        def grab(argv, **kw):
            captured["argv"] = list(argv)
            return types.SimpleNamespace(returncode=0)
        with mock.patch("subprocess.run", side_effect=grab):
            _REAL_SSH_REACHABLE("172.20.1.22", 22, 3)
        ra = captured["argv"]
        # 提取 _ssh_reachable 的 (-o, value) 对
        reachable_pairs = []
        i = 0
        while i < len(ra):
            if ra[i] == "-o":
                reachable_pairs += ["-o", ra[i + 1]]
                i += 2
            else:
                i += 1
        self.assertEqual(list(transport._ssh_opts(3)), reachable_pairs)
        # 端口与目标也一致
        self.assertEqual(ra[1:3], ["-p", "22"])
        self.assertIn("root@172.20.1.22", ra)

    def test_ssh_shell_argv(self):
        t = transport.SshTransport("172.20.1.22")
        argv = t.shell_argv("echo hi")
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[1:3], ["-p", "22"])
        self.assertEqual(argv[-2], "root@172.20.1.22")
        self.assertEqual(argv[-1], "echo hi")          # cmd 为单个 argv 元素
        self.assertIn("ServerAliveInterval=15", argv)   # 长命令 keepalive
        self.assertIn("ServerAliveCountMax=4", argv)
        # -o 子序列与 _ssh_opts 一致
        self.assertEqual([argv[i] for i in range(len(argv)) if argv[i] == "-o"][:5],
                         ["-o"] * 5)

    def test_ssh_spawn_has_no_tty(self):
        t = transport.SshTransport("172.20.1.119")
        argv = t.spawn_argv("/tmp/start_vlmu_mcu.sh")
        self.assertIn("-T", argv)
        self.assertEqual(argv[-1], "/tmp/start_vlmu_mcu.sh")
        self.assertIn("ServerAliveInterval=15", argv)

    def test_scp_argv_flags(self):
        t = transport.SshTransport("172.20.1.119")
        argv = t.push_argv("/local/V.zip", "/tmp/V.zip")
        self.assertEqual(argv[0], "scp")
        self.assertEqual(argv[1:3], ["-P", "22"])       # 大写 -P
        self.assertIn("-O", argv)                        # 传统 SCP 协议
        self.assertEqual(argv[-2], os.path.abspath("/local/V.zip"))
        self.assertEqual(argv[-1], "root@172.20.1.119:/tmp/V.zip")
        self.assertIn("ServerAliveInterval=15", argv)

    def test_adb_argv(self):
        t = transport.AdbTransport("SER")
        self.assertEqual(t.shell_argv("cmd"), ["adb", "-s", "SER", "shell", "cmd"])
        self.assertEqual(t.push_argv("l", "r"), ["adb", "-s", "SER", "push", "l", "r"])
        self.assertEqual(t.spawn_argv("cmd"), ["adb", "-s", "SER", "shell", "cmd"])

    def test_for_device_selection(self):
        self.assertIsInstance(transport.for_device("SER"), transport.AdbTransport)
        self.assertIsInstance(transport.for_device("SER", use_ssh=True), transport.SshTransport)
        self.assertIsInstance(transport.for_device("172.20.1.22"), transport.SshTransport)
        self.assertIsInstance(transport.for_device("172.20.1.119"), transport.SshTransport)

    def test_drop_rc(self):
        self.assertFalse(transport.AdbTransport("S").is_transport_drop_rc(255))
        self.assertFalse(transport.AdbTransport("S").is_transport_drop_rc(0))
        self.assertTrue(transport.SshTransport("172.20.1.22").is_transport_drop_rc(255))
        self.assertFalse(transport.SshTransport("172.20.1.22").is_transport_drop_rc(0))
        self.assertFalse(transport.SshTransport("172.20.1.22").is_transport_drop_rc(1))


# ---------------------------------------------------------------- 噪声过滤
class TestNoiseFilters(unittest.TestCase):
    def test_adb_noise(self):
        for s in ["error: device offline", "error: device not found",
                  "device offline", "no devices/emulators found"]:
            self.assertTrue(transport.adb_offline_noise(s), s)
        # 真实失败标记/输出不是掉线噪声, 不能被滤掉
        for s in ["Current upgrade progress 100%", "Upgrade progress 100% Success",
                  "Fail!!!", "Aurix active boot :A", "adb: error: failed to copy"]:
            self.assertFalse(transport.adb_offline_noise(s), s)

    def test_ssh_noise(self):
        for s in ["ssh: connect to host 172.20.1.119 port 22: No route to host",
                  "Connection closed by remote host",
                  "kex_exchange_identification: Connection closed by remote host",
                  "Connection reset by peer", "lost connection",
                  "Permission denied (publickey).", "scp: Connection closed",
                  "No route to host", "Connection timed out"]:
            self.assertTrue(transport.ssh_offline_noise(s), s)

    def test_ssh_noise_does_not_eat_real_output(self):
        for s in ["Current upgrade progress 100%",
                  "Upgrade progress 100% Success",
                  "FlashExit Success", "SwitchReboot", "Fail!!!",
                  "Aurix active boot :B", "md5sum: /tmp/x: No such file"]:
            self.assertFalse(transport.ssh_offline_noise(s), s)

    def test_basename_guard(self):
        for n in ["VLMUMCU_x.zip", "x_APPA.hex", "a-b_c.d+e.hex"]:
            self.assertTrue(transport.basename_ok(n), n)
        for n in ["x;rm -rf /.hex", "a b.hex", "x$(id).hex", "x`id`.hex",
                  "../evil.hex", "x|y.hex", "x&y.hex"]:
            self.assertFalse(transport.basename_ok(n), n)


# ---------------------------------------------------------------- MCU ssh
class TestMcuSsh(TransportTest):
    IP = "172.20.1.22"

    def setup_board(self, appa, appb, zones=("A", "B"), dts="xp5_xccp-a"):
        import hashlib
        b = self.board
        b.ssh_online = True
        b.capture(common.KERNEL_DTS_NODE, dts)
        vn = {"n": 0}

        def zone(cmd):
            i = vn["n"]
            vn["n"] += 1
            z = zones[i] if i < len(zones) else zones[-1]
            if z is None:
                return ""
            if z == "DUAL":
                return "Aurix active boot :A\nAurix active boot :B\n"
            return f"Aurix active boot :{z}\n"
        b.capture("aurix_utility -v", zone)
        for p in (appa, appb):
            with open(p, "rb") as f:
                m = hashlib.md5(f.read()).hexdigest()
            b.capture(f"md5sum {mcu.REMOTE_DIR}/{os.path.basename(p)}", m)
        return b

    def updates(self):
        return [c for c in self.board.stream_cmds() if "--update=" in c]

    def test_happy_path_A_B_A(self):
        appa, appb = self.hex_files()
        self.setup_board(appa, appb, zones=("A", "B"))
        ok = mcu.flash_mcu(appa, appb, serial=self.IP, use_ssh=True, log=self.quiet)
        self.assertTrue(ok)
        ups = self.updates()
        self.assertEqual(len(ups), 2)
        self.assertIn("x_APPB.hex", ups[0])     # 当前 A -> 先刷对侧 B
        self.assertIn("x_APPA.hex", ups[1])     # 切到 B -> 再刷 A
        for u in ups:
            self.assertIn(",/xpilot/aurix_utility/FlashDrv/FlashDrv.hex --reboot=1", u)
        self.assertEqual(self.board.sleeps, [30, 30])
        # 走的是 scp 而非 adb push
        self.assertTrue(any(a[0] == "scp" for a in self.board.argv_of("stream")))
        self.assertFalse(any(a[0] == "adb" for k, a in self.board.calls))

    def test_slave_opt(self):
        appa, appb = self.hex_files()
        self.setup_board(appa, appb, zones=("A", "B"))
        ok = mcu.flash_mcu(appa, appb, serial=self.IP, slave=True, use_ssh=True, log=self.quiet)
        self.assertTrue(ok)
        for u in self.updates():
            self.assertTrue(u.endswith("--reboot=1 --m=1"), u)

    def test_switch_mismatch_aborts(self):
        appa, appb = self.hex_files()
        self.setup_board(appa, appb, zones=("A", "A"))   # 刷 B 后仍跑 A -> 切区失败
        ok = mcu.flash_mcu(appa, appb, serial=self.IP, use_ssh=True, log=self.quiet)
        self.assertFalse(ok)
        self.assertEqual(len(self.updates()), 1)          # 没有第二次 --update

    def test_unknown_zone_aborts_before_write(self):
        appa, appb = self.hex_files()
        self.setup_board(appa, appb, zones=(None,))       # -v 空输出
        ok = mcu.flash_mcu(appa, appb, serial=self.IP, use_ssh=True, log=self.quiet)
        self.assertFalse(ok)
        self.assertEqual(self.updates(), [])

    def test_dual_zone_banner_aborts(self):
        appa, appb = self.hex_files()
        self.setup_board(appa, appb, zones=("DUAL",))     # ssh banner 污染 :A+:B
        ok = mcu.flash_mcu(appa, appb, serial=self.IP, use_ssh=True, log=self.quiet)
        self.assertFalse(ok)
        self.assertEqual(self.updates(), [])

    def test_push_failure_aborts(self):
        appa, appb = self.hex_files()
        b = self.setup_board(appa, appb)
        b.rc(":/xdata/latest_aurix/", 1)                  # scp 推送失败
        ok = mcu.flash_mcu(appa, appb, serial=self.IP, use_ssh=True, log=self.quiet)
        self.assertFalse(ok)
        self.assertEqual(self.updates(), [])

    def test_offline_exhausted_aborts(self):
        appa, appb = self.hex_files()
        b = self.setup_board(appa, appb)
        b.ssh_online = False
        ok = mcu.flash_mcu(appa, appb, serial=self.IP, use_ssh=True, log=self.quiet)
        self.assertFalse(ok)
        self.assertEqual(self.updates(), [])
        self.assertEqual(b.sleeps, [5] * 60)              # wait_online 60x5s

    def test_dts_preflight_wrong_board_aborts(self):
        appa, appb = self.hex_files()
        self.setup_board(appa, appb, dts="xp5_xccp-vlm")  # 静态 IP 指到了 VLMU
        ok = mcu.flash_mcu(appa, appb, serial=self.IP, use_ssh=True, log=self.quiet)
        self.assertFalse(ok)
        self.assertEqual(self.updates(), [])
        self.assertFalse(any(a[0] == "scp" for a in self.board.argv_of("stream")))

    def test_md5_mismatch_aborts(self):
        appa, appb = self.hex_files()
        b = self.setup_board(appa, appb)
        b.capture_rules.insert(0, (f"md5sum {mcu.REMOTE_DIR}/x_APPA.hex", "deadbeef"))
        ok = mcu.flash_mcu(appa, appb, serial=self.IP, use_ssh=True, log=self.quiet)
        self.assertFalse(ok)
        self.assertEqual(self.updates(), [])

    def test_illegal_basename_aborts(self):
        appa, appb = self.hex_files(appa="x;rm.hex")
        self.setup_board(appa, appb)
        ok = mcu.flash_mcu(appa, appb, serial=self.IP, use_ssh=True, log=self.quiet)
        self.assertFalse(ok)
        self.assertFalse(any(a[0] == "scp" for a in self.board.argv_of("stream")))

    def test_auto_detect_prefers_ssh_when_no_usb(self):
        appa, appb = self.hex_files()
        self.setup_board(appa, appb, zones=("A", "B"))
        with mock.patch("common.detect_devices",
                        return_value=[(self.IP, "SoC_A", "ssh")]):
            ok = mcu.flash_mcu(appa, appb, serial=None, log=self.quiet)
        self.assertTrue(ok)
        self.assertTrue(any(a[0] == "scp" for a in self.board.argv_of("stream")))


# ---------------------------------------------------------------- VLMU ssh
class TestVlmuSsh(TransportTest):
    IP = "172.20.1.119"
    LOG = "/xdata/log/latest/update_service.txt.log"

    def setUp(self):
        super().setUp()
        self.logs = []
        self.board.capture("cat /xdata/log", "")          # 清日志后为空

    def log(self, s=""):
        self.logs.append(s)

    def run_upgrade(self):
        t = transport.for_device(self.IP, use_ssh=True)
        return flasher._run_vlmu_mcu_upgrade("VLMUMCU_x.zip", t, log=self.log)

    def test_success(self):
        self.board.lines("grep -E 'Upgrade progress 100%.*Success|FlashExit.*Success'",
                         ["Upgrade progress 100% Success"])
        self.assertTrue(self.run_upgrade())
        self.assertFalse(any("Step 5" in l for l in self.logs))   # 已锁存, 不兜底

    def test_fail_marker(self):
        self.board.lines("grep 'Fail!!!'", ["Fail!!! upgrade error"])
        self.assertFalse(self.run_upgrade())

    def test_switch_reboot_latches_success(self):
        self.board.lines("grep -E 'Upgrade progress 100%|FlashExit|SwitchReboot'",
                         ["SwitchReboot"])
        self.assertTrue(self.run_upgrade())
        self.assertFalse(any("Step 5" in l for l in self.logs))

    def test_clear_log_residual_aborts(self):
        self.board.capture_rules.insert(0, ("cat /xdata/log", "Upgrade progress 100% Success\n"))
        self.assertFalse(self.run_upgrade())
        self.assertEqual(self.board.argv_of("popen"), [])         # 未启动升级

    def test_clear_log_rc_nonzero_aborts(self):
        self.board.rc("> /xdata/log", 1)
        self.assertFalse(self.run_upgrade())
        self.assertEqual(self.board.argv_of("popen"), [])

    def test_rc255_goes_to_fallback_not_instant_verdict(self):
        b = self.board
        b.spawn_rc = 255
        b.spawn_exits_after = 0                                  # poll 立即返回 255
        b.lines("grep 'Current upgrade progress'", ["Current upgrade progress 100%"])
        b.lines("| tail -3", ["Upgrade progress 100%"])          # 监控 check (无 Success)
        b.lines("| tail -5", ["Upgrade progress 100% Success"])  # 兜底查到 100% -> 成功
        self.assertTrue(self.run_upgrade())
        self.assertTrue(any("ssh 会话关闭" in l for l in self.logs))
        self.assertTrue(any("Step 5" in l for l in self.logs))    # 走了兜底而非秒判

    def test_rc255_fallback_noise_only_is_failure(self):
        b = self.board
        b.spawn_rc = 255
        b.spawn_exits_after = 0
        b.lines("grep 'Current upgrade progress'", ["Current upgrade progress 100%"])
        b.lines("| tail -3", ["Upgrade progress 100%"])
        b.lines("| tail -5", ["Connection closed by remote host"])  # 兜底仅噪声
        self.assertFalse(self.run_upgrade())                      # 无证据 -> 不判成功

    def test_push_ota_md5_and_basename(self):
        import hashlib
        pkg = os.path.join(self._tmp, "VLMUMCU_x.zip")
        with open(pkg, "wb") as f:
            f.write(b"ota-bytes")
        md5 = hashlib.md5(b"ota-bytes").hexdigest()
        t = transport.for_device(self.IP, use_ssh=True)
        self.board.capture(f"md5sum /tmp/VLMUMCU_x.zip", md5)
        self.assertTrue(flasher._push_ota(t, pkg, self.log))
        # md5 不符
        self.board.capture_rules.insert(0, ("md5sum /tmp/VLMUMCU_x.zip", "bad"))
        self.assertFalse(flasher._push_ota(t, pkg, self.log))
        # 非法文件名
        bad = os.path.join(self._tmp, "V;rm.zip")
        with open(bad, "wb") as f:
            f.write(b"x")
        self.assertFalse(flasher._push_ota(t, bad, self.log))


# ---------------------------------------------------------------- 分发
class TestDispatch(TransportTest):
    def test_mixed_snapshot_maps(self):
        devs = [("SER_A", "SoC_A", "adb"),
                ("172.20.1.119", "SoC_VLMU", "ssh"),
                ("FB_B", "SoC_B", "fastboot")]
        maps = flasher._build_maps(devs)
        km = flasher.KIND_MAP
        self.assertEqual(flasher.serial_for_target("mcu", maps[km["mcu"]]), "SER_A")
        self.assertEqual(flasher.serial_for_target("vlmu", maps[km["vlmu_mcu"]]), "172.20.1.119")
        self.assertEqual(flasher.serial_for_target("xp5_b", maps[km["big"]]), "FB_B")
        self.assertEqual(flasher.serial_for_target("vlmu", maps[km["small"]]), "172.20.1.119")
        # big 排除 ssh; small/mcu 排除 fastboot
        self.assertEqual(flasher.serial_for_target("vlmu", maps["big"]), "")
        self.assertEqual(flasher.serial_for_target("xp5_b", maps["small"]), "")

    def test_run_job_threads_use_ssh(self):
        for src, expect in [("ssh", True), ("adb", False), ("fastboot", False)]:
            with mock.patch.object(flasher, "flash_mcu", return_value=True) as m:
                flasher._run_job("mcu", "mcu", "/p/A.tar.gz", "S", "/w",
                                 self.quiet, False, dev_source=src)
                self.assertEqual(m.call_args.kwargs.get("use_ssh"), expect, src)
            with mock.patch.object(flasher, "flash_vlmu_mcu", return_value=True) as v:
                flasher._run_job("vlmu_mcu", "vlmu", "/p/V.zip", "S", "/w",
                                 self.quiet, False, dev_source=src)
                self.assertEqual(v.call_args.kwargs.get("use_ssh"), expect, src)

    def test_run_job_extracted_threads_use_ssh(self):
        with mock.patch.object(flasher, "flash_mcu_extracted", return_value=True) as m:
            flasher._run_job_extracted("mcu", "mcu", "/d", "S", self.quiet, False, dev_source="ssh")
            self.assertTrue(m.call_args.kwargs.get("use_ssh"))
        with mock.patch.object(flasher, "flash_vlmu_mcu_extracted", return_value=True) as v:
            flasher._run_job_extracted("vlmu_mcu", "vlmu", "/d", "S", self.quiet, False, dev_source="ssh")
            self.assertTrue(v.call_args.kwargs.get("use_ssh"))

    def test_fastboot_only_mcu_skipped_all_skip_false(self):
        with mock.patch("common.detect_devices", return_value=[("FB_A", "SoC_A", "fastboot")]), \
             mock.patch.object(flasher, "REBOOT_WAIT_SEC", 0), \
             mock.patch.object(flasher, "flash_mcu") as m:
            results = []
            ok = flasher.execute_jobs([("mcu", "mcu", "/p/A.tar.gz", False)],
                                      log=self.quiet, results_out=results)
        self.assertFalse(ok)
        m.assert_not_called()
        self.assertEqual(results[0]["status"], "skipped")

    def test_reboot_wait_then_ssh_pickup(self):
        import itertools
        clock = itertools.count(0, 1)
        with mock.patch("common.detect_devices",
                        return_value=[("172.20.1.22", "SoC_A", "ssh")]), \
             mock.patch("time.monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(flasher, "REBOOT_POLL_SEC", 0), \
             mock.patch.object(flasher, "flash_mcu", return_value=True) as m:
            ok = flasher.execute_jobs(
                [("mcu", "mcu", "/p/A.tar.gz", False)],
                log=self.quiet, devices=[("FB_A", "SoC_A", "fastboot")])
        self.assertTrue(ok)
        self.assertEqual(m.call_args.args[1], "172.20.1.22")   # serial 为第 2 个位置参数
        self.assertTrue(m.call_args.kwargs.get("use_ssh"))


# ---------------------------------------------------------------- dry-run
class TestDryRun(TransportTest):
    def test_adb_mcu_text_golden(self):
        logs = []
        with mock.patch("common.detect_devices", return_value=[("SER_A", "SoC_A", "adb")]):
            flasher.execute_jobs([("mcu", "mcu", "/pkg/A.tar.gz", False)],
                                 log=lambda s="": logs.append(s), dry_run=True)
        dry = [l for l in logs if "DRY-RUN" in l]
        self.assertEqual(dry, [
            "[DRY-RUN] extract /pkg/A.tar.gz",
            "[DRY-RUN] find *_APPA.hex / *_APPB.hex in extracted dir",
            "[DRY-RUN] mcu.flash_mcu(serial=SER_A) via adb (A/B 双区)",
        ])
        self.assertEqual(self.board.calls, [])            # dry-run 零 subprocess

    def test_ssh_mcu_text(self):
        logs = []
        with mock.patch("common.detect_devices", return_value=[("172.20.1.22", "SoC_A", "ssh")]):
            flasher.execute_jobs([("mcu", "mcu", "/pkg/A.tar.gz", False)],
                                 log=lambda s="": logs.append(s), dry_run=True)
        dry = "\n".join(l for l in logs if "DRY-RUN" in l)
        self.assertIn("scp", dry)
        self.assertIn("via ssh", dry)
        self.assertIn("root@172.20.1.22", dry)
        self.assertEqual(self.board.calls, [])

    def test_vlmu_dry_run_push_cmd(self):
        for src, dev, word in [("adb", "SER_V", "adb push"), ("ssh", "172.20.1.119", "scp")]:
            logs = []
            with mock.patch("common.detect_devices", return_value=[(dev, "SoC_VLMU", src)]):
                flasher.execute_jobs([("vlmu_mcu", "vlmu", "/pkg/V.zip", False)],
                                     log=lambda s="": logs.append(s), dry_run=True)
            dry = "\n".join(l for l in logs if "DRY-RUN" in l)
            self.assertIn(word, dry)
            self.assertEqual(self.board.calls, [])


if __name__ == "__main__":
    unittest.main()