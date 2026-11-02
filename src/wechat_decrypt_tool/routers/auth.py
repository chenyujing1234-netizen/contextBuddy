"""手机号 + 验证码登录 API（仅本机访问）。"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth_service import (
    PHONE_RE,
    clear_login,
    issue_code,
    load_account,
    set_login,
    verify_code,
)


def _local_only(request: Request):
    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            raise ValueError()
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError()
        host = request.url.hostname
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError()
    except (ValueError, AttributeError):
        raise HTTPException(403, "登录服务仅允许本机应用访问") from None


router = APIRouter(prefix="/api/auth", dependencies=[Depends(_local_only)])


class SendCodeRequest(BaseModel):
    phone: str


class LoginRequest(BaseModel):
    phone: str
    code: str


@router.get("/status", summary="当前登录状态")
async def auth_status():
    phone = str(load_account().get("phone") or "").strip()
    return {"status": "ok", "logged_in": bool(phone), "phone": phone}


@router.post("/send-code", summary="发送验证码")
async def send_code(payload: SendCodeRequest):
    phone = payload.phone.strip()
    if not PHONE_RE.match(phone):
        raise HTTPException(400, "请输入有效的手机号")
    ok, message, debug_code = issue_code(phone)
    if not ok:
        raise HTTPException(400, message)
    resp: dict = {"status": "ok", "message": message}
    if debug_code:
        resp["debug_code"] = debug_code
    return resp


@router.post("/login", summary="校验验证码并登录")
async def login(payload: LoginRequest):
    phone = payload.phone.strip()
    code = payload.code.strip()
    if not PHONE_RE.match(phone):
        raise HTTPException(400, "请输入有效的手机号")
    if not re.fullmatch(r"\d{4}", code):
        raise HTTPException(400, "请输入4位数字验证码")
    ok, message = verify_code(phone, code)
    if not ok:
        raise HTTPException(400, message)
    set_login(phone)
    return {"status": "ok", "message": "登录成功", "logged_in": True, "phone": phone}


@router.post("/logout", summary="退出登录")
async def logout():
    clear_login()
    return {"status": "ok", "logged_in": False, "phone": ""}