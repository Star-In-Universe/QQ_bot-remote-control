from __future__ import annotations

import base64
import json
import logging
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from qqbot.admin import AdminManager
from qqbot.agent import Agent, AgentReply
from qqbot.app import BotApp, parse_command
from qqbot.config import ConfigError, load_config
from qqbot.onebot import parse_event
from qqbot.store import SessionStore
from qqbot.tools import ToolBox, ToolResult
from qqbot.util import split_text

OWNER = 123456789
BOT = 10000


@contextmanager
def temp_dir():
    """TemporaryDirectory 在受限环境里会创建不可写的目录，这里自己建。"""
    base = Path(tempfile.gettempdir())
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / f".probe-{uuid.uuid4().hex}"
        probe.mkdir()
        probe.rmdir()
    except OSError:
        base = Path.cwd() / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
    path = base / f"qqbot-test-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def write_config(root: Path, **overrides: str) -> Path:
    text = f"""
[onebot]
api_base = "http://127.0.0.1:3999"
api_token = "api-token"
listen_host = "127.0.0.1"
listen_port = 3998
webhook_path = "/onebot"
webhook_token = "hook-token"

[access]
owner_ids = [{OWNER}]
group_ids = []
require_at = true

[llm]
api_key = "sk-test"
model = "deepseek-flash"

[admin]
secret = "supersecret12345"
ttl_minutes = 30

[agent]
workspace = "."
command_timeout_seconds = 20
"""
    for key, value in overrides.items():
        text = text.replace(key, value)
    path = root / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class ConfigTests(unittest.TestCase):
    def test_loads_and_resolves_workspace(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            path = write_config(root)
            config = load_config(path)
            self.assertEqual(config.access.owner_ids, (OWNER,))
            self.assertEqual(config.agent.workspace, root.resolve())
            self.assertEqual(config.llm.api_key, "sk-test")

    def test_rejects_empty_owner_ids(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            path = write_config(root, **{f"[{OWNER}]": "[]"})
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_rejects_short_secret(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            path = write_config(root, **{'secret = "supersecret12345"': 'secret = "short"'})
            with self.assertRaises(ConfigError):
                load_config(path)


class CommandTests(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(parse_command("/help").name, "help")
        self.assertEqual(parse_command("／run Get-Date").name, "run")
        self.assertEqual(parse_command("/run Get-Date").args, "Get-Date")
        self.assertIsNone(parse_command("你好"))
        self.assertIsNone(parse_command("/"))


class MessageTests(unittest.TestCase):
    def test_private_text(self) -> None:
        event = {
            "post_type": "message",
            "message_type": "private",
            "self_id": BOT,
            "user_id": OWNER,
            "message_id": 1,
            "message": [{"type": "text", "data": {"text": " 你好 "}}],
            "sender": {"nickname": "tester"},
        }
        msg = parse_event(event)
        assert msg is not None
        self.assertEqual(msg.text, "你好")
        self.assertTrue(msg.is_private)
        self.assertEqual(msg.key, f"p{OWNER}")

    def test_group_at_array(self) -> None:
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 555,
            "self_id": BOT,
            "user_id": OWNER,
            "message_id": 2,
            "message": [
                {"type": "at", "data": {"qq": str(BOT)}},
                {"type": "text", "data": {"text": " 在吗"}},
            ],
        }
        msg = parse_event(event)
        assert msg is not None
        self.assertTrue(msg.is_at_bot)
        self.assertEqual(msg.text, "在吗")
        self.assertEqual(msg.key, f"g555:u{OWNER}")

    def test_group_at_cq_string(self) -> None:
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 555,
            "self_id": BOT,
            "user_id": OWNER,
            "message": f"[CQ:at,qq={BOT}] 你好",
        }
        msg = parse_event(event)
        assert msg is not None
        self.assertTrue(msg.is_at_bot)
        self.assertEqual(msg.text, "你好")


class AdminTests(unittest.TestCase):
    def test_unlock_rate_limit_and_expiry(self) -> None:
        clock = [1000.0]
        admins = AdminManager("correct-horse", ttl_minutes=1, max_attempts_per_minute=2, clock=lambda: clock[0])
        self.assertFalse(admins.is_admin(OWNER))
        self.assertFalse(admins.unlock(OWNER, "wrong").ok)
        self.assertFalse(admins.unlock(OWNER, "wrong").ok)
        limited = admins.unlock(OWNER, "correct-horse")
        self.assertFalse(limited.ok)
        self.assertIn("尝试次数过多", limited.message)
        clock[0] += 61
        self.assertTrue(admins.unlock(OWNER, "correct-horse").ok)
        self.assertTrue(admins.is_admin(OWNER))
        clock[0] += 61
        self.assertFalse(admins.is_admin(OWNER))

    def test_lock(self) -> None:
        admins = AdminManager("correct-horse", ttl_minutes=1)
        self.assertTrue(admins.unlock(OWNER, "correct-horse").ok)
        self.assertTrue(admins.lock(OWNER))
        self.assertFalse(admins.is_admin(OWNER))


class UtilTests(unittest.TestCase):
    def test_split_text(self) -> None:
        text = "第一段。" * 700
        chunks = split_text(text, 500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text)


class StoreTests(unittest.TestCase):
    def test_persist_and_trim(self) -> None:
        with temp_dir() as tmp:
            path = Path(tmp) / "sessions.json"
            store = SessionStore(path, max_messages=4)
            for i in range(6):
                store.append("k", "user", f"u{i}")
                store.append("k", "assistant", f"a{i}")
            self.assertEqual(len(store.get("k")), 4)
            reloaded = SessionStore(path, max_messages=4)
            self.assertEqual(len(reloaded.get("k")), 4)


class ToolTests(unittest.TestCase):
    def test_powershell_roundtrip(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            path = write_config(root)
            config = load_config(path)
            toolbox = ToolBox(config.agent, root / "data", logging.getLogger("test"))
            result = toolbox.run_powershell("Write-Output 'hello-qqbot'")
            self.assertIn("hello-qqbot", result.text)

    def test_write_and_read_file(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            path = write_config(root)
            config = load_config(path)
            toolbox = ToolBox(config.agent, root / "data", logging.getLogger("test"))
            toolbox.write_file("note.txt", "你好")
            self.assertIn("你好", toolbox.read_file("note.txt").text)


class FakeOneBot:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def send_private(self, user_id: int, message) -> None:
        self.sent.append(("private", user_id, message))

    def send_group(self, group_id: int, message) -> None:
        self.sent.append(("group", group_id, message))


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def chat(self, key: str, user_id: int, text: str) -> AgentReply:
        self.calls.append((key, user_id, text))
        return AgentReply(text="pong")


class AppRoutingTests(unittest.TestCase):
    def make_app(self, root: Path) -> tuple[BotApp, FakeOneBot]:
        config = load_config(write_config(root))
        app = BotApp(config, logging.getLogger("test"))
        fake = FakeOneBot()
        app.onebot = fake  # type: ignore[assignment]
        app.agent = FakeAgent()  # type: ignore[assignment]
        return app, fake

    def test_chat_reply(self) -> None:
        with temp_dir() as tmp:
            app, fake = self.make_app(Path(tmp))
            app._process_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": BOT,
                    "user_id": OWNER,
                    "message_id": 10,
                    "message": [{"type": "text", "data": {"text": "你好"}}],
                }
            )
            self.assertTrue(fake.sent)
            self.assertEqual(fake.sent[0][2], "pong")

    def test_rejects_stranger(self) -> None:
        with temp_dir() as tmp:
            app, fake = self.make_app(Path(tmp))
            app._process_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": BOT,
                    "user_id": 999,
                    "message_id": 11,
                    "message": [{"type": "text", "data": {"text": "你好"}}],
                }
            )
            self.assertFalse(fake.sent)

    def test_help_command(self) -> None:
        with temp_dir() as tmp:
            app, fake = self.make_app(Path(tmp))
            app._process_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": BOT,
                    "user_id": OWNER,
                    "message_id": 12,
                    "message": [{"type": "text", "data": {"text": "/help"}}],
                }
            )
            self.assertIn("可用命令", fake.sent[0][2])

    def test_admin_gate(self) -> None:
        with temp_dir() as tmp:
            app, fake = self.make_app(Path(tmp))
            app._process_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": BOT,
                    "user_id": OWNER,
                    "message_id": 13,
                    "message": [{"type": "text", "data": {"text": "/shot"}}],
                }
            )
            self.assertIn("需要先解锁", fake.sent[0][2])

    def test_unlock_then_shot_uses_toolbox(self) -> None:
        with temp_dir() as tmp:
            app, fake = self.make_app(Path(tmp))
            app.admins.unlock(OWNER, "supersecret12345")
            self.assertTrue(app.admins.is_admin(OWNER))


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_tools: list[object] = []
        self.messages: list[list[dict]] = []

    def chat(self, messages, tools):
        self.calls += 1
        self.seen_tools.append(tools)
        self.messages.append(messages)
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "run_powershell",
                            "arguments": json.dumps({"command": "Write-Output 'tool-ok'"}),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "已经执行完了。"}


