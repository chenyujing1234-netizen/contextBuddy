"""微信上下文自动保存服务。

后台常驻线程（模式参考 CHAT_REALTIME_AUTOSYNC），约 1h 一轮：
1. 进程扫描（复用 wechat_detection）
2. 检测当前登录账号 → 解析 db_storage 路径
3. 凭据就绪（key store 或重新采集）→ 落盘 output/databases/<wxid>
4. 启动实时同步；更新 contexts/wechat/manifest.json
状态机：idle → wechat_detected → saving → up_to_date / waiting_wechat / error / disabled
对外错误信息不透出内部处理细节（隐藏解密）。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Any

from .app_paths import get_output_databases_dir
from .chat_realtime_autosync import CHAT_REALTIME_AUTOSYNC
from .context_registry import load_settings, write_manifest
from .key_store import get_account_keys_from_store
from .key_service import get_db_key_workflow
from .logging_config import get_logger
from .routers.decrypt import (
    _acquire_decrypt_account_guards,
    _persist_db_keys,
    _release_decrypt_account_guards,
)
from .wechat_decrypt import decrypt_wechat_databases
from .wechat_detection import (
    _wechat_process_targets,
    detect_current_logged_in_account,
    detect_wechat_installation,
    get_process_list,
)

logger = get_logger(__name__)

# 自动保存轮询间隔：每 1 小时起一轮全量保存；数据新鲜度由实时同步（1s 粒度）保障
_INTERVAL_SECONDS = 3600.0
_STORED_KEY_FAILURE_LIMIT = 2
# 手动保存等待在途轮的上限：一轮含等待实时同步与保存，可能耗时一两分钟
_SYNC_NOW_WAIT_SECONDS = 300.0
# 近期成功窗口：窗口内手动保存直接复用最近一轮结果，避免重复保存
_RECENT_SUCCESS_SKIP_SECONDS = 60.0


def _wechat_running() -> bool:
    """复用 wechat_detection 的进程扫描：Weixin.exe / WeChat.exe。"""
    try:
        targets = _wechat_process_targets()
        return any(
            str(name or "").lower() in targets
            for _, name in get_process_list()
        )
    except Exception:
        logger.exception("[wechat-autosave] 微信进程扫描失败")
        return False


def _resolve_current_account_context() -> dict[str, Any]:
    """检测当前登录账号，解析 {"account", "db_storage_path", "wechat_install_path"}。

    matched_folder 规则与 /api/wechat-detection 一致：
    目录名 wxid_xxxx_yyyy 归属真实 wxid_xxxx。
    """
    account_info = detect_current_logged_in_account()
    wxid = str(account_info.get("current_account") or "").strip()
    if not wxid:
        return {}

    installation = detect_wechat_installation()
    account = ""
    data_dir = ""
    for acc in installation.get("accounts", []):
        name = str(acc.get("account_name") or "")
        if name == wxid or name.startswith(f"{wxid}_"):
            account = name
            data_dir = str(acc.get("data_dir") or "")
            break
    if not account or not data_dir:
        return {}

    db_storage_path = os.path.join(data_dir, "db_storage")
    if not os.path.isdir(db_storage_path):
        return {}
    return {
        "account": account,
        "db_storage_path": db_storage_path,
        "wechat_install_path": installation.get("wechat_install_path"),
    }


def _force_rmtree(path: Path) -> None:
    """尽量删除目录：处理 Windows 只读文件；删除失败仅告警不抛出。"""

    def _on_error(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except Exception:
            logger.warning(
                "[wechat-autosave] 清理目录失败（可能被占用）path=%s", target
            )

    shutil.rmtree(path, onerror=_on_error)


def _replace_structured_dir(staging_data: Path, final_dir: Path) -> None:
    """用暂存目录整体替换 structured_data。

    Windows 下旧目录可能因只读/被占用而删不干净（rmtree ignore_errors 静默失败，
    随后 rename 报 WinError 183）：这里删除时处理只读属性并重试 rename；
    若仍失败则回退为 copytree 合并覆盖，保证本轮数据落盘。
    """
    _force_rmtree(final_dir)
    last_error: OSError | None = None
    for _ in range(3):
        try:
            staging_data.rename(final_dir)
            return
        except OSError as exc:
            last_error = exc
            logger.warning(
                "[wechat-autosave] 替换 structured_data 失败，重试 error=%s", exc
            )
            _force_rmtree(final_dir)
            time.sleep(0.2)
    # 目标目录被其他进程长期占用时 rename 无法成功，退化为合并覆盖
    logger.warning(
        "[wechat-autosave] rename 重试仍失败，回退为合并覆盖 error=%s",
        last_error,
    )
    shutil.copytree(staging_data, final_dir, dirs_exist_ok=True)


class WechatAutosaveService:
    """微信上下文自动保存：常驻线程轮询，异常只记日志不崩线程。"""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._sync_lock = threading.Lock()
        self._state = "idle"
        self._last_error = ""
        self._stored_key_failures = 0
        self._last_success_at = 0.0
        self._last_success_result: dict[str, Any] | None = None

    def get_state(self) -> dict[str, Any]:
        with self._mu:
            return {"state": self._state, "last_error": self._last_error}

    def _set_state(self, state: str, last_error: str = "") -> None:
        with self._mu:
            self._state = state
            self._last_error = str(last_error or "")
        logger.info("[wechat-autosave] state=%s", state)

    def start(self) -> None:
        with self._mu:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="wechat-autosave", daemon=True
            )
            self._thread.start()
        logger.info("[wechat-autosave] started interval_s=%s", _INTERVAL_SECONDS)

    def stop(self) -> None:
        with self._mu:
            thread = self._thread
            self._thread = None
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout=5.0)
        logger.info("[wechat-autosave] stopped")

    def _enabled(self) -> bool:
        return bool(load_settings().get("wechat", {}).get("enabled", True))

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if not self._enabled():
                    self._set_state("disabled")
                elif self._sync_lock.acquire(blocking=False):
                    try:
                        self._run_once()
                    finally:
                        self._sync_lock.release()
            except Exception:
                logger.exception("[wechat-autosave] tick failed")
                self._set_state("error", "内部错误")
            self._stop_event.wait(_INTERVAL_SECONDS)

    def sync_now(self) -> dict[str, Any]:
        """手动触发一轮保存；等待在途轮完成后执行，近期已成功则直接复用结果。

        后台 tick 每小时起一轮、每轮耗时约一两分钟（期间由实时同步保持新鲜），
        立即报"忙"会让用户误以为保存失败，因此这里等待在途轮结束再返回。
        """
        if not self._sync_lock.acquire(timeout=_SYNC_NOW_WAIT_SECONDS):
            return {
                "status": -1,
                "errmsg": "正在保存中，请稍候",
                "state": self.get_state()["state"],
            }
        try:
            if (
                self._last_success_result is not None
                and time.time() - self._last_success_at <= _RECENT_SUCCESS_SKIP_SECONDS
            ):
                result = dict(self._last_success_result)
                result["state"] = self.get_state()["state"]
                return result
            summary = self._run_once()
            result = dict(summary)
            result.setdefault("state", self.get_state()["state"])
            return result
        finally:
            self._sync_lock.release()

    def _run_once(self) -> dict[str, Any]:
        try:
            return self._save_pass()
        except Exception:
            logger.exception("[wechat-autosave] 保存失败")
            self._set_state("error", "本轮保存未完成")
            # 对外错误不透出内部处理细节（隐藏解密）
            return {
                "status": -1,
                "errmsg": "本轮保存未完成，微信保持登录后稍后会自动重试",
                "state": self.get_state()["state"],
            }

    def _save_pass(self) -> dict[str, Any]:
        if not _wechat_running():
            self._set_state("waiting_wechat")
            return {"status": -1, "errmsg": "未检测到微信客户端"}

        context = _resolve_current_account_context()
        account = str(context.get("account") or "")
        db_storage_path = str(context.get("db_storage_path") or "")
        if not account or not db_storage_path:
            self._set_state("waiting_wechat")
            return {"status": -1, "errmsg": "微信已运行，但尚未识别到已登录账号"}

        self._set_state("wechat_detected")

        used_stored_key = False
        key = ""
        stored = get_account_keys_from_store(account)
        if (
            isinstance(stored, dict)
            and str(stored.get("db_key") or "").strip()
            and self._stored_key_failures < _STORED_KEY_FAILURE_LIMIT
        ):
            key = str(stored["db_key"]).strip()
            used_stored_key = True
        if not key:
            key = self._acquire_key(context)

        self._set_state("saving")
        guards = _acquire_decrypt_account_guards([account], reason="wechat-autosave")
        try:
            results = decrypt_wechat_databases(
                db_storage_path=db_storage_path, key=key
            )
        finally:
            _release_decrypt_account_guards(guards, reason="wechat-autosave")

        successful_count = int(results.get("successful_count") or 0)
        if results.get("status") != "success" or successful_count <= 0:
            if used_stored_key:
                self._stored_key_failures += 1
                logger.warning(
                    "[wechat-autosave] 使用已保存凭据保存失败 account=%s failures=%s",
                    account,
                    self._stored_key_failures,
                )
            raise RuntimeError(str(results.get("message") or "本轮保存没有成功数据"))

        _persist_db_keys(results.get("account_results"), key)
        self._stored_key_failures = 0

        try:
            CHAT_REALTIME_AUTOSYNC.start()
        except Exception:
            logger.exception("[wechat-autosave] 启动实时同步失败")

        # 归档内容：structured（默认）每轮保存后生成结构化的聊天 JSON/TXT；
        # databases 仅保存数据库文件（当前行为）。数据库文件是内部处理必需的
        # 中间数据，两种选择下都会保留在账号目录中。
        archive_content = str(
            load_settings().get("wechat", {}).get("archive_content") or "structured"
        )
        structured_summary: dict[str, Any] | None = None
        if archive_content == "structured":
            structured_summary = self._generate_structured_data(account)

        summary = {
            "accounts": 1,
            "account": account,
            "successful_databases": successful_count,
            "archive_content": archive_content,
        }
        if structured_summary is not None:
            summary["structured"] = structured_summary
        manifest = write_manifest(
            "wechat",
            summary=summary,
            data_ref=str(get_output_databases_dir()),
        )
        self._set_state("up_to_date")
        logger.info(
            "[wechat-autosave] saved account=%s databases=%s",
            account,
            successful_count,
        )
        result = {
            "status": 0,
            "errmsg": "ok",
            "state": "up_to_date",
            "summary": manifest["summary"],
            "last_saved_at": manifest["last_saved_at"],
            "archive_content": archive_content,
        }
        if structured_summary is not None:
            result["structured"] = structured_summary
        # 供 sync_now 在近期成功窗口内复用（读写均在 _sync_lock 内，线程安全）
        self._last_success_at = time.time()
        self._last_success_result = dict(result)
        return result

    def _generate_structured_data(self, account: str) -> dict[str, Any]:
        """按“结构化的数据”归档内容生成聊天 JSON/TXT 到账号目录；失败不影响本轮保存。

        复用归档导出的结构化生成逻辑：先写入账号目录下的临时目录，
        成功后整体替换 structured_data，中途失败时旧的结构化数据仍然可用。
        """
        try:
            from .routers.account_archive_export import (
                AccountArchiveExportJob,
                _generate_structured_chat_data,
                _safe_file_name,
            )

            account_dir = get_output_databases_dir() / account
            staging_root = account_dir / "structured_data.tmp"
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)
            staging_root.mkdir(parents=True, exist_ok=True)
            job = AccountArchiveExportJob(
                export_id=f"wechat-autosave:{account}", account=account
            )
            files = _generate_structured_chat_data(
                job=job,
                account_dir=account_dir,
                staging_dir=staging_root,
                account_prefix=_safe_file_name(account, "account"),
            )
            # 生成器会在暂存目录下创建 structured_data/，整体替换到账号目录
            final_dir = account_dir / "structured_data"
            staging_data = staging_root / "structured_data"
            _replace_structured_dir(staging_data, final_dir)
            shutil.rmtree(staging_root, ignore_errors=True)
            summary: dict[str, Any] = {"ok": True, "files": len(files)}
            try:
                index = json.loads(
                    (final_dir / "index.json").read_text(encoding="utf-8")
                )
                chat = index.get("dataTypes", {}).get("chatMessages", {})
                summary["conversations"] = int(chat.get("conversationCount") or 0)
                summary["messages"] = int(chat.get("messageCount") or 0)
            except Exception:
                logger.exception(
                    "[wechat-autosave] 结构化数据索引读取失败 account=%s", account
                )
            logger.info(
                "[wechat-autosave] structured generated account=%s files=%s "
                "conversations=%s messages=%s",
                account,
                len(files),
                summary.get("conversations", 0),
                summary.get("messages", 0),
            )
            return summary
        except Exception:
            logger.exception(
                "[wechat-autosave] 结构化的数据生成失败 account=%s", account
            )
            return {"ok": False}

    def _acquire_key(self, context: dict[str, Any]) -> str:
        result = get_db_key_workflow(
            wechat_install_path=context.get("wechat_install_path"),
            db_storage_path=context.get("db_storage_path"),
        )
        key = str(result.get("db_key") or "").strip()
        if not key:
            raise RuntimeError("本轮凭据采集没有结果")
        return key


WECHAT_AUTOSAVE = WechatAutosaveService()
