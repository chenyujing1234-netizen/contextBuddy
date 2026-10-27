"""文件传输助手聊天记录按人分类——核心逻辑。

职责：
1. 解析微信导出的 chat_txt 会话文件（如“文件传输助手.txt”）中的消息行。
2. 扫描素材根目录（含根目录自身与递归子目录，跳过隐藏/技术目录），得到候选目录。
3. 调用 LLM（OpenAI 兼容接口）判断每条消息归属哪个目录（只做推荐，不写盘）；
   系统/附件消息（表情、文件传输记录等）自动跳过，不发给 LLM；
   用户标注的“涉及人物”会附在消息末尾一并发给 LLM 参考。
4. 用户在界面确认后，按目录分组、按时间排序、整行去重后追加写入目标 txt；
   有人物标注的消息写入时在行尾附加“（涉及人物：A、B）”。

消息行格式（由 ContextBuddy 结构化导出生成）：
    [2026-07-22 20:16:11] 余井(wxid_jzemtwvdyvml21): 消息内容
消息内容中的真实换行在导出时已展平为字面量 ``\\n``，因此一条消息只占一行。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

# 消息行：[时间] 显示名(用户名): 内容
# 用户名限定为微信 ID 字符集，避免把内容里的“(xx): ”误当作发送者。
MESSAGE_LINE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(.+?)\(([A-Za-z0-9_\-@.]{2,64})\):\s?(.*)$"
)

# 技术性目录名（导出数据自身/公众号/群聊 ID），整棵子树跳过
_ID_NAME_RE = re.compile(r"^(wxid_|gh_)[A-Za-z0-9_-]+$|@chatroom$", re.IGNORECASE)

# 目录名命中即整棵子树不作为候选
_EXCLUDED_DIR_NAMES = {
    ".workbuddy", "structured_data", "chat_txt", "chat",
    "__pycache__", "node_modules", ".git", ".venv",
}

# 消息内容含这些标记时视为系统/附件消息，不发送给 LLM（仍可手动指定目录）
# “复制打开抖音”是抖音分享文案的固定字眼，这类推广消息自动废弃
DEFAULT_NOISE_MARKERS = (
    "<?xml", "@cdn_", "eyj", "<sysmsg", "<emoji", "<msgsource", "复制打开抖音",
)
# 当前生效的废弃关键词（界面里可编辑，通过 set_noise_markers 更新）
_noise_markers: tuple[str, ...] = DEFAULT_NOISE_MARKERS


def set_noise_markers(markers) -> None:
    """设置“内容包含即废弃”的关键词列表，完全替代默认列表（空白项自动忽略）。"""
    global _noise_markers
    _noise_markers = tuple(
        m.strip() for m in markers if isinstance(m, str) and m.strip()
    )


def is_noise_content(content: str) -> bool:
    """系统/附件消息（表情、文件传输记录、链接 XML、命中废弃关键词的文案）不发给 LLM。"""
    if not content.strip():
        return True
    low = content.lower()
    return any(marker.lower() in low for marker in _noise_markers)

# 单条消息送入 LLM 的最大字符数
_LLM_CONTENT_LIMIT = 600
# 候选目录送入 LLM 的最大条数
_LLM_DIR_LIMIT = 1000
# 写入行末尾的人物标注后缀（读取目标文件时用于还原原始行做去重）
_PEOPLE_SUFFIX_RE = re.compile(r"（涉及人物：[^）]*）$")

LLM_SYSTEM_PROMPT = """你是微信聊天记录归档助手。用户给你一个候选目录列表（相对路径）和一批编号的聊天消息。
任务：为每条消息判断它最适合归档到哪个人的目录/哪个主题目录。
规则：
- 目录名多为人物（如家人、孩子、同事）或专辑主题；消息内容与某个人相关，就选对应人的目录。
- 消息末尾可能带“（涉及人物：A、B）”，这是用户标注的该消息相关人物，判断目录时优先参考。
- 只能从候选目录列表中选择，dir 字段必须与列表中的路径完全一致；没有合适目录时 dir 为空字符串。
- reason 用不超过 20 个字说明判断理由。
输出严格 JSON（不要 Markdown 代码块、不要多余文字）：
{"results":[{"index":1,"dir":"喜乐","reason":"提到孩子喜乐"}]}"""


@dataclass
class ChatMessage:
    time_str: str
    sender_display: str
    sender_username: str
    content: str
    line: str  # 原始消息行，写入目标文件时原样保留
    people: list[str] = field(default_factory=list)  # 用户标注的涉及人物（参与 LLM 判断与写入）


@dataclass
class CandidateDir:
    path: Path
    rel_display: str  # 相对根目录的显示路径；根目录自身为 "."
    depth: int


@dataclass
class ClassifyResult:
    message: ChatMessage
    candidate: CandidateDir | None = None
    reason: str = ""  # LLM 给出的判断理由；未分类时为原因说明


@dataclass
class WriteReport:
    dir_path: Path
    file_path: Path
    written: int
    skipped_existing: int
    error: str = ""


def parse_chat_txt(file_path: str | os.PathLike) -> list[ChatMessage]:
    """解析会话 txt，返回全部消息（头部元信息行自动忽略）。"""
    file_path = Path(file_path)
    text = file_path.read_text(encoding="utf-8", errors="replace")
    messages: list[ChatMessage] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        match = MESSAGE_LINE_RE.match(line)
        if not match:
            continue
        messages.append(
            ChatMessage(
                time_str=match.group(1),
                sender_display=match.group(2),
                sender_username=match.group(3),
                content=match.group(4),
                line=line,
            )
        )
    return messages


# 涉及人员中固定包含的“我”（微信本人，文件传输助手消息都涉及自己）
SELF_PERSON = "我"


def ensure_self(people: list[str]) -> list[str]:
    """涉及人员固定包含“我”（放最前，去重保序）；空标注保持为空（视为未标注）。"""
    names = normalize_people(people)
    if not names:
        return []
    return [SELF_PERSON] + [p for p in names if p != SELF_PERSON]


# 汉字（CJK 统一表意文字基本区，不含中文标点）
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")


def chinese_char_count(text: str) -> int:
    """统计文本中的汉字个数（标点、字母、数字均不计）。"""
    return len(_CHINESE_RE.findall(text or ""))


def scan_candidate_dirs(
    root: str | os.PathLike, max_depth: int | None = None
) -> list[CandidateDir]:
    """扫描素材根目录：根目录自身 + 递归子目录（跳过隐藏/技术/ID 目录）。

    max_depth 限制目录层级（根目录自身为 0 层）：候选目录过多会让每批
    LLM 提示词变得非常庞大、又慢又贵，默认建议只取 1 层（顶层目录）。
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"素材根目录不存在或不是目录：{root}")

    def make(path: Path) -> CandidateDir:
        rel = path.relative_to(root)
        return CandidateDir(
            path=path,
            rel_display="." if not rel.parts else rel.as_posix(),
            depth=len(rel.parts),
        )

    out = [make(root)]
    for dirpath, dirnames, _filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        if max_depth is not None and depth >= max_depth:
            dirnames[:] = []  # 已到深度上限，不再深入
            continue
        kept = []
        for name in sorted(dirnames):
            if name.startswith(".") or name in _EXCLUDED_DIR_NAMES:
                continue
            if _ID_NAME_RE.match(name):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in kept:
            out.append(make(Path(dirpath) / name))
    out.sort(key=lambda c: (c.depth, c.rel_display))
    return out


