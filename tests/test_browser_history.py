"""浏览器记录：扩展上传协议、归一化入库、去重检查与统计。"""
import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wechat_decrypt_tool import browser_history as bh


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_TOOL_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("WECHAT_TOOL_OUTPUT_DIR", raising=False)
    return tmp_path


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(bh.upload_router)
    app.include_router(bh.manage_router)
    return TestClient(app, client=("127.0.0.1", 52310), base_url="http://localhost")


def _deepseek_single() -> dict:
    return {
        "data": {
            "biz_data": {
                "chat_session": {"title": "测试会话"},
                "chat_messages": [
                    {"message_id": 1, "role": "USER", "inserted_at": "2021-01-01 10:00:00", "fragments": [{"type": "REQUEST", "content": "你好"}]},
                    {"message_id": 2, "role": "ASSISTANT", "inserted_at": "2021-01-01 10:00:05", "fragments": [{"type": "THINK", "content": "思考中"}, {"type": "RESPONSE", "content": "你好！"}]},
                ],
            }
        }
    }


def _post_upload(c, platform, conversation, conversation_id="c1", title=None, token=bh.UPLOAD_TOKEN, device_id=None):
    payload = {
        "platform": platform,
        "conversation": conversation,
        "conversation_id": conversation_id,
        "title": title,
    }
    if device_id:
        payload["device_id"] = device_id
    return c.post(
        "/upload",
        json=payload,
        headers={"X-Upload-Token": token},
    )


def test_upload_rejects_bad_token(client):
    resp = _post_upload(client, "deepseek", _deepseek_single(), token="wrong")
    assert resp.status_code == 401


