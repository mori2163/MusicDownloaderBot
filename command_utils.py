"""
外部コマンドの解決と検証ユーティリティ
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from typing import Optional

_PYTHON_COMMAND_MAP = {
    "yt-dlp": "yt_dlp",
    "spotdl": "spotdl",
}


def resolve_command(cmd: list[str]) -> tuple[Optional[list[str]], Optional[str]]:
    """外部コマンドを解決し、実行可能な形式を返す"""
    if not cmd:
        return None, "コマンドが空です"

    executable = cmd[0]
    module_name = _PYTHON_COMMAND_MAP.get(executable)
    if module_name:
        # PATH上の実行ファイルよりも、現在のPython環境のモジュール実行を優先する
        # （環境差分で古いyt-dlp/spotdlが呼ばれる事故を防ぐため）
        if importlib.util.find_spec(module_name) is not None:
            resolved_cmd = [sys.executable, "-m", module_name] + cmd[1:]
        elif shutil.which(executable) is not None:
            resolved_cmd = cmd
        else:
            return None, _missing_command_message(executable)
    else:
        if shutil.which(executable) is None:
            return None, _missing_command_message(executable)
        resolved_cmd = cmd

    node_error = _validate_js_runtime(resolved_cmd)
    if node_error:
        return None, node_error

    return resolved_cmd, None


def _validate_js_runtime(cmd: list[str]) -> Optional[str]:
    if "--js-runtimes" not in cmd:
        return None

    runtime_index = cmd.index("--js-runtimes")
    if runtime_index + 1 >= len(cmd):
        return None

    runtimes = {runtime.strip() for runtime in cmd[runtime_index + 1].split(",")}
    if "node" in runtimes and shutil.which("node") is None:
        return "node が見つかりません。Node.js をインストールして PATH に追加してください。"

    return None


def _missing_command_message(executable: str) -> str:
    if executable == "yt-dlp":
        return (
            "yt-dlp が見つかりません。`uv sync` または `pip install yt-dlp` を実行し、"
            "`yt-dlp --version` が通ることを確認してください。"
        )
    if executable == "spotdl":
        return (
            "spotdl が見つかりません。`uv sync` または `pip install spotdl` を実行し、"
            "`spotdl --version` が通ることを確認してください。"
        )
    return f"コマンドが見つかりません: {executable}"
