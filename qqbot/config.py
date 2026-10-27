from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLACEHOLDERS = {
    "",
    "REPLACE_ME",
    "CHANGE_ME",
    "REPLACE_WITH_LONG_RANDOM_SECRET",
    "your-token",
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class OneBotConfig:
    api_base: str = "http://127.0.0.1:3000"
    api_token: str = ""
    listen_host: str = "127.0.0.1"
    listen_port: int = 3001
    webhook_path: str = "/onebot"
    webhook_token: str = ""
    request_timeout: int = 30


@dataclass(frozen=True)
class AccessConfig:
    owner_ids: tuple[int, ...] = ()
    group_ids: tuple[int, ...] = ()
    require_at: bool = True


@dataclass(frozen=True)
class LLMConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    max_tokens: int = 4096
    temperature: float = 0.7
    timeout_seconds: int = 180
    max_history_messages: int = 16


@dataclass(frozen=True)
class AdminConfig:
    secret: str = ""
    ttl_minutes: int = 30
    allow_group_admin: bool = False
    max_attempts_per_minute: int = 5


@dataclass(frozen=True)
class AgentConfig:
    workspace: Path = field(default_factory=lambda: Path.cwd())
    max_tool_rounds: int = 8
    command_timeout_seconds: int = 120
    max_output_chars: int = 12000
    min_reply_interval_seconds: float = 1.5
    analyze_screenshots: bool = True


@dataclass(frozen=True)
class Config:
    onebot: OneBotConfig
    access: AccessConfig
    llm: LLMConfig
    admin: AdminConfig
    agent: AgentConfig
    base_dir: Path
    config_path: Path
    warnings: tuple[str, ...] = ()

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"


def _as_int_list(value: Any, name: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{name} 必须是数字数组，例如 [123456789]")
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{name} 里有不是数字的值：{item!r}") from exc
    return tuple(result)


def _pick(value: Any, env: str, default: Any) -> Any:
    """配置值优先，其次环境变量，最后默认值。"""
    if value not in (None, ""):
        return value
    if env and os.environ.get(env):
        return os.environ[env]
    return default


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是一个配置段")
    return value


def load_config(path: str | Path) -> Config:
    config_path = Path(path).expanduser().resolve()
    try:
        with open(config_path, "rb") as handle:
            raw_bytes = handle.read()
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到配置文件：{config_path}") from exc

    # Windows PowerShell 5.1 的 UTF8 编码会带 BOM，tomllib 不接受。
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raw_bytes = raw_bytes[3:]
    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"配置文件格式错误：{exc}") from exc

    base_dir = config_path.parent

    onebot_raw = _section(raw, "onebot")
    access_raw = _section(raw, "access")
    llm_raw = _section(raw, "llm")
    admin_raw = _section(raw, "admin")
    agent_raw = _section(raw, "agent")

    onebot = OneBotConfig(
        api_base=str(onebot_raw.get("api_base", "http://127.0.0.1:3000")).rstrip("/"),
        api_token=str(_pick(onebot_raw.get("api_token"), "QQBOT_ONEBOT_TOKEN", "")),
        listen_host=str(onebot_raw.get("listen_host", "127.0.0.1")),
        listen_port=int(onebot_raw.get("listen_port", 3001)),
        webhook_path=str(onebot_raw.get("webhook_path", "/onebot")),
        webhook_token=str(_pick(onebot_raw.get("webhook_token"), "QQBOT_WEBHOOK_TOKEN", "")),
        request_timeout=int(onebot_raw.get("request_timeout", 30)),
    )

    access = AccessConfig(
        owner_ids=_as_int_list(access_raw.get("owner_ids"), "access.owner_ids"),
        group_ids=_as_int_list(access_raw.get("group_ids"), "access.group_ids"),
        require_at=bool(access_raw.get("require_at", True)),
    )

    llm = LLMConfig(
        api_key=str(_pick(llm_raw.get("api_key"), "DEEPSEEK_API_KEY", "")),
        base_url=str(llm_raw.get("base_url", "https://api.deepseek.com")).rstrip("/"),
        model=str(llm_raw.get("model", "deepseek-flash")),
        max_tokens=int(llm_raw.get("max_tokens", 4096)),
        temperature=float(llm_raw.get("temperature", 0.7)),
        timeout_seconds=int(llm_raw.get("timeout_seconds", 180)),
        max_history_messages=int(llm_raw.get("max_history_messages", 16)),
    )

    admin = AdminConfig(
        secret=str(_pick(admin_raw.get("secret"), "QQBOT_ADMIN_SECRET", "")),
        ttl_minutes=int(admin_raw.get("ttl_minutes", 30)),
        allow_group_admin=bool(admin_raw.get("allow_group_admin", False)),
        max_attempts_per_minute=int(admin_raw.get("max_attempts_per_minute", 5)),
    )

    workspace = Path(str(agent_raw.get("workspace", "."))).expanduser()
    if not workspace.is_absolute():
        workspace = (base_dir / workspace).resolve()

    agent = AgentConfig(
        workspace=workspace,
        max_tool_rounds=int(agent_raw.get("max_tool_rounds", 8)),
        command_timeout_seconds=int(agent_raw.get("command_timeout_seconds", 120)),
        max_output_chars=int(agent_raw.get("max_output_chars", 12000)),
        min_reply_interval_seconds=float(agent_raw.get("min_reply_interval_seconds", 1.5)),
        analyze_screenshots=bool(agent_raw.get("analyze_screenshots", True)),
    )

    config = Config(
        onebot=onebot,
        access=access,
        llm=llm,
        admin=admin,
        agent=agent,
        base_dir=base_dir,
        config_path=config_path,
    )
    warnings = validate_config(config)
    return Config(
        onebot=config.onebot,
        access=config.access,
        llm=config.llm,
        admin=config.admin,
        agent=config.agent,
        base_dir=config.base_dir,
        config_path=config.config_path,
        warnings=tuple(warnings),
    )


def validate_config(config: Config) -> list[str]:
    problems: list[str] = []
    warnings: list[str] = []

    if not config.access.owner_ids:
        problems.append("access.owner_ids 不能为空：必须写明允许使用机器人的 QQ 号。")

    if not config.admin.secret or config.admin.secret in PLACEHOLDERS:
        problems.append("admin.secret 未设置：请换成一串只有你知道的随机字符（至少 12 位）。")
    elif len(config.admin.secret) < 12:
        problems.append("admin.secret 太短：至少 12 位，建议 24 位以上随机字符。")

    if not config.llm.api_key or config.llm.api_key in PLACEHOLDERS:
        problems.append(
            "llm.api_key 未设置：请填写 DeepSeek API Key，或设置环境变量 DEEPSEEK_API_KEY。"
        )

    loopback = config.onebot.listen_host in {"127.0.0.1", "::1", "localhost"}
    if not loopback and not config.onebot.webhook_token:
        problems.append(
            "监听地址不是本机回环地址时必须设置 onebot.webhook_token，否则任何人都能伪造 QQ 消息。"
        )
    if not config.onebot.webhook_token:
        warnings.append("onebot.webhook_token 为空：本机其他程序可以伪造消息，建议设置。")
    if not config.onebot.api_token:
        warnings.append("onebot.api_token 为空：建议在 NapCat 的 HTTP 服务端上设置 Token。")

    if config.admin.ttl_minutes <= 0:
        problems.append("admin.ttl_minutes 必须大于 0。")
    if config.llm.max_history_messages < 2:
        problems.append("llm.max_history_messages 至少为 2。")

    if problems:
        raise ConfigError("配置有问题：\n- " + "\n- ".join(problems))
    return warnings


def describe(config: Config) -> str:
    return (
        f"配置文件：{config.config_path}\n"
        f"监听：{config.onebot.listen_host}:{config.onebot.listen_port}"
        f"{config.onebot.webhook_path}\n"
        f"NapCat API：{config.onebot.api_base}\n"
        f"模型：{config.llm.model}\n"
        f"白名单：{list(config.access.owner_ids)}\n"
        f"工作目录：{config.agent.workspace}"
    )
