"""浏览器记录数据源：对接「AI对话导出 word/json/md」浏览器扩展。

扩展（DeepSeek/纳米AI/腾讯元宝/Kimi/通义千问/讯飞星火/豆包）在平台上拦截对话数据，
一键上传到本服务；本模块提供：
- 上传接收 API（与原 upload-server 协议兼容：POST /upload /check /batch-log）
- 各平台消息归一化（移植自原 server.js，写入 SQLite）
- 扩展副本管理（vendor → <data_dir>/browser_history/extension/，patch 上传地址）
- 安装检测（扫描 Chrome/Edge 用户数据目录的扩展配置）
- ContextSource 状态汇总（各平台已同步会话统计）
"""

from __future__ import annotations

import datetime
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote as _url_quote
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .app_paths import get_data_dir
from .logging_config import get_logger

logger = get_logger(__name__)

# 与扩展副本一致的固定 token（仅本机回环使用，双方配对即可）
UPLOAD_TOKEN = "525186097a4d4879266580c3c3e72cd9384d0eb50a1f52b7"
# 上传服务端口：默认 10392，后端以其他端口启动时优先环境变量
BACKEND_PORT = int(os.environ.get("WECHAT_TOOL_PORT") or 10392)
UPLOAD_BASE_URL = f"http://127.0.0.1:{BACKEND_PORT}"
EXTENSION_NAME = "AI对话导出 word/json/md"

# 平台元数据：code 与扩展/上传协议一致
PLATFORMS: list[dict[str, str]] = [
    {"code": "deepseek", "name": "DeepSeek", "url": "https://chat.deepseek.com/"},
    {"code": "ncn", "name": "纳米AI", "url": "https://bot.n.cn/"},
    {"code": "yuanbao", "name": "腾讯元宝", "url": "https://yuanbao.tencent.com/"},
    {"code": "kimi", "name": "Kimi", "url": "https://kimi.com/"},
    {"code": "qianwen", "name": "通义千问", "url": "https://www.tongyi.com/"},
    {"code": "tongyi", "name": "通义千问（旧版）", "url": "https://tongyi.com/"},
    {"code": "iflytek", "name": "讯飞星火", "url": "https://xinghuo.xfyun.cn/"},
    {"code": "doubao", "name": "豆包", "url": "https://www.doubao.com/chat/"},
]
PLATFORM_META = {p["code"]: p for p in PLATFORMS}

_VENDOR_DIR = Path(__file__).parent / "browser_extension_src"


# ---------------- 存储 ----------------

def _browser_history_root() -> Path:
    return get_data_dir() / "browser_history"


def _db_path() -> Path:
    return _browser_history_root() / "conversations.db"


