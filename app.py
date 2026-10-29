"""wx_ROBOT —— 微信朋友圈定时发布助手（Windows / 微信 4.x）

双击“启动.bat”即可使用。程序会自动读取“待发布”文件夹里的计划，
在设定的时间调用微信 PC 客户端发布朋友圈。
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import store
from wechat_ui import WeChatUI, WeChatNotReady, AutomationError, enable_dpi_awareness

APP_NAME = "wx_ROBOT"
BG = "#f2f4f5"
CARD = "#ffffff"
GREEN = "#07c160"
GREEN_DARK = "#06ad56"
TEXT = "#1f2329"
MUTED = "#7a8087"
BORDER = "#e3e6e8"
RED = "#e5484d"
ORANGE = "#f08c00"
BLUE = "#2f6fed"

STATUS_TAGS = {
    store.STATUS_DRAFT: ("draft", MUTED),
    store.STATUS_SCHEDULED: ("scheduled", GREEN_DARK),
    store.STATUS_PUBLISHING: ("publishing", ORANGE),
    store.STATUS_FAILED: ("failed", RED),
    store.STATUS_SKIPPED: ("skipped", MUTED),
}


def _single_instance() -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\wx_ROBOT_single_instance")
    if not handle:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return False
    globals()["_MUTEX_HANDLE"] = handle
    return True


def _keep_awake(enable: bool) -> None:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if enable else ES_CONTINUOUS
        kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


class TkLogHandler(logging.Handler):
    def __init__(self, ui_queue: queue.Queue):
        super().__init__()
        self.ui_queue = ui_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ui_queue.put(("log", (record.levelname, self.format(record))))
        except Exception:
            pass


class App(tk.Tk):
    def __init__(self, minimized: bool = False):
        super().__init__()
        self.title("wx_ROBOT · 微信朋友圈定时助手")
        self.geometry("1120x820")
        self.minsize(1000, 720)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            self.iconbitmap(str(Path(__file__).resolve().parent / "wx_robot.ico"))
        except Exception:
            pass

        self.cfg = store.load_config()
        store.ensure_dirs()

        self.ui_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._paused = threading.Event()
        self._wake_event = threading.Event()
        self._publish_lock = threading.Lock()
        self._scheduler_thread: threading.Thread | None = None
        self._closing = False

        self.posts: dict[str, store.Post] = {}
        self.current: store.Post | None = None
        self.edit_images: list[Path] = []
        self._dirty = False
        self._selected_id: str | None = None
        self._thumb_ref = None

        self._setup_logging()
        self._setup_style()
        self._build_ui()
        self._bind_events()

        recovered = store.recover_interrupted()
        if recovered:
            self.log("WARNING", f"检测到 {recovered} 条上次中断的发布任务，已恢复为“待发布”。")

        self._refresh_list(select_id=None)
        self._new_post(confirm=False)
        self.after(300, self._poll_ui_queue)
        self.after(500, self._start_scheduler)
        self.after(800, self._tick_status)
        if minimized:
            self.after(600, self.iconify)

    # ---------------- 日志 ----------------
    def _setup_logging(self) -> None:
        self.logger = logging.getLogger("wxrobot")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = logging.handlers.RotatingFileHandler(
            store.LOG_DIR / "wxrobot.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        self.logger.addHandler(fh)
        gh = TkLogHandler(self.ui_queue)
        gh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        self.logger.addHandler(gh)

    def log(self, level: str, msg: str) -> None:
        self.logger.log(getattr(logging, level.upper(), logging.INFO), msg)

    # ---------------- 样式 ----------------
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Microsoft YaHei UI", 10), background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Header.TFrame", background="#ffffff")
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", background="#ffffff", foreground=TEXT, font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Sub.TLabel", background="#ffffff", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", background=CARD, foreground=TEXT, font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Primary.TButton", background=GREEN, foreground="#ffffff", borderwidth=0,
                        focusthickness=0, padding=(16, 8), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", GREEN_DARK), ("disabled", "#a8e6c4")])
        style.configure("Secondary.TButton", background="#eef1f2", foreground=TEXT, borderwidth=0,
                        padding=(12, 7))
        style.map("Secondary.TButton", background=[("active", "#e2e6e8")])
        style.configure("Danger.TButton", background="#fdecec", foreground=RED, borderwidth=0, padding=(12, 7))
        style.map("Danger.TButton", background=[("active", "#fbdcdc")])
        style.configure("Treeview", background=CARD, fieldbackground=CARD, rowheight=32,
                        borderwidth=0, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", background="#f7f8f9", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9), borderwidth=0, padding=(6, 7))
        style.map("Treeview", background=[("selected", "#e7f8ef")], foreground=[("selected", TEXT)])
        style.configure("TLabelframe", background=CARD, bordercolor=BORDER)
        style.configure("TLabelframe.Label", background=CARD, foreground=TEXT)
        style.configure("TEntry", fieldbackground="#f7f8f9", bordercolor=BORDER, padding=7)
        style.configure("TCombobox", fieldbackground="#f7f8f9", padding=6)

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", height=86)
        header.pack(side="top", fill="x")
        header.pack_propagate(False)

        left = ttk.Frame(header, style="Header.TFrame")
        left.pack(side="left", padx=22, pady=14)
        ttk.Label(left, text="wx_ROBOT", style="Title.TLabel").pack(anchor="w")
        acct = self.cfg.get("account_wxid", "")
        ttk.Label(left, text=f"微信朋友圈定时发布  ·  {acct}", style="Sub.TLabel").pack(anchor="w")

        right = ttk.Frame(header, style="Header.TFrame")
        right.pack(side="right", padx=22)
        self.wechat_dot = tk.Label(right, text="●", bg="#ffffff", fg=ORANGE, font=("Segoe UI", 12))
        self.wechat_dot.pack(side="left")
        self.wechat_status = ttk.Label(right, text="正在检测微信…", style="Sub.TLabel")
        self.wechat_status.pack(side="left", padx=(2, 18))
        self.sched_dot = tk.Label(right, text="●", bg="#ffffff", fg=MUTED, font=("Segoe UI", 12))
        self.sched_dot.pack(side="left")
        self.sched_status = ttk.Label(right, text="调度未启动", style="Sub.TLabel")
        self.sched_status.pack(side="left", padx=(2, 0))

        body = ttk.Frame(self, style="TFrame")
        body.pack(side="top", fill="both", expand=True, padx=16, pady=(14, 8))

        paned = ttk.Panedwindow(body, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # 左侧：计划列表
        left_card = ttk.Frame(paned, style="Card.TFrame", padding=12)
        paned.add(left_card, weight=3)

        toolbar = ttk.Frame(left_card, style="Card.TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        head_left = ttk.Frame(toolbar, style="Card.TFrame")
        head_left.pack(side="left")
        ttk.Label(head_left, text="发布计划", style="Section.TLabel").pack(anchor="w")
        self.count_label = ttk.Label(head_left, text="", style="Muted.TLabel")
        self.count_label.pack(anchor="w")
        ttk.Button(toolbar, text="新建", style="Secondary.TButton", command=self._new_post).pack(side="right", padx=(6, 0))
        ttk.Button(toolbar, text="删除", style="Danger.TButton", command=self._delete_post).pack(side="right", padx=(6, 0))
        ttk.Button(toolbar, text="刷新", style="Secondary.TButton", command=lambda: self._refresh_list()).pack(side="right")

        columns = ("time", "status", "images", "title")
        self.tree = ttk.Treeview(left_card, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("time", text="发布时间")
        self.tree.heading("status", text="状态")
        self.tree.heading("images", text="图片")
        self.tree.heading("title", text="文案")
        self.tree.column("time", width=128, anchor="w", stretch=False)
        self.tree.column("status", width=64, anchor="center", stretch=False)
        self.tree.column("images", width=48, anchor="center", stretch=False)
        self.tree.column("title", width=250, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(left_card, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        for status, (tag, color) in STATUS_TAGS.items():
            self.tree.tag_configure(tag, foreground=color)

        # 右侧：编辑器
        right_card = ttk.Frame(paned, style="Card.TFrame", padding=16)
        paned.add(right_card, weight=4)

        top_row = ttk.Frame(right_card, style="Card.TFrame")
        top_row.pack(fill="x")
        self.form_title = ttk.Label(top_row, text="新建计划", style="Section.TLabel")
        self.form_title.pack(side="left")
        self.form_hint = ttk.Label(top_row, text="  提前 24 小时准备文案和图片", style="Muted.TLabel")
        self.form_hint.pack(side="left")

        time_row = ttk.Frame(right_card, style="Card.TFrame")
        time_row.pack(fill="x", pady=(14, 6))
        ttk.Label(time_row, text="发布时间", style="Card.TLabel").pack(side="left")
        self.time_var = tk.StringVar()
        self.time_entry = ttk.Entry(time_row, textvariable=self.time_var, width=19)
        self.time_entry.pack(side="left", padx=(10, 8))
        ttk.Label(time_row, text="格式：2026-10-30 08:30", style="Muted.TLabel").pack(side="left")
        ttk.Button(time_row, text="1小时后", style="Secondary.TButton",
                   command=lambda: self._quick_time(1)).pack(side="right", padx=(4, 0))
        ttk.Button(time_row, text="今晚20:00", style="Secondary.TButton",
                   command=lambda: self._quick_time("tonight")).pack(side="right", padx=(4, 0))
        ttk.Button(time_row, text="明早08:00", style="Secondary.TButton",
                   command=lambda: self._quick_time("tomorrow")).pack(side="right", padx=(4, 0))

        text_head = ttk.Frame(right_card, style="Card.TFrame")
        text_head.pack(fill="x", pady=(10, 4))
        ttk.Label(text_head, text="朋友圈文案", style="Card.TLabel").pack(side="left")
        self.char_label = ttk.Label(text_head, text="0 / 1500", style="Muted.TLabel")
        self.char_label.pack(side="right")

        self.text = tk.Text(right_card, height=8, wrap="word", relief="flat", bd=0,
                            bg="#f7f8f9", fg=TEXT, insertbackground=TEXT,
                            font=("Microsoft YaHei UI", 10), padx=10, pady=8,
                            highlightthickness=1, highlightbackground=BORDER, highlightcolor=GREEN)
        self.text.pack(fill="x")

        img_head = ttk.Frame(right_card, style="Card.TFrame")
        img_head.pack(fill="x", pady=(14, 4))
        ttk.Label(img_head, text="图片（1–9 张，按列表顺序发布）", style="Card.TLabel").pack(side="left")
        self.img_count = ttk.Label(img_head, text="0 张", style="Muted.TLabel")
        self.img_count.pack(side="right")

        img_area = ttk.Frame(right_card, style="Card.TFrame")
        img_area.pack(fill="both", expand=True)

        img_btns = ttk.Frame(img_area, style="Card.TFrame")
        img_btns.pack(side="left", fill="y", padx=(0, 10))
        ttk.Button(img_btns, text="添加图片…", style="Secondary.TButton", command=self._add_images).pack(fill="x")
        ttk.Button(img_btns, text="移除", style="Secondary.TButton", command=self._remove_image).pack(fill="x", pady=(6, 0))
        ttk.Button(img_btns, text="上移", style="Secondary.TButton", command=lambda: self._move_image(-1)).pack(fill="x", pady=(6, 0))
        ttk.Button(img_btns, text="下移", style="Secondary.TButton", command=lambda: self._move_image(1)).pack(fill="x", pady=(6, 0))

        self.img_list = tk.Listbox(img_area, height=5, relief="flat", bd=0, bg="#f7f8f9",
                                   fg=TEXT, selectbackground="#d9f5e6", selectforeground=TEXT,
                                   font=("Microsoft YaHei UI", 9), highlightthickness=1,
                                   highlightbackground=BORDER, activestyle="none")
        self.img_list.pack(side="left", fill="both", expand=True)
        self.img_list.bind("<<ListboxSelect>>", lambda e: self._show_thumb())

        self.thumb = tk.Label(img_area, text="图片预览", bg="#f7f8f9", fg=MUTED,
                              width=22, height=7, relief="flat", bd=0)
        self.thumb.pack(side="right", padx=(10, 0))

        self.error_label = ttk.Label(right_card, text="", style="Muted.TLabel", foreground=RED)
        self.error_label.pack(fill="x", pady=(6, 0))

        actions = ttk.Frame(right_card, style="Card.TFrame")
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="保存计划", style="Primary.TButton", command=self._save_post).pack(side="left")
        ttk.Button(actions, text="预览填充（不发布）", style="Secondary.TButton",
                   command=self._preview_fill).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="立即发布", style="Danger.TButton",
                   command=self._publish_now).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="打开文件夹", style="Secondary.TButton",
                   command=self._open_folder).pack(side="right")

        # 底部日志
        log_frame = ttk.Frame(self, style="Card.TFrame", padding=(12, 8))
        log_frame.pack(side="bottom", fill="x", padx=16, pady=(0, 10))
        log_head = ttk.Frame(log_frame, style="Card.TFrame")
        log_head.pack(fill="x")
        ttk.Label(log_head, text="运行日志", style="Section.TLabel").pack(side="left")
        self.next_label = ttk.Label(log_head, text="", style="Muted.TLabel")
        self.next_label.pack(side="right")
        self.log_box = ScrolledText(log_frame, height=6, relief="flat", bd=0, bg="#fbfcfc",
                                    fg="#4a4f55", font=("Consolas", 9), padx=8, pady=6,
                                    highlightthickness=0, state="disabled")
        self.log_box.pack(fill="x", pady=(4, 0))
        self.log_box.tag_configure("WARNING", foreground="#b26a00")
        self.log_box.tag_configure("ERROR", foreground=RED)
        self.log_box.tag_configure("INFO", foreground="#4a4f55")

    def _bind_events(self) -> None:
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.text.bind("<<Modified>>", self._on_text_modified)
        self.time_var.trace_add("write", lambda *_: self._mark_dirty())
        self.img_list.bind("<Double-Button-1>", lambda e: self._show_thumb())
        self.bind("<Control-s>", lambda e: self._save_post())

    # ---------------- 状态 / 列表 ----------------
    def _mark_dirty(self, *_):
        self._dirty = True

    def _on_text_modified(self, _event=None):
        try:
            if self.text.edit_modified():
                self._dirty = True
                self.text.edit_modified(False)
        except tk.TclError:
            pass
        content = self.text.get("1.0", "end-1c")
        self.char_label.configure(text=f"{len(content)} / {int(self.cfg.get('max_text_chars', 1500))}")

    def _refresh_list(self, select_id: str | None = None) -> None:
        try:
            posts = store.list_posts()
        except Exception as exc:
            self.log("ERROR", f"读取计划失败：{exc}")
            return
        self.posts = {p.id: p for p in posts}
        selected = select_id if select_id is not None else self._selected_id
        self.tree.delete(*self.tree.get_children())
        for p in posts:
            tag = STATUS_TAGS.get(p.status, ("draft", MUTED))[0]
            self.tree.insert(
                "", "end", iid=p.id,
                values=(
                    p.publish_at.strftime("%m-%d %H:%M"),
                    p.status_label,
                    f"{len(p.images)} 张",
                    p.title,
                ),
                tags=(tag,),
            )
        counts = {}
        for p in posts:
            counts[p.status] = counts.get(p.status, 0) + 1
        summary = " · ".join(
            f"{store.STATUS_LABELS.get(k, k)} {v}" for k, v in counts.items() if v
        ) or "暂无计划"
        self.count_label.configure(text=summary)
        if selected and selected in self.posts:
            self.tree.selection_set(selected)
            self.tree.see(selected)
        elif selected:
            self._selected_id = None

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        pid = sel[0]
        if pid == self._selected_id and self.current is not None and self.current.id == pid:
            return
        if self._dirty:
            if not messagebox.askyesno("未保存的修改", "当前编辑内容还没保存，切换会丢失。继续吗？"):
                if self._selected_id and self._selected_id in self.posts:
                    self.tree.selection_set(self._selected_id)
                return
        post = self.posts.get(pid)
        if post:
            self._load_post(post)

    def _load_post(self, post: store.Post) -> None:
        self.current = post
        self._selected_id = post.id
        self.form_title.configure(text="编辑计划")
        self.form_hint.configure(text=f"  {post.folder.name}")
        self.time_var.set(post.publish_at.strftime("%Y-%m-%d %H:%M"))
        self.text.delete("1.0", "end")
        self.text.insert("1.0", post.text)
        self.text.edit_modified(False)
        self.edit_images = list(post.images)
        self._refresh_images()
        self.error_label.configure(text=post.last_error or "")
        self._dirty = False
        self._on_text_modified()

    def _new_post(self, confirm: bool = True) -> None:
        if confirm and self._dirty:
            if not messagebox.askyesno("未保存的修改", "当前编辑内容还没保存，新建会丢失。继续吗？"):
                return
        self.current = None
        self._selected_id = None
        self.tree.selection_remove(self.tree.selection())
        self.form_title.configure(text="新建计划")
        self.form_hint.configure(text="  提前 24 小时准备文案和图片")
        base = datetime.now().astimezone() + timedelta(hours=1)
        base = base.replace(minute=(base.minute // 5) * 5, second=0, microsecond=0)
        self.time_var.set(base.strftime("%Y-%m-%d %H:%M"))
        self.text.delete("1.0", "end")
        self.text.edit_modified(False)
        self.edit_images = []
        self._refresh_images()
        self.error_label.configure(text="")
        self._dirty = False
        self._on_text_modified()

    def _refresh_images(self) -> None:
        self.img_list.delete(0, "end")
        for p in self.edit_images:
            self.img_list.insert("end", p.name)
        self.img_count.configure(text=f"{len(self.edit_images)} 张")
        if self.edit_images:
            self.img_list.selection_set(0)
            self._show_thumb()
        else:
            self._thumb_ref = None
            self.thumb.configure(image="", text="图片预览")

    def _show_thumb(self) -> None:
        sel = self.img_list.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if idx >= len(self.edit_images):
            return
        path = self.edit_images[idx]
        try:
            from PIL import Image, ImageTk
            im = Image.open(path)
            im.thumbnail((200, 140))
            photo = ImageTk.PhotoImage(im)
            self._thumb_ref = photo
            self.thumb.configure(image=photo, text="", width=200, height=140)
        except Exception:
            self._thumb_ref = None
            self.thumb.configure(image="", text="无法预览", width=22, height=7)

    def _add_images(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择朋友圈图片（最多 9 张）",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        existing = {str(p).lower() for p in self.edit_images}
        for raw in paths:
            p = Path(raw)
            if p.suffix.lower() not in store.IMAGE_EXTS:
                messagebox.showwarning("格式不支持", f"{p.name} 不是 jpg/png/bmp 图片。")
                continue
            if str(p).lower() not in existing:
                self.edit_images.append(p)
                existing.add(str(p).lower())
            if len(self.edit_images) >= int(self.cfg.get("max_images", 9)):
                break
        if len(self.edit_images) > int(self.cfg.get("max_images", 9)):
            self.edit_images = self.edit_images[: int(self.cfg.get("max_images", 9))]
            messagebox.showinfo("最多 9 张", "已自动保留前 9 张图片。")
        self._refresh_images()
        self._mark_dirty()

    def _remove_image(self) -> None:
        sel = self.img_list.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.edit_images):
            self.edit_images.pop(idx)
            self._refresh_images()
            self._mark_dirty()

    def _move_image(self, delta: int) -> None:
        sel = self.img_list.curselection()
        if not sel:
            return
        idx = int(sel[0])
        new = idx + delta
        if 0 <= new < len(self.edit_images):
            self.edit_images[idx], self.edit_images[new] = self.edit_images[new], self.edit_images[idx]
            self._refresh_images()
            self.img_list.selection_set(new)
            self._show_thumb()
            self._mark_dirty()

    def _quick_time(self, kind) -> None:
        now = datetime.now().astimezone()
        if kind == 1:
            target = now + timedelta(hours=1)
        elif kind == "tonight":
            target = now.replace(hour=20, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
        else:
            target = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        self.time_var.set(target.strftime("%Y-%m-%d %H:%M"))

    def _parse_time(self) -> datetime | None:
        raw = self.time_var.get().strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt).astimezone()
            except ValueError:
                continue
        messagebox.showerror("时间格式不对", "请按这个格式填写：2026-10-30 08:30")
        return None

    def _current_form_values(self):
        publish_at = self._parse_time()
        if publish_at is None:
            return None
        text = self.text.get("1.0", "end-1c")
        return publish_at, text, list(self.edit_images)

    def _save_post(self, silent: bool = False) -> store.Post | None:
        values = self._current_form_values()
        if values is None:
            return None
        publish_at, text, images = values
        errors = store.validate(publish_at, text, images, self.cfg)
        if errors:
            self.error_label.configure(text=errors[0])
            messagebox.showerror("请检查计划", "\n".join(errors))
            return None
        try:
            if self.current is None:
                post = store.create_post(publish_at, text, images, self.cfg)
                self.log("INFO", f"已创建计划：{post.folder.name}")
            else:
                post = store.update_post(self.current, publish_at, text, images, self.cfg)
                self.log("INFO", f"已保存计划：{post.folder.name}")
        except Exception as exc:
            self.error_label.configure(text=str(exc))
            messagebox.showerror("保存失败", str(exc))
            return None
        self.current = post
        self._selected_id = post.id
        self._dirty = False
        self.error_label.configure(text="")
        self._refresh_list(select_id=post.id)
        self._load_post(post)
        if not silent:
            messagebox.showinfo("已保存", f"计划已保存：\n{post.publish_at.strftime('%Y-%m-%d %H:%M')}")
        return post

    def _delete_post(self) -> None:
        if self.current is None:
            messagebox.showinfo("没有选中", "请先在左侧选中一条计划。")
            return
        if not messagebox.askyesno("删除计划", f"确定删除这条计划吗？\n\n{self.current.title}\n\n（会移到“已删除”文件夹，可恢复）"):
            return
        try:
            dest = store.delete_post(self.current)
            self.log("INFO", f"已删除计划，移动到：{dest}")
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        self.current = None
        self._selected_id = None
        self._refresh_list(select_id=None)
        self._new_post(confirm=False)

    def _open_folder(self) -> None:
        os.startfile(store.QUEUE_DIR)

    # ---------------- 调度 ----------------
    def _start_scheduler(self) -> None:
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, name="wxrobot-scheduler", daemon=True)
        self._scheduler_thread.start()
        self.log("INFO", "定时调度已启动。")
        self._update_sched_status()

    def _scheduler_loop(self) -> None:
        if self.cfg.get("keep_awake", True):
            _keep_awake(True)
        try:
            while not self._stop_event.is_set():
                try:
                    if not self._paused.is_set():
                        self._scheduler_tick()
                except Exception as exc:
                    self.log("ERROR", f"调度循环异常：{exc}")
                self._stop_event.wait(max(1, int(self.cfg.get("check_interval_seconds", 3) or 3)))
        finally:
            _keep_awake(False)

    def _scheduler_tick(self) -> None:
        for post in store.list_posts():
            if self._stop_event.is_set() or self._paused.is_set():
                return
            if not post.due():
                continue
            late = post.late_minutes()
            catch_up = float(self.cfg.get("catch_up_minutes", 90) or 90)
            if late > catch_up:
                store.set_status(post, store.STATUS_SKIPPED,
                                 error=f"错过发布时间 {late:.0f} 分钟，超过补发窗口，已跳过。")
                try:
                    store.move_post(post, store.FAILED_DIR)
                except Exception:
                    pass
                self.log("WARNING", f"跳过过期计划：{post.folder.name}（迟到 {late:.0f} 分钟）")
                self._notify_refresh()
                continue
            self._publish_worker(post, manual=False)

    def _notify_refresh(self, select_id: str | None = None) -> None:
        self.ui_queue.put(("refresh", select_id))

    def _publish_worker(self, post: store.Post, *, manual: bool) -> bool:
        if not self._publish_lock.acquire(blocking=False):
            if manual:
                messagebox.showinfo("正在发布", "当前已有发布任务在进行中，请稍候。")
            return False
        try:
            if self._paused.is_set() and not manual:
                return False
            self.log("INFO", f"开始发布：{post.folder.name}")
            store.set_status(post, store.STATUS_PUBLISHING)
            self._notify_refresh(post.id)
            ui = WeChatUI(self.cfg, logger=self.log)
            try:
                ui.publish(post.text, post.images, preview_only=False, post_id=post.id)
                sent_at = store.now_local().isoformat(timespec="seconds")
                store.set_status(post, store.STATUS_SENT, sent_at=sent_at)
                try:
                    dest = store.move_post(post, store.SENT_DIR)
                    self.log("INFO", f"发布成功，已归档到：{dest}")
                except Exception as exc:
                    self.log("WARNING", f"发布成功但归档失败：{exc}")
                self.ui_queue.put(("published", (post.id, True, "")))
                return True
            except Exception as exc:
                attempts = post.attempts + 1
                max_retries = int(self.cfg.get("max_retries", 2) or 0)
                msg = str(exc)
                self.log("ERROR", f"发布失败：{msg}")
                try:
                    ui.cleanup()
                except Exception as clean_exc:
                    self.log("WARNING", f"清理发布窗口失败：{clean_exc}")
                # 图片上传失败/结果无法确认时不要自动重试，避免刷屏或重复发帖
                retryable = ("照片未发送" not in msg) and ("无法确认" not in msg)
                if attempts <= max_retries and retryable:
                    retry_at = (store.now_local() +
                                timedelta(minutes=float(self.cfg.get("retry_delay_minutes", 8) or 8)))
                    store.set_status(
                        post, store.STATUS_SCHEDULED,
                        error=f"第 {attempts} 次失败：{msg}",
                        next_retry_at=retry_at.isoformat(timespec="seconds"),
                        attempts=attempts,
                    )
                    self.log("WARNING", f"{self.cfg.get('retry_delay_minutes', 8)} 分钟后重试（第 {attempts + 1} 次）。")
                else:
                    store.set_status(post, store.STATUS_FAILED,
                                     error=f"第 {attempts} 次失败：{msg}", attempts=attempts)
                    try:
                        store.move_post(post, store.FAILED_DIR)
                    except Exception:
                        pass
                    if not retryable:
                        self.log("ERROR", "这次失败不适合自动重试，已停止并移入“失败”文件夹。")
                    else:
                        self.log("ERROR", "已达到最大重试次数，计划移入“失败”文件夹。")
                self.ui_queue.put(("published", (post.id, False, msg)))
                return False
            finally:
                self._notify_refresh()
        finally:
            self._publish_lock.release()

    def _preview_fill(self) -> None:
        if self._publish_lock.locked():
            messagebox.showinfo("正在发布", "当前有发布任务在进行中，请稍候。")
            return
        post = self._save_post(silent=True)
        if post is None:
            return
        if not messagebox.askyesno(
            "预览填充",
            "接下来会自动操作微信：打开朋友圈编辑框，填入文案和图片，\n"
            "但不会点击“发表”。\n\n"
            "检查完后，请你在微信里点“取消”并选择“不保留”。\n\n继续吗？",
        ):
            return

        def work():
            ok = self._publish_lock.acquire(blocking=False)
            if not ok:
                return
            try:
                self.log("INFO", "测试模式：开始填充朋友圈编辑框（不会发表）…")
                ui = WeChatUI(self.cfg, logger=self.log)
                shot = ui.publish(post.text, post.images, preview_only=True, post_id=post.id)
                self.ui_queue.put(("preview_ok", str(shot) if shot else ""))
            except Exception as exc:
                try:
                    WeChatUI(self.cfg, logger=self.log).cleanup()
                except Exception:
                    pass
                self.ui_queue.put(("preview_fail", str(exc)))
            finally:
                self._publish_lock.release()

        threading.Thread(target=work, daemon=True).start()

    def _publish_now(self) -> None:
        if self._publish_lock.locked():
            messagebox.showinfo("正在发布", "当前有发布任务在进行中，请稍候。")
            return
        post = self._save_post(silent=True)
        if post is None:
            return
        if not messagebox.askyesno(
            "立即发布",
            f"确定现在就把这条发到朋友圈吗？\n\n{post.publish_at.strftime('%Y-%m-%d %H:%M')}\n{post.title}\n\n"
            "这是真实发布，好友可以看到。",
        ):
            return
        threading.Thread(target=lambda: self._publish_worker(post, manual=True), daemon=True).start()

    # ---------------- 状态更新 ----------------
    def _update_sched_status(self) -> None:
        if self._stop_event.is_set():
            self.sched_dot.configure(fg=MUTED)
            self.sched_status.configure(text="调度已停止")
        elif self._paused.is_set():
            self.sched_dot.configure(fg=ORANGE)
            self.sched_status.configure(text="调度已暂停")
        else:
            self.sched_dot.configure(fg=GREEN)
            self.sched_status.configure(text="调度进行中")

    def _tick_status(self) -> None:
        if self._closing:
            return
        try:
            main = self._ui_finder.find_main_window()
        except Exception:
            main = None
        if main:
            self.wechat_dot.configure(fg=GREEN)
            self.wechat_status.configure(text="微信已连接")
        else:
            self.wechat_dot.configure(fg=RED)
            self.wechat_status.configure(text="微信未连接")
        latest, _ = store.latest_login_account()
        configured = self.cfg.get("account_wxid", "")
        if latest and configured and latest != configured:
            self.wechat_dot.configure(fg=ORANGE)
            self.wechat_status.configure(text=f"账号提示：{latest}")

        next_post = None
        try:
            for p in store.list_posts():
                if p.status == store.STATUS_SCHEDULED:
                    next_post = p
                    break
        except Exception:
            pass
        if next_post:
            delta = next_post.publish_at - store.now_local()
            total = int(max(0, delta.total_seconds()))
            h, rem = divmod(total, 3600)
            m, s = divmod(rem, 60)
            when = f"{h}小时{m}分" if h else (f"{m}分{s}秒" if m else f"{s}秒")
            self.next_label.configure(
                text=f"下一条：{next_post.publish_at.strftime('%m-%d %H:%M')}（还有 {when}）"
            )
        else:
            self.next_label.configure(text="下一条：暂无待发布计划")
        self._update_sched_status()
        self.after(1000, self._tick_status)

    # ---------------- UI 队列 ----------------
    def _poll_ui_queue(self) -> None:
        if self._closing:
            return
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                level, msg = payload
                self._append_log(level, msg)
            elif kind == "refresh":
                self._refresh_list(select_id=payload)
            elif kind == "preview_ok":
                self._append_log("INFO", f"已填充，预览截图：{payload}")
                messagebox.showinfo(
                    "已填充",
                    "朋友圈编辑框已填好，但还没有发表。\n\n"
                    "请切换到微信检查；检查完后点“取消”，选择“不保留”。\n\n"
                    f"预览截图：\n{payload}",
                )
            elif kind == "preview_fail":
                self._append_log("ERROR", f"预览填充失败：{payload}")
                messagebox.showerror("预览失败", str(payload))
            elif kind == "published":
                pid, ok, msg = payload
                if ok:
                    self._append_log("INFO", "发布成功。")
                    self._refresh_list(select_id=None)
                else:
                    self._append_log("ERROR", f"发布失败：{msg}")
                    self._refresh_list(select_id=pid)
        self.after(200, self._poll_ui_queue)

    def _append_log(self, level: str, msg: str) -> None:
        self.log_box.configure(state="normal")
        tag = level if level in ("WARNING", "ERROR") else "INFO"
        self.log_box.insert("end", msg + "\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        # 控制日志长度
        if int(self.log_box.index("end-1c").split(".")[0]) > 400:
            self.log_box.delete("1.0", "100.0")

    # ---------------- 关闭 ----------------
    def _on_close(self) -> None:
        if messagebox.askyesno("退出 wx_ROBOT", "退出后定时发布将停止。确定退出吗？"):
            self._closing = True
            self._stop_event.set()
            self._wake_event.set()
            try:
                self.destroy()
            except tk.TclError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimized", action="store_true", help="启动后最小化到任务栏")
    args = parser.parse_args()

    enable_dpi_awareness()
    if not _single_instance():
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(APP_NAME, "wx_ROBOT 已经在运行了。\n请在任务栏查看已打开的窗口。")
            root.destroy()
        except Exception:
            pass
        return 0

    app = App(minimized=args.minimized)
    from wechat_ui import WeChatUI as _Finder
    app._ui_finder = _Finder(app.cfg, logger=app.log)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
