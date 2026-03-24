"""cnki.driver - 浏览器管理、Cookie 持久化、验证码处理"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Tuple

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
    """将浏览器移回屏幕可见区域（用于验证码等需要人工操作的场景）。"""
    try:
        driver.set_window_position(0, 0)
        driver.set_window_size(1200, 900)
    except Exception as e:
        _log(f"[cnki] 警告：无法将浏览器移至可见区域: {e}，请手动在任务栏找到浏览器窗口")


def _create_driver(browser: str = None, headless: bool = False) -> "webdriver.Remote":
    """创建浏览器实例。默认有头模式（窗口移至屏幕外静默运行），避免无头被知网检测。"""
    if not HAS_SELENIUM:
        raise RuntimeError(
            "selenium 未安装。请运行: pip install selenium>=4.10\n"
            "Selenium 4.10+ 会自动管理 WebDriver，无需手动下载。"
        )

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
        f"--user-agent={_REALISTIC_UA}",
        "--window-size=1920,1080",
    ]
    if not headless:
        startup_args.append(f"--window-position={_OFFSCREEN_POS[0]},{_OFFSCREEN_POS[1]}")
    for arg in startup_args:
        options.add_argument(arg)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    driver = DriverClass(options=options)

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
    """检测当前网络是否有知网访问权限（绕过代理直连）"""
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
    except Exception as e:
        return False, f"知网不可访问: {e}"


# ── 验证码 / 安全验证页 ─────────────────────────────────

def _is_cnki_security_gate(driver: "webdriver.Remote") -> bool:
    """知网在访问检索页前可能跳转到滑块等安全验证页。"""
    try:
        url = driver.current_url or ""
        title = (driver.title or "").strip()
    except Exception:
        return False
    if "/verify/" in url:
        return True
    if "安全验证" in title:
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

    _log(f"\n[!] {prompt}")
    _log(f"    正在自动等待验证完成（最长 {poll_timeout} 秒）...\n")

    deadline = time.time() + poll_timeout
    poll_count = 0
    while time.time() < deadline:
        time.sleep(3)
        poll_count += 1
        try:
            still_on_gate = _is_cnki_security_gate(driver)
            has_captcha = bool(driver.find_elements(
                By.CSS_SELECTOR, "#verify_pic, .verify-img, #CheckCodeImg"
            ))
            has_slider = bool(driver.execute_script(
                "return document.body && ("
                "document.body.innerText.indexOf('滑动验证') >= 0 || "
                "document.body.innerText.indexOf('继续阅读全文') >= 0)"
            ))
            if poll_count % 5 == 0:
                _log(f"[cnki] 轮询 #{poll_count}: gate={still_on_gate}, captcha={has_captcha}, slider={has_slider}")
            if not still_on_gate and not has_captcha and not has_slider:
                _log("[OK] 验证完成，浏览器移至后台...")
                _save_cookies(driver)
                _hide_browser(driver)
                time.sleep(1)
                _log("[cnki] 浏览器已隐藏，继续执行...")
                return driver
        except Exception as e:
            _log(f"[cnki] 轮询异常: {e}")

    raise RuntimeError(
        f"等待验证超时（{poll_timeout}秒），请完成浏览器中的验证后重新运行命令"
    )


def _handle_captcha(driver: "webdriver.Remote") -> "webdriver.Remote":
    """检测验证码或安全验证页。浏览器默认已是有头模式，直接弹出窗口让用户操作。"""
    try:
        if _is_cnki_security_gate(driver):
            _log("[cnki] 检测到知网安全验证页，弹出浏览器窗口...")
            return _show_browser_for_captcha(
                driver,
                "知网安全验证（如滑块），请在浏览器中完成...",
            )

        captcha = driver.find_elements(By.CSS_SELECTOR, "#verify_pic, .verify-img, #CheckCodeImg")
        if not captcha:
            return driver

        _log("[cnki] 知网出现验证码，弹出浏览器窗口...")
        return _show_browser_for_captcha(
            driver,
            "知网出现验证码，请在浏览器中完成...",
        )

    except Exception as e:
        try:
            driver.title
            return driver
        except Exception:
            raise RuntimeError(f"验证码处理失败且浏览器会话已丢失: {e}")