_DB_LOCK = threading.Lock()
_DB_CONN: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    global _DB_CONN
    if _DB_CONN is not None:
        # 数据目录可能切换（测试/迁移），连接指向旧文件时重建
        try:
            current = _DB_CONN.execute("PRAGMA database_list").fetchone()[2]
            if Path(current) != _db_path():
                _DB_CONN.close()
                _DB_CONN = None
        except Exception:
            _DB_CONN = None
    if _DB_CONN is None:
        root = _browser_history_root()
        root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_db_path(), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_code TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                title TEXT,
                message_count INTEGER DEFAULT 0,
                char_count INTEGER DEFAULT 0,
                first_message_at TEXT,
                last_message_at TEXT,
                platform_created_at TEXT,
                platform_updated_at TEXT,
                content_hash TEXT,
                received_at TEXT,
                UNIQUE(platform_code, conversation_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_row_id INTEGER NOT NULL,
                platform_code TEXT NOT NULL,
                message_id TEXT,
                role TEXT,
                seq INTEGER,
                content TEXT,
                thinking_content TEXT,
                msg_time TEXT,
                UNIQUE(conversation_row_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS export_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_no TEXT,
                platform_code TEXT,
                batch_type TEXT,
                conversation_count INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                total_chars INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                client_version TEXT,
                exported_at TEXT,
                UNIQUE(batch_no)
            );
            """
        )
        # 设备隔离迁移：旧库补充 device_id 列（扩展按浏览器生成并持久化的设备标识）
        try:
            conn.execute("ALTER TABLE conversations ADD COLUMN device_id TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在
        conn.commit()
        _DB_CONN = conn
    return _DB_CONN


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------- 归一化（移植自原 upload-server server.js） ----------------

def _to_dt(v: Any) -> str | None:
    """秒/毫秒时间戳或时间字符串 → ISO 文本（无效返回 None）。"""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        ms = v if v > 1e12 else (v * 1000 if v > 1e9 else None)
        if ms is None:
            return None
        try:
            return datetime.datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return _to_dt(float(s))
        try:
            d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d.replace(tzinfo=None).isoformat(timespec="seconds")
        except ValueError:
            return None
    return None


def _stable_id(*parts: Any) -> str:
    return "h-" + hashlib.md5("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:24]


def _norm_deepseek(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages or []:
        content = ""
        thinking = ""
        for f in m.get("fragments") or []:
            ftype = f.get("type")
            if ftype == "THINK":
                thinking += (f.get("content") or "") + "\n"
            elif ftype in ("RESPONSE", "TIP", "REQUEST"):
                content += (f.get("content") or "") + "\n"
        if not content.strip() and isinstance(m.get("content"), str):
            content = m["content"]
        role_map = {"USER": "user", "ASSISTANT": "assistant", "SYSTEM": "system"}
        out.append({
            "message_id": str(
                m.get("message_id")
                if m.get("message_id") is not None
                else _stable_id("ds", m.get("inserted_at"), content[:80])
            ),
            "role": role_map.get(m.get("role"), "assistant"),
            "content": content.strip() or None,
            "thinking_content": thinking.strip() or None,
            "msg_time": _to_dt(m.get("inserted_at")),
        })
    return out


def _norm_ncn(messages: list[dict], conv_id: str) -> list[dict]:
    out = []
    for i, m in enumerate(messages or []):
        if m.get("prompt") and str(m["prompt"]).strip():
            out.append({
                "message_id": f"{conv_id}-u{i}",
                "role": "user",
                "content": str(m["prompt"]).strip() or None,
                "thinking_content": None,
                "msg_time": None,
            })
        if m.get("result") and str(m["result"]).strip():
            out.append({
                "message_id": f"{conv_id}-a{i}",
                "role": "assistant",
                "content": str(m["result"]).strip() or None,
                "thinking_content": None,
                "msg_time": None,
            })
    return out


def _yuanbao_blocks(blocks: list, acc: dict) -> None:
    for b in blocks or []:
        btype = b.get("type")
        if btype == "text" and b.get("msg"):
            acc["content"] += str(b["msg"]) + "\n\n"
        elif btype == "think":
            acc["thinking"] += (b.get("content") or "") + "\n"
        elif not isinstance(b, dict):
            continue


def _norm_yuanbao(convs: list[dict], conv_id: str) -> list[dict]:
    out = []
    for i, turn in enumerate(sorted(convs or [], key=lambda t: t.get("index") or 0)):
        acc = {"content": "", "thinking": ""}
        if isinstance(turn.get("content"), list):
            _yuanbao_blocks(turn["content"], acc)
        if not acc["content"].strip() and turn.get("speechesV2"):
            for sp in turn["speechesV2"]:
                _yuanbao_blocks(sp.get("content"), acc)
        if not acc["content"].strip() and isinstance(turn.get("content"), str):
            acc["content"] = turn["content"]
        if not acc["content"].strip() and isinstance(turn.get("displayPrompt"), str):
            acc["content"] = turn["displayPrompt"]
        out.append({
            "message_id": str(
                turn.get("msgId")
                if turn.get("msgId") is not None
                else f"{conv_id}-{turn.get('index') if turn.get('index') is not None else i}"
            ),
            "role": "user" if turn.get("speaker") == "human" else "assistant",
            "content": acc["content"].strip() or None,
            "thinking_content": acc["thinking"].strip() or None,
            "msg_time": _to_dt(turn.get("createTime")),
        })
    return out


def _norm_kimi(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") == "system" or not m.get("id"):
            continue
        content = ""
        thinking = ""
        for b in m.get("blocks") or []:
            if isinstance(b, dict) and isinstance(b.get("text"), dict) and b["text"].get("content"):
                content += str(b["text"]["content"]) + "\n"
            elif isinstance(b, dict) and isinstance(b.get("think"), dict) and b["think"].get("content"):
                thinking += str(b["think"]["content"]) + "\n"
        out.append({
            "message_id": str(m["id"]),
            "role": "user" if m.get("role") == "user" else "assistant",
            "content": content.strip() or None,
            "thinking_content": thinking.strip() or None,
            "msg_time": _to_dt(m.get("createTime")),
        })
    return out


def _norm_qianwen(turns: list[dict], session_id: str) -> list[dict]:
    def _key(t: dict):
        pos = t.get("pos")
        return float(pos) if pos is not None else float(t.get("request_timestamp") or 0)

    out = []
    for i, turn in enumerate(sorted(turns or [], key=_key)):
        t = _to_dt(turn.get("pos") or turn.get("request_timestamp"))
        base_id = str(
            turn.get("req_id")
            if turn.get("req_id") is not None
            else f"{session_id}-t{i}"
        )
        texts = []
        for m in turn.get("request_messages") or []:
            if m.get("mime_type") == "text/plain" and m.get("content"):
                texts.append(str(m["content"]))
        if texts:
            out.append({
                "message_id": f"{base_id}-u",
                "role": "user",
                "content": "\n".join(texts) or None,
                "thinking_content": None,
                "msg_time": t,
            })
        answers = [
            str(m["content"])
            for m in turn.get("response_messages") or []
            if m.get("mime_type") == "multi_load/iframe"
            and m.get("content")
            and str(m["content"]).strip()
        ]
        if answers:
            out.append({
                "message_id": f"{base_id}-a",
                "role": "assistant",
                "content": "\n\n".join(answers) or None,
                "thinking_content": None,
                "msg_time": t,
            })
    return out


def _norm_iflytek(history_list: list[dict], chat_id: str) -> list[dict]:
    out = []
    for i, m in enumerate(history_list or []):
        is_user = m.get("type") == 0
        content = m.get("message") or ""
        if not str(content).strip() and not is_user and m.get("jsonMessage"):
            try:
                content = json.loads(m["jsonMessage"]).get("text") or m["jsonMessage"]
            except (ValueError, TypeError):
                content = m["jsonMessage"]
        out.append({
            "message_id": str(
                m.get("messageId")
                if m.get("messageId") is not None
                else (m.get("msgId") if m.get("msgId") is not None else f"{chat_id}-m{i}")
            ),
            "role": "user" if is_user else "assistant",
            "content": str(content).strip() or None,
            "thinking_content": None,
            "msg_time": _to_dt(m.get("createTime")),
        })
    return out


def _doubao_text(msg: dict) -> str:
    content = ""

    def push_block(item: dict) -> None:
        nonlocal content
        if not isinstance(item, dict):
            return
        if item.get("block_type") == 10000:
            tb = (item.get("content") or {}).get("text_block") or {}
            if tb.get("text"):
                content += tb["text"] + "\n\n"

    try:
        if msg.get("content_type") == 1:
            obj = msg.get("content") or {}
            if isinstance(obj, str):
                obj = json.loads(obj or "{}")
            content = obj.get("text") or ""
        elif msg.get("content_type") == 9999:
            arr = msg.get("content") or []
            if isinstance(arr, str):
                arr = json.loads(arr or "[]")
            if isinstance(arr, list):
                for item in arr:
                    push_block(item)
            if not content.strip() and isinstance(msg.get("content_block"), list):
                for item in msg["content_block"]:
                    push_block(item)
    except (ValueError, TypeError):
        pass
    return content


def _norm_doubao(messages: list[dict]) -> list[dict]:
    out = []
    for m in sorted(messages or [], key=lambda x: x.get("create_time") or 0):
        content = _doubao_text(m)
        out.append({
            "message_id": str(
                m.get("message_id")
                if m.get("message_id") is not None
                else _stable_id("db", m.get("create_time"), content[:80])
            ),
            "role": "user" if m.get("user_type") == 1 else "assistant",
            "content": content.strip() or None,
            "thinking_content": m.get("thinking") if isinstance(m.get("thinking"), str) else None,
            "msg_time": _to_dt(m.get("create_time")),
        })
    return out


def _norm_tongyi(conv: dict, ctx: dict) -> list[dict]:
    msgs = sorted(conv.get("data") or [], key=lambda m: m.get("createTime") or 0)
    out = []
    for i, m in enumerate(msgs):
        texts = []
        for c in m.get("contents") or []:
            if c.get("contentType") == "text" and c.get("content"):
                texts.append(str(c["content"]))
        conv_id = str(ctx.get("conversation_id") or "") or _stable_id(
            "tongyi", ctx.get("title") or "", str((msgs[0].get("contents") or [{}])[0].get("content") or "")[:100]
        )
        out.append({
            "message_id": str(
                m.get("messageId")
                if m.get("messageId") is not None
                else f"{conv_id}-m{i}"
            ),
            "role": "user" if m.get("senderType") == "USER" else "assistant",
            "content": "\n".join(texts) or None,
            "thinking_content": None,
            "msg_time": _to_dt(m.get("createTime")),
        })
    return out


def normalize_conversation(platform: str, conv: dict, ctx: dict | None = None) -> dict:
    """平台原始会话数据 → 统一结构 {conversation_id, title, times, messages[]}。"""
    ctx = ctx or {}
    if not isinstance(conv, dict):
        raise ValueError("conversation 数据无效")

    if platform == "deepseek":
        is_single = not conv.get("messages") and (conv.get("data") or {}).get("biz_data")
        biz = (conv.get("data") or {}).get("biz_data") or {}
        messages = biz.get("chat_messages") if is_single else conv.get("messages") or []
        conversation_id = str(
            (ctx.get("conversation_id") or "") if is_single else (conv.get("chat_session_id") or ctx.get("conversation_id") or "")
        )
        title = (
            (biz.get("chat_session") or {}).get("title") or ctx.get("title")
            if is_single
            else conv.get("title") or ctx.get("title")
        )
        norm = _norm_deepseek(messages)
        return {
            "conversation_id": conversation_id or _stable_id("deepseek", title, str((norm[0] or {}).get("content") or "")[:100]),
            "title": title or "DeepSeek对话",
            "platform_created_at": None,
            "platform_updated_at": _to_dt(None if is_single else conv.get("updated_at")),
            "messages": norm,
        }

    if platform == "ncn":
        meta = conv.get("data") or {}
        messages = meta.get("messages") or []
        title = meta.get("title") or ctx.get("title") or "纳米AI对话"
        conversation_id = str(ctx.get("conversation_id") or "")
        conv_id = conversation_id or _stable_id("ncn", title, str((messages[0] or {}).get("prompt") or "")[:100])
        return {
            "conversation_id": conv_id,
            "title": title,
            "platform_created_at": None,
            "platform_updated_at": _to_dt(conv.get("timestamp")),
            "messages": _norm_ncn(messages, conv_id),
        }

    if platform == "yuanbao":
        has_batch_field = conv.get("conversation_id") is not None
        convs = conv.get("convs") or []
        title = conv.get("title") or conv.get("sessionTitle") or ctx.get("title") or "元宝对话"
        conversation_id = str(conv.get("conversation_id") if has_batch_field else (ctx.get("conversation_id") or ""))
        conv_id = conversation_id or _stable_id("yuanbao", title, str(((convs[0] or {}).get("content") or [{}])[0].get("msg") or (convs[0] or {}).get("displayPrompt") or "")[:100])
        return {
            "conversation_id": conv_id,
            "title": title,
            "platform_created_at": None,
            "platform_updated_at": _to_dt(conv.get("last_replied_at")),
            "messages": _norm_yuanbao(convs, conv_id),
        }

    if platform == "kimi":
        messages = conv.get("messages") or []
        conversation_id = str(conv.get("chat_id") or ctx.get("conversation_id") or "")
        conv_id = conversation_id or _stable_id("kimi", ctx.get("title") or "", "")
        return {
            "conversation_id": conv_id,
            "title": conv.get("title") or ctx.get("title") or "Kimi对话",
            "platform_created_at": _to_dt(conv.get("create_time")),
            "platform_updated_at": _to_dt(conv.get("update_time")),
            "messages": _norm_kimi(messages),
        }

    if platform == "qianwen":
        is_single = (conv.get("data") or {}).get("list") is not None
        turns = (conv.get("data") or {}).get("list") if is_single else conv.get("turns") or []
        conversation_id = str(
            (ctx.get("conversation_id") or "") if is_single else (conv.get("session_id") or ctx.get("conversation_id") or "")
        )
        title = (ctx.get("title") or "通义千问对话") if is_single else (conv.get("title") or ctx.get("title"))
        conv_id = conversation_id or _stable_id("qianwen", title, str(((turns[0] or {}).get("request_messages") or [{}])[0].get("content") or "")[:100])
        return {
            "conversation_id": conv_id,
            "title": title or "通义千问对话",
            "platform_created_at": _to_dt(None if is_single else conv.get("created_at")),
            "platform_updated_at": _to_dt(None if is_single else conv.get("updated_at")),
            "messages": _norm_qianwen(turns, conv_id),
        }

    if platform == "iflytek":
        is_single = isinstance(conv.get("data"), list)
        item = (conv.get("data") or [{}])[0] if is_single else conv
        history_list = item.get("historyList") or []
        conversation_id = str(item.get("chatId") or item.get("chat_id") or ctx.get("conversation_id") or "")
        conv_id = conversation_id or _stable_id("iflytek", ctx.get("title") or "", str(next((m.get("message") for m in history_list if m.get("type") == 0), "") or "")[:100])
        return {
            "conversation_id": conv_id,
            "title": item.get("title") or ctx.get("title") or "讯飞星火对话",
            "platform_created_at": _to_dt(item.get("create_time")),
            "platform_updated_at": _to_dt(item.get("update_time")),
            "messages": _norm_iflytek(history_list, conv_id),
        }

    if platform == "doubao":
        is_single = conv.get("downlink_body") is not None
        if is_single:
            messages = ((conv.get("downlink_body") or {}).get("pull_singe_chain_downlink_body") or {}).get("messages") or []
        else:
            messages = conv.get("messages") or []
        conversation_id = str(
            (ctx.get("conversation_id") or "") if is_single else (conv.get("conversation_id") or ctx.get("conversation_id") or "")
        )
        title = (ctx.get("title") or "豆包对话") if is_single else (conv.get("name") or conv.get("title") or ctx.get("title"))
        conv_id = conversation_id or _stable_id("doubao", title, str((messages[0] or {}).get("content") or "")[:100])
        return {
            "conversation_id": conv_id,
            "title": title or "豆包对话",
            "platform_created_at": _to_dt(None if is_single else conv.get("create_time")),
            "platform_updated_at": _to_dt(None if is_single else conv.get("update_time")),
            "messages": _norm_doubao(messages),
        }

    if platform == "tongyi":
        list_ = conv.get("data") or []
        conv_id = str(ctx.get("conversation_id") or "") or _stable_id("tongyi", ctx.get("title") or "", str((list_[0].get("contents") or [{}])[0].get("content") or "")[:100] if list_ else "")
        return {
            "conversation_id": conv_id,
            "title": ctx.get("title") or "通义千问对话",
            "platform_created_at": None,
            "platform_updated_at": None,
            "messages": _norm_tongyi(conv, ctx),
        }

    raise ValueError(f"不支持的平台: {platform}")


def _content_hash(messages: list[dict]) -> str:
    h = hashlib.md5()
    parts = [
        "\x01".join([
            str(m.get("message_id") or ""),
            str(m.get("role") or ""),
            str(m.get("content") or ""),
            str(m.get("thinking_content") or ""),
            str(m.get("msg_time") or ""),
        ])
        for m in messages
    ]
    h.update("\x02".join(parts).encode("utf-8"))
    return h.hexdigest()


def _upsert_conversation(platform: str, n: dict, device_id: str | None = None) -> dict:
    db = _get_db()
    now = _now_iso()
    char_count = sum(
        len(m.get("content") or "") + len(m.get("thinking_content") or "") for m in n["messages"]
    )
    times = [m["msg_time"] for m in n["messages"] if m.get("msg_time")]
    first = min(times) if times else None
    last = max(times) if times else None
    chash = _content_hash(n["messages"])

    with _DB_LOCK, db:
        existing = db.execute(
            "SELECT id, content_hash FROM conversations WHERE platform_code=? AND conversation_id=?",
            (platform, n["conversation_id"]),
        ).fetchone()
        if existing and existing["content_hash"] and existing["content_hash"] == chash:
            db.execute(
                "UPDATE conversations SET received_at=?, device_id=COALESCE(?, device_id) WHERE id=?",
                (now, device_id, existing["id"]),
            )
            return {"skipped": True, "conversation_id": n["conversation_id"], "message_count": len(n["messages"])}

        db.execute(
            """INSERT INTO conversations
                 (platform_code, conversation_id, title, message_count, char_count,
                  first_message_at, last_message_at, platform_created_at, platform_updated_at,
                  content_hash, received_at, device_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(platform_code, conversation_id) DO UPDATE SET
                 title=excluded.title, message_count=excluded.message_count,
                 char_count=excluded.char_count, first_message_at=excluded.first_message_at,
                 last_message_at=excluded.last_message_at, platform_created_at=excluded.platform_created_at,
                 platform_updated_at=excluded.platform_updated_at, content_hash=excluded.content_hash,
                 received_at=excluded.received_at, device_id=COALESCE(excluded.device_id, conversations.device_id)""",
            (
                platform, n["conversation_id"], (n.get("title") or "")[:512] or None,
                len(n["messages"]), char_count, first, last,
                n.get("platform_created_at"), n.get("platform_updated_at"), chash, now, device_id,
            ),
        )
        row = db.execute(
            "SELECT id FROM conversations WHERE platform_code=? AND conversation_id=?",
            (platform, n["conversation_id"]),
        ).fetchone()
        conv_row_id = row["id"]
        db.execute("DELETE FROM messages WHERE conversation_row_id=?", (conv_row_id,))
        db.executemany(
            """INSERT OR IGNORE INTO messages
                 (conversation_row_id, platform_code, message_id, role, seq, content, thinking_content, msg_time)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (conv_row_id, platform, m.get("message_id"), m.get("role"), i,
                 m.get("content"), m.get("thinking_content"), m.get("msg_time"))
                for i, m in enumerate(n["messages"])
            ],
        )
    return {
        "conversation_id": n["conversation_id"],
        "title": n.get("title"),
        "message_count": len(n["messages"]),
        "char_count": char_count,
    }


