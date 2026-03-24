"""config.py - Scholar Kit 统一配置加载

优先级: 环境变量 > .scholar-kit/config.json > 内置默认值
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DEFAULTS = {
    "request_interval": 3,
    "cache_ttl_days": 30,
    "mailto": "scholarkit@example.com",
    "save_dir": "./papers",
    "browser": "auto",
    "batch_window_size": 10,
}

_ENV_MAP = {
    "request_interval": "SCHOLAR_REQUEST_INTERVAL",
    "cache_ttl_days": "SCHOLAR_CACHE_TTL_DAYS",
    "mailto": "SCHOLAR_MAILTO",
    "save_dir": "SCHOLAR_SAVE_DIR",
    "browser": "SCHOLAR_BROWSER",
    "batch_window_size": "SCHOLAR_BATCH_WINDOW_SIZE",
}

_INT_KEYS = {"request_interval", "cache_ttl_days", "batch_window_size"}

_loaded: dict[str, Any] | None = None


def _config_path() -> Path:
    return Path.cwd() / ".scholar-kit" / "config.json"


def _load_file() -> dict[str, Any]:
    p = _config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            import sys
            print(f"[scholar-kit] config.json 解析失败，已使用默认值: {e}",
                  file=sys.stderr)
    return {}


def load() -> dict[str, Any]:
    global _loaded
    if _loaded is not None:
        return _loaded

    file_cfg = _load_file()
    result = {}

    for key, default in _DEFAULTS.items():
        env_name = _ENV_MAP.get(key, "")
        env_val = os.environ.get(env_name) if env_name else None

        if env_val is not None:
            if key in _INT_KEYS:
                try:
                    result[key] = int(env_val)
                except (TypeError, ValueError):
                    result[key] = default
            else:
                result[key] = env_val
        elif key in file_cfg:
            val = file_cfg[key]
            if key in _INT_KEYS:
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    val = default
            result[key] = val
        else:
            result[key] = default

    _loaded = result
    return result


def get(key: str, fallback: Any = None) -> Any:
    cfg = load()
    return cfg.get(key, fallback)


def reset():
    """强制重新加载（测试用）"""
    global _loaded
    _loaded = None