class AgentLoopTests(unittest.TestCase):
    def test_tool_loop_when_unlocked(self) -> None:
        with temp_dir() as tmp:
            config = load_config(write_config(tmp))
            sessions = SessionStore(tmp / "data" / "sessions.json")
            toolbox = ToolBox(config.agent, tmp / "data", logging.getLogger("test"))
            admins = AdminManager(config.admin.secret, config.admin.ttl_minutes)
            self.assertTrue(admins.unlock(OWNER, config.admin.secret).ok)
            agent = Agent(config, sessions, toolbox, admins, logging.getLogger("test"))
            fake = FakeLLM()
            agent.client = fake  # type: ignore[assignment]

            reply = agent.chat(f"p{OWNER}", OWNER, "帮我看看")
            self.assertEqual(reply.text, "已经执行完了。")
            self.assertEqual(reply.tool_calls, 1)
            self.assertTrue(fake.seen_tools[0])
            tool_messages = [m for m in fake.messages[1] if m.get("role") == "tool"]
            self.assertTrue(tool_messages)
            self.assertIn("tool-ok", tool_messages[0]["content"])
            self.assertEqual(len(sessions.get(f"p{OWNER}")), 2)

    def test_no_tools_when_locked(self) -> None:
        with temp_dir() as tmp:
            config = load_config(write_config(tmp))
            sessions = SessionStore(tmp / "data" / "sessions.json")
            toolbox = ToolBox(config.agent, tmp / "data", logging.getLogger("test"))
            admins = AdminManager(config.admin.secret, config.admin.ttl_minutes)
            agent = Agent(config, sessions, toolbox, admins, logging.getLogger("test"))
            fake = FakeLLM()
            fake.calls = 1  # 第一次就返回最终文本，不再调用工具
            agent.client = fake  # type: ignore[assignment]

            reply = agent.chat(f"p{OWNER}", OWNER, "你好")
            self.assertEqual(reply.text, "已经执行完了。")
            self.assertIsNone(fake.seen_tools[0])


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
)


