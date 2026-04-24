"""cnki.download - 论文下载与导出文件解析"""
from __future__ import annotations

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
    _detect_browser, _create_driver, _load_cookies, _save_cookies,
    _handle_captcha, check_cnki_access,
)

def _get_batch_window_size() -> int:
    try:
        from config import get as cfg_get
        val = cfg_get("batch_window_size", 10)
    except ImportError:
        val = os.environ.get("SCHOLAR_BATCH_WINDOW_SIZE", "10")
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 10

BATCH_WINDOW_SIZE = _get_batch_window_size()
COOLDOWN_MIN = 4
COOLDOWN_MAX = 8


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


def _click_download_btn(driver, file_format: str = "pdf") -> bool:
    """点击下载按钮，成功返回 True。

    知网不同页面（期刊 vs 学位论文）的按钮 ID/结构不一致，
    因此先按 ID 找，再按链接文字找，确保都能命中。
    """
    btn_id = "pdfDown" if file_format == "pdf" else "cajDown"
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
            return True
        except (TimeoutException, NoSuchElementException):
            continue

    try:
        links = driver.find_elements(By.CSS_SELECTOR, "a")
        for link in links:
            text = (link.text or "").strip()
            if text == btn_text:
                link.click()
                return True
            href = link.get_attribute("href") or ""
            if "download" in href and btn_text[:3] in text:
                link.click()
                return True
    except Exception:
        pass

    if file_format == "pdf":
        try:
            caj_btn = driver.find_element(By.ID, "cajDown")
            if caj_btn:
                _log("[cnki-download] PDF 按钮未找到，尝试 CAJ 兜底")
                caj_btn.click()
                return True
        except (NoSuchElementException, Exception):
            pass

    return False


def download_cnki(url: str, save_dir: str = "./papers", file_format: str = "pdf") -> Dict[str, Any]:
    """
    从知网下载单篇论文。

    Args:
        url:         知网论文详情页 URL
        save_dir:    保存目录
        file_format: 下载格式 (pdf/caj)

    Returns:
        下载结果信息
    """
    os.makedirs(save_dir, exist_ok=True)
    abs_save_dir = os.path.abspath(save_dir)

    driver = None
    try:
        browser = _detect_browser()
        driver = _create_driver(browser=browser)

        driver.get("https://kns.cnki.net/")
        time.sleep(1)
        _load_cookies(driver)

        driver.execute_cdp_cmd("Browser.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": abs_save_dir,
            "eventsEnabled": True,
        })

        driver.get(url)
        time.sleep(REQUEST_INTERVAL)
        driver = _handle_captcha(driver)

        title = _get_title(driver)
        snapshot = set(os.listdir(save_dir))

        if not _click_download_btn(driver, file_format):
            return {"status": "error", "code": "DOWNLOAD_BTN_NOT_FOUND",
                    "message": "未找到{}下载按钮".format(file_format)}

        downloaded = _wait_for_download(save_dir, timeout=180, before=snapshot)

        if downloaded:
            _log(f"[cnki-download] 下载完成: {downloaded}")
            return {
                "status": "success",
                "title": title,
                "save_dir": save_dir,
                "filename": downloaded,
                "format": file_format,
            }
        else:
            return {
                "status": "warning",
                "code": "DOWNLOAD_TIMEOUT",
                "title": title,
                "save_dir": save_dir,
                "message": "下载可能仍在进行中，请检查目录",
                "format": file_format,
            }

    except Exception as e:
        return {"status": "error", "code": "CNKI_DOWNLOAD_FAILED",
                "message": str(e)}
    finally:
        if driver is not None:
            try:
                _save_cookies(driver)
                driver.quit()
            except Exception:
                pass


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


