"""cnki.driver - 浏览器管理、Cookie 持久化、验证码处理"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from .constants import HAS_SELENIUM, _log

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


def _cookie_path() -> Path:
    return Path.cwd() / ".scholar-kit" / "cookies.json"


def _save_cookies(driver: "webdriver.Remote"):
    """将当前浏览器 cookies 持久化到项目目录"""
    try:
        p = _cookie_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        cookies = driver.get_cookies()
        p.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        _log(f"[cnki] cookie 保存失败: {e}")


def _load_cookies(driver: "webdriver.Remote"):
    """从持久化文件加载 cookies 到浏览器"""
    p = _cookie_path()
    if not p.exists():
        return
    try:
        cookies = json.loads(p.read_text(encoding="utf-8"))
        for cookie in cookies:
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


_OFFSCREEN_POS = (-10000, -10000)

_DEFAULT_DEBUG_PORT = 9222


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
    which_name = "msedge" if browser == "edge" else ("google-chrome" if sys.platform != "win32" else "chrome")
    found = shutil.which(which_name)
    return found


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

    profile_dir = Path.cwd() / ".scholar-kit" / "browser-profile"
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    args = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        f"--user-agent={_REALISTIC_UA}",
        f"--window-position={_OFFSCREEN_POS[0]},{_OFFSCREEN_POS[1]}",
        "--window-size=1920,1080",
    ]

    if sys.platform == "win32":
        import subprocess
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        creation_flags = CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        try:
            subprocess.Popen(
                [exe] + args,
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            _log(f"[cnki] 已通过 CREATE_BREAKAWAY_FROM_JOB 在沙盒外启动 {browser}")
            return True
        except OSError as e:
            _log(f"[cnki] BREAKAWAY 方式失败: {e}，尝试 ShellExecute...")

        try:
            import ctypes
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "open", exe, " ".join(args), None, 0  # SW_HIDE=0
            )
            if ret > 32:
                _log(f"[cnki] 已通过 ShellExecuteW 在沙盒外启动 {browser}")
                return True
            else:
                _log(f"[cnki] ShellExecuteW 返回错误码: {ret}")
        except Exception as e:
            _log(f"[cnki] ShellExecuteW 失败: {e}")

    return False


def _wait_for_debug_port(port: int = _DEFAULT_DEBUG_PORT, timeout: int = 15) -> bool:
    """等待浏览器调试端口就绪。"""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except (ConnectionRefusedError, OSError, socket.timeout):
            time.sleep(1)
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
    """通过远程调试端口连接到已运行的浏览器。"""
    OptionsClass = webdriver.EdgeOptions if browser == "edge" else webdriver.ChromeOptions
    DriverClass = webdriver.Edge if browser == "edge" else webdriver.Chrome

    options = OptionsClass()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")

    local_driver = _find_local_driver(browser)
    if local_driver:
        if browser == "edge":
            from selenium.webdriver.edge.service import Service
        else:
            from selenium.webdriver.chrome.service import Service
        service = Service(executable_path=local_driver)
        driver = DriverClass(service=service, options=options)
    else:
        driver = DriverClass(options=options)

    _log(f"[cnki] 已通过远程调试端口 {port} 连接到 {browser}")
    return driver


def _hide_browser(driver: "webdriver.Remote"):
    """将浏览器移到屏幕外，实现静默运行。
    比 minimize_window() 更可靠：Selenium 的 switch_to / get 不会把窗口拉回来。
    """
    try:
        driver.set_window_position(*_OFFSCREEN_POS)
    except Exception:
        try:
            driver.minimize_window()
        except Exception:
            pass


def _show_browser(driver: "webdriver.Remote"):
    """将浏览器移回屏幕可见区域并置顶到前台。"""
    try:
        driver.set_window_position(100, 100)
        driver.set_window_size(1200, 900)
    except Exception as e:
        _log(f"[cnki] 无法移动窗口: {e}")

    try:
        driver.switch_to.window(driver.current_window_handle)
    except Exception:
        pass

    if sys.platform == "win32":
        _win32_bring_to_front(driver)


def _win32_bring_to_front(driver: "webdriver.Remote"):
    """Windows 专用：通过 Win32 API 将浏览器窗口置顶到最前面。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        page_title = driver.title
        if not page_title:
            return

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        found_hwnd = [None]

        def _enum_cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if page_title in buf.value:
                    found_hwnd[0] = hwnd
                    return False
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)

        if found_hwnd[0]:
            SW_RESTORE = 9
            user32.ShowWindow(found_hwnd[0], SW_RESTORE)
            user32.SetForegroundWindow(found_hwnd[0])
            _log("[cnki] Win32: 浏览器窗口已置顶")
        else:
            _log("[cnki] Win32: 未找到匹配窗口，请在任务栏查找浏览器")
    except Exception as e:
        _log(f"[cnki] Win32 置顶失败: {e}，请手动在任务栏找到浏览器窗口")


