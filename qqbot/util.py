from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any


def now() -> float:
    return time.time()


def constant_time_eq(left: str, right: str) -> bool:
    """避免比较密钥时泄露长度/前缀信息。"""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def truncate(text: str, limit: int, note: str = "\n…（输出过长，已截断）") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - len(note))] + note


def split_text(text: str, limit: int = 1200) -> list[str]:
    """把长回复切成 QQ 能接受的若干段，尽量在换行处断开。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind("。")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        else:
            cut += 1
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def b64encode_file(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


def setup_logging(level: str, log_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("qqbot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if log_file is not None:
        try:
            from logging.handlers import RotatingFileHandler

            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except OSError:
            logger.warning("无法写入日志文件 %s，仅输出到控制台", log_file)

    return logger
