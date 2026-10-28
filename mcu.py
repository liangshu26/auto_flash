#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcu.py — MCU (Aurix) A/B 双区刷机 via adb (aurix_v7_xp5.sh 的 Python 版)

目标设备: SoC_A (xp5_a) 板, 其上跑 /xpilot/aurix_utility。
逻辑与 bash 版一致: 先刷“对侧区” -> reboot -> 校验区已切换 -> 再刷另一侧。

hex 文件 (*_APPA.hex / *_APPB.hex) 由调用方提供路径 (flasher 从解压目录里找到,
CLI 则默认在当前目录里找)。

  CLI:  python3 mcu.py                 # 自动识别 SoC_A, 在当前目录找 hex
        python3 mcu.py -s <serial>     # 指定设备
        python3 mcu.py S               # 刷 slave Aurix (--m=1)
  import:
        import mcu
        mcu.flash_mcu("/path/x_APPA.hex", "/path/x_APPB.hex", serial=..., log=print)
"""

import os
import sys
import glob
import time
import argparse
import subprocess

import common

AURIX_IMG = "latest_aurix"
REMOTE_DIR = f"/xdata/{AURIX_IMG}"
AURIX_UTIL = "/xpilot/aurix_utility/aurix_utility"
FLASHDRV = "/xpilot/aurix_utility/FlashDrv/FlashDrv.hex"

# 区号: A=1, B=2 (0 表示未知/读取失败)
ZONE_A, ZONE_B, ZONE_UNKNOWN = 1, 2, 0


# ---------------------------------------------------------------- adb 底层
def _adb_shell(serial, cmd, timeout=60):
    """adb shell 并捕获输出 (清掉 NUL/CR); 出错返回空串。"""
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "shell", cmd],
            capture_output=True, text=True, errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa
        return ""
    return common.strip_ctrl(r.stdout)


def _adb_stream(serial, argv_tail, log):
    """adb 命令 (push / 刷机 shell) 走流式输出。返回退出码。"""
    return common.run_streaming(["adb", "-s", serial] + argv_tail, log=log)


def _get_state(serial, timeout=15):
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "get-state"],
            capture_output=True, text=True, errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa
        return ""
    return common.strip_ctrl(r.stdout).strip()


def _is_online(serial):
    return _get_state(serial) == "device"


def _wait_online(serial, log, retries=60, interval=5):
    """等待设备在线; 已在线立即返回 True, 超时返回 False。"""
    if _is_online(serial):
        return True
    log(f"[WARN] device {serial} offline, waiting for it to come back...")
    for _ in range(retries):
        time.sleep(interval)
        if _is_online(serial):
            return True
    log(f"[ERROR] device {serial} still offline after {retries * interval}s")
    return False


# ---------------------------------------------------------------- 步骤
def _copy_image(serial, appa, appb, log):
    """把两个 hex push 到 /xdata/latest_aurix/。"""
    _adb_shell(serial, "chmod 777 /xdata")
    _adb_shell(serial, f"rm -rf {REMOTE_DIR}")
    _adb_shell(serial, f"mkdir -p {REMOTE_DIR}")
    for p in (appa, appb):
        if not os.path.isfile(p):
            log(f"[ERROR] hex 文件不存在: {p}")
            return False
    log(f"[INFO] pushing {os.path.basename(appa)} 和 {os.path.basename(appb)} "
        f"到 {REMOTE_DIR}/")
    for p in (appa, appb):
        if _adb_stream(serial, ["push", p, f"{REMOTE_DIR}/"], log) != 0:
            log(f"[ERROR] adb push 失败: {p}")
            return False
    return True


def _check_zone(serial, slave_opt, log):
    """读 aurix_utility -v, 判断当前运行区 (A=1 / B=2)。失败返回 0。"""
    out = _adb_shell(serial, f"{AURIX_UTIL} -v{slave_opt}")
    if not out.strip():
        log(f"[ERROR] 无法从设备 {serial} 读取 aurix 区信息")
        return ZONE_UNKNOWN
    for line in out.splitlines():
        if "Aurix active boot" in line and ":A" in line:
            return ZONE_A
    return ZONE_B


def _flash_zone(serial, hexpath, slave_opt, log, running, flashing):
    """刷指定 hex (--reboot=1)。设备刷完会 reboot, adb 断开属正常, 不判退出码。"""
    log(f"[INFO] Currently Running on Zone: {running}")
    log(f"[INFO] Currently Flashing Zone: {flashing}")
    name = os.path.basename(hexpath)
    remote = (f"{AURIX_UTIL} --update={REMOTE_DIR}/{name},{FLASHDRV} "
              f"--reboot=1{slave_opt}")
    _adb_stream(serial, ["shell", remote], log)


# ---------------------------------------------------------------- 主流程
def flash_mcu(appa, appb, serial=None, slave=False, log=print):
    """A/B 双区刷 MCU。成功返回 True。

    appa/appb: *_APPA.hex / *_APPB.hex 路径。serial 省略则自动识别 SoC_A。
    """
    if not serial:
        log("[INFO] 未指定 serial, 自动识别 SoC_A (xp5_a) 设备...")
        serial = common.find_serial_by_mode("SoC_A")
        if not serial:
            log("[ERROR] 没有通过 adb 找到 SoC_A (xp5_a) 设备")
            return False
    log(f"[INFO] Using adb device: {serial}")
    slave_opt = " --m=1" if slave else ""
    if slave:
        log("[INFO] Flashing Slave!")

    if not _wait_online(serial, log):
        return False

    if not _copy_image(serial, appa, appb, log):
        return False

    curr = _check_zone(serial, slave_opt, log)
    if curr == ZONE_UNKNOWN:
        log("[ERROR] Cannot Start Flash! (未知区)")
        return False

    # 先刷对侧区
    if curr == ZONE_A:
        _flash_zone(serial, appb, slave_opt, log, running="A", flashing="B")
        finish = ZONE_B
    else:
        _flash_zone(serial, appa, slave_opt, log, running="B", flashing="A")
        finish = ZONE_A
    log(f"[INFO] FINISH_FLASH_ZONE = {finish}")

    time.sleep(30)
    if not _wait_online(serial, log):
        return False

    # 校验区已切换
    curr = _check_zone(serial, slave_opt, log)
    log(f"[INFO] FINISH_FLASH_ZONE: {finish}")
    log(f"[INFO] CURR_AURIX_ZONE: {curr}")
    if curr != finish:
        log("[ERROR] Switch failed! Cannot Start Flash!")
        return False

    # 再刷另一侧
    if curr == ZONE_A:
        _flash_zone(serial, appb, slave_opt, log, running="A", flashing="B")
    else:
        _flash_zone(serial, appa, slave_opt, log, running="B", flashing="A")

    time.sleep(30)
    log("[OK] MCU flashing finished.")
    return True


# ---------------------------------------------------------------- CLI
def _find_hex(pattern):
    m = sorted(glob.glob(pattern))
    return m[0] if m else None


def build_parser():
    p = argparse.ArgumentParser(
        prog="mcu.py",
        description="MCU (Aurix) A/B 双区刷机 via adb (aurix_v7_xp5.sh 的 Python 版)。")
    p.add_argument("-s", "--serial", default="",
                   help="SoC_A 设备 adb serial (省略则自动识别)")
    p.add_argument("-S", "--slave", action="store_true",
                   help="刷 slave Aurix (--m=1)")
    p.add_argument("rest", nargs="*",
                   help="兼容旧脚本: 传 'S' 亦表示刷 slave")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    slave = args.slave or any(x.strip().upper() == "S" for x in args.rest)

    appa = _find_hex("*_APPA.hex")
    appb = _find_hex("*_APPB.hex")
    if not appa or not appb:
        sys.stderr.write(
            "[ERROR] 当前目录找不到 *_APPA.hex / *_APPB.hex\n")
        return 1

    ok = flash_mcu(appa, appb, serial=(args.serial or None),
                   slave=slave, log=print)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
