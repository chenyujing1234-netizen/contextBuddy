#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPT对话记录查看器
==================
访问「AI对话导出 · 上传服务」,把服务端所有AI平台的对话记录拉取下来,
以会话为单位归整,在图形界面中浏览。

功能
----
- 启动时自动健康检查并加载设备列表
- 支持「全部设备」合并拉取(跨设备去重)或指定设备拉取
- 「全部设备」拉取时若发现未认领历史数据,会提示先认领再自动拉取全部
- 右键打分类:左侧会话列表/右侧消息上右键即可标注分类,分类支持手动输入新建
- 分类导出:按类别把会话(含说话人标注)导出为本地TXT文件
- 支持按平台过滤:DeepSeek / 纳米AI / 腾讯元宝 / Kimi / 通义千问 / 讯飞星火 / 豆包 / 通义(旧版)
- 左侧会话列表:标题、平台、消息数、最后活跃、设备;支持标题/内容关键字搜索
- 右侧消息视图:每条消息均标注说话人(用户/助手/系统/工具)与时间,
  正文、思考过程、附件、引用来源、Token用量一目了然
- 支持「认领历史数据」:把服务端未归属设备的历史数据认领到所选设备

运行
----
零第三方依赖,Python 3.8+ 直接运行:

    python gpt_conversation_viewer.py

