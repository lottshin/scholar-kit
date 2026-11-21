"""cnki.download - 论文下载与导出文件解析"""
from __future__ import annotations

import json
import math
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .constants import (
    HAS_SELENIUM, REQUEST_INTERVAL, _log,
)
from .driver import (
    _detect_browser,
    _create_driver,
    _detect_cnki_login_wall,
    _load_cookies,
    _navigate_within_cnki,
    _prepare_cnki_session,
    _save_cookies,
    _handle_captcha,
    check_cnki_access,
    _release_driver,
    _safe_get,
)

def _get_batch_window_size() -> int:
    try:
        from config import get as cfg_get
        val = cfg_get("batch_window_size", 2)
    except ImportError:
        val = os.environ.get("SCHOLAR_BATCH_WINDOW_SIZE", "2")
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 2

BATCH_WINDOW_SIZE = _get_batch_window_size()
COOLDOWN_MIN = 4
COOLDOWN_MAX = 8


def _enable_download_behavior(driver, abs_save_dir: str) -> None:
    """在当前浏览器会话与当前标签页同时绑定下载目录（兼容多标签与远程调试连接）。"""
    for cmd, params in (
        ("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": abs_save_dir, "eventsEnabled": True}),
        ("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": abs_save_dir}),
    ):
        try:
            driver.execute_cdp_cmd(cmd, params)
        except Exception:
            pass


def _split_evenly(items: list, max_per_batch: int) -> list[list]:
    """将 items 均匀分成若干批，每批不超过 max_per_batch。"""
    total = len(items)
    if total == 0:
        return []
    n_batches = math.ceil(total / max_per_batch)
    base, remainder = divmod(total, n_batches)
    batches, start = [], 0
    for i in range(n_batches):
        size = base + (1 if i < remainder else 0)
        batches.append(items[start:start + size])
        start += size
    return batches


if HAS_SELENIUM:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException,
    )


def _match_by_title(title: str, candidates: list[str], claimed: set[str]) -> Optional[str]:
    """启发式：用标题前 20 字符与文件名做字符重叠率匹配（>50% 判定命中）。
    CNKI 下载文件名通常包含论文标题，因此多数情况有效；
    若文件名为纯数字编号等极端情况则会漏匹配，由调用方的顺序绑定兜底。"""
    if not title:
        return None
    title_clean = re.sub(r'[\s\-_：:""''【】（）()]+', '', title).lower()
    if len(title_clean) < 4:
        return None
    best, best_ratio = None, 0
    for f in candidates:
        if f in claimed:
            continue
        fname_clean = re.sub(r'[\s\-_.]', '', Path(f).stem).lower()
        if not fname_clean:
            continue
        overlap = sum(1 for c in title_clean[:20] if c in fname_clean)
        ratio = overlap / min(len(title_clean[:20]), len(fname_clean)) if fname_clean else 0
        if ratio > best_ratio:
            best_ratio = ratio
            best = f
    return best if best_ratio > 0.5 else None


# ── 下载 ──────────────────────────────────────────────

def _get_title(driver) -> str:
    """从详情页提取标题，带降级策略：h1 文本 → driver.title → 空"""
    title = ""
    try:
        title_el = WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "h1, .wx-tit h1"))
        )
        title = (title_el.text or "").strip()
    except (TimeoutException, NoSuchElementException):
        pass
    if not title:
        raw = (driver.title or "").strip()
        for suffix in (" - 中国知网", " - CNKI", " - 知网"):
            if raw.endswith(suffix):
                raw = raw[: -len(suffix)].strip()
                break
        if raw and raw not in ("中国知网", "CNKI", "知网", ""):
            title = raw
    return title


def _click_download_btn(driver, file_format: str = "pdf") -> Optional[str]:
    """点击下载按钮，成功返回实际触发的格式；失败返回 None。

    知网不同页面（期刊 vs 学位论文）的按钮 ID/结构不一致，
    因此先按 ID 找，再按链接文字找，确保都能命中。
    """
    from .selectors import CNKI_SELECTORS

    dl_sel = CNKI_SELECTORS["download"]
    btn_id = dl_sel["pdf_btn_id"] if file_format == "pdf" else dl_sel["caj_btn_id"]
    btn_text = "PDF下载" if file_format == "pdf" else "CAJ下载"

    btn_selectors = [
        (By.ID, btn_id),
        (By.CSS_SELECTOR, "a#{}".format(btn_id)),
        (By.CSS_SELECTOR, "a.{}-download".format(file_format)),
    ]
    for by, selector in btn_selectors:
        try:
            btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((by, selector))
            )
            btn.click()
            return file_format
        except (TimeoutException, NoSuchElementException):
            continue

    try:
        links = driver.find_elements(By.CSS_SELECTOR, "a")
        for link in links:
            text = (link.text or "").strip()
            if text == btn_text:
                link.click()
                return file_format
            href = link.get_attribute("href") or ""
            if "download" in href and btn_text[:3] in text:
                link.click()
                return file_format
    except Exception:
        pass

    return None