def _trigger_batch_window(
    driver, urls_window: List[str], save_dir: str, file_format: str,
    global_idx_offset: int, total: int,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """并行加载批次：先一口气开 N 个标签页让页面同时加载，再逐个切回去点下载。
    Phase B 中途异常时 break 并把剩余 URL 推入重试，同时清理所有孤儿标签页。
    返回 (ok_results, batch_errors, retry_urls)"""

    main_window = driver.current_window_handle
    batch_meta: List[Dict[str, Any]] = []
    batch_errors = []
    retry_urls = []
    snapshot_before = set(os.listdir(save_dir))

    tab_handles: List[tuple[str, str, int]] = []
    handles_before = set(driver.window_handles)

    for i, url in enumerate(urls_window):
        idx = global_idx_offset + i + 1
        _log(f"[cnki-download] [{idx}/{total}] 并行打开: {url[:80]}...")
        orphan_handle = None
        try:
            before_open = set(driver.window_handles)
            driver.execute_script("window.open('');")
            after_open = set(driver.window_handles)
            new_handles = after_open - before_open
            if not new_handles:
                _log(f"[cnki-download] [{idx}/{total}] 未能打开新标签")
                batch_errors.append({"url": url, "code": "CNKI_DOWNLOAD_FAILED", "error": "无法打开新标签页"})
                continue
            orphan_handle = new_handles.pop()
            driver.switch_to.window(orphan_handle)
            driver.get(url)
            tab_handles.append((url, orphan_handle, idx))
            orphan_handle = None
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

            title = _get_title(driver)
            _log(f"[cnki-download] [{idx}/{total}] 标题: {title or '(未获取)'}")

            if _is_already_downloaded(title, save_dir):
                _log(f"[cnki-download] [{idx}/{total}] 已存在，跳过")
                batch_meta.append({"url": url, "title": title, "skipped": True})
                driver.close()
                driver.switch_to.window(main_window)
                continue

            if not _click_download_btn(driver, file_format):
                err = f"未找到{file_format}下载按钮"
                _log(f"[cnki-download] [{idx}/{total}] {err}")
                batch_errors.append({"url": url, "title": title,
                                     "code": "DOWNLOAD_BTN_NOT_FOUND", "error": err})
                retry_urls.append(url)
                driver.close()
                driver.switch_to.window(main_window)
                continue

            batch_meta.append({"url": url, "title": title})
            _log(f"[cnki-download] [{idx}/{total}] 下载已触发")

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
    _driver=None,
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

    os.makedirs(save_dir, exist_ok=True)
    abs_save_dir = os.path.abspath(save_dir)

    driver = _driver
    owns_driver = _driver is None

    if owns_driver:
        accessible, msg = check_cnki_access()
        if not accessible:
            if msg.startswith("SANDBOX_BLOCKED"):
                _log("[cnki-download] 沙盒限制导致预检失败，继续尝试...")
            else:
                return {"status": "error", "code": "CNKI_UNREACHABLE", "message": msg}

    total = len(urls)
    _log(f"[cnki-download] 开始批量下载: {total} 篇论文"
         f"（每批 {BATCH_WINDOW_SIZE} 篇，批间冷却 {COOLDOWN_MIN}-{COOLDOWN_MAX}s）")

    all_ok: List[Dict[str, Any]] = []
    all_errors: List[Dict[str, Any]] = []
    all_retry: List[str] = []

    try:
        if owns_driver:
            browser = _detect_browser()
            driver = _create_driver(browser=browser)

            driver.get("https://kns.cnki.net/")
            time.sleep(1)
            _load_cookies(driver)

            driver = _handle_captcha(driver)
            _save_cookies(driver)

        driver.execute_cdp_cmd("Browser.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": abs_save_dir,
            "eventsEnabled": True,
        })

        batches = _split_evenly(urls, BATCH_WINDOW_SIZE)

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
            )
            offset += len(batch_urls)
            all_ok.extend(ok)
            all_errors.extend(errs)
            all_retry.extend(retries)

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

            retry_batches = _split_evenly(unique_retry, BATCH_WINDOW_SIZE)

            for rb_idx, rb_urls in enumerate(retry_batches):
                _log(f"[cnki-download] 重试批次 {rb_idx + 1}/{len(retry_batches)}")
                ok, errs, _ = _trigger_batch_window(
                    driver, rb_urls, save_dir, file_format,
                    global_idx_offset=0,
                    total=len(unique_retry),
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
            try:
                _save_cookies(driver)
                driver.quit()
            except Exception:
                pass


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
        doi_m = re.search(r'doi\s*=\s*\{(.+?)\}', entry)
        if doi_m:
            paper["doi"] = doi_m.group(1).strip()
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
        if paper.get("title"):
            results.append(paper)
    return results


def _parse_refworks_export(content: str) -> list[dict]:
    results = []
    blocks = re.split(r'\nER\s*\n', content)
    for block in blocks:
        paper = {"source": "CNKI-export"}
        for line in block.strip().split("\n"):
            line = line.strip()
            if line.startswith("T1 "):
                paper["title"] = line[3:].strip()
            elif line.startswith("A1 "):
                existing = paper.get("authors", "")
                author = line[3:].strip()
                paper["authors"] = f"{existing}; {author}" if existing else author
            elif line.startswith("YR "):
                y = re.search(r'\d{4}', line)
                if y:
                    paper["year"] = int(y.group())
            elif line.startswith("JF "):
                paper["journal"] = line[3:].strip()
            elif line.startswith("DO "):
                paper["doi"] = line[3:].strip()
        if paper.get("title"):
            results.append(paper)
    return results