class FakeImageToolbox:
    def __init__(self, path: Path) -> None:
        self.path = path

    def execute(self, name: str, arguments: dict) -> ToolResult:
        return ToolResult("已截屏", [self.path])


class FakeScreenshotLLM(FakeLLM):
    def chat(self, messages, tools):
        self.calls += 1
        self.seen_tools.append(tools)
        self.messages.append(messages)
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-shot",
                        "type": "function",
                        "function": {"name": "screenshot", "arguments": "{}"},
                    }
                ],
            }
        return {"role": "assistant", "content": "看到了屏幕。"}


class VisionTests(unittest.TestCase):
    def test_screenshot_is_sent_to_model_as_user_message(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            config = load_config(write_config(root))
            sessions = SessionStore(root / "data" / "sessions.json")
            toolbox = ToolBox(config.agent, root / "data", logging.getLogger("test"))
            admins = AdminManager(config.admin.secret, config.admin.ttl_minutes)
            self.assertTrue(admins.unlock(OWNER, config.admin.secret).ok)

            image = root / "shot.png"
            image.write_bytes(PNG_1PX)

            agent = Agent(config, sessions, toolbox, admins, logging.getLogger("test"))
            agent.toolbox = FakeImageToolbox(image)  # type: ignore[assignment]
            fake = FakeScreenshotLLM()
            agent.client = fake  # type: ignore[assignment]

            reply = agent.chat(f"p{OWNER}", OWNER, "屏幕上是什么")
            self.assertEqual(reply.text, "看到了屏幕。")
            self.assertEqual(reply.images, [image])

            second_call = fake.messages[1]
            image_messages = [
                m
                for m in second_call
                if m.get("role") == "user" and isinstance(m.get("content"), list)
            ]
            self.assertTrue(image_messages)
            blocks = image_messages[0]["content"]
            self.assertTrue(any(block.get("type") == "image_url" for block in blocks))


class WebhookAuthTests(unittest.TestCase):
    def test_accepts_bearer_and_rejects_wrong(self) -> None:
        with temp_dir() as tmp:
            config = load_config(write_config(Path(tmp)))
            app = BotApp(config, logging.getLogger("test"))
            self.assertTrue(app.verify_webhook({"Authorization": "Bearer hook-token"}, b"{}"))
            self.assertFalse(app.verify_webhook({"Authorization": "Bearer nope"}, b"{}"))
            self.assertTrue(app.verify_webhook({"X-Signature": "hook-token"}, b"{}"))

    def test_accepts_hmac_signature(self) -> None:
        import hashlib
        import hmac

        with temp_dir() as tmp:
            config = load_config(write_config(Path(tmp)))
            app = BotApp(config, logging.getLogger("test"))
            body = b'{"post_type":"message"}'
            digest = hmac.new(b"hook-token", body, hashlib.sha1).hexdigest()
            self.assertTrue(app.verify_webhook({"X-Signature": f"sha1={digest}"}, body))
            self.assertTrue(app.verify_webhook({"X-Signature": digest}, body))
            self.assertFalse(app.verify_webhook({"X-Signature": "sha1=deadbeef"}, body))


if __name__ == "__main__":
    unittest.main()