def _check_post_download_auth_wall(driver, pre_handles: set, keep_window: str) -> Optional[str]:
    """点击下载按钮后快速检查是否弹出了知网登录/付费拦截标签页（避免无权限时傻等 180s 超时）。"""
    time.sleep(1.8)
    try:
        cur_handles = list(driver.window_handles or [])
    except Exception:
        return None

    extra_handles = [h for h in cur_handles if h not in pre_handles]
    reason: Optional[str] = None
    for h in extra_handles:
        try:
            driver.switch_to.window(h)
            hit = _detect_cnki_login_wall(driver)
            if hit:
                reason = hit
                try:
                    driver.close()
                except Exception:
                    pass
        except Exception:
            continue

    try:
        if keep_window in (driver.window_handles or []):
            driver.switch_to.window(keep_window)
            if not reason:
                reason = _detect_cnki_login_wall(driver)
    except Exception:
        pass
    return reason


def _format_save_dir(save_dir: str, file_format: str, separate_formats: bool = True) -> str:
    if not separate_formats:
        return save_dir
    return str(Path(save_dir) / file_format.lower())


def _heal_expired_download_url(
    driver,
    current_url: str,
    hint: Optional[Dict[str, Any]] = None,
) -> str:
    """当知网加密详情链接 (`kcms2?v=...`) 隔天失效返回 404/错误页时，利用会话题名与期刊自动重定位新 URL。"""
    if not hint or not hint.get("title"):
        return current_url
    try:
        from .detail import _is_cnki_error_or_404
        if not _is_cnki_error_or_404(driver):
            return current_url
        from .search import refresh_cnki_url
        _log(f"[cnki-download] 详情链接已过期，正在按题名自动重定位: {hint.get('title', '')[:36]}...")
        refreshed = refresh_cnki_url(
            driver,
            title=hint.get("title", ""),
            author=hint.get("authors", ""),
            journal=hint.get("journal", ""),
        )
        if refreshed:
            hint["url"] = refreshed
            _navigate_within_cnki(driver, refreshed)
            _handle_captcha(driver)
            return refreshed
    except Exception as e:
        _log(f"[cnki-download] 过期链接自动重定位失败: {e}")
    return current_url


def _open_download_tab(driver, url: str, abs_save_dir: str) -> Optional[str]:
    """使用原生 Target.createTarget + 带 Referer 导航打开下载详情标签页，免疫浏览器弹窗拦截。"""
    handles_before = set(getattr(driver, "window_handles", []) or [])
    switch_to = getattr(driver, "switch_to", None)
    if switch_to is not None and hasattr(switch_to, "new_window"):
        try:
            switch_to.new_window("tab")
            new_handle = driver.current_window_handle
            _enable_download_behavior(driver, abs_save_dir)
            navigated = False
            if hasattr(driver, "execute_cdp_cmd"):
                try:
                    driver.execute_cdp_cmd(
                        "Page.navigate",
                        {"url": url, "referrer": "https://kns.cnki.net/"},
                    )
                    navigated = True
                except Exception:
                    navigated = False
            if not navigated:
                _navigate_within_cnki(driver, url, base_delay=0.5)
            return new_handle
        except Exception:
            pass

    try:
        driver.execute_script("window.open('');")
        new_handles = set(getattr(driver, "window_handles", []) or []) - handles_before
        if new_handles:
            new_handle = new_handles.pop()
            driver.switch_to.window(new_handle)
            _enable_download_behavior(driver, abs_save_dir)
            _safe_get(driver, url)
            return new_handle
    except Exception:
        pass
    return None


