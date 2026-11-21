"""cnki.driver - 浏览器管理、Cookie 持久化、验证码处理"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from .constants import HAS_SELENIUM, REQUEST_INTERVAL, _log

if HAS_SELENIUM:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException,
    )


# ── 浏览器检测与创建 ─────────────────────────────────

def _detect_browser() -> str:
    """检测可用浏览器，优先 Edge。支持 SCHOLAR_BROWSER / config.json 强制指定。"""
    try:
        from config import get as cfg_get
        forced = cfg_get("browser", "auto")
    except ImportError:
        forced = os.environ.get("SCHOLAR_BROWSER", "auto")
    if forced and forced.lower() in ("edge", "chrome"):
        return forced.lower()

    if sys.platform == "win32":
        edge_paths = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        ]
        for p in edge_paths:
            if os.path.exists(p):
                return "edge"
        if shutil.which("chrome") or shutil.which("google-chrome"):
            return "chrome"
    elif sys.platform == "darwin":
        app_bundles = {
            "edge": [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                str(Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ],
            "chrome": [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ],
        }
        for browser_name in ("edge", "chrome"):
            for p in app_bundles[browser_name]:
                if os.path.isfile(p):
                    return browser_name
        if shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable"):
            return "edge"
        if shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser"):
            return "chrome"
    else:
        if shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable"):
            return "edge"
        if shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser"):
            return "chrome"

    raise RuntimeError("未检测到 Edge 或 Chrome 浏览器，请安装其中之一")


_REALISTIC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
)


def _build_user_agent(browser: str = "edge", headless: bool = False) -> Optional[str]:
    """构建与当前操作系统和浏览器一致的 User-Agent。

    有头模式下返回 None，直接使用本机浏览器原生 UA，避免在 macOS/Linux Chrome
    上注入 Windows Edge UA 导致 navigator.platform / userAgentData 指纹冲突。
    无头模式下返回去除 HeadlessChrome 特征且匹配 OS 与浏览器的 UA。
    """
    if not headless:
        return None

    if sys.platform == "darwin":
        os_token = "Macintosh; Intel Mac OS X 10_15_7"
    elif sys.platform == "win32":
        os_token = "Windows NT 10.0; Win64; x64"
    else:
        os_token = "X11; Linux x86_64"

    base = (
        f"Mozilla/5.0 ({os_token}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    if browser == "edge":
        return f"{base} Edg/131.0.0.0"
    return base


def _profile_dir(create: bool = False) -> Path:
    try:
        from config import browser_profile_dir
        return browser_profile_dir(create=create)
    except ImportError:
        d = Path.cwd() / ".scholar-kit" / "browser-profile"
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d


def _cookie_path(for_write: bool = False) -> Path:
    try:
        from config import cookie_file
        return cookie_file(for_write=for_write)
    except ImportError:
        return Path.cwd() / ".scholar-kit" / "cookies.json"


def _save_cookies(driver: "webdriver.Remote"):
    """将当前浏览器 cookies 持久化到状态目录"""
    try:
        p = _cookie_path(for_write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        cookies = driver.get_cookies()
        p.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        _log(f"[cnki] cookie 保存失败: {e}")


def _load_cookies(driver: "webdriver.Remote", *, merge_only: bool = True):
    """从持久化文件加载 cookies 到浏览器（默认 merge_only=True，绝不覆盖浏览器内存中已有的新鲜会话 Cookie）。"""
    p = _cookie_path(for_write=False)
    if not p.exists():
        return
    try:
        existing_names = set()
        if merge_only:
            try:
                existing_names = {c.get("name") for c in (driver.get_cookies() or []) if isinstance(c, dict) and c.get("name")}
            except Exception:
                existing_names = set()
        cookies = json.loads(p.read_text(encoding="utf-8"))
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            if merge_only and cookie.get("name") in existing_names:
                continue
            cookie.pop("sameSite", None)
            try:
                driver.add_cookie(cookie)
            except Exception:
                cookie.pop("expiry", None)
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass
    except Exception as e:
        _log(f"[cnki] cookie 加载失败: {e}")


def _browser_activity_file() -> Path:
    return _profile_dir(create=False).parent / "browser-last-active.txt"


def _touch_browser_activity() -> None:
    """刷新共享浏览器最近活跃时间戳（供闲置守护进程判断是否超时回收）。"""
    try:
        act_p = _browser_activity_file()
        act_p.parent.mkdir(parents=True, exist_ok=True)
        act_p.write_text(f"{time.time():.3f}\n", encoding="utf-8")
    except Exception:
        pass


def _idle_timeout_seconds() -> int:
    raw = os.environ.get("SCHOLAR_BROWSER_IDLE_MINUTES", "").strip()
    if not raw:
        try:
            from config import get as cfg_get
            raw = str(cfg_get("browser_idle_minutes", 20))
        except Exception:
            raw = "20"
    try:
        mins = float(raw)
        return max(0, int(mins * 60))
    except (TypeError, ValueError):
        return 1200


def _browser_lock_file(port: int = _DEFAULT_DEBUG_PORT if "_DEFAULT_DEBUG_PORT" in globals() else 9222) -> Path:
    try:
        from config import global_state_dir
        return global_state_dir(create=True) / f"browser-port-{int(port)}.lock"
    except Exception:
        return Path.cwd() / ".scholar-kit" / f"browser-port-{int(port)}.lock"


def _try_lock_file_nonblocking(fh) -> bool:
    if sys.platform == "win32":
        import msvcrt
        fh.seek(0)
        if fh.tell() == 0:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write("0")
                fh.flush()
            fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True


def _acquire_browser_lock(port: int = 9222, timeout: float = 180.0):
    """跨平台获取共享浏览器调试端口文件锁，防止多命令/多智能体并发踩踏同一常驻 Chrome 标签页。"""
    lock_p = _browser_lock_file(port)
    try:
        lock_p.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_p, "a+", encoding="utf-8")
    except Exception:
        return None

    deadline = time.time() + max(0.0, float(timeout))
    warned = False
    while True:
        try:
            _try_lock_file_nonblocking(fh)
            return fh
        except (OSError, IOError):
            if time.time() >= deadline:
                try:
                    fh.close()
                except Exception:
                    pass
                if timeout <= 0:
                    return False
                raise TimeoutError(f"等待共享浏览器端口 {port} 释放锁超时 ({int(timeout)}s)")
            if not warned:
                _log(f"[cnki] 端口 {port} 共享浏览器正被另一知网任务使用，排队等待释放...")
                warned = True
            time.sleep(0.4)


def _release_browser_lock(fh) -> None:
    if not fh or fh is False:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _spawn_idle_reaper(port: int) -> None:
    """启动极轻量后台守护子进程：共享浏览器连续闲置超过阈值（默认 20 分钟）后自动调用 close_shared_browser 退出，避免程序坞长期残留图标。"""
    idle_sec = _idle_timeout_seconds()
    if idle_sec <= 0:
        return
    scripts_root = str(Path(__file__).resolve().parents[1])
    act_file = str(_browser_activity_file())
    reaper_code = (
        "import pathlib, socket, sys, time\n"
        f"sys.path.insert(0, {scripts_root!r})\n"
        f"port, idle_sec, act_p = {int(port)}, {int(idle_sec)}, pathlib.Path({act_file!r})\n"
        "def port_alive():\n"
        "    try:\n"
        "        with socket.create_connection(('127.0.0.1', port), timeout=0.5): return True\n"
        "    except Exception: return False\n"
        "time.sleep(15)\n"
        "while port_alive():\n"
        "    try:\n"
        "        last = float(act_p.read_text(encoding='utf-8').strip())\n"
        "    except Exception:\n"
        "        last = time.time()\n"
        "    if time.time() - last >= idle_sec:\n"
        "        try:\n"
        "            from cnki.driver import close_shared_browser\n"
        "            res = close_shared_browser(port, respect_lock=True)\n"
        "            if res.get('reason') == 'busy':\n"
        "                time.sleep(45)\n"
        "                continue\n"
        "        except Exception: pass\n"
        "        break\n"
        "    time.sleep(45)\n"
    )
    import subprocess
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000200 | 0x00000008
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "-c", reaper_code], **kwargs)
    except Exception:
        pass


_STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer'},
            {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
            {name:'Native Client', filename:'internal-nacl-plugin'}
        ]
    });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['zh-CN', 'zh', 'en-US', 'en']
    });
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) => (
        params.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : origQuery(params)
    );
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel Iris OpenGL Engine';
        return getParam.apply(this, arguments);
    };
"""


