#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""transport.py — 设备命令传输层 (adb / ssh)。

把"在板子上跑命令 / 推文件 / 判在线"抽象成统一接口, 让 mcu.py 与 flasher.py 的
刷写逻辑既能走 adb 也能走 ssh, 而不必在每个调用点写 if/else。

adb 路径的 argv 与文案与重构前逐字一致; ssh 路径复用 common._ssh_reachable 的
认证/hostkey/日志选项集 (见 _ssh_opts, 有 parity 单测守着)。
"""

import hashlib
import os
import re
import subprocess
import time

import common

SSH_CONNECT_TIMEOUT = 3       # 与 common._ssh_reachable 默认一致
SSH_IO_TIMEOUT = 10           # scp / 远端命令的连接超时
# 长命令防挂死: 刷机不可取消又无 watchdog, 连接半开时靠 keepalive 及时报错。
_KEEPALIVE = ("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4")


def _ssh_opts(connect_timeout):
    """与 common._ssh_reachable 完全一致的选项 (顺序也一致)。"""
    return ("-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={connect_timeout}",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR")


def ssh_argv(ip, cmd, connect_timeout=SSH_CONNECT_TIMEOUT, keepalive=False, no_tty=False):
    """ssh 远端执行 cmd。cmd 作为单个 argv 元素, 由远端 login shell 解析引号/重定向。"""
    argv = ["ssh", "-p", str(common.SSH_PORT)]
    argv += list(_ssh_opts(connect_timeout))
    if keepalive:
        argv += list(_KEEPALIVE)
    if no_tty:
        argv.append("-T")
    argv += [f"root@{ip}", cmd]
    return argv


def scp_argv(ip, local, remote, connect_timeout=SSH_IO_TIMEOUT, keepalive=True):
    """scp 推单文件。-P 大写指定端口; -O 强制传统 SCP 协议 (嵌入式板常无 sftp-server)。"""
    argv = ["scp", "-P", str(common.SSH_PORT), "-O"]
    argv += list(_ssh_opts(connect_timeout))
    if keepalive:
        argv += list(_KEEPALIVE)
    argv += [os.path.abspath(local), f"root@{ip}:{remote}"]
    return argv


# ---------------------------------------------------------------- 掉线噪声
def adb_offline_noise(line):
    """adb 因设备离线/重启打出的错误噪声 (原 flasher._adb_offline)。"""
    s = line.strip()
    return (s.startswith("error:") or "device not found" in s
            or "device offline" in s or "no devices/emulators found" in s)


_SSH_NOISE = (
    "ssh: connect to host", "Connection closed by", "Connection reset by peer",
    "kex_exchange_identification", "Connection timed out", "lost connection",
    "No route to host", "Network is unreachable", "Permission denied (publickey",
    "Host key verification failed", "Could not resolve hostname",
    "scp: Connection closed", "subsystem request failed", "Timeout, server",
)


def ssh_offline_noise(line):
    """ssh/scp 连接断开/不可达的噪声。不得误滤远端真实输出 (进度/Success/Fail!!!)。"""
    s = line.strip()
    return any(p in s for p in _SSH_NOISE)


# ---------------------------------------------------------------- 传输基类
class Transport:
    name = "transport"
    push_name = "push"

    def __init__(self, serial):
        self.serial = serial

    # 子类提供 argv 构造与在线/噪声判定
    def shell_argv(self, cmd):
        raise NotImplementedError

    def push_argv(self, local, remote):
        raise NotImplementedError

    def spawn_argv(self, cmd):
        raise NotImplementedError

    def is_online(self):
        raise NotImplementedError

    def is_offline_noise(self, line):
        raise NotImplementedError

    def is_transport_drop_rc(self, rc):
        return False

    # 共享实现: adb / ssh 只有 argv 不同
    def shell_stream(self, cmd, log):
        return common.run_streaming(self.shell_argv(cmd), log=log)

    def push(self, local, remote, log):
        return common.run_streaming(self.push_argv(local, remote), log=log)

    def shell_capture(self, cmd, timeout=60):
        """跑命令只取 stdout (清 NUL/CR); 出错返回空串。stderr 不混入。"""
        try:
            r = subprocess.run(
                self.shell_argv(cmd), capture_output=True, text=True,
                errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
            )
        except Exception:  # noqa
            return ""
        return common.strip_ctrl(r.stdout)

    def spawn(self, cmd):
        """后台执行 (DEVNULL×3), 调用方负责 track_proc/untrack_proc。"""
        return subprocess.Popen(
            self.spawn_argv(cmd), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )

    def wait_online(self, log, retries=60, interval=5):
        """等待设备在线; 已在线立即返回 True, 超时返回 False (文案与原 mcu 一致)。"""
        if self.is_online():
            return True
        log(f"[WARN] device {self.serial} offline, waiting for it to come back...")
        for _ in range(retries):
            time.sleep(interval)
            if self.is_online():
                return True
        log(f"[ERROR] device {self.serial} still offline after {retries * interval}s")
        return False


class AdbTransport(Transport):
    name = "adb"
    push_name = "adb push"

    def shell_argv(self, cmd):
        return ["adb", "-s", self.serial, "shell", cmd]

    def push_argv(self, local, remote):
        return ["adb", "-s", self.serial, "push", local, remote]

    def spawn_argv(self, cmd):
        return ["adb", "-s", self.serial, "shell", cmd]

    def is_online(self):
        try:
            r = subprocess.run(
                ["adb", "-s", self.serial, "get-state"],
                capture_output=True, text=True, errors="replace",
                timeout=15, stdin=subprocess.DEVNULL,
            )
        except Exception:  # noqa
            return False
        return common.strip_ctrl(r.stdout).strip() == "device"

    def is_offline_noise(self, line):
        return adb_offline_noise(line)


class SshTransport(Transport):
    name = "ssh"
    push_name = "scp"

    def shell_argv(self, cmd):
        return ssh_argv(self.serial, cmd, keepalive=True)

    def push_argv(self, local, remote):
        return scp_argv(self.serial, local, remote)

    def spawn_argv(self, cmd):
        return ssh_argv(self.serial, cmd, keepalive=True, no_tty=True)

    def is_online(self):
        return common._ssh_reachable(self.serial, common.SSH_PORT, SSH_CONNECT_TIMEOUT)

    def is_offline_noise(self, line):
        return ssh_offline_noise(line)

    def is_transport_drop_rc(self, rc):
        return rc == 255


def for_device(serial, use_ssh=False):
    """按接入方式选传输。use_ssh 为真或 serial 是已知 ssh IP 时走 ssh, 否则 adb。"""
    if use_ssh or serial in common.SSH_DEVICES:
        return SshTransport(serial)
    return AdbTransport(serial)


# ---------------------------------------------------------------- ssh 推送安全闸
_SAFE_BASENAME = re.compile(r"^[A-Za-z0-9._+-]+$")


def basename_ok(name):
    """远端路径会经过远端 shell, 文件名只允许安全字符 (防展开/注入)。"""
    return bool(_SAFE_BASENAME.match(name))


def remote_md5_ok(t, local, remote, log):
    """scp 不像 adb push 那样有传输校验, 推完比对远端 md5 与本地, 防截断固件被刷入。"""
    with open(local, "rb") as f:
        local_md5 = hashlib.md5(f.read()).hexdigest()
    out = t.shell_capture(f"md5sum {remote}")
    parts = out.split()
    remote_md5 = parts[0] if parts else ""
    if remote_md5 != local_md5:
        log(f"[ERROR] 远端 md5 校验失败: {remote} (本地 {local_md5}, 远端 {remote_md5!r})")
        return False
    return True