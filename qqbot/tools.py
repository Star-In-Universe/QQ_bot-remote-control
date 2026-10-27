from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AgentConfig
from .util import atomic_write_text, truncate

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_powershell",
            "description": (
                "在用户这台 Windows 电脑上执行一段 PowerShell 命令，返回标准输出和错误输出。"
                "适合查看系统状态、管理文件、启动程序、排查问题。"
                "执行前先说明你要做什么；不要执行破坏性操作，除非用户明确要求。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 PowerShell 脚本"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "超时秒数，默认 60，最大 120",
                        "minimum": 1,
                        "maximum": 120,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": (
                "截取当前整个屏幕。图片会发给用户；如果开启了 analyze_screenshots，"
                "下一轮对话也会带上这张图供你查看。"
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出某个目录下的文件和子目录（名称、大小、修改时间）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径；相对路径基于工作目录"}
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文本文件的内容（最多约 200KB，超出部分会被截断）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "max_bytes": {
                        "type": "integer",
                        "description": "最多读取的字节数，默认 200000",
                        "minimum": 1,
                        "maximum": 1000000,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把文本写入文件（覆盖原内容；自动创建父目录）。仅在用户明确要求修改文件时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文本"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "获取电脑的主机名、系统版本、CPU、内存、磁盘占用等概要信息。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_target",
            "description": "用系统默认方式打开一个文件、文件夹或网址（例如打开浏览器、播放器、文档）。",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string", "description": "文件路径或 http(s) 网址"}},
                "required": ["target"],
                "additionalProperties": False,
            },
        },
    },
]


class ToolError(Exception):
    pass


@dataclass
class ToolResult:
    text: str
    images: list[Path] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def _ps_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _powershell_exe() -> str:
    return shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"


def _decode_clixml(text: str) -> str:
    """PowerShell 在 stderr 被重定向时会输出 CLIXML，把它还原成可读文本。"""
    import html
    import re

    if "#< CLIXML" not in text and "<Objs" not in text:
        return ""
    parts = re.findall(r'<S S="Error">(.*?)</S>', text, flags=re.S)
    if not parts:
        return ""
    decoded: list[str] = []
    for part in parts:
        part = html.unescape(part)
        part = part.replace("_x000D_", "").replace("_x000A_", "\n").replace("_x0009_", "\t")
        decoded.append(part)
    return "".join(decoded).strip()


def _decode(data: bytes) -> str:
    if not data:
        return ""
    try:
        text = data.decode("utf-8")
        if "\ufffd" not in text:
            return _decode_clixml(text) or text
    except UnicodeDecodeError:
        pass
    for encoding in ("gb18030", "utf-16-le"):
        try:
            text = data.decode(encoding)
            if text:
                return _decode_clixml(text) or text
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