def download_cnki(
    url: str,
    save_dir: str = "./papers",
    file_format: str = "pdf",
    fallback_format: Optional[str] = None,
    separate_formats: bool = True,
    expected_title: str = "",
    expected_journal: str = "",
) -> Dict[str, Any]:
    """
    从知网下载单篇论文。

    Args:
        url:         知网论文详情页 URL
        save_dir:    保存目录
        file_format: 下载格式 (pdf/caj)

    Returns:
        下载结果信息
    """
    requested_format = (file_format or "pdf").lower()
    fallback_format = (fallback_format or "").lower() or None
    active_save_dir = _format_save_dir(save_dir, requested_format, separate_formats)
    os.makedirs(active_save_dir, exist_ok=True)
    abs_save_dir = os.path.abspath(active_save_dir)

    driver = None
    try:
        browser = _detect_browser()
        driver = _create_driver(browser=browser)

        _prepare_cnki_session(driver)

        _enable_download_behavior(driver, abs_save_dir)

        _navigate_within_cnki(driver, url)
        driver = _handle_captcha(driver)
        if expected_title:
            url = _heal_expired_download_url(
                driver,
                url,
                {"title": expected_title, "journal": expected_journal},
            )

        title = _get_title(driver)
        snapshot = set(os.listdir(active_save_dir))
        main_win = driver.current_window_handle
        pre_click_handles = set(driver.window_handles or [])

        actual_format = _click_download_btn(driver, requested_format)
        fallback_used = False
        if not actual_format and fallback_format:
            fallback_save_dir = _format_save_dir(save_dir, fallback_format, separate_formats)
            os.makedirs(fallback_save_dir, exist_ok=True)
            active_save_dir = fallback_save_dir
            abs_save_dir = os.path.abspath(active_save_dir)
            _enable_download_behavior(driver, abs_save_dir)
            snapshot = set(os.listdir(active_save_dir))
            pre_click_handles = set(driver.window_handles or [])
            actual_format = _click_download_btn(driver, fallback_format)
            fallback_used = bool(actual_format)

        if not actual_format:
            return {"status": "error", "code": "DOWNLOAD_BTN_NOT_FOUND",
                    "message": "未找到{}下载按钮".format(requested_format),
                    "requested_format": requested_format,
                    "fallback_format": fallback_format}

        auth_err = _check_post_download_auth_wall(driver, pre_click_handles, main_win)
        if auth_err:
            return {
                "status": "error",
                "code": "CNKI_AUTH_REQUIRED",
                "title": title,
                "message": auth_err,
                "requested_format": requested_format,
            }

        downloaded = _wait_for_download(active_save_dir, timeout=180, before=snapshot)

        if downloaded:
            _log(f"[cnki-download] 下载完成: {downloaded}")
            return {
                "status": "success",
                "title": title,
                "save_dir": active_save_dir,
                "filename": downloaded,
                "format": actual_format,
                "requested_format": requested_format,
                "fallback_used": fallback_used,
            }
        else:
            return {
                "status": "warning",
                "code": "DOWNLOAD_TIMEOUT",
                "title": title,
                "save_dir": active_save_dir,
                "message": "下载可能仍在进行中，请检查目录",
                "format": actual_format,
                "requested_format": requested_format,
                "fallback_used": fallback_used,
            }

    except Exception as e:
        return {"status": "error", "code": "CNKI_DOWNLOAD_FAILED",
                "message": str(e)}
    finally:
        if driver is not None:
            _release_driver(driver)


def _cleanup_orphan_tabs(driver, safe_handles: set, main_window: str):
    """关闭所有不在 safe_handles 中的标签页，最后切回 main_window"""
    try:
        for h in driver.window_handles:
            if h not in safe_handles:
                try:
                    driver.switch_to.window(h)
                    driver.close()
                except Exception:
                    pass
        driver.switch_to.window(main_window)
    except Exception:
        pass


def _is_already_downloaded(title: str, save_dir: str) -> bool:
    """检查目录中是否已存在与标题匹配的文件（断点续传）。
    比 _match_by_title 更严格：要求标题前 20 字符作为连续子串出现在文件名中，
    避免同领域论文共享公共短语（如"建构XX知识体系"）导致误判。"""
    if not title:
        return False
    title_clean = re.sub(r'[\s\-_：:""''【】（）()]+', '', title).lower()
    if len(title_clean) < 6:
        return False
    core = title_clean[:min(len(title_clean), 20)]
    for f in os.listdir(save_dir):
        fname_clean = re.sub(r'[\s\-_.]', '', Path(f).stem).lower()
        if core in fname_clean:
            return True
    return False


