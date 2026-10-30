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
        root.title("XP5 刷机工具 v2.0")
        root.geometry("1024x900")
        root.minsize(900, 800)
        
        # 设置主题样式
        self.style = ttk.Style()
        self.style.theme_use('clam')
        
        # 自定义样式
        self.style.configure('Title.TLabel', font=('Helvetica', 12, 'bold'), foreground='#2c3e50')
        self.style.configure('Section.TLabel', font=('Helvetica', 10, 'bold'), foreground='#34495e')
        self.style.configure('Action.TButton', font=('Helvetica', 10), padding=8)
        self.style.configure('Success.TButton', font=('Helvetica', 10, 'bold'), padding=10)
        self.style.configure('TLabelFrame', padding=10)
        self.style.configure('TLabelFrame.Label', font=('Helvetica', 10, 'bold'), foreground='#2c3e50')

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
        # 主容器
        main_container = ttk.Frame(self.root)
        main_container.pack(fill="both", expand=True, padx=12, pady=8)

        # 顶部: 设备栏
        top = ttk.LabelFrame(main_container, text="  设备状态  ")
        top.pack(fill="x", pady=(0, 8))
        
        top_inner = ttk.Frame(top)
        top_inner.pack(fill="x", padx=10, pady=8)
        
        self.dev_var = tk.StringVar(value="正在检测设备...")
        dev_label = ttk.Label(top_inner, textvariable=self.dev_var, justify="left",
                              wraplength=700, font=('Helvetica', 9))
        dev_label.pack(side="left", fill="x", expand=True)
        
        self.btn_refresh_dev = ttk.Button(top_inner, text="刷新设备", 
                                          command=self.refresh_devices, style='Action.TButton')
        self.btn_refresh_dev.pack(side="right", padx=(8, 0))

        # 中部: 选项卡
        nb = ttk.Notebook(main_container)
        nb.pack(fill="both", expand=True, pady=8)
        
        self.tab_download = ttk.Frame(nb)
        self.tab_flash = ttk.Frame(nb)
        nb.add(self.tab_download, text="  下载刷机包  ")
        nb.add(self.tab_flash, text="  刷机  ")
        
        self._build_download_tab(self.tab_download)
        self._build_flash_tab(self.tab_flash)

        # 底部: 日志
        logf = ttk.LabelFrame(main_container, text="  运行日志  ")
        logf.pack(fill="both", expand=True, pady=(8, 0))
        
        # 状态栏 (先打包, 确保在底部有空间)
        bar = ttk.Frame(logf)
        bar.pack(fill="x", side="bottom", padx=8, pady=(0, 8))
        
        self.status_var = tk.StringVar(value="就绪")
        status_label = ttk.Label(bar, textvariable=self.status_var, 
                                 font=('Helvetica', 9), foreground="#666")
        status_label.pack(side="left")
        
        # 按钮组
        btn_frame = ttk.Frame(bar)
        btn_frame.pack(side="right")
        
        self.btn_cancel = ttk.Button(btn_frame, text="取消任务", command=self._on_cancel,
                                     state="disabled", style='Action.TButton')
        self.btn_cancel.pack(side="right", padx=(6, 0))
        
        self.btn_pause = ttk.Button(btn_frame, text="暂停", command=self._on_pause,
                                    state="disabled", style='Action.TButton')
        self.btn_pause.pack(side="right", padx=(6, 0))
        
        ttk.Button(btn_frame, text="清空日志", command=self._clear_log,
                   style='Action.TButton').pack(side="right")
        
        # 日志文本区域 (后打包, 填充剩余空间)
        self.log = ScrolledText(logf, height=22, wrap="word", state="disabled",
                                bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
                                font=('Consolas', 9), relief="flat", borderwidth=0)
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        
        # 日志颜色标签
        for tag, color in (("cmd", "#569cd6"), ("ok", "#6a9955"),
                           ("err", "#f48771"), ("dim", "#808080"),
                           ("info", "#dcdcaa"), ("warn", "#ce9178")):
            self.log.tag_config(tag, foreground=color)

    # ---------------- 下载 tab ----------------
    def _build_download_tab(self, parent):
        # 创建主容器
        main_frame = ttk.Frame(parent)
        main_frame.pack(fill="both", expand=True, padx=8, pady=8)
        
        # 绑定鼠标滚轮(如果需要)
        def _on_mousewheel(event):
            parent.yview_scroll(int(-1*(event.delta/120)), "units")
        
        pad = {"padx": 10, "pady": 6}
        
        # 车型和下载目录在同一行
        row1 = ttk.Frame(main_frame)
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="车型:", style='Section.TLabel').pack(side="left")
        self.platform_var = tk.StringVar()
        platform_entry = ttk.Entry(row1, textvariable=self.platform_var, width=12, 
                                   font=('Helvetica', 10))
        platform_entry.pack(side="left", padx=(6, 20))
        
        ttk.Label(row1, text="下载目录:", style='Section.TLabel').pack(side="left")
        self.dl_dir_var = tk.StringVar()
        dir_entry = ttk.Entry(row1, textvariable=self.dl_dir_var, width=40, 
                              font=('Helvetica', 9))
        dir_entry.pack(side="left", padx=(6, 4))
        ttk.Button(row1, text="浏览...", command=self._browse_dl_dir).pack(side="left", padx=(0, 4))
        ttk.Button(row1, text="自动命名", command=self._auto_name_dl_dir).pack(side="left")
        
        # DT 号输入区域
        dt_frame = ttk.LabelFrame(main_frame, text="  DT 号配置  ")
        dt_frame.pack(fill="x", padx=10, pady=10)
        
        self.dt_vars = {}
        for name, label in [("vlmu", "VLMU"), ("xpu", "XPU")]:
            f = ttk.LabelFrame(dt_frame, text=f" {label} ")
            f.pack(fill="x", padx=8, pady=6)
            
            dt_big = tk.StringVar()
            dt_small = tk.StringVar()
            
            row_big = ttk.Frame(f)
            row_big.pack(fill="x", padx=10, pady=4)
            ttk.Label(row_big, text="大包 DT:", width=10, anchor="e").pack(side="left")
            ttk.Entry(row_big, textvariable=dt_big, width=45, 
                      font=('Consolas', 9)).pack(side="left", padx=(8, 6))
            ttk.Label(row_big, text="(可多个, 空格分隔)", foreground="#888", 
                      font=('Helvetica', 8)).pack(side="left")
            
            row_small = ttk.Frame(f)
            row_small.pack(fill="x", padx=10, pady=4)
            ttk.Label(row_small, text="小包 DT:", width=10, anchor="e").pack(side="left")
            ttk.Entry(row_small, textvariable=dt_small, width=45, 
                      font=('Consolas', 9)).pack(side="left", padx=(8, 6))
            ttk.Label(row_small, text="(可多个, 空格分隔)", foreground="#888", 
                      font=('Helvetica', 8)).pack(side="left")
            
            self.dt_vars[name] = {"big": dt_big, "small": dt_small}
        
        # MCU
        f_mcu = ttk.LabelFrame(dt_frame, text=" MCU ")
        f_mcu.pack(fill="x", padx=8, pady=6)
        dt_mcu = tk.StringVar()
        row_mcu = ttk.Frame(f_mcu)
        row_mcu.pack(fill="x", padx=10, pady=6)
        ttk.Label(row_mcu, text="MCUDEBUG:", width=10, anchor="e").pack(side="left")
        ttk.Entry(row_mcu, textvariable=dt_mcu, width=45, 
                  font=('Consolas', 9)).pack(side="left", padx=(8, 6))
        ttk.Label(row_mcu, text="(可多个, 空格分隔)", foreground="#888", 
                  font=('Helvetica', 8)).pack(side="left")
        self.dt_vars["mcu"] = {"mcu": dt_mcu}
        
        # 轮询模式设置 (内联到 DT 配置区域)
        watch_row = ttk.Frame(dt_frame)
        watch_row.pack(fill="x", padx=8, pady=(8, 6))
        
        self.watch_enabled_var = tk.BooleanVar(value=False)
        watch_cb = ttk.Checkbutton(watch_row, text="启用轮询", variable=self.watch_enabled_var,
                                   command=self._toggle_watch_options)
        watch_cb.pack(side="left", padx=(10, 6))
        
        ttk.Label(watch_row, text="间隔:", font=('Helvetica', 9)).pack(side="left")
        self.watch_interval_var = tk.StringVar(value="10")
        self.watch_interval_entry = ttk.Entry(watch_row, textvariable=self.watch_interval_var, 
                                              width=6, font=('Consolas', 9), state="disabled")
        self.watch_interval_entry.pack(side="left", padx=(4, 2))
        ttk.Label(watch_row, text="分钟", font=('Helvetica', 9)).pack(side="left", padx=(0, 12))
        
        ttk.Label(watch_row, text="超时:", font=('Helvetica', 9)).pack(side="left")
        self.watch_timeout_var = tk.StringVar(value="24")
        self.watch_timeout_entry = ttk.Entry(watch_row, textvariable=self.watch_timeout_var, 
                                             width=6, font=('Consolas', 9), state="disabled")
        self.watch_timeout_entry.pack(side="left", padx=(4, 2))
        ttk.Label(watch_row, text="小时", font=('Helvetica', 9)).pack(side="left", padx=(0, 12))
        
        ttk.Label(watch_row, text="(可用取消按钮停止轮询)", 
                  foreground="#999", font=('Helvetica', 8)).pack(side="left")
        
        # 下载按钮
        bar = ttk.Frame(main_frame)
        bar.pack(fill="x", padx=10, pady=12)
        self.btn_download = ttk.Button(bar, text="开始下载", command=self.on_download, 
                                       style='Success.TButton')
        self.btn_download.pack(side="left", padx=(0, 8))
        
        self.btn_cancel_watch = ttk.Button(bar, text="取消轮询", command=self._on_cancel_watch,
                                           state="disabled", style='Action.TButton')
        self.btn_cancel_watch.pack(side="left")

    def _browse_dl_dir(self):
        d = filedialog.askdirectory(title="选择下载目录",
                                    initialdir=(self.dl_dir_var.get() or PREP_ROOT or SCRIPT_DIR))
        if d:
            self.dl_dir_var.set(d)
    
    def _auto_name_dl_dir(self):
        """自动生成带时间戳的下载目录路径"""
        auto_path = os.path.join(PREP_ROOT, datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.dl_dir_var.set(auto_path)
    
    def _toggle_watch_options(self):
        """根据轮询复选框状态启用/禁用输入框"""
        if self.watch_enabled_var.get():
            self.watch_interval_entry.configure(state="normal")
            self.watch_timeout_entry.configure(state="normal")
        else:
            self.watch_interval_entry.configure(state="disabled")
            self.watch_timeout_entry.configure(state="disabled")

    # ---------------- 刷机 tab ----------------
    def _build_flash_tab(self, parent):
        pad = {"padx": 10, "pady": 6}
        
        # 目录选择
        row1 = ttk.Frame(parent)
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="刷机包目录:", style='Section.TLabel').pack(side="left")
        self.dir_var = tk.StringVar()
        self.dir_box = ttk.Combobox(row1, textvariable=self.dir_var, width=60, 
                                    font=('Consolas', 9))
        self.dir_box.pack(side="left", padx=(10, 6))
        self.dir_box.bind("<<ComboboxSelected>>", lambda e: self._load_dir_packages())
        self.dir_box.bind("<Return>", lambda e: self._load_dir_packages())
        ttk.Button(row1, text="浏览...", command=self._browse_flash_dir).pack(side="left", padx=(0, 6))
        self.btn_refresh_sess = ttk.Button(row1, text="刷新列表",
                                           command=self._reload_dir_suggestions)
        self.btn_refresh_sess.pack(side="left")
        
        # 包列表
        pf = ttk.LabelFrame(parent, text="  刷机包列表  ")
        pf.pack(fill="both", expand=True, **pad)
        
        # 创建可滚动的包列表
        canvas_frame = ttk.Frame(pf)
        canvas_frame.pack(fill="both", expand=True, padx=10, pady=8)
        
        self._pkg_canvas = tk.Canvas(canvas_frame, height=220, bg="#ffffff", 
                                     highlightthickness=1, highlightbackground="#ddd")
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self._pkg_canvas.yview)
        # 使用 tk.Frame 而不是 ttk.Frame，以便设置白色背景
        self._pkg_list_frame = tk.Frame(self._pkg_canvas, bg="#ffffff")
        
        self._pkg_list_frame.bind(
            "<Configure>",
            lambda e: self._pkg_canvas.configure(scrollregion=self._pkg_canvas.bbox("all"))
        )
        
        self._pkg_canvas.create_window((0, 0), window=self._pkg_list_frame, anchor="nw")
        self._pkg_canvas.configure(yscrollcommand=scrollbar.set)
        
        self._pkg_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # 绑定鼠标滚轮
        def _on_pkg_mousewheel(event):
            self._pkg_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        self._pkg_canvas.bind_all("<MouseWheel>", _on_pkg_mousewheel)
        
        # XP5 设备选择
        dev_frame = ttk.Frame(pf)
        dev_frame.pack(fill="x", padx=10, pady=(0, 8))
        
        ttk.Label(dev_frame, text="XP5 设备:", style='Section.TLabel').pack(side="left")
        self.xp5_a_var = tk.BooleanVar(value=True)
        self.xp5_b_var = tk.BooleanVar(value=True)
        
        dev_check_frame = ttk.Frame(dev_frame)
        dev_check_frame.pack(side="left", padx=(10, 0))
        
        ttk.Checkbutton(dev_check_frame, text="A 设备", variable=self.xp5_a_var).pack(side="left", padx=4)
        ttk.Checkbutton(dev_check_frame, text="B 设备", variable=self.xp5_b_var).pack(side="left", padx=4)
        
        # 刷机按钮
        btn_frame = ttk.Frame(pf)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        
        self.btn_flash_all = ttk.Button(btn_frame, text="刷选中的包", 
                                        command=self.on_flash_selected, 
                                        style='Success.TButton')
        self.btn_flash_all.pack(side="left", padx=(0, 10))
        
        # 存储包复选框
        self._pkg_checkboxes = {}  # {key: BooleanVar}
        self._pkg_types = {}  # {key: pkg_type}
        self._pkg_paths = {}  # {key: full_path}
        self._pkg_categories = {}  # {cat_key: [item_frames]} 用于收放
        self._pkg_cat_expanded = {}  # {cat_key: Boolean} 记录展开状态

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
        self._pkg_categories.clear()
        self._pkg_cat_expanded.clear()
        
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
        
        type_colors = {
            "vlmu_big": "#3498db",
            "vlmu_small": "#5dade2",
            "xp5_big": "#2ecc71",
            "xp5_small": "#58d68d",
            "mcu": "#e74c3c",
            "vlmu_mcu": "#9b59b6"
        }
        
        row = 0
        for cat_key in ["vlmu_big", "vlmu_small", "xp5_big", "xp5_small", "mcu", "vlmu_mcu"]:
            pkgs = cats.get(cat_key, [])
            extracted = extracted_cats.get(cat_key, [])
            
            if not pkgs and not extracted:
                continue
            
            # 类别标题 (可点击收放)
            cat_frame = tk.Frame(self._pkg_list_frame, bg="#ffffff")
            cat_frame.grid(row=row, column=0, sticky="w", padx=8, pady=(10, 4))
            
            arrow_var = tk.StringVar(value="▾")  # 默认展开
            cat_label = tk.Label(cat_frame, 
                                 textvariable=arrow_var,
                                 font=('Helvetica', 10, 'bold'),
                                 foreground=type_colors.get(cat_key, '#2c3e50'),
                                 background="#ffffff",
                                 cursor="hand2")
            cat_label.pack(side="left")
            
            title_label = tk.Label(cat_frame, 
                                   text=f" {type_labels[cat_key]} ({len(pkgs) + len(extracted)})", 
                                   font=('Helvetica', 10, 'bold'),
                                   foreground=type_colors.get(cat_key, '#2c3e50'),
                                   background="#ffffff",
                                   cursor="hand2")
            title_label.pack(side="left", padx=(2, 0))
            
            # 存储当前分类的 item frames
            item_frames = []
            self._pkg_categories[cat_key] = item_frames
            self._pkg_cat_expanded[cat_key] = True  # 默认展开
            row += 1
            
            # 绑定点击事件
            def toggle_cat(event=None, ck=cat_key, av=arrow_var, items=item_frames):
                self._toggle_category(ck, av, items)
            cat_label.bind("<Button-1>", toggle_cat)
            title_label.bind("<Button-1>", toggle_cat)
            
            # 已解压目录 (优先显示)
            for dir_name, full_path in extracted:
                var = tk.BooleanVar(value=False)
                cb_frame = tk.Frame(self._pkg_list_frame, bg="#ffffff")
                cb_frame.grid(row=row, column=0, sticky="w", padx=24, pady=2)
                
                cb = tk.Checkbutton(cb_frame, variable=var, bg="#ffffff", activebackground="#ffffff",
                                   command=lambda ck=cat_key, v=var: self._on_pkg_select(ck, v))
                cb.pack(side="left")
                
                name_label = tk.Label(cb_frame, text=dir_name, 
                                      font=('Consolas', 9), foreground="#2c3e50", bg="#ffffff")
                name_label.pack(side="left", padx=(4, 6))
                
                tag_label = tk.Label(cb_frame, text="[已解压]", 
                                     font=('Helvetica', 8), foreground="#27ae60", bg="#ffffff")
                tag_label.pack(side="left")
                
                key = f"[extracted]{dir_name}"
                self._pkg_checkboxes[key] = var
                self._pkg_types[key] = cat_key
                self._pkg_paths[key] = full_path
                item_frames.append(cb_frame)
                row += 1
            
            # 压缩包
            for pkg_name in pkgs:
                var = tk.BooleanVar(value=False)
                cb_frame = tk.Frame(self._pkg_list_frame, bg="#ffffff")
                cb_frame.grid(row=row, column=0, sticky="w", padx=24, pady=2)
                
                cb = tk.Checkbutton(cb_frame, variable=var, bg="#ffffff", activebackground="#ffffff",
                                   command=lambda ck=cat_key, v=var: self._on_pkg_select(ck, v))
                cb.pack(side="left")
                
                name_label = tk.Label(cb_frame, text=pkg_name, 
                                      font=('Consolas', 9), foreground="#34495e", bg="#ffffff")
                name_label.pack(side="left", padx=4)
                
                self._pkg_checkboxes[pkg_name] = var
                self._pkg_types[pkg_name] = cat_key
                self._pkg_paths[pkg_name] = os.path.join(dirpath, pkg_name)
                item_frames.append(cb_frame)
                row += 1
        
        if row == 0:
            empty_label = ttk.Label(self._pkg_list_frame, 
                                   text="(目录中没有找到刷机包)", 
                                   foreground="#999", font=('Helvetica', 10),
                                   background="#ffffff")
            empty_label.grid(row=0, column=0, padx=10, pady=20)
    
    def _toggle_category(self, cat_key, arrow_var, item_frames):
        """收放某个分类的包列表。"""
        expanded = self._pkg_cat_expanded.get(cat_key, True)
        if expanded:
            # 收起
            arrow_var.set("▸")
            for frame in item_frames:
                frame.grid_remove()
            self._pkg_cat_expanded[cat_key] = False
        else:
            # 展开
            arrow_var.set("▾")
            for frame in item_frames:
                frame.grid()
            self._pkg_cat_expanded[cat_key] = True
    
    def _on_pkg_select(self, cat_key, selected_var):
        """每个分类只能选一个包: 选中一个时取消同分类其他包。"""
        if not selected_var.get():
            return  # 取消选中时不做处理
        # 取消同分类其他包的选中状态
        for key, var in self._pkg_checkboxes.items():
            if self._pkg_types.get(key) == cat_key and var is not selected_var:
                var.set(False)

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
        try:
            self._log("info", "[INFO] 下载按钮已点击\n")
            if self._busy:
                self._log("warn", "[WARN] 当前有任务在运行, 请等待结束后再操作\n")
                return
            platform, tasks, err = self._collect_download_tasks()
            if err:
                self._log("err", f"[ERROR] 无法下载: {err}\n")
                self._modal_ask(messagebox.showwarning, "无法下载", err)
                return
            
            # 读取轮询设置
            watch = 0
            timeout_hrs = 24
            if self.watch_enabled_var.get():
                try:
                    watch = int(self.watch_interval_var.get().strip())
                    if watch <= 0:
                        watch = 0
                except ValueError:
                    watch = 0
                try:
                    timeout_hrs = int(self.watch_timeout_var.get().strip())
                    if timeout_hrs <= 0:
                        timeout_hrs = 24
                except ValueError:
                    timeout_hrs = 24
            
            custom = self.dl_dir_var.get().strip()
            session_dir = custom or os.path.join(PREP_ROOT, datetime.now().strftime("%Y%m%d_%H%M%S"))

            plan = "\n".join(f"  {t}  DT: {dts}" for t, dts in tasks)
            watch_info = ""
            if watch > 0:
                watch_info = f"\n\n轮询模式: 每 {watch} 分钟检查一次\n超时时间: {timeout_hrs} 小时"
            
            if not self._modal_ask(
                    messagebox.askyesno,
                    "确认下载", f"车型: {platform}\n\n下载计划:\n{plan}\n\n"
                                f"目标目录:\n  {session_dir}{watch_info}\n\n开始下载?"):
                return

            try:
                os.makedirs(session_dir, exist_ok=True)
            except OSError as e:
                self._modal_ask(messagebox.showerror, "失败", f"无法创建下载目录: {e}")
                return

            def job(log, progress_cb):
                fail = downloader.run_download(
                    platform, tasks, session_dir, log=log,
                    all_=True, watch=watch, timeout_hrs=timeout_hrs,
                    cancel_cb=lambda: self._cancelled,
                    pause_cb=lambda: self._paused,
                    progress_cb=progress_cb)
                return fail == 0

            def after():
                self._reload_dir_suggestions()
                self.dir_var.set(session_dir)

            task_desc = f"下载到 {session_dir}"
            if watch > 0:
                task_desc += f" (轮询模式: {watch}分钟)"
            self._run_task(job, task_desc, after=after)
        except Exception as e:
            self._log("err", f"[ERROR] 下载按钮异常: {type(e).__name__}: {e}\n")
            import traceback
            traceback.print_exc()

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
        # 取消轮询按钮: 只在轮询模式下启用
        try:
            if busy and self.watch_enabled_var.get():
                self.btn_cancel_watch.configure(state="normal")
            else:
                self.btn_cancel_watch.configure(state="disabled")
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
            finally:
                # 无论成功、失败还是异常, 都要发送 finish 消息恢复按钮状态
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
        # 检查是否在轮询模式
        if self.watch_enabled_var.get() and self._task_started_at:
            self._log("warn", "[WARN] 已请求取消轮询, 等待当前操作结束...\n")
        else:
            self._log("warn", "[WARN] 已请求取消, 等待当前操作结束...\n")
    
    def _on_cancel_watch(self):
        """专门取消轮询模式。"""
        if not self._busy:
            return
        self._cancelled = True
        self.status_var.set("正在取消轮询...")
        self._log("warn", "[WARN] 正在取消轮询...\n")
        # 禁用取消轮询按钮
        try:
            self.btn_cancel_watch.configure(state="disabled")
        except tk.TclError:
            pass

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
