"""config.py - Scholar Kit 统一配置与路径管理

配置优先级:
  环境变量 > 工作区 ./.scholar-kit/config.json > 全局 ~/.scholar-kit/config.json > 内置默认值

状态目录分层:
  - 全局状态目录 (`global_state_dir`，默认 `~/.scholar-kit/`，支持 `SCHOLAR_HOME` 覆盖):
    存放 `browser-profile/`、`cookies.json`、`cache/`、`fulltext/`、`selenium-cache/` 及全局 `config.json`，
    一次登录/缓存即可跨所有工作目录复用；若处于仅允许写工作区的受限沙盒则自动回退到工作区目录。
  - 工作区状态目录 (`workspace_state_dir`，固定 `./.scholar-kit/`):
    仅存放当前课题会话 `session.json`、`projects/<project>/session.json` 与工作区级 `config.json`，
    并在创建时自动写入 `.scholar-kit/.gitignore`（`*`），防止误提交敏感文件。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_DEFAULTS = {
    "request_interval": 3,
    "cache_ttl_days": 30,
    "mailto": "scholarkit@example.com",
    "save_dir": "./papers",
    "browser": "auto",
    "batch_window_size": 4,
    "reuse_browser": 1,
}

_ENV_MAP = {
    "request_interval": "SCHOLAR_REQUEST_INTERVAL",
    "cache_ttl_days": "SCHOLAR_CACHE_TTL_DAYS",
    "mailto": "SCHOLAR_MAILTO",
    "save_dir": "SCHOLAR_SAVE_DIR",
    "browser": "SCHOLAR_BROWSER",
    "batch_window_size": "SCHOLAR_BATCH_WINDOW_SIZE",
    "reuse_browser": "SCHOLAR_REUSE_BROWSER",
}

_INT_KEYS = {"request_interval", "cache_ttl_days", "batch_window_size", "reuse_browser"}
_BOOL_INT_KEYS = {"reuse_browser"}

_loaded: dict[str, Any] | None = None
_loaded_key: tuple[str, str] | None = None


def _ensure_dir_with_gitignore(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        try:
            gi.write_text("*\n", encoding="utf-8")
        except OSError:
            pass
    return d


def workspace_state_dir(create: bool = False) -> Path:
    """当前工作区状态目录 (`./.scholar-kit`)；若当前工作目录不可写（如 `/`），自动回退到全局目录下。"""
    cwd = Path.cwd()
    d = cwd / ".scholar-kit"
    if str(cwd) == "/" or not os.access(str(cwd), os.W_OK):
        fallback_root = Path(os.environ.get("SCHOLAR_HOME", "")).expanduser() if os.environ.get("SCHOLAR_HOME") else (Path.home() / ".scholar-kit")
        d = fallback_root / "default-workspace"
    if create:
        _ensure_dir_with_gitignore(d)
    return d


def _is_writable_dir(target: Path) -> bool:
    try:
        target.mkdir(parents=True, exist_ok=True)
        return os.access(str(target), os.W_OK)
    except OSError:
        return False


def global_state_dir(create: bool = False) -> Path:
    """用户级全局状态目录 (`~/.scholar-kit` 或 `SCHOLAR_HOME`)。

    当检测到 `CODEX_SANDBOX_NETWORK_DISABLED` 等仅允许写工作区的沙盒环境，
    或主目录不可写时，自动降级到 `workspace_state_dir`。
    """
    explicit = os.environ.get("SCHOLAR_HOME", "").strip()
    if explicit:
        d = Path(explicit).expanduser()
        if create:
            try:
                _ensure_dir_with_gitignore(d)
            except OSError:
                return workspace_state_dir(create=True)
        return d

    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED"):
        return workspace_state_dir(create=create)

    home_dir = Path.home() / ".scholar-kit"
    if _is_writable_dir(home_dir):
        if create:
            _ensure_dir_with_gitignore(home_dir)
        return home_dir

    return workspace_state_dir(create=create)


def browser_profile_dir(create: bool = False) -> Path:
    """知网 Chromium Profile 目录（跨工作目录共享登录态）。"""
    d = global_state_dir(create=create) / "browser-profile"
    if create:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return d


def cookie_file(for_write: bool = False) -> Path:
    """知网 Cookie 持久化文件路径（读取时兼容旧版工作区 cookies.json）。"""
    global_cookie = global_state_dir(create=for_write) / "cookies.json"
    if not for_write and not global_cookie.exists():
        legacy_cookie = workspace_state_dir(create=False) / "cookies.json"
        if legacy_cookie.exists():
            return legacy_cookie
    return global_cookie


def cache_dir(subdir: str = "cache", create: bool = False) -> Path:
    """全局缓存子目录（如 `cache`、`fulltext`、`selenium-cache`）。"""
    base = global_state_dir(create=create)
    d = base / subdir if subdir else base
    if create:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return d


def allowed_fulltext_dirs() -> list[Path]:
    """允许读取全文缓存的安全目录列表（兼容全局与旧版工作区目录）。"""
    dirs = []
    for candidate in (
        cache_dir("fulltext", create=False),
        workspace_state_dir(create=False) / "fulltext",
    ):
        try:
            resolved = candidate.resolve()
            if resolved not in dirs:
                dirs.append(resolved)
        except OSError:
            continue
    return dirs


def _config_path() -> Path:
    return workspace_state_dir(create=False) / "config.json"


def _global_config_path() -> Path:
    return global_state_dir(create=False) / "config.json"


def _read_json_config(p: Path) -> dict[str, Any]:
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"[scholar-kit] {p} 解析失败，已忽略: {e}", file=sys.stderr)
    return {}


def _load_file() -> dict[str, Any]:
    merged: dict[str, Any] = {}
    global_cfg_path = _global_config_path()
    local_cfg_path = _config_path()
    if global_cfg_path.resolve() != local_cfg_path.resolve():
        merged.update(_read_json_config(global_cfg_path))
    merged.update(_read_json_config(local_cfg_path))
    return merged


def load() -> dict[str, Any]:
    global _loaded, _loaded_key
    cache_token = (str(Path.cwd()), os.environ.get("SCHOLAR_HOME", ""))
    if _loaded is not None and _loaded_key == cache_token:
        return _loaded

    file_cfg = _load_file()
    result: dict[str, Any] = {}

    for key, default in _DEFAULTS.items():
        env_name = _ENV_MAP.get(key, "")
        env_val = os.environ.get(env_name) if env_name else None

        if env_val is not None:
            if key in _BOOL_INT_KEYS and str(env_val).strip().lower() in ("true", "yes", "on", "false", "no", "off"):
                result[key] = 1 if str(env_val).strip().lower() in ("true", "yes", "on") else 0
            elif key in _INT_KEYS:
                try:
                    result[key] = int(env_val)
                except (TypeError, ValueError):
                    result[key] = default
            else:
                result[key] = env_val
        elif key in file_cfg:
            val = file_cfg[key]
            if key in _BOOL_INT_KEYS and isinstance(val, bool):
                val = 1 if val else 0
            elif key in _INT_KEYS:
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    val = default
            result[key] = val
        else:
            result[key] = default

    _loaded = result
    _loaded_key = cache_token
    return result


def get(key: str, fallback: Any = None) -> Any:
    cfg = load()
    return cfg.get(key, fallback)


def reset():
    """强制重新加载（测试用）"""
    global _loaded, _loaded_key
    _loaded = None
    _loaded_key = None