# ---------------- 上传 API（扩展协议兼容） ----------------

def _check_token(request: Request, payload: dict) -> None:
    token = request.headers.get("x-upload-token") or payload.get("token")
    if token != UPLOAD_TOKEN:
        raise HTTPException(401, "token 无效")


upload_router = APIRouter()


def _upload_json(content: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content,
        status_code=status_code,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, X-Upload-Token",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        },
    )


@upload_router.options("/upload")
@upload_router.options("/check")
@upload_router.options("/batch-log")
async def _upload_preflight():
    return _upload_json({"ok": True})


@upload_router.get("/upload/health")
async def upload_health():
    return _upload_json({"ok": True, "time": _now_iso()})


@upload_router.post("/upload")
async def upload_conversation(request: Request):
    payload = await request.json()
    _check_token(request, payload)
    platform = re.sub(r"_(title|chat)$", "", str(payload.get("platform") or ""))
    if not platform:
        return _upload_json({"ok": False, "error": "缺少 platform"}, 400)
    # 设备标识：body.device_id 优先，其次 X-Device-Id 头（与原服务一致）
    device_id = str(payload.get("device_id") or request.headers.get("x-device-id") or "").strip()[:64] or None
    try:
        n = normalize_conversation(
            platform,
            payload.get("conversation"),
            {"conversation_id": payload.get("conversation_id"), "title": payload.get("title")},
        )
    except Exception as e:  # noqa: BLE001 - 与原服务一致，归一化错误原样返回
        return _upload_json({"ok": False, "error": f"归一化失败: {e}"}, 400)
    if not n["messages"]:
        return _upload_json({"ok": False, "error": "会话无消息"}, 400)
    try:
        result = _upsert_conversation(platform, n, device_id)
        refresh_manifest()
    except Exception:
        logger.exception("[browser_history] /upload 入库失败 platform=%s", platform)
        return _upload_json({"ok": False, "error": "入库失败"}, 500)
    return _upload_json({"ok": True, **result})


