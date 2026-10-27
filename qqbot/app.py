from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .admin import AdminManager
from .agent import Agent, LLMError
from .config import Config, describe
from .onebot import IncomingMessage, OneBotClient, OneBotError, mixed_segment, parse_event
from .store import SessionStore
from .tools import ToolBox, ToolError
from .util import constant_time_eq, now, split_text, truncate

HELP_TEXT = """可用命令：
/help            显示这份帮助
/status          查看运行状态
/id              查看你的 QQ 号和当前群号
/reset           清空当前会话的上下文
/unlock <口令>   解锁电脑操作权限（默认 30 分钟）
/lock            立即重新锁定
/shot            截取当前屏幕（需解锁）
/sys             查看电脑概况（需解锁）
/run <命令>      直接执行 PowerShell（需解锁）
/power <lock|sleep|restart|shutdown> confirm
                 锁定/睡眠/重启/关机（需解锁，必须带 confirm）

直接发消息就是和 AI 聊天。未解锁时只能聊天，不能操作电脑。"""


@dataclass
class ParsedCommand:
    name: str
    args: str


def parse_command(text: str) -> ParsedCommand | None:
    text = (text or "").strip()
    if not text:
        return None
    if text[0] in {"/", "／"}:
        body = text[1:].strip()
        if not body:
            return None
        parts = body.split(None, 1)
        return ParsedCommand(parts[0].lower(), parts[1].strip() if len(parts) > 1 else "")
    return None