class ToolBox:
    def __init__(self, config: AgentConfig, data_dir: Path, logger) -> None:
        self.config = config
        self.data_dir = data_dir
        self.logger = logger
        self._audit_lock = threading.Lock()
        self._audit_path = data_dir / "audit.jsonl"
        self._shots_dir = data_dir / "screenshots"

    # ---------------- 审计 ----------------

    def _audit(self, record: dict[str, Any]) -> None:
        record = {"ts": datetime.now().isoformat(timespec="seconds"), **record}
        line = json.dumps(record, ensure_ascii=False)
        with self._audit_lock:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                with open(self._audit_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                self.logger.warning("写审计日志失败", exc_info=True)

    # ---------------- 路径与执行 ----------------

    def _resolve(self, raw: str) -> Path:
        if not raw or not raw.strip():
            raise ToolError("路径不能为空。")
        path = Path(os.path.expandvars(os.path.expanduser(raw.strip())))
        if not path.is_absolute():
            path = (self.config.workspace / path).resolve()
        else:
            path = path.resolve()
        return path

    def _run_powershell(self, script: str, timeout: int) -> tuple[int, str, str]:
        prefix = (
            "$ProgressPreference='SilentlyContinue';"
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
            "$OutputEncoding=[System.Text.Encoding]::UTF8;"
        )
        encoded = _ps_encoded(prefix + "\n" + script)
        command = [
            _powershell_exe(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-OutputFormat",
            "Text",
            "-EncodedCommand",
            encoded,
        ]
        cwd = self.config.workspace if self.config.workspace.is_dir() else None
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
            creationflags=CREATE_NO_WINDOW,
        )
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
            )
            try:
                out, err = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                out, err = b"", b""
            return 124, _decode(out), _decode(err) + f"\n[命令超过 {timeout} 秒，已强制结束]"
        return process.returncode or 0, _decode(out), _decode(err)

    def run_powershell(self, command: str, timeout_seconds: int | None = None) -> ToolResult:
        if not command or not command.strip():
            raise ToolError("命令不能为空。")
        if len(command) > 8000:
            raise ToolError("命令太长（超过 8000 字符）。")
        timeout = min(
            max(1, int(timeout_seconds or 60)),
            max(1, self.config.command_timeout_seconds),
        )
        started = time.monotonic()
        code, out, err = self._run_powershell(command, timeout)
        elapsed = round(time.monotonic() - started, 2)

        pieces: list[str] = []
        if out.strip():
            pieces.append(out.strip())
        if err.strip():
            pieces.append("[stderr]\n" + err.strip())
        body = "\n".join(pieces).strip() or "(没有输出)"
        body = truncate(body, self.config.max_output_chars)

        header = f"[exit code: {code}, {elapsed}s]"
        text = f"{header}\n{body}" if code != 0 else body
        self._audit(
            {
                "kind": "tool",
                "tool": "run_powershell",
                "command": truncate(command, 500),
                "exit": code,
                "elapsed": elapsed,
                "ok": code == 0,
            }
        )
        return ToolResult(text=text, meta={"exit": code, "elapsed": elapsed})

    # ---------------- 具体工具 ----------------

    def screenshot(self) -> ToolResult:
        self._shots_dir.mkdir(parents=True, exist_ok=True)
        target = self._shots_dir / f"shot_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
        path_literal = str(target).replace("'", "''")
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
try {{
  Add-Type @"
using System;
using System.Runtime.InteropServices;
public class DpiHelper {{ [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }}
"@
  [DpiHelper]::SetProcessDPIAware() | Out-Null
}} catch {{ }}
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bmp)
$graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bmp.Size)
$codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object {{ $_.MimeType -eq 'image/jpeg' }} | Select-Object -First 1
$params = New-Object System.Drawing.Imaging.EncoderParameters(1)
$params.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter([System.Drawing.Imaging.Encoder]::Quality, 80L)
$bmp.Save('{path_literal}', $codec, $params)
$graphics.Dispose()
$bmp.Dispose()
Write-Output ("{{0}}x{{1}}" -f $bounds.Width, $bounds.Height)
"""
        code, out, err = self._run_powershell(script, timeout=30)
        if code != 0 or not target.exists():
            self._audit({"kind": "tool", "tool": "screenshot", "ok": False, "error": err[:300]})
            raise ToolError(f"截屏失败：{err.strip() or out.strip() or '未知错误'}")
        size = target.stat().st_size
        self._audit({"kind": "tool", "tool": "screenshot", "ok": True, "bytes": size})
        return ToolResult(
            text=f"已截屏：{target.name}（{out.strip() or '?'}，{size // 1024} KB）",
            images=[target],
            meta={"path": str(target), "bytes": size},
        )

    def list_directory(self, path: str) -> ToolResult:
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"目录不存在：{target}")
        if not target.is_dir():
            raise ToolError(f"不是目录：{target}")
        entries = []
        try:
            for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if len(entries) >= 200:
                    entries.append("…（条目过多，仅显示前 200 个）")
                    break
                try:
                    stat = entry.stat()
                    kind = "<DIR>" if entry.is_dir() else f"{stat.st_size:>10}"
                    mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                    entries.append(f"{kind}  {mtime}  {entry.name}")
                except OSError:
                    entries.append(f"???  {entry.name}")
        except PermissionError as exc:
            raise ToolError(f"没有权限读取：{target}") from exc
        body = "\n".join(entries) or "(空目录)"
        self._audit({"kind": "tool", "tool": "list_directory", "path": str(target), "ok": True})
        return ToolResult(text=f"{target}\n{body}")

    def read_file(self, path: str, max_bytes: int = 200_000) -> ToolResult:
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"文件不存在：{target}")
        if not target.is_file():
            raise ToolError(f"不是文件：{target}")
        max_bytes = min(max(1, int(max_bytes)), 1_000_000)
        try:
            raw = target.read_bytes()[:max_bytes]
        except PermissionError as exc:
            raise ToolError(f"没有权限读取：{target}") from exc
        if b"\x00" in raw[:4096]:
            raise ToolError("这是二进制文件，不能按文本读取。可改用 run_powershell 处理。")
        text = _decode(raw)
        note = ""
        if target.stat().st_size > max_bytes:
            note = f"\n…（文件较大，仅读取前 {max_bytes} 字节）"
        self._audit({"kind": "tool", "tool": "read_file", "path": str(target), "ok": True})
        return ToolResult(text=f"{target}\n{text}{note}")

    def write_file(self, path: str, content: str) -> ToolResult:
        target = self._resolve(path)
        if target.exists() and target.is_dir():
            raise ToolError(f"目标是目录，不能写入：{target}")
        try:
            atomic_write_text(target, content)
        except OSError as exc:
            raise ToolError(f"写入失败：{exc}") from exc
        self._audit({"kind": "tool", "tool": "write_file", "path": str(target), "ok": True})
        return ToolResult(text=f"已写入 {target}（{len(content)} 字符）")

    def system_info(self) -> ToolResult:
        script = """
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
"主机名: $($env:COMPUTERNAME)"
"当前用户: $($env:USERDOMAIN)/$($env:USERNAME)"
"系统: $($os.Caption) $($os.Version)"
"启动时间: $($os.LastBootUpTime)"
"CPU: $cpu"
"内存: {0:N1} GB 可用 / {1:N1} GB 总量" -f (($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / 1MB), ($os.TotalVisibleMemorySize / 1MB)
"磁盘:"
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
  "  {0} 可用 {1:N1} GB / 共 {2:N1} GB" -f $_.DeviceID, ($_.FreeSpace / 1GB), ($_.Size / 1GB)
}
"""
        result = self.run_powershell(script, timeout_seconds=30)
        return result

    def open_target(self, target: str) -> ToolResult:
        target = (target or "").strip()
        if not target:
            raise ToolError("目标不能为空。")
        if target.lower().startswith(("http://", "https://")):
            self._open_with_shell(target)
            self._audit({"kind": "tool", "tool": "open_target", "target": target, "ok": True})
            return ToolResult(text=f"已用默认浏览器打开：{target}")
        path = self._resolve(target)
        if not path.exists():
            raise ToolError(f"路径不存在：{path}")
        self._open_with_shell(str(path))
        self._audit({"kind": "tool", "tool": "open_target", "target": str(path), "ok": True})
        return ToolResult(text=f"已打开：{path}")

    @staticmethod
    def _open_with_shell(target: str) -> None:
        if hasattr(os, "startfile"):
            os.startfile(target)  # type: ignore[attr-defined]
        else:  # 其他平台上的兜底，正式部署仍建议 Windows
            import webbrowser

            webbrowser.open(target)

    # ---------------- 分发 ----------------

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            arguments = {}
        if name == "run_powershell":
            return self.run_powershell(
                str(arguments.get("command", "")), arguments.get("timeout_seconds")
            )
        if name == "screenshot":
            return self.screenshot()
        if name == "list_directory":
            return self.list_directory(str(arguments.get("path", "")))
        if name == "read_file":
            return self.read_file(
                str(arguments.get("path", "")), int(arguments.get("max_bytes", 200_000))
            )
        if name == "write_file":
            return self.write_file(
                str(arguments.get("path", "")), str(arguments.get("content", ""))
            )
        if name == "system_info":
            return self.system_info()
        if name == "open_target":
            return self.open_target(str(arguments.get("target", "")))
        raise ToolError(f"未知工具：{name}")
