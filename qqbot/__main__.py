from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .app import BotApp
from .config import ConfigError, describe, load_config
from .util import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qqbot",
        description="把 DeepSeek 智能体接到 QQ 上，并允许远程操作这台 Windows 电脑。",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.toml",
        help="配置文件路径（默认：config.toml）",
    )
    parser.add_argument("--check", action="store_true", help="只检查配置，不启动服务")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    parser.add_argument("--version", action="version", version=f"qqbot {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"配置错误：\n{exc}", file=sys.stderr)
        print("\n可以参考 config.example.toml 复制一份 config.toml 再修改。", file=sys.stderr)
        return 2

    if args.check:
        print(describe(config))
        print(f"数据目录：{config.data_dir}")
        for warning in config.warnings:
            print(f"提示：{warning}")
        print("配置检查通过。")
        return 0

    logger = setup_logging(args.log_level, config.data_dir / "bot.log")
    logger.debug("启动参数：%s", vars(args))

    app = BotApp(config, logger)
    app.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