服务端接口与鉴权信息来自《AI对话导出-上传服务API对接文档》。
"""

import datetime
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode

# ------------------------------------------------------------------ #
# 服务配置(参考《AI对话导出-上传服务API对接文档》)
# ------------------------------------------------------------------ #
BASE_URL = "http://114.55.254.123:8321"
TOKEN = "525186097a4d4879266580c3c3e72cd9384d0eb50a1f52b7"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "viewer_config.json")

# 分类标注持久化文件:{categories: [...], conv: {key: 分类}, msg: {key: 分类}}
CATEGORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "viewer_categories.json")

FONT_FAMILY = "Microsoft YaHei UI"
FONT_BASE = (FONT_FAMILY, 10)
FONT_SMALL = (FONT_FAMILY, 9)
FONT_BOLD = (FONT_FAMILY, 10, "bold")
FONT_ITALIC = (FONT_FAMILY, 10, "italic")
FONT_TITLE = (FONT_FAMILY, 14, "bold")

PLATFORM_NAMES = {
    "deepseek": "DeepSeek",
    "ncn": "纳米AI",
    "yuanbao": "腾讯元宝",
    "kimi": "Kimi",
    "qianwen": "通义千问",
    "iflytek": "讯飞星火",
    "doubao": "豆包",
    "tongyi": "通义(旧版)",
}
PLATFORM_CODES = {name: code for code, name in PLATFORM_NAMES.items()}

ROLE_NAMES = {"user": "用户", "assistant": "助手", "system": "系统", "tool": "工具"}

ALL_DEVICES = "== 全部设备 =="
ALL_PLATFORMS = "全部平台"


# ------------------------------------------------------------------ #
# HTTP / 工具函数
# ------------------------------------------------------------------ #
def http_json(method, path, params=None, body=None, timeout=30):
    """发起请求并解析JSON响应;失败抛出RuntimeError(含服务端错误详情)"""
    url = BASE_URL + path
    if params:
        url += "?" + urlencode(params)
    data = None
    headers = {"X-Upload-Token": TOKEN}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib_request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib_error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        try:
            detail = json.loads(detail).get("error") or detail
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {detail}") from None
    except urllib_error.URLError as e:
        raise RuntimeError(f"无法连接服务端({e.reason}),请检查网络") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError(f"响应不是JSON: {raw[:200]}") from None


def fmt_dt(value):
    """ISO时间字符串 → 本地时间 'YYYY-MM-DD HH:MM';解析失败原样返回"""
    if not value:
        return "-"
    s = str(value).strip()
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s


def short_dev(device_id):
    """dev-3f2a9c1e-xxxx… → 3f2a9c1e"""
    return (device_id or "").replace("dev-", "", 1)[:8]


def conv_key(c):
    """会话唯一标识:平台 + 会话ID"""
    return "{}:{}".format(c.get("platform"),
                          c.get("conversation_id") or c.get("title"))


def msg_key(c, m, idx):
    """消息唯一标识:优先 message_id,缺省用 平台+会话ID+序号"""
    mid = m.get("message_id")
    if mid:
        return str(mid)
    return "{}:{}:{}".format(c.get("platform"),
                             c.get("conversation_id") or c.get("title"),
                             m.get("seq", idx))


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ------------------------------------------------------------------ #
# 主应用
# ------------------------------------------------------------------ #
class ViewerApp:
    def __init__(self, root):
        self.root = root
        root.title("GPT对话记录查看器")
        root.geometry("1280x800")
        root.minsize(1000, 600)

        self.cfg = load_config()
        self.devices = []          # [{device_id, conversations, messages, last_active}]
        self.device_display = {}   # 下拉框显示文本 -> device_id
        self.conversations = []    # 归整(去重、排序)后的会话列表
        self.view_indices = []     # 搜索过滤后保留的会话索引
        self.keyword = ""
        self.ui_queue = queue.Queue()
        self.orphan_convs = 0           # 未认领历史会话数(device_id 为空的行)
        self._orphan_dismissed = False  # 本次运行用户已选择跳过认领
        self._auto_pull_pending = False # 认领完成后自动继续拉取
        # 右键分类标注(持久化到 CATEGORY_FILE)
        self.categories = []        # 用户自建分类列表
        self.conv_cats = {}         # {会话key: 分类}
        self.msg_cats = {}          # {消息key: 分类}
        self._msg_tag_map = {}      # 渲染时 消息tag名 -> 消息key
        self._load_categories()

        self._build_style()
        self._build_toolbar()
        self._build_statusbar()
        self._build_main()
        self._render_empty_hint()

        self.root.after(80, self._drain_queue)
        self._startup()

    # ---------------------------- 界面搭建 ---------------------------- #
    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=FONT_BASE)
        style.configure("Treeview", font=FONT_BASE, rowheight=30)
        style.configure("Treeview.Heading", font=FONT_BOLD)
        self.root.option_add("*TCombobox*Listbox*Font", FONT_BASE)

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill="x")

        ttk.Label(bar, text="设备:").grid(row=0, column=0, sticky="w")
        self.device_var = tk.StringVar()
        self.device_cb = ttk.Combobox(bar, textvariable=self.device_var,
                                      state="readonly", width=44)
        self.device_cb.grid(row=0, column=1, padx=(4, 8), sticky="w")

        self.refresh_btn = ttk.Button(bar, text="刷新设备",
                                      command=self.refresh_devices, width=10)
        self.refresh_btn.grid(row=0, column=2, padx=(0, 8))

        self.claim_btn = ttk.Button(bar, text="认领历史数据",
                                    command=self.claim_orphans, width=12)
        self.claim_btn.grid(row=0, column=3, padx=(0, 24))

        ttk.Label(bar, text="平台:").grid(row=0, column=4, sticky="w")
        self.platform_var = tk.StringVar(value=ALL_PLATFORMS)
        self.platform_cb = ttk.Combobox(
            bar, textvariable=self.platform_var, state="readonly", width=12,
            values=[ALL_PLATFORMS] + list(PLATFORM_NAMES.values()))
        self.platform_cb.grid(row=0, column=5, padx=(4, 8))

        self.pull_btn = ttk.Button(bar, text="⟳ 拉取数据",
                                   command=self.pull_data, width=12)
        self.pull_btn.grid(row=0, column=6)

        self.export_btn = ttk.Button(bar, text="导出TXT",
                                     command=self.export_txt, width=10)
        self.export_btn.grid(row=0, column=7, padx=(8, 0))

        ttk.Label(bar, text="搜索:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(bar, textvariable=self.search_var, width=44)
        search_entry.grid(row=1, column=1, padx=(4, 8), pady=(8, 0), sticky="w")
        ttk.Label(bar, text="按标题或消息内容过滤", foreground="#6b7280",
                  font=FONT_SMALL).grid(row=1, column=2, sticky="w", pady=(8, 0))
        self.search_var.trace_add("write", self._on_search_changed)

    def _build_statusbar(self):
        bar = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        bar.pack(fill="x", side="bottom")
        self.health_var = tk.StringVar(value="● 正在连接服务…")
        self.health_label = tk.Label(bar, textvariable=self.health_var,
                                     font=FONT_SMALL, fg="#6b7280")
        self.health_label.pack(side="left")
        self.stats_var = tk.StringVar(value="尚未拉取数据")
        ttk.Label(bar, textvariable=self.stats_var,
                  font=FONT_SMALL, foreground="#374151").pack(side="right")

    def _build_main(self):
        main = ttk.PanedWindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        # 左:会话列表
        left = ttk.Frame(main)
        main.add(left, weight=1)
        columns = ("title", "platform", "cat", "count", "last", "device")
        self.tree = ttk.Treeview(left, columns=columns, show="headings",
                                 selectmode="browse")
        self.tree.heading("title", text="会话标题")
        self.tree.column("title", width=300, anchor="w")
        self.tree.heading("platform", text="平台")
        self.tree.column("platform", width=80, anchor="center")
        self.tree.heading("cat", text="分类")
        self.tree.column("cat", width=80, anchor="center")
        self.tree.heading("count", text="消息数")
        self.tree.column("count", width=56, anchor="center")
        self.tree.heading("last", text="最后活跃")
        self.tree.column("last", width=120, anchor="center")
        self.tree.heading("device", text="设备")
        self.tree.column("device", width=80, anchor="center")
        self.tree.tag_configure("odd", background="#f3f4f6")
        ysb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._on_tree_rightclick)

        # 右:消息视图
        right = ttk.Frame(main)
        main.add(right, weight=2)
        self.text = tk.Text(right, wrap="word", state="disabled",
                            padx=14, pady=12, font=FONT_BASE,
                            spacing1=2, spacing3=2)
        ysb2 = ttk.Scrollbar(right, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=ysb2.set)
        self.text.pack(side="left", fill="both", expand=True)
        ysb2.pack(side="right", fill="y")

        t = self.text
        t.tag_configure("title", font=FONT_TITLE, foreground="#111827")
        t.tag_configure("meta", font=FONT_SMALL, foreground="#6b7280")
        t.tag_configure("sep", font=FONT_SMALL, foreground="#d1d5db")
        t.tag_configure("role_user", font=FONT_BOLD, foreground="#1d4ed8")
        t.tag_configure("role_assistant", font=FONT_BOLD, foreground="#047857")
        t.tag_configure("role_system", font=FONT_BOLD, foreground="#b45309")
        t.tag_configure("role_tool", font=FONT_BOLD, foreground="#6d28d9")
        t.tag_configure("time", font=FONT_SMALL, foreground="#9ca3af")
        t.tag_configure("body", foreground="#111827")
        t.tag_configure("thinking", font=FONT_ITALIC, foreground="#6b7280")
        t.tag_configure("attach", font=FONT_SMALL, foreground="#0f766e")
        t.tag_configure("cat", font=FONT_BOLD, foreground="#7c3aed")
        self.text.bind("<Button-3>", self._on_text_rightclick)

    # ---------------------------- 线程调度 ---------------------------- #
    def _run_task(self, kind, fn):
        """在后台线程执行fn,结果通过ui_queue交回主线程处理"""
        def worker():
            try:
                self.ui_queue.put((kind, fn(), None))
            except Exception as e:  # noqa: BLE001
                self.ui_queue.put((kind, None, e))
        threading.Thread(target=worker, daemon=True).start()

    def _drain_queue(self):
        handlers = {
            "startup": self._on_startup,
            "devices": self._on_devices,
            "pull": self._on_pulled,
            "claim": self._on_claimed,
            "progress": self._on_progress,
        }
        try:
            while True:
                kind, result, err = self.ui_queue.get_nowait()
                handler = handlers.get(kind)
                if handler:
                    handler(result, err)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_queue)

    def _set_busy(self, busy, text=None):
        state = "disabled" if busy else "normal"
        for btn in (self.pull_btn, self.refresh_btn, self.claim_btn,
                    self.export_btn):
            btn.config(state=state)
        if text:
            self.stats_var.set(text)

    # ---------------------------- 启动 / 设备 ---------------------------- #
    def _startup(self):
        def task():
            health = http_json("GET", "/health", params={"token": TOKEN}, timeout=8)
            devices = http_json("GET", "/devices", params={"token": TOKEN}, timeout=15)
            return health, devices
        self._run_task("startup", task)

    def _on_startup(self, result, err):
        if err:
            self.health_var.set("● 服务不可达")
            self.health_label.config(fg="#dc2626")
            self.stats_var.set("连接失败,可点击「刷新设备」重试")
            messagebox.showerror("连接失败",
                                 f"无法连接上传服务:\n{err}\n\n"
                                 "请检查网络后点击「刷新设备」重试。")
            return
        health, devices = result or ({}, {})
        if health.get("ok") and health.get("db") == "up":
            self.health_var.set("● 服务正常")
            self.health_label.config(fg="#16a34a")
        else:
            self.health_var.set("● 服务状态异常")
            self.health_label.config(fg="#d97706")
        self._apply_devices(devices)

    def refresh_devices(self):
        self._set_busy(True, "正在刷新设备列表…")
        self._run_task(
            "devices",
            lambda: http_json("GET", "/devices",
                              params={"token": TOKEN}, timeout=15))

    def _on_devices(self, result, err):
        self._set_busy(False)
        if err:
            messagebox.showerror("获取设备列表失败", str(err))
            return
        self._apply_devices(result)

    def _apply_devices(self, devices_resp):
        devices = (devices_resp or {}).get("devices") or []
        self.devices = [d for d in devices if d.get("device_id")]
        self.device_display.clear()
        values = [ALL_DEVICES]
        for d in self.devices:
            label = "{}  ({}会话)".format(d["device_id"], d.get("conversations", 0))
            self.device_display[label] = d["device_id"]
            values.append(label)
        self.device_cb["values"] = values

        chosen = None
        last = self.cfg.get("device")
        if last:
            for label in values[1:]:
                if self.device_display[label] == last:
                    chosen = label
                    break
        self.device_var.set(chosen or ALL_DEVICES)

        if self.cfg.get("platform") in [ALL_PLATFORMS] + list(PLATFORM_NAMES.values()):
            self.platform_var.set(self.cfg["platform"])

        orphan = next((d for d in devices if d.get("device_id") is None), None)
        self.orphan_convs = (orphan or {}).get("conversations") or 0
        if self.orphan_convs:
            self.stats_var.set(
                "提示: 服务端有 {} 个未认领历史会话,「全部设备」拉取时可先认领".format(
                    self.orphan_convs))

        if self._auto_pull_pending:  # 认领完成后自动继续拉取
            self._auto_pull_pending = False
            self.root.after(150, self.pull_data)

    # ---------------------------- 拉取数据 ---------------------------- #
    def _selected_platform_code(self):
        name = self.platform_var.get()
        if name == ALL_PLATFORMS:
            return None
        return PLATFORM_CODES.get(name)

    def _save_selection(self):
        target = self.device_var.get()
        if target != ALL_DEVICES:
            self.cfg["device"] = self.device_display.get(target)
        self.cfg["platform"] = self.platform_var.get()
        save_config(self.cfg)

    def pull_data(self):
        target = self.device_var.get()
        if target == ALL_DEVICES and not self.devices:
            messagebox.showwarning(
                "提示",
                "设备列表为空(没有任何已归属数据的设备)。\n"
                "服务端数据按设备隔离:请先用浏览器插件上传一次数据生成设备后再拉取。")
            return
        # 「全部设备」模式下,未认领的历史数据(device_id 为空)下载接口拉不到:
        # 先引导认领,再自动拉取全部
        if (target == ALL_DEVICES and self.orphan_convs
                and not self._orphan_dismissed and self.devices):
            device_id = self._default_claim_device()
            if device_id and messagebox.askyesno(
                    "发现未认领历史数据",
                    "服务端有 {} 个未归属任何设备的历史会话,按设备隔离规则,"
                    "下载接口无法直接拉取它们。\n\n"
                    "是否先把它们认领到设备:\n{}\n"
                    "然后自动拉取全部数据?(先到先得,不影响已归属数据)".format(
                        self.orphan_convs, device_id)):
                self._claim_then_pull(device_id)
                return
            self._orphan_dismissed = True
        platform = self._selected_platform_code()
        self._save_selection()
        self._set_busy(True, "正在拉取数据…")

        def download_one(device_id):
            body = {"device_id": device_id, "include_messages": True}
            if platform:
                body["platform"] = platform
            resp = http_json("POST", "/download", body=body, timeout=120)
            convs = (resp or {}).get("conversations") or []
            for c in convs:  # 响应的会话内不带device_id,这里补上以便展示
                c["device_id"] = device_id
            return convs

        def task():
            convs = []
            if target == ALL_DEVICES:
                total = len(self.devices)
                for i, d in enumerate(self.devices, 1):
                    self.ui_queue.put(("progress",
                                      "正在拉取设备 {} ({}/{})…".format(
                                          short_dev(d["device_id"]), i, total), None))
                    convs.extend(download_one(d["device_id"]))
            else:
                device_id = self.device_display.get(target)
                if not device_id:
                    raise RuntimeError("所选设备无效,请刷新设备列表")
                convs.extend(download_one(device_id))
            return convs

        self._run_task("pull", task)

    def _on_progress(self, text, _err):
        self.stats_var.set(text)

    def _on_pulled(self, convs, err):
        self._set_busy(False)
        if err:
            self.stats_var.set("拉取失败")
            messagebox.showerror("拉取失败", str(err))
            return
        self.conversations = self._merge(convs)
        self._apply_filter()
        total_msgs = sum(c.get("message_count") or 0 for c in self.conversations)
        total_chars = sum(c.get("char_count") or 0 for c in self.conversations)
        note = (" · 另有 {} 个未认领会话未包含".format(self.orphan_convs)
                if self.orphan_convs else "")
        self.stats_var.set("共 {} 个会话 · {:,} 条消息 · {:,} 字符{}".format(
            len(self.conversations), total_msgs, total_chars, note))

    @staticmethod
    def _merge(convs):
        """跨设备去重(同平台+会话ID保留消息数最多的一份),按最后活跃倒序"""
        by_key = {}
        for c in convs or []:
            key = (c.get("platform"), c.get("conversation_id") or c.get("title"))
            prev = by_key.get(key)
            if prev is None or (c.get("message_count") or 0) > (prev.get("message_count") or 0):
                by_key[key] = c
        merged = list(by_key.values())
        merged.sort(key=lambda c: str(c.get("last_message_at")
                                     or c.get("platform_updated_at")
                                     or c.get("db_updated_at") or ""), reverse=True)
        return merged

    # ---------------------------- 会话列表 / 搜索 ---------------------------- #
    def _on_search_changed(self, *_):
        self.keyword = self.search_var.get()
        self._apply_filter()

    @staticmethod
    def _conv_title(c):
        t = (c.get("title") or "").strip()
        if t:
            return t
        for m in c.get("messages") or []:
            if m.get("role") == "user" and m.get("content"):
                s = str(m["content"]).strip().replace("\n", " ")
                return s[:24] + ("…" if len(s) > 24 else "")
        return "(无标题)"

    @staticmethod
    def _conv_matches(c, kw):
        if kw in (c.get("title") or "").lower():
            return True
        if kw in str(c.get("conversation_id") or "").lower():
            return True
        for m in c.get("messages") or []:
            if kw in str(m.get("content") or "").lower():
                return True
        return False

    def _apply_filter(self):
        kw = self.keyword.strip().lower()
        self.tree.delete(*self.tree.get_children())
        self.view_indices = []
        for i, c in enumerate(self.conversations):
            if kw and not self._conv_matches(c, kw):
                continue
            self.view_indices.append(i)
            self.tree.insert("", "end", iid=str(i),
                              tags=("odd",) if len(self.view_indices) % 2 == 0 else (),
                              values=(
                                  self._conv_title(c),
                                  PLATFORM_NAMES.get(c.get("platform"),
                                                     c.get("platform") or "-"),
                                  self.conv_cats.get(conv_key(c), ""),
                                  c.get("message_count", 0),
                                  fmt_dt(c.get("last_message_at")
                                         or c.get("platform_updated_at")),
                                  short_dev(c.get("device_id")) or "-"))
        self._render_empty_hint()

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.conversations):
            self._render_conversation(self.conversations[idx])

    # ---------------------------- 消息渲染 ---------------------------- #
    def _render_empty_hint(self):
        t = self.text
        t.config(state="normal")
        t.delete("1.0", "end")
        if not self.conversations:
            t.insert("end", "暂无数据。请选择设备与平台后,点击「⟳ 拉取数据」。\n", ("meta",))
        elif not self.view_indices:
            t.insert("end", "没有匹配搜索关键字的会话。\n", ("meta",))
        else:
            t.insert("end", "请在左侧选择一个会话查看对话内容。\n", ("meta",))
        t.config(state="disabled")

    def _render_conversation(self, c):
        t = self.text
        t.config(state="normal")
        t.delete("1.0", "end")

        t.insert("end", self._conv_title(c) + "\n", ("title",))
        meta = "  |  ".join([
            "平台: {}".format(PLATFORM_NAMES.get(c.get("platform"),
                                                  c.get("platform") or "-")),
            "设备: {}".format(c.get("device_id") or "-"),
            "会话ID: {}".format(c.get("conversation_id") or "-"),
            "消息数: {}".format(c.get("message_count", 0)),
            "字符数: {:,}".format(c.get("char_count") or 0),
        ])
        t.insert("end", meta + "\n", ("meta",))
        times = []
        if c.get("platform_created_at"):
            times.append("创建于 {}".format(fmt_dt(c["platform_created_at"])))
        if c.get("last_message_at") or c.get("platform_updated_at"):
            times.append("最后活跃 {}".format(
                fmt_dt(c.get("last_message_at") or c.get("platform_updated_at"))))
        if times:
            t.insert("end", "  ·  ".join(times) + "\n", ("meta",))
        ccat = self.conv_cats.get(conv_key(c))
        if ccat:
            t.insert("end", "  【分类:{}】\n".format(ccat), ("cat",))
        t.insert("end", "─" * 64 + "\n", ("sep",))

        self._msg_tag_map = {}
        messages = c.get("messages") or []
        if not messages:
            t.insert("end", "\n(该会话没有消息正文)", ("meta",))
        for i, m in enumerate(messages):
            self._render_message(t, m, c, i)
        t.config(state="disabled")

    @staticmethod
    def _as_list(value):
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        if isinstance(value, dict):
            return [value]
        return []

    def _render_message(self, t, m, c, idx):
        # 用 tag 覆盖整条消息区域,供右键菜单定位;key 用于分类持久化
        key = msg_key(c, m, idx)
        tag = "m{}".format(idx)
        self._msg_tag_map[tag] = key
        start = t.index("end-1c")

        role = m.get("role") or "system"
        role_name = ROLE_NAMES.get(role, role)
        role_tag = "role_{}".format(role) if role in ROLE_NAMES else "role_system"
        if m.get("msg_time"):
            t.insert("end", "【{}】".format(role_name), (role_tag,))
            t.insert("end", "  {}".format(fmt_dt(m["msg_time"])), ("time",))
        else:
            t.insert("end", "【{}】".format(role_name), (role_tag,))
        cat = self.msg_cats.get(key)
        if cat:
            t.insert("end", "  【分类:{}】".format(cat), ("cat",))
        t.insert("end", "\n")

        content = str(m.get("content") or "").rstrip()
        thinking = m.get("thinking_content")
        media = self._as_list(m.get("media"))
        tools = self._as_list(m.get("tool"))
        if content:
            t.insert("end", content + "\n", ("body",))
        elif not thinking and not media and not tools:
            t.insert("end", "(空消息)\n", ("meta",))
        if thinking:
            t.insert("end", "[思考过程]\n", ("thinking",))
            t.insert("end", str(thinking).rstrip() + "\n", ("thinking",))
        for item in media:
            name = item.get("name") or item.get("type") or "文件"
            t.insert("end", "[附件] {}\n".format(name), ("attach",))
            if item.get("url"):
                t.insert("end", "    {}\n".format(item["url"]), ("attach",))
        for item in tools:
            title = item.get("title") or item.get("type") or "引用"
            t.insert("end", "[引用/工具] {}\n".format(title), ("attach",))
            if item.get("url"):
                t.insert("end", "    {}\n".format(item["url"]), ("attach",))
        if m.get("token_usage"):
            t.insert("end", "Token用量: {}\n".format(m["token_usage"]), ("meta",))
        t.insert("end", "\n")
        t.tag_add(tag, start, "end-1c")

    # ---------------------------- 右键分类标注 ---------------------------- #
    def _load_categories(self):
        try:
            with open(CATEGORY_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.categories = [str(x) for x in (d.get("categories") or [])]
            self.conv_cats = dict(d.get("conv") or {})
            self.msg_cats = dict(d.get("msg") or {})
        except Exception:
            self.categories = []
            self.conv_cats = {}
            self.msg_cats = {}

    def _save_categories(self):
        try:
            with open(CATEGORY_FILE, "w", encoding="utf-8") as f:
                json.dump({"categories": self.categories,
                           "conv": self.conv_cats,
                           "msg": self.msg_cats},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _on_tree_rightclick(self, event):
        """左侧列表右键:给选中的会话打分类"""
        iid = self.tree.identify_row(event.y)
        if not iid or int(iid) >= len(self.conversations):
            return
        self.tree.selection_set(iid)  # 聚焦该行(同时刷新右侧视图)
        c = self.conversations[int(iid)]
        key = conv_key(c)
        self._popup_category_menu(event, "conv", key, self.conv_cats.get(key))

    def _on_text_rightclick(self, event):
        """右侧消息区右键:给点中的消息打分类"""
        idx = self.text.index("@{},{}".format(event.x, event.y))
        key = next((self._msg_tag_map[tg]
                    for tg in self.text.tag_names(idx)
                    if tg in self._msg_tag_map), None)
        if not key:
            return
        self._popup_category_menu(event, "msg", key, self.msg_cats.get(key))

    def _popup_category_menu(self, event, kind, key, current):
        menu = tk.Menu(self.root, tearoff=0)
        if not self.categories:
            menu.add_command(label="(暂无分类,请新建)", state="disabled")
        for cat in self.categories:
            menu.add_command(
                label=("✓ " if cat == current else "　") + cat,
                command=lambda c=cat: self._assign_category(kind, key, c))
        menu.add_separator()
        menu.add_command(label="新建分类…",
                         command=lambda: self._new_category_then_assign(kind, key))
        if current:
            menu.add_command(label="清除分类",
                             command=lambda: self._clear_category(kind, key))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _assign_category(self, kind, key, cat):
        (self.conv_cats if kind == "conv" else self.msg_cats)[key] = cat
        self._save_categories()
        self._refresh_after_tagging(kind)

    def _clear_category(self, kind, key):
        (self.conv_cats if kind == "conv" else self.msg_cats).pop(key, None)
        self._save_categories()
        self._refresh_after_tagging(kind)

    def _new_category_then_assign(self, kind, key):
        name = (simpledialog.askstring("新建分类", "请输入新的分类名称:",
                                       parent=self.root) or "").strip()
        if not name:
            return
        if name not in self.categories:
            self.categories.append(name)
        self._assign_category(kind, key, name)

    def _refresh_after_tagging(self, kind):
        """标注后刷新视图,保持选中状态与滚动位置"""
        frac = self.text.yview()[0]
        sel = self.tree.selection()
        if kind == "conv":
            iid = sel[0] if sel else None
            self._apply_filter()
            if iid and self.tree.exists(iid):
                self.tree.selection_set(iid)  # 触发右侧重渲染
        elif sel:
            self._render_conversation(self.conversations[int(sel[0])])
        self.text.yview_moveto(frac)

    # ---------------------------- 导出 TXT ---------------------------- #
    def export_txt(self):
        if not self.conversations:
            messagebox.showwarning("提示", "尚未拉取数据,请先点击「⟳ 拉取数据」。")
            return
        options = (["全部会话"] + list(self.categories)
                   + (["未分类会话"] if self.categories else []))
        dlg = tk.Toplevel(self.root)
        dlg.title("导出 TXT")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.wait_visibility()
        dlg.grab_set()
        ttk.Label(dlg, text="选择要导出的数据类别:",
                  font=FONT_BOLD).grid(row=0, column=0, columnspan=2,
                                       padx=16, pady=(16, 6), sticky="w")
        var = tk.StringVar(value=options[0])
        cb = ttk.Combobox(dlg, textvariable=var, state="readonly",
                          values=options, width=22)
        cb.grid(row=1, column=0, columnspan=2, padx=16, sticky="w")
        info = ttk.Label(dlg, text="", foreground="#6b7280")
        info.grid(row=2, column=0, columnspan=2, padx=16, pady=(6, 12), sticky="w")

        def update_info(*_):
            convs = self._convs_for_export(var.get())
            info.config(text="共 {} 个会话 · {:,} 条消息".format(
                len(convs),
                sum(c.get("message_count") or 0 for c in convs)))

        var.trace_add("write", update_info)
        update_info()

        def do_export():
            convs = self._convs_for_export(var.get())
            if not convs:
                messagebox.showwarning("提示", "所选类别下没有会话。", parent=dlg)
                return
            name = var.get()
            for ch in '\\/:*?"<>|':
                name = name.replace(ch, "_")
            path = filedialog.asksaveasfilename(
                parent=dlg, title="保存TXT文件",
                initialdir=os.path.dirname(os.path.abspath(__file__)),
                initialfile="GPT对话导出_{}_{}.txt".format(
                    name, datetime.datetime.now().strftime("%Y%m%d_%H%M")),
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
            if not path:
                return
            try:
                self._write_txt(path, convs, var.get())
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("导出失败", str(e), parent=dlg)
                return
            dlg.destroy()
            self.stats_var.set("已导出 {} 个会话 → {}".format(len(convs), path))
            messagebox.showinfo("导出完成", "已导出 {} 个会话到:\n{}".format(
                len(convs), path))

        ttk.Button(dlg, text="导出", command=do_export).grid(
            row=3, column=0, padx=(16, 4), pady=(0, 16), sticky="e")
        ttk.Button(dlg, text="取消", command=dlg.destroy).grid(
            row=3, column=1, padx=(4, 16), pady=(0, 16), sticky="w")
        dlg.wait_window()

    def _convs_for_export(self, option):
        """按导出选项筛选会话"""
        if option == "全部会话":
            return list(self.conversations)
        if option == "未分类会话":
            return [c for c in self.conversations
                    if conv_key(c) not in self.conv_cats]
        return [c for c in self.conversations
                if self.conv_cats.get(conv_key(c)) == option]

    def _write_txt(self, path, convs, option):
        bar = "=" * 78
        out = ["GPT对话记录导出 · 类别:{} · 会话:{}个 · 导出时间:{}".format(
                   option, len(convs),
                   datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
               ""]
        for i, c in enumerate(convs, 1):
            out.append(bar)
            out.append("【会话 {}/{}】{}".format(i, len(convs), self._conv_title(c)))
            meta = "    平台:{} | 设备:{} | 会话ID:{} | 消息数:{}".format(
                PLATFORM_NAMES.get(c.get("platform"), c.get("platform") or "-"),
                c.get("device_id") or "-", c.get("conversation_id") or "-",
                c.get("message_count", 0))
            ccat = self.conv_cats.get(conv_key(c))
            if ccat:
                meta += " | 分类:" + ccat
            out.append(meta)
            times = []
            if c.get("platform_created_at"):
                times.append("创建于 " + fmt_dt(c["platform_created_at"]))
            if c.get("last_message_at") or c.get("platform_updated_at"):
                times.append("最后活跃 " + fmt_dt(
                    c.get("last_message_at") or c.get("platform_updated_at")))
            if times:
                out.append("    " + " · ".join(times))
            out.append(bar)
            out.append("")
            messages = c.get("messages") or []
            if not messages:
                out.append("    (无消息正文)")
            for j, m in enumerate(messages):
                out.extend(self._message_to_lines(c, m, j))
            out.append("")
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(out))

    def _message_to_lines(self, c, m, idx):
        lines = []
        head = "【{}】".format(ROLE_NAMES.get(m.get("role") or "system",
                                            m.get("role")))
        if m.get("msg_time"):
            head += "  " + fmt_dt(m["msg_time"])
        mcat = self.msg_cats.get(msg_key(c, m, idx))
        if mcat:
            head += "  【分类:{}】".format(mcat)
        lines.append(head)
        content = str(m.get("content") or "").rstrip()
        if content:
            lines.extend("    " + ln for ln in content.splitlines())
        thinking = m.get("thinking_content")
        if thinking:
            lines.append("    [思考过程]")
            lines.extend("    " + ln
                         for ln in str(thinking).rstrip().splitlines())
        for item in self._as_list(m.get("media")):
            lines.append("    [附件] {}".format(item.get("name")
                                                or item.get("type") or "文件"))
            if item.get("url"):
                lines.append("        " + item["url"])
        for item in self._as_list(m.get("tool")):
            lines.append("    [引用/工具] {}".format(item.get("title")
                                                     or item.get("type") or "引用"))
            if item.get("url"):
                lines.append("        " + item["url"])
        if m.get("token_usage"):
            lines.append("    Token用量: {}".format(m["token_usage"]))
        lines.append("")
        return lines

    # ---------------------------- 认领历史数据 ---------------------------- #
    def _default_claim_device(self):
        """认领目标设备:优先上次选择的设备,否则第一个设备"""
        last = self.cfg.get("device")
        if last and any(d["device_id"] == last for d in self.devices):
            return last
        return self.devices[0]["device_id"] if self.devices else None

    def _claim_then_pull(self, device_id):
        """认领历史数据,完成后刷新设备列表并自动继续拉取"""
        self._orphan_dismissed = True
        self._auto_pull_pending = True
        self._save_selection()
        self._set_busy(True, "正在认领历史数据…")
        self._run_task("claim",
                       lambda: http_json("POST", "/claim",
                                         body={"device_id": device_id}, timeout=120))

    def claim_orphans(self):
        target = self.device_var.get()
        if target == ALL_DEVICES or not target:
            messagebox.showwarning(
                "提示", "认领需要指定目标设备:请先在设备下拉框选择一个具体设备。")
            return
        device_id = self.device_display.get(target)
        if not device_id:
            messagebox.showerror("错误", "所选设备无效,请刷新设备列表。")
            return
        if not messagebox.askyesno(
                "确认认领",
                "将把服务端中尚未归属任何设备的历史数据,一次性认领到设备:\n{}\n\n"
                "先到先得,已被其他设备认领的数据不会被抢走。是否继续?".format(device_id)):
            return
        self._set_busy(True, "正在认领历史数据…")
        self._save_selection()
        self._run_task("claim",
                       lambda: http_json("POST", "/claim",
                                         body={"device_id": device_id}, timeout=60))

    def _on_claimed(self, result, err):
        self._set_busy(False)
        if err:
            self._auto_pull_pending = False
            messagebox.showerror("认领失败", str(err))
            return
        n = (result or {}).get("claimed", 0)
        if self._auto_pull_pending:
            # 「认领后拉取」流程:不弹窗,刷新设备列表后自动继续拉取
            self.stats_var.set("已认领 {} 个历史会话,正在拉取全部数据…".format(n))
            self.refresh_devices()
            return
        messagebox.showinfo("认领完成",
                            "本次认领了 {} 个历史会话。\n\n"
                            "点击「⟳ 拉取数据」即可查看最新数据。".format(n))
        self.refresh_devices()


def main():
    try:  # Windows高DPI适配,让界面文字清晰
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    ViewerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
