#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XP5 刷机工具 · 图形界面 (tkinter)

跨平台 (Linux / macOS) 前端。下载/刷机都在子线程里跑，通过队列更新 UI：
  - 下载: downloader.run_download
  - 刷机: flasher.run_flash / flasher.execute_jobs
adb 设备识别、包分类等共享逻辑走 common.py。

用法: python3 flash_gui.py
依赖: 仅需标准库 tkinter (mac 自带; Linux 若缺: sudo apt install python3-tk)
"""

import os
import sys
import time
import queue
import threading
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText

import common
import flasher
import downloader

# ---------------------------------------------------------------- 路径 / 常量
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PREP_ROOT = common.default_prep_root()

MODE_TO_TARGET = common.MODE_TO_TARGET


# ---------------------------------------------------------------- 主程序
class App:
    def __init__(self, root):
        self.root = root
        root.title("XP5 刷机工具")
        root.geometry("960x760")
        root.minsize(780, 600)

        self._q = queue.Queue()
        self._busy = False
        self._dev_busy = False
        self._devices = []
        self._cancelled = False
        self._paused = False
        self._task_started_at = None
        self._build_ui()
        self._poll()
        self.refresh_devices()
        self._reload_dir_suggestions(select_first=True)

    # ------------------------------------------------------------ UI 构建
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        # 顶部: 设备栏
        top = ttk.LabelFrame(self.root, text="设备 (adb)")
        top.pack(fill="x", **pad)
        self.dev_var = tk.StringVar(value="正在检测设备...")
        ttk.Label(top, textvariable=self.dev_var, justify="left",
                  wraplength=740).pack(side="left", fill="x", expand=True, padx=8, pady=6)
        self.btn_refresh_dev = ttk.Button(top, text="刷新设备", command=self.refresh_devices)
        self.btn_refresh_dev.pack(side="right", padx=8, pady=6)

        # 中部: 选项卡
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, **pad)
        self.tab_download = ttk.Frame(nb)
        self.tab_flash = ttk.Frame(nb)
        nb.add(self.tab_download, text="  下载刷机包  ")
        nb.add(self.tab_flash, text="  刷机  ")
        self._build_download_tab(self.tab_download)
        self._build_flash_tab(self.tab_flash)

        # 底部: 状态 + 日志
        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(fill="both", expand=True, **pad)
        self.log = ScrolledText(logf, height=13, wrap="word", state="disabled",
                                bg="#111", fg="#ddd", insertbackground="#ddd")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        for tag, color in (("cmd", "#7fd0ff"), ("ok", "#8ce08c"),
                           ("err", "#ff8080"), ("dim", "#888")):
            self.log.tag_config(tag, foreground=color)
        bar = ttk.Frame(logf)
        bar.pack(fill="x", padx=6, pady=(0, 6))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var, foreground="#555").pack(side="left")
        ttk.Button(bar, text="清空日志", command=self._clear_log).pack(side="right")
        self.btn_cancel = ttk.Button(bar, text="取消", command=self._on_cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="right", padx=6)
        self.btn_pause = ttk.Button(bar, text="暂停", command=self._on_pause,
                                    state="disabled")
        self.btn_pause.pack(side="right", padx=6)

    # ---------------- 下载 tab ----------------
    def _build_download_tab(self, parent):
        pad = {"padx": 8, "pady": 4}
        row = ttk.Frame(parent)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="车型 (如 E29):").pack(side="left")
        self.platform_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.platform_var, width=16).pack(side="left", padx=6)
        ttk.Label(row, text="(必填)", foreground="#888").pack(side="left")

        # 下载目录 (可自由选择; 留空=自动时间戳)
        drow = ttk.Frame(parent)
        drow.pack(fill="x", **pad)
        ttk.Label(drow, text="下载目录:").pack(side="left")
        self.dl_dir_var = tk.StringVar()
        ttk.Entry(drow, textvariable=self.dl_dir_var, width=46).pack(side="left", padx=6)
        ttk.Button(drow, text="浏览...", command=self._browse_dl_dir).pack(side="left")
        ttk.Button(drow, text="自动(时间戳)",
                   command=lambda: self.dl_dir_var.set("")).pack(side="left", padx=4)
        ttk.Label(drow, text="留空 = 新建 flash_prep/<时间戳>/",
                  foreground="#888").pack(side="left", padx=6)

        # DT 号输入 (vlmu/xpu 有大包小包两个框, mcu 只有一个)
        self.dt_vars = {}
        for name in ("vlmu", "xpu"):
            f = ttk.LabelFrame(parent, text=f" {name} ")
            f.pack(fill="x", **pad)
            dt_big = tk.StringVar()
            dt_small = tk.StringVar()
            ttk.Label(f, text="大包 DT:").grid(row=0, column=0, sticky="e", padx=8, pady=4)
            ttk.Entry(f, textvariable=dt_big, width=40).grid(row=0, column=1, sticky="w", pady=4)
            ttk.Label(f, text="(可多个, 空格分隔)", foreground="#888").grid(
                row=0, column=2, sticky="w", padx=6)
            ttk.Label(f, text="小包 DT:").grid(row=1, column=0, sticky="e", padx=8, pady=4)
            ttk.Entry(f, textvariable=dt_small, width=40).grid(row=1, column=1, sticky="w", pady=4)
            ttk.Label(f, text="(可多个, 空格分隔)", foreground="#888").grid(
                row=1, column=2, sticky="w", padx=6)
            self.dt_vars[name] = {"big": dt_big, "small": dt_small}

        f = ttk.LabelFrame(parent, text=" mcu ")
        f.pack(fill="x", **pad)
        dt_mcu = tk.StringVar()
        ttk.Label(f, text="MCUDEBUG DT:").grid(row=0, column=0, sticky="e", padx=8, pady=4)
        ttk.Entry(f, textvariable=dt_mcu, width=40).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(f, text="(可多个, 空格分隔)", foreground="#888").grid(
            row=0, column=2, sticky="w", padx=6)
        self.dt_vars["mcu"] = {"mcu": dt_mcu}

        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=8, pady=10)
        self.btn_download = ttk.Button(bar, text="开始下载", command=self.on_download)
        self.btn_download.pack(side="left")

    def _browse_dl_dir(self):
        d = filedialog.askdirectory(title="选择下载目录",
                                    initialdir=(self.dl_dir_var.get() or PREP_ROOT or SCRIPT_DIR))
        if d:
            self.dl_dir_var.set(d)

    # ---------------- 刷机 tab ----------------
    def _build_flash_tab(self, parent):
        pad = {"padx": 8, "pady": 4}
        # 目录选择 (可自由浏览任意目录)
        row = ttk.Frame(parent)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="下载目录:").pack(side="left")
        self.dir_var = tk.StringVar()
        self.dir_box = ttk.Combobox(row, textvariable=self.dir_var, width=54)  # 可编辑
        self.dir_box.pack(side="left", padx=6)
        self.dir_box.bind("<<ComboboxSelected>>", lambda e: self._load_dir_packages())
        self.dir_box.bind("<Return>", lambda e: self._load_dir_packages())
        ttk.Button(row, text="浏览...", command=self._browse_flash_dir).pack(side="left")
        self.btn_refresh_sess = ttk.Button(row, text="刷新列表",
                                           command=self._reload_dir_suggestions)
        self.btn_refresh_sess.pack(side="left", padx=4)

        # 包列表 (勾选要刷的包)
        pf = ttk.LabelFrame(parent, text="刷机包列表 (勾选要刷的包)")
        pf.pack(fill="both", expand=True, **pad)
        
        # 创建可滚动的包列表
        canvas_frame = ttk.Frame(pf)
        canvas_frame.pack(fill="both", expand=True, padx=8, pady=4)
        
        self._pkg_canvas = tk.Canvas(canvas_frame, height=200, bg="white")
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self._pkg_canvas.yview)
        self._pkg_list_frame = ttk.Frame(self._pkg_canvas)
        
        self._pkg_list_frame.bind(
            "<Configure>",
            lambda e: self._pkg_canvas.configure(scrollregion=self._pkg_canvas.bbox("all"))
        )
        
        self._pkg_canvas.create_window((0, 0), window=self._pkg_list_frame, anchor="nw")
        self._pkg_canvas.configure(yscrollcommand=scrollbar.set)
        
        self._pkg_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # xp5 设备选择 (A/B 可多选)
        r = ttk.Frame(pf)
        r.pack(fill="x", padx=8, pady=4)
        ttk.Label(r, text="xp5 设备:").pack(side="left")
        self.xp5_a_var = tk.BooleanVar(value=True)
        self.xp5_b_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="A", variable=self.xp5_a_var).pack(side="left", padx=4)
        ttk.Checkbutton(r, text="B", variable=self.xp5_b_var).pack(side="left", padx=4)
        
        # 刷机按钮
        cf = ttk.Frame(pf)
        cf.pack(fill="x", padx=8, pady=4)
        self.btn_flash_all = ttk.Button(cf, text="刷勾选的包", command=self.on_flash_selected)
        self.btn_flash_all.pack(side="left", padx=12)
        
        # 存储包复选框
        self._pkg_checkboxes = {}  # {key: BooleanVar}
        self._pkg_types = {}  # {key: pkg_type}
        self._pkg_paths = {}  # {key: full_path} (压缩包路径或已解压目录路径)

    def _browse_flash_dir(self):
        d = filedialog.askdirectory(title="选择刷机包目录",
                                    initialdir=(self.dir_var.get() or PREP_ROOT or SCRIPT_DIR))
        if d:
            self.dir_var.set(d)
            self._load_dir_packages()

    def _load_dir_packages(self):
        """加载目录中的包并显示复选框列表。"""
        dirpath = self.dir_var.get().strip()
        if not dirpath or not os.path.isdir(dirpath):
            return
        
        # 清空现有复选框
        for widget in self._pkg_list_frame.winfo_children():
            widget.destroy()
        self._pkg_checkboxes.clear()
        self._pkg_types.clear()
        self._pkg_paths.clear()
        
        # 扫描压缩包和已解压目录
        cats = common.scan_dir(dirpath)
        extracted_cats = common.scan_extracted_dir(dirpath)
        
        # 按类别显示包
        type_labels = {
            "vlmu_big": "VLMU 大包",
            "vlmu_small": "VLMU 小包",
            "xp5_big": "XP5 大包",
            "xp5_small": "XP5 小包",
            "mcu": "MCU",
            "vlmu_mcu": "VLMU MCU"
        }
        
        row = 0
        for cat_key in ["vlmu_big", "vlmu_small", "xp5_big", "xp5_small", "mcu", "vlmu_mcu"]:
            pkgs = cats.get(cat_key, [])
            extracted = extracted_cats.get(cat_key, [])
            
            if not pkgs and not extracted:
                continue
            
            # 类别标题
            ttk.Label(self._pkg_list_frame, text=f"【{type_labels[cat_key]}】", 
                     font=("", 9, "bold")).grid(row=row, column=0, sticky="w", padx=4, pady=(8, 2))
            row += 1
            
            # 已解压目录 (优先显示)
            for dir_name, full_path in extracted:
                var = tk.BooleanVar(value=False)
                cb = ttk.Checkbutton(self._pkg_list_frame, text=f"{dir_name} [已解压]", 
                                    variable=var)
                cb.grid(row=row, column=0, sticky="w", padx=20, pady=1)
                # 用目录名作为 key, 加前缀避免与压缩包冲突
                key = f"[extracted]{dir_name}"
                self._pkg_checkboxes[key] = var
                self._pkg_types[key] = cat_key
                self._pkg_paths[key] = full_path  # 已解压目录的完整路径
                row += 1
            
            # 压缩包
            for pkg_name in pkgs:
                var = tk.BooleanVar(value=False)
                cb = ttk.Checkbutton(self._pkg_list_frame, text=pkg_name, variable=var)
                cb.grid(row=row, column=0, sticky="w", padx=20, pady=1)
                self._pkg_checkboxes[pkg_name] = var
                self._pkg_types[pkg_name] = cat_key
                self._pkg_paths[pkg_name] = os.path.join(dirpath, pkg_name)
                row += 1
        
        if row == 0:
            ttk.Label(self._pkg_list_frame, text="(目录中没有找到刷机包)", 
                     foreground="#888").grid(row=0, column=0, padx=4, pady=4)

    # ------------------------------------------------------------ 设备
    def refresh_devices(self):
        if self._dev_busy:
            return
        self._dev_busy = True
        self.dev_var.set("正在检测设备...")

        def worker():
            devs = common.detect_devices()          # 重活只在子线程
            self._q.put(("ui", lambda: self._apply_devices(devs)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_devices(self, devs):
        self._dev_busy = False
        self._devices = devs
        if not devs:
            self.dev_var.set('未检测到在线设备 (接好 adb 线后点"刷新设备")')
            return
        self.dev_var.set("\n".join(
            f"{s}  [{m}] -> {MODE_TO_TARGET.get(m, '-')}" for s, m in devs))

    # ------------------------------------------------------------ 目录 / 包
    def _reload_dir_suggestions(self, select_first=False):
        paths = [p for _, p in common.list_session_dirs(PREP_ROOT)]
        self.dir_box["values"] = paths
        cur = self.dir_var.get()
        if select_first or (not cur and paths):
            if paths:
                self.dir_var.set(paths[0])
        # 加载当前目录的包
        self._load_dir_packages()

    # ------------------------------------------------------------ 下载
    def _collect_download_tasks(self):
        platform = self.platform_var.get().strip()
        if not platform:
            return None, [], "请先填车型 (如 E29)。"
        tasks = []
        # vlmu: 大包+小包 (分别填 DT)
        vlmu = self.dt_vars["vlmu"]
        vlmu_big = vlmu["big"].get().strip()
        vlmu_small = vlmu["small"].get().strip()
        if vlmu_big:
            tasks.append(("vlmu-big", vlmu_big))
        if vlmu_small:
            tasks.append(("vlmu-small", vlmu_small))
        # xpu: 大包+小包 (分别填 DT)
        xpu = self.dt_vars["xpu"]
        xpu_big = xpu["big"].get().strip()
        xpu_small = xpu["small"].get().strip()
        if xpu_big:
            tasks.append(("xpu-big", xpu_big))
        if xpu_small:
            tasks.append(("xpu-small", xpu_small))
        # mcu
        mcu_dt = self.dt_vars["mcu"]["mcu"].get().strip()
        if mcu_dt:
            tasks.append(("mcu", mcu_dt))
        if not tasks:
            return None, [], "没有可下载的任务 (至少填一个 DT 号)。"
        return platform, tasks, None

    def on_download(self):
        if self._busy:
            return
        platform, tasks, err = self._collect_download_tasks()
        if err:
            self._log("err", f"[ERROR] 无法下载: {err}\n")
            self._modal_ask(messagebox.showwarning, "无法下载", err)
            return
        custom = self.dl_dir_var.get().strip()
        session_dir = custom or os.path.join(PREP_ROOT, datetime.now().strftime("%Y%m%d_%H%M%S"))

        plan = "\n".join(f"  {t}  DT: {dts}" for t, dts in tasks)
        if not self._modal_ask(
                messagebox.askyesno,
                "确认下载", f"车型: {platform}\n\n下载计划:\n{plan}\n\n"
                            f"目标目录:\n  {session_dir}\n\n开始下载?"):
            return

        try:
            os.makedirs(session_dir, exist_ok=True)
        except OSError as e:
            self._modal_ask(messagebox.showerror, "失败", f"无法创建下载目录: {e}")
            return

        def job(log, progress_cb):
            fail = downloader.run_download(
                platform, tasks, session_dir, log=log,
                all_=True, cancel_cb=lambda: self._cancelled,
                pause_cb=lambda: self._paused,
                progress_cb=progress_cb)
            return fail == 0

        def after():
            self._reload_dir_suggestions()
            self.dir_var.set(session_dir)

        self._run_task(job, f"下载到 {session_dir}", after=after)

    # ------------------------------------------------------------ 刷机
    def on_flash_selected(self):
        """刷写勾选的包。"""
        if self._busy:
            self._log("dim", f"[INFO] 当前有任务在运行, 请等待结束后再操作\n")
            return
        dirpath = self.dir_var.get().strip()
        if not dirpath or not os.path.isdir(dirpath):
            self._log("err", f"[ERROR] 无法刷机: 请先选择有效的下载目录\n")
            self._modal_ask(messagebox.showwarning, "无法刷机", "请先选择有效的下载目录。")
            return
        
        # 收集勾选的包
        selected = []
        for key, var in self._pkg_checkboxes.items():
            if var.get():
                pkg_type = self._pkg_types[key]
                pkg_path = self._pkg_paths[key]  # 使用存储的完整路径
                is_extracted = key.startswith("[extracted]")
                selected.append((key, pkg_type, pkg_path, is_extracted))
        
        if not selected:
            self._log("err", f"[ERROR] 无法刷机: 请至少勾选一个包\n")
            self._modal_ask(messagebox.showwarning, "无法刷机", "请至少勾选一个包。")
            return
        
        # 确定 xp5 目标设备 (仅当有 xp5 包时需要)
        has_xp5_pkg = any(pt in ("xp5_big", "xp5_small") for _, pt, _, _ in selected)
        xp5_targets = []
        if has_xp5_pkg:
            if self.xp5_a_var.get():
                xp5_targets.append("xp5_a")
            if self.xp5_b_var.get():
                xp5_targets.append("xp5_b")
            if not xp5_targets:
                self._log("err", f"[ERROR] 无法刷机: 选择了 XP5 包但未选择目标设备 (A 或 B)\n")
                self._modal_ask(messagebox.showwarning, "无法刷机", "选择了 XP5 包，请至少选择一个目标设备 (A 或 B)。")
                return
        
        # 构建 job 列表
        jobs = []
        files = []
        for key, pkg_type, pkg_path, is_extracted in selected:
            display_name = os.path.basename(pkg_path)
            if is_extracted:
                display_name += " [已解压]"
            
            if pkg_type == "mcu":
                jobs.append(("mcu", "mcu", pkg_path, is_extracted))
                files.append(f"{display_name} -> mcu")
            elif pkg_type == "vlmu_mcu":
                jobs.append(("vlmu_mcu", "vlmu", pkg_path, is_extracted))
                files.append(f"{display_name} -> vlmu mcu")
            elif pkg_type in ("vlmu_big", "vlmu_small"):
                kind = "big" if "big" in pkg_type else "small"
                jobs.append((kind, "vlmu", pkg_path, is_extracted))
                files.append(f"{display_name} -> vlmu ({pkg_type})")
            elif pkg_type in ("xp5_big", "xp5_small"):
                kind = "big" if "big" in pkg_type else "small"
                for target in xp5_targets:
                    jobs.append((kind, target, pkg_path, is_extracted))
                    files.append(f"{display_name} -> {target} ({pkg_type})")
            else:
                self._log("err", f"[ERROR] 未知包类型: {pkg_type}\n")
                continue
        
        if not self._modal_ask(
                messagebox.askyesno,
                "确认刷写",
                f"目录: {dirpath}\n\n将刷写以下 {len(jobs)} 个任务:\n  " + "\n  ".join(files) +
                "\n\n确认继续?"):
            return
        
        header = f"刷勾选的包 ({len(jobs)} 个任务)"
        self._run_task(
            lambda log, prog: flasher.execute_jobs(jobs, log=log),
            header)

    # ------------------------------------------------------------ 命令执行
    def _set_busy(self, busy):
        self._busy = busy
        state = ("disabled" if busy else "normal")
        for w in (self.btn_download, self.btn_flash_all,
                  self.btn_refresh_dev, self.btn_refresh_sess):
            try:
                w.configure(state=state)
            except tk.TclError:
                pass
        try:
            self.btn_cancel.configure(state=("normal" if busy else "disabled"))
            self.btn_pause.configure(state=("normal" if busy else "disabled"))
            self.btn_pause.configure(text="暂停")
        except tk.TclError:
            pass
        self._paused = False
        self.status_var.set("运行中..." if busy else "就绪")
        if busy:
            self._task_started_at = time.monotonic()
        else:
            self._task_started_at = None

    # macOS / 部分 WM 下 messagebox 模态框会从主窗口后面弹出来, 用户看不见,
    # 主线程却阻塞在 askyesno 上 -> 整个 UI "点了没反应"。弹之前临时置顶,
    # 弹完 100ms 后取消置顶, 避免一直挡在其他应用前面。
    def _modal_ask(self, fn, *args, **kwargs):
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.update_idletasks()
        except tk.TclError:
            pass
        try:
            return fn(*args, **kwargs)
        finally:
            def _restore():
                try:
                    self.root.attributes("-topmost", False)
                except tk.TclError:
                    pass
            self.root.after(100, _restore)

    def _run_task(self, job, header, after=None):
        """在子线程跑一个 job(log, progress_cb)->bool; 下载/刷机都走这里。

        job 内部 (downloader / flasher) 把日志逐行喂给 log 回调, 进度喂给
        progress_cb; 两者都只入队, 由主线程 _poll 落地, 保证 UI 不被阻塞。
        """
        self._set_busy(True)
        self._cancelled = False
        self._log("cmd", f"\n==== {header} ====\n")

        def worker():
            ok = False
            try:
                ok = bool(job(self._job_log, self._job_progress))
            except Exception as e:  # noqa
                self._q.put(("log", ("err", f"[异常] {e}\n")))
            tail = "已取消" if self._cancelled else ("完成" if ok else "失败")
            tag = "dim" if self._cancelled else ("ok" if ok else "err")
            self._q.put(("log", (tag, f"\n==== {tail} ====\n")))
            self._q.put(("finish", (after,)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_cancel(self):
        """协作式取消: 置位 _cancelled, 等 worker 自然结束后检查标志。"""
        if not self._busy:
            return
        self._cancelled = True
        self.status_var.set("正在取消...")
        self._log("warn", "[WARN] 已请求取消, 等待当前操作结束...\n")

    def _on_pause(self):
        """暂停/继续: 切换 _paused 标志。"""
        if not self._busy:
            return
        self._paused = not self._paused
        if self._paused:
            self.btn_pause.configure(text="继续")
            self.status_var.set("已暂停")
            self._log("dim", "[INFO] 下载已暂停\n")
        else:
            self.btn_pause.configure(text="暂停")
            self.status_var.set("运行中...")
            self._log("dim", "[INFO] 下载继续\n")

    def _job_log(self, msg):
        """downloader/flasher 的 log 回调: 按前缀上色, 走队列到主线程。"""
        if not isinstance(msg, str):
            msg = str(msg)
        text = msg.rstrip("\n")
        if not text.strip():
            return
        if text.startswith("[ERROR]") or text.startswith("[FAIL"):
            tag = "err"
        elif text.startswith("[OK]"):
            tag = "ok"
        elif text.startswith("[WARN]") or text.startswith("[DRY-RUN]"):
            tag = "dim"
        elif text.startswith("[INFO]"):
            tag = "cmd"
        else:
            tag = ""
        self._q.put(("log", (tag, text + "\n")))

    def _job_progress(self, written, total, speed, eta_str):
        """下载进度回调, 实时更新状态栏。"""
        # 通过队列发送到主线程更新
        self._q.put(("progress", (written, total, speed, eta_str)))

    # ------------------------------------------------------------ 日志 / 队列
    def _log(self, tag, text):
        self._q.put(("log", (tag, text)))

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    MAXLINES = 3000

    def _poll(self):
        # 关键: 无论本轮发生什么异常, 结尾一定要重新排期, 否则 after 链断裂
        # -> 队列不再被消费 -> 整个界面"卡死"(日志不动/按钮一直禁用)。
        try:
            self._poll_once()
        except Exception as e:  # noqa  兜底, 绝不让异常逃出 _poll
            try:
                self._q.put(("log", ("err", f"[界面异常] {type(e).__name__}: {e}\n")))
            except Exception:  # noqa
                pass
        finally:
            self.root.after(80, self._poll)

    def _poll_once(self):
        logs = []       # [(tag, text)]
        uis = []
        finishes = []
        progress = None
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    logs.append(payload)
                elif kind == "ui":
                    uis.append(payload)
                elif kind == "finish":
                    finishes.append(payload)
                elif kind == "progress":
                    progress = payload  # 只保留最新的进度
        except queue.Empty:
            pass

        if logs:
            # 合并相邻同 tag 的行, 一次 state 切换 + 一次滚动, 避免逐行重绘拖慢 UI
            merged = []
            for tag, text in logs:
                if merged and merged[-1][0] == tag:
                    merged[-1][1].append(text)
                else:
                    merged.append((tag, [text]))
            self.log.configure(state="normal")
            for tag, chunks in merged:
                self.log.insert("end", "".join(chunks), tag if tag else ())
            # 限制总行数, 防止 Text 无限增长导致重绘越来越慢
            linecount = int(self.log.index("end-1c").split(".")[0])
            if linecount > self.MAXLINES:
                self.log.delete("1.0", f"{linecount - self.MAXLINES}.0")
            self.log.see("end")
            self.log.configure(state="disabled")

        # 更新下载进度到状态栏
        if progress is not None:
            written, total, speed, eta_str = progress
            if total:
                pct = written * 100 // total
                status = f"下载中 {downloader.format_size(written)} / {downloader.format_size(total)} ({pct}%) @ {downloader.format_size(speed)}/s"
                if eta_str:
                    status += f" ETA {eta_str}"
                self.status_var.set(status)
            else:
                self.status_var.set(f"下载中 {downloader.format_size(written)} @ {downloader.format_size(speed)}/s")

        # 每个回调单独兜底: 一个坏了不影响其余, 更不会中断 after 链
        for fn in uis:
            try:
                fn()
            except Exception as e:  # noqa
                self._q.put(("log", ("err", f"[界面异常] {type(e).__name__}: {e}\n")))
        for payload in finishes:
            (after,) = payload
            self._set_busy(False)
            if after:
                try:
                    after()
                except Exception as e:  # noqa
                    self._q.put(("log", ("err", f"[收尾异常] {type(e).__name__}: {e}\n")))

        # 看门狗: 任务跑太久自动取消, 防止子进程挂死把 UI 锁死
        if self._busy and self._task_started_at:
            elapsed = time.monotonic() - self._task_started_at
            if elapsed > 30 * 60:  # 30 分钟
                self._cancelled = True
                self._log("warn", "[WARN] 任务运行超过 30 分钟, 已请求取消\n")
                self.status_var.set("超时, 正在取消...")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    # macOS: 从终端启动时窗口可能不自动获得焦点, 主动激活
    if sys.platform == "darwin":
        try:
            root.update_idletasks()
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.focus_force()
            root.after(600, lambda: root.attributes("-topmost", False))
        except tk.TclError:
            pass
    root.mainloop()


if __name__ == "__main__":
    main()
