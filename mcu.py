#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcu.py — MCU (Aurix) A/B 双区刷机 (aurix_v7_xp5.sh 的 Python 版)

目标设备: SoC_A (xp5_a) 板, 其上跑 /xpilot/aurix_utility。
逻辑与 bash 版一致: 先刷“对侧区” -> reboot -> 校验区已切换 -> 再刷另一侧。

传输: 默认 adb; 无 USB 直连但免密 root ssh 可达时自动走 ssh (与小包一致)。
hex 文件 (*_APPA.hex / *_APPB.hex) 由调用方提供路径 (flasher 从解压目录里找到,
CLI 则默认在当前目录里找)。

  CLI:  python3 mcu.py                 # 自动识别 SoC_A, 在当前目录找 hex
        python3 mcu.py -s <serial>     # 指定设备
        python3 mcu.py S               # 刷 slave Aurix (--m=1)
        python3 mcu.py -s <ip> --ssh   # 强制走 ssh
  import:
        import mcu
        mcu.flash_mcu("/path/x_APPA.hex", "/path/x_APPB.hex", serial=..., log=print)
"""

import os
import sys
import glob
import time
import argparse

import common
import transport

AURIX_IMG = "latest_aurix"
REMOTE_DIR = f"/xdata/{AURIX_IMG}"
AURIX_UTIL = "/xpilot/aurix_utility/aurix_utility"
FLASHDRV = "/xpilot/aurix_utility/FlashDrv/FlashDrv.hex"

# 区号: A=1, B=2 (0 表示未知/读取失败)
ZONE_A, ZONE_B, ZONE_UNKNOWN = 1, 2, 0


# ---------------------------------------------------------------- 步骤
def _copy_image(t, appa, appb, log):
    """把两个 hex 推到 /xdata/latest_aurix/。ssh 额外做文件名闸与远端 md5 校验。"""
    t.shell_capture("chmod 777 /xdata")
    t.shell_capture(f"rm -rf {REMOTE_DIR}")
    t.shell_capture(f"mkdir -p {REMOTE_DIR}")
    for p in (appa, appb):
        if not os.path.isfile(p):
            log(f"[ERROR] hex 文件不存在: {p}")
            return False
    log(f"[INFO] pushing {os.path.basename(appa)} 和 {os.path.basename(appb)} "
        f"到 {REMOTE_DIR}/")
    is_ssh = t.name == "ssh"
    for p in (appa, appb):
        name = os.path.basename(p)
        if is_ssh and not transport.basename_ok(name):
            log(f"[ERROR] 文件名含远端 shell 不安全字符, 拒绝推送: {name}")
            return False
        if t.push(p, f"{REMOTE_DIR}/", log) != 0:
            log(f"[ERROR] {t.push_name} 失败: {p}")
            return False
        if is_ssh and not transport.remote_md5_ok(t, p, f"{REMOTE_DIR}/{name}", log):
            return False
    return True


def _check_zone(t, slave_opt, log, verbose=False):
    """读 aurix_utility -v, 判断当前运行区 (A=1 / B=2)。失败或解析不到返回 0。"""
    out = t.shell_capture(f"{AURIX_UTIL} -v{slave_opt}")
    boot_lines = [l for l in out.splitlines() if "Aurix active boot" in l]
    if verbose:
        # 打印原始区信息, 便于诊断"刷上了却报切区失败"
        if boot_lines:
            for l in boot_lines:
                log(f"[INFO] aurix -v: {l.strip()}")
        else:
            log(f"[INFO] aurix -v 无 'Aurix active boot' 行, 原始输出: {out.strip()[:200]!r}")
    if not out.strip():
        log(f"[ERROR] 无法从设备 {t.serial} 读取 aurix 区信息")
        return ZONE_UNKNOWN
    # ssh 下 banner/MOTD 可能污染 stdout: 同时出现 :A 与 :B 判未知, 不冒险
    if t.name == "ssh":
        if any(":A" in l for l in boot_lines) and any(":B" in l for l in boot_lines):
            log("[ERROR] aurix 区输出同时含 :A 与 :B (疑似 banner 污染), 判未知")
            return ZONE_UNKNOWN
    for line in boot_lines:
        if ":A" in line:
            return ZONE_A
        if ":B" in line:
            return ZONE_B
    log("[ERROR] 无法从设备输出解析当前 Aurix 运行区")
    return ZONE_UNKNOWN


def _wait_zone_switch(t, slave_opt, finish, log, retries=6, interval=5):
    """刷对侧区重启后校验区已切到 finish。

    Aurix 切区可能慢于 adb 回 online, 刚回来时 -v 未必反映新区, 故有限重试:
    只重读区、绝不刷写; 切成 finish 立即返回, 重试耗尽仍不符才返回最后读到的区
    (由调用方判 Switch failed)。与 vendor 脚本相比多了重试, 但依旧 fail-closed。
    """
    curr = ZONE_UNKNOWN
    for attempt in range(retries + 1):
        curr = _check_zone(t, slave_opt, log, verbose=True)
        log(f"[INFO] FINISH_FLASH_ZONE: {finish}")
        log(f"[INFO] CURR_AURIX_ZONE: {curr} (读区 {attempt + 1}/{retries + 1})")
        if curr == finish:
            return curr
        if attempt < retries:
            log(f"[WARN] 区尚未切到 {finish} (当前 {curr}), {interval}s 后重读 ...")
            time.sleep(interval)
    return curr


def _flash_zone(t, hexpath, slave_opt, log, running, flashing):
    """刷指定 hex (--reboot=1)。设备刷完会 reboot, 连接断开属正常, 不判退出码。"""
    log(f"[INFO] Currently Running on Zone: {running}")
    log(f"[INFO] Currently Flashing Zone: {flashing}")
    name = os.path.basename(hexpath)
    remote = (f"{AURIX_UTIL} --update={REMOTE_DIR}/{name},{FLASHDRV} "
              f"--reboot=1{slave_opt}")
    t.shell_stream(remote, log)


def _ssh_preflight_soc_a(t, log):
    """ssh 身份预检: 静态 IP 表可能把 MCU 指向别的板, 刷前用设备树确认是 SoC_A。"""
    dts = t.shell_capture(f"cat {common.KERNEL_DTS_NODE}").strip()
    mode = common._DTS_TO_MODE.get(dts)
    if mode != "SoC_A":
        log(f"[ERROR] ssh 目标 {t.serial} 板型为 {mode or dts or 'unknown'}, "
            f"非 SoC_A, 中止 (MCU 只能经 SoC_A)")
        return False
    return True


def _detect_soc_a(log):
    """自动识别 SoC_A, 返回 (serial, use_ssh)。USB 优先继承自 detect_devices。"""
    for d in common.detect_devices():
        s, m = d[0], d[1]
        src = d[2] if len(d) > 2 else "adb"
        if m == "SoC_A":
            return s, (src == "ssh")
    log("[ERROR] 没有通过 adb/ssh 找到 SoC_A (xp5_a) 设备")
    return None, False


# ---------------------------------------------------------------- 主流程
def flash_mcu(appa, appb, serial=None, slave=False, log=print, use_ssh=False):
    """A/B 双区刷 MCU。成功返回 True。

    appa/appb: *_APPA.hex / *_APPB.hex 路径。serial 省略则自动识别 SoC_A。
    use_ssh: 强制走 ssh (serial 须为 ssh IP); 省略时由自动识别的接入方式决定。
    """
    if not serial:
        log("[INFO] 未指定 serial, 自动识别 SoC_A (xp5_a) 设备...")
        serial, use_ssh = _detect_soc_a(log)
        if not serial:
            return False
    t = transport.for_device(serial, use_ssh)
    log(f"[INFO] Using {t.name} device: {serial}")
    slave_opt = " --m=1" if slave else ""
    if slave:
        log("[INFO] Flashing Slave!")

    if not t.wait_online(log):
        return False

    if t.name == "ssh" and not _ssh_preflight_soc_a(t, log):
        return False

    if not _copy_image(t, appa, appb, log):
        return False

    curr = _check_zone(t, slave_opt, log)
    if curr == ZONE_UNKNOWN:
        log("[ERROR] Cannot Start Flash! (未知区)")
        return False

    # 先刷对侧区
    if curr == ZONE_A:
        _flash_zone(t, appb, slave_opt, log, running="A", flashing="B")
        finish = ZONE_B
    else:
        _flash_zone(t, appa, slave_opt, log, running="B", flashing="A")
        finish = ZONE_A
    log(f"[INFO] FINISH_FLASH_ZONE = {finish}")

    time.sleep(30)
    if not t.wait_online(log):
        return False

    # 校验区已切换 (切区可能慢, 有限重试只读不刷)
    curr = _wait_zone_switch(t, slave_opt, finish, log)
    if curr != finish:
        log("[ERROR] Switch failed! Cannot Start Flash!")
        return False

    # 再刷另一侧
    if curr == ZONE_A:
        _flash_zone(t, appb, slave_opt, log, running="A", flashing="B")
    else:
        _flash_zone(t, appa, slave_opt, log, running="B", flashing="A")

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
        description="MCU (Aurix) A/B 双区刷机 (aurix_v7_xp5.sh 的 Python 版), adb 优先, 可走 ssh。")
    p.add_argument("-s", "--serial", default="",
                   help="SoC_A 设备 adb serial 或 ssh IP (省略则自动识别)")
    p.add_argument("-S", "--slave", action="store_true",
                   help="刷 slave Aurix (--m=1)")
    p.add_argument("--ssh", action="store_true",
                   help="强制走 ssh (须配合 -s <ssh IP>)")
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
                   slave=slave, log=print, use_ssh=args.ssh)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())