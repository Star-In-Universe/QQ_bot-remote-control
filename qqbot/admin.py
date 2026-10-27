from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from .util import constant_time_eq


@dataclass
class UnlockResult:
    ok: bool
    message: str
    remaining_seconds: int = 0


class AdminManager:
    """按 QQ 号管理“高危权限”的临时解锁状态。

    默认锁定；只有提供正确口令才解锁一段时间。进程重启后自动回到锁定状态。
    """

    def __init__(
        self,
        secret: str,
        ttl_minutes: int = 30,
        max_attempts_per_minute: int = 5,
        clock=time.monotonic,
    ) -> None:
        self._secret = secret
        self._ttl_seconds = max(1, ttl_minutes * 60)
        self._max_attempts = max(1, max_attempts_per_minute)
        self._clock = clock
        self._lock = threading.RLock()
        self._expires: dict[int, float] = {}
        self._attempts: dict[int, deque[float]] = {}
        self._unlock_count = 0

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def _prune(self, user_id: int) -> None:
        expiry = self._expires.get(user_id)
        if expiry is not None and expiry <= self._clock():
            self._expires.pop(user_id, None)

    def is_admin(self, user_id: int) -> bool:
        with self._lock:
            self._prune(user_id)
            return user_id in self._expires

    def remaining(self, user_id: int) -> int:
        with self._lock:
            self._prune(user_id)
            expiry = self._expires.get(user_id)
            if expiry is None:
                return 0
            return max(0, int(expiry - self._clock()))

    def unlock(self, user_id: int, supplied: str) -> UnlockResult:
        supplied = (supplied or "").strip()
        with self._lock:
            if not self._secret:
                return UnlockResult(False, "机器人没有配置解锁口令，远程控制已禁用。")

            now = self._clock()
            attempts = self._attempts.setdefault(user_id, deque())
            while attempts and now - attempts[0] > 60:
                attempts.popleft()
            if len(attempts) >= self._max_attempts:
                wait = int(60 - (now - attempts[0])) + 1
                return UnlockResult(False, f"尝试次数过多，请 {wait} 秒后再试。")
            attempts.append(now)

            if not supplied or not constant_time_eq(supplied, self._secret):
                left = self._max_attempts - len(attempts)
                return UnlockResult(False, f"口令不正确。本轮还可尝试 {max(0, left)} 次。")

            self._expires[user_id] = now + self._ttl_seconds
            attempts.clear()
            self._unlock_count += 1
            return UnlockResult(
                True,
                f"已解锁，有效期 {self._ttl_seconds // 60} 分钟。在这段时间内我可以执行命令、截屏、读写文件。",
                self._ttl_seconds,
            )

    def lock(self, user_id: int) -> bool:
        with self._lock:
            return self._expires.pop(user_id, None) is not None

    def lock_all(self) -> None:
        with self._lock:
            self._expires.clear()

    def active_users(self) -> list[int]:
        with self._lock:
            now = self._clock()
            return [uid for uid, exp in list(self._expires.items()) if exp > now]

    @property
    def unlock_count(self) -> int:
        return self._unlock_count