def _save_download_progress(progress_file: Path, completed_urls: set):
    try:
        progress_file.write_text(
            json.dumps({"completed": list(completed_urls)}, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception:
        pass


def _trigger_batch_window(
    driver, urls_window: List[str], save_dir: str, file_format: str,
    global_idx_offset: int, total: int,
    paper_hints: Optional[Dict[str, Dict[str, Any]]] = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """并行加载批次：先开 N 个标签页让页面同时加载，再逐个切回去点下载。
    Phase B 中途异常时 break 并把剩余 URL 推入重试，同时清理所有孤儿标签页。
    返回 (ok_results, batch_errors, retry_urls)"""

    main_window = driver.current_window_handle
    batch_meta: List[Dict[str, Any]] = []
    batch_errors = []
    retry_urls = []
    snapshot_before = set(os.listdir(save_dir))
    abs_save_dir = os.path.abspath(save_dir)

    tab_handles: List[tuple[str, str, int]] = []
    handles_before = set(driver.window_handles)

    for i, url in enumerate(urls_window):
        idx = global_idx_offset + i + 1
        _log(f"[cnki-download] [{idx}/{total}] 打开详情页: {url[:80]}...")
        orphan_handle = None
        try:
            orphan_handle = _open_download_tab(driver, url, abs_save_dir)
            if not orphan_handle:
                _log(f"[cnki-download] [{idx}/{total}] 未能打开新标签")
                batch_errors.append({"url": url, "code": "CNKI_DOWNLOAD_FAILED", "error": "无法打开新标签页"})
                continue
            tab_handles.append((url, orphan_handle, idx))
            orphan_handle = None
            if i < len(urls_window) - 1:
                time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            _log(f"[cnki-download] [{idx}/{total}] 开标签失败: {e}")
            batch_errors.append({"url": url, "code": "CNKI_DOWNLOAD_FAILED", "error": str(e)})
            retry_urls.append(url)
            if orphan_handle:
                try:
                    driver.switch_to.window(orphan_handle)
                    driver.close()
                except Exception:
                    pass
            try:
                driver.switch_to.window(main_window)
            except Exception:
                pass

    if tab_handles:
        time.sleep(REQUEST_INTERVAL)

    for ti, (url, handle, idx) in enumerate(tab_handles):
        try:
            driver.switch_to.window(handle)
            driver = _handle_captcha(driver)
            if paper_hints and url in paper_hints:
                _heal_expired_download_url(driver, url, paper_hints.get(url))

            title = _get_title(driver)
            _log(f"[cnki-download] [{idx}/{total}] 标题: {title or '(未获取)'}")

            if _is_already_downloaded(title, save_dir):
                _log(f"[cnki-download] [{idx}/{total}] 已存在，跳过")
                batch_meta.append({"url": url, "title": title, "skipped": True})
                driver.close()
                driver.switch_to.window(main_window)
                continue

            pre_click = set(driver.window_handles or [])
            if not _click_download_btn(driver, file_format):
                err = f"未找到{file_format}下载按钮"
                _log(f"[cnki-download] [{idx}/{total}] {err}")
                batch_errors.append({"url": url, "title": title,
                                     "code": "DOWNLOAD_BTN_NOT_FOUND", "error": err})
                retry_urls.append(url)
                driver.close()
                driver.switch_to.window(main_window)
                continue

            auth_err = _check_post_download_auth_wall(driver, pre_click, handle)
            if auth_err:
                _log(f"[cnki-download] [{idx}/{total}] {auth_err}")
                driver._cnki_download_auth_blocked = auth_err
                batch_errors.append({
                    "url": url,
                    "title": title,
                    "code": "CNKI_AUTH_REQUIRED",
                    "error": auth_err,
                })
                _cleanup_orphan_tabs(driver, handles_before, main_window)
                break

            batch_meta.append({"url": url, "title": title})
            _log(f"[cnki-download] [{idx}/{total}] 下载已触发")
            time.sleep(random.uniform(0.6, 1.4))

            driver.close()
            driver.switch_to.window(main_window)

        except Exception as e:
            _log(f"[cnki-download] [{idx}/{total}] 失败: {e}")
            batch_errors.append({"url": url, "code": "CNKI_DOWNLOAD_FAILED", "error": str(e)})
            retry_urls.append(url)
            remaining = tab_handles[ti + 1:]
            for rem_url, _, rem_idx in remaining:
                _log(f"[cnki-download] [{rem_idx}/{total}] 因批次异常推迟至重试")
                retry_urls.append(rem_url)
            _cleanup_orphan_tabs(driver, handles_before, main_window)
            break

    triggered = [m for m in batch_meta if "skipped" not in m]
    if not triggered:
        ok_results = [m for m in batch_meta if m.get("skipped")]
        for r in ok_results:
            r["format"] = file_format
            r.pop("skipped", None)
            r["filename"] = "(already exists)"
        return ok_results, batch_errors, retry_urls

    per_file_timeout = 30
    wait_timeout = max(60, len(triggered) * per_file_timeout)
    _log(f"[cnki-download] 等待本批 {len(triggered)} 个文件落盘（超时 {wait_timeout}s）...")

    _partial_suffixes = (".crdownload", ".tmp", ".part")
    deadline = time.time() + wait_timeout
    claimed: set[str] = set()

    while time.time() < deadline:
        current = set(os.listdir(save_dir))
        all_new = current - snapshot_before - claimed
        all_stable = [f for f in all_new if not f.endswith(_partial_suffixes)]

        all_done = True
        for meta in triggered:
            if "filename" in meta:
                continue
            all_done = False
            title = meta.get("title", "")
            if title and all_stable:
                title_match = _match_by_title(title, all_stable, claimed)
                if title_match:
                    meta["filename"] = title_match
                    claimed.add(title_match)
                    _log(f"[cnki-download] 标题匹配: {title[:30]} → {title_match}")

        if all_done:
            break

        unmatched = [m for m in triggered if "filename" not in m]
        unclaimed = [f for f in all_stable if f not in claimed]
        if len(unmatched) == 1 and len(unclaimed) == 1:
            m, f = unmatched[0], unclaimed[0]
            m["filename"] = f
            claimed.add(f)
            _log(f"[cnki-download] 唯一匹配: {m.get('title', '')[:30]} → {f}")
            break

        time.sleep(3)

    ok_results = []
    for meta in batch_meta:
        meta["format"] = file_format
        if meta.get("skipped"):
            meta.pop("skipped", None)
            meta["filename"] = "(already exists)"
            ok_results.append(meta)
        elif "filename" in meta:
            ok_results.append(meta)
        else:
            batch_errors.append({
                "url": meta["url"],
                "title": meta.get("title", ""),
                "code": "DOWNLOAD_TIMEOUT",
                "error": "下载等待超时",
            })
            retry_urls.append(meta["url"])

    return ok_results, batch_errors, retry_urls


def batch_download_cnki(
    urls: List[str],
    save_dir: str = "./papers",
    file_format: str = "pdf",
    separate_formats: bool = True,
    _driver=None,
    paper_hints: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    批量下载知网论文 — 均匀分批 + 并行加载 + 反爬冷却 + 断点续传 + 失败重试。

    策略：
      1. URL 去重后均匀分批（每批不超过 BATCH_WINDOW_SIZE，各批大小差至多 1）
      2. 每批：一口气开 N 个标签页（页面并行加载）→ 逐个点下载 → 等本批全部落盘
      3. 批间随机休息 COOLDOWN_MIN~COOLDOWN_MAX 秒
      4. 已下载的论文（标题匹配已有文件）自动跳过
      5. 全部批次完成后，对失败的 URL 统一重试一次

    Args:
        _driver: 外部传入的浏览器实例（由 search_cnki 复用），为 None 时自行创建。
                 传入时跳过浏览器启动、网络检测和验证码，调用方负责 quit。
    """
    if not urls:
        return {"status": "error", "code": "NO_URLS", "message": "未提供下载 URL"}

    urls = list(dict.fromkeys(urls))
    requested_format = (file_format or "pdf").lower()
    root_save_dir = save_dir
    save_dir = _format_save_dir(root_save_dir, requested_format, separate_formats)

    os.makedirs(save_dir, exist_ok=True)
    abs_save_dir = os.path.abspath(save_dir)

    # 断点续传：加载进度文件，跳过已成功下载的 URL
    progress_file = Path(abs_save_dir) / ".download_progress.json"
    completed_urls: set = set()
    if progress_file.exists():
        try:
            progress_data = json.loads(progress_file.read_text(encoding="utf-8"))
            completed_urls = set(progress_data.get("completed", []))
        except Exception:
            pass

    remaining_urls = [u for u in urls if u not in completed_urls]
    if not remaining_urls:
        _log("[cnki-download] 所有 URL 已在之前的会话中下载完成")
        return {"status": "success", "count": len(urls), "save_dir": save_dir,
                "root_save_dir": root_save_dir, "requested_format": requested_format,
                "results": [{"url": u, "filename": "(previously completed)",
                             "format": requested_format, "requested_format": requested_format,
                             "save_dir": save_dir} for u in urls]}

    if len(remaining_urls) < len(urls):
        _log(f"[cnki-download] 断点续传: 跳过 {len(urls) - len(remaining_urls)} 篇已完成，"
             f"继续下载 {len(remaining_urls)} 篇")
    urls = remaining_urls

    driver = _driver
    owns_driver = _driver is None

    if owns_driver:
        accessible, msg = check_cnki_access()
        if not accessible:
            if msg.startswith("SANDBOX_BLOCKED"):
                _log("[cnki-download] 沙盒限制导致预检失败，继续尝试...")
            else:
                return {"status": "error", "code": "CNKI_UNREACHABLE", "message": msg}

    window_size = _get_batch_window_size()
    total = len(urls)
    _log(f"[cnki-download] 开始批量下载: {total} 篇论文"
         f"（每批 {window_size} 篇，批间冷却 {COOLDOWN_MIN}-{COOLDOWN_MAX}s）")

    all_ok: List[Dict[str, Any]] = []
    all_errors: List[Dict[str, Any]] = []
    all_retry: List[str] = []

    try:
        if owns_driver:
            browser = _detect_browser()
            driver = _create_driver(browser=browser)

            _prepare_cnki_session(driver)
            driver = _handle_captcha(driver)
            _save_cookies(driver)

        _enable_download_behavior(driver, abs_save_dir)

        batches = _split_evenly(urls, window_size)

        offset = 0
        for batch_idx, batch_urls in enumerate(batches):
            batch_num = batch_idx + 1
            batch_start = time.time()
            _log(f"\n[cnki-download] === 批次 {batch_num}/{len(batches)}"
                 f" ({len(batch_urls)} 篇) ===")

            ok, errs, retries = _trigger_batch_window(
                driver, batch_urls, save_dir, file_format,
                global_idx_offset=offset,
                total=total,
                paper_hints=paper_hints,
            )
            offset += len(batch_urls)
            all_ok.extend(ok)
            all_errors.extend(errs)
            all_retry.extend(retries)

            if getattr(driver, "_cnki_download_auth_blocked", None):
                auth_msg = str(driver._cnki_download_auth_blocked)
                for rem_b in batches[batch_idx + 1:]:
                    for rem_u in rem_b:
                        all_errors.append({"url": rem_u, "code": "CNKI_AUTH_REQUIRED", "error": auth_msg})
                all_retry.clear()
                break

            # 保存断点进度
            for r in ok:
                if r.get("url"):
                    completed_urls.add(r["url"])
            _save_download_progress(progress_file, completed_urls)

            batch_elapsed = time.time() - batch_start
            _log(f"[cnki-download] 批次 {batch_num} 完成:"
                 f" 成功 {len(ok)}, 失败 {len(errs)},"
                 f" 耗时 {batch_elapsed:.1f}s")

            if batch_idx < len(batches) - 1:
                cooldown = random.uniform(COOLDOWN_MIN, COOLDOWN_MAX)
                _log(f"[cnki-download] 冷却 {cooldown:.1f}s...")
                time.sleep(cooldown)

        if all_retry:
            unique_retry = list(dict.fromkeys(all_retry))
            _log(f"\n[cnki-download] === 重试阶段: {len(unique_retry)} 篇 ===")

            cooldown = random.uniform(COOLDOWN_MIN, COOLDOWN_MAX)
            _log(f"[cnki-download] 重试前冷却 {cooldown:.1f}s...")
            time.sleep(cooldown)

            retry_batches = _split_evenly(unique_retry, window_size)

            for rb_idx, rb_urls in enumerate(retry_batches):
                _log(f"[cnki-download] 重试批次 {rb_idx + 1}/{len(retry_batches)}")
                ok, errs, _ = _trigger_batch_window(
                    driver, rb_urls, save_dir, file_format,
                    global_idx_offset=0,
                    total=len(unique_retry),
                    paper_hints=paper_hints,
                )
                for r in ok:
                    if not any(existing["url"] == r["url"] for existing in all_ok):
                        all_ok.append(r)
                        retry_errs_to_remove = [e for e in all_errors if e.get("url") == r["url"]]
                        for rem in retry_errs_to_remove:
                            all_errors.remove(rem)

                for e in errs:
                    replaced = False
                    for i, existing in enumerate(all_errors):
                        if existing.get("url") == e.get("url"):
                            all_errors[i] = e
                            replaced = True
                            break
                    if not replaced:
                        all_errors.append(e)

                if rb_idx < len(retry_batches) - 1:
                    time.sleep(random.uniform(COOLDOWN_MIN, COOLDOWN_MAX))

        _log(f"\n[cnki-download] 全部完成: 成功 {len(all_ok)} 篇, 失败 {len(all_errors)} 篇")

        if not all_ok and all_errors:
            status, code = "error", "CNKI_BATCH_DOWNLOAD_FAILED"
        elif all_errors:
            status, code = "partial", None
        else:
            status, code = "success", None

        result: Dict[str, Any] = {
            "status": status,
            "count": len(all_ok),
            "save_dir": save_dir,
            "root_save_dir": root_save_dir,
            "requested_format": requested_format,
            "results": all_ok,
            "errors": all_errors if all_errors else None,
        }
        if code:
            result["code"] = code
            result["message"] = f"共 {len(all_errors)} 篇论文下载失败"
        return result

    except Exception as e:
        return {"status": "error", "code": "CNKI_BATCH_DOWNLOAD_FAILED",
                "message": str(e)}
    finally:
        if driver is not None and owns_driver:
            _release_driver(driver)


def _wait_for_download(
    directory: str,
    timeout: int = 180,
    before: Optional[set] = None,
) -> Optional[str]:
    """等待下载完成，返回新增文件名。超时返回 None。

    Args:
        directory: 下载目录
        timeout:   最长等待秒数
        before:    点击下载按钮前的目录文件快照；若未提供则在此记录（不推荐）
    """
    _partial_suffixes = (".crdownload", ".tmp", ".part")
    if before is None:
        before = set(os.listdir(directory))

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        current = set(os.listdir(directory))
        new_files = current - before
        if not new_files:
            continue
        new_partial = [f for f in new_files if f.endswith(_partial_suffixes)]
        new_stable = [f for f in new_files if not f.endswith(_partial_suffixes)]
        if new_stable and not new_partial:
            chosen = sorted(new_stable, key=lambda f: os.path.getmtime(
                os.path.join(directory, f)), reverse=True)[0]
            return chosen
        # partial 文件存在说明还在下载中，继续等待

    _log(f"[cnki-download] 等待下载超时 ({timeout}s)")
    return None


# ── 导出文件解析（保底方案）─────────────────────────────

def parse_cnki_export(filepath: str) -> list[dict]:
    """
    解析知网导出的题录文件（NoteExpress/Refworks/BibTeX 格式）。
    当 Selenium 自动化失败时的保底方案。
    """
    path = Path(filepath)
    if not path.exists():
        return [{"status": "error", "code": "FILE_NOT_FOUND", "message": f"文件不存在: {filepath}"}]

    content = path.read_text(encoding="utf-8", errors="ignore")

    if content.strip().startswith("@"):
        return _parse_bibtex_export(content)
    elif "{Reference Type}" in content or "RT " in content:
        return _parse_noteexpress_export(content)
    else:
        return _parse_refworks_export(content)


def _parse_bibtex_export(content: str) -> list[dict]:
    results = []
    entries = re.findall(r'@\w+\{[^@]+\}', content, re.DOTALL)
    for entry in entries:
        paper = {"source": "CNKI-export"}
        title_m = re.search(r'title\s*=\s*\{(.+?)\}', entry)
        if title_m:
            paper["title"] = title_m.group(1).strip()
        author_m = re.search(r'author\s*=\s*\{(.+?)\}', entry)
        if author_m:
            paper["authors"] = author_m.group(1).strip()
        year_m = re.search(r'year\s*=\s*\{?(\d{4})\}?', entry)
        if year_m:
            paper["year"] = int(year_m.group(1))
        journal_m = re.search(r'journal\s*=\s*\{(.+?)\}', entry)
        if journal_m:
            paper["journal"] = journal_m.group(1).strip()
        volume_m = re.search(r'volume\s*=\s*\{(.+?)\}', entry)
        if volume_m:
            paper["volume"] = volume_m.group(1).strip()
        number_m = re.search(r'number\s*=\s*\{(.+?)\}', entry)
        if number_m:
            paper["issue"] = number_m.group(1).strip()
        pages_m = re.search(r'pages\s*=\s*\{(.+?)\}', entry)
        if pages_m:
            paper["pages"] = pages_m.group(1).strip().replace('--', '-')
        doi_m = re.search(r'doi\s*=\s*\{(.+?)\}', entry)
        if doi_m:
            paper["doi"] = doi_m.group(1).strip()
        abs_m = re.search(r'abstract\s*=\s*\{(.+?)\}', entry, re.DOTALL)
        if abs_m:
            paper["abstract"] = re.sub(r'\s+', ' ', abs_m.group(1)).strip()
        kw_m = re.search(r'keywords\s*=\s*\{(.+?)\}', entry)
        if kw_m:
            kws = [k.strip() for k in re.split(r'[;；,，]+', kw_m.group(1)) if k.strip()]
            if kws:
                paper["keywords"] = kws
        if paper.get("title"):
            results.append(paper)
    return results


def _parse_noteexpress_export(content: str) -> list[dict]:
    results = []
    blocks = re.split(r'\n\s*\n', content)
    for block in blocks:
        paper = {"source": "CNKI-export"}
        for line in block.strip().split("\n"):
            if line.startswith("{Title}") or line.startswith("T1 "):
                paper["title"] = line.split(" ", 1)[-1].strip()
            elif line.startswith("{Author}") or line.startswith("A1 "):
                paper["authors"] = line.split(" ", 1)[-1].strip()
            elif line.startswith("{Year}") or line.startswith("YR "):
                y = re.search(r'\d{4}', line)
                if y:
                    paper["year"] = int(y.group())
            elif line.startswith("{Journal}") or line.startswith("JF "):
                paper["journal"] = line.split(" ", 1)[-1].strip()
            elif line.startswith("{Volume}") or line.startswith("VL "):
                paper["volume"] = line.split(" ", 1)[-1].strip()
            elif line.startswith("{Issue}") or line.startswith("IS "):
                paper["issue"] = line.split(" ", 1)[-1].strip()
            elif line.startswith("{Pages}") or line.startswith("SP "):
                paper["pages"] = line.split(" ", 1)[-1].strip()
            elif line.startswith("{DOI}") or line.startswith("DO "):
                paper["doi"] = line.split(" ", 1)[-1].strip()
            elif line.startswith("{Abstract}") or line.startswith("AB "):
                paper["abstract"] = line.split(" ", 1)[-1].strip()
            elif line.startswith("{Keywords}") or line.startswith("K1 ") or line.startswith("KW "):
                raw_kw = line.split(" ", 1)[-1].strip()
                kws = [k.strip() for k in re.split(r'[;；,，]+', raw_kw) if k.strip()]
                if kws:
                    paper["keywords"] = list(dict.fromkeys((paper.get("keywords") or []) + kws))
            elif line.startswith("{Author Address}") or line.startswith("AD "):
                addr = line.split(" ", 1)[-1].strip()
                if addr:
                    paper["institutions"] = [s.strip() for s in re.split(r'[;；]+', addr) if s.strip()]
        if paper.get("title"):
            results.append(paper)
    return results


def _parse_refworks_export(content: str) -> list[dict]:
    results = []
    blocks = re.split(r'\nER\s*(?:\n|$)|(?=\nRT\s)', content)
    tag_re = re.compile(r'^[A-Z][A-Z0-9]\s')
    for block in blocks:
        paper: Dict[str, Any] = {"source": "CNKI-export"}
        sp_val = ""
        ep_val = ""
        op_val = ""
        last_tag = ""
        for raw_line in block.strip().split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if not tag_re.match(line):
                if last_tag == "AB" and paper.get("abstract"):
                    paper["abstract"] = f"{paper['abstract']} {line}".strip()
                continue
            last_tag = line[:2]
            val = line[3:].strip()
            if line.startswith("T1 "):
                paper["title"] = val
            elif line.startswith("A1 "):
                existing = paper.get("authors", "")
                if val:
                    paper["authors"] = f"{existing}; {val}" if existing else val
            elif line.startswith("YR "):
                y = re.search(r'\d{4}', val)
                if y:
                    paper["year"] = int(y.group())
            elif line.startswith("JF ") or line.startswith("JO "):
                paper["journal"] = val
            elif line.startswith("VO "):
                paper["volume"] = val
            elif line.startswith("IS "):
                paper["issue"] = val
            elif line.startswith("SP "):
                sp_val = val
            elif line.startswith("EP "):
                ep_val = val
            elif line.startswith("OP "):
                op_val = val
            elif line.startswith("DO "):
                paper["doi"] = val
            elif line.startswith("AB "):
                paper["abstract"] = val
            elif line.startswith("K1 ") or line.startswith("KW "):
                kws = [k.strip() for k in re.split(r'[;；,，]+', val) if k.strip()]
                if kws:
                    paper["keywords"] = list(dict.fromkeys((paper.get("keywords") or []) + kws))
            elif line.startswith("AD "):
                addrs = [s.strip() for s in re.split(r'[;；]+', val) if s.strip()]
                if addrs:
                    paper["institutions"] = addrs

        # 精确合成起止页码（避免 CNKI 同时输出 `SP 65` 与 `OP 65-76` 时拼成 `65-65-76`）
        if re.search(r'\d+\s*[-–—]\s*\d+', op_val):
            paper["pages"] = re.sub(r'[–—]', '-', op_val)
        elif re.search(r'\d+\s*[-–—]\s*\d+', sp_val):
            paper["pages"] = re.sub(r'[–—]', '-', sp_val)
        elif sp_val and ep_val:
            paper["pages"] = sp_val if sp_val == ep_val else f"{sp_val}-{ep_val}"
        elif sp_val and op_val:
            if sp_val.isdigit() and op_val.isdigit() and int(op_val) >= int(sp_val):
                paper["pages"] = sp_val if sp_val == op_val else f"{sp_val}-{op_val}"
            else:
                paper["pages"] = sp_val
        elif sp_val:
            paper["pages"] = sp_val
        elif op_val:
            paper["pages"] = op_val

        if paper.get("title"):
            results.append(paper)
    return results
