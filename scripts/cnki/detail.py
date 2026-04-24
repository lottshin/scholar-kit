"""cnki.detail - 论文详情页解析、HTML 全文阅读与缓存"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .constants import (
    HAS_SELENIUM, REQUEST_INTERVAL, _log,
)
from .driver import (
    _detect_browser, _create_driver, _load_cookies, _save_cookies,
    _handle_captcha,
)

if HAS_SELENIUM:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException,
    )


# ── 论文详情 ──────────────────────────────────────────

def get_detail(url: str) -> Dict[str, Any]:
    """获取知网论文详情页信息（摘要、关键词、参考文献等）"""
    driver = None
    try:
        browser = _detect_browser()
        driver = _create_driver(browser=browser)

        driver.get("https://kns.cnki.net/")
        time.sleep(1)
        _load_cookies(driver)

        driver.get(url)
        time.sleep(REQUEST_INTERVAL)
        driver = _handle_captcha(driver)

        detail = _parse_detail_page(driver)
        detail["url"] = url
        detail["source"] = "CNKI"
        return detail

    except Exception as e:
        return {"status": "error", "code": "CNKI_DETAIL_FAILED",
                "message": str(e), "url": url}
    finally:
        if driver is not None:
            try:
                _save_cookies(driver)
                driver.quit()
            except Exception:
                pass


def _parse_detail_page(driver) -> Dict[str, Any]:
    """从已加载的知网详情页 DOM 中提取元数据（标题、摘要、关键词等）。
    调用者负责导航到详情页并处理验证码，本函数只做解析。
    """
    detail: Dict[str, Any] = {}

    try:
        title_el = driver.find_element(By.CSS_SELECTOR, "h1, .wx-tit h1")
        detail["title"] = title_el.text.strip()
    except NoSuchElementException:
        detail["title"] = ""

    try:
        abstract_el = driver.find_element(
            By.CSS_SELECTOR, "#ChDivSummary, .abstract-text, span#ChDivSummary"
        )
        detail["abstract"] = abstract_el.text.strip()
    except NoSuchElementException:
        detail["abstract"] = ""

    try:
        kw_els = driver.find_elements(By.CSS_SELECTOR, ".keywords a, p.keywords a")
        detail["keywords"] = [
            kw.text.strip().rstrip(";；") for kw in kw_els if kw.text.strip()
        ]
    except Exception:
        detail["keywords"] = []

    try:
        author_els = driver.find_elements(By.CSS_SELECTOR, ".author a, h3.author a")
        detail["authors"] = "; ".join(
            a.text.strip() for a in author_els if a.text.strip()
        )
    except NoSuchElementException:
        detail["authors"] = ""

    try:
        org_els = driver.find_elements(By.CSS_SELECTOR, ".orgn a, .organ a")
        detail["institutions"] = [o.text.strip() for o in org_els if o.text.strip()]
    except Exception:
        detail["institutions"] = []

    try:
        fund_els = driver.find_elements(By.CSS_SELECTOR, ".fund a, p.fund a")
        detail["funds"] = [f.text.strip() for f in fund_els if f.text.strip()]
    except Exception:
        detail["funds"] = []

    return detail


# ── HTML 全文阅读 ─────────────────────────────────────

def _fulltext_cache_dir() -> Path:
    d = Path.cwd() / ".scholar-kit" / "fulltext"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(url: str) -> str:
    """从知网 URL 中提取稳定的缓存文件名"""
    return hashlib.md5(url.encode()).hexdigest()


def _get_cache_ttl() -> int:
    try:
        from config import get as cfg_get
        return cfg_get("cache_ttl_days", 30)
    except ImportError:
        return int(os.environ.get("SCHOLAR_CACHE_TTL_DAYS", "30"))

_FULLTEXT_CACHE_TTL_DAYS = _get_cache_ttl()


def _load_cached_fulltext(url: str) -> Optional[Dict[str, Any]]:
    cache_file = _fulltext_cache_dir() / f"{_cache_key(url)}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if not data.get("fulltext"):
                return None
            ts = data.get("_cached_at", "")
            if ts and _FULLTEXT_CACHE_TTL_DAYS > 0:
                from datetime import datetime, timedelta
                try:
                    cached_at = datetime.fromisoformat(ts)
                    if datetime.now() - cached_at > timedelta(days=_FULLTEXT_CACHE_TTL_DAYS):
                        _log(f"[cnki-detail] 缓存已过期（>{_FULLTEXT_CACHE_TTL_DAYS}天），重新抓取")
                        return None
                except ValueError:
                    pass
            return data
        except Exception as e:
            _log(f"[cnki-detail] 缓存读取失败: {e}")
    return None


def _save_cached_fulltext(url: str, data: Dict[str, Any]):
    from datetime import datetime
    data["_cached_at"] = datetime.now().isoformat()
    cache_file = _fulltext_cache_dir() / f"{_cache_key(url)}.json"
    tmp_file = cache_file.with_suffix(".tmp")
    try:
        tmp_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp_file), str(cache_file))
    except Exception as e:
        _log(f"[cnki-detail] 缓存写入失败: {e}")
        try:
            tmp_file.unlink(missing_ok=True)
        except Exception:
            pass


def _has_fulltext_slider(driver) -> bool:
    """检测全文阅读页底部的滑块验证（"请向右滑动验证，继续阅读全文"）。
    临时关闭 implicit_wait 避免宽泛选择器 × 3s 延迟。
    """
    try:
        has_text = driver.execute_script(
            "return document.body && ("
            "document.body.innerText.indexOf('滑动验证') >= 0 || "
            "document.body.innerText.indexOf('继续阅读全文') >= 0)"
        )
        if has_text:
            return True
        driver.implicitly_wait(0)
        try:
            sliders = driver.find_elements(
                By.CSS_SELECTOR,
                ".slider-verify, .verify-slide, .slide-verify, "
                "[class*='slider'], [class*='captcha'], [class*='verify']"
            )
            for s in sliders:
                if s.is_displayed():
                    return True
        finally:
            driver.implicitly_wait(3)
    except Exception:
        pass
    return False


def _read_fulltext_html(driver, detail_url: str) -> Optional[str]:
    """在已打开的详情页上查找并进入 HTML 全文阅读页，提取正文文本。"""
    original_window = driver.current_window_handle

    try:
        html_link = None

        link_selectors = [
            "a#htmlRead",
            "a.btn-html",
            "a[href*='bar.cnki.net/bar/download/order']",
            "a[href*='HtmlView']",
            "a[href*='htmlview']",
        ]
        for sel in link_selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    el_text = el.text.strip()
                    if "HTML" in el_text and "AI" not in el_text:
                        html_link = el
                        break
                if html_link:
                    break
            except NoSuchElementException:
                continue

        if html_link is None:
            try:
                all_links = driver.find_elements(By.CSS_SELECTOR, "a")
                for a in all_links:
                    text = a.text.strip()
                    if text == "HTML阅读":
                        html_link = a
                        break
            except Exception:
                pass

        if html_link is None:
            _log("[cnki-fulltext] 未找到 HTML 阅读链接")
            return None

        href = html_link.get_attribute("href") or ""
        _log(f"[cnki-fulltext] 找到 HTML 阅读链接: {href[:80]}")

        handles_before = set(driver.window_handles)
        driver.execute_script("window.open(arguments[0], '_blank');", href)
        time.sleep(3)

        new_handles = set(driver.window_handles) - handles_before
        if not new_handles:
            _log("[cnki-fulltext] 未能打开新标签页")
            return None

        new_window = new_handles.pop()
        fulltext = None
        try:
            driver.switch_to.window(new_window)
            time.sleep(3)

            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            except TimeoutException:
                _log("[cnki-fulltext] HTML 全文页加载超时")
                return None

            driver = _handle_captcha(driver)
            if _has_fulltext_slider(driver):
                _log("[cnki-fulltext] 全文页出现滑块验证，弹出浏览器窗口...")
                from .driver import _show_browser_for_captcha
                driver = _show_browser_for_captcha(
                    driver,
                    "知网全文阅读页要求滑块验证，请在浏览器中完成...",
                    poll_timeout=120,
                )
                time.sleep(2)

            fulltext = _extract_html_fulltext(driver)
            return fulltext
        finally:
            try:
                if new_window in driver.window_handles:
                    driver.switch_to.window(new_window)
                    driver.close()
            except Exception:
                pass
            try:
                driver.switch_to.window(original_window)
            except Exception:
                pass

    except Exception as e:
        _log(f"[cnki-fulltext] 全文阅读失败: {e}")
        return None


def _extract_html_fulltext(driver) -> Optional[str]:
    """从知网 HTML 全文阅读页提取正文文本。"""
    content_selectors = [
        ".article-body",
        ".main-text",
        ".article",
        "#content",
        ".content",
        ".doc",
        ".p-content",
    ]

    container = None
    for sel in content_selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            text = el.text.strip()
            if len(text) > 200:
                container = el
                _log(f"[cnki-fulltext] 找到正文容器: {sel} ({len(text)} 字)")
                break
        except NoSuchElementException:
            continue

    if container is None:
        try:
            paragraphs = driver.find_elements(By.CSS_SELECTOR, "p")
            texts = [p.text.strip() for p in paragraphs if len(p.text.strip()) > 20]
            if texts:
                combined = "\n\n".join(texts)
                if len(combined) > 300:
                    _log(f"[cnki-fulltext] 通过段落收集: {len(combined)} 字")
                    return combined
        except Exception:
            pass

    if container is None:
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body_text = body.text.strip()
            if len(body_text) > 500:
                _log(f"[cnki-fulltext] 使用 body 兜底: {len(body_text)} 字")
                return body_text
        except Exception:
            pass
        return None

    sections = []
    try:
        headings = container.find_elements(
            By.CSS_SELECTOR, "h1, h2, h3, h4, .title, .section-title"
        )
        if headings:
            all_text = container.text.strip()
            sections.append(all_text)
        else:
            paras = container.find_elements(By.CSS_SELECTOR, "p, div.para, div.p")
            if paras:
                for p in paras:
                    t = p.text.strip()
                    if t:
                        sections.append(t)
            else:
                sections.append(container.text.strip())
    except Exception:
        sections.append(container.text.strip())

    return "\n\n".join(sections) if sections else None


# ── FlowPDF / KXReader 全文阅读（硕博论文） ─────────────

def _flowpdf_extract_all_textlayers(driver, total_pages: int) -> dict:
    """一次 JS 调用提取所有已加载的 textLayer 文本。"""
    raw = driver.execute_script("""
        var canvases = document.querySelectorAll('canvas');
        var results = {};
        for (var i = 0; i < canvases.length; i++) {
            var canvas = canvases[i];
            function search(el, depth) {
                if (!el || depth > 3) return '';
                var tl = el.querySelector('[class*="textLayer"]');
                if (tl) {
                    var spans = tl.querySelectorAll('span, div');
                    var arr = [];
                    spans.forEach(function(s) {
                        var t = s.textContent.trim();
                        if (t) arr.push(t);
                    });
                    return arr.join('');
                }
                return search(el.parentElement, depth + 1);
            }
            var text = search(canvas.parentElement, 0);
            if (text && text.length > 0) results[i + 1] = text;
        }
        return results;
    """)
    if not raw:
        return {}
    return {int(k): v for k, v in raw.items() if v and v.strip()}


def _read_fulltext_flowpdf(driver, detail_url: str) -> Optional[str]:
    """KXReader 全文提取（硕博论文）。三级加速：API直取 → 批量滚动 → 逐页补漏。"""
    original_window = driver.current_window_handle

    try:
        href = driver.execute_script("""
            var links = document.querySelectorAll('a');
            for (var i = 0; i < links.length; i++) {
                var txt = links[i].textContent.trim();
                if (txt.indexOf('原版') >= 0 || txt.indexOf('在线阅读') >= 0) {
                    return links[i].href;
                }
            }
            var els = document.querySelectorAll('li, button, span');
            for (var i = 0; i < els.length; i++) {
                var txt = els[i].textContent.trim();
                if (txt === '原版阅读') {
                    var a = els[i].querySelector('a') || els[i].closest('a');
                    if (a) return a.href;
                }
            }
            return null;
        """)

        if not href:
            dbcode = driver.execute_script(
                "var e = document.getElementById('param-dbcode'); return e ? e.value : '';")
            dbname = driver.execute_script(
                "var e = document.getElementById('param-dbname'); return e ? e.value : '';")
            filename = driver.execute_script(
                "var e = document.getElementById('param-filename'); return e ? e.value : '';")
            if dbcode and filename:
                href = (f"https://kns.cnki.net/KXReader/Detail"
                        f"?DBCODE={dbcode}&DBNAME={dbname}&FILENAME={filename}")
                _log(f"[cnki-flowpdf] 构造 KXReader URL: {href[:80]}")
            else:
                _log("[cnki-flowpdf] 未找到原版阅读链接，也无法构造 URL")
                return None

        _log(f"[cnki-flowpdf] 原版阅读链接: {href[:80]}")

        handles_before = set(driver.window_handles)
        driver.execute_script("window.open(arguments[0], '_blank');", href)
        time.sleep(3)

        new_handles = set(driver.window_handles) - handles_before
        if not new_handles:
            _log("[cnki-flowpdf] 未能打开新标签页")
            return None

        new_window = new_handles.pop()
        try:
            driver.switch_to.window(new_window)

            try:
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.TAG_NAME, "canvas"))
                )
            except TimeoutException:
                _log("[cnki-flowpdf] 页面加载超时（未发现 canvas）")
                return None

            driver = _handle_captcha(driver)

            total_pages = driver.execute_script(
                "return document.querySelectorAll('canvas').length;")
            if not total_pages:
                _log("[cnki-flowpdf] 未找到 canvas 元素，无法提取")
                return None

            _log(f"[cnki-flowpdf] 共 {total_pages} 页")

            # ── 方法 1: PDF.js API 直取（最快，~3s） ──────────────
            all_pages = {}
            try:
                driver.set_script_timeout(30)
                api_result = driver.execute_async_script("""
                    var cb = arguments[arguments.length - 1];
                    try {
                        var app = window.PDFViewerApplication;
                        if (!app) {
                            var iframes = document.querySelectorAll('iframe');
                            for (var f = 0; f < iframes.length; f++) {
                                try { if (iframes[f].contentWindow.PDFViewerApplication)
                                    { app = iframes[f].contentWindow.PDFViewerApplication; break; }
                                } catch(e) {}
                            }
                        }
                        if (!app || !app.pdfDocument) { cb(null); return; }
                        var doc = app.pdfDocument;
                        var n = doc.numPages;
                        var results = {};
                        var done = 0;
                        for (var i = 1; i <= n; i++) {
                            (function(pn) {
                                doc.getPage(pn).then(function(page) {
                                    return page.getTextContent();
                                }).then(function(content) {
                                    results[pn] = content.items.map(function(it) {
                                        return it.str;
                                    }).join('');
                                    done++;
                                    if (done === n) cb(results);
                                }).catch(function() { done++; if (done === n) cb(results); });
                            })(i);
                        }
                        setTimeout(function() { cb(results); }, 25000);
                    } catch(e) { cb(null); }
                """)
                if api_result:
                    all_pages = {int(k): v.strip() for k, v in api_result.items()
                                 if v and v.strip()}
                    if len(all_pages) >= total_pages * 0.5:
                        _log(f"[cnki-flowpdf] PDF.js API 直取成功: "
                             f"{len(all_pages)}/{total_pages} 页")
            except Exception as e:
                _log(f"[cnki-flowpdf] PDF.js API 不可用: {e}")

            # ── 方法 2: 批量滚动 + 一次性提取（~30s） ─────────────
            if len(all_pages) < total_pages * 0.5:
                _log("[cnki-flowpdf] 使用批量滚动模式...")
                for batch_end in range(5, total_pages + 5, 5):
                    target = min(batch_end, total_pages) - 1
                    driver.execute_script(f"""
                        var c = document.querySelectorAll('canvas');
                        if (c.length > {target})
                            c[{target}].scrollIntoView({{block: 'center'}});
                    """)
                    time.sleep(0.5)
                    if batch_end % 50 == 0:
                        driver = _handle_captcha(driver)

                time.sleep(3)
                all_pages = _flowpdf_extract_all_textlayers(driver, total_pages)
                _log(f"[cnki-flowpdf] 批量提取: {len(all_pages)}/{total_pages} 页")

            # ── 方法 3: 逐页补漏（仅处理缺失页） ─────────────────
            if 0 < len(all_pages) < total_pages * 0.8:
                missing = [p for p in range(1, total_pages + 1)
                           if p not in all_pages]
                if missing:
                    _log(f"[cnki-flowpdf] 补漏 {len(missing)} 页...")
                    for page_num in missing:
                        driver.execute_script(f"""
                            var c = document.querySelectorAll('canvas');
                            if (c.length >= {page_num})
                                c[{page_num - 1}].scrollIntoView({{block: 'center'}});
                        """)
                        time.sleep(0.8)
                    time.sleep(2)
                    all_pages = _flowpdf_extract_all_textlayers(driver, total_pages)
                    _log(f"[cnki-flowpdf] 补漏后: {len(all_pages)}/{total_pages} 页")

            if not all_pages:
                _log("[cnki-flowpdf] 未能提取到任何文本")
                return None

            parts = [all_pages[p] for p in sorted(all_pages.keys())]
            combined = "\n\n".join(parts)
            lines = combined.split("\n")
            deduped = []
            for line in lines:
                s = line.strip()
                if s and (not deduped or s != deduped[-1]):
                    deduped.append(s)
            result = "\n".join(deduped)

            _log(f"[cnki-flowpdf] 提取完成: {len(all_pages)}/{total_pages} 页, "
                 f"{len(result)} 字")
            return result if len(result) > 200 else None

        finally:
            try:
                if new_window in driver.window_handles:
                    driver.switch_to.window(new_window)
                    driver.close()
            except Exception:
                pass
            try:
                driver.switch_to.window(original_window)
            except Exception:
                pass

    except Exception as e:
        _log(f"[cnki-flowpdf] 提取失败: {e}")
        return None


# ── 批量详情 ──────────────────────────────────────────

def batch_read_detail(
    papers: List[Dict[str, Any]],
    top_n: int = 5,
    fulltext: bool = False,
) -> List[Dict[str, Any]]:
    """批量获取论文详情（摘要+可选全文），复用同一个浏览器会话。

    Args:
        papers:   搜索结果列表（需含 url 字段）
        top_n:    只处理前 N 篇（默认 5）
        fulltext: 是否抓取 HTML 全文（默认 False，只抓摘要）

    Returns:
        增强后的论文列表（追加 abstract/keywords/fulltext 等字段）
    """
    if not papers:
        return []

    targets = [p for p in papers if p.get("url")][:top_n]
    if not targets:
        _log("[cnki-detail] 没有可访问的论文 URL")
        return papers

    _log(f"[cnki-detail] 将获取 {len(targets)} 篇论文的{'全文' if fulltext else '摘要'}")

    driver = None
    try:
        browser = _detect_browser()
        driver = _create_driver(browser=browser)

        driver.get("https://kns.cnki.net/")
        time.sleep(1)
        _load_cookies(driver)
        _save_cookies(driver)

        for idx, paper in enumerate(targets):
            url = paper["url"]
            _log(f"\n[cnki-detail] === {idx + 1}/{len(targets)}: {paper.get('title', '')[:40]} ===")

            if fulltext:
                cached = _load_cached_fulltext(url)
                if cached:
                    _log("[cnki-detail] 命中缓存，跳过抓取")
                    for k, v in cached.items():
                        if k == "fulltext":
                            paper["has_fulltext"] = True
                            paper["fulltext_length"] = len(v)
                            paper["fulltext_cache"] = str(
                                _fulltext_cache_dir() / f"{_cache_key(url)}.json"
                            )
                        elif v and not paper.get(k):
                            paper[k] = v
                    continue

            try:
                driver.get(url)
                time.sleep(REQUEST_INTERVAL)
                driver = _handle_captcha(driver)

                detail = _parse_detail_page(driver)
                for k, v in detail.items():
                    if v and not paper.get(k):
                        paper[k] = v

                if fulltext:
                    ft = _read_fulltext_html(driver, url)
                    if not ft:
                        _log("[cnki-detail] HTML阅读不可用，尝试原版阅读(FlowPDF)...")
                        ft = _read_fulltext_flowpdf(driver, url)
                    if ft:
                        paper["has_fulltext"] = True
                        paper["fulltext"] = ft
                        paper["fulltext_length"] = len(ft)
                        paper["fulltext_cache"] = str(
                            _fulltext_cache_dir() / f"{_cache_key(url)}.json"
                        )
                        _log(f"[cnki-detail] 全文获取成功: {len(ft)} 字")
                        _save_cached_fulltext(url, {
                            "abstract": paper.get("abstract", ""),
                            "keywords": paper.get("keywords", []),
                            "fulltext": ft,
                            "fulltext_length": len(ft),
                        })
                    else:
                        _log("[cnki-detail] HTML阅读和原版阅读均失败，仅保留摘要")
                        paper["has_fulltext"] = False
                else:
                    _log(f"[cnki-detail] 摘要: {paper.get('abstract', '')[:60]}...")

            except Exception as e:
                _log(f"[cnki-detail] 获取详情失败: {e}")
                paper["detail_error"] = str(e)
                continue

            time.sleep(1)

        return papers

    except Exception as e:
        _log(f"[cnki-detail] 批量详情获取失败: {e}")
        return papers
    finally:
        if driver is not None:
            try:
                _save_cookies(driver)
                driver.quit()
            except Exception:
                pass