# ---------- LLM 分类 ----------


def display_rel(cand: CandidateDir) -> str:
    """目录在界面/提示词中的显示名：根目录自身显示为“(根目录)”。"""
    return "(根目录)" if cand.rel_display == "." else cand.rel_display


def build_llm_messages(candidates: list[CandidateDir], messages: list[ChatMessage]) -> list[dict]:
    """构造单批分类请求的消息体：候选目录列表 + 编号消息。"""
    lines = ["候选目录列表："]
    for cand in candidates[:_LLM_DIR_LIMIT]:
        lines.append(f"- {display_rel(cand)}")
    if len(candidates) > _LLM_DIR_LIMIT:
        lines.append(f"（目录过多，仅列出前 {_LLM_DIR_LIMIT} 个）")
    lines.append("")
    lines.append("消息：")
    for idx, msg in enumerate(messages, start=1):
        content = msg.content
        if len(content) > _LLM_CONTENT_LIMIT:
            content = content[:_LLM_CONTENT_LIMIT] + "…"
        line = f"{idx}. [{msg.time_str}] {content}"
        if msg.people:
            line += f"（涉及人物：{'、'.join(msg.people)}）"
        lines.append(line)
    return [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def parse_llm_results(text: str) -> dict[int, tuple[str, str]]:
    """解析 LLM 返回文本中的 JSON：{编号: (目录显示名, 理由)}。

    兼容模型输出的 Markdown 代码块等包装（取首个“{”到末个“}”之间的内容）。
    """
    text = str(text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM 返回中没有 JSON 对象")
    data = json.loads(text[start : end + 1])
    out: dict[int, tuple[str, str]] = {}
    for item in data.get("results", []):
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        out[idx] = (str(item.get("dir", "")).strip(), str(item.get("reason", "")).strip())
    return out


def openai_chat_call(
    base_url: str, api_key: str, model: str, messages: list[dict], *, timeout: int = 120
) -> str:
    """调用 OpenAI 兼容的 /chat/completions，返回助手文本（仅用标准库）。"""
    url = str(base_url or "").rstrip("/") + "/chat/completions"
    payload = json.dumps(
        {"model": model, "messages": messages, "temperature": 0.1, "max_tokens": 4000},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


# ---------- 分类缓存（跨会话复用，同一消息不重复分析） ----------

CACHE_VERSION = 2


def message_key(msg: ChatMessage) -> str:
    """消息的稳定键：时间+发送者+内容 的哈希（与行号无关，导出新增消息也不受影响）。"""
    raw = f"{msg.time_str}|{msg.sender_username}|{msg.content}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def normalize_people(people) -> list[str]:
    """人物列表规整：去空白、去重、保序。"""
    out: list[str] = []
    for p in people or []:
        name = str(p).strip()
        if name and name not in out:
            out.append(name)
    return out


def load_cache_data(path: str | os.PathLike) -> dict:
    """读取完整缓存 {"people","msg_people","msg_discarded","msg_exported","entries"}；缺失或损坏返回空结构。

    - people：用户在标注对话框里新增的列表外人物（跨会话保留）；
    - msg_people：message_key -> 人物标注，重启后恢复每条消息的标注；
    - msg_discarded：message_key -> 废弃标记（True=手动废弃，False=保留系统/附件行）；
    - msg_exported：message_key -> 已导出日期（人物消息导出去重，避免重复保存）；
    - entries：分类结果缓存（含 people，供判断标注变化后是否需要重新分析）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    entries = data.get("entries")
    raw_people = data.get("people")
    msg_people = data.get("msg_people")
    msg_discarded = data.get("msg_discarded")
    msg_exported = data.get("msg_exported")
    return {
        "people": normalize_people(raw_people) if isinstance(raw_people, list) else [],
        "msg_people": {
            str(k): normalize_people(v) for k, v in msg_people.items()
        } if isinstance(msg_people, dict) else {},
        "msg_discarded": {
            str(k): bool(v) for k, v in msg_discarded.items()
        } if isinstance(msg_discarded, dict) else {},
        "msg_exported": {
            str(k): str(v) for k, v in msg_exported.items()
        } if isinstance(msg_exported, dict) else {},
        "entries": entries if isinstance(entries, dict) else {},
    }


def load_cache(path: str | os.PathLike) -> dict:
    """读取缓存 entries（兼容旧调用方与 v1 旧文件）；损坏返回空 dict。"""
    return load_cache_data(path)["entries"]


def save_cache(
    path: str | os.PathLike,
    cache: dict,
    *,
    people: list | None = None,
    msg_people: dict | None = None,
    msg_discarded: dict | None = None,
    msg_exported: dict | None = None,
) -> None:
    """原子写入完整缓存（先写临时文件再整体替换）。"""
    tmp = f"{os.fspath(path)}.tmp"
    data = {
        "version": CACHE_VERSION,
        "people": normalize_people(people),
        "msg_people": {str(k): normalize_people(v) for k, v in (msg_people or {}).items()},
        "msg_discarded": {str(k): bool(v) for k, v in (msg_discarded or {}).items()},
        "msg_exported": {str(k): str(v) for k, v in (msg_exported or {}).items()},
        "entries": cache,
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def cache_lookup(cache: dict, msg: ChatMessage, cand_index: dict) -> ClassifyResult | None:
    """命中缓存则返回对应分类结果（已确认的用确认目录），失效或未命中返回 None。

    未确认的条目若人物标注与分析时不一致，视为失效重新分析（标注变了，
    需要让 LLM 把新人物考虑进来）；已确认条目不受标注变化影响。
    """
    entry = cache.get(message_key(msg))
    if not isinstance(entry, dict):
        return None
    dir_name = str(entry.get("dir") or "")
    cand = cand_index.get(dir_name) if dir_name else None
    if dir_name and cand is None:
        return None  # 上次推荐的目录已不存在，缓存失效需重新分析
    if not entry.get("confirmed") and normalize_people(entry.get("people")) != normalize_people(msg.people):
        return None  # 人物标注变了，缓存失效需重新分析
    reason = str(entry.get("reason") or "")
    if entry.get("confirmed"):
        reason = "上次已确认写入" + (f"：{reason}" if reason else "")
    else:
        reason = f"上次分析：{reason}" if reason else "上次分析：未分类"
    return ClassifyResult(message=msg, candidate=cand, reason=reason)


def classify_with_llm(
    messages: list[ChatMessage],
    candidates: list[CandidateDir],
    *,
    call_llm,
    batch_size: int = 20,
    concurrency: int = 4,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
    cache: dict | None = None,
) -> list[ClassifyResult]:
    """用 LLM 批量判断每条消息的归属目录。

    - 系统/附件消息不发送 LLM，直接标“系统/附件”；
    - cache 非空时，命中缓存的消息直接沿用上次结果（用户确认过的用确认目录），不再调 LLM；
    - 其余消息按 batch_size 分批，concurrency 个线程并发调用 call_llm；
    - 每批失败自动重试 2 次，仍失败则该批消息标“LLM调用失败”（不中断整体）；
    - cancel_event 置位后，未开始的批不再调用 LLM，相关消息标“已取消”；
    - 本轮新的有效 LLM 结果会写回 cache（失败/取消/无效目录不写），由调用方负责落盘；
    - progress_cb(done, total, failed, chunk) 在每批完成后回调（在 worker 线程中执行），
      failed 为累计失败批数（重试耗尽），chunk 为本批的 ClassifyResult 列表，
      可用于把已完成的结果实时显示到界面。
    """
    cand_index = {display_rel(c): c for c in candidates}
    results: list[ClassifyResult | None] = [None] * len(messages)
    todo_idx: list[int] = []
    for i, msg in enumerate(messages):
        if is_noise_content(msg.content):
            results[i] = ClassifyResult(message=msg, reason="系统/附件消息，自动跳过")
            continue
        if cache is not None:
            hit = cache_lookup(cache, msg, cand_index)
            if hit is not None:
                results[i] = hit
                continue
        todo_idx.append(i)

    batches = [todo_idx[k : k + batch_size] for k in range(0, len(todo_idx), batch_size)]
    total = len(batches)
    done = 0
    failed_batches = 0
    lock = threading.Lock()

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def run_batch(indices: list[int]) -> None:
        nonlocal done, failed_batches
        is_cancelled = cancelled()
        parsed = None
        last_error: Exception | None = None
        if not is_cancelled:
            batch_messages = [messages[i] for i in indices]
            for attempt in range(3):
                if cancelled():  # 重试等待期间被取消
                    is_cancelled = True
                    break
                try:
                    parsed = parse_llm_results(call_llm(build_llm_messages(candidates, batch_messages)))
                    break
                except Exception as exc:  # 网络/解析失败统一重试
                    last_error = exc
                    time.sleep(1.5 * (attempt + 1))
        out: list[ClassifyResult] = []
        cache_worthy: list[bool] = []  # 只有有效结果才进缓存（失败/取消/无效目录不进）
        for n, gi in enumerate(indices, start=1):
            if is_cancelled:
                out.append(ClassifyResult(message=messages[gi], reason="已取消"))
                cache_worthy.append(False)
                continue
            if parsed is None:
                out.append(
                    ClassifyResult(message=messages[gi], reason=f"LLM调用失败：{last_error}")
                )
                cache_worthy.append(False)
                continue
            dir_name, reason = parsed.get(n, ("", ""))
            cand = cand_index.get(dir_name) if dir_name else None
            invalid_dir = False
            if dir_name and cand is None:
                reason = f"LLM返回了无效目录“{dir_name}”，未采纳"
                invalid_dir = True
            elif not cand and not reason:
                reason = "LLM判断为未分类"
            out.append(ClassifyResult(message=messages[gi], candidate=cand, reason=reason))
            cache_worthy.append(not invalid_dir)
        with lock:
            for gi, res in zip(indices, out):
                results[gi] = res
            if cache is not None:
                for res, ok in zip(out, cache_worthy):
                    if ok:
                        cache[message_key(res.message)] = {
                            "dir": display_rel(res.candidate) if res.candidate else "",
                            "reason": res.reason,
                            "confirmed": False,
                            "people": list(res.message.people),
                        }
            done += 1
            if parsed is None and not is_cancelled:
                failed_batches += 1
            if progress_cb:
                try:
                    progress_cb(done, total, failed_batches, list(out))
                except Exception:
                    pass

    if batches:
        workers = max(1, min(int(concurrency or 1), total))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run_batch, batches))
    return [r if r is not None else ClassifyResult(message=messages[i], reason="未处理") for i, r in enumerate(results)]


# ---------- 写入 ----------


def sanitize_output_name(name: object) -> str:
    """写入文件名清洗：默认“微信聊天记录.txt”，去除非法字符。"""
    text = str(name or "").strip()
    if not text:
        text = "微信聊天记录.txt"
    if not text.lower().endswith(".txt"):
        text += ".txt"
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")
    return cleaned or "微信聊天记录.txt"


def people_suffix(msg: ChatMessage) -> str:
    """消息写入目标文件时的人物标注后缀（未标注为空字符串）。"""
    return f"（涉及人物：{'、'.join(msg.people)}）" if msg.people else ""


def apply_writes(
    assignments: list[tuple[CandidateDir, ChatMessage]],
    *,
    output_filename: str,
    source_label: str,
) -> list[WriteReport]:
    """把已确认的消息按目录分组写入目标 txt。

    - 同一目录的消息按时间排序后追加，行尾附人物标注“（涉及人物：A、B）”；
    - 目标文件中已存在的整行自动跳过（带人物标注的旧行，其原始行同样视为已存在，
      重复运行不会重复写入）；
    - 新建文件时先写入一行来源说明。
    """
    groups: dict[Path, list[ChatMessage]] = {}
    for cand, msg in assignments:
        groups.setdefault(cand.path, []).append(msg)

    out_name = sanitize_output_name(output_filename)
    reports: list[WriteReport] = []
    for dir_path, msgs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        file_path = dir_path / out_name
        written = 0
        skipped = 0
        error = ""
        try:
            existing: set[str] = set()
            file_exists = file_path.exists()
            if file_exists:
                with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                    for ln in handle:
                        line = ln.rstrip("\r\n")
                        existing.add(line)
                        stem = _PEOPLE_SUFFIX_RE.sub("", line)
                        if stem != line:
                            existing.add(stem)  # 旧行带人物标注时，原始行也算已存在
            new_lines: list[str] = []
            if not file_exists:
                new_lines.append(f"# 微信聊天记录（来源：{source_label}，按时间排序，重复自动跳过）")
            for msg in sorted(msgs, key=lambda m: m.time_str):
                if msg.line in existing:
                    skipped += 1
                    continue
                out_line = msg.line + people_suffix(msg)
                existing.add(msg.line)
                if out_line != msg.line:
                    existing.add(out_line)
                new_lines.append(out_line)
                written += 1
            if new_lines:
                with file_path.open("a", encoding="utf-8", newline="\n") as handle:
                    for ln in new_lines:
                        handle.write(ln + "\n")
        except OSError as exc:
            error = str(exc)
        reports.append(WriteReport(dir_path, file_path, written, skipped, error))
    return reports


def scan_existing_message_lines(directory: str | os.PathLike) -> set[str]:
    """扫描目录下的全部 txt 文件，收集出现过的整行（含去掉人物标注后缀的原始行）。

    用于人物消息导出前的去重：导出目录里之前保存的 txt 中已存在的消息，
    这次不再重复保存。目录不存在或个别文件读取失败（被占用等）时安静跳过。
    """
    directory = Path(directory)
    lines: set[str] = set()
    if not directory.is_dir():
        return lines
    for txt in sorted(directory.glob("*.txt")):
        try:
            with txt.open("r", encoding="utf-8", errors="replace") as handle:
                for ln in handle:
                    line = ln.rstrip("\r\n")
                    lines.add(line)
                    stem = _PEOPLE_SUFFIX_RE.sub("", line)
                    if stem != line:
                        lines.add(stem)  # 旧行带人物标注时，原始行也算已存在
        except OSError:
            continue
    return lines


def summarize(results: list[ClassifyResult]) -> str:
    """生成一行统计摘要。"""
    total = len(results)
    classified = sum(1 for r in results if r.candidate is not None)
    noise = sum(1 for r in results if is_noise_content(r.message.content))
    annotated = sum(1 for r in results if r.message.people)
    failed = sum(1 for r in results if r.reason.startswith("LLM调用失败"))
    cancelled = sum(1 for r in results if r.reason == "已取消")
    text = (
        f"共 {total} 条消息：LLM 已推荐 {classified} 条，"
        f"未分类 {total - classified} 条（其中系统/附件 {noise} 条）"
    )
    if annotated:
        text += f"，已标注人物 {annotated} 条"
    if failed:
        text += f"，LLM 调用失败 {failed} 条（可重新分析或手动指定目录）"
    if cancelled:
        text += f"，已取消 {cancelled} 条"
    return text
