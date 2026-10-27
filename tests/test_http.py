from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

from qqbot.agent import AgentReply
from qqbot.app import BotApp, OneBotHandler
from qqbot.config import load_config

OWNER = 123456789
BOT = 10000


def make_config(root: Path) -> Path:
    path = root / "config.toml"
    path.write_text(
        f"""
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

[llm]
api_key = "sk-test"

[admin]
secret = "supersecret12345"
""",
        encoding="utf-8",
    )
    return path


class FakeOneBot:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def send_private(self, user_id: int, message) -> None:
        self.sent.append(("private", user_id, message))

    def send_group(self, group_id: int, message) -> None:
        self.sent.append(("group", group_id, message))


class FakeAgent:
    def chat(self, key: str, user_id: int, text: str) -> AgentReply:
        return AgentReply(text="http-pong")


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.gettempdir())
        try:
            probe = base / f".probe-{uuid.uuid4().hex}"
            probe.mkdir()
            probe.rmdir()
        except OSError:
            base = Path.cwd() / ".tmp"
        self.root = base / f"qqbot-http-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.app = BotApp(load_config(make_config(self.root)), logging.getLogger("test"))
        self.app.agent = FakeAgent()  # type: ignore[assignment]
        self.fake = FakeOneBot()
        self.app.onebot = self.fake  # type: ignore[assignment]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), OneBotHandler)
        self.server.app = self.app  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.app.executor.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def post(self, payload: dict, token: str | None = "hook-token"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/onebot",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=5)

    def event(self, text: str) -> dict:
        return {
            "post_type": "message",
            "message_type": "private",
            "self_id": BOT,
            "user_id": OWNER,
            "message_id": int(time.time() * 1000),
            "message": [{"type": "text", "data": {"text": text}}],
        }

    def wait_for_sent(self, timeout: float = 3.0) -> None:
        deadline = time.time() + timeout
        while not self.fake.sent and time.time() < deadline:
            time.sleep(0.05)

    def test_event_gets_reply(self) -> None:
        with self.post(self.event("你好")) as response:
            self.assertEqual(response.status, 200)
        self.wait_for_sent()
        self.assertTrue(self.fake.sent)
        self.assertEqual(self.fake.sent[0][2], "http-pong")

    def test_rejects_bad_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(self.event("你好"), token="wrong")
        self.assertEqual(ctx.exception.code, 401)

    def test_health(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