@upload_router.post("/check")
async def check_conversations(request: Request):
    payload = await request.json()
    _check_token(request, payload)
    platform = re.sub(r"_(title|chat)$", "", str(payload.get("platform") or ""))
    items = payload.get("items") or []
    if not platform or not items:
        return _upload_json({"ok": False, "error": "参数缺失"}, 400)
    ids = {str(it.get("conversation_id") or "").strip() for it in items if str(it.get("conversation_id") or "").strip()}
    db = _get_db()
    with _DB_LOCK:
        db_map = {}
        if ids:
            rows = db.execute(
                "SELECT conversation_id, platform_updated_at, last_message_at, message_count"
                " FROM conversations WHERE platform_code=?"
                f" AND conversation_id IN ({','.join('?' * len(ids))})",
                [platform, *ids],
            ).fetchall()
            db_map = {r["conversation_id"]: r for r in rows}
    need_upload = []
    skipped = 0
    for it in items:
        cid = str(it.get("conversation_id") or "").strip()
        db_row = db_map.get(cid) if cid else None
        if not db_row:
            need_upload.append(cid or None)
            continue
        item_time = _to_dt(it.get("updated"))
        db_time = db_row["platform_updated_at"] or db_row["last_message_at"]
        not_newer = bool(item_time and db_time and item_time <= db_time)
        not_more = (not it.get("count")) or int(it.get("count") or 0) <= int(db_row["message_count"] or 0)
        if not_newer and not_more:
            skipped += 1
        else:
            need_upload.append(cid)
    return _upload_json({"ok": True, "need_upload": need_upload, "skipped_count": skipped})


@upload_router.post("/batch-log")
async def upload_batch_log(request: Request):
    payload = await request.json()
    _check_token(request, payload)
    db = _get_db()
    with _DB_LOCK, db:
        db.execute(
            """INSERT INTO export_batches
                 (batch_no, platform_code, batch_type, conversation_count, message_count,
                  total_chars, failed_count, client_version, exported_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(batch_no) DO UPDATE SET
                 conversation_count=excluded.conversation_count,
                 message_count=excluded.message_count,
                 total_chars=excluded.total_chars, failed_count=excluded.failed_count""",
            (
                payload.get("batch_no"), payload.get("platform"), payload.get("batch_type") or "single",
                payload.get("conversation_count") or 0, payload.get("message_count") or 0,
                payload.get("total_chars") or 0, payload.get("failed_count") or 0,
                payload.get("client_version"), _to_dt(payload.get("exported_at")) or _now_iso(),
            ),
        )
    return _upload_json({"ok": True})


# ---------------- 统计与状态 ----------------

