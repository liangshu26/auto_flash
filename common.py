#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
common.py — XP5 刷机工具共享逻辑

把原先散落在 flash.sh / flash_menu.sh / aurix_v7_xp5.sh / flash_gui.py 里、
重复了 4 份的“设备识别 + 包分类”逻辑收敛到这里, 供 GUI 和后续 flash.py 复用。

只用标准库。所有 adb 输出都按“可能含 NUL/控制字节”处理 (Linux GNU grep 曾因
此把整行当成 binary 而丢行), 且 adb 一律 stdin=DEVNULL (避免在循环里吞掉列表)。
"""

import os
import re
import subprocess

# ChipMode -> 刷机目标名 (与 bash 脚本一致)
MODE_TO_TARGET = {"SoC_VLMU": "vlmu", "SoC_A": "xp5_a", "SoC_B": "xp5_b"}
ALL_TARGETS = ("xp5_a", "xp5_b", "vlmu", "mcu")

ARCHIVE_EXT = (".tar.gz", ".tgz", ".zip")

# 包分类的键
CATEGORIES = ("vlmu_big", "vlmu_small", "xp5_big", "xp5_small", "mcu", "vlmu_mcu")

# 清掉 adb 输出里的 C0 控制字节 (保留 \n \t), 否则正则/grep 会被 NUL 干扰
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CHIPMODE_RE = re.compile(r"Current ChipMode:\s*([A-Za-z0-9_]+)", re.IGNORECASE)


def strip_ctrl(s):
    """清掉 adb 输出里的 NUL/CR 等 C0 控制字节 (保留 \\n \\t)。

    Linux GNU grep 曾因 NUL 把整行当 binary 丢掉, 所有 adb 文本都先过这个。
    """
    return _CTRL_RE.sub("", s)


# ------------------------------------------------------------------ adb 设备
def adb_devices(timeout=15):
    """返回在线 (state=device) 的 serial 列表。adb 不存在/出错时返回 []。"""
    try:
        out = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True,
            errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
        ).stdout
    except Exception:
        return []
    serials = []
    for line in out.splitlines()[1:]:          # 跳过 "List of devices attached"
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def device_chipmode(serial, timeout=15):
    """读取某设备的 ChipMode (SoC_VLMU/SoC_A/SoC_B); 失败返回 'unknown'。"""
    try:
        v = subprocess.run(
            ["adb", "-s", serial, "shell", "version"],
            capture_output=True, text=True, errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL,
        ).stdout
    except Exception:
        return "unknown"
    v = strip_ctrl(v)
    m = _CHIPMODE_RE.search(v)
    return m.group(1) if m else "unknown"


def detect_devices(timeout=15):
    """返回 [(serial, chipmode), ...]。会调用 adb, 可能较慢, 调用方应放子线程。"""
    return [(s, device_chipmode(s, timeout)) for s in adb_devices(timeout)]


def find_serial_by_mode(mode, timeout=15):
    """返回第一个 ChipMode==mode 的 serial, 没有则 None。"""
    for s, m in detect_devices(timeout):
        if m == mode:
            return s
    return None


# ------------------------------------------------------------------ 包分类
def classify(name):
    """按大小写敏感前缀给包名分类; 不属于任何类别返回 None。

    VLMU*          -> vlmu_big      (adc_flashall.sh 全量刷)
    vlmu*          -> vlmu_small    (deploy_xpilot_by_adb.sh 快速部署)
    XCCP*          -> xp5_big
    xpilot*        -> xp5_small
    XPilot_AURIX*  -> mcu
    VLMUMCU_*      -> vlmu_mcu      (VLMU MCU OTA 包, 可直接刷)
    *_VLMU_*       -> vlmu_mcu      (VLMU MCU 升级包, 如 G01_VLMU_*, E29_VLMU_*)
    """
    if name.startswith("XPilot_AURIX"):
        return "mcu"
    if name.startswith("VLMUMCU"):
        return "vlmu_mcu"
    if name.startswith("VLMU"):
        return "vlmu_big"
    if name.startswith("vlmu"):
        return "vlmu_small"
    if name.startswith("XCCP"):
        return "xp5_big"
    if name.startswith("xpilot"):
        return "xp5_small"
    # VLMU MCU 包: 车型前缀 + _VLMU_, 如 G01_VLMU_*, E29_VLMU_*
    if "_VLMU_" in name and not name.startswith("VLMU"):
        return "vlmu_mcu"
    return None


def scan_dir(dirpath):
    """扫描任意目录, 返回 {类别: [文件名,...]} (每类按 mtime 新->旧)。"""
    cats = {c: [] for c in CATEGORIES}
    if not dirpath or not os.path.isdir(dirpath):
        return cats
    try:
        files = [f for f in os.listdir(dirpath) if f.endswith(ARCHIVE_EXT)]
    except OSError:
        return cats
    files.sort(key=lambda f: os.path.getmtime(os.path.join(dirpath, f)), reverse=True)
    for f in files:
        c = classify(f)
        if c:
            cats[c].append(f)
    return cats


def scan_extracted_dir(dirpath):
    """扫描已解压目录, 返回 {类别: [(目录名, 完整路径), ...]}。
    
    扫描主目录中的已解压文件夹。
    已解压目录按 mtime 新->旧排序。
    """
    cats = {c: [] for c in CATEGORIES}
    if not dirpath or not os.path.isdir(dirpath):
        return cats
    
    # 收集所有已解压目录
    extracted_dirs = []
    
    # 扫描主目录中的子目录
    try:
        for d in os.listdir(dirpath):
            full_path = os.path.join(dirpath, d)
            if os.path.isdir(full_path):
                # 检查是否是有效的解压目录 (包含刷机脚本或文件)
                if _is_extracted_package(full_path):
                    extracted_dirs.append((d, full_path))
    except OSError:
        pass
    
    # 按 mtime 排序 (新->旧)
    extracted_dirs.sort(key=lambda x: os.path.getmtime(x[1]), reverse=True)
    
    # 分类
    for dir_name, full_path in extracted_dirs:
        c = classify(dir_name)
        if c:
            cats[c].append((dir_name, full_path))
    
    return cats


def _is_extracted_package(dirpath):
    """检查目录是否是有效的已解压刷机包。"""
    # 检查是否包含常见的刷机文件
    markers = [
        "flash_tools",
        "deploy_xpilot_by_adb.sh",
        "adc_flashall.sh",
        "OTAPackage",
    ]
    try:
        for item in os.listdir(dirpath):
            if item in markers:
                return True
            # 检查是否是 MCU 包 (包含 .hex 文件)
            if item.endswith("_APPA.hex") or item.endswith("_APPB.hex"):
                return True
    except OSError:
        pass
    return False


def newest_pkg(cats, category):
    """取某类别最新的包名, 没有则空串。"""
    lst = cats.get(category) or []
    return lst[0] if lst else ""


def targets_with_packages(cats):
    """根据扫描结果返回有包可刷的目标列表。"""
    out = []
    if cats["vlmu_big"] or cats["vlmu_small"]:
        out.append("vlmu")
    if cats["xp5_big"] or cats["xp5_small"]:
        out += ["xp5_a", "xp5_b"]
    if cats["mcu"]:
        out.append("mcu")
    return out


# ------------------------------------------------------------------ 会话目录
def default_prep_root():
    """flash_prep 根目录 (可用环境变量 PREP_ROOT 覆盖)。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.environ.get("PREP_ROOT", os.path.join(here, "flash_prep"))