def test_upload_deepseek_and_summary(client):
    resp = _post_upload(client, "deepseek", _deepseek_single(), conversation_id="ds-1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["conversation_id"] == "ds-1"
    assert body["message_count"] == 2

    db = bh._get_db()
    rows = db.execute("SELECT * FROM conversations").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "测试会话"
    msgs = db.execute("SELECT * FROM messages ORDER BY seq").fetchall()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["thinking_content"] == "思考中"

    summary = bh._build_summary()
    assert summary["total_conversations"] == 1
    assert summary["total_messages"] == 2
    assert summary["platforms"][0]["code"] == "deepseek"


def test_upload_same_content_skips(client):
    _post_upload(client, "deepseek", _deepseek_single(), conversation_id="ds-1")
    resp = _post_upload(client, "deepseek", _deepseek_single(), conversation_id="ds-1")

    body = resp.json()
    assert body["ok"] is True
    assert body.get("skipped") is True

    db = bh._get_db()
    assert db.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 2


def test_upload_updated_content_replaces_messages(client):
    _post_upload(client, "deepseek", _deepseek_single(), conversation_id="ds-1")
    updated = _deepseek_single()
    msgs = updated["data"]["biz_data"]["chat_messages"]
    msgs.append({"message_id": 3, "role": "USER", "fragments": [{"type": "REQUEST", "content": "再见"}]})
    resp = _post_upload(client, "deepseek", updated, conversation_id="ds-1")

    body = resp.json()
    assert body.get("skipped") is not True
    assert body["message_count"] == 3
    db = bh._get_db()
    assert db.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 3


def test_check_reports_what_needs_upload(client):
    _post_upload(client, "deepseek", _deepseek_single(), conversation_id="ds-1")

    resp = client.post(
        "/check",
        json={
            "platform": "deepseek",
            "items": [
                {"conversation_id": "ds-1", "updated": "2020-01-01T00:00:00", "count": 2},
                {"conversation_id": "ds-new", "updated": "2030-01-01T00:00:00", "count": 5},
            ],
        },
        headers={"X-Upload-Token": bh.UPLOAD_TOKEN},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["need_upload"] == ["ds-new"]
    assert body["skipped_count"] == 1


def test_batch_log_roundtrip(client):
    resp = client.post(
        "/batch-log",
        json={"batch_no": "b1", "platform": "deepseek", "conversation_count": 3, "message_count": 30},
        headers={"X-Upload-Token": bh.UPLOAD_TOKEN},
    )
    assert resp.json()["ok"] is True
    # 幂等
    resp = client.post(
        "/batch-log",
        json={"batch_no": "b1", "platform": "deepseek", "conversation_count": 4, "message_count": 40},
        headers={"X-Upload-Token": bh.UPLOAD_TOKEN},
    )
    assert resp.json()["ok"] is True
    db = bh._get_db()
    row = db.execute("SELECT * FROM export_batches WHERE batch_no='b1'").fetchone()
    assert row["conversation_count"] == 4


def test_normalize_kimi_batch_item():
    conv = {
        "chat_id": "k1",
        "title": "Kimi标题",
        "messages": [
            {"id": "m1", "role": "user", "createTime": 1700000000000, "blocks": [{"text": {"content": "问题"}}]},
            {"id": "m2", "role": "assistant", "createTime": 1700000001000, "blocks": [{"think": {"content": "推理"}}, {"text": {"content": "回答"}}]},
        ],
    }
    n = bh.normalize_conversation("kimi", conv)
    assert n["conversation_id"] == "k1"
    assert n["title"] == "Kimi标题"
    assert [m["role"] for m in n["messages"]] == ["user", "assistant"]
    assert n["messages"][1]["thinking_content"] == "推理"
    assert n["messages"][0]["msg_time"] is not None


def test_normalize_doubao_batch_item():
    conv = {
        "conversation_id": "d1",
        "name": "豆包标题",
        "messages": [
            {"message_id": "a", "user_type": 1, "create_time": 1700000000, "content_type": 1, "content": '{"text":"提问"}'},
            {
                "message_id": "b",
                "user_type": 2,
                "create_time": 1700000001,
                "content_type": 9999,
                "content": json_dumps_if_needed([{"block_type": 10000, "content": {"text_block": {"text": "回答"}}}]),
                "thinking": "思考",
            },
        ],
    }
    n = bh.normalize_conversation("doubao", conv)
    assert n["conversation_id"] == "d1"
    assert [m["role"] for m in n["messages"]] == ["user", "assistant"]
    assert n["messages"][1]["content"] == "回答"
    assert n["messages"][1]["thinking_content"] == "思考"


def json_dumps_if_needed(v):
    import json

    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def test_upload_unknown_platform(client):
    resp = _post_upload(client, "unknown_platform", _deepseek_single())
    assert resp.status_code == 400
    assert "不支持的平台" in resp.json()["error"]


def test_manage_status_endpoint(client):
    _post_upload(client, "deepseek", _deepseek_single(), conversation_id="ds-1")

    resp = client.get("/api/context/browser-history/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["installed"] in (True, False)  # 取决于本机浏览器，不炸即可
    assert body["summary"]["total_conversations"] == 1


def test_detect_extension_by_store_id_in_preferences(tmp_path, monkeypatch):
    """商店正式版固定 ID 出现在扩展配置中即视为已安装（即便 manifest 名称为空）。"""
    import json as _json

    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(
        _json.dumps({"extensions": {"settings": {bh.STORE_EXTENSION_ID: {"path": r"C:\Chrome\Extensions", "manifest": {}}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bh, "_browser_profile_dirs", lambda: [("Chrome", profile)])

    result = bh.detect_extension_install()

    assert result["installed"] is True
    assert result["installations"][0]["extension_id"] == bh.STORE_EXTENSION_ID
    assert result["installations"][0]["name"] == bh.EXTENSION_NAME


def test_detect_extension_by_store_directory(tmp_path, monkeypatch):
    """Preferences 无法解析时，Extensions/<商店版ID>/ 目录存在也能检出（文件系统兜底途径）。"""
    profile = tmp_path / "Default"
    profile.mkdir()
    version_dir = profile / "Extensions" / bh.STORE_EXTENSION_ID / "2026.10.21_0"
    version_dir.mkdir(parents=True)
    (version_dir / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(bh, "_browser_profile_dirs", lambda: [("Chrome", profile)])

    result = bh.detect_extension_install()

    assert result["installed"] is True
    inst = result["installations"][0]
    assert inst["browser"] == "Chrome"
    assert inst["extension_id"] == bh.STORE_EXTENSION_ID
    assert "Extensions" in inst["path"]


def test_detect_extension_by_name_keeps_working(tmp_path, monkeypatch):
    """本地加载副本仍按名称匹配（原有行为不回归）。"""
    import json as _json

    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(
        _json.dumps({"extensions": {"settings": {
            "abcdefg": {"path": r"D:\data\browser_history\extension", "manifest": {"name": "AI对话导出 word/json/md"}}
        }}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bh, "_browser_profile_dirs", lambda: [("Chrome", profile)])

    result = bh.detect_extension_install()

    assert result["installed"] is True
    assert result["installations"][0]["extension_id"] == "abcdefg"


def test_detect_extension_no_match(tmp_path, monkeypatch):
    """无扩展时返回未安装。"""
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(bh, "_browser_profile_dirs", lambda: [("Chrome", profile)])

    result = bh.detect_extension_install()

    assert result["installed"] is False
    assert result["installations"] == []


def test_prepare_extension_creates_patched_copy(client, tmp_path):
    vendor = Path(bh.__file__).parent / "browser_extension_src"
    if not vendor.is_dir():
        pytest.skip("vendor 扩展目录不存在")

    resp = client.post("/api/context/browser-history/extension/prepare")

    assert resp.status_code == 200
    ext_dir = Path(resp.json()["extension_dir"])
    assert (ext_dir / "manifest.json").exists()
    manifest = (ext_dir / "manifest.json").read_text(encoding="utf-8")
    assert "http://127.0.0.1/*" in manifest
    content = (ext_dir / "content.js").read_text(encoding="utf-8")
    assert f"http://127.0.0.1:{bh.BACKEND_PORT}" in content
    assert "114.55.254.123" not in content


def test_exe_from_command_extracts_quoted_path():
    assert bh._exe_from_command('"C:\\Apps\\chrome.exe" --single-argument') == "C:\\Apps\\chrome.exe"
    assert bh._exe_from_command("C:\\Apps\\chrome.exe --flag") == "C:\\Apps\\chrome.exe"
    assert bh._exe_from_command("") == ""


def test_open_extensions_page_launches_found_exe_directly(monkeypatch):
    """找到浏览器时直接以可执行文件 + URL 启动，绝不走系统默认打开（会弹选择应用框）。"""
    launched: list[list[str]] = []
    monkeypatch.setattr(bh, "_candidate_browser_exes", lambda kind: [r"C:\fake\chrome.exe"] if kind == "chrome" else [])
    monkeypatch.setattr(bh, "_is_browser_process_running", lambda kind: False)
    monkeypatch.setattr("subprocess.Popen", lambda args, **kw: launched.append(args))
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", lambda *a, **kw: pytest.fail("不应调用 os.startfile"))

    result = bh.open_extensions_page("chrome")

    assert result["ok"] is True
    assert result["url"] == "chrome://extensions/"
    assert launched == [[r"C:\fake\chrome.exe", "chrome://extensions/"]]


def test_open_extensions_page_without_browser_returns_error(monkeypatch):
    """任何浏览器都找不到时返回引导手动打开的错误信息，不弹系统对话框。"""
    monkeypatch.setattr(bh, "_candidate_browser_exes", lambda kind: [])
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", lambda *a, **kw: pytest.fail("不应调用 os.startfile"))

    result = bh.open_extensions_page("chrome")

    assert result["ok"] is False
    assert "chrome://extensions/" in result["error"]


def test_open_extensions_page_skips_launch_when_browser_running(monkeypatch):
    """浏览器已在运行时不再重复启动，返回 already_running 标记。"""
    monkeypatch.setattr(bh, "_candidate_browser_exes", lambda kind: [r"C:\fake\chrome.exe"] if kind == "chrome" else [])
    monkeypatch.setattr(bh, "_is_browser_process_running", lambda kind: kind == "chrome")
    monkeypatch.setattr("subprocess.Popen", lambda args, **kw: pytest.fail("浏览器已在运行时不应再启动"))

    result = bh.open_extensions_page("chrome")

    assert result["ok"] is True
    assert result["already_running"] is True
    assert result["url"] == "chrome://extensions/"


def test_is_browser_process_running_parses_tasklist(monkeypatch):
    class _FakeCompleted:
        def __init__(self, stdout):
            self.stdout = stdout

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _FakeCompleted('"chrome.exe","1234","Console","1","100 K"'),
    )
    assert bh._is_browser_process_running("chrome") is True

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _FakeCompleted("信息: 没有运行的任务匹配指定标准。"),
    )
    assert bh._is_browser_process_running("chrome") is False


# ---------------- 会话查询与 Markdown 导出 ----------------

def _kimi_conv(chat_id: str, title: str) -> dict:
    return {
        "chat_id": chat_id,
        "title": title,
        "messages": [
            {"id": f"{chat_id}-m1", "role": "user", "createTime": 1700000000000, "blocks": [{"text": {"content": "第一个问题"}}]},
            {"id": f"{chat_id}-m2", "role": "assistant", "createTime": 1700000001000, "blocks": [{"think": {"content": "推理"}}, {"text": {"content": "第一个回答"}}]},
        ],
    }


def _seed_two_conversations(c):
    _post_upload(c, "deepseek", _deepseek_single(), conversation_id="ds-1", title="深度求索会话")
    _post_upload(c, "kimi", _kimi_conv("k1", "Kimi会话"), conversation_id="k1")


def test_list_conversations_filters_and_paginates(client):
    _seed_two_conversations(client)

    resp = client.get("/api/context/browser-history/conversations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {c["platform_code"] for c in body["conversations"]} == {"deepseek", "kimi"}
    assert all(c["platform_name"] for c in body["conversations"])

    resp = client.get("/api/context/browser-history/conversations", params={"platform": "kimi"})
    body = resp.json()
    assert body["total"] == 1
    assert body["conversations"][0]["platform_code"] == "kimi"
    assert body["conversations"][0]["title"] == "Kimi会话"
    assert body["conversations"][0]["message_count"] == 2

    resp = client.get("/api/context/browser-history/conversations", params={"limit": 1, "offset": 1})
    body = resp.json()
    assert body["total"] == 2
    assert len(body["conversations"]) == 1


def test_get_conversation_detail(client):
    _seed_two_conversations(client)
    conv_id = client.get("/api/context/browser-history/conversations", params={"platform": "kimi"}).json()["conversations"][0]["id"]

    resp = client.get(f"/api/context/browser-history/conversations/{conv_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform_name"] == "Kimi"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "第一个问题"
    assert body["messages"][1]["thinking_content"] == "推理"

    resp = client.get("/api/context/browser-history/conversations/99999")
    assert resp.status_code == 404


def test_export_single_conversation_as_markdown(client):
    _seed_two_conversations(client)
    conv_id = client.get("/api/context/browser-history/conversations", params={"platform": "deepseek"}).json()["conversations"][0]["id"]

    resp = client.get("/api/context/browser-history/export", params={"ids": str(conv_id)})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition and "filename*=UTF-8''" in disposition
    text = resp.text
    assert text.startswith("# DeepSeek对话 - 测试会话")
    assert "## 👤 用户" in text
    assert "## 🤖 助手" in text
    assert "你好！" in text
    assert "> **[思考过程]**" in text
    assert "---" in text


def test_export_multiple_conversations_as_zip(client):
    _seed_two_conversations(client)

    resp = client.get("/api/context/browser-history/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        assert len(names) == 2
        assert any(n.startswith("Kimi-") and n.endswith(".md") for n in names)
        assert any(n.startswith("DeepSeek-") and n.endswith(".md") for n in names)
        kimi_md = next(zf.read(n).decode("utf-8") for n in names if n.startswith("Kimi-"))
        assert kimi_md.startswith("# Kimi对话 - Kimi会话")

    # 平台筛选导出：单平台仅一个会话时直接返回 .md
    resp = client.get("/api/context/browser-history/export", params={"platform": "kimi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "Kimi对话" in resp.text


def test_export_rejects_empty_database_and_bad_ids(client):
    resp = client.get("/api/context/browser-history/export")
    assert resp.status_code == 404
    resp = client.get("/api/context/browser-history/export", params={"ids": "abc"})
    assert resp.status_code == 400
    resp = client.get("/api/context/browser-history/export", params={"ids": "12345"})
    assert resp.status_code == 404


# ---------------- 从服务端拉取（按 device_id 设备隔离） ----------------

def test_upload_stores_device_id(client):
    resp = _post_upload(client, "deepseek", _deepseek_single(), device_id="dev-local-1")
    assert resp.status_code == 200 and resp.json()["ok"]

    db = bh._get_db()
    with bh._DB_LOCK:
        row = db.execute("SELECT device_id FROM conversations").fetchone()
    assert row["device_id"] == "dev-local-1"


_LOGIN_PHONE = "13800001111"


def _login(tmp_phone=_LOGIN_PHONE):
    """模拟已登录：写入 account.json（与 auth_service.set_login 同构）。"""
    from wechat_decrypt_tool.auth_service import get_account_path

    get_account_path().write_text(
        json.dumps({"phone": tmp_phone, "logged_in_at": "2026-01-01T00:00:00"}),
        encoding="utf-8",
    )


_REMOTE_DOWNLOAD = {
    "ok": True,
    "phone": _LOGIN_PHONE,
    "total": 3,
    "conversations": [
        {
            "platform": "deepseek",
            "conversation_id": "r-1",
            "title": "远程会话1",
            "user_phone": _LOGIN_PHONE,
            "device_id": "dev-remote",
            "platform_created_at": None,
            "platform_updated_at": "2026-01-02 10:00:00",
            "messages": [
                {"message_id": "m1", "role": "user", "content": "远程问题", "thinking_content": None, "msg_time": "2026-01-02 10:00:00"},
                {"message_id": "m2", "role": "assistant", "content": "远程回答", "thinking_content": "远程推理", "msg_time": "2026-01-02 10:00:05"},
            ],
        },
        {
            "platform": "kimi",
            "conversation_id": "r-2",
            "title": "远程会话2",
            "user_phone": _LOGIN_PHONE,
            "device_id": "dev-remote",
            "platform_created_at": None,
            "platform_updated_at": None,
            "messages": [
                {"message_id": "k1", "role": "user", "content": "Kimi远程问题", "thinking_content": None, "msg_time": None},
            ],
        },
        # 其他手机号的记录：服务端漏返时本地也不得入库
        {
            "platform": "kimi",
            "conversation_id": "r-other",
            "title": "别人手机号的会话",
            "user_phone": "13900002222",
            "device_id": "dev-other",
            "platform_created_at": None,
            "platform_updated_at": None,
            "messages": [
                {"message_id": "o1", "role": "user", "content": "别人的数据", "thinking_content": None, "msg_time": None},
            ],
        },
    ],
}


def test_remote_pull_requires_login(client, monkeypatch):
    """未登录时拒绝拉取。"""
    resp = client.post("/api/context/browser-history/remote/pull", json={})
    assert resp.status_code == 401
    assert "登录" in resp.json()["detail"]


def test_remote_pull_imports_and_dedupes(client, monkeypatch):
    _login()
    seen = {}

    def fake_remote_get(server_url, path, token, params):
        seen["server_url"] = server_url
        seen["token"] = token
        seen["params"] = params
        return _REMOTE_DOWNLOAD

    monkeypatch.setattr(bh, "_remote_get", fake_remote_get)

    resp = client.post("/api/context/browser-history/remote/pull", json={})
    assert resp.status_code == 200, resp.text
    # 服务端地址与 token 直接使用硬编码值，并按登录手机号请求
    assert seen["server_url"] == bh.REMOTE_SERVER_URL
    assert seen["token"] == bh.REMOTE_TOKEN
    assert seen["params"]["phone"] == _LOGIN_PHONE
    body = resp.json()
    assert body["server_total"] == 3
    assert body["imported"] == 2
    assert body["failed"] == 0
    # 其他手机号的会话被本地二次过滤
    assert body["filtered"] == 1

    # 会话进入本地列表，可查看、可导出（与既有展示/下载链路一致）
    resp = client.get("/api/context/browser-history/conversations")
    assert resp.json()["total"] == 2
    conv_id = resp.json()["conversations"][0]["id"]
    resp = client.get(f"/api/context/browser-history/conversations/{conv_id}")
    assert resp.status_code == 200 and resp.json()["messages"]

    # 拉取的数据保留服务端的 device_id；别人的会话未入库
    db = bh._get_db()
    with bh._DB_LOCK:
        rows = db.execute("SELECT conversation_id, device_id FROM conversations").fetchall()
    assert {r["device_id"] for r in rows} == {"dev-remote"}
    assert {r["conversation_id"] for r in rows} == {"r-1", "r-2"}

    # 同一数据再次拉取：内容一致 → 全部跳过
    resp = client.post("/api/context/browser-history/remote/pull", json={})
    body = resp.json()
    assert body["imported"] == 0
    assert body["skipped"] == 2


def test_remote_pull_only_own_phone_records(client, monkeypatch):
    """本地二次校验：服务端即使返回其他手机号的记录也不入库。"""
    _login()
    monkeypatch.setattr(
        bh, "_remote_get",
        lambda *a, **kw: {"ok": True, "conversations": [_REMOTE_DOWNLOAD["conversations"][2]]})
    resp = client.post("/api/context/browser-history/remote/pull", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported"] == 0
    assert body["filtered"] == 1
    db = bh._get_db()
    with bh._DB_LOCK:
        assert db.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"] == 0


def test_remote_get_validates_url_and_token():
    try:
        bh._remote_get("ftp://bad", "/download", "t", {})
        raise AssertionError("应当 400")
    except Exception as e:
        assert getattr(e, "status_code", None) == 400
    try:
        bh._remote_get("http://srv", "/download", "", {})
        raise AssertionError("应当 400")
    except Exception as e:
        assert getattr(e, "status_code", None) == 400
