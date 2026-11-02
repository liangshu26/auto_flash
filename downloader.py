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

# type -> (DT 路径前缀, 默认 filter)。-f 显式给出时覆盖默认 filter。
TYPE_MAP = {
    "vlmu-big":   ("DEBUG",    "VLMU/ENG_"),
    "vlmu-small": ("DEBUG",    "VLMU/vlmu_xpilot_release"),
    "xpu-big":    ("DEBUG",    "XPU/ENG_"),
    "xpu-small":  ("DEBUG",    "XPU/xpilot_release_full"),
    "mcu":        ("MCUDEBUG", "XPilot_AURIX"),
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


def find_files(path, filter_str="", log=default_log):
    """递归列出 path 下匹配 filter 的文件, 返回 [(key, size), ...]。

    跳过 .md5; filter 为大小写不敏感的子串匹配 (空则不过滤)。
    """
    results = []

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
            results.append((key, f.get("size", 0)))

    rec(path)
    return results


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


def check_md5(key, path, log=default_log, timeout=30):
    """下载后与服务器上的 <url>.md5 校验。服务器无 .md5 或格式不识别时跳过 (返回 True)。"""
    url = _resolve_url(key) + ".md5"
    req = urllib.request.Request(url, headers={"User-Agent": "xp5-downloader/1.0"})
    try:
        with _urlopen(req, timeout) as r:
            content = r.read().decode("utf-8", "replace")
    except Exception:  # noqa
        log(f"[WARN] 服务器无 .md5, 跳过校验: {os.path.basename(path)}")
        return True
    m = _MD5_RE.search(content)
    if not m:
        log(f"[WARN] .md5 内容无法解析, 跳过校验: {os.path.basename(path)}")
        return True
    expect = m.group(0).lower()
    log(f"[INFO] 校验 MD5: {os.path.basename(path)} ...")
    try:
        actual = _file_md5(path)
    except OSError as e:
        log(f"[ERROR] 读取文件失败: {e}")
        return False
    if actual != expect:
        log(f"[ERROR] MD5 不匹配: 期望 {expect}, 实际 {actual}")
        return False
    log(f"[OK] MD5 校验通过: {os.path.basename(path)}")
    return True


def download_file(key, dest_dir, log=default_log, timeout=30, cancel_cb=None,
                  pause_cb=None, progress_cb=None, retries=5, verify=True):
    """下载单个文件 (含失败自动重试 + MD5 校验)。成功返回 True。

    网络类失败 (含连接中断/卡死超时) 自动重试并带 Range 续传, 不从头下;
    MD5 不匹配则删除文件重下。timeout 为 socket 读超时, 见 _download_once。
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
                return True
            if check_md5(key, os.path.join(dest_dir, filename), log):
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
    """返回 (prefix, filter)。type_str 为空则用 DEBUG 前缀 + 用户 filter。"""
    if not type_str:
        return "DEBUG", user_filter
    if type_str not in TYPE_MAP:
        raise ValueError(
            f"Unknown type: {type_str} (可选 {'|'.join(TYPE_MAP)})")
    prefix, default_filter = TYPE_MAP[type_str]
    return prefix, (user_filter or default_filter)


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


def _watch_poll(dt, platform, prefix, filter_str, interval_min, timeout_hrs, log, cancel_cb=None):
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
            found = find_files(path, filter_str, log)
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


def download_dt(dt, platform, prefix, filter_str, dest_dir,
                all_=True, list_only=False, log=default_log,
                watch=0, timeout_hrs=DEFAULT_TIMEOUT_HOURS, cancel_cb=None,
                pause_cb=None, progress_cb=None):
    """处理单个 DT: 扫描 -> 列出 -> (下载)。成功返回 True。

    mcu 类型由调用方把 filter_str 设为 'MCUDEBUG-<dt>.zip' (见 run_download)。
    cancel_cb: 可选, 无参函数, 返回 True 表示应取消下载。
    pause_cb: 可选, 无参函数, 返回 True 表示应暂停下载。
    progress_cb: 可选, 函数签名 (written, total, speed, eta_str), 用于实时更新进度。
    """
    watch_first = False
    if watch > 0:
        if not _watch_poll(dt, platform, prefix, filter_str, watch, timeout_hrs, log, cancel_cb):
            return False
        watch_first = True

    path = dt_path(platform, prefix, dt)
    log(f"[INFO] 扫描 {prefix}-{dt} (车型 {platform}) 的文件...")
    files = find_files(path, filter_str, log)
    if not files:
        log(f"[ERROR] DT {dt} / 车型 {platform} 没找到文件, 请核对 DT 号与车型。")
        return False

    if list_only:
        for i, (key, size) in enumerate(files, 1):
            rel = key[len(path):] if key.startswith(path) else os.path.basename(key)
            sz = f"({format_size(size)})" if size else ""
            print(f"  {i:2d}. {rel} {sz}")
        return True

    if all_:
        selected = [k for k, _ in files]
        log(f"[OK] 将下载全部 {len(selected)} 个文件。")
    elif watch_first:
        selected = [files[0][0]]
        log(f"[OK] Watch 模式: 只下载第一个文件 {os.path.basename(selected[0])}")
    else:
        selected = _select_files_interactive(files, path, log)

    if not selected:
        log(f"[WARN] DT {dt} 未选择任何文件。")
        return True

    log(f"[INFO] 将下载 {len(selected)} 个文件到: {dest_dir}\n")
    if len(selected) == 1:
        key = selected[0]
        if cancel_cb and cancel_cb():
            log("[WARN] 下载已取消")
            return False
        return download_file(key, dest_dir, log=log, cancel_cb=cancel_cb,
                             pause_cb=pause_cb, progress_cb=progress_cb)

    # 多文件: 最多 3 个并发 (跨文件并发不拖慢单文件, 总耗时更短)
    agg = _ProgressAgg(progress_cb) if progress_cb else None

    def one(k):
        if cancel_cb and cancel_cb():
            return False
        cb = (lambda w, t, s, e, kk=k: agg.update(kk, w, t)) if agg else None
        return download_file(k, dest_dir, log=log, cancel_cb=cancel_cb,
                             pause_cb=pause_cb, progress_cb=cb)

    ok = True
    with ThreadPoolExecutor(max_workers=min(3, len(selected))) as ex:
        for res in ex.map(one, selected):
            ok = ok and res
    return ok


def run_download(platform, tasks, dest_dir, log=default_log,
                 list_only=False, all_=True, user_filter="",
                 watch=0, timeout_hrs=DEFAULT_TIMEOUT_HOURS, cancel_cb=None,
                 pause_cb=None, progress_cb=None):
    """批量下载。tasks=[(type_str, dt_string), ...]; dt_string 可含多个空格分隔 DT。

    返回失败的 DT 任务数 (0 表示全成功)。
    cancel_cb: 可选, 无参函数, 返回 True 表示应取消下载。
    pause_cb: 可选, 无参函数, 返回 True 表示应暂停下载。
    progress_cb: 可选, 函数签名 (written, total, speed, eta_str), 用于实时更新进度。
    """
    fail = 0
    for type_str, dt_string in tasks:
        if cancel_cb and cancel_cb():
            log("[WARN] 下载已取消")
            return fail + 1
        try:
            prefix, filt = resolve_type(type_str, user_filter)
        except ValueError as e:
            log(f"[ERROR] {e}")
            fail += 1
            continue
        for dt in dt_string.split():
            if cancel_cb and cancel_cb():
                log("[WARN] 下载已取消")
                return fail + 1
            log(f"\n[INFO] ==== 处理 DT {dt} (type={type_str or '-'}) ====")
            # mcu: 只匹配该 DT 的顶层 zip, 跳过散装 .hex
            if type_str == "mcu":
                filt = f"MCUDEBUG-{dt}.zip"
            if not download_dt(dt, platform, prefix, filt, dest_dir,
                               all_=all_, list_only=list_only, log=log,
                               watch=watch, timeout_hrs=timeout_hrs,
                               cancel_cb=cancel_cb, pause_cb=pause_cb,
                               progress_cb=progress_cb):
                fail += 1
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

    if len(urls) == 1:
        u = urls[0]
        log(f"\n[INFO] ==== 直链下载: {_filename_of(u)} ====")
        if cancel_cb and cancel_cb():
            log("[WARN] 下载已取消")
            return 1
        return 0 if download_file(u, dest_dir, log=log, cancel_cb=cancel_cb,
                                  pause_cb=pause_cb, progress_cb=progress_cb) else 1

    # 多链接: 最多 3 个并发, 聚合进度
    agg = _ProgressAgg(progress_cb) if progress_cb else None

    def one(u):
        if cancel_cb and cancel_cb():
            return False
        log(f"\n[INFO] ==== 直链下载: {_filename_of(u)} ====")
        cb = (lambda w, t, s, e, uu=u: agg.update(uu, w, t)) if agg else None
        return download_file(u, dest_dir, log=log, cancel_cb=cancel_cb,
                             pause_cb=pause_cb, progress_cb=cb)

    fail = 0
    with ThreadPoolExecutor(max_workers=min(3, len(urls))) as ex:
        for res in ex.map(one, urls):
            if not res:
                fail += 1
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
