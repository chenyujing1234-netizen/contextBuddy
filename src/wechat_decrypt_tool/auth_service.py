"""手机号 + 验证码登录：验证码签发/校验、登录态持久化、插件账号文件写入。

短信发送接入阿里云短信（参考 weChat_assistance/phone_sms_opt_aliyun.py 的
sign_name / template_code），凭据直接内置在代码中。

- WECHAT_TOOL_AUTH_DEBUG=1           本地联调：不发短信，直接把验证码返回/写日志

账号文件落盘策略：
- 应用登录态：<data_dir>/account.json
- 浏览器插件读取：<data_dir>/browser_history/extension/account.json（Chrome 根目录）
  以及 <data_dir>/browser_history/extension/firefox/account.json（Firefox）
具体的目录/文件/格式见 docs/浏览器插件账号文件说明.md。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .app_paths import get_data_dir
from .logging_config import get_logger

logger = get_logger(__name__)

# ---- 阿里云短信配置（参考 weChat_assistance/phone_sms_opt_aliyun.py）----
# 凭据放在本地私有文件 _sms_secret.py（已 .gitignore），打包时随模块一起内置，
# 终端用户无需任何配置；缺少该文件时短信发送返回「未配置」。
SMS_SIGN_NAME = "哈希流光"
SMS_TEMPLATE_CODE = "SMS_495955218"
try:
    from ._sms_secret import SMS_ACCESS_KEY_ID, SMS_ACCESS_KEY_SECRET  # type: ignore
except Exception:  # pragma: no cover - 本机私有文件缺失
    SMS_ACCESS_KEY_ID = ""
    SMS_ACCESS_KEY_SECRET = ""
AUTH_DEBUG = os.environ.get("WECHAT_TOOL_AUTH_DEBUG", "") == "1"

# 验证码有效期（秒）与单号重发冷却（秒），与参考实现（4 位数字 / 60s）一致
CODE_TTL_SECONDS = 60
CODE_RESEND_COOLDOWN = 60

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

_code_lock = threading.Lock()
_codes: dict[str, dict] = {}


def _now() -> float:
    return time.time()


# ---------------- 登录态持久化 ----------------

def get_account_path() -> Path:
    return get_data_dir() / "account.json"


def load_account() -> dict:
    try:
        data = json.loads(get_account_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("[auth] account.json 读取失败")
        return {}


def _write_account(account: dict) -> None:
    path = get_account_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _extension_account_paths() -> list[Path]:
    from .browser_history import extension_copy_dir

    root = extension_copy_dir()
    return [root / "account.json", root / "firefox" / "account.json"]


def write_extension_account_files(phone: str) -> None:
    """把手机号写入浏览器插件安装目录的账号文件（Chrome 根目录 + Firefox 子目录）。

    文件名与结构约定以 docs/浏览器插件账号文件说明.md 为准。
    """
    payload = {"phone": str(phone)}
    for path in _extension_account_paths():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            logger.exception("[auth] 写入插件账号文件失败 path=%s", path)


def set_login(phone: str) -> dict:
    phone = str(phone).strip()
    account = {
        "phone": phone,
        "logged_in_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _write_account(account)
    write_extension_account_files(phone)
    logger.info("[auth] 登录成功 phone=%s", phone)
    return account


def clear_login() -> None:
    try:
        get_account_path().unlink(missing_ok=True)
    except Exception:
        logger.exception("[auth] 删除 account.json 失败")
    for path in _extension_account_paths():
        try:
            path.unlink(missing_ok=True)
        except Exception:
            logger.exception("[auth] 删除插件账号文件失败 path=%s", path)


# ---------------- 验证码签发与校验 ----------------

def _aliyun_send_sms(phone: str, code: str) -> tuple[bool, str]:
    """阿里云短信 Dysmsapi 2017-05-25 SendSms（RPC 签名 V1，仅标准库）。"""
    if not SMS_ACCESS_KEY_ID or not SMS_ACCESS_KEY_SECRET:
        return False, "短信服务未配置（缺少本地短信凭据文件）"

    params: dict[str, str] = {
        "AccessKeyId": SMS_ACCESS_KEY_ID,
        "Action": "SendSms",
        "Format": "JSON",
        "PhoneNumbers": phone,
        "RegionId": "cn-hangzhou",
        "SignName": SMS_SIGN_NAME,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": str(uuid.uuid4()),
        "SignatureVersion": "1.0",
        "TemplateCode": SMS_TEMPLATE_CODE,
        "TemplateParam": json.dumps({"code": str(code)}, ensure_ascii=False),
        "Timestamp": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "Version": "2017-05-25",
    }

    def _pct(value: str) -> str:
        return urllib.parse.quote(str(value), safe="~")

    canonical = "&".join(
        f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items())
    )
    string_to_sign = "GET&%2F&" + _pct(canonical)
    signature = base64.b64encode(
        hmac.new(
            (SMS_ACCESS_KEY_SECRET + "&").encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")
    params["Signature"] = signature

    url = "https://dysmsapi.aliyuncs.com/?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - 短信失败原因需要透出给前端
        logger.exception("[auth] 阿里云短信请求失败")
        return False, f"短信发送请求失败：{e}"

    if data.get("Code") == "OK":
        return True, ""
    return False, str(data.get("Message") or data.get("Code") or "短信发送失败")


def issue_code(phone: str) -> tuple[bool, str, str | None]:
    """签发 4 位验证码并下发短信，返回 (ok, message, debug_code)。"""
    now = _now()
    with _code_lock:
        existing = _codes.get(phone)
        if existing and now - existing.get("sent_at", 0.0) < CODE_RESEND_COOLDOWN:
            remain = int(CODE_RESEND_COOLDOWN - (now - existing["sent_at"]))
            return False, f"验证码已发送，请 {max(remain, 1)} 秒后再试", None

    code = str(random.randint(1000, 9999))
    if not AUTH_DEBUG:
        ok, message = _aliyun_send_sms(phone, code)
        if not ok:
            return False, message, None

    with _code_lock:
        _codes[phone] = {
            "code": code,
            "expires_at": now + CODE_TTL_SECONDS,
            "sent_at": now,
        }
    logger.info("[auth] 验证码已签发 phone=%s debug=%s", phone, AUTH_DEBUG)
    return True, "验证码已发送，请注意查收", (code if AUTH_DEBUG else None)


def verify_code(phone: str, code: str) -> tuple[bool, str]:
    now = _now()
    with _code_lock:
        record = _codes.get(phone)
    if not record:
        return False, "请先获取验证码"
    if now > float(record.get("expires_at", 0.0)):
        with _code_lock:
            _codes.pop(phone, None)
        return False, "验证码已过期，请重新获取"
    if str(code).strip() != str(record.get("code")):
        return False, "验证码不正确"
    with _code_lock:
        _codes.pop(phone, None)
    return True, "登录成功"