def list_session_dirs(prep_root=None):
    """返回 [(name, fullpath), ...]: prep_root 下含压缩包子目录, 新的在前。

    目录名为时间戳 (YYYYmmdd_HHMMSS), 名字倒序即时序倒序。
    """
    prep_root = prep_root or default_prep_root()
    if not os.path.isdir(prep_root):
        return []
    out = []
    for name in os.listdir(prep_root):
        d = os.path.join(prep_root, name)
        if not os.path.isdir(d):
            continue
        try:
            if any(f.endswith(ARCHIVE_EXT) for f in os.listdir(d)):
                out.append((name, d))
        except OSError:
            continue
    out.sort(key=lambda x: x[0], reverse=True)
    return out


# ------------------------------------------------------------------ 子进程流式执行
_ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")


def _last_cr(b):
    """返回最后一个 \\r 之后的内容; 末尾为空段则取最后一个非空段 (进度条折叠)。"""
    segs = b.split(b"\r")
    if segs[-1] != b"":
        return segs[-1]
    for s in reversed(segs):
        if s != b"":
            return s
    return b""


def _emit_line(bline, log):
    if b"\r" in bline:
        bline = _last_cr(bline)
    bline = _ANSI_RE.sub(b"", bline)
    text = bline.decode("utf-8", "replace")
    if text.strip() == "":
        return
    log(text)


def run_streaming(argv, cwd=None, log=print, env=None,
                  start_new_session=False, proc_out=None):
    """运行 argv, 把 stdout+stderr 逐行喂给 log(str)。

    二进制读取, 只在 \\n 处切分 (避免通用换行把 curl/wget 的 \\r 进度条拆成海量行),
    \\r 段折叠为最后一帧, 并清掉 ANSI 颜色码。返回退出码 (无法启动返回 127)。

    start_new_session=True 时子进程自成进程组 (POSIX setsid), 调用方可用
    os.killpg 一次杀掉整棵进程树 (含 adb/tar/sleep 等孙进程)。只有 GUI 直接
    拉起的顶层子进程才应置 True; flasher/mcu 内部嵌套调用保持默认 False, 让孙
    进程留在同一进程组里一起被杀。
    proc_out 是一个 list, spawn 成功后立刻把 Popen 追加进去, 供调用方取消。
    """
    try:
        popen_kwargs = {}
        if start_new_session and os.name == "posix":
            popen_kwargs["start_new_session"] = True
        p = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, bufsize=0, env=env, **popen_kwargs,
        )
    except Exception as e:  # noqa
        log(f"[ERROR] 无法启动 {argv[0] if argv else '?'}: {e}")
        return 127
    if proc_out is not None:
        proc_out.append(p)
    fd = p.stdout.fileno()
    buf = b""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        parts = buf.split(b"\n")
        buf = parts.pop()               # 最后一段不完整, 留到下次
        for part in parts:
            _emit_line(part, log)
        # 仅在缓冲区过大时裁到最后一个 \r 之后 (保留分隔符), 防止跨 chunk 段拼接错乱
        if len(buf) > (1 << 18):        # 256KB
            idx = buf.rfind(b"\r")
            buf = buf[idx + 1:] if idx != -1 else buf[-(1 << 18):]
    if buf:
        _emit_line(buf, log)
    p.wait()
    return p.returncode
