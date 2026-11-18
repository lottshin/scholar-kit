"""
doctor.py - 环境自检 (`check`)、PEP 668 隔离虚拟环境引导、沙箱配置修复与安全缓存清理 (`clean-cache`)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cnki import check_cnki_access
from config import get as cfg_get, global_state_dir, workspace_state_dir
from search import USER_AGENT


def _default_output(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


_PROTECTED_DIRS = {"browser-profile", "projects", ".venv"}
_PROTECTED_FILES = {"session.json", "config.json", "cookies.json", ".gitignore"}


def cmd_clean_cache(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    """安全清理缓存目录（严格保护 browser-profile、projects、.venv 与会话/Cookie/配置文件）。"""
    candidate_roots: List[Path] = []
    for d in (global_state_dir(create=False), workspace_state_dir(create=False)):
        if d.exists():
            resolved = d.resolve()
            if resolved not in candidate_roots:
                candidate_roots.append(resolved)

    if not candidate_roots:
        output_fn({"status": "success", "message": "无缓存目录", "deleted": 0, "freed_bytes": 0, "freed_mb": 0.0})
        return

    ttl_days = cfg_get("cache_ttl_days", 30)
    now = datetime.now()
    stats = {"total": 0, "expired": 0, "deleted": 0, "freed_bytes": 0, "kept": 0}

    for cache_root in candidate_roots:
        for root, dirs, files in os.walk(str(cache_root)):
            # 绝不进入登录态 Profile、课题文献库与虚拟环境目录
            dirs[:] = [d for d in dirs if d not in _PROTECTED_DIRS]
            for fname in files:
                fpath = Path(root) / fname
                if fname in _PROTECTED_FILES:
                    stats["kept"] += 1
                    continue

                stats["total"] += 1
                try:
                    fsize = fpath.stat().st_size
                except OSError:
                    continue

                should_delete = bool(args.clean_all)
                if not should_delete and fname.endswith(".json") and ttl_days > 0:
                    try:
                        data = json.loads(fpath.read_text(encoding="utf-8"))
                        ts = data.get("_cached_at", "")
                        if ts:
                            cached_at = datetime.fromisoformat(ts)
                            if now - cached_at > timedelta(days=ttl_days):
                                should_delete = True
                                stats["expired"] += 1
                    except Exception:
                        pass

                if should_delete:
                    if not args.dry_run:
                        try:
                            fpath.unlink()
                            stats["deleted"] += 1
                            stats["freed_bytes"] += fsize
                        except OSError:
                            stats["kept"] += 1
                    else:
                        stats["deleted"] += 1
                        stats["freed_bytes"] += fsize
                else:
                    stats["kept"] += 1

    stats["freed_mb"] = round(stats["freed_bytes"] / 1024 / 1024, 2)
    mode = "dry-run" if args.dry_run else ("全部清理" if args.clean_all else f"TTL>{ttl_days}天")
    output_fn({
        "status": "success",
        "mode": mode,
        "scanned_dirs": [str(p) for p in candidate_roots],
        **stats,
    })


def managed_venv_python() -> Optional[str]:
    """若全局目录下存在托管 .venv，返回其 Python 解释器路径。"""
    venv_dir = global_state_dir(create=False) / ".venv"
    candidates = (
        [venv_dir / "Scripts" / "python.exe"]
        if sys.platform == "win32"
        else [venv_dir / "bin" / "python3", venv_dir / "bin" / "python"]
    )
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _install_package_pep668_safe(pkg_spec: str, requirements_file: Optional[Path] = None) -> tuple[bool, str, Optional[str]]:
    """尝试安装缺失依赖；遇到 PEP 668 (externally-managed-environment) 时自动回退到托管 .venv。"""
    direct = subprocess.run(
        [sys.executable, "-m", "pip", "install", pkg_spec],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if direct.returncode == 0:
        return True, "installed_in_current_python", None

    stderr_low = (direct.stderr or "").lower()
    if "externally-managed-environment" not in stderr_low and "permission" not in stderr_low:
        return False, (direct.stderr or direct.stdout or "pip install failed").strip()[:200], None

    venv_dir = global_state_dir(create=True) / ".venv"
    venv_py = managed_venv_python()
    if not venv_py:
        uv_bin = shutil.which("uv")
        if uv_bin:
            subprocess.run([uv_bin, "venv", str(venv_dir)], capture_output=True, text=True, timeout=60)
        else:
            import venv
            venv.EnvBuilder(with_pip=True).create(str(venv_dir))
        venv_py = managed_venv_python()

    if not venv_py:
        return False, "PEP 668 环境且无法创建托管 .venv", None

    install_cmd = (
        [venv_py, "-m", "pip", "install", "-r", str(requirements_file)]
        if requirements_file and requirements_file.is_file()
        else [venv_py, "-m", "pip", "install", pkg_spec]
    )
    res = subprocess.run(install_cmd, capture_output=True, text=True, timeout=180)
    if res.returncode == 0:
        return True, f"已安装到隔离环境 {venv_py}", venv_py
    return False, (res.stderr or res.stdout or "venv pip install failed").strip()[:200], venv_py


def check_browser(subprocess_mod) -> tuple:
    """检测可用浏览器，返回 (ok: bool, detail: str)"""
    try:
        from cnki.driver import _detect_browser, _find_browser_exe

        browser = _detect_browser()
        exe = _find_browser_exe(browser)
    except Exception as e:
        return False, f"未检测到 Edge/Chrome: {e}"
    if not exe:
        return False, f"已识别 {browser}，但未找到可执行文件"
    try:
        r = subprocess_mod.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        detail = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else f"{browser} ({exe})"
    except Exception:
        detail = f"{browser} ({exe})"
    return True, detail


def check_driver() -> tuple:
    """检测浏览器驱动是否可用，返回 (ok: bool, detail: str)。"""
    try:
        from cnki.driver import _detect_browser, _find_local_driver
        browser = _detect_browser()
    except Exception as e:
        return False, f"无法检测浏览器类型: {e}"

    try:
        from selenium.webdriver.common.selenium_manager import SeleniumManager
        sm_bin = SeleniumManager._get_binary()
        if sm_bin and os.path.isfile(str(sm_bin)):
            browser_arg = "MicrosoftEdge" if browser == "edge" else "chrome"
            result = subprocess.run(
                [str(sm_bin), "--browser", browser_arg, "--offline", "--output", "json"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                try:
                    info = json.loads(result.stdout)
                    driver_path = info.get("result", {}).get("driver_path", "")
                    if driver_path and os.path.isfile(driver_path):
                        return True, f"selenium-manager 找到: {driver_path}"
                except (ValueError, KeyError):
                    pass
    except Exception:
        pass

    local = _find_local_driver(browser)
    if local:
        return True, f"本地驱动: {local}"

    driver_name = "msedgedriver" if browser == "edge" else "chromedriver"
    return False, (
        f"未找到 {driver_name}（Selenium Manager 需联网下载）。"
        "解决：1) 提权获取网络权限 2) 设置 SCHOLAR_DRIVER_PATH 环境变量"
    )


def check_cnki() -> tuple:
    """检测知网连通性，返回 (ok: bool, detail: str)。"""
    try:
        accessible, msg = check_cnki_access()
        if accessible:
            return True, "可访问"
        if msg.startswith("SANDBOX_BLOCKED"):
            return False, "沙盒权限阻止（WinError 10013 等），提权后可能正常"
        return False, msg
    except Exception as e:
        return False, str(e)


def fix_sandbox_network() -> List[str]:
    """检测项目级沙箱配置目录并按需补充网络规则（仅作用于当前工作区配置，除非已存在用户级 Codex 配置）。"""
    fixes = []

    codex_dir = Path.cwd() / ".codex"
    codex_home = Path.home() / ".codex"
    is_codex_env = (
        codex_dir.exists()
        or codex_home.exists()
        or os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED")
    )
    if is_codex_env:
        config_targets = []
        if codex_dir.exists():
            config_targets.append(codex_dir / "config.toml")
        if codex_home.exists() and codex_home != codex_dir:
            config_targets.append(codex_home / "config.toml")
        if not config_targets:
            codex_dir.mkdir(parents=True, exist_ok=True)
            config_targets.append(codex_dir / "config.toml")

        codex_net_config = (
            '\n# Scholar Kit - 知网检索需要网络权限\n'
            'approval_policy = "on-request"\n'
            'sandbox_mode = "workspace-write"\n\n'
            '[sandbox_workspace_write]\n'
            'network_access = true\n'
        )
        for config_path in config_targets:
            try:
                existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
                if "network_access" in existing:
                    continue
                with open(config_path, "a", encoding="utf-8") as f:
                    if existing and not existing.endswith("\n"):
                        f.write("\n")
                    f.write(codex_net_config)
                fixes.append(f"codex: 已写入 {config_path}（network_access = true）")
            except (PermissionError, OSError):
                continue

    claude_dir = Path.cwd() / ".claude"
    if claude_dir.exists():
        settings_path = claude_dir / "settings.json"
        try:
            settings = {}
            if settings_path.exists():
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            domains = settings.setdefault("sandbox", {}).setdefault("network", {}).setdefault("allowedDomains", [])
            if "*.cnki.net" not in domains:
                domains.append("*.cnki.net")
                settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
                fixes.append("claude-code: 已在 .claude/settings.json 添加 *.cnki.net")
        except Exception:
            pass

    return fixes


def fetch_json(url: str, timeout: int = 5) -> Any:
    """轻量 JSON GET，用于只读远程元信息检查。"""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    try:
        import httpx as _httpx
        with _httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            return resp.json()
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))


def check_update(current_version: str, fetch_fn: Callable[..., Any] = fetch_json) -> Optional[Dict[str, Any]]:
    """只读版本对比：本地版本 vs GitHub 最新 Release/Tag（默认缓存 12 小时且 3s 超时，绝不拖慢启动）。"""
    semver_re = re.compile(r"^\d+\.\d+(\.\d+)?$")

    def _version_key(value: str) -> tuple:
        parts = [int(p) for p in value.split(".")]
        return tuple((parts + [0, 0])[:3])

    use_cache = fetch_fn is fetch_json
    cache_file = global_state_dir(create=False) / "cache" / "update_check.json"
    if use_cache and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            ts = cached.get("_cached_at", "")
            latest_cached = cached.get("latest", "")
            current = current_version.removeprefix("v")
            if ts and latest_cached and semver_re.match(current) and semver_re.match(latest_cached):
                if datetime.now() - datetime.fromisoformat(ts) <= timedelta(hours=12):
                    if _version_key(latest_cached) > _version_key(current):
                        return {
                            "update_available": True,
                            "current": current,
                            "latest": latest_cached,
                            "message": f"新版本 {latest_cached} 可用，在 skill 目录执行 git pull 更新",
                        }
                    return {"update_available": False, "current": current, "latest": latest_cached}
        except Exception:
            pass

    repo = "lottshin/scholar-kit"
    urls = [
        f"https://api.github.com/repos/{repo}/releases/latest",
        f"https://api.github.com/repos/{repo}/tags?per_page=1",
    ]

    try:
        latest = None
        for url in urls:
            if latest:
                break
            try:
                data = fetch_fn(url, timeout=3)
                if data is None:
                    continue
                if isinstance(data, list):
                    latest = data[0].get("name", "") if data else ""
                else:
                    latest = data.get("tag_name", "")
            except Exception:
                continue

        if not latest:
            return None

        latest = latest.removeprefix("v")
        if not semver_re.match(latest):
            return None

        if use_cache:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps({"_cached_at": datetime.now().isoformat(), "latest": latest}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass

        current = current_version.removeprefix("v")
        if not semver_re.match(current):
            return None

        if _version_key(latest) > _version_key(current):
            return {
                "update_available": True,
                "current": current,
                "latest": latest,
                "message": f"新版本 {latest} 可用，在 skill 目录执行 git pull 更新",
            }
        return {"update_available": False, "current": current, "latest": latest}
    except Exception:
        return None


def run_check(
    args,
    *,
    version: str,
    check_browser_fn: Callable[..., tuple] = check_browser,
    check_driver_fn: Callable[[], tuple] = check_driver,
    check_cnki_fn: Callable[[], tuple] = check_cnki,
    check_update_fn: Callable[[], Any] = lambda: None,
    output_fn: Callable[[Any], None] = _default_output,
) -> None:
    """环境自检：逐项检查运行条件，输出能力位供 Agent 决策。"""
    fix_mode = getattr(args, "fix", False)
    checks = []
    fixes_applied = []
    venv_python_hint = managed_venv_python()
    req_file = Path(__file__).resolve().parent / "requirements.txt"

    v = sys.version_info
    checks.append({
        "item": "Python",
        "status": "ok" if v >= (3, 9) else "warn" if v >= (3, 8) else "fail",
        "detail": f"{v.major}.{v.minor}.{v.micro}",
    })

    for pkg, import_name in [
        ("selenium", "selenium"),
        ("httpx", "httpx"),
        ("beautifulsoup4", "bs4"),
        ("openpyxl", "openpyxl"),
        ("python-docx", "docx"),
        ("pdfplumber", "pdfplumber"),
    ]:
        try:
            mod = __import__(import_name)
            ver = getattr(mod, "__version__", "?")
            status = "ok"
            detail = ver
            if pkg == "selenium" and ver != "?":
                try:
                    parts = [int(x) for x in ver.split(".")[:2]]
                    if parts < [4, 10]:
                        status = "warn"
                        detail = f"{ver}（需要 >=4.10）"
                except (ValueError, IndexError):
                    pass
            checks.append({"item": pkg, "status": status, "detail": detail})
        except ImportError:
            if pkg in ("httpx", "pdfplumber"):
                fallback_note = "urllib 兜底可用" if pkg == "httpx" else "仅影响 pdf-meta"
                checks.append({"item": pkg, "status": "warn", "detail": f"未安装（{fallback_note}）"})
            elif fix_mode and pkg == "selenium":
                ok, install_msg, created_venv_py = _install_package_pep668_safe("selenium>=4.10", req_file)
                if created_venv_py:
                    venv_python_hint = created_venv_py
                if ok and not created_venv_py:
                    try:
                        mod = __import__(import_name)
                        ver = getattr(mod, "__version__", "?")
                        checks.append({"item": pkg, "status": "ok", "detail": ver})
                        fixes_applied.append(f"selenium: 已自动安装 ({ver})")
                    except ImportError:
                        checks.append({"item": pkg, "status": "warn", "detail": install_msg})
                elif ok and created_venv_py:
                    checks.append({"item": pkg, "status": "warn", "detail": f"已安装至隔离环境: {created_venv_py}"})
                    fixes_applied.append(f"pep668-venv: {install_msg}")
                else:
                    checks.append({"item": pkg, "status": "fail", "detail": f"自动安装失败: {install_msg}"})
            else:
                checks.append({"item": pkg, "status": "fail", "detail": "未安装"})

    encoding = sys.stdout.encoding or "unknown"
    checks.append({
        "item": "终端编码",
        "status": "ok" if "utf" in encoding.lower() else "warn",
        "detail": encoding,
    })

    browser_ok, browser_detail = check_browser_fn(subprocess)
    checks.append({
        "item": "浏览器",
        "status": "ok" if browser_ok else "warn",
        "detail": browser_detail,
    })

    driver_ok, driver_detail = check_driver_fn()
    checks.append({
        "item": "浏览器驱动",
        "status": "ok" if driver_ok else "warn",
        "detail": driver_detail,
    })

    cnki_ok, cnki_detail = check_cnki_fn()
    checks.append({
        "item": "知网连通性",
        "status": "ok" if cnki_ok else "fail",
        "detail": cnki_detail,
    })

    if fix_mode and not cnki_ok:
        sandbox_fixes = fix_sandbox_network()
        if sandbox_fixes:
            fixes_applied.extend(sandbox_fixes)
            cnki_ok, cnki_detail = check_cnki_fn()
            for c in checks:
                if c["item"] == "知网连通性":
                    c["status"] = "ok" if cnki_ok else "fail"
                    c["detail"] = cnki_detail
                    break

    g_dir = global_state_dir(create=False)
    w_dir = workspace_state_dir(create=False)
    if g_dir.exists():
        total = sum(f.stat().st_size for f in g_dir.rglob("*") if f.is_file())
        checks.append({
            "item": "全局状态目录",
            "status": "ok",
            "detail": f"{g_dir} ({round(total / 1024 / 1024, 2)} MB)",
        })
    else:
        checks.append({"item": "全局状态目录", "status": "ok", "detail": f"{g_dir}（尚未创建）"})

    selenium_item = next((c for c in checks if c["item"] == "selenium"), None)
    selenium_ok = selenium_item is not None and selenium_item["status"] != "fail"
    docx_item = next((c for c in checks if c["item"] == "python-docx"), None)
    excel_item = next((c for c in checks if c["item"] == "openpyxl"), None)
    sandbox_blocked = bool(cnki_detail and "沙盒权限阻止" in cnki_detail)
    cnki_feasible = browser_ok and cnki_ok and selenium_ok and driver_ok

    cnki_reasons = []
    if not selenium_ok:
        cnki_reasons.append("selenium 未安装")
    if not browser_ok:
        cnki_reasons.append("未检测到浏览器")
    if not driver_ok:
        cnki_reasons.append("浏览器驱动缺失（需联网下载）")
    if not cnki_ok:
        cnki_reasons.append("知网不可达（沙盒权限阻止）" if sandbox_blocked else "知网不可达")

    is_codex = (
        (Path.cwd() / ".codex").exists()
        or os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED")
        or any(k.startswith("CODEX_") for k in os.environ)
    )

    capabilities: Dict[str, Any] = {
        "cnki_feasible": cnki_feasible,
        "api_sources": True,
        "docx_tools": docx_item is not None and docx_item["status"] == "ok",
        "excel_export": excel_item is not None and excel_item["status"] == "ok",
        "paths": {
            "global_state_dir": str(g_dir),
            "workspace_state_dir": str(w_dir),
        },
        "sources": {
            "cnki": {
                "available": cnki_feasible,
                "features": ["search", "batch_search", "download", "fulltext", "core_filter", "cite_enrich"],
                "recommended_for": ["中文期刊", "硕博学位论文", "核心期刊筛选"],
            },
            "openalex": {
                "available": True,
                "features": ["search", "sort_citations", "sort_date", "sort_quality", "filter_journal", "filter_author", "filter_field", "pagination"],
                "recommended_for": ["英文文献", "高被引论文", "跨学科检索"],
            },
            "semantic_scholar": {
                "available": True,
                "features": ["search", "sort_citations", "sort_date", "citations_network"],
                "recommended_for": ["计算机科学", "生物医学", "引文追踪"],
            },
            "arxiv": {
                "available": True,
                "features": ["search", "sort_date", "fulltext_oa", "pagination"],
                "recommended_for": ["最新预印本", "物理/数学/计算机科学"],
            },
            "nssd": {
                "available": True,
                "features": ["search", "chinese_social_science"],
                "recommended_for": ["中文社科开放文献"],
            },
        },
        "performance": {
            "cache_enabled": True,
            "concurrent_sources": True,
        },
    }

    if venv_python_hint:
        capabilities["recommended_python"] = venv_python_hint

    if not cnki_feasible:
        capabilities["cnki_blocked_reasons"] = cnki_reasons
        capabilities["suggested_sources"] = ["openalex", "semantic", "arxiv", "nssd"]
        capabilities["sources"]["cnki"]["available"] = False

        if sandbox_blocked or (is_codex and not driver_ok):
            capabilities["needs_escalation"] = True
            capabilities["reason_code"] = "browser_or_driver_requires_host_permission"
            capabilities["agent_action"] = "rerun_same_command_with_escalated_permissions"
            py_cmd = venv_python_hint or ("py -3 -X utf8" if sys.platform == "win32" else "python3")
            capabilities["retry_command"] = f"{py_cmd} scripts/literature.py check --fix"
            capabilities["recommended_env"] = {
                "SE_CACHE_PATH": str(g_dir / "selenium-cache"),
                "SCHOLAR_SKIP_NETWORK_CHECK": "1",
            }
            capabilities["fix_note"] = (
                "沙盒限制导致预检/驱动下载失败。"
                "请通过宿主平台的标准授权流程重跑 retry_command（携带 recommended_env）以允许网络与浏览器访问。"
            )
        elif not driver_ok:
            capabilities["fix_note"] = (
                "浏览器驱动缺失。确保有网络连接后运行任意知网命令，"
                "Selenium Manager 会自动下载匹配的驱动，或设置 SCHOLAR_DRIVER_PATH。"
            )
        elif fix_mode and not cnki_ok and fixes_applied:
            capabilities["fix_note"] = "已写入沙箱网络配置但知网仍不可达。可能原因：未连接校园网/VPN 或未运行 auth-cnki"

    update_info = check_update_fn()

    cnki_dom_info = None
    if getattr(args, "cnki_dom", False):
        if cnki_feasible:
            from cnki.selectors import probe_cnki_dom_contracts
            cnki_dom_info = probe_cnki_dom_contracts()
            checks.append({
                "item": "知网高级检索 DOM 契约",
                "status": "ok" if cnki_dom_info.get("status") == "ok" else ("warn" if cnki_dom_info.get("status") == "degraded" else "fail"),
                "detail": (
                    "7/7 核心锚点正常"
                    if cnki_dom_info.get("status") == "ok"
                    else f"异常锚点: {cnki_dom_info.get('failed_contracts')}"
                ),
            })
        else:
            cnki_dom_info = {
                "status": "skipped",
                "message": "当前 cnki_feasible=False，跳过浏览器 DOM 契约巡检",
            }

    all_ok = all(c["status"] != "fail" for c in checks)
    output: Dict[str, Any] = {
        "status": "success" if all_ok else "warning",
        "version": version,
        "capabilities": capabilities,
        "checks": checks,
    }
    if fixes_applied:
        output["fixes_applied"] = fixes_applied
    if cnki_dom_info:
        output["cnki_dom"] = cnki_dom_info
    if update_info:
        output["update"] = update_info
    output_fn(output)