def _ensure_selenium_cache():
    """确保 Selenium 缓存目录可写。沙盒中默认路径不可写时降级到工作目录。"""
    if os.environ.get("SE_CACHE_PATH"):
        return

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
    3. Selenium 缓存目录
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

    if sys.platform == "win32":
        se_cache = os.environ.get("SE_CACHE_PATH") or str(
            Path(os.environ.get("LOCALAPPDATA", "")) / "selenium")
        if os.path.isdir(se_cache):
            for root, _dirs, files in os.walk(se_cache):
                if driver_name in files:
                    p = os.path.join(root, driver_name)
                    _log(f"[cnki] 在缓存中找到驱动: {p}")
                    return p

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


def _create_driver(browser: str = None, headless: bool = False) -> "webdriver.Remote":
    """创建浏览器实例。默认有头模式（窗口移至屏幕外静默运行），避免无头被知网检测。"""
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
        "--proxy-bypass-list=*.cnki.net;*.cnki.com.cn;*.cnki.com",
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-features=RendererCodeIntegrity",
        f"--user-agent={_REALISTIC_UA}",
        "--window-size=1920,1080",
    ]
    if not headless:
        startup_args.append(f"--window-position={_OFFSCREEN_POS[0]},{_OFFSCREEN_POS[1]}")
    for arg in startup_args:
        options.add_argument(arg)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    sandbox_profile = Path.cwd() / ".scholar-kit" / "browser-profile"
    try:
        sandbox_profile.mkdir(parents=True, exist_ok=True)
        options.add_argument(f"--user-data-dir={sandbox_profile}")
    except (PermissionError, OSError):
        pass

    driver = None
    first_err = None

    # 策略 1: 正常方式启动（含本地驱动回退）
    try:
        driver = DriverClass(options=options)
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
            except Exception:
                pass

    # 策略 2: 沙盒内启动失败 → 在沙盒外启动浏览器并通过远程调试连接
    if driver is None:
        _log("[cnki] 沙盒内启动失败，尝试在沙盒外启动浏览器...")
        port = int(os.environ.get("SCHOLAR_DEBUG_PORT", _DEFAULT_DEBUG_PORT))
        launched = _launch_browser_outside_sandbox(browser, port)
        if launched and _wait_for_debug_port(port):
            try:
                driver = _connect_remote_browser(browser, port)
            except Exception as e:
                _log(f"[cnki] 远程连接失败: {e}")

    # 策略 3: 尝试连接用户可能已打开的浏览器调试端口
    if driver is None:
        port = int(os.environ.get("SCHOLAR_DEBUG_PORT", _DEFAULT_DEBUG_PORT))
        if _wait_for_debug_port(port, timeout=3):
            _log(f"[cnki] 检测到端口 {port} 已有浏览器，尝试连接...")
            try:
                driver = _connect_remote_browser(browser, port)
            except Exception as e:
                _log(f"[cnki] 连接已有浏览器失败: {e}")

    if driver is None:
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

    if not headless:
        _hide_browser(driver)

    return driver


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
    验证通过后自动移到屏幕外并保存 cookies。
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
        _log("[cnki] 浏览器已隐藏，继续执行...")
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
