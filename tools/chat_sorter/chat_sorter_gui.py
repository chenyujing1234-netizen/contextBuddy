"""文件传输助手聊天分类工具——tkinter 界面。

流程：选好源文件与素材根目录 -> 点“加载”先展示全部消息，双击行弹出内容详情
并在窗口内标注“涉及人物”（从列表选择或新增）-> 点“分析”（LLM 把人物标注一并
考虑，逐批推荐目录，只推荐不写盘）-> 在表格中查看/修改目标目录 -> 点“确认写入”
把消息连同人物标注一起追加到各目录的 txt。
LLM 配置保存在脚本旁的 llm_config.json，下次启动自动加载。
"""

from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import chat_sorter_core as core

DEFAULT_SOURCE = (
    r"D:\weChat_assistance\素材\我\我的微信导出数据\wxid_jzemtwvdyvml21"
    r"\structured_data\chat_txt\文件传输助手.txt"
)
DEFAULT_ROOT = r"D:\weChat_assistance\素材"
DEFAULT_OUTPUT = "微信聊天记录.txt"
# 默认使用智谱 GLM 的 OpenAI 兼容地址（与 ContextBuddy 内置服务商一致），可改成任意兼容服务
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4.6"
CONFIG_PATH = Path(__file__).with_name("llm_config.json")
# 分类缓存：同一消息上次分析过就不再调 LLM，用户确认过的目录直接沿用
CACHE_PATH = Path(__file__).with_name("classify_cache.json")
UNCLASSIFIED = "未分类"
# 废弃状态：系统/附件消息自动废弃 + 手动标为废弃；废弃行不分析、不写入
DISCARDED = "数据废弃"
# 展示下限：汉字不足 10 个的行（无中文/超短消息/数字/链接/系统附件等）加载时直接过滤
MIN_CHINESE_CHARS = 10


def load_llm_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_llm_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


class ChatSorterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("微信聊天分类工具（文件传输助手·LLM 判定）")
        self.geometry("1180x780")
        self.minsize(960, 620)

        self.assignments: list[dict] = []  # [{"message", "result", "target"}]
        self.candidates: list[core.CandidateDir] = []
        self.cand_by_display: dict[str, core.CandidateDir] = {}
        self._queue: queue.Queue = queue.Queue()
        self._cancel_event: threading.Event | None = None  # 分析运行中的取消信号
        self._analysis_started: float = 0.0  # 本轮分析开始时刻（monotonic），用于显示已用秒数
        self._msg_index: dict[int, int] = {}  # id(消息) -> 行号，用于把批次结果映射回表格行
        self._row_count = 0  # 上次填充的行数（detach 隐藏的行清表时也要删掉）
        self._cache: dict = {}  # 分类缓存 entries（加载时从磁盘读取）
        self._custom_people: list[str] = []  # 用户新增的列表外人物（持久化在缓存文件）
        self._msg_people: dict[str, list[str]] = {}  # message_key -> 人物标注（持久化，重启后恢复）
        self._msg_discarded: dict[str, bool] = {}  # message_key -> 废弃标记（持久化；False=保留系统/附件行）
        self._msg_exported: dict[str, str] = {}  # message_key -> 已导出日期（持久化，导出时去重）
        self._loaded_sig: tuple | None = None  # 已加载数据的 (源文件, 根目录, 深度)
        self._dir_editor: ttk.Combobox | None = None  # 行内目录编辑器（双击“推荐目录”列出现）
        cfg = load_llm_config()
        # 人物消息导出目录：记住上一次的选择，导出时不再重复询问
        self._export_dir: str = str(cfg.get("export_dir", "") or "")
        # 废弃关键词（内容包含即废弃）：默认用内置列表，用户可在界面修改
        self.noise_markers: list[str] = [
            str(m).strip() for m in cfg.get("noise_markers", []) if str(m).strip()
        ] or list(core.DEFAULT_NOISE_MARKERS)
        core.set_noise_markers(self.noise_markers)

        self._build_ui(cfg)
        self.after(100, self._poll_queue)
        # 启动时自动加载数据：界面先显示，再静默加载，无需手动点“加 载”
        self.after(150, self._auto_load)

    # ---------- 界面搭建 ----------

    def _build_ui(self, cfg: dict) -> None:
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="源文件:").grid(row=0, column=0, sticky="w", pady=2)
        self.src_var = tk.StringVar(value=cfg.get("source", DEFAULT_SOURCE))
        ttk.Entry(top, textvariable=self.src_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="浏览…", command=self._browse_source).grid(row=0, column=2)

        ttk.Label(top, text="素材根目录:").grid(row=1, column=0, sticky="w", pady=2)
        self.root_var = tk.StringVar(value=cfg.get("root", DEFAULT_ROOT))
        ttk.Entry(top, textvariable=self.root_var).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="浏览…", command=self._browse_root).grid(row=1, column=2)

        ttk.Label(top, text="LLM API:").grid(row=2, column=0, sticky="w", pady=2)
        api_row = ttk.Frame(top)
        api_row.grid(row=2, column=1, columnspan=2, sticky="ew")
        self.base_var = tk.StringVar(value=cfg.get("base_url", DEFAULT_BASE_URL))
        ttk.Entry(api_row, textvariable=self.base_var).pack(side="left", fill="x", expand=True, padx=(4, 8))
        ttk.Label(api_row, text="模型:").pack(side="left")
        self.model_var = tk.StringVar(value=cfg.get("model", DEFAULT_MODEL))
        ttk.Entry(api_row, textvariable=self.model_var, width=16).pack(side="left", padx=(3, 8))
        ttk.Label(api_row, text="密钥:").pack(side="left")
        self.key_var = tk.StringVar(value=cfg.get("api_key", ""))
        ttk.Entry(api_row, textvariable=self.key_var, show="*", width=26).pack(side="left", padx=3)
        ttk.Label(api_row, text="并发:").pack(side="left")
        self.conc_var = tk.IntVar(value=int(cfg.get("concurrency", 4)))

        ttk.Label(top, text="写入文件名:").grid(row=3, column=0, sticky="w", pady=2)
        out_row = ttk.Frame(top)
        out_row.grid(row=3, column=1, columnspan=2, sticky="ew")
        self.out_var = tk.StringVar(value=cfg.get("output_name", DEFAULT_OUTPUT))
        ttk.Entry(out_row, textvariable=self.out_var, width=24).pack(side="left", padx=(4, 14))
        ttk.Label(out_row, text="每批条数:").pack(side="left")
        self.batch_var = tk.IntVar(value=int(cfg.get("batch_size", 20)))
        ttk.Spinbox(out_row, from_=5, to=50, textvariable=self.batch_var, width=5).pack(side="left", padx=3)
        ttk.Spinbox(out_row, from_=1, to=8, textvariable=self.conc_var, width=5).pack(side="left", padx=(14, 0))
        ttk.Label(out_row, text="目录深度:").pack(side="left", padx=(14, 0))
        self.depth_var = tk.IntVar(value=int(cfg.get("max_depth", 1)))
        ttk.Spinbox(out_row, from_=1, to=5, textvariable=self.depth_var, width=5).pack(side="left", padx=3)
        ttk.Label(out_row, text="只加载最近(天，0=全部):").pack(side="left", padx=(14, 0))
        self.recent_var = tk.IntVar(value=int(cfg.get("recent_days", 0)))
        ttk.Spinbox(out_row, from_=0, to=3650, textvariable=self.recent_var, width=6).pack(side="left", padx=3)
        self.load_btn = ttk.Button(out_row, text="加 载", command=self._load_data)
        self.load_btn.pack(side="left", padx=(18, 4))
        self.analyze_btn = ttk.Button(out_row, text="分 析", command=self._start_analysis)
        self.analyze_btn.pack(side="left", padx=(0, 4))

        self.status_var = tk.StringVar(
            value="先点“加 载”展示全部消息，双击行查看内容并标注“涉及人物”，再点“分 析”让 LLM 推荐目录（分析不写任何文件）。"
        )
        ttk.Label(self, textvariable=self.status_var, padding=(12, 2)).pack(fill="x")

        # 目录下拉行：放在列表上方，方便查看与批量修改
        assign = ttk.Frame(self, padding=(10, 4))
        assign.pack(fill="x")
        ttk.Label(assign, text="将选中行的目标目录改为:").pack(side="left")
        self.dir_var = tk.StringVar(value=UNCLASSIFIED)
        self.dir_combo = ttk.Combobox(assign, textvariable=self.dir_var, state="disabled", width=44)
        self.dir_combo.pack(side="left", padx=4)
        self.apply_btn = ttk.Button(assign, text="应用到选中行", command=self._apply_target, state="disabled")
        self.apply_btn.pack(side="left")
        ttk.Button(
            assign, text="全选", command=lambda: self.tree.selection_set(self.tree.get_children())
        ).pack(side="left", padx=(12, 0))
        ttk.Button(
            assign, text="全不选", command=lambda: self.tree.selection_remove(self.tree.selection())
        ).pack(side="left")
        # 隐藏废弃项：默认勾选，表格中不显示已废弃（系统/附件 + 手动标记）的行
        self.hide_discarded_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            assign, text="隐藏废弃项", variable=self.hide_discarded_var,
            command=self._apply_hide_discarded,
        ).pack(side="left", padx=(12, 0))
        ttk.Button(assign, text="废弃关键词…", command=self._edit_noise_markers).pack(
            side="left", padx=(12, 0)
        )

        # 列表区：先建容器，Treeview 必须以容器为父级，
        # 否则列表会被排到主窗口底部，中间只剩一片空白
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=4)

        columns = ("idx", "time", "preview", "people", "dir", "reason")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        for col, text, width, anchor in (
            ("idx", "序号", 52, "e"),
            ("time", "时间", 132, "w"),
            ("preview", "内容预览", 380, "w"),
            ("people", "涉及人物（双击标注）", 140, "w"),
            ("dir", "推荐目录（可修改）", 220, "w"),
            ("reason", "LLM 理由", 190, "w"),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "preview"))
        self.tree.tag_configure("noise", foreground="#888888")
        self.tree.tag_configure("unmatched", foreground="#b06000")
        self.tree.tag_configure("pending", foreground="#7a7aa0")  # 分析中、尚未出结果的行
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        # 右击行弹出“废弃/取消废弃”菜单（对全部选中行生效）
        self.ctx_menu = tk.Menu(self, tearoff=0)
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        ybar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")

        bottom = ttk.Frame(self, padding=(10, 6))
        bottom.pack(fill="x")
        self.confirm_btn = ttk.Button(bottom, text="确认写入", command=self._confirm_write, state="disabled")
        self.confirm_btn.pack(side="left")
        self.export_people_btn = ttk.Button(
            bottom, text="导出人物消息", command=self._export_people_messages, state="disabled"
        )
        self.export_people_btn.pack(side="left", padx=(10, 0))
        self.change_export_dir_btn = ttk.Button(
            bottom, text="更换导出目录", command=self._choose_export_dir
        )
        self.change_export_dir_btn.pack(side="left", padx=(6, 0))
        ttk.Label(
            bottom,
            text="“确认写入”把消息追加到各自目标目录的 txt（已存在的行自动跳过）；"
                 "“导出人物消息”把所有有人物标注的行合并成一个 txt 存入导出目录。",
        ).pack(side="left", padx=10)

        # 导出结果提示：显示在“导出人物消息”按钮行的正下方
        self.export_status_var = tk.StringVar(value="")
        ttk.Label(
            self, textvariable=self.export_status_var, padding=(12, 0), foreground="#2a6f2a"
        ).pack(fill="x")

        self.result_text = scrolledtext.ScrolledText(self, height=9, wrap="word", state="disabled")
        self.result_text.pack(fill="both", padx=10, pady=(4, 10))

    # ---------- 事件处理 ----------

    def _browse_source(self) -> None:
        path = filedialog.askopenfilename(
            title="选择会话 txt 文件", filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if path:
            self.src_var.set(path)

    def _browse_root(self) -> None:
        path = filedialog.askdirectory(title="选择素材根目录")
        if path:
            self.root_var.set(path)

    def _load_data(self) -> bool:
        """解析源文件 + 扫描候选目录 + 读取人物标注缓存，先把所有数据展示到表格。

        缓存命中的消息（上次分析过/已确认写入）直接定稿显示；分析前即可标注
        “涉及人物”与手动指定目录。返回是否加载成功。
        """
        if self._cancel_event is not None:
            messagebox.showinfo("分析进行中", "请等本轮分析结束后再重新加载数据。")
            return False
        src = self.src_var.get().strip()
        root = self.root_var.get().strip()
        if not src or not Path(src).is_file():
            messagebox.showerror("缺少源文件", "请先选择有效的会话 txt 文件。")
            return False
        if not root or not Path(root).is_dir():
            messagebox.showerror("缺少素材根目录", "请先选择有效的素材根目录。")
            return False
        depth = max(1, min(5, int(self.depth_var.get() or 1)))
        recent_days = max(0, int(self.recent_var.get() or 0))
        try:
            messages = core.parse_chat_txt(src)
            candidates = core.scan_candidate_dirs(root, max_depth=depth)
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))
            return False

        # 只加载最近 N 天（0=全部）：按消息日期（含当天）往前推 N 天，更早的不展示、不分析、不写入
        recent_dropped = 0
        if recent_days:
            cutoff = (date.today() - timedelta(days=recent_days - 1)).isoformat()
            before = len(messages)
            messages = [m for m in messages if m.time_str[:10] >= cutoff]
            recent_dropped = before - len(messages)

        # 过滤：没有汉字或汉字不足 10 个的行（数字/链接/超短消息/系统附件等）不展示、不分析、不写入
        total = len(messages)
        messages = [
            m for m in messages
            if core.chinese_char_count(m.content) >= MIN_CHINESE_CHARS
        ]
        filtered = total - len(messages)

        cache_data = core.load_cache_data(CACHE_PATH)
        self._cache = cache_data["entries"]
        self._custom_people = cache_data["people"]
        self._msg_people = cache_data["msg_people"]
        # 涉及人员固定包含“我”：历史标注一并补上（空标注保持为空）
        for key, people in self._msg_people.items():
            self._msg_people[key] = core.ensure_self(people)
        self._msg_discarded = cache_data["msg_discarded"]
        self._msg_exported = cache_data["msg_exported"]
        for m in messages:  # 恢复上次标注的人物
            key = core.message_key(m)
            if key in self._msg_people:
                m.people = list(self._msg_people[key])

        self.candidates = candidates
        self.cand_by_display = {core.display_rel(c): c for c in candidates}
        self.assignments = [
            {"message": m, "result": None, "target": None, "discarded": self._is_discarded(m)}
            for m in messages
        ]
        self._msg_index = {id(m): i for i, m in enumerate(messages)}
        self._loaded_sig = (src, root, depth, recent_days)
        self._populate_rows_initial()

        self.dir_combo.configure(values=[UNCLASSIFIED, DISCARDED] + list(self.cand_by_display))
        self.dir_combo.set(UNCLASSIFIED)
        self.dir_combo.configure(state="readonly")
        self.apply_btn.configure(state="normal")
        self.export_people_btn.configure(state="normal")
        discarded_cnt = sum(1 for a in self.assignments if a["discarded"])
        self.status_var.set(
            f"已加载 {len(messages)} 条消息、{len(candidates)} 个候选目录"
            + (f"（最近 {recent_days} 天外已过滤 {recent_dropped} 条）" if recent_dropped else "")
            + (f"（已过滤 {filtered} 条汉字不足 {MIN_CHINESE_CHARS} 个的行）" if filtered else "")
            + (f"，其中废弃 {discarded_cnt} 条" if discarded_cnt else "")
            + "。双击行弹出内容详情并可修改“涉及人物”；右击行可标记/取消废弃；点“分 析”让 LLM 结合人物推荐目录。"
        )
        return True

    def _auto_load(self) -> None:
        """启动时自动加载：路径均有效就直接展示全部消息，无效则提示后由手动加载。"""
        src = self.src_var.get().strip()
        root = self.root_var.get().strip()
        if not src or not Path(src).is_file() or not root or not Path(root).is_dir():
            self.status_var.set("启动自动加载跳过：请设置有效的源文件与素材根目录后点“加 载”。")
            return
        self._load_data()

    def _is_discarded(self, msg) -> bool:
        """行是否废弃：手动标记（True=废弃 / False=保留）优先，否则系统/附件消息自动废弃。"""
        flag = self._msg_discarded.get(core.message_key(msg))
        if flag is not None:
            return flag
        return core.is_noise_content(msg.content)

    def _populate_rows_initial(self) -> None:
        """把当前 assignments 全部填进表格：废弃/缓存命中直接定稿，其余待分析。"""
        # 清空旧行：被“隐藏废弃项”detach 的行不在 get_children 里，需按旧行号逐个清掉
        self.tree.delete(*self.tree.get_children(""))
        for i in range(self._row_count):
            if self.tree.exists(str(i)):
                self.tree.delete(str(i))
        self._row_count = len(self.assignments)
        cached_hits = 0
        for i, item in enumerate(self.assignments):
            msg = item["message"]
            preview = msg.content.replace("\\n", " ")
            if len(preview) > 70:
                preview = preview[:70] + "…"
            people_text = "、".join(msg.people)
            if item["discarded"]:
                reason = (
                    "系统/附件消息，已废弃"
                    if core.is_noise_content(msg.content) and not self._msg_discarded.get(core.message_key(msg))
                    else "手动标为废弃"
                )
                values = (i + 1, msg.time_str, preview, people_text, DISCARDED, reason)
                tag = "noise"
            else:
                hit = core.cache_lookup(self._cache, msg, self.cand_by_display)
                if hit is not None:
                    # 缓存命中：上次分析过（或已确认写入）直接显示，不再调 LLM
                    item["result"] = hit
                    item["target"] = hit.candidate
                    cached_hits += 1
                    values = (
                        i + 1, msg.time_str, preview, people_text,
                        core.display_rel(hit.candidate) if hit.candidate else UNCLASSIFIED,
                        hit.reason,
                    )
                    tag = () if hit.candidate else "unmatched"
                else:
                    values = (i + 1, msg.time_str, preview, people_text, "未分析", "")
                    tag = "pending"
            self.tree.insert("", "end", iid=str(i), values=values, tags=(tag,))
        self._apply_hide_discarded()  # “隐藏废弃项”开关状态立即生效
        return cached_hits

    def _save_cache_file(self) -> None:
        """把分类缓存、人物名单、人物标注、废弃标记与导出去重标记整体落盘（失败不打断界面）。"""
        try:
            core.save_cache(
                CACHE_PATH, self._cache,
                people=self._custom_people, msg_people=self._msg_people,
                msg_discarded=self._msg_discarded, msg_exported=self._msg_exported,
            )
        except OSError:
            pass

    def _edit_noise_markers(self) -> None:
        """编辑“内容包含即废弃”的关键词（每行一个，保存后立即重新应用并持久化）。"""
        if self._cancel_event is not None:
            messagebox.showinfo("分析进行中", "请等本轮分析结束后再修改废弃关键词。")
            return
        win = tk.Toplevel(self)
        win.title("废弃关键词")
        win.geometry("560x380")
        win.transient(self)
        ttk.Label(
            win, text="消息内容包含以下任一字符串即自动废弃（每行一个，大小写不敏感；空列表恢复默认）：",
            padding=(10, 8),
        ).pack(anchor="w")
        text = scrolledtext.ScrolledText(win, wrap="none")
        text.pack(fill="both", expand=True, padx=10)
        text.insert("1.0", "\n".join(self.noise_markers))
        text.focus_set()

        def save() -> None:
            markers = [
                ln.strip() for ln in text.get("1.0", "end-1c").splitlines() if ln.strip()
            ]
            self.noise_markers = markers or list(core.DEFAULT_NOISE_MARKERS)
            core.set_noise_markers(self.noise_markers)
            cfg = load_llm_config()
            cfg["noise_markers"] = self.noise_markers
            save_llm_config(cfg)
            win.destroy()
            if self.assignments:
                self._load_data()  # 重新应用废弃判定（缓存中的保留/手动标记不受影响）
                self.status_var.set(
                    f"废弃关键词已保存并重新加载（{len(self.noise_markers)} 个）。"
                )
            else:
                self.status_var.set(f"废弃关键词已保存（{len(self.noise_markers)} 个）。")

        def reset_default() -> None:
            text.delete("1.0", "end")
            text.insert("1.0", "\n".join(core.DEFAULT_NOISE_MARKERS))

        row = ttk.Frame(win)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="保 存", command=save).pack(side="left", padx=4)
        ttk.Button(row, text="恢复默认", command=reset_default).pack(side="left", padx=4)
        ttk.Button(row, text="取 消", command=win.destroy).pack(side="left", padx=4)

    def _set_row_discard(self, idx: int, iid: str, discarded: bool) -> None:
        """标记/取消一行废弃：手动标记优先于系统/附件自动判定，并立即持久化。

        废弃行不分析、不写入；取消废弃时，系统/附件行记录“保留”（False），
        下次加载不再自动废弃，其余行删掉标记恢复默认判定。
        """
        item = self.assignments[idx]
        msg = item["message"]
        key = core.message_key(msg)
        values = list(self.tree.item(iid, "values"))
        if discarded:
            item["discarded"] = True
            item["target"] = None  # 废弃行不写入
            self._msg_discarded[key] = True
            values[4] = DISCARDED
            values[5] = "手动标为废弃"
            self.tree.item(iid, values=values, tags=("noise",))
            if self.hide_discarded_var.get():
                self.tree.detach(iid)  # “隐藏废弃项”打开时立即隐藏
        else:
            item["discarded"] = False
            if core.is_noise_content(msg.content):
                self._msg_discarded[key] = False  # 记住：这条系统/附件行保留
            else:
                self._msg_discarded.pop(key, None)
            # 恢复上次结果（如有），否则回到待分析显示
            if item["result"] is not None:
                res = item["result"]
                item["target"] = res.candidate
                values[4] = core.display_rel(res.candidate) if res.candidate else UNCLASSIFIED
                values[5] = res.reason
                tags = self._tag_for_result(res)
            else:
                item["target"] = None
                values[4] = "未分析"
                values[5] = ""
                tags = "pending"
            self.tree.item(iid, values=values, tags=(tags,))
            if iid not in self.tree.get_children(""):
                self.tree.move(iid, "", idx)  # 曾被隐藏的行按原行号归位
        self._save_cache_file()

    def _apply_hide_discarded(self) -> None:
        """按“隐藏废弃项”开关隐藏/恢复废弃行（detach 不删数据，可随时恢复）。"""
        hide = self.hide_discarded_var.get()
        visible = set(self.tree.get_children(""))
        for i, item in enumerate(self.assignments):
            iid = str(i)
            if not item["discarded"]:
                continue
            if hide and iid in visible:
                self.tree.detach(iid)
            elif not hide and iid not in visible:
                self.tree.move(iid, "", i)

    def _start_analysis(self) -> None:
        src = self.src_var.get().strip()
        root = self.root_var.get().strip()
        api_key = self.key_var.get().strip()
        if not src or not Path(src).is_file():
            messagebox.showerror("缺少源文件", "请先选择有效的会话 txt 文件。")
            return
        if not root or not Path(root).is_dir():
            messagebox.showerror("缺少素材根目录", "请先选择有效的素材根目录。")
            return
        if not api_key:
            messagebox.showerror("缺少 API 密钥", "请先填写 LLM API 密钥（OpenAI 兼容服务）。")
            return
        depth = max(1, min(5, int(self.depth_var.get() or 1)))
        recent_days = max(0, int(self.recent_var.get() or 0))
        if self._loaded_sig != (src, root, depth, recent_days) and not self._load_data():
            return  # 首次或源/根目录/深度/天数变了：先加载展示，再开始分析
        cfg = {
            "source": src,
            "root": root,
            "base_url": self.base_var.get().strip(),
            "model": self.model_var.get().strip(),
            "api_key": api_key,
            "concurrency": max(1, min(8, int(self.conc_var.get() or 4))),
            "batch_size": max(5, min(50, int(self.batch_var.get() or 20))),
            "max_depth": depth,
            "recent_days": recent_days,
            "export_dir": self._export_dir,
            "output_name": self.out_var.get().strip(),
            "noise_markers": self.noise_markers,
        }
        save_llm_config(cfg)
        self._cancel_event = threading.Event()
        self._analysis_started = time.monotonic()
        # 运行中按钮变为“停止”，可随时中止剩余批次（已完成的结果仍然保留）
        self.analyze_btn.configure(text="停 止", command=self._cancel_analysis)
        self.load_btn.configure(state="disabled")
        self.confirm_btn.configure(state="disabled")
        self.apply_btn.configure(state="disabled")
        self.dir_combo.configure(state="disabled")
        hits = sum(1 for a in self.assignments if a["result"] is not None and not a["discarded"])
        discarded_cnt = sum(1 for a in self.assignments if a["discarded"])
        todo = len(self.assignments) - hits - discarded_cnt
        self.status_var.set(
            f"已命中缓存 {hits} 条、废弃 {discarded_cnt} 条，本次需 LLM 分析 {todo} 条…"
        )
        # 废弃行不送 LLM：省调用，也保证结果只覆盖需要更新的行
        messages = [item["message"] for item in self.assignments if not item["discarded"]]
        threading.Thread(
            target=self._analyze_worker, args=(cfg, messages, self.candidates), daemon=True
        ).start()

    def _cancel_analysis(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.analyze_btn.configure(state="disabled")
        self.status_var.set("正在停止：等待进行中的 LLM 批次结束后，返回已完成的分类…")

    def _analyze_worker(self, cfg: dict, messages: list, candidates: list) -> None:
        try:
            def call_llm(llm_messages: list[dict]) -> str:
                return core.openai_chat_call(
                    cfg["base_url"], cfg["api_key"], cfg["model"], llm_messages
                )

            results = core.classify_with_llm(
                messages,
                candidates,
                call_llm=call_llm,
                batch_size=cfg["batch_size"],
                concurrency=cfg["concurrency"],
                progress_cb=lambda done, total, failed, chunk: self._queue.put(
                    ("progress", done, total, failed, chunk)
                ),
                cancel_event=self._cancel_event,
                cache=self._cache,
            )
            # 本轮新结果连同人物名单与标注落盘，下次不再重复分析
            self._save_cache_file()
            self._queue.put(("done", results))
        except Exception as exc:
            self._queue.put(("error", str(exc)))

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    self._on_batch_progress(item[1], item[2], item[3], item[4])
                elif kind == "done":
                    self._on_analysis_done(item[1])
                else:
                    self._on_analysis_failed(item[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _tag_for_result(self, res) -> tuple:
        if core.is_noise_content(res.message.content):
            return ("noise",)
        return () if res.candidate else ("unmatched",)

    def _apply_result_to_row(self, idx: int, res) -> None:
        """把一条分类结果写回 assignments 与表格行。"""
        item = self.assignments[idx]
        item["result"] = res
        item["target"] = res.candidate
        values = list(self.tree.item(str(idx), "values"))
        values[4] = core.display_rel(res.candidate) if res.candidate else UNCLASSIFIED
        values[5] = res.reason
        self.tree.item(str(idx), values=values, tags=(self._tag_for_result(res),))

    def _on_batch_progress(self, done: int, total: int, failed: int, chunk) -> None:
        # 实时刷新：本批结果一到就更新对应行
        for res in chunk:
            idx = self._msg_index.get(id(res.message))
            if idx is not None:
                self._apply_result_to_row(idx, res)
        elapsed = max(0, int(time.monotonic() - self._analysis_started))
        extra = f"（{failed} 失败）" if failed else ""
        shown = sum(1 for a in self.assignments if a["result"] is not None or a["discarded"])
        self.status_var.set(
            f"LLM 分类中：已完成 {done}/{total} 批{extra} · "
            f"已出结果 {shown}/{len(self.assignments)} 条 · 已用 {elapsed} 秒…"
        )

    def _on_analysis_failed(self, error: str) -> None:
        self._cancel_event = None
        self.analyze_btn.configure(text="分 析", command=self._start_analysis, state="normal")
        self.load_btn.configure(state="normal")
        # 中途失败：尚未出结果的行标“分析中止”，已出结果的行保持原样
        for i, item in enumerate(self.assignments):
            if (
                item["result"] is None
                and not item["discarded"]
                and not core.is_noise_content(item["message"].content)
            ):
                values = list(self.tree.item(str(i), "values"))
                values[4] = UNCLASSIFIED
                values[5] = "分析中止"
                self.tree.item(str(i), values=values, tags=("unmatched",))
        self.status_var.set("分析失败，请检查路径与 API 配置后重试。")
        messagebox.showerror("分析失败", error)

    def _on_analysis_done(self, results) -> None:
        self._cancel_event = None
        self.analyze_btn.configure(text="分 析", command=self._start_analysis)
        self.load_btn.configure(state="normal")
        # 按消息键把结果映射回行（本次只送非废弃消息给 LLM，与行号不再一一对应）
        res_by_key = {core.message_key(r.message): r for r in results}
        for i, item in enumerate(self.assignments):
            if item["discarded"]:
                continue  # 废弃行保持“数据废弃”显示，不被结果覆盖
            res = res_by_key.get(core.message_key(item["message"]))
            if res is not None:
                self._apply_result_to_row(i, res)
        self.dir_combo.configure(state="readonly")
        self.apply_btn.configure(state="normal")
        self.analyze_btn.configure(state="normal")
        self.confirm_btn.configure(state="normal")
        discarded_cnt = sum(1 for a in self.assignments if a["discarded"])
        self.status_var.set(
            core.summarize(results)
            + (f"  另有废弃 {discarded_cnt} 条（不分析、不写入）。" if discarded_cnt else "")
            + "  双击“推荐目录”单元格改目录；双击其他位置弹出内容详情，窗口内可修改“涉及人物”（改完再分析会把新人物考虑进去）。"
        )
        self._refresh_confirm_label()

    def _on_tree_double_click(self, event) -> None:
        """双击分发：#5“推荐目录”列行内改目录；其余任意位置弹出内容详情（窗口内可改人物）。"""
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if self.tree.identify_column(event.x) == "#5":
            self._begin_dir_edit(row)
        else:
            self._show_detail(row)

    def _on_tree_right_click(self, event) -> None:
        """右击行弹出菜单：废弃选中行；右击的是废弃行时提供“取消废弃”。"""
        row = self.tree.identify_row(event.y)
        if not row:
            return  # 空白/表头处不弹菜单
        if row not in self.tree.selection():
            self.tree.selection_set(row)  # 右击未选中的行时切换选中
        self.ctx_menu.delete(0, "end")
        if self.assignments[int(row)]["discarded"]:
            self.ctx_menu.add_command(label="取消废弃", command=lambda: self._discard_selected(False))
        else:
            self.ctx_menu.add_command(label="废弃", command=lambda: self._discard_selected(True))
        try:
            self.ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.ctx_menu.grab_release()

    def _discard_selected(self, discarded: bool) -> None:
        """把当前选中的行批量标记/取消废弃（右键菜单触发，立即持久化）。"""
        if self._cancel_event is not None:
            self.status_var.set("分析进行中，请等本轮结束后再修改废弃标记。")
            return
        for iid in self.tree.selection():
            self._set_row_discard(int(iid), iid, discarded)
        self._refresh_confirm_label()

    def _people_counts(self) -> dict[str, int]:
        """统计每个人物在历史标注（缓存中全部）里被引用的次数。"""
        counts: dict[str, int] = {}
        for people in self._msg_people.values():
            for name in people:
                if name != core.SELF_PERSON:
                    counts[name] = counts.get(name, 0) + 1
        return counts

    def _people_choices(self, extra: list[str] | None = None) -> list[str]:
        """人物候选名单：用户新增人物 + 所有已标注过的人物，固定包含“我”。

        不再从目录列表的文件夹名取人物，只认用户输入/选择过的名字。
        排序：按历史标注中被引用的次数降序（次数多者靠前），同次数按名字，
        “我”固定排最前。
        """
        counts = self._people_counts()
        names = set(self._custom_people)
        names.update(counts)
        if extra:
            names.update(extra)
        names.discard(core.SELF_PERSON)
        ranked = sorted(names, key=lambda n: (-counts.get(n, 0), n))
        return [core.SELF_PERSON] + ranked

    def _build_people_selector(self, parent, msg) -> tuple:
        """在 parent 内创建人物选择控件（全量点选 + 输入联想），返回 (selected_names, refill)。

        所有可选人物平铺为按钮，点击切换选中（“✓”前缀表示已选）；
        “我”始终排在第一位，允许只选“我”一个（保存时非空标注自动包含“我”）。
        输入框打字即时联想，回车或双击加入；输入全新名字回车即新增人物。
        selected_names() 返回当前已选人物集合；refill(select) 以新集合重建面板。
        """
        selected: set[str] = set(msg.people)

        palette_frame = ttk.Frame(parent)
        palette_frame.pack(fill="x")
        ttk.Label(
            parent, text="点击人物名切换选中（✓ 为已选）；输入框联想回车加入；全新名字回车即新增"
        ).pack(anchor="w", pady=(4, 1))
        entry = ttk.Entry(parent)
        entry.pack(fill="x")
        sugg = tk.Listbox(parent, height=4, exportselection=False)

        def hide_sugg() -> None:
            sugg.pack_forget()

        def update_sugg(*_args) -> None:
            text = entry.get().strip().lower()
            sugg.delete(0, "end")
            if text:
                for name in self._people_choices():
                    if name not in selected and text in name.lower():
                        sugg.insert("end", name)
            if sugg.size():
                sugg.pack(fill="x", pady=(2, 0))
                sugg.selection_clear(0, "end")
                sugg.activate(0)
            else:
                hide_sugg()

        def rebuild_palette() -> None:
            for child in palette_frame.winfo_children():
                child.destroy()
            counts = self._people_counts()
            names = [core.SELF_PERSON] + sorted(
                (set(self._people_choices()) | selected) - {core.SELF_PERSON},
                key=lambda n: (-counts.get(n, 0), n),  # 引用次数多者靠前
            )
            for i, name in enumerate(names):
                row, col = divmod(i, 5)  # 每行最多 5 个，超出自动换行
                mark = "✓ " if name in selected else ""
                ttk.Button(
                    palette_frame, text=f"{mark}{name}",
                    command=lambda n=name: toggle(n),
                ).grid(row=row, column=col, sticky="w", padx=(0, 10), pady=2)

        def toggle(name: str) -> None:
            if name in selected:
                selected.discard(name)
            else:
                selected.add(name)
            rebuild_palette()

        def add_name(name: str) -> None:
            name = name.strip()
            if not name:
                return
            if name not in self._custom_people and name != core.SELF_PERSON:
                self._custom_people.append(name)  # 列表外人物持久保存，下次仍在
                self._save_cache_file()
            selected.add(name)
            entry.delete(0, "end")
            hide_sugg()
            rebuild_palette()
            entry.focus_set()

        def on_return(_event) -> str:
            if sugg.winfo_ismapped() and sugg.size():
                add_name(sugg.get("active"))  # 有联想：加入当前高亮项
            else:
                add_name(entry.get())  # 无联想：把输入当作新人物新增
            return "break"

        entry.bind("<KeyRelease>", update_sugg)
        entry.bind("<Return>", on_return)
        sugg.bind("<Double-1>", lambda _e: add_name(sugg.get("active")))
        sugg.bind("<Return>", lambda _e: add_name(sugg.get("active")))

        def selected_names() -> set[str]:
            return set(selected)

        def refill(select: set[str]) -> None:
            selected.clear()
            selected.update(core.ensure_self(sorted(select)))
            rebuild_palette()

        rebuild_palette()
        entry.focus_set()
        return selected_names, refill

    def _commit_people(self, row: str, people: list[str]) -> None:
        """把人物标注应用到消息、持久化缓存与表格行（由详情窗口“保存并关闭”调用）。"""
        item = self.assignments[int(row)]
        msg = item["message"]
        msg.people = core.ensure_self(people)
        self._msg_people[core.message_key(msg)] = list(msg.people)
        self._save_cache_file()
        values = list(self.tree.item(row, "values"))
        values[3] = "、".join(msg.people)
        self.tree.item(row, values=values)
        self.status_var.set(
            f"第 {int(row) + 1} 条消息已标注人物：{'、'.join(msg.people) or '无'}"
            "（下次“分 析”会把这些人物考虑进去）"
        )

    def _begin_dir_edit(self, row: str) -> None:
        if self._cancel_event is not None:
            self.status_var.set("分析进行中，请等本轮结束后再修改目录。")
            return
        if not self.cand_by_display:
            messagebox.showinfo("无候选目录", "尚未扫描到候选目录，请先点击“加 载”。")
            return
        self._end_dir_edit()  # 同一时间只保留一个编辑器
        bbox = self.tree.bbox(row, column="#5")
        if not bbox:
            return
        x, y, w, h = bbox
        item = self.assignments[int(row)]
        current = (
            DISCARDED if item["discarded"]
            else (core.display_rel(item["target"]) if item["target"] else UNCLASSIFIED)
        )
        editor = ttk.Combobox(
            self.tree,
            state="readonly",
            values=[UNCLASSIFIED, DISCARDED] + list(self.cand_by_display),
        )
        editor.set(current)
        editor.place(x=x, y=y, width=max(w, 200), height=h)
        editor.focus_set()
        editor.bind("<<ComboboxSelected>>", lambda _e: self._commit_dir_edit(row))
        editor.bind("<Return>", lambda _e: self._commit_dir_edit(row))
        editor.bind("<Escape>", lambda _e: self._end_dir_edit())
        editor.bind("<FocusOut>", lambda _e: self._end_dir_edit())
        self._dir_editor = editor

    def _commit_dir_edit(self, row: str) -> None:
        if self._dir_editor is None:
            return
        choice = self._dir_editor.get()
        self._end_dir_edit()
        if choice == DISCARDED:
            self._set_row_discard(int(row), row, True)
            self._refresh_confirm_label()
            return  # 标为废弃：不分析、不写入
        target = None if choice == UNCLASSIFIED else self.cand_by_display.get(choice)
        if choice != UNCLASSIFIED and target is None:
            return  # 只能从候选列表中选
        item = self.assignments[int(row)]
        if item["discarded"]:
            self._set_row_discard(int(row), row, False)  # 改回普通目录 = 取消废弃
        item["target"] = target
        values = list(self.tree.item(row, "values"))
        values[4] = core.display_rel(target) if target else UNCLASSIFIED
        values[5] = "手动指定" if target else ""
        self.tree.item(row, values=values)
        if target:
            self.tree.item(row, tags=())
        self._refresh_confirm_label()

    def _end_dir_edit(self) -> None:
        editor, self._dir_editor = self._dir_editor, None
        if editor is not None:
            editor.destroy()

    def _apply_target(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("未选择行", "请先在表格中选择要修改目标目录的消息行。")
            return
        choice = self.dir_var.get()
        if choice == DISCARDED:  # 批量标废弃
            for iid in selected:
                self._set_row_discard(int(iid), iid, True)
            self._refresh_confirm_label()
            return
        target = None if choice == UNCLASSIFIED else self.cand_by_display.get(choice)
        if choice != UNCLASSIFIED and target is None:
            messagebox.showerror("无效目录", "请从下拉框中选择有效的目录。")
            return
        for iid in selected:
            item = self.assignments[int(iid)]
            if item["discarded"]:
                self._set_row_discard(int(iid), iid, False)  # 改回普通目录 = 取消废弃
            item["target"] = target
            values = list(self.tree.item(iid, "values"))
            values[4] = core.display_rel(target) if target else UNCLASSIFIED
            values[5] = "手动指定" if target else ""
            self.tree.item(iid, values=values)
            if target:
                self.tree.item(iid, tags=())
        self._refresh_confirm_label()

    def _refresh_confirm_label(self) -> None:
        count = sum(1 for item in self.assignments if item["target"])
        self.confirm_btn.configure(text=f"确认写入（{count} 条）")

    def _choose_export_dir(self) -> None:
        """手动更换人物消息导出目录（会被记住，导出时不再重复询问）。"""
        directory = filedialog.askdirectory(
            title="选择人物消息的保存目录", initialdir=self._export_dir or None
        )
        if directory:
            self._save_export_dir(directory)
            self.status_var.set(f"导出目录已切换为 {directory}。")

    def _save_export_dir(self, directory: str) -> None:
        """记住导出目录：合并写回 llm_config.json，不影响其他配置。"""
        self._export_dir = directory
        cfg = load_llm_config()
        cfg["export_dir"] = directory
        save_llm_config(cfg)

    def _export_people_messages(self) -> None:
        """把所有标注了“涉及人物”的消息（废弃行除外）合并导出到按日期命名的 txt。

        上次已导出过的消息、以及导出目录已有 txt 中已存在的消息都自动跳过，
        提示不用再保存；文件名含导出日期，同一天重复导出时向同一文件追加；
        保存目录记住上次的选择。
        """
        annotated = [
            item for item in self.assignments
            if not item["discarded"]
            and self._msg_people.get(core.message_key(item["message"]))
        ]
        if not annotated:
            messagebox.showinfo("无可导出数据", "当前没有标注“涉及人物”的消息行。")
            return
        rows = [
            item for item in annotated
            if core.message_key(item["message"]) not in self._msg_exported
        ]
        skipped_cache = len(annotated) - len(rows)
        if not rows:
            self.export_status_var.set(
                f"所有 {len(annotated)} 条有人物标注的消息上次已导出过，不用再保存了。"
            )
            return
        # 用上次导出目录直接保存（目录不存在时才重新选），选择结果会记住
        directory = self._export_dir
        if not directory or not Path(directory).is_dir():
            directory = filedialog.askdirectory(title="选择人物消息的保存目录")
            if not directory:
                return  # 用户取消
            self._save_export_dir(directory)
        # 导出目录里之前保存的 txt 中已存在的消息，这次不再重复保存
        existing_lines = core.scan_existing_message_lines(directory)
        skipped_in_files = sum(
            1 for item in rows if item["message"].line in existing_lines
        )
        rows = [item for item in rows if item["message"].line not in existing_lines]
        if not rows:
            detail = []
            if skipped_cache:
                detail.append(f"{skipped_cache} 条上次已导出过")
            if skipped_in_files:
                detail.append(f"{skipped_in_files} 条在导出目录已有的 txt 中")
            self.export_status_var.set(
                f"所有 {len(annotated)} 条有人物标注的消息均已存在"
                f"（{'，'.join(detail)}），不用再保存了。"
            )
            return
        today = date.today().isoformat()
        path = Path(directory) / f"涉及人员消息_{today}.txt"
        blocks = []
        for item in rows:
            msg = item["message"]
            key = core.message_key(msg)
            people = "、".join(self._msg_people[key])
            blocks.append(f"==== 涉及人员：{people} ====\n{msg.line}")
            self._msg_exported[key] = today  # 标记已导出，下次不再重复保存
        try:
            exists = path.exists()
            with open(path, "a", encoding="utf-8") as f:
                if exists:
                    f.write("\n")  # 与之前的导出内容隔一个空行
                f.write("\n\n".join(blocks) + "\n")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        self._save_cache_file()
        skipped_discard = sum(
            1 for item in self.assignments
            if item["discarded"] and self._msg_people.get(core.message_key(item["message"]))
        )
        self.export_status_var.set(
            f"已导出 {len(rows)} 条人物消息到 {path}"
            + (f"（{skipped_cache} 条上次已导出过，未重复保存）" if skipped_cache else "")
            + (f"（{skipped_in_files} 条在导出目录已有的 txt 中已存在，未重复保存）" if skipped_in_files else "")
            + (f"（{skipped_discard} 条废弃行未导出）" if skipped_discard else "")
        )

    def _confirm_write(self) -> None:
        discarded_cnt = sum(1 for item in self.assignments if item["discarded"])
        pairs = [(item["target"], item["message"]) for item in self.assignments if item["target"]]
        unclassified = len(self.assignments) - len(pairs) - discarded_cnt
        if not pairs:
            messagebox.showinfo("没有可写入的消息", "当前没有消息被指定到任何目录（未分类的消息不会写入）。")
            return
        out_name = core.sanitize_output_name(self.out_var.get())
        if not messagebox.askyesno(
            "确认写入",
            f"即将把 {len(pairs)} 条消息追加写入到对应目录下的“{out_name}”（{unclassified} 条未分类、{discarded_cnt} 条废弃将被跳过）。\n确定继续吗？",
        ):
            return
        try:
            reports = core.apply_writes(
                pairs, output_filename=self.out_var.get(), source_label=Path(self.src_var.get()).name
            )
        except Exception as exc:
            messagebox.showerror("写入失败", str(exc))
            return
        lines = []
        for rep in reports:
            state = (
                f"写入 {rep.written} 条，跳过已存在 {rep.skipped_existing} 条"
                if not rep.error
                else f"失败：{rep.error}"
            )
            lines.append(f"{state}\n  {rep.file_path}")
        total_written = sum(r.written for r in reports)
        lines.append(f"完成：共写入 {total_written} 条；未分类跳过 {unclassified} 条。")
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", "\n".join(lines))
        self.result_text.configure(state="disabled")
        self.status_var.set(f"写入完成：{total_written} 条新消息，重复行已自动跳过。")
        # 已写入的消息记入缓存：下次分析直接显示确认过的目录，不再调 LLM
        for item in self.assignments:
            if item["target"] is None:
                continue
            key = core.message_key(item["message"])
            old = self._cache.get(key)
            old_reason = str(old.get("reason") or "") if isinstance(old, dict) else ""
            self._cache[key] = {
                "dir": core.display_rel(item["target"]),
                "reason": old_reason,
                "confirmed": True,
                "people": list(item["message"].people),
            }
        self._save_cache_file()

    def _show_detail(self, row: str) -> None:
        """双击行：弹出该行内容文本（可选中复制），并可在窗口内修改“涉及人物”。"""
        if not self.assignments:
            messagebox.showinfo("未加载数据", "请先点击“加 载”展示消息。")
            return
        item = self.assignments[int(row)]
        msg = item["message"]
        reason = item["result"].reason if item["result"] else ""
        win = tk.Toplevel(self)
        win.title(f"消息详情 - {msg.time_str}")
        win.geometry("860x640")
        dir_text = (
            f"{DISCARDED}（已废弃：不分析、不写入）"
            if item["discarded"]
            else (core.display_rel(item["target"]) if item["target"] else UNCLASSIFIED)
        )
        info = (
            f"时间: {msg.time_str}    发送者: {msg.sender_display} ({msg.sender_username})\n"
            f"当前目标目录: {dir_text}\n"
            f"LLM 理由: {reason}"
        )
        ttk.Label(win, text=info, justify="left", padding=(12, 10, 12, 4)).pack(anchor="w")

        # 涉及人物：全量平铺点选 + 输入联想，保存后写回消息、缓存与表格行
        people_frame = ttk.LabelFrame(win, text="涉及人物（点击切换选中，✓ 为已选）", padding=(8, 6))
        people_frame.pack(fill="x", padx=12)
        selected_names, _refill = self._build_people_selector(people_frame, msg)

        btn_row = ttk.Frame(people_frame)
        btn_row.pack(pady=(6, 0))

        def save() -> None:
            if self._cancel_event is not None:
                self.status_var.set("分析进行中，请等本轮结束后再修改人物。")
                return
            self._commit_people(row, sorted(selected_names()))
            win.destroy()

        save_btn = ttk.Button(btn_row, text="保存并关闭", command=save)
        save_btn.pack(side="left", padx=4)
        ttk.Button(btn_row, text="取 消", command=win.destroy).pack(side="left", padx=4)
        if self._cancel_event is not None:
            save_btn.configure(state="disabled")  # 分析中禁止修改人物

        text = scrolledtext.ScrolledText(win, wrap="word")
        text.pack(fill="both", expand=True, padx=12, pady=(6, 10))
        text.insert("1.0", msg.content.replace("\\n", "\n"))

        def copy_selection() -> None:
            sel = text.tag_ranges("sel")
            if sel:
                self.clipboard_clear()
                self.clipboard_append(text.get(sel[0], sel[-1]))
                self.status_var.set("选中内容已复制到剪贴板。")

        def copy_all() -> None:
            self.clipboard_clear()
            self.clipboard_append(text.get("1.0", "end-1c"))
            self.status_var.set("消息内容已复制到剪贴板。")

        def _readonly_keys(event) -> str | None:
            # 只读但可复制：Ctrl+A 放行给默认全选，Ctrl+C 复制选中内容，
            # 其余按键（输入、粘贴、剪切、撤销）一律拦截，内容不可被修改
            if event.state & 0x4 and event.keysym.lower() == "a":
                return None
            if event.state & 0x4 and event.keysym.lower() == "c":
                copy_selection()
                return "break"
            return "break"

        # Windows 下 Ctrl+V 等还会以 <<Paste>> 虚拟事件派发（<Key> 拦不到），
        # 编辑类虚拟事件单独绑定拦截；复制走 Ctrl+C 处理与右键菜单
        for virt in ("<<Paste>>", "<<Cut>>", "<<Undo>>", "<<Redo>>"):
            text.bind(virt, lambda _e: "break")
        text.bind("<Key>", _readonly_keys)
        text.focus_set()

        # 右键菜单：全选 / 复制选中 / 复制全部
        menu = tk.Menu(win, tearoff=0)
        menu.add_command(label="全选", command=lambda: text.tag_add("sel", "1.0", "end"))
        menu.add_command(label="复制选中", command=copy_selection)
        menu.add_command(label="复制全部内容", command=copy_all)
        text.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        ttk.Button(btn_row, text="复制全部内容", command=copy_all).pack(
            side="left", padx=4, before=save_btn
        )


if __name__ == "__main__":
    ChatSorterApp().mainloop()