def _build_summary() -> dict[str, Any]:
    db = _get_db()
    with _DB_LOCK:
        rows = db.execute(
            "SELECT platform_code, COUNT(*) AS convs, COALESCE(SUM(message_count),0) AS msgs,"
            " MAX(received_at) AS last_at FROM conversations GROUP BY platform_code"
        ).fetchall()
        total = db.execute(
            "SELECT COUNT(*) AS convs, COALESCE(SUM(message_count),0) AS msgs,"
            " MAX(received_at) AS last_at FROM conversations"
        ).fetchone()
    stats = {r["platform_code"]: r for r in rows}
    platforms = []
    for meta in PLATFORMS:
        s = stats.get(meta["code"])
        if not s:
            continue
        platforms.append({
            "code": meta["code"],
            "name": meta["name"],
            "url": meta["url"],
            "conversations": s["convs"],
            "messages": s["msgs"],
            "last_received_at": s["last_at"],
        })
    return {
        "platforms": platforms,
        "total_conversations": total["convs"],
        "total_messages": total["msgs"],
        "last_uploaded_at": total["last_at"],
        "db_path": str(_db_path()),
    }


def refresh_manifest() -> None:
    from .context_registry import write_manifest

    write_manifest("browser_history", summary=_build_summary(), data_ref=str(_db_path()))


# ---------------- 扩展副本管理 ----------------

def extension_copy_dir() -> Path:
    return _browser_history_root() / "extension"


def _copy_and_patch_extension(src: Path, dst_root: Path) -> None:
    """把单个浏览器版本的扩展复制到目标目录，并 patch host_permissions 与上传地址。"""
    dst_root.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "background.js", "bridge.js", "content.js", "popup.html", "popup.js"):
        shutil.copy2(src / name, dst_root / name)
    (dst_root / "icons").mkdir(exist_ok=True)
    for icon in ("icon16.png", "icon48.png", "icon128.png"):
        shutil.copy2(src / "icons" / icon, dst_root / "icons" / icon)

    # patch manifest：host_permissions 增加本机后端，允许扩展后台跨域上传
    manifest = json.loads((dst_root / "manifest.json").read_text(encoding="utf-8"))
    hosts = manifest.setdefault("host_permissions", [])
    for host in ("http://127.0.0.1/*", "http://localhost/*"):
        if host not in hosts:
            hosts.append(host)
    (dst_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=4), encoding="utf-8"
    )

    # patch content.js：上传服务地址指向本机
    content = (dst_root / "content.js").read_text(encoding="utf-8")
    content = re.sub(
        r"UPLOAD_SERVER:\s*\{[^}]*\}",
        f"UPLOAD_SERVER: {{\n            url: '{UPLOAD_BASE_URL}',\n            token: '{UPLOAD_TOKEN}'\n        }}",
        content,
        count=1,
    )
    (dst_root / "content.js").write_text(content, encoding="utf-8")


def ensure_extension_copy() -> dict[str, Any]:
    """把 vendor 扩展（Chrome 根目录 + Firefox 子目录）复制到数据目录并 patch。幂等。"""
    if not _VENDOR_DIR.is_dir():
        raise HTTPException(500, f"扩展源目录缺失：{_VENDOR_DIR}")
    dst = extension_copy_dir()
    _copy_and_patch_extension(_VENDOR_DIR, dst)

    firefox_src = _VENDOR_DIR / "firefox"
    firefox_dir = None
    if firefox_src.is_dir():
        _copy_and_patch_extension(firefox_src, dst / "firefox")
        firefox_dir = str(dst / "firefox")

    logger.info("[browser_history] 扩展副本已生成 dir=%s upload_url=%s", dst, UPLOAD_BASE_URL)
    result: dict[str, Any] = {"extension_dir": str(dst), "upload_url": UPLOAD_BASE_URL}
    if firefox_dir:
        result["firefox_extension_dir"] = firefox_dir
    return result


# ---------------- 安装检测 ----------------

# 商店正式版「AI对话导出 word/json/md」的固定扩展 ID（Chrome Web Store 安装版，
# 版本描述：支持 DeepSeek、纳米AI、腾讯元宝、Kimi、通义千问、讯飞星火、豆包）
STORE_EXTENSION_ID = "cjdpniadfgdaekgbcdfbkoiilhmcdkge"
# Firefox 版扩展在 browser_specific_settings.gecko.id 中声明的固定 ID
FIREFOX_EXTENSION_ID = "ai-dialog-export@scrollmerge.extension"


def _browser_profile_dirs() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return out
    for browser, rel in (
        ("Chrome", Path(local) / "Google" / "Chrome" / "User Data"),
        ("Edge", Path(local) / "Microsoft" / "Edge" / "User Data"),
    ):
        user_data = rel
        if not user_data.is_dir():
            continue
        for profile in user_data.iterdir():
            if profile.name in ("Default",) or profile.name.startswith("Profile "):
                if (profile / "Preferences").exists() or (profile / "Secure Preferences").exists():
                    out.append((browser, profile))
    return out


def _firefox_extension_installations() -> list[dict[str, str]]:
    """扫描 Firefox 各 profile 的 extensions.json，命中扩展 ID/名称即视为已安装。"""
    out: list[dict[str, str]] = []
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return out
    profiles_root = Path(appdata) / "Mozilla" / "Firefox" / "Profiles"
    if not profiles_root.is_dir():
        return out
    for profile in profiles_root.iterdir():
        ext_json = profile / "extensions.json"
        try:
            data = json.loads(ext_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        addons = data.get("addons")
        if not isinstance(addons, list):
            continue
        for addon in addons:
            if not isinstance(addon, dict):
                continue
            ext_id = str(addon.get("id") or "")
            name = str(
                (addon.get("defaultLocale") or {}).get("name") or addon.get("name") or ""
            )
            if ext_id == FIREFOX_EXTENSION_ID or EXTENSION_NAME in name:
                out.append({
                    "browser": "Firefox",
                    "profile": profile.name,
                    "extension_id": ext_id,
                    "name": name or EXTENSION_NAME,
                    "path": str(ext_json),
                })
                break
    return out


def detect_extension_install() -> dict[str, Any]:
    """扫描 Chrome/Edge/Firefox 各 profile 检测扩展安装状态（任一途径命中即已安装）。

    1. Preferences/Secure Preferences 中扩展名称包含「AI对话导出 word/json/md」（本地加载副本）
    2. 扩展配置中存在商店正式版的固定 ID（cjdpniadfgdaekgbcdfbkoiilhmcdkge）
    3. profile 的 Extensions/<商店版固定ID>/ 目录存在（商店安装的落盘位置，不依赖 Preferences 解析）
    4. Firefox 的 extensions.json 中存在 gecko 固定 ID（ai-dialog-export@scrollmerge.extension）
    """
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(browser: str, profile_name: str, ext_id: str, name: str, path: str) -> None:
        key = (browser, profile_name, ext_id or name)
        if key in seen:
            return
        seen.add(key)
        found.append({
            "browser": browser,
            "profile": profile_name,
            "extension_id": str(ext_id),
            "name": name or EXTENSION_NAME,
            "path": path,
        })

    for browser, profile in _browser_profile_dirs():
        for pref_name in ("Preferences", "Secure Preferences"):
            pref = profile / pref_name
            try:
                data = json.loads(pref.read_text(encoding="utf-8"))
            except Exception:
                continue
            settings = ((data.get("extensions") or {}).get("settings") or {})
            for ext_id, ext in settings.items():
                if not isinstance(ext, dict):
                    continue
                name = str(((ext.get("manifest") or {}).get("name")) or "")
                path = str(ext.get("path") or "")
                if (
                    EXTENSION_NAME in name
                    or "browser_history" in path.replace("/", "\\")
                    or str(ext_id) == STORE_EXTENSION_ID
                ):
                    add(browser, profile.name, str(ext_id), name, path)

        # 文件系统途径：商店版扩展的安装目录存在（Preferences 结构变化时兜底）
        store_dir = profile / "Extensions" / STORE_EXTENSION_ID
        if store_dir.is_dir():
            add(browser, profile.name, STORE_EXTENSION_ID, EXTENSION_NAME, str(store_dir))

    # Firefox：读取 extensions.json 中的 addons 列表
    for firefox_install in _firefox_extension_installations():
        add(
            firefox_install["browser"],
            firefox_install["profile"],
            firefox_install["extension_id"],
            firefox_install["name"],
            firefox_install["path"],
        )

    return {
        "installed": bool(found),
        "installations": found,
        "extension_name": EXTENSION_NAME,
        "extension_dir": str(extension_copy_dir()) if extension_copy_dir().exists() else None,
    }


_EXTENSION_PAGE_URLS = {
    "chrome": "chrome://extensions/",
    "edge": "edge://extensions/",
    "firefox": "about:debugging#/runtime/this-firefox",
}


def _winreg_value(root, subkey: str, name: str = "") -> Optional[str]:
    """读注册表字符串值，失败返回 None（winreg 仅 Windows 存在）。"""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(root, subkey) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


def _exe_from_command(command: str) -> str:
    """从注册表的 'C:\\...\\chrome.exe" --single-argument' 提取可执行文件路径。"""
    command = (command or "").strip()
    if not command:
        return ""
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end].strip() if end > 0 else ""
    return command.split(" ", 1)[0].strip()


