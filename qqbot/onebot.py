from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)((?:,[^\]]*)?)\]")
CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^,\]]+)[^\]]*\]")
CQ_UNESCAPE = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))


class OneBotError(Exception):
    pass


@dataclass
class IncomingMessage:
    self_id: int
    user_id: int
    text: str
    is_private: bool
    group_id: int | None = None
    message_id: int | None = None
    is_at_bot: bool = False
    sender_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def source(self) -> str:
        return f"群 {self.group_id}" if self.group_id is not None else "私聊"

    @property
    def key(self) -> str:
        """同一用户在同一会话里的上下文键。"""
        if self.group_id is not None:
            return f"g{self.group_id}:u{self.user_id}"
        return f"p{self.user_id}"


def _unescape(text: str) -> str:
    for src, dst in CQ_UNESCAPE:
        text = text.replace(src, dst)
    return text


def _segment_text(segments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if segment.get("type") == "text":
            parts.append(str((segment.get("data") or {}).get("text", "")))
    return "".join(parts)


def _strip_cq(text: str) -> str:
    return CQ_RE.sub("", text)


def parse_event(event: dict[str, Any]) -> IncomingMessage | None:
    if event.get("post_type") != "message":
        return None
    message_type = event.get("message_type")
    if message_type not in {"private", "group"}:
        return None

    try:
        self_id = int(event.get("self_id", 0))
        user_id = int(event.get("user_id", 0))
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None

    raw_message = event.get("message")
    text = ""
    at_bot = False

    if isinstance(raw_message, list):
        text = _segment_text(raw_message)
        for segment in raw_message:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") == "at":
                qq = str((segment.get("data") or {}).get("qq", ""))
                if qq == str(self_id):
                    at_bot = True
    elif isinstance(raw_message, str):
        for match in CQ_AT_RE.finditer(raw_message):
            if match.group(1) == str(self_id):
                at_bot = True
        text = _strip_cq(raw_message)
    else:
        text = str(event.get("raw_message") or "")

    text = _unescape(text).strip()

    group_id: int | None = None
    if message_type == "group":
        try:
            group_id = int(event.get("group_id", 0))
        except (TypeError, ValueError):
            return None
        if group_id <= 0:
            return None

    sender = event.get("sender") or {}
    sender_name = str(sender.get("card") or sender.get("nickname") or "")

    return IncomingMessage(
        self_id=self_id,
        user_id=user_id,
        text=text,
        is_private=message_type == "private",
        group_id=group_id,
        message_id=event.get("message_id"),
        is_at_bot=at_bot,
        sender_name=sender_name,
        raw=event,
    )


def text_segment(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "data": {"text": text}}]


def image_segment(path: Path) -> list[dict[str, Any]]:
    import base64

    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return [{"type": "image", "data": {"file": "base64://" + payload}}]


def mixed_segment(text: str, image_paths: list[Path]) -> list[dict[str, Any]]:
    segments = text_segment(text) if text else []
    for path in image_paths:
        segments.extend(image_segment(path))
    return segments


class OneBotClient:
    """通过 OneBot 11 HTTP API 调用 NapCat。"""

    def __init__(self, base_url: str, token: str = "", timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, action: str, **params: Any) -> dict[str, Any]:
        url = f"{self.base_url}/{action}"
        data = json.dumps(params, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        # 本机回环地址不应该走系统代理；否则开了代理的用户会连不上 NapCat。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise OneBotError(f"NapCat HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OneBotError(f"连接 NapCat 失败：{exc}") from exc

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise OneBotError(f"NapCat 返回的不是 JSON：{body[:200]}") from exc

        if payload.get("status") == "failed" or payload.get("retcode", 0) != 0:
            raise OneBotError(f"NapCat 接口 {action} 失败：{payload}")
        return payload

    def send_private(self, user_id: int, message: str | list[dict[str, Any]]) -> dict[str, Any]:
        return self.call("send_private_msg", user_id=user_id, message=message)

    def send_group(self, group_id: int, message: str | list[dict[str, Any]]) -> dict[str, Any]:
        return self.call("send_group_msg", group_id=group_id, message=message)

    def get_login_info(self) -> dict[str, Any]:
        return self.call("get_login_info").get("data") or {}

    def get_status(self) -> dict[str, Any]:
        return self.call("get_status").get("data") or {}
