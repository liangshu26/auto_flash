#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
downloader.py — 从 CI 平台按 DT 号下载 ROM 刷机包 (download_rom.sh 的 Python 版)

纯标准库 (urllib), 无 pip 依赖。既可命令行使用, 也可被 flash_gui.py import:

  CLI:
    python3 downloader.py -p E29 -T vlmu-big -a 196163 196164
    python3 downloader.py -l -p E29 -T mcu 60974
    python3 downloader.py -p E29 -w 30 196163          # 轮询直到出包再下

  import:
    import downloader
    downloader.run_download("E29", [("vlmu-big","196163"),("mcu","60974")],
                            "/path/dest", log=print)

行为与 download_rom.sh 对齐: browse API、递归找文件、大小写不敏感 filter、
断点续传 (HTTP Range)、watch 轮询、批量 DT、-a 全下 / 交互式选择。
"""

import os
import re
import sys
import json
import time
import ssl
import shutil
import hashlib
import threading
import argparse
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

BASE_URL = "https://cicdops.xiaopeng.link/data/xpilot"
DEFAULT_WATCH_INTERVAL = 10   # 分钟
DEFAULT_TIMEOUT_HOURS = 24
MAX_PARALLEL = 3              # 同时下载的文件数上限 (跨 DT / 跨直链共用)

# type -> (DT 路径前缀, 默认 filter, 文件名白名单正则)。-f 显式给出时覆盖默认 filter。
# ENG_DEV/ 目录下除真正可刷的大包外, 还并列着 XPU_/XPUS_/XPUMDL_/XPUSMDL_ 与
# VLMU_EEA36_*_ULTRA.zip 等非刷机产物, 光靠路径子串会把它们全下下来 (选 xpu-big
# 能多下十几 GB), 因此大包类型再按文件名收紧。
TYPE_MAP = {
    "vlmu-big":   ("DEBUG",    "VLMU/ENG_",                r"^VLMU\d"),
    "vlmu-small": ("DEBUG",    "VLMU/vlmu_xpilot_release", ""),
    "xpu-big":    ("DEBUG",    "XPU/ENG_",                 r"^XCCP"),
    "xpu-small":  ("DEBUG",    "XPU/xpilot_release_full",  ""),
    "mcu":        ("MCUDEBUG", "XPilot_AURIX",             ""),
}

# ---------------------------------------------------------------- 日志 / 颜色
_COLOR = sys.stdout.isatty()


def _c(code, s):
    return f"\033[{code}{s}\033[0m" if _COLOR else s


def default_log(msg):
    """按前缀上色打印 (供 CLI 用)。"""
    if msg.startswith("[INFO]"):
        print(_c("0;34m", "[INFO]") + msg[6:])
    elif msg.startswith("[WARN]"):
        print(_c("1;33m", "[WARN]") + msg[6:])
    elif msg.startswith("[ERROR]"):
        print(_c("0;31m", "[ERROR]") + msg[6:], file=sys.stderr)
    elif msg.startswith("[OK]"):
        print(_c("0;32m", "[OK]") + msg[4:])
    else:
        print(msg)


def format_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "? B"
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{int(n)} B"


# ---------------------------------------------------------------- HTTP
# 默认走系统/Python 信任库校验证书 (公网 *.xiaopeng.link 证书可正常校验)。
# 若企业内网用自签 CA, 请设置环境变量 SSL_CERT_FILE=/path/to/ca.pem 指向该 CA,
# 而不是关闭校验。仅在用户显式 --insecure 时才退回不校验 (等价 curl -k)。
_INSECURE = False


def set_insecure(on=True):
    global _INSECURE
    _INSECURE = bool(on)
    if _INSECURE:
        sys.stderr.write("[WARN] 已启用 --insecure: 不校验 TLS 证书 (存在中间人风险)\n")


def _ssl_context():
    if _INSECURE:
        return ssl._create_unverified_context()
    # create_default_context 会加载系统默认 CA, 并遵循 SSL_CERT_FILE/SSL_CERT_DIR
    return ssl.create_default_context()


def _urlopen(req, timeout=60):
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def _resolve_url(key_or_url):
    """DT 流程传相对 key (拼到 BASE_URL 下); 直链流程传完整 http(s) URL, 原样返回。"""
    if key_or_url.startswith(("http://", "https://")):
        return key_or_url
    return f"{BASE_URL}/{key_or_url}"


def _filename_of(url):
    """从 URL 取文件名, 去掉 query/fragment。"""
    path = urllib.parse.urlparse(url).path
    return os.path.basename(path) or "download"


def browse(path, timeout=60):
    """GET {BASE_URL}/browse/{path} -> dict; 失败抛异常。"""
    url = f"{BASE_URL}/browse/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "xp5-downloader/1.0"})
    with _urlopen(req, timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def list_platforms(log=default_log):
    """返回车型代码列表 (E29 等), 取自 images/DEBUG/ 的子目录名。"""
    log("[INFO] Fetching available vehicle models...")
    data = browse("images/DEBUG/")
    out = []
    for d in sorted(data.get("directories", [])):
        parts = d.rstrip("/").split("/")
        if len(parts) >= 3:
            out.append(parts[2])
    return out


def dt_path(platform, prefix, dt):
    return f"images/DEBUG/{platform}/{prefix}-{dt}/"


def find_files(path, filter_str="", log=default_log, name_re=""):
    """递归列出 path 下匹配 filter 的文件, 返回 [(key, size), ...]。

    跳过 .md5; filter 为大小写不敏感的子串匹配 (空则不过滤)。
    name_re 非空时再按文件名做正则匹配 (大小写敏感), 用于排除路径子串命中
    但并非刷机产物的文件 (如 XPU_/XPUS_/XPUMDL_/XPUSMDL_); 被排除的会汇总
    打一条日志, 便于命名变化时及时发现白名单失效。
    """
    results = []
    skipped = set()
    rx = re.compile(name_re) if name_re else None

    def rec(p):
        try:
            data = browse(p)
        except Exception as e:  # noqa
            log(f"[WARN] browse 失败 {p}: {e}")
            return
        for d in data.get("directories", []):
            rec(d)
        for f in data.get("files", []):
            key = f.get("key", "")
            if not key or key.endswith(".md5"):
                continue
            if filter_str and filter_str.lower() not in key.lower():
                continue
            name = os.path.basename(key)
            if rx and not rx.search(name):
                skipped.add(name)
                continue
            results.append((key, f.get("size", 0)))

    rec(path)
    if skipped:
        names = sorted(skipped)
        shown = ", ".join(names[:3]) + (" ..." if len(names) > 3 else "")
        log(f"[INFO] 已跳过 {len(names)} 个同目录下的非刷机产物: {shown}")
    return results


def _disk_ok(dest_dir, need, log, filename):
    """下载前检查磁盘空间是否够 (need 为还需写入的字节数)。

    空间不足时直接失败, 否则几个 GB 的包会写到一半报错、再重试 5 次同样失败。
    拿不到磁盘信息时放行 (只 WARN), 不因探测失败阻断下载。
    """
    if need <= 0:
        return True
    try:
        free = shutil.disk_usage(dest_dir).free
    except OSError as e:
        log(f"[WARN] 无法检查磁盘空间: {e}")
        return True
    if free < need:
        log(f"[ERROR] 磁盘空间不足: {filename} 还需 {format_size(need)}, "
            f"可用 {format_size(free)} ({dest_dir})")
        return False
    return True


def _download_once(key, dest_dir, log=default_log, timeout=30, cancel_cb=None, pause_cb=None, progress_cb=None):
    """下载单个文件到 dest_dir, 支持断点续传 (Range)。成功返回 True。

    timeout 是 socket 读超时 (秒): 连接卡死时最多等这么久就中断, 交由上层续传重试,
    因此设得较短 (30s) 以便快速从卡顿中自动恢复; 慢但存活的连接 (每 64KB 远快于 30s)
    不受影响。
    cancel_cb: 可选, 无参函数, 返回 True 表示应取消下载。
    pause_cb: 可选, 无参函数, 返回 True 表示应暂停下载。
    progress_cb: 可选, 函数签名 (written, total, speed, eta_str), 用于实时更新进度。
    """
    filename = _filename_of(_resolve_url(key))
    dest = os.path.join(dest_dir, filename)
    url = _resolve_url(key)
    os.makedirs(dest_dir, exist_ok=True)

    # 先获取服务器上的文件大小
    req_head = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "xp5-downloader/1.0"})
    try:
        with _urlopen(req_head, timeout) as r_head:
            server_size = int(r_head.headers.get("Content-Length", 0))
    except Exception:
        server_size = 0

    existing = os.path.getsize(dest) if os.path.exists(dest) else 0
    
    # 如果本地文件已存在且大小与服务器一致, 跳过下载
    if server_size > 0 and existing == server_size:
        log(f"[OK] 已存在完整文件, 跳过: {filename} ({format_size(existing)})")
        if progress_cb:
            progress_cb(existing, existing, 0, "")
        return True
    
    # 如果本地文件比服务器大, 删除重下
    if server_size > 0 and existing > server_size:
        log(f"[WARN] 本地文件损坏 ({format_size(existing)} > {format_size(server_size)}), 删除重下: {filename}")
        try:
            os.remove(dest)
        except OSError:
            pass
        existing = 0

    headers = {"User-Agent": "xp5-downloader/1.0"}
    resume = existing > 0
    if resume:
        headers["Range"] = f"bytes={existing}-"
        log(f"[INFO] 续传: {filename} (已有 {format_size(existing)})")
    else:
        log(f"[INFO] 下载: {filename}")

    req = urllib.request.Request(url, headers=headers)
    try:
        r = _urlopen(req, timeout)
    except Exception as e:  # noqa
        log(f"[ERROR] 下载失败 {filename}: {e}")
        return False

    with r:
        code = r.getcode()
        clen = r.headers.get("Content-Length")
        clen = int(clen) if clen and clen.isdigit() else None
        # 服务器忽略 Range (返回 200) -> 从头重下
        if resume and code != 206:
            resume = False
            existing = 0
        total = (existing + clen) if (resume and clen) else clen
        mode = "ab" if resume else "wb"
        # 磁盘空间预检: 续传只需剩余部分; 从头下 (wb) 会截断已有文件, 按全量算
        have = existing if mode == "ab" else 0
        if not _disk_ok(dest_dir, max(0, (server_size or total or 0) - have),
                        log, filename):
            return False
        written = existing
        start_time = time.time()
        last_log_pct = -1
        last_emit = 0.0
        try:
            with open(dest, mode) as fh:
                while True:
                    if cancel_cb and cancel_cb():
                        log(f"[WARN] 下载已取消: {filename}")
                        return False
                    # 暂停检查: 等待直到取消或继续
                    while pause_cb and pause_cb():
                        if cancel_cb and cancel_cb():
                            log(f"[WARN] 下载已取消: {filename}")
                            return False
                        time.sleep(0.2)
                    try:
                        chunk = r.read(64 * 1024)  # 64KB chunks, 更频繁检查取消
                    except Exception as e:
                        log(f"[WARN] 连接中断 {filename}: {e}")
                        return False
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    
                    # 实时回调更新进度 (节流: 每 0.25s 一次, 避免高频塞爆 GUI 队列)
                    if progress_cb:
                        now = time.time()
                        if now - last_emit >= 0.25:
                            last_emit = now
                            elapsed = now - start_time
                            speed = (written - existing) / elapsed if elapsed > 0 else 0
                            eta_str = ""
                            if total and speed > 0:
                                eta_sec = (total - written) / speed
                                eta_str = f"{int(eta_sec // 60):02d}:{int(eta_sec % 60):02d}"
                            progress_cb(written, total, speed, eta_str)
                    
                    # 每 10% 打印一次日志
                    if total:
                        pct = written * 100 // total
                        if pct >= last_log_pct + 10:
                            last_log_pct = pct
                            now = time.time()
                            elapsed = now - start_time
                            speed = (written - existing) / elapsed if elapsed > 0 else 0
                            if speed > 0:
                                eta_sec = (total - written) / speed
                                eta_min = int(eta_sec // 60)
                                eta_sec = int(eta_sec % 60)
                                log(f"[INFO] {filename}: {pct}% @ {format_size(speed)}/s ETA {eta_min:02d}:{eta_sec:02d}")
                            else:
                                log(f"[INFO] {filename}: {pct}%")
        except Exception as e:  # noqa
            log(f"[ERROR] 写入中断 {filename}: {e}")
            return False

    # 下载完成后校验完整性: 优先用 HEAD 得到的 server_size, 退化用 Content-Length 推算的 total
    expected = server_size or (total or 0)
    final_size = os.path.getsize(dest) if os.path.exists(dest) else 0
    if not expected:
        # 服务器既没给 HEAD Content-Length 也没给 GET Content-Length:
        # 截断的文件在大小检查这一关看不出来, 只能靠后续 MD5 兜底
        log(f"[WARN] 服务器未提供文件大小, 无法核对完整性 (仅靠 MD5 兜底): {filename}")
    if expected and final_size < expected:
        # 连接中断导致没下完 (不是损坏): 上层会带 Range 续传重试
        log(f"[WARN] 下载不完整 ({format_size(final_size)}/{format_size(expected)}), "
            f"连接中断, 将自动续传重试: {filename}")
        return False
    if server_size and final_size > server_size:
        log(f"[ERROR] 文件超出预期大小 ({format_size(final_size)} > {format_size(server_size)}), "
            f"删除重下: {filename}")
        try:
            os.remove(dest)
        except OSError:
            pass
        return False

    if progress_cb:
        progress_cb(written, total or written, 0, "")
    log(f"[OK] 已下载: {filename} ({format_size(written)})")
    return True


# ---------------------------------------------------------------- MD5 校验
_MD5_RE = re.compile(r"[0-9a-fA-F]{32}")


def _file_md5(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def check_md5(key, path, log=default_log, timeout=30, unverified_out=None):
    """下载后与服务器上的 <url>.md5 校验。服务器无 .md5 或格式不识别时跳过 (返回 True)。

    unverified_out: 可选 list, 跳过校验时把文件名追加进去, 供调用方在结尾汇总
    提醒 —— 直链下载的包常常没有 .md5, 逐条 WARN 容易被刷过去。
    """
    name = os.path.basename(path)

    def _skip(msg):
        log(f"[WARN] {msg}: {name}")
        if unverified_out is not None:
            unverified_out.append(name)
        return True

    url = _resolve_url(key) + ".md5"
    req = urllib.request.Request(url, headers={"User-Agent": "xp5-downloader/1.0"})
    try:
        with _urlopen(req, timeout) as r:
            content = r.read().decode("utf-8", "replace")
    except Exception:  # noqa
        return _skip("服务器无 .md5, 跳过校验")
    m = _MD5_RE.search(content)
    if not m:
        return _skip(".md5 内容无法解析, 跳过校验")
    expect = m.group(0).lower()
    log(f"[INFO] 校验 MD5: {name} ...")
    try:
        actual = _file_md5(path)
    except OSError as e:
        log(f"[ERROR] 读取文件失败: {e}")
        return False
    if actual != expect:
        log(f"[ERROR] MD5 不匹配: 期望 {expect}, 实际 {actual}")
        return False
    log(f"[OK] MD5 校验通过: {name}")
    return True


def download_file(key, dest_dir, log=default_log, timeout=30, cancel_cb=None,
                  pause_cb=None, progress_cb=None, retries=5, verify=True,
                  unverified_out=None):
    """下载单个文件 (含失败自动重试 + MD5 校验)。成功返回 True。

    网络类失败 (含连接中断/卡死超时) 自动重试并带 Range 续传, 不从头下;
    MD5 不匹配则删除文件重下。timeout 为 socket 读超时, 见 _download_once。
    unverified_out: 可选 list, 收集未做完整性校验的文件名, 见 check_md5。
    """
    filename = _filename_of(_resolve_url(key))
    for attempt in range(1, retries + 1):
        if cancel_cb and cancel_cb():
            return False
        ok = _download_once(key, dest_dir, log=log, timeout=timeout,
                            cancel_cb=cancel_cb, pause_cb=pause_cb,
                            progress_cb=progress_cb)
        if cancel_cb and cancel_cb():
            return False
        if ok:
            if not verify:
                if unverified_out is not None:
                    unverified_out.append(filename)
                return True
            if check_md5(key, os.path.join(dest_dir, filename), log,
                         unverified_out=unverified_out):
                return True
            log(f"[ERROR] MD5 校验失败, 删除文件后重下: {filename}")
            try:
                os.remove(os.path.join(dest_dir, filename))
            except OSError:
                pass
            ok = False
        if attempt < retries:
            log(f"[WARN] {filename} 第 {attempt}/{retries} 次尝试失败, 3 秒后重试")
            time.sleep(3)
    log(f"[ERROR] 下载失败 (已重试 {retries} 次): {filename}")
    return False


class _ProgressAgg:
    """并发下载时聚合多个文件的进度, 统一回调 GUI。"""

    def __init__(self, cb):
        self._cb = cb
        self._lock = threading.Lock()
        self._st = {}            # key -> (written, total)
        self._last_t = time.time()
        self._last_w = 0
        self._speed = 0.0

    def update(self, key, written, total):
        with self._lock:
            self._st[key] = (written, total or 0)
            w = sum(a for a, _ in self._st.values())
            t = sum(b for _, b in self._st.values())
            now = time.time()
            dt = now - self._last_t
            if dt < 0.25:
                return
            inst = (w - self._last_w) / dt if dt > 0 else 0
            self._speed = self._speed * 0.6 + inst * 0.4 if self._speed else inst
            self._last_t = now
            self._last_w = w
            eta = ""
            if t and self._speed > 0:
                sec = max(0, (t - w) / self._speed)
                eta = f"{int(sec // 60):02d}:{int(sec % 60):02d}"
            self._cb(w, t, self._speed, eta)


# ---------------------------------------------------------------- 高层流程
def resolve_type(type_str, user_filter=""):
    """返回 (prefix, filter, name_re)。type_str 为空则用 DEBUG 前缀 + 用户 filter。

    用户显式给出 filter 时视为"清楚自己要什么", 不再叠加文件名白名单。
    """
    if not type_str:
        return "DEBUG", user_filter, ""
    if type_str not in TYPE_MAP:
        raise ValueError(
            f"Unknown type: {type_str} (可选 {'|'.join(TYPE_MAP)})")
    prefix, default_filter, name_re = TYPE_MAP[type_str]
    if user_filter:
        return prefix, user_filter, ""
    return prefix, default_filter, name_re


def select_platform_interactive(log=default_log):
    plats = list_platforms(log)
    if not plats:
        log("[ERROR] 没有找到任何车型。")
        sys.exit(1)
    print("\n[INFO] 可选车型:")
    for i, p in enumerate(plats, 1):
        print(f"  {i:3d}. {p}")
    choice = input("\n选择车型编号 (或直接输入车型代码): ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(plats):
        plat = plats[int(choice) - 1]
    else:
        plat = choice
    if not plat:
        log("[ERROR] 无效的车型选择。")
        sys.exit(1)
    log(f"[OK] 车型: {plat}")
    return plat


def _select_files_interactive(files, prefix_path, log=default_log):
    """CLI 交互选择要下的文件; 返回 [key,...]。files=[(key,size)]。"""
    print(f"\n[INFO] 找到 {len(files)} 个文件:\n")
    for i, (key, size) in enumerate(files, 1):
        rel = key[len(prefix_path):] if key.startswith(prefix_path) else os.path.basename(key)
        sz = f"({format_size(size)})" if size else ""
        print(f"  {i:2d}. {rel} {sz}")
    sel = input("\n输入要下载的文件编号 (如 1 3 5, 或 'all'): ").strip()
    if sel.lower() in ("all", "a"):
        return [k for k, _ in files]
    chosen = []
    for tok in sel.split():
        if tok.isdigit() and 1 <= int(tok) <= len(files):
            chosen.append(files[int(tok) - 1][0])
        else:
            log(f"[WARN] 无效编号: {tok} (跳过)")
    return chosen


def _watch_poll(dt, platform, prefix, filter_str, interval_min, timeout_hrs, log, cancel_cb=None,
                name_re=""):
    """轮询直到 DT 目录出现且匹配到文件; 成功返回 True。"""
    start = time.time()
    timeout_sec = timeout_hrs * 3600
    check_sec = interval_min * 60
    attempt = 0
    path = dt_path(platform, prefix, dt)
    print()
    log("[OK] Watch 模式已启动")
    log(f"[INFO] DT: {dt} | 车型: {platform} | filter: {filter_str}")
    log(f"[INFO] 每 {interval_min} 分钟轮询一次 | 超时 {timeout_hrs}h | Ctrl+C 停止\n")
    while True:
        # 检查取消
        if cancel_cb and cancel_cb():
            log("[WARN] 轮询已取消")
            return False
        
        attempt += 1
        elapsed = time.time() - start
        if elapsed >= timeout_sec:
            log(f"[ERROR] 超时 ({timeout_hrs}h), DT {dt} 仍未出包。")
            return False
        log(f"{datetime.now():%Y-%m-%d %H:%M:%S} [POLL #{attempt}] 检查中...")
        try:
            found = find_files(path, filter_str, log, name_re)
        except Exception:  # noqa
            found = []
        if found:
            log(f"[OK] 找到 {len(found)} 个匹配 '{filter_str}' 的文件!")
            for key, size in found:
                log(f"  * {os.path.basename(key)} ({format_size(size)})")
            return True
        remaining = int((timeout_sec - elapsed) / 60)
        log(f"[WARN] 暂无匹配文件, {interval_min} 分钟后重试 (剩余 {remaining} 分钟)")
        nxt = datetime.now() + timedelta(minutes=interval_min)
        log(f"[INFO] 下次轮询: {nxt:%Y-%m-%d %H:%M:%S}\n")
        # 分片 sleep, 避免系统休眠导致长时间卡住, 同时频繁检查取消
        slept = 0
        while slept < check_sec:
            if cancel_cb and cancel_cb():
                log("[WARN] 轮询已取消")
                return False
            time.sleep(min(2, check_sec - slept))  # 每 2 秒检查一次取消
            slept += 2


def _select_dt_files(dt, platform, prefix, filter_str, name_re,
                     all_=True, list_only=False, log=default_log, first_only=False):
    """扫描单个 DT 并选出要下的文件, 返回 [(key, size), ...]。

    没找到任何文件返回 None (调用方计入失败); list_only 打印清单后返回 []。
    first_only: 轮询模式下只取第一个文件 (仅 all_=False 时生效, 与原语义一致)。
    """
    path = dt_path(platform, prefix, dt)
    log(f"[INFO] 扫描 {prefix}-{dt} (车型 {platform}) 的文件...")
    files = find_files(path, filter_str, log, name_re)
    if not files:
        log(f"[ERROR] DT {dt} / 车型 {platform} 没找到文件, 请核对 DT 号与车型。")
        return None

    if list_only:
        for i, (key, size) in enumerate(files, 1):
            rel = key[len(path):] if key.startswith(path) else os.path.basename(key)
            sz = f"({format_size(size)})" if size else ""
            print(f"  {i:2d}. {rel} {sz}")
        return []

    if all_:
        selected = files
        log(f"[OK] 将下载全部 {len(selected)} 个文件。")
    elif first_only and files:
        selected = files[:1]
        log(f"[OK] Watch 模式: 只下载第一个文件 {os.path.basename(selected[0][0])}")
    else:
        chosen = _select_files_interactive(files, path, log)
        by_key = dict(files)
        selected = [(k, by_key[k]) for k in chosen if k in by_key]

    if not selected:
        log(f"[WARN] DT {dt} 未选择任何文件。")
    return selected


def _download_pool(keys, dest_dir, log, cancel_cb=None, pause_cb=None,
                   progress_cb=None, unverified_out=None, header_fn=None):
    """并发下载一批文件/链接 (最多 MAX_PARALLEL 个), 聚合进度。返回失败数。

    只有一个文件时直接下, 不开线程池。header_fn(key) 可选, 在每个文件开始前打
    一条标题日志 (直链用它标明来源)。并发时各文件日志会交错, 属预期。
    """
    if not keys:
        return 0

    def one(k, cb):
        if cancel_cb and cancel_cb():
            return False
        if header_fn:
            log(header_fn(k))
        return download_file(k, dest_dir, log=log, cancel_cb=cancel_cb,
                             pause_cb=pause_cb, progress_cb=cb,
                             unverified_out=unverified_out)

    if len(keys) == 1:
        if cancel_cb and cancel_cb():
            log("[WARN] 下载已取消")
            return 1
        return 0 if one(keys[0], progress_cb) else 1

    agg = _ProgressAgg(progress_cb) if progress_cb else None

    def worker(k):
        cb = (lambda w, t, s, e, kk=k: agg.update(kk, w, t)) if agg else None
        return one(k, cb)

    fail = 0
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(keys))) as ex:
        for res in ex.map(worker, keys):
            if not res:
                fail += 1
    return fail


def download_dt(dt, platform, prefix, filter_str, dest_dir,
                all_=True, list_only=False, log=default_log,
                watch=0, timeout_hrs=DEFAULT_TIMEOUT_HOURS, cancel_cb=None,
                pause_cb=None, progress_cb=None, name_re="", unverified_out=None):
    """处理单个 DT: 扫描 -> 列出 -> (下载)。成功返回 True。

    mcu 类型由调用方把 filter_str 设为 'MCUDEBUG-<dt>.zip' (见 run_download)。
    cancel_cb: 可选, 无参函数, 返回 True 表示应取消下载。
    pause_cb: 可选, 无参函数, 返回 True 表示应暂停下载。
    progress_cb: 可选, 函数签名 (written, total, speed, eta_str), 用于实时更新进度。
    name_re: 可选, 文件名白名单正则, 见 find_files。
    unverified_out: 可选 list, 收集未校验的文件名, 见 check_md5。
    """
    watch_first = False
    if watch > 0:
        if not _watch_poll(dt, platform, prefix, filter_str, watch, timeout_hrs, log,
                           cancel_cb, name_re):
            return False
        watch_first = True

    selected = _select_dt_files(dt, platform, prefix, filter_str, name_re,
                                all_=all_, list_only=list_only, log=log,
                                first_only=watch_first)
    if selected is None:
        return False
    if not selected:
        return True

    log(f"[INFO] 将下载 {len(selected)} 个文件到: {dest_dir}\n")
    return _download_pool([k for k, _ in selected], dest_dir, log, cancel_cb,
                          pause_cb, progress_cb, unverified_out) == 0


def _log_unverified(unverified, log):
    """结尾汇总未做完整性校验的文件 (服务器无 .md5), 避免逐条 WARN 被刷过去。"""
    names = sorted(set(unverified or []))
    if names:
        log(f"[WARN] 以下 {len(names)} 个文件未做完整性校验 (服务器无 .md5), "
            f"刷机前请自行确认: {', '.join(names)}")


def run_download(platform, tasks, dest_dir, log=default_log,
                 list_only=False, all_=True, user_filter="",
                 watch=0, timeout_hrs=DEFAULT_TIMEOUT_HOURS, cancel_cb=None,
                 pause_cb=None, progress_cb=None):
    """批量下载。tasks=[(type_str, dt_string), ...]; dt_string 可含多个空格分隔 DT。

    返回失败数 (0 表示全成功)。
    非轮询模式: 先串行扫完所有 DT (只是目录请求, 很快), 再把全部文件汇总进同一个
    并发池下载 (最多 MAX_PARALLEL 个) —— 跨 DT 并行, 总耗时接近除以并发数。
    轮询模式 (watch>0): 语义就是逐个 DT 等出包再下, 保持串行。
    cancel_cb: 可选, 无参函数, 返回 True 表示应取消下载。
    pause_cb: 可选, 无参函数, 返回 True 表示应暂停下载。
    progress_cb: 可选, 函数签名 (written, total, speed, eta_str), 用于实时更新进度。
    """
    fail = 0
    unverified = []
    plan = []                      # [(key, size, dt, type_str)]

    for type_str, dt_string in tasks:
        if cancel_cb and cancel_cb():
            log("[WARN] 下载已取消")
            _log_unverified(unverified, log)
            return fail + 1
        try:
            prefix, filt, name_re = resolve_type(type_str, user_filter)
        except ValueError as e:
            log(f"[ERROR] {e}")
            fail += 1
            continue
        for dt in dt_string.split():
            if cancel_cb and cancel_cb():
                log("[WARN] 下载已取消")
                _log_unverified(unverified, log)
                return fail + 1
            log(f"\n[INFO] ==== 处理 DT {dt} (type={type_str or '-'}) ====")
            # mcu: 只匹配该 DT 的顶层 zip, 跳过散装 .hex
            dt_filt = f"MCUDEBUG-{dt}.zip" if type_str == "mcu" else filt
            if watch > 0:
                # 轮询: 逐个 DT 等出包并立即下载, 不进并发池
                if not download_dt(dt, platform, prefix, dt_filt, dest_dir,
                                   all_=all_, list_only=list_only, log=log,
                                   watch=watch, timeout_hrs=timeout_hrs,
                                   cancel_cb=cancel_cb, pause_cb=pause_cb,
                                   progress_cb=progress_cb, name_re=name_re,
                                   unverified_out=unverified):
                    fail += 1
                continue
            selected = _select_dt_files(dt, platform, prefix, dt_filt, name_re,
                                        all_=all_, list_only=list_only, log=log)
            if selected is None:
                fail += 1
            else:
                plan.extend((k, s, dt, type_str) for k, s in selected)

    if watch > 0:
        _log_unverified(unverified, log)
        return fail

    if plan:
        total_size = sum(s for _, s, _, _ in plan)
        log(f"\n[INFO] ==== 开始下载 {len(plan)} 个文件 (共 {format_size(total_size)}, "
            f"最多 {MAX_PARALLEL} 个并发) ====")
        for k, s, dt, typ in plan:
            log(f"[INFO]   {os.path.basename(k)} ({format_size(s)})  <- DT {dt}"
                + (f" {typ}" if typ else ""))
        log("")
        fail += _download_pool([k for k, _, _, _ in plan], dest_dir, log,
                               cancel_cb, pause_cb, progress_cb, unverified)

    _log_unverified(unverified, log)
    return fail


def run_download_urls(urls, dest_dir, log=default_log, cancel_cb=None,
                      pause_cb=None, progress_cb=None):
    """直接按完整 URL 下载 (无需 DT 号, 适用于 RC 等直链)。返回失败数 (0=全成功)。

    urls 里的每个元素是完整 http(s) 链接; 复用 download_file 的续传/重试/MD5 逻辑。
    """
    urls = [u.strip() for u in urls if u and u.strip()]
    if not urls:
        log("[ERROR] 没有可下载的链接。")
        return 1
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        log(f"[ERROR] 无法创建下载目录 {dest_dir}: {e}")
        return len(urls)

    unverified = []
    fail = _download_pool(
        urls, dest_dir, log, cancel_cb, pause_cb, progress_cb, unverified,
        header_fn=lambda u: f"\n[INFO] ==== 直链下载: {_filename_of(u)} ====")
    _log_unverified(unverified, log)
    return fail


# ---------------------------------------------------------------- CLI
def build_parser():
    p = argparse.ArgumentParser(
        prog="downloader.py",
        description="按 DT 号从 CI 下载 ROM 刷机包 (download_rom.sh 的 Python 版)。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  downloader.py 169233                        # 交互选择
  downloader.py -p E29 169233                 # 指定车型
  downloader.py -l -p E29 169233              # 只列文件
  downloader.py -p E29 -T vlmu-big -a 196163 196164   # 批量下大包
  downloader.py -p E29 -T mcu -a 60974        # 下 MCU 包
  downloader.py -p E29 -w 30 196163           # 每 30min 轮询, 出包自动下
""")
    p.add_argument("dt", nargs="*", help="一个或多个 DT 号")
    p.add_argument("-p", "--platform", default="", help="车型代码 (如 E29); 不填则列出选择")
    p.add_argument("-l", "--list-only", action="store_true", help="只列文件, 不下载")
    p.add_argument("-d", "--download-dir", default="",
                   help="下载目录 (默认 ./flash_prep/<时间戳>, 每次独立目录)")
    p.add_argument("-f", "--filter", default="", help="按子串过滤文件 (如 ENG_DEV)")
    p.add_argument("-T", "--type", default="", help="包类型: " + "|".join(TYPE_MAP))
    p.add_argument("-k", "--task", action="append", default=[], metavar="TYPE:DT[ DT...]",
                   help="可重复: 一次提交多个带类型的任务 (供 GUI 用), 如 "
                        "'vlmu-big:196163 196164'。给出时忽略位置 DT 与 -T")
    p.add_argument("-a", "--all", action="store_true", help="不询问, 下载全部匹配文件")
    p.add_argument("--url", action="append", default=[], metavar="URL",
                   help="可重复: 直接下载完整 URL (跳过 DT 流程, 适用于 RC 等直链)")
    p.add_argument("-w", "--watch", nargs="?", const=DEFAULT_WATCH_INTERVAL,
                   type=int, default=0, help="轮询模式, 每 N 分钟 (默认 10)")
    p.add_argument("-t", "--timeout", type=int, default=DEFAULT_TIMEOUT_HOURS,
                   help="轮询最长时长(小时), 默认 24")
    p.add_argument("--emit-progress", action="store_true",
                   help="把下载进度以 '@@PROG@@ <written> <total>' 单行输出 (供 GUI 解析)")
    p.add_argument("--insecure", action="store_true",
                   help="不校验 TLS 证书 (等价 curl -k; 仅在无法配置 CA 时使用)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.insecure:
        set_insecure(True)

    # 直链模式: --url 直接给完整链接, 跳过 DT / 车型 / watch 流程
    if args.url:
        dest_dir = args.download_dir or os.path.join(
            "flash_prep", datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(dest_dir, exist_ok=True)
        fail = run_download_urls(args.url, dest_dir, log=default_log)
        print()
        if fail:
            default_log(f"[WARN] {fail} 个链接下载失败。")
        default_log(f"[OK] 下载完成, 文件保存在: {dest_dir}")
        return 1 if fail else 0

    # GUI 模式: --task 直接给出带类型的多任务, 跳过位置 DT / -T / watch 解析
    if args.task:
        return _main_tasks(args)

    # 复刻 bash 的 -w 启发式: -w 后若跟了个 >1440 的数, 那其实是 DT 号
    dts = list(args.dt)
    watch = args.watch
    if watch and watch > 1440:
        dts.append(str(watch))
        watch = DEFAULT_WATCH_INTERVAL
    if not dts:
        print("[ERROR] 至少需要一个 DT 号。", file=sys.stderr)
        build_parser().print_help()
        return 2

    platform = args.platform or select_platform_interactive()

    # watch 模式默认 filter=ENG_DEV
    user_filter = args.filter
    if watch and not user_filter and not args.type:
        user_filter = "ENG_DEV"

    dest_dir = args.download_dir or os.path.join(
        "flash_prep", datetime.now().strftime("%Y%m%d_%H%M%S"))
    if not args.list_only:
        os.makedirs(dest_dir, exist_ok=True)

    if len(dts) > 1:
        default_log(f"[INFO] 批量模式: {len(dts)} 个 DT -> {dest_dir}")

    # CLI 下把多个 DT 归到同一个 type 任务
    tasks = [(args.type, " ".join(dts))]
    fail = run_download(platform, tasks, dest_dir, log=default_log,
                        list_only=args.list_only,
                        all_=args.all, user_filter=user_filter,
                        watch=watch, timeout_hrs=args.timeout)

    print()
    if args.list_only:
        default_log("[INFO] 只列模式, 退出。")
        return 0
    if fail:
        default_log(f"[WARN] {fail} 个 DT 任务无文件或失败。")
    default_log(f"[OK] 下载完成, 文件保存在: {dest_dir}")
    return 1 if fail else 0


def _main_tasks(args):
    """GUI 走这里: 由 --task 'TYPE:DT DT...' 组装多类型任务, 一次进程下载。"""
    tasks = []
    for spec in args.task:
        type_str, _, dts = spec.partition(":")
        dts = dts.strip()
        if not dts:
            print(f"[ERROR] 无效 --task (缺 DT): {spec}", file=sys.stderr)
            return 2
        tasks.append((type_str.strip(), dts))

    platform = args.platform or select_platform_interactive()
    dest_dir = args.download_dir or os.path.join(
        "flash_prep", datetime.now().strftime("%Y%m%d_%H%M%S"))
    if not args.list_only:
        os.makedirs(dest_dir, exist_ok=True)

    fail = run_download(platform, tasks, dest_dir, log=default_log,
                        list_only=args.list_only,
                        all_=args.all, user_filter=args.filter,
                        timeout_hrs=args.timeout)
    print()
    if args.list_only:
        default_log("[INFO] 只列模式, 退出。")
        return 0
    if fail:
        default_log(f"[WARN] {fail} 个 DT 任务无文件或失败。")
    default_log(f"[OK] 下载完成, 文件保存在: {dest_dir}")
    return 1 if fail else 0


def _emit_progress(written, total):
    """进度以单行哨兵输出, 供 GUI 子进程读取 (不刷屏、可实时解析)。"""
    sys.stdout.write(f"@@PROG@@ {int(written)} {int(total or 0)}\n")
    sys.stdout.flush()


def _cli_progress(written, total):
    if total:
        pct = written * 100 // total
        bar = f"{format_size(written)} / {format_size(total)} ({pct}%)"
    else:
        bar = format_size(written)
    sys.stdout.write("\r  " + bar.ljust(48))
    sys.stdout.flush()
    if total and written >= total:
        sys.stdout.write("\n")


if __name__ == "__main__":
    sys.exit(main())