def _candidate_browser_exes(kind: str) -> list[str]:
    """多渠道定位浏览器可执行文件，覆盖不同电脑的自定义安装位置。

    依次尝试：注册表 App Paths、默认浏览器 ProgID、PATH、常见安装目录。
    """
    try:
        import winreg
    except ImportError:
        winreg = None  # type: ignore[assignment]

    exes: list[str] = []
    if winreg is not None:
        # 1) 注册表 App Paths（Chrome/Edge 安装时注册，任意安装位置都能查到）
        app_paths = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{kind}.exe"
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for subkey in (app_paths, "SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + f"{kind}.exe"):
                value = _winreg_value(root, subkey)
                if value:
                    exes.append(value)
                    # 有的机器默认值只写目录，不写完整 exe 路径
                    if not value.lower().endswith(".exe"):
                        exes.append(str(Path(value) / f"{kind}.exe"))
        # 2) 默认浏览器 ProgID（默认浏览器恰好是目标浏览器时命中）
        prog_id = _winreg_value(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
            "ProgID",
        )
        if prog_id:
            exe = _exe_from_command(
                _winreg_value(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command")
            )
            if exe and Path(exe).name.lower() == f"{kind}.exe":
                exes.append(exe)
    # 3) PATH 环境变量中的可执行文件
    found = shutil.which(kind)
    if found:
        exes.append(found)
    # 4) 常见安装目录
    if kind == "chrome":
        exes += [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif kind == "edge":
        exes += [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        ]
    elif kind == "firefox":
        exes += [
            os.path.expandvars(r"%ProgramFiles%\Mozilla Firefox\firefox.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Mozilla Firefox\firefox.exe"),
        ]
    # 去重、仅保留真实存在的文件
    seen: set[str] = set()
    result: list[str] = []
    for exe in exes:
        if not exe:
            continue
        key = os.path.normcase(os.path.normpath(exe))
        if key in seen:
            continue
        seen.add(key)
        if Path(exe).is_file():
            result.append(exe)
    return result


def _is_browser_process_running(kind: str) -> bool:
    """检查浏览器进程是否在运行（Windows 下用 tasklist 查询）。"""
    if os.name != "nt":
        return False
    import subprocess

    exe_name = f"{kind}.exe"
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return exe_name.lower() in (result.stdout or "").lower()


def open_extensions_page(browser: str = "chrome") -> dict[str, Any]:
    """直接用浏览器可执行文件打开扩展管理页，不弹系统「选择应用」对话框。

    浏览器已在运行则不再重复启动（返回 already_running 标记）；未运行才启动，
    带上扩展管理页地址参数启动后自动打开对应页面。
    Chrome 找不到时自动降级 Edge；Firefox（about:debugging）用于临时加载已解压扩展。
    """
    import subprocess

    if browser == "firefox":
        order = ["firefox"]
    elif browser == "edge":
        order = ["edge"]
    else:
        order = ["chrome", "edge"]
    for kind in order:
        exes = _candidate_browser_exes(kind)
        if not exes:
            continue
        url = _EXTENSION_PAGE_URLS[kind]
        if _is_browser_process_running(kind):
            return {"ok": True, "browser": kind, "url": url, "already_running": True}
        for exe in exes:
            try:
                subprocess.Popen([exe, url], close_fds=True)
            except OSError:
                logger.warning("browser_history open-page launch failed: %s", exe)
                continue
            return {"ok": True, "browser": kind, "url": url}
    return {
        "ok": False,
        "error": "未找到 Chrome/Edge/Firefox 浏览器，请手动打开浏览器扩展管理页加载扩展",
    }


def open_platform_url(url: str) -> dict[str, Any]:
    import webbrowser

    webbrowser.open(url)
    return {"ok": True, "url": url}


# ---------------- ContextSource 实现 ----------------

class BrowserHistorySource:
    """浏览器记录上下文源：接收扩展上传的 GPT 平台会话数据，提供统计状态。"""

    planned = False
    source_id = "browser_history"
    display_name = "浏览器记录"

    def status(self) -> dict[str, Any]:
        from .context_registry import load_settings, read_manifest

        settings = load_settings()
        manifest = read_manifest("browser_history")
        install = detect_extension_install()
        summary = _build_summary()
        state = "up_to_date" if summary["total_conversations"] else "idle"
        return {
            "source_id": self.source_id,
            "name": self.display_name,
            "enabled": bool(settings.get("browser_history", {}).get("enabled", True)),
            "state": state,
            "last_saved_at": manifest.get("last_saved_at") or summary.get("last_uploaded_at"),
            "summary": {**summary, "extension": install},
            "planned": False,
        }

    def start(self) -> None:
        pass  # 被动接收：上传端点常驻，无后台任务

    def stop(self) -> None:
        pass


BROWSER_HISTORY_SOURCE = BrowserHistorySource()


# ---------------- 管理端点（本地前端调用） ----------------

def _local_only(request: Request):
    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            raise ValueError()
        host = request.url.hostname
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError()
    except (ValueError, AttributeError):
        raise HTTPException(403, "上下文服务仅允许本机应用访问") from None


manage_router = APIRouter(
    prefix="/api/context/browser-history", dependencies=[Depends(_local_only)]
)


@manage_router.get("/status", summary="扩展安装状态与平台统计")
async def browser_history_status():
    return {"status": "ok", **detect_extension_install(), "summary": _build_summary()}


@manage_router.post("/extension/prepare", summary="生成/更新扩展副本（已指向本机后端）")
async def prepare_extension():
    result = await _run(ensure_extension_copy)
    refresh_manifest()
    return {"status": "ok", **result}


@manage_router.post("/extension/open-page", summary="打开浏览器扩展管理页")
async def open_extensions_page_endpoint(payload: dict):
    browser = str(payload.get("browser") or "chrome")
    return {"status": "ok", **await _run(open_extensions_page, browser)}


@manage_router.post("/open-platform", summary="打开 GPT 平台网站")
async def open_platform_endpoint(payload: dict):
    url = str(payload.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "无效的平台地址")
    return {"status": "ok", **await _run(open_platform_url, url)}


# ---------------- 会话查询与 Markdown 导出（本地前端调用） ----------------

_ROLE_LABELS = {"user": "👤 用户", "assistant": "🤖 助手", "system": "📋 系统"}


def _safe_filename(name: str, max_len: int = 60) -> str:
    """清洗文件名：非法字符替换为 _，去首尾空白与点，超长截断。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name or "").strip()).strip(" .")
    return cleaned[:max_len].strip(" .") or "未命名"


def _conversation_markdown(conv: sqlite3.Row, messages: list[sqlite3.Row]) -> str:
    """会话 → Markdown 文本（格式对齐原扩展的 md 导出：标题/统计/按序消息/分隔线）。"""
    meta = PLATFORM_META.get(conv["platform_code"], {})
    platform_name = meta.get("name") or conv["platform_code"]
    lines = [
        f"# {platform_name}对话 - {conv['title'] or '未命名会话'}",
        "",
        f"> 生成时间：{_now_iso()}",
        f"> 消息数：{len(messages)} | 总字数：{conv['char_count'] or 0} | 平台：{platform_name}",
        "",
    ]
    for m in messages:
        role = _ROLE_LABELS.get(str(m["role"] or ""), str(m["role"] or "未知"))
        lines.append(f"## {role}")
        lines.append(f"\n*时间: {m['msg_time']}*\n" if m["msg_time"] else "")
        if m["thinking_content"]:
            quoted = "\n".join(f"> {ln}" for ln in str(m["thinking_content"]).splitlines())
            lines.append(f"> **[思考过程]**\n{quoted}\n")
        lines.append(f"{str(m['content'] or '').strip() or '（无内容）'}\n")
        lines.append("---\n")
    return "\n".join(lines).rstrip() + "\n"


def _md_filename(conv: sqlite3.Row) -> str:
    meta = PLATFORM_META.get(conv["platform_code"], {})
    platform_name = meta.get("name") or conv["platform_code"]
    return f"{_safe_filename(platform_name)}-{_safe_filename(conv['title'] or '未命名会话')}-{conv['id']}.md"


def _content_disposition(filename: str) -> str:
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", filename).strip() or "export.md"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{_url_quote(filename)}'


def _load_conversation(conv_id: int) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    db = _get_db()
    with _DB_LOCK:
        conv = db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        if not conv:
            raise HTTPException(404, "会话不存在")
        messages = db.execute(
            "SELECT * FROM messages WHERE conversation_row_id=? ORDER BY seq", (conv_id,)
        ).fetchall()
    return conv, messages


@manage_router.get("/conversations", summary="已同步会话列表（支持平台筛选/分页）")
async def list_conversations(platform: str = "", limit: int = 50, offset: int = 0):
    platform = (platform or "").strip()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    def query():
        db = _get_db()
        where, params = "", []
        if platform:
            where = "WHERE platform_code=?"
            params.append(platform)
        with _DB_LOCK:
            total = db.execute(f"SELECT COUNT(*) AS c FROM conversations {where}", params).fetchone()["c"]
            rows = db.execute(
                "SELECT id, platform_code, conversation_id, title, message_count, char_count,"
                " first_message_at, last_message_at, received_at"
                f" FROM conversations {where} ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return {
            "total": total,
            "conversations": [
                {
                    **{k: r[k] for k in r.keys()},
                    "platform_name": PLATFORM_META.get(r["platform_code"], {}).get("name", r["platform_code"]),
                }
                for r in rows
            ],
        }

    return await _run(query)


@manage_router.get("/conversations/{conv_id}", summary="会话消息明细")
async def get_conversation(conv_id: int):
    def query():
        conv, messages = _load_conversation(conv_id)
        return {
            "id": conv["id"],
            "platform_code": conv["platform_code"],
            "platform_name": PLATFORM_META.get(conv["platform_code"], {}).get("name", conv["platform_code"]),
            "title": conv["title"],
            "message_count": conv["message_count"],
            "char_count": conv["char_count"],
            "first_message_at": conv["first_message_at"],
            "last_message_at": conv["last_message_at"],
            "received_at": conv["received_at"],
            "messages": [
                {
                    "role": m["role"],
                    "content": m["content"],
                    "thinking_content": m["thinking_content"],
                    "msg_time": m["msg_time"],
                }
                for m in messages
            ],
        }

    return await _run(query)


@manage_router.get("/export", summary="下载会话为 Markdown（单会话 .md，多会话 zip，仅本机数据）")
async def export_conversations(ids: str = "", platform: str = ""):
    """数据全部来自本机数据库（<data_dir>/browser_history），无任何远程拉取。"""
    ids = (ids or "").strip()
    platform = (platform or "").strip()

    def build():
        db = _get_db()
        id_list = [t.strip() for t in ids.split(",") if t.strip().isdigit()] if ids else []
        if ids and not id_list:
            raise HTTPException(400, "ids 参数无效")
        with _DB_LOCK:
            if id_list:
                placeholders = ",".join("?" * len(id_list))
                convs = db.execute(
                    f"SELECT * FROM conversations WHERE id IN ({placeholders}) ORDER BY platform_code, id",
                    id_list,
                ).fetchall()
                if not convs:
                    raise HTTPException(404, "会话不存在")
            else:
                where, params = "", []
                if platform:
                    where = "WHERE platform_code=?"
                    params.append(platform)
                convs = db.execute(
                    f"SELECT * FROM conversations {where} ORDER BY platform_code, id", params
                ).fetchall()
                if not convs:
                    raise HTTPException(404, "暂无可下载的会话")

            if len(convs) == 1:
                conv = convs[0]
                messages = db.execute(
                    "SELECT * FROM messages WHERE conversation_row_id=? ORDER BY seq", (conv["id"],)
                ).fetchall()
                return Response(
                    content=_conversation_markdown(conv, messages),
                    media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": _content_disposition(_md_filename(conv))},
                )

            buf = io.BytesIO()
            used: set[str] = set()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for conv in convs:
                    messages = db.execute(
                        "SELECT * FROM messages WHERE conversation_row_id=? ORDER BY seq", (conv["id"],)
                    ).fetchall()
                    name = _md_filename(conv)
                    base, n = name[:-3], 2
                    while name in used:
                        name = f"{base}_{n}.md"
                        n += 1
                    used.add(name)
                    zf.writestr(name, _conversation_markdown(conv, messages))
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            return Response(
                content=buf.getvalue(),
                media_type="application/zip",
                headers={"Content-Disposition": _content_disposition(f"浏览器对话导出-{len(convs)}个会话-{stamp}.zip")},
            )

    return await _run(build)


# ---------------- 从服务端拉取（按 device_id 设备隔离，仅本机浏览器数据） ----------------

def _local_device_id() -> str | None:
    """本机最近一次上传使用的设备 ID（扩展在本机浏览器生成并持久化）。"""
    db = _get_db()
    with _DB_LOCK:
        row = db.execute(
            "SELECT device_id FROM conversations WHERE device_id IS NOT NULL"
            " ORDER BY received_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return row["device_id"] if row else None


def _remote_settings() -> dict[str, str]:
    from .context_registry import load_settings

    remote = (load_settings().get("browser_history") or {}).get("remote") or {}
    return {
        "server_url": str(remote.get("server_url") or "").strip(),
        "token": str(remote.get("token") or "").strip(),
        "device_id": str(remote.get("device_id") or "").strip(),
    }


def _save_remote_settings(server_url: str, token: str, device_id: str) -> None:
    from .context_registry import load_settings, save_settings

    settings = load_settings()
    remote = settings.setdefault("browser_history", {}).setdefault("remote", {})
    remote["server_url"] = server_url.strip()
    remote["token"] = token.strip()
    remote["device_id"] = device_id.strip()
    save_settings(settings)


def _remote_get(server_url: str, path: str, token: str, params: dict[str, str]) -> dict:
    """调用远程上传服务的 GET 接口（/download、/devices），返回解析后的 JSON。

    兼容原「AI对话导出」上传服务（Node/MySQL）的查询协议：token 与 device_id 走 query。
    """
    base = server_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(400, "服务端地址必须以 http:// 或 https:// 开头")
    if not token:
        raise HTTPException(400, "缺少服务端 token")
    url = f"{base}{path}?{urllib.parse.urlencode({'token': token, **params})}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(502, f"服务端拒绝（HTTP {e.code}）：{detail or e.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HTTPException(502, f"无法连接服务端：{getattr(e, 'reason', e)}") from None
    if not isinstance(data, dict) or not data.get("ok"):
        raise HTTPException(502, "服务端返回异常数据")
    return data


def _pull_from_remote(server_url: str, token: str, device_id: str, platform: str = "") -> dict:
    """从远程服务端拉取指定设备的全部会话并入库（服务端已按 device_id 隔离）。"""
    params = {"device_id": device_id}
    if platform:
        params["platform"] = platform
    data = _remote_get(server_url, "/download", token, params)
    convs = data.get("conversations") or []
    imported = skipped = failed = 0
    for c in convs:
        plat = re.sub(r"_(title|chat)$", "", str(c.get("platform") or ""))
        if plat not in PLATFORM_META:
            failed += 1
            continue
        n = {
            "conversation_id": str(c.get("conversation_id") or ""),
            "title": c.get("title"),
            "platform_created_at": _to_dt(c.get("platform_created_at")),
            "platform_updated_at": _to_dt(c.get("platform_updated_at")),
            "messages": [
                {
                    "message_id": str(m.get("message_id") or ""),
                    "role": m.get("role"),
                    "content": m.get("content"),
                    "thinking_content": m.get("thinking_content"),
                    "msg_time": _to_dt(m.get("msg_time")),
                }
                for m in (c.get("messages") or [])
            ],
        }
        if not n["conversation_id"] or not n["messages"]:
            failed += 1
            continue
        try:
            result = _upsert_conversation(plat, n, device_id=device_id)
        except Exception:  # noqa: BLE001
            logger.exception("[browser_history] 远程会话入库失败 platform=%s conv=%s", plat, n["conversation_id"])
            failed += 1
            continue
        if result.get("skipped"):
            skipped += 1
        else:
            imported += 1
    return {
        "device_id": device_id,
        "server_url": server_url.rstrip("/"),
        "server_total": len(convs),
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
    }


@manage_router.get("/remote/config", summary="服务端拉取配置与本机设备 ID")
async def get_remote_config():
    def query():
        cfg = _remote_settings()
        return {
            "server_url": cfg["server_url"],
            "token": cfg["token"],
            "device_id": cfg["device_id"],
            "local_device_id": _local_device_id(),
        }

    return await _run(query)


@manage_router.post("/remote/config", summary="保存服务端地址 / token / 设备 ID")
async def save_remote_config(payload: dict):
    def persist():
        _save_remote_settings(
            str(payload.get("server_url") or ""),
            str(payload.get("token") or ""),
            str(payload.get("device_id") or ""),
        )
        cfg = _remote_settings()
        return {**cfg, "local_device_id": _local_device_id()}

    return {"status": "ok", **await _run(persist)}


@manage_router.post("/remote/devices", summary="列出服务端已见设备（远程 /devices，便于确认本机设备 ID）")
async def list_remote_devices(payload: dict):
    def call():
        cfg = _remote_settings()
        server_url = str(payload.get("server_url") or cfg["server_url"]).strip()
        token = str(payload.get("token") or cfg["token"]).strip()
        data = _remote_get(server_url, "/devices", token, {})
        return {"devices": data.get("devices") or []}

    return {"status": "ok", **await _run(call)}


@manage_router.post("/remote/pull", summary="从服务端拉取本机设备的会话（设备隔离，仅本机浏览器上传的数据）")
async def pull_remote_conversations(payload: dict):
    payload = payload or {}

    def job():
        cfg = _remote_settings()
        server_url = str(payload.get("server_url") or cfg["server_url"]).strip()
        token = str(payload.get("token") or cfg["token"]).strip()
        device_id = str(payload.get("device_id") or cfg["device_id"] or "").strip()[:64] or _local_device_id() or ""
        if not server_url:
            raise HTTPException(400, "请先配置服务端地址")
        if not token:
            raise HTTPException(400, "请先配置服务端 token")
        if not device_id:
            raise HTTPException(400, "无法确定本机设备 ID：请先在本机浏览器用扩展上传一次，或在配置中指定设备 ID")
        result = _pull_from_remote(server_url, token, device_id, str(payload.get("platform") or "").strip())
        refresh_manifest()
        return result

    return {"status": "ok", **await _run(job)}


async def _run(func, *args):
    import asyncio

    return await asyncio.to_thread(func, *args)