_VISIBLE_POS = (100, 100)

_DEFAULT_DEBUG_PORT = 9222

_DEFAULT_CNKI_DIRECT_DOMAINS = [
    "*.cnki.net",
    "*.cnki.com.cn",
    "*.cnki.com",
    "*.carsi.edu.cn",
    "fsso.cnki.net",
]


def _split_domains(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[;,]", value) if part.strip()]


def _cnki_proxy_bypass_domains(extra_domains: Optional[list[str]] = None) -> list[str]:
    """Return CNKI/CARSI domains that should bypass browser-level proxies.

    Users can extend this list with `SCHOLAR_CNKI_DIRECT_DOMAINS`, for example
    school SSO domains or VPN portal domains. The browser flag helps with
    system proxy / PAC / Clash rule mode. TUN/global proxy modes may still need
    native proxy rules outside Scholar Kit.
    """
    domains = list(_DEFAULT_CNKI_DIRECT_DOMAINS)
    domains.extend(_split_domains(os.environ.get("SCHOLAR_CNKI_DIRECT_DOMAINS", "")))
    for item in extra_domains or []:
        domains.extend(_split_domains(item))

    seen = set()
    unique = []
    for domain in domains:
        cleaned = domain.strip()
        if cleaned and cleaned not in seen:
            unique.append(cleaned)
            seen.add(cleaned)
    return unique


def _merge_direct_domains(extra_domains: Optional[list[str]]) -> Tuple[Optional[str], list[str]]:
    previous = os.environ.get("SCHOLAR_CNKI_DIRECT_DOMAINS")
    merged = _split_domains(previous or "")
    for domain in extra_domains or []:
        merged.extend(_split_domains(domain))
    if merged:
        os.environ["SCHOLAR_CNKI_DIRECT_DOMAINS"] = ";".join(merged)
    return previous, _cnki_proxy_bypass_domains()


def _restore_direct_domains(previous: Optional[str]) -> None:
    if previous is None:
        os.environ.pop("SCHOLAR_CNKI_DIRECT_DOMAINS", None)
    else:
        os.environ["SCHOLAR_CNKI_DIRECT_DOMAINS"] = previous


def _is_codex_like_env() -> bool:
    """Return true when running under Codex or a Codex-like sandbox wrapper."""
    return any(k.startswith("CODEX_") for k in os.environ)


def _find_browser_exe(browser: str) -> Optional[str]:
    """找到浏览器可执行文件的绝对路径。"""
    if sys.platform == "win32":
        if browser == "edge":
            candidates = [
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
            ]
        else:
            candidates = [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            ]
        for p in candidates:
            if os.path.isfile(p):
                return p
    elif sys.platform == "darwin":
        candidates = (
            [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                str(Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ]
            if browser == "edge"
            else [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
        )
        for p in candidates:
            if os.path.isfile(p):
                return p

    names = (
        ["microsoft-edge", "microsoft-edge-stable", "msedge"]
        if browser == "edge"
        else ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]
    )
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _launch_browser_outside_sandbox(browser: str, port: int = _DEFAULT_DEBUG_PORT) -> bool:
    """在沙盒外启动浏览器（带远程调试端口）。

    尝试两种方式脱离沙盒的进程限制：
    1. CREATE_BREAKAWAY_FROM_JOB — 让子进程脱离 Windows Job Object
    2. ShellExecuteW — 通过 Windows Shell 通道启动（explorer.exe 不在沙盒内）
    """
    exe = _find_browser_exe(browser)
    if not exe:
        _log(f"[cnki] 未找到 {browser} 可执行文件，无法外部启动")
        return False

    profile_dir = _profile_dir(create=True)

    args = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        f"--proxy-bypass-list={';'.join(_cnki_proxy_bypass_domains())}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-blink-features=AutomationControlled",
        "--window-position=-32000,-32000",
        "--window-size=1280,900",
        "about:blank",
    ]

    if sys.platform == "win32":
        import subprocess
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        creation_flags = CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7  # SW_SHOWMINNOACTIVE：静默后台最小化启动，绝不抢占前台焦点
        try:
            subprocess.Popen(
                [exe] + args,
                creationflags=creation_flags,
                startupinfo=si,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            _log(f"[cnki] 已通过 CREATE_BREAKAWAY_FROM_JOB 静默后台启动 {browser}")
            return True
        except OSError as e:
            _log(f"[cnki] BREAKAWAY 方式失败: {e}，尝试 ShellExecute...")

        try:
            import ctypes
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "open", exe, " ".join(args), None, 7  # SW_SHOWMINNOACTIVE
            )
            if ret > 32:
                _log(f"[cnki] 已通过 ShellExecuteW 静默后台启动 {browser}")
                return True
            else:
                _log(f"[cnki] ShellExecuteW 返回错误码: {ret}")
        except Exception as e:
            _log(f"[cnki] ShellExecuteW 失败: {e}")

    elif sys.platform == "darwin":
        import subprocess
        app_idx = exe.find(".app/")
        if app_idx > 0 and shutil.which("open"):
            app_bundle = exe[: app_idx + 4]
            try:
                subprocess.Popen(
                    ["open", "-g", "-j", "-n", "-a", app_bundle, "--args"] + args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                _log(f"[cnki] 已通过 macOS open -g -j 静默后台启动 {browser}（不抢焦点）")
                return True
            except OSError as e:
                _log(f"[cnki] open -g -j 启动失败: {e}，回退直接启动...")
        try:
            subprocess.Popen(
                [exe] + args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            _log(f"[cnki] 已在独立会话启动 {browser}，等待调试端口连接")
            return True
        except OSError as e:
            _log(f"[cnki] 独立会话启动 {browser} 失败: {e}")

    else:
        import subprocess
        try:
            subprocess.Popen(
                [exe] + args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            _log(f"[cnki] 已在独立会话启动 {browser}，等待调试端口连接")
            return True
        except OSError as e:
            _log(f"[cnki] 独立会话启动 {browser} 失败: {e}")

    return False


def _is_debug_port_open(port: int = _DEFAULT_DEBUG_PORT, timeout: float = 0.35) -> bool:
    """毫秒级探测本地调试端口是否已存活。"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except PermissionError:
        return _wait_for_debug_port_http(port, 1)
    except (ConnectionRefusedError, OSError, socket.timeout):
        return False


def _wait_for_debug_port(port: int = _DEFAULT_DEBUG_PORT, timeout: int = 15) -> bool:
    """等待浏览器调试端口就绪。"""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError, socket.timeout):
            time.sleep(0.25)
        except PermissionError:
            _log("[cnki] socket 权限被拒（沙盒），改用 HTTP 探测调试端口...")
            return _wait_for_debug_port_http(port, max(1, int(deadline - time.time())))
    return False


def _wait_for_debug_port_http(port: int, timeout: int) -> bool:
    """通过 HTTP 请求探测调试端口（绕过 socket 限制）。"""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/json/version")
            with urllib.request.urlopen(req, timeout=2):
                return True
        except Exception:
            time.sleep(1)
    return False


def _connect_remote_browser(browser: str, port: int = _DEFAULT_DEBUG_PORT) -> "webdriver.Remote":
    """通过远程调试端口连接到已运行的浏览器（优先由 Selenium Manager 精确匹配驱动版本，离线时回退最高版本本地驱动）。"""
    OptionsClass = webdriver.EdgeOptions if browser == "edge" else webdriver.ChromeOptions
    DriverClass = webdriver.Edge if browser == "edge" else webdriver.Chrome

    options = OptionsClass()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")

    driver = None
    try:
        driver = DriverClass(options=options)
    except Exception as first_err:
        local_driver = _find_local_driver(browser)
        if not local_driver:
            raise first_err
        if browser == "edge":
            from selenium.webdriver.edge.service import Service
        else:
            from selenium.webdriver.chrome.service import Service
        service = Service(executable_path=local_driver)
        driver = DriverClass(service=service, options=options)

    _log(f"[cnki] 已通过远程调试端口 {port} 连接到 {browser}")
    return driver


def _macos_hide_browser() -> bool:
    """macOS 专用：通过 AppKit NSRunningApplication.hide 将 Scholar Kit 浏览器进程静默隐藏（无最小化果冻动画、不改视口尺寸）。"""
    if sys.platform != "darwin":
        return False
    import subprocess
    pid = _find_scholar_browser_pid_macos()
    if not pid:
        return False
    jxa = (
        'ObjC.import("AppKit"); '
        f'var app = $.NSRunningApplication.runningApplicationWithProcessIdentifier({pid}); '
        'if (app && !app.isNil()) { app.hide; "ok"; } else { "miss"; }'
    )
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", jxa], capture_output=True, text=True, timeout=3)
        return r.returncode == 0 and "ok" in (r.stdout or "")
    except Exception:
        return False


def _hide_browser(driver: "webdriver.Remote"):
    """将浏览器静默收至后台（macOS 使用进程级 AppKit hide 免抢焦点；其余平台最小化到任务栏）。"""
    if sys.platform == "darwin" and _macos_hide_browser():
        return
    try:
        driver.minimize_window()
    except Exception:
        try:
            driver.set_window_position(*_VISIBLE_POS)
            driver.set_window_size(1200, 900)
        except Exception:
            pass


def _show_browser(driver: "webdriver.Remote"):
    """将浏览器解除最小化、移回屏幕可见区域并强制置顶到系统最前台（兼容 macOS / Windows / Linux）。"""
    # 1. 优先通过 CDP 强制解除最小化（Browser.setWindowBounds windowState='normal'）并激活当前标签页
    #    解决 Chrome 被 minimize_window() 收进 macOS 程序坞 / Windows 任务栏后 set_window_position 报错或仍在后台的问题
    unminimized_by_cdp = False
    try:
        win_info = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        wid = win_info.get("windowId") if isinstance(win_info, dict) else None
        if wid is not None:
            driver.execute_cdp_cmd("Browser.setWindowBounds", {
                "windowId": wid,
                "bounds": {"windowState": "normal"},
            })
            driver.execute_cdp_cmd("Browser.setWindowBounds", {
                "windowId": wid,
                "bounds": {
                    "left": _VISIBLE_POS[0],
                    "top": _VISIBLE_POS[1],
                    "width": 1200,
                    "height": 900,
                    "windowState": "normal",
                },
            })
            unminimized_by_cdp = True
    except Exception:
        pass

    try:
        driver.execute_cdp_cmd("Page.bringToFront", {})
    except Exception:
        pass

    if not unminimized_by_cdp:
        try:
            driver.set_window_position(*_VISIBLE_POS)
            driver.set_window_size(1200, 900)
        except Exception as e:
            _log(f"[cnki] 无法移动窗口: {e}")

    try:
        driver.switch_to.window(driver.current_window_handle)
    except Exception:
        pass

    try:
        driver.execute_script("window.focus();")
    except Exception:
        pass

    # 2. 操作系统级窗口强制置顶（突破 IDE / 终端前台焦点遮挡）
    if sys.platform == "win32":
        _win32_bring_to_front(driver)
    elif sys.platform == "darwin":
        _macos_bring_to_front(driver)
    elif sys.platform.startswith("linux"):
        _linux_bring_to_front(driver)


def _find_scholar_browser_pid_macos() -> Optional[int]:
    """在 macOS 上精确定位当前 Scholar Kit 浏览器主进程 PID（避免误激活用户日常打开的另一个 Chrome）。"""
    import subprocess
    port = str(os.environ.get("SCHOLAR_DEBUG_PORT", _DEFAULT_DEBUG_PORT))
    prof = str(_profile_dir(create=False))
    try:
        out = subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True, timeout=3)
    except Exception:
        return None

    for marker in (f"--remote-debugging-port={port}", f"--user-data-dir={prof}"):
        for line in out.splitlines():
            s = line.strip()
            if not s or marker not in s or "--type=" in s or "chromedriver" in s or "msedgedriver" in s:
                continue
            parts = s.split(None, 1)
            if parts and parts[0].isdigit():
                return int(parts[0])
    return None


def _macos_bring_to_front(driver: "webdriver.Remote"):
    """macOS 专用：通过 AppKit NSRunningApplication 按精确 PID 抢占前台焦点（无需辅助功能 TCC 授权）。"""
    import subprocess
    pid = _find_scholar_browser_pid_macos()
    if pid:
        jxa = (
            'ObjC.import("AppKit"); '
            f'var app = $.NSRunningApplication.runningApplicationWithProcessIdentifier({pid}); '
            'if (app && !app.isNil()) { app.unhide; app.activateWithOptions(3); "ok"; } else { "miss"; }'
        )
        try:
            r = subprocess.run(["osascript", "-l", "JavaScript", "-e", jxa], capture_output=True, text=True, timeout=4)
            if r.returncode == 0 and "ok" in (r.stdout or ""):
                _log(f"[cnki] macOS: 浏览器窗口已强制置顶 (pid={pid})")
                return
        except Exception as e:
            _log(f"[cnki] macOS JXA 按 PID 置顶异常: {e}")

    # 兜底：按浏览器应用名激活
    caps_name = ""
    try:
        caps_name = str((getattr(driver, "capabilities", None) or {}).get("browserName", "")).lower()
    except Exception:
        pass
    app_name = "Microsoft Edge" if "edge" in caps_name else "Google Chrome"
    try:
        subprocess.run(["osascript", "-e", f'tell application "{app_name}" to activate'], capture_output=True, timeout=4)
        _log(f"[cnki] macOS: 已激活 {app_name} 到前台")
    except Exception as e:
        _log(f"[cnki] macOS 置顶失败: {e}，请点击程序坞中的浏览器图标完成验证")


def _linux_bring_to_front(driver: "webdriver.Remote"):
    """Linux 专用：优先尝试 wmctrl / xdotool 将验证窗口切到最前。"""
    import subprocess
    title = ""
    try:
        title = (driver.title or "").strip()
    except Exception:
        pass
    if not title:
        return
    for cmd in (
        ["wmctrl", "-a", title],
        ["xdotool", "search", "--name", title, "windowactivate"],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=3)
                _log(f"[cnki] Linux: 已通过 {cmd[0]} 置顶浏览器窗口")
                return
            except Exception:
                continue


def _win32_bring_to_front(driver: "webdriver.Remote"):
    """Windows 专用：通过 Win32 API 解除最小化并用 TOPMOST->NOTOPMOST 突破后台进程焦点锁置顶窗口。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        page_title = (driver.title or "").strip()

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        found_hwnd = [None]
        fallback_hwnd = [None]

        def _enum_cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title_val = buf.value
                if page_title and page_title in title_val:
                    found_hwnd[0] = hwnd
                    return False
                if ("Chrome" in title_val or "Edge" in title_val) and any(
                    k in title_val for k in ("知网", "安全验证", "CNKI")
                ):
                    fallback_hwnd[0] = hwnd
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        target_hwnd = found_hwnd[0] or fallback_hwnd[0]

        if target_hwnd:
            SW_RESTORE = 9
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_SHOWWINDOW = 0x0040
            user32.ShowWindow(target_hwnd, SW_RESTORE)
            user32.SetWindowPos(target_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetWindowPos(target_hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.BringWindowToTop(target_hwnd)
            user32.SetForegroundWindow(target_hwnd)
            _log("[cnki] Win32: 浏览器窗口已强制置顶")
        else:
            _log("[cnki] Win32: 未找到匹配窗口，请在任务栏查找浏览器")
    except Exception as e:
        _log(f"[cnki] Win32 置顶失败: {e}，请手动在任务栏找到浏览器窗口")


def _ensure_selenium_cache():
    """确保 Selenium 缓存目录可写。沙盒中默认路径不可写时降级到工作目录。"""
    if os.environ.get("SE_CACHE_PATH"):
        return

    if _is_codex_like_env():
        fallback = Path.cwd() / ".scholar-kit" / "selenium-cache"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
            os.environ["SE_CACHE_PATH"] = str(fallback)
            _log(f"[cnki] Codex 环境使用项目内 Selenium 缓存: {fallback}")
            return
        except Exception as e:
            _log(f"[cnki] 项目内 Selenium 缓存不可用: {e}")

    if sys.platform == "win32":
        default_cache = Path(os.environ.get("LOCALAPPDATA", "")) / "selenium"
    else:
        default_cache = Path.home() / ".cache" / "selenium"

    try:
        default_cache.mkdir(parents=True, exist_ok=True)
        test_file = default_cache / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
    except (PermissionError, OSError):
        try:
            from config import cache_dir
            fallback = cache_dir("selenium-cache", create=True)
        except ImportError:
            fallback = Path.cwd() / ".scholar-kit" / "selenium-cache"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
            os.environ["SE_CACHE_PATH"] = str(fallback)
            _log(f"[cnki] 默认缓存不可写，已切换到: {fallback}")
        except Exception as e:
            _log(f"[cnki] 缓存目录降级也失败: {e}")


def _find_local_driver(browser: str) -> Optional[str]:
    """查找已安装的浏览器驱动（当 Selenium Manager 无法联网下载时使用）。

    搜索顺序：
    1. SCHOLAR_DRIVER_PATH 环境变量（用户显式指定）
    2. PATH 环境变量
    3. Selenium 缓存目录（全平台支持）
    4. Edge/Chrome 安装目录的版本子文件夹
    5. selenium-manager --offline 模式（利用 Selenium 内置工具查找）
    """
    user_path = os.environ.get("SCHOLAR_DRIVER_PATH", "").strip()
    if user_path and os.path.isfile(user_path):
        _log(f"[cnki] 使用 SCHOLAR_DRIVER_PATH: {user_path}")
        return user_path

    driver_name = "msedgedriver" if browser == "edge" else "chromedriver"
    if sys.platform == "win32":
        driver_name += ".exe"

    found = shutil.which(driver_name)
    if found:
        _log(f"[cnki] 在 PATH 中找到驱动: {found}")
        return found

    cache_roots = []
    if os.environ.get("SE_CACHE_PATH"):
        cache_roots.append(Path(os.environ["SE_CACHE_PATH"]))
    if sys.platform == "win32":
        cache_roots.append(Path(os.environ.get("LOCALAPPDATA", "")) / "selenium")
    else:
        cache_roots.append(Path.home() / ".cache" / "selenium")
    try:
        from config import cache_dir
        cache_roots.append(cache_dir("selenium-cache", create=False))
    except ImportError:
        pass

    cached_candidates: list[str] = []
    for se_cache in cache_roots:
        if se_cache and se_cache.is_dir():
            for root, _dirs, files in os.walk(str(se_cache)):
                if driver_name in files:
                    cached_candidates.append(os.path.join(root, driver_name))
    if cached_candidates:
        def _ver_key(path_str: str) -> tuple:
            nums = tuple(int(x) for x in re.findall(r"\d+", path_str))
            try:
                mtime = os.path.getmtime(path_str)
            except OSError:
                mtime = 0.0
            return (nums, mtime)

        cached_candidates.sort(key=_ver_key, reverse=True)
        best = cached_candidates[0]
        _log(f"[cnki] 在缓存中找到最新驱动: {best}")
        return best

    if sys.platform == "win32":
        app_dirs = []
        if browser == "edge":
            app_dirs = [
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application"),
                os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application"),
            ]
        else:
            app_dirs = [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application"),
                os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application"),
            ]
        for app_dir in app_dirs:
            if not os.path.isdir(app_dir):
                continue
            for entry in os.listdir(app_dir):
                candidate = os.path.join(app_dir, entry, driver_name)
                if os.path.isfile(candidate):
                    _log(f"[cnki] 在浏览器安装目录找到驱动: {candidate}")
                    return candidate

    try:
        from selenium.webdriver.common.selenium_manager import SeleniumManager
        sm_bin = SeleniumManager._get_binary()
        if sm_bin and os.path.isfile(str(sm_bin)):
            import subprocess
            browser_arg = "MicrosoftEdge" if browser == "edge" else "chrome"
            result = subprocess.run(
                [str(sm_bin), "--browser", browser_arg, "--offline", "--output", "json"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                import json as _json
                try:
                    info = _json.loads(result.stdout)
                    driver_path = info.get("result", {}).get("driver_path", "")
                    if driver_path and os.path.isfile(driver_path):
                        _log(f"[cnki] selenium-manager --offline 找到驱动: {driver_path}")
                        return driver_path
                except (ValueError, KeyError):
                    pass
    except Exception as e:
        _log(f"[cnki] selenium-manager 查找失败: {e}")

    _log(f"[cnki] 未找到本地 {driver_name}")
    return None


def _should_reuse_browser(headless: bool = False) -> bool:
    """判断是否启用常驻浏览器热复用（默认开启，可通过 SCHOLAR_REUSE_BROWSER=0 关闭）。"""
    if headless:
        return False
    env_raw = os.environ.get("SCHOLAR_REUSE_BROWSER", "").strip().lower()
    if env_raw in ("0", "false", "no", "off"):
        return False
    if env_raw in ("1", "true", "yes", "on"):
        return True
    try:
        from config import get as cfg_get
        return bool(int(cfg_get("reuse_browser", 1)))
    except Exception:
        return True


def _safe_get(driver: "webdriver.Remote", url: str, retries: int = 2, retry_delay: float = 1.2) -> None:
    """带瞬态连接重置自动重试的 driver.get 包装器。"""
    transient_markers = (
        "ERR_CONNECTION_RESET",
        "ERR_CONNECTION_CLOSED",
        "ERR_NETWORK_CHANGED",
        "ERR_TIMED_OUT",
        "ERR_SOCKET_NOT_CONNECTED",
        "Read timed out",
    )
    last_exc = None
    for attempt in range(retries + 1):
        try:
            driver.get(url)
            return
        except Exception as e:
            last_exc = e
            msg = str(e)
            if attempt < retries and any(m in msg for m in transient_markers):
                _log(f"[cnki] 导航瞬态网络波动 ({msg[:80]})，{retry_delay}s 后第 {attempt + 1} 次重试...")
                time.sleep(retry_delay)
                if "kns.cnki.net/" not in url.rstrip("/") or url.rstrip("/") != "https://kns.cnki.net":
                    try:
                        driver.get("https://kns.cnki.net/")
                        time.sleep(0.4)
                    except Exception:
                        pass
                continue
            raise
    if last_exc:
        raise last_exc


def _release_driver(driver: Optional["webdriver.Remote"], *, force_quit: bool = False) -> None:
    """安全释放浏览器会话：
    - 常驻复用模式 (`_scholar_reused=True`) 下仅保存 Cookie、清理多余标签页并停止本轮 chromedriver 服务，
      保持后台 Chrome 进程存活供下一条 CLI 命令秒级热附着；
    - 独占模式或 `force_quit=True` 时完整退出浏览器；
    - 始终在 finally 中释放跨进程文件锁。
    """
    if driver is None:
        return
    lock_fh = getattr(driver, "_scholar_lock_fh", None)
    try:
        try:
            _touch_browser_activity()
            _save_cookies(driver)
        except Exception:
            pass

        reused = bool(getattr(driver, "_scholar_reused", False))
        if reused and not force_quit:
            try:
                handles = list(getattr(driver, "window_handles", []) or [])
                if len(handles) > 1:
                    closed_via_cdp = False
                    if hasattr(driver, "execute_cdp_cmd"):
                        try:
                            cur_h = getattr(driver, "current_window_handle", "") or ""
                            t_info = driver.execute_cdp_cmd("Target.getTargets", {}) or {}
                            pages = [
                                t for t in (t_info.get("targetInfos") or [])
                                if isinstance(t, dict) and t.get("type") == "page" and t.get("targetId")
                            ]
                            if len(pages) > 1:
                                keep_tid = next(
                                    (t["targetId"] for t in pages if t["targetId"] in cur_h),
                                    pages[0]["targetId"],
                                )
                                for t in pages:
                                    if t["targetId"] != keep_tid:
                                        try:
                                            driver.execute_cdp_cmd("Target.closeTarget", {"targetId": t["targetId"]})
                                        except Exception:
                                            pass
                                closed_via_cdp = True
                        except Exception:
                            closed_via_cdp = False
                    if not closed_via_cdp:
                        keep = handles[0]
                        for h in handles[1:]:
                            try:
                                driver.switch_to.window(h)
                                driver.close()
                            except Exception:
                                pass
                        try:
                            driver.switch_to.window(keep)
                        except Exception:
                            pass
                        _hide_browser(driver)
            except Exception:
                pass
            try:
                svc = getattr(driver, "service", None)
                if svc is not None:
                    svc.stop()
            except Exception:
                pass
            return

        try:
            if reused and force_quit:
                try:
                    driver.execute_cdp_cmd("Browser.close", {})
                except Exception:
                    pass
            driver.quit()
        except Exception:
            pass
    finally:
        driver._scholar_lock_fh = None
        _release_browser_lock(lock_fh)


def close_shared_browser(port: Optional[int] = None, *, respect_lock: bool = False) -> dict:
    """显式关闭后台常驻的知网共享浏览器实例。"""
    active_port = int(port or os.environ.get("SCHOLAR_DEBUG_PORT", _DEFAULT_DEBUG_PORT))
    lock_fh = None
    if respect_lock:
        lock_fh = _acquire_browser_lock(active_port, timeout=0.0)
        if lock_fh is False:
            return {
                "status": "success",
                "closed": False,
                "port": active_port,
                "reason": "busy",
                "message": f"端口 {active_port} 正在执行任务，跳过空闲回收",
            }
    if not _is_debug_port_open(active_port):
        _release_browser_lock(lock_fh)
        return {
            "status": "success",
            "closed": False,
            "port": active_port,
            "message": f"端口 {active_port} 未检测到运行中的共享浏览器实例",
        }

    # macOS 优先通过进程级 SIGTERM 优雅退出（无需再拉起 chromedriver 触发窗口唤醒，Profile 会自动刷盘，Cookie 已在每轮 _release_driver 持久化）
    if sys.platform == "darwin":
        pid = _find_scholar_browser_pid_macos()
        if pid:
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
                deadline = time.time() + 3.0
                while time.time() < deadline and _is_debug_port_open(active_port, timeout=0.2):
                    time.sleep(0.15)
                if not _is_debug_port_open(active_port, timeout=0.2):
                    _release_browser_lock(lock_fh)
                    return {
                        "status": "success",
                        "closed": True,
                        "port": active_port,
                        "message": f"已静默关闭端口 {active_port} 上的共享浏览器实例",
                    }
            except Exception:
                pass

    driver = None
    try:
        browser = _detect_browser()
        driver = _connect_remote_browser(browser, active_port)
        _save_cookies(driver)
        try:
            driver.execute_cdp_cmd("Browser.close", {})
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass
        deadline = time.time() + 3.0
        while time.time() < deadline and _is_debug_port_open(active_port, timeout=0.2):
            time.sleep(0.2)
        return {
            "status": "success",
            "closed": True,
            "port": active_port,
            "message": f"已保存 Cookie 并关闭端口 {active_port} 上的共享浏览器实例",
        }
    except Exception as e:
        return {
            "status": "error",
            "code": "CLOSE_BROWSER_FAILED",
            "port": active_port,
            "message": str(e),
        }
    finally:
        if driver is not None:
            try:
                svc = getattr(driver, "service", None)
                if svc is not None:
                    svc.stop()
            except Exception:
                pass
        _release_browser_lock(lock_fh)


def _create_driver(browser: str = None, headless: bool = False) -> "webdriver.Remote":
    """创建或热附着浏览器实例。默认开启有头常驻热复用，后续命令秒级连接。"""
    if not HAS_SELENIUM:
        raise RuntimeError(
            "selenium 未安装。请运行: pip install selenium>=4.10\n"
            "Selenium 4.10+ 会自动管理 WebDriver，无需手动下载。"
        )

    _ensure_selenium_cache()

    if browser is None:
        browser = _detect_browser()

    OptionsClass = webdriver.EdgeOptions if browser == "edge" else webdriver.ChromeOptions
    DriverClass = webdriver.Edge if browser == "edge" else webdriver.Chrome

    options = OptionsClass()
    if headless:
        options.add_argument("--headless=new")
    startup_args = [
        f"--proxy-bypass-list={';'.join(_cnki_proxy_bypass_domains())}",
        "--disable-popup-blocking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-features=RendererCodeIntegrity",
        "--window-size=1920,1080",
    ]
    custom_ua = _build_user_agent(browser, headless=headless)
    if custom_ua:
        startup_args.append(f"--user-agent={custom_ua}")
    if not headless:
        startup_args.append(f"--window-position={_VISIBLE_POS[0]},{_VISIBLE_POS[1]}")
    for arg in startup_args:
        options.add_argument(arg)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    sandbox_profile = _profile_dir(create=True)
    try:
        sandbox_profile.mkdir(parents=True, exist_ok=True)
        options.add_argument(f"--user-data-dir={sandbox_profile}")
    except (PermissionError, OSError):
        pass

    driver = None
    first_err = None
    was_already_warm = False
    port = int(os.environ.get("SCHOLAR_DEBUG_PORT", _DEFAULT_DEBUG_PORT))
    reuse_mode = _should_reuse_browser(headless=headless)
    lock_fh = _acquire_browser_lock(port) if reuse_mode else None

    # 策略 0: 常驻热复用模式（优先连接已有调试端口，未启动则以独立会话启动供跨命令共享）
    if reuse_mode:
        if _is_debug_port_open(port):
            try:
                driver = _connect_remote_browser(browser, port)
                handles = list(driver.window_handles or [])
                if not handles:
                    driver.switch_to.new_window("tab")
                    _hide_browser(driver)
                # 切勿在此无故调用 driver.switch_to.window(handles[0])！
                # chromedriver 连接时已默认绑定主标签页，调用 switch_to.window 会发送 Target.activateTarget 导致浏览器弹到前台抢焦点！
                driver._scholar_reused = True
                was_already_warm = True
                _log(f"[cnki] 热复用已就绪浏览器实例 (port={port})")
            except Exception as e:
                _log(f"[cnki] 复用已有调试端口失败，将重新启动: {e}")
                driver = None
        if driver is None:
            launched = _launch_browser_outside_sandbox(browser, port)
            if launched and _wait_for_debug_port(port, timeout=10):
                if not headless and sys.platform == "darwin":
                    _macos_hide_browser()
                _touch_browser_activity()
                _spawn_idle_reaper(port)
                try:
                    driver = _connect_remote_browser(browser, port)
                    driver._scholar_reused = True
                except Exception as e:
                    first_err = e
                    _log(f"[cnki] 独立会话浏览器连接失败，回退常规启动: {e}")
                    driver = None

    # 策略 1: 正常方式启动（含本地驱动回退）
    if driver is None:
        try:
            driver = DriverClass(options=options)
            driver._scholar_reused = False
        except Exception as e:
            first_err = e
            _log(f"[cnki] 默认方式创建浏览器失败: {e}")
            local_driver = _find_local_driver(browser)
            if local_driver:
                _log(f"[cnki] 找到本地驱动，尝试使用: {local_driver}")
                if browser == "edge":
                    from selenium.webdriver.edge.service import Service
                else:
                    from selenium.webdriver.chrome.service import Service
                service = Service(executable_path=local_driver)
                try:
                    driver = DriverClass(service=service, options=options)
                    driver._scholar_reused = False
                except Exception:
                    pass

    # 策略 2: 沙盒内启动失败 → 在沙盒外启动浏览器并通过远程调试连接
    if driver is None:
        _log("[cnki] 沙盒内启动失败，尝试在沙盒外启动浏览器...")
        launched = _launch_browser_outside_sandbox(browser, port)
        if launched and _wait_for_debug_port(port):
            try:
                driver = _connect_remote_browser(browser, port)
                driver._scholar_reused = _should_reuse_browser(headless=headless)
            except Exception as e:
                _log(f"[cnki] 远程连接失败: {e}")

    # 策略 3: 尝试连接用户可能已打开的浏览器调试端口
    if driver is None:
        if _wait_for_debug_port(port, timeout=3):
            _log(f"[cnki] 检测到端口 {port} 已有浏览器，尝试连接...")
            try:
                driver = _connect_remote_browser(browser, port)
                driver._scholar_reused = _should_reuse_browser(headless=headless)
            except Exception as e:
                _log(f"[cnki] 连接已有浏览器失败: {e}")

    if driver is None:
        _release_browser_lock(lock_fh)
        err_msg = (
            f"无法创建浏览器实例（{browser}）: {first_err}\n\n"
            f"【诊断】所有启动方式均失败（直接启动 / 本地驱动 / 沙盒外启动）。\n\n"
            f"【解决方案（按优先级）】\n"
            f"1. 提权运行：Cursor 加 required_permissions: [\"all\"]，"
            f"Codex 在 .codex/config.toml 加 network_access = true\n"
            f"2. 手动指定驱动路径：设置环境变量 SCHOLAR_DRIVER_PATH=/path/to/driver\n"
            f"3. 确认网络可用后：pip install selenium>=4.10 会自动管理驱动"
        )
        raise RuntimeError(err_msg)

    driver._scholar_lock_fh = lock_fh if getattr(driver, "_scholar_reused", False) else None
    if lock_fh and not getattr(driver, "_scholar_reused", False):
        _release_browser_lock(lock_fh)

    no_stealth = os.environ.get("SCHOLAR_KIT_NO_STEALTH", "").strip()
    if no_stealth not in ("1", "true", "yes"):
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": _STEALTH_JS
            })
        except Exception:
            pass

    driver.set_page_load_timeout(30)
    driver.implicitly_wait(3)
    driver._scholar_was_warm = was_already_warm
    _touch_browser_activity()

    if not headless:
        if not was_already_warm:
            _hide_browser(driver)
        elif sys.platform == "darwin":
            _macos_hide_browser()

    return driver


def _prepare_cnki_session(driver: "webdriver.Remote") -> None:
    """初始化或热复用知网会话：
    - 若热复用实例当前已停留在 cnki.net 正常页面，直接保留内存新鲜会话（不重刷首页、不用旧文件覆盖 Cookie，大幅降低二次滑块触发率）；
    - 仅在冷启动（如 about:blank）时访问一次知网首页并合并缺失 Cookie。
    """
    import random as _random
    _touch_browser_activity()
    cur_url = ""
    try:
        cur_url = driver.current_url or ""
    except Exception:
        cur_url = ""

    if (
        getattr(driver, "_scholar_was_warm", False)
        and "cnki.net" in cur_url
        and not _is_cnki_security_gate(driver)
    ):
        _log("[cnki] 复用已就绪知网页面会话，跳过重刷首页与 Cookie 覆盖")
        return

    _safe_get(driver, "https://kns.cnki.net/")
    time.sleep(_random.uniform(0.55, 0.95))
    _load_cookies(driver, merge_only=True)


def _navigate_within_cnki(
    driver: "webdriver.Remote",
    target_url: str,
    *,
    base_delay: Optional[float] = None,
) -> None:
    """站内自然跳转：当前已在 cnki.net 时优先通过 window.location.href 导航以携带真实站内 Referer，并附加随机抖动间隔。"""
    import random as _random
    _touch_browser_activity()
    cur_url = ""
    try:
        cur_url = driver.current_url or ""
    except Exception:
        cur_url = ""

    navigated = False
    if "cnki.net" in cur_url and target_url.startswith("http"):
        try:
            driver.execute_script("window.location.href = arguments[0];", target_url)
            navigated = True
        except Exception:
            navigated = False

    if not navigated:
        _safe_get(driver, target_url)

    wait_base = base_delay if base_delay is not None else max(1.0, REQUEST_INTERVAL - 1.2)
    time.sleep(wait_base + _random.uniform(0.35, 1.15))


def _detect_cnki_login_wall(driver: "webdriver.Remote") -> Optional[str]:
    """毫秒级检测当前标签页是否被知网重定向到登录/机构认证/付费拦截页（用于全文与下载快速短路）。"""
    try:
        url = (driver.current_url or "").lower()
        title = (driver.title or "").strip()
    except Exception:
        return None

    login_hosts = (
        "login.cnki.net",
        "passport.cnki.net",
        "fsso.cnki.net",
        "my.cnki.net/login",
        "ecp.cnki.net",
    )
    if any(h in url for h in login_hosts):
        return "当前未登录知网机构或个人账号（已跳转至登录认证页），全文阅读与下载需连接校园网/VPN 或先运行 auth-cnki"

    login_titles = ("欢迎登录", "会员登录", "账号登录", "机构登录", "个人登录", "统一身份认证", "登录-中国知网")
    if any(t in title for t in login_titles):
        return f"知网要求登录认证（页面：{title}），请连接校园网/VPN 或先运行 auth-cnki"

    try:
        wall_hit = driver.execute_script(r"""
            var body = (document.body && document.body.innerText) || '';
            var hasContent = !!(document.querySelector('#ChDivSummary, .article-body, .main-text, canvas'));
            if (hasContent) return '';
            var loginEl = document.querySelector('.login-box, #loginBox, form[action*="login"], .ecp_login, .pay-box');
            if (loginEl && loginEl.offsetParent !== null) {
                return '检测到知网登录/付费拦截框';
            }
            var markers = ['请先登录', '非授权用户', '机构用户登录', '个人账号登录', '购买本篇', '余额不足，请充值'];
            for (var i = 0; i < markers.length; i++) {
                if (body.indexOf(markers[i]) >= 0 && body.length < 3500) {
                    return markers[i];
                }
            }
            return '';
        """)
        if wall_hit:
            return f"知网全文/下载权限受限（{wall_hit}），请连接校园网/VPN 或先运行 auth-cnki"
    except Exception:
        pass
    return None


# ── 校园网检测 ────────────────────────────────────────

def check_cnki_access() -> Tuple[bool, str]:
    """检测当前网络是否有知网访问权限（绕过代理直连）。

    Returns:
        (accessible, message)。
        accessible=False 且 message 以 'SANDBOX_BLOCKED' 开头时，
        表示预检因沙盒权限被拒（WinError 10013 等），
        浏览器侧可能仍可正常访问知网——调用方应继续尝试。
    """
    skip = os.environ.get("SCHOLAR_SKIP_NETWORK_CHECK", "").strip().lower()
    if skip in ("1", "true", "yes"):
        _log("[cnki] 跳过网络预检（SCHOLAR_SKIP_NETWORK_CHECK=1）")
        return True, "已跳过网络预检"

    import urllib.request
    try:
        proxy_handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(
            "https://kns.cnki.net/",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with opener.open(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
            if "个人中心" in content or "机构馆" in content or "退出" in content:
                return True, "检测到知网机构权限"
            return True, "知网可访问（未确认机构权限）"
    except PermissionError as e:
        return False, f"SANDBOX_BLOCKED: 网络权限被阻止 ({e})"
    except OSError as e:
        if getattr(e, "winerror", None) == 10013:
            return False, f"SANDBOX_BLOCKED: 网络权限被阻止 ({e})"
        return False, f"知网不可访问: {e}"
    except Exception as e:
        return False, f"知网不可访问: {e}"


# ── 校外认证 / 会话预热 ────────────────────────────────

def _page_text(driver: "webdriver.Remote") -> str:
    try:
        return driver.execute_script(
            "return (document.body && document.body.innerText || '').substring(0, 5000);"
        ) or ""
    except Exception:
        return ""


_CNKI_INSTITUTION_HEADER_RE = re.compile(
    r"[\u4e00-\u9fa5]{2,30}"
    r"(?:大学|学院|研究院|研究所|图书馆|博物馆|党校|社科院|干部学院)"
    r"[\s\u3000]*(?:个人登录|退出登录|退出)"
)


def _has_cnki_institution_access(driver: "webdriver.Remote", institution: Optional[str] = None) -> bool:
    try:
        current_url = driver.current_url or ""
    except Exception:
        current_url = ""
    low_url = current_url.lower()
    if "cnki.net" not in low_url:
        return False
    if any(h in low_url for h in ("login.cnki.net", "passport.cnki.net", "fsso.cnki.net", "my.cnki.net/login")):
        return False

    text = _page_text(driver)
    try:
        title = driver.title or ""
    except Exception:
        title = ""
    haystack = text + "\n" + title
    # 注意：绝不能包含「机构登录」「IP登录」——未登录知网首页顶栏按钮本身就含这两个词，会导致严重假阳性！
    markers = ("机构馆", "机构用户", "机构账号", "机构已登录")
    if any(marker in haystack for marker in markers):
        return True
    # 校内 IP 已识别但未登录个人账号时，知网头部显示“机构名 + 个人登录/退出”
    if _CNKI_INSTITUTION_HEADER_RE.search(haystack):
        return True
    if institution and institution in haystack:
        return True
    return False


def _has_cnki_personal_login(driver: "webdriver.Remote") -> bool:
    """检测知网个人账号/漫游账号登录态（基于 Ecp_LoginStuts Cookie 与顶栏用户 DOM，避免未登录顶栏按钮假阳性）。"""
    try:
        current_url = (driver.current_url or "").lower()
    except Exception:
        current_url = ""
    if "cnki.net" not in current_url:
        return False
    if any(h in current_url for h in ("login.cnki.net", "passport.cnki.net", "fsso.cnki.net", "my.cnki.net/login")):
        return False

    try:
        for c in (driver.get_cookies() or []):
            if not isinstance(c, dict):
                continue
            name = c.get("name", "")
            val = str(c.get("value", "") or "").strip()
            if name == "Ecp_LoginStuts" and val and ("UserName" in val or "ShowName" in val or "IsAutoLogin" in val):
                return True
    except Exception:
        pass

    try:
        dom_hit = driver.execute_script(r"""
            var el = document.querySelector('#Ecp_loginShowName, #Ecp_Text_UserName, .ecp_user-name, .login-user-name');
            if (!el || el.offsetParent === null) return '';
            var t = (el.innerText || el.textContent || '').trim();
            if (!t || t === '登录' || t === '个人登录' || t === '机构登录' || t === '注册') return '';
            return t;
        """)
        if isinstance(dom_hit, str) and dom_hit.strip():
            return True
    except Exception:
        pass
    return False


def _has_cnki_any_access(driver: "webdriver.Remote", institution: Optional[str] = None) -> bool:
    if _has_cnki_institution_access(driver, institution=institution):
        return True
    if not institution and _has_cnki_personal_login(driver):
        return True
    return False


def _auth_diagnostics(driver: "webdriver.Remote") -> dict:
    text = _page_text(driver)
    try:
        current_url = driver.current_url or ""
    except Exception:
        current_url = ""
    try:
        title = driver.title or ""
    except Exception:
        title = ""
    try:
        cookies = driver.get_cookies()
    except Exception:
        cookies = []
    return {
        "page": {
            "url": current_url,
            "title": title,
            "text_preview": text[:500],
        },
        "markers": {
            "institution": _has_cnki_institution_access(driver),
            "personal_account": _has_cnki_personal_login(driver),
            "login": any(
                k in text or k in title
                for k in ("统一身份认证", "用户名", "密码", "选择高校", "扫码登录", "验证码")
            ),
            "security_gate": _is_cnki_security_gate(driver),
        },
        "cookies": {
            "count": len(cookies),
            "domains": sorted({c.get("domain", "") for c in cookies if c.get("domain")}),
            "names": sorted({c.get("name", "") for c in cookies if c.get("name")}),
        },
    }


def _write_debug_snapshot(driver: "webdriver.Remote", output_path: Optional[str]) -> Optional[str]:
    if not output_path:
        return None
    path = Path(output_path)
    if path.is_dir():
        path = path / "cnki-auth-snapshot.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        html = driver.page_source
    except Exception:
        html = ""
    path.write_text(html, encoding="utf-8")
    return str(path)


def _select_cnki_institution(driver: "webdriver.Remote", institution: Optional[str]) -> bool:
    """Select a school/institution on CNKI FSSO pages when a name is provided."""
    if not institution:
        return False
    target = institution.strip()
    if not target:
        return False

    try:
        link_snapshot = driver.execute_script("""
            return Array.from(document.querySelectorAll('a')).map(function(a) {
                return {text: (a.innerText || a.textContent || '').trim(), href: a.href || ''};
            });
        """)
    except Exception:
        link_snapshot = []

    candidates = []
    for link in link_snapshot or []:
        text = (link.get("text", "") or "").strip()
        href = link.get("href", "") or ""
        if not href:
            continue
        if text == target:
            candidates.insert(0, (href, text))
        elif target in text or (text and text in target):
            candidates.append((href, text))

    if not candidates:
        _log(f"[cnki-auth] 未找到机构: {target}。请在浏览器中手动搜索/选择你的学校或机构。")
        return False

    href, text = candidates[0]
    _log(f"[cnki-auth] 选择机构: {text} -> {href}")
    driver.get(href)
    return True


def _wait_for_cnki_auth(
    driver: "webdriver.Remote",
    verify_url: str,
    institution: Optional[str],
    wait_seconds: int,
    poll_interval: int = 5,
) -> bool:
    """Wait for the user to complete off-campus SSO/CARSI login."""
    deadline = time.time() + max(wait_seconds, 0)
    _log("")
    _log("[cnki-auth] 浏览器已打开，请完成校外认证。")
    _log("[cnki-auth] 可手动选择学校/机构、输入账号密码、完成短信/扫码/滑块等验证。")
    _log("[cnki-auth] 请不要关闭浏览器窗口；登录成功后脚本会自动保存会话并继续。")
    _log(f"[cnki-auth] 最长等待 {wait_seconds} 秒。")

    while time.time() < deadline:
        try:
            if _is_cnki_security_gate(driver) or _has_captcha_elements(driver):
                driver = _show_browser_for_captcha(
                    driver,
                    "知网安全验证（如滑块），请在浏览器中完成；完成前请不要关闭窗口。",
                    poll_timeout=max(30, int(deadline - time.time())),
                )
            if _has_cnki_any_access(driver, institution=institution):
                _log("[cnki-auth] 已检测到登录/机构权限，保存会话。")
                return True
            if verify_url:
                current_url = driver.current_url or ""
                if "cnki.net" not in current_url:
                    # Stay on the user's SSO page until it redirects back.
                    pass
        except Exception as e:
            _log(f"[cnki-auth] 等待认证时浏览器状态异常: {e}")
            return False

        remaining = int(deadline - time.time())
        if remaining > 0:
            _log(f"[cnki-auth] 等待登录完成... 剩余约 {remaining} 秒")
            time.sleep(min(poll_interval, max(1, remaining)))

    _log("[cnki-auth] 等待超时，将尝试访问验证页并保存当前诊断信息。")
    try:
        driver.get(verify_url)
        time.sleep(1)
    except Exception:
        pass
    return _has_cnki_any_access(driver, institution=institution)


def authenticate_cnki(
    auth_url: str = "https://fsso.cnki.net/",
    verify_url: str = "https://kns.cnki.net/",
    institution: Optional[str] = None,
    wait_seconds: int = 180,
    captcha_timeout: int = 180,
    direct_domains: Optional[list[str]] = None,
    debug_snapshot: Optional[str] = None,
    keep_browser: bool = False,
    force: bool = False,
) -> dict:
    """Open CNKI off-campus authentication and persist the browser session.

    The function is intentionally account-agnostic. Users can pass a school name
    for CNKI FSSO auto-selection, or omit it and complete their institution's
    portal manually. Cookies and the Chrome/Edge profile are reused by later CNKI
    commands, so one successful login is normally enough for the current project
    and at least one agent conversation until the institution session expires.
    """
    driver = None
    previous_direct_domains = None
    direct_domain_list: list[str] = []
    try:
        previous_direct_domains, direct_domain_list = _merge_direct_domains(direct_domains)
        browser = _detect_browser()
        driver = _create_driver(browser=browser)

        if getattr(driver, "_scholar_was_warm", False):
            _prepare_cnki_session(driver)
            _navigate_within_cnki(driver, verify_url, base_delay=0.6)
        else:
            driver.get("https://kns.cnki.net/")
            time.sleep(0.8)
            try:
                _load_cookies(driver, merge_only=True)
            except TypeError:
                _load_cookies(driver)
            driver.get(verify_url)
            time.sleep(0.8)
        if not force and _has_cnki_any_access(driver, institution=institution):
            cookies_saved = bool(_save_cookies(driver) or _cookie_path().exists())
            return {
                "status": "success",
                "auth_url": auth_url,
                "verify_url": verify_url,
                "institution": institution,
                "already_authenticated": True,
                "access_confirmed": True,
                "cookies_saved": cookies_saved,
                "cookie_file": str(_cookie_path()),
                "profile_dir": str(_profile_dir()),
                "direct_domains": direct_domain_list,
                "diagnostics": _auth_diagnostics(driver),
                "message": "检测到已有知网登录/机构会话，已复用，无需再次登录。",
            }

        _log(f"[cnki-auth] 打开认证入口: {auth_url}")
        driver.get(auth_url)
        selected_institution = _select_cnki_institution(driver, institution)
        _show_browser(driver)

        access_confirmed = _wait_for_cnki_auth(
            driver,
            verify_url=verify_url,
            institution=institution,
            wait_seconds=max(int(wait_seconds or 0), 0),
        )

        _log(f"[cnki-auth] 验证知网访问: {verify_url}")
        driver.get(verify_url)
        time.sleep(1)
        if captcha_timeout > 0 and (_is_cnki_security_gate(driver) or _has_captcha_elements(driver)):
            driver = _show_browser_for_captcha(
                driver,
                "知网安全验证（如滑块），请在浏览器中完成；完成前请不要关闭窗口。",
                poll_timeout=captcha_timeout,
            )
        elif captcha_timeout <= 0:
            _log("[cnki-auth] 跳过安全验证等待（captcha_timeout=0）")

        access_confirmed = access_confirmed or _has_cnki_any_access(driver, institution=institution)
        diagnostics = _auth_diagnostics(driver)
        cookies_saved = bool(_save_cookies(driver) or _cookie_path().exists())
        snapshot_file = _write_debug_snapshot(driver, debug_snapshot)

        if not keep_browser and access_confirmed:
            _hide_browser(driver)

        status = "success" if access_confirmed else "warning"
        return {
            "status": status,
            "auth_url": auth_url,
            "verify_url": verify_url,
            "institution": institution,
            "institution_selected": selected_institution,
            "already_authenticated": False,
            "access_confirmed": access_confirmed,
            "cookies_saved": cookies_saved,
            "cookie_file": str(_cookie_path()),
            "profile_dir": str(_profile_dir()),
            "direct_domains": direct_domain_list,
            "diagnostics": diagnostics,
            "debug_snapshot": snapshot_file,
            "message": (
                "已保存知网浏览器会话；后续 CNKI 命令会复用该 profile/cookies。"
                if access_confirmed else
                "已保存当前浏览器状态，但未确认机构权限；请检查浏览器是否仍停留在学校登录或验证页。"
            ),
            "next_step": (
                "运行 search --source cnki 或 batch-search。"
                if access_confirmed else
                "保留浏览器完成登录后重试 auth-cnki，或增加 --wait-seconds / --direct-domain。"
            ),
        }
    except Exception as e:
        return {
            "status": "error",
            "code": "CNKI_AUTH_FAILED",
            "message": str(e),
            "auth_url": auth_url,
            "verify_url": verify_url,
            "institution": institution,
            "captcha_timeout": captcha_timeout,
            "direct_domains": direct_domain_list,
        }
    finally:
        _restore_direct_domains(previous_direct_domains)
        if driver is not None and not keep_browser:
            _release_driver(driver)


# ── 验证码 / 安全验证页 ─────────────────────────────────

def _is_cnki_security_gate(driver: "webdriver.Remote") -> bool:
    """知网在访问检索页前可能跳转到滑块等安全验证页。
    仅通过 URL 和页面标题判断，不检查 body text（正常页面的页脚/脚本
    常含"安全验证"等词汇，会导致误报）。
    """
    try:
        url = driver.current_url or ""
        title = (driver.title or "").strip()
    except Exception:
        return False
    if "/verify/" in url:
        return True
    gate_keywords = ("安全验证", "安全检查", "请完成验证", "人机验证")
    for kw in gate_keywords:
        if kw in title:
            return True
    return False


def _show_browser_for_captcha(
    driver: "webdriver.Remote",
    prompt: str = "知网要求安全验证，已弹出浏览器窗口，请完成验证...",
    poll_timeout: int = 180,
) -> "webdriver.Remote":
    """将浏览器移回屏幕让用户完成验证。
    验证通过后自动最小化到任务栏并保存 cookies。
    """
    _show_browser(driver)

    initial_url = driver.current_url or ""
    was_gate = "/verify/" in initial_url

    _log(f"\n[!] {prompt}")
    _log(f"    正在自动等待验证完成（最长 {poll_timeout} 秒）...\n")

    deadline = time.time() + poll_timeout
    poll_count = 0
    clean_count = 0
    verified = False

    while time.time() < deadline:
        time.sleep(3)
        poll_count += 1
        try:
            current_url = driver.current_url or ""

            if was_gate and "/verify/" not in current_url:
                _log(f"[OK] 已离开验证页 → {current_url[:80]}")
                verified = True
                break

            on_gate = _is_cnki_security_gate(driver)
            has_elements = _has_captcha_elements(driver)

            if poll_count % 5 == 0:
                _log(f"[cnki] 轮询 #{poll_count}: gate={on_gate}, elements={has_elements}")

            if not on_gate and not has_elements:
                clean_count += 1
                if clean_count >= 2:
                    _log("[OK] 连续无验证码检出，验证完成")
                    verified = True
                    break
            else:
                clean_count = 0
        except Exception as e:
            _log(f"[cnki] 轮询异常: {e}")

    _save_cookies(driver)
    if verified:
        _hide_browser(driver)
        time.sleep(1)
        _log("[cnki] 浏览器已最小化到任务栏，继续执行；如需查看状态可从任务栏恢复窗口...")
        return driver

    _show_browser(driver)
    raise RuntimeError(
        "知网人机验证超时，浏览器窗口已保持在前台。"
        "请先在该窗口完成验证，然后重新执行命令。"
    )


def _has_captcha_elements(driver: "webdriver.Remote") -> bool:
    """仅通过 CSS 选择器检测页面是否存在可见的验证码元素。
    不做文本匹配，适用于轮询退出判断（避免文本误报导致轮询不退出）。
    临时关闭 implicit_wait 避免每个选择器等待 3 秒（14 个选择器 × 3s = 42s 延迟）。
    """
    try:
        driver.implicitly_wait(0)
        captcha_selectors = [
            "#verify_pic", ".verify-img", "#CheckCodeImg",
            ".slider-verify", ".verify-slide", ".slide-verify",
            "[class*='captcha']", "[id*='captcha']",
            "[class*='verify'][class*='slide']",
            ".geetest_panel", ".geetest_holder",
            "#tcaptcha_iframe", ".tcaptcha-popup",
            ".nc-container", ".nc_wrapper",
        ]
        for sel in captcha_selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed():
                        return True
            except Exception:
                continue
        return False
    except Exception:
        return False
    finally:
        try:
            driver.implicitly_wait(3)
        except Exception:
            pass


def _has_any_captcha(driver: "webdriver.Remote") -> bool:
    """广泛检测页面上是否存在任何形式的验证码/人机验证（含文本匹配，用于首次检测）。
    临时关闭 implicit_wait 避免 14 × 3s = 42s 延迟。
    """
    try:
        driver.implicitly_wait(0)
        captcha_selectors = [
            "#verify_pic", ".verify-img", "#CheckCodeImg",
            ".slider-verify", ".verify-slide", ".slide-verify",
            "[class*='captcha']", "[id*='captcha']",
            "[class*='verify'][class*='slide']",
            ".geetest_panel", ".geetest_holder",
            "#tcaptcha_iframe", ".tcaptcha-popup",
            ".nc-container", ".nc_wrapper",
        ]
        for sel in captcha_selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed():
                        return True
            except Exception:
                continue

        body_text = driver.execute_script(
            "return (document.body && document.body.innerText || '').substring(0, 3000);")
        captcha_texts = ("滑动验证", "请向右滑动", "安全验证", "验证码",
                         "人机验证", "请完成验证", "点击验证", "拖动滑块")
        for kw in captcha_texts:
            if kw in body_text:
                return True
        return False
    except Exception:
        return False
    finally:
        try:
            driver.implicitly_wait(3)
        except Exception:
            pass


def _handle_captcha(driver: "webdriver.Remote") -> "webdriver.Remote":
    """检测验证码或安全验证页。仅在确认存在真实验证码时弹窗。

    检测策略（从严到宽）：
    1. _is_cnki_security_gate: URL 含 /verify/ 或页面标题含安全验证关键词 → 一定是验证页
    2. _has_captcha_elements: DOM 中有可见的验证码元素（滑块、图片等） → 内联验证码
    不使用文本匹配（_has_any_captcha），因为知网正常页面也含有"验证"相关词汇，会误报。
    """
    try:
        if _is_cnki_security_gate(driver):
            _log("[cnki] 检测到知网安全验证页，弹出浏览器窗口...")
            return _show_browser_for_captcha(
                driver,
                "知网安全验证（如滑块），请在浏览器中完成...",
            )

        if _has_captcha_elements(driver):
            _log("[cnki] 页面出现验证码元素，弹出浏览器窗口...")
            return _show_browser_for_captcha(
                driver,
                "知网出现验证码，请在浏览器中完成...",
            )

        return driver

    except Exception as e:
        try:
            driver.title
            return driver
        except Exception:
            raise RuntimeError(f"验证码处理失败且浏览器会话已丢失: {e}")