class BotApp:
    def __init__(self, config: Config, logger) -> None:
        self.config = config
        self.logger = logger
        self.started_at = now()
        self.data_dir = config.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.onebot = OneBotClient(
            config.onebot.api_base,
            config.onebot.api_token,
            config.onebot.request_timeout,
        )
        self.admins = AdminManager(
            config.admin.secret,
            config.admin.ttl_minutes,
            config.admin.max_attempts_per_minute,
        )
        self.sessions = SessionStore(
            self.data_dir / "sessions.json",
            config.llm.max_history_messages,
        )
        self.toolbox = ToolBox(config.agent, self.data_dir, logger)
        self.agent = Agent(config, self.sessions, self.toolbox, self.admins, logger)

        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="msg")
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._dedup: OrderedDict[str, float] = OrderedDict()
        self._dedup_lock = threading.Lock()
        self._last_chat: dict[int, float] = {}
        self._chat_guard = threading.Lock()
        self._processed = 0
        self._replied = 0
        self._rejected = 0

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        host = self.config.onebot.listen_host
        port = self.config.onebot.listen_port
        try:
            server = ThreadingHTTPServer((host, port), OneBotHandler)
        except OSError as exc:
            self.logger.error("无法监听 %s:%s：%s", host, port, exc)
            self.logger.error("端口可能被占用；可以修改 config.toml 里的 onebot.listen_port。")
            raise SystemExit(1) from exc
        server.daemon_threads = True
        server.app = self  # type: ignore[attr-defined]
        self.httpd = server

        self.logger.info("=" * 60)
        self.logger.info("QQ 机器人已启动  v%s", __version__)
        for line in describe(self.config).splitlines():
            self.logger.info(line)
        for warning in self.config.warnings:
            self.logger.warning("提示：%s", warning)
        self.logger.info("等待 NapCat 推送消息：http://%s:%s%s", host, port, self.config.onebot.webhook_path)
        self.logger.info("=" * 60)

        try:
            probe = OneBotClient(
                self.config.onebot.api_base,
                self.config.onebot.api_token,
                timeout=5,
            )
            info = probe.get_login_info()
            if info:
                self.logger.info("NapCat 已连接，机器人 QQ：%s（%s）", info.get("user_id"), info.get("nickname"))
        except OneBotError as exc:
            self.logger.warning("暂时连不上 NapCat（启动后仍会继续接收消息）：%s", exc)

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            self.logger.info("收到退出信号，正在停止…")
        finally:
            server.shutdown()
            server.server_close()
            self.executor.shutdown(wait=False, cancel_futures=True)

    # ---------------- 接收与过滤 ----------------

    def verify_webhook(self, headers, body: bytes, path: str = "") -> bool:
        token = self.config.onebot.webhook_token
        if not token:
            return True

        authorization = headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            authorization = authorization[7:]
        candidates = [
            authorization,
            headers.get("X-Signature", ""),
            headers.get("X-Webhook-Token", ""),
            headers.get("X-NapCat-Token", ""),
        ]
        query_token = urllib.parse.parse_qs(urllib.parse.urlparse(path).query).get("token", [""])[0]
        if query_token:
            candidates.append(query_token)

        for candidate in candidates:
            if not candidate:
                continue
            digest = hmac.new(token.encode("utf-8"), body, hashlib.sha1).hexdigest()
            if candidate.startswith("sha1="):
                if constant_time_eq(candidate[5:], digest):
                    return True
                continue
            if constant_time_eq(candidate, token):
                return True
            # 有些实现直接发 40 位十六进制摘要，不带 sha1= 前缀。
            if len(candidate) == 40 and all(ch in "0123456789abcdefABCDEF" for ch in candidate):
                if constant_time_eq(candidate.lower(), digest):
                    return True
        return False

    def submit_event(self, event: dict[str, Any]) -> None:
        message_id = event.get("message_id")
        if not message_id:
            seed = json.dumps(
                [event.get("user_id"), event.get("time"), event.get("message")],
                ensure_ascii=False,
                sort_keys=True,
            )
            message_id = "h" + hashlib.sha1(seed.encode("utf-8")).hexdigest()
        key = str(message_id)
        with self._dedup_lock:
            if key in self._dedup:
                return
            self._dedup[key] = now()
            while len(self._dedup) > 2000:
                self._dedup.popitem(last=False)
        self.executor.submit(self._process_event_safe, event)

    def _process_event_safe(self, event: dict[str, Any]) -> None:
        try:
            self._process_event(event)
        except Exception:  # noqa: BLE001 - 不能让单条消息拖垮服务
            self.logger.exception("处理消息时发生未捕获异常")

    def _process_event(self, event: dict[str, Any]) -> None:
        message = parse_event(event)
        if message is None:
            return
        self._processed += 1

        if message.user_id not in self.config.access.owner_ids:
            self._rejected += 1
            self.logger.info("忽略非白名单消息：user=%s group=%s", message.user_id, message.group_id)
            return

        if message.group_id is not None:
            if message.group_id not in self.config.access.group_ids:
                self.logger.info("忽略未授权群：%s", message.group_id)
                return
            if self.config.access.require_at and not message.is_at_bot:
                return
            if not message.text:
                return

        if not message.text:
            return

        lock = self._lock_for(message.key)
        with lock:
            command = parse_command(message.text)
            try:
                if command:
                    self._handle_command(message, command)
                else:
                    self._handle_chat(message)
            except Exception:  # noqa: BLE001 - 单条消息失败不应影响服务
                self.logger.exception("处理消息失败：user=%s", message.user_id)
                try:
                    self._reply(message, "处理这条消息时出错了，详细信息已经写进 data\\bot.log。")
                except Exception:  # noqa: BLE001
                    pass

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    # ---------------- 发送 ----------------

    def _send_text(self, message: IncomingMessage, text: str) -> None:
        chunks = split_text(text, 1400)
        for index, chunk in enumerate(chunks):
            if message.group_id is not None:
                self.onebot.send_group(message.group_id, chunk)
            else:
                self.onebot.send_private(message.user_id, chunk)
            if index < len(chunks) - 1:
                time.sleep(0.35)
        self._replied += 1

    def _send_images(self, message: IncomingMessage, images) -> None:
        for path in images:
            try:
                payload = mixed_segment("", [path])
                if message.group_id is not None:
                    self.onebot.send_group(message.group_id, payload)
                else:
                    self.onebot.send_private(message.user_id, payload)
                time.sleep(0.3)
            except OneBotError as exc:
                self.logger.warning("发送图片失败：%s", exc)

    def _reply(self, message: IncomingMessage, text: str, images=None) -> None:
        try:
            if text:
                self._send_text(message, text)
            if images:
                self._send_images(message, images)
        except OneBotError as exc:
            self.logger.error("发送消息失败：%s", exc)

    # ---------------- 聊天 ----------------

    def _handle_chat(self, message: IncomingMessage) -> None:
        if not self._allow_rate(message.user_id):
            return

        ack_sent = threading.Event()

        def send_ack() -> None:
            if ack_sent.is_set():
                return
            ack_sent.set()
            try:
                self._send_text(message, "收到，正在处理…")
            except OneBotError:
                pass

        timer = threading.Timer(6.0, send_ack)
        timer.daemon = True
        timer.start()
        try:
            reply = self.agent.chat(message.key, message.user_id, message.text)
        except LLMError as exc:
            timer.cancel()
            self.logger.warning("模型调用失败：%s", exc)
            self._reply(message, f"模型调用失败：{truncate(str(exc), 300)}")
            return
        finally:
            timer.cancel()

        ack_sent.set()
        self._reply(message, reply.text, reply.images)

    def _allow_rate(self, user_id: int) -> bool:
        interval = self.config.agent.min_reply_interval_seconds
        with self._chat_guard:
            last = self._last_chat.get(user_id, 0.0)
            current = now()
            if current - last < interval:
                return False
            self._last_chat[user_id] = current
        return True

    # ---------------- 命令 ----------------

    def _handle_command(self, message: IncomingMessage, command: ParsedCommand) -> None:
        name = command.name
        if name == "help":
            self._reply(message, HELP_TEXT)
            return
        if name in {"id", "whoami"}:
            extra = f"\n群号：{message.group_id}" if message.group_id else ""
            self._reply(message, f"你的 QQ 号：{message.user_id}{extra}")
            return
        if name == "status":
            self._reply(message, self._status_text(message))
            return
        if name == "reset":
            self.sessions.clear(message.key)
            self._reply(message, "已清空当前会话的上下文。")
            return
        if name == "unlock":
            self._handle_unlock(message, command.args)
            return
        if name == "lock":
            changed = self.admins.lock(message.user_id)
            self._reply(message, "已重新锁定。" if changed else "当前本来就处于锁定状态。")
            return
        if name in {"shot", "screenshot"}:
            self._require_admin(message, lambda: self._handle_shot(message))
            return
        if name == "sys":
            self._require_admin(message, lambda: self._handle_sys(message))
            return
        if name == "run":
            self._require_admin(message, lambda: self._handle_run(message, command.args))
            return
        if name == "power":
            self._require_admin(message, lambda: self._handle_power(message, command.args))
            return

        self._reply(message, f"未知命令：/{name}\n发送 /help 查看可用命令。")

    def _require_admin(self, message: IncomingMessage, action) -> None:
        if not self.admins.is_admin(message.user_id):
            self._reply(message, "需要先解锁：发送 /unlock <口令>。")
            return
        if message.group_id is not None and not self.config.admin.allow_group_admin:
            self._reply(message, "出于安全考虑，高危命令只能在私聊里使用。")
            return
        action()

    def _handle_unlock(self, message: IncomingMessage, secret: str) -> None:
        if message.group_id is not None and not self.config.admin.allow_group_admin:
            self._reply(message, "解锁口令只能在私聊里发送。")
            return
        result = self.admins.unlock(message.user_id, secret)
        self._reply(message, result.message)

    def _handle_shot(self, message: IncomingMessage) -> None:
        try:
            result = self.toolbox.screenshot()
        except ToolError as exc:
            self._reply(message, f"截屏失败：{exc}")
            return
        self._reply(message, "当前屏幕：", result.images)

    def _handle_sys(self, message: IncomingMessage) -> None:
        try:
            result = self.toolbox.system_info()
        except ToolError as exc:
            self._reply(message, f"获取系统信息失败：{exc}")
            return
        self._reply(message, result.text)

    def _handle_run(self, message: IncomingMessage, command: str) -> None:
        if not command.strip():
            self._reply(message, "用法：/run <PowerShell 命令>\n例如：/run Get-Date")
            return
        try:
            result = self.toolbox.run_powershell(command, timeout_seconds=60)
        except ToolError as exc:
            self._reply(message, f"执行失败：{exc}")
            return
        self._reply(message, truncate(result.text, 2400))

    def _handle_power(self, message: IncomingMessage, args: str) -> None:
        parts = args.split()
        action = parts[0].lower() if parts else ""
        confirmed = len(parts) >= 2 and parts[1].lower() == "confirm"
        if action not in {"lock", "sleep", "restart", "shutdown"}:
            self._reply(
                message,
                "用法：/power <lock|sleep|restart|shutdown> confirm\n"
                "例如：/power shutdown confirm（5 秒后关机）",
            )
            return
        if not confirmed:
            self._reply(message, f"确认要执行 {action} 吗？请在命令末尾加上 confirm 再发一次。")
            return

        commands = {
            "lock": "rundll32.exe user32.dll,LockWorkStation",
            "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
            "restart": "shutdown /r /t 5 /c \"QQ bot 远程重启\"",
            "shutdown": "shutdown /s /t 5 /c \"QQ bot 远程关机\"",
        }
        labels = {
            "lock": "锁定屏幕",
            "sleep": "进入睡眠",
            "restart": "5 秒后重启",
            "shutdown": "5 秒后关机",
        }
        self._reply(message, f"好的，{labels[action]}。")

        def run_later() -> None:
            time.sleep(1.5)
            try:
                self.toolbox.run_powershell(commands[action], timeout_seconds=15)
            except ToolError as exc:
                self.logger.error("电源操作失败：%s", exc)

        threading.Thread(target=run_later, daemon=True).start()

    def _status_text(self, message: IncomingMessage) -> str:
        uptime = int(now() - self.started_at)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        remaining = self.admins.remaining(message.user_id)
        admin_state = f"已解锁，剩余 {remaining // 60} 分 {remaining % 60} 秒" if remaining else "未解锁"
        return (
            f"运行时间：{hours} 小时 {minutes} 分 {seconds} 秒\n"
            f"模型：{self.config.llm.model}\n"
            f"你的权限：{admin_state}\n"
            f"已处理消息：{self._processed}，已回复：{self._replied}，已拒绝：{self._rejected}\n"
            f"会话数：{self.sessions.stats()['sessions']}"
        )


class OneBotHandler(BaseHTTPRequestHandler):
    server_version = f"qqbot/{__version__}"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        self.server.app.logger.debug("HTTP %s", fmt % args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        if urllib.parse.urlparse(self.path).path == "/health":
            app = self.server.app
            self._send_json(
                200,
                {
                    "ok": True,
                    "version": __version__,
                    "uptime": int(now() - app.started_at),
                    "processed": app._processed,
                    "replied": app._replied,
                },
            )
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        app: BotApp = self.server.app
        path = urllib.parse.urlparse(self.path).path
        if path != app.config.onebot.webhook_path:
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "empty body"})
            return
        if length > 2 * 1024 * 1024:
            self._send_json(413, {"ok": False, "error": "body too large"})
            return

        body = self.rfile.read(length)
        if not app.verify_webhook(self.headers, body, self.path):
            app.logger.warning("拒绝未通过鉴权的上报：%s", self.client_address)
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        try:
            event = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return
        if not isinstance(event, dict):
            self._send_json(400, {"ok": False, "error": "invalid event"})
            return

        app.submit_event(event)
        # OneBot 的 HTTP 上报只要求 2xx；先应答，慢活交给线程池。
        self._send_json(200, {"status": "ok", "retcode": 0})
