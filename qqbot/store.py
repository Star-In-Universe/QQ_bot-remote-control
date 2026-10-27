from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .util import atomic_write_json, read_json


class SessionStore:
    """按会话保存最近若干轮对话，落盘为 JSON，重启后仍能接上上下文。"""

    def __init__(self, path: Path, max_messages: int = 16, max_chars: int = 12000) -> None:
        self.path = path
        self.max_messages = max(2, max_messages)
        self.max_chars = max(1000, max_chars)
        self._lock = threading.RLock()
        data = read_json(path, {})
        self._sessions: dict[str, list[dict[str, str]]] = {}
        if isinstance(data, dict):
            for key, messages in data.items():
                if isinstance(key, str) and isinstance(messages, list):
                    cleaned = [
                        {"role": str(m.get("role")), "content": str(m.get("content"))}
                        for m in messages
                        if isinstance(m, dict)
                        and m.get("role") in {"user", "assistant"}
                        and isinstance(m.get("content"), str)
                    ]
                    if cleaned:
                        self._sessions[key] = cleaned[-self.max_messages :]

    def get(self, key: str) -> list[dict[str, str]]:
        with self._lock:
            return [dict(item) for item in self._sessions.get(key, [])]

    def append(self, key: str, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            return
        content = (content or "").strip()
        if not content:
            return
        with self._lock:
            messages = self._sessions.setdefault(key, [])
            messages.append({"role": role, "content": content[:6000]})
            self._trim(messages)
            self._sessions[key] = messages
            self._prune_keys()
            self._save()

    def clear(self, key: str) -> None:
        with self._lock:
            self._sessions.pop(key, None)
            self._save()

    def _trim(self, messages: list[dict[str, str]]) -> None:
        while len(messages) > self.max_messages:
            messages.pop(0)
        total = sum(len(m["content"]) for m in messages)
        while total > self.max_chars and len(messages) > 2:
            removed = messages.pop(0)
            total -= len(removed["content"])

    def _prune_keys(self, limit: int = 300) -> None:
        if len(self._sessions) <= limit:
            return
        for key in list(self._sessions)[: len(self._sessions) - limit]:
            self._sessions.pop(key, None)

    def _save(self) -> None:
        try:
            atomic_write_json(self.path, self._sessions)
        except OSError:
            # 会话丢失不应导致机器人崩溃，内存里仍然可用。
            pass

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"sessions": len(self._sessions)}
