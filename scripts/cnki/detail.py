"""cnki.detail - 论文详情页解析、HTML 全文阅读与缓存"""
from __future__ import annotations

import hashlib
import json
import os
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
    _release_driver,
    _safe_get,
)

if HAS_SELENIUM:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException,
    )


def _is_cnki_error_or_404(driver) -> bool:
    """检测知网加密详情链接 (`kcms2?v=...`) 是否已过期或跳转到 404/错误页。"""
    try:
        title = (driver.title or "").strip().lower()
        if "404" in title or "error page" in title or "错误" in title:
            return True
        body = driver.execute_script(
            "return (document.body && document.body.innerText || '').substring(0, 800);"
        ) or ""
        return any(m in body for m in ("404", "页面不存在", "所访问的页面不存在", "参数错误"))
    except Exception:
        return False


# ── 论文详情 ──────────────────────────────────────────

def get_detail(url: str, expected_title: str = "") -> Dict[str, Any]:
    """获取知网论文详情页信息（摘要、关键词、隐藏结构化字段等，支持过期 URL 自动重定位）。"""
    driver = None
    try:
        browser = _detect_browser()
        driver = _create_driver(browser=browser)

        _prepare_cnki_session(driver)

        active_url = url
        _navigate_within_cnki(driver, active_url)
        driver = _handle_captcha(driver)

        if _is_cnki_error_or_404(driver) and expected_title:
            from .search import refresh_cnki_url
            refreshed = refresh_cnki_url(driver, title=expected_title)
            if refreshed:
                active_url = refreshed
                _navigate_within_cnki(driver, active_url)
                driver = _handle_captcha(driver)

        detail = _parse_detail_page(driver)
        detail["url"] = active_url
        detail["source"] = "CNKI"
        return detail

    except Exception as e:
        return {"status": "error", "code": "CNKI_DETAIL_FAILED",
                "message": str(e), "url": url}
    finally:
        if driver is not None:
            _release_driver(driver)


def _split_authors_institutions(raw_items: List[str]) -> tuple:
    """将知网详情页作者列表中混入的机构信息分离出来。

    知网 kcms2 版详情页的 .author a 元素可能包含：
    - 纯作者名: "赵涛", "张智"
    - 带上标编号的作者名: "赵涛1,2", "张智3"
    - 机构名: "1.中央财经大学财政税务学院"
    """
    authors = []
    institutions = []
    for item in raw_items:
        # 机构：以 "数字." 开头（如 "1.中央财经大学"）
        if re.match(r'^\d+\.\s*', item):
            inst = re.sub(r'^\d+\.\s*', '', item).strip()
            if inst:
                institutions.append(inst)
        else:
            # 作者：去掉尾部的上标数字（如 "赵涛1,2" → "赵涛"）
            clean = re.sub(r'[\d,，]+$', '', item).strip()
            if clean:
                authors.append(clean)
    return authors, institutions


def _parse_detail_page(driver) -> Dict[str, Any]:
    """从已加载的知网详情页 DOM 中提取元数据（临时关闭隐式等待，消除可选字段每项空等 3s）。"""
    try:
        driver.implicitly_wait(0)
    except Exception:
        pass
    try:
        return _parse_detail_page_inner(driver)
    finally:
        try:
            driver.implicitly_wait(3)
        except Exception:
            pass


def _parse_detail_page_inner(driver) -> Dict[str, Any]:
    from .search import _clean_scraped_cnki_title, _extract_pages_from_gbt7714
    from .selectors import CNKI_SELECTORS

    d_sel = CNKI_SELECTORS["detail"]
    detail: Dict[str, Any] = {}

    try:
        pure_title = driver.execute_script(r"""
            var cands = ['.wx-tit h1', '.brief h1', '.doc-top h1', 'h1.title'];
            for (var i = 0; i < cands.length; i++) {
                var el = document.querySelector(cands[i]);
                if (!el) continue;
                var h = el.cloneNode(true);
                h.querySelectorAll('span, a, sup, i, em').forEach(function(n){
                    var t = (n.textContent || '').trim();
                    if (t === '网络首发' || t === '增强出版' || t === '优先出版' || t === 'OA' || t.indexOf('附录') >= 0 || /^\[\w+\]$/.test(t) || n.tagName === 'SUP') {
                        n.remove();
                    }
                });
                var txt = (h.textContent || '').trim();
                if (txt && txt !== '自动登录' && txt !== '会员登录' && txt !== '个人登录') {
                    return txt;
                }
            }
            var metaT = document.querySelector('meta[name="citation_title"], meta[property="og:title"]');
            return metaT ? (metaT.content || '').trim() : '';
        """) or ""
        detail["title"] = _clean_scraped_cnki_title(pure_title)
    except Exception:
        detail["title"] = ""

    if not detail["title"]:
        try:
            page_title = driver.title or ""
            detail["title"] = _clean_scraped_cnki_title(page_title.split("-")[0].split("_")[0])
        except Exception:
            pass

    try:
        abstract_el = driver.find_element(By.CSS_SELECTOR, d_sel["abstract"])
        detail["abstract"] = abstract_el.text.strip()
    except NoSuchElementException:
        detail["abstract"] = ""

    try:
        kw_els = driver.find_elements(By.CSS_SELECTOR, d_sel["keywords"])
        detail["keywords"] = [
            kw.text.strip().rstrip(";；") for kw in kw_els if kw.text.strip()
        ]
    except Exception:
        detail["keywords"] = []

    institutions = []
    try:
        author_els = driver.find_elements(By.CSS_SELECTOR, d_sel["authors"])
        raw_authors = [a.text.strip() for a in author_els if a.text.strip()]
        authors, institutions = _split_authors_institutions(raw_authors)
        detail["authors"] = "; ".join(authors)
    except NoSuchElementException:
        detail["authors"] = ""

    try:
        org_els = driver.find_elements(By.CSS_SELECTOR, d_sel["orgs"])
        orgs = [o.text.strip() for o in org_els if o.text.strip()]
        detail["institutions"] = orgs if orgs else institutions
    except Exception:
        detail["institutions"] = institutions

    try:
        fund_els = driver.find_elements(By.CSS_SELECTOR, d_sel["funds"])
        detail["funds"] = [f.text.strip() for f in fund_els if f.text.strip()]
    except Exception:
        detail["funds"] = []

    # ── 卷期页码、DOI 与原生分章目录提取 ──────────────────
    _extract_pub_info(driver, detail)
    native_toc = _extract_detail_page_toc(driver)
    if native_toc:
        detail["native_toc"] = native_toc

    return detail


def _extract_detail_page_toc(driver) -> List[Dict[str, Any]]:
    """提取知网硕博学位论文/长文详情页自带的『分章下载/目录』结构（免打开 200 页 FlowPDF 秒拿目录）。"""
    try:
        raw_items = driver.execute_script(r"""
            var rows = document.querySelectorAll(
                '#DownLoadParts li, #DownLoadParts tr, .catalog-list li, #catalogDiv li, ul.catalog li'
            );
            var out = [];
            for (var i = 0; i < rows.length; i++) {
                var txt = (rows[i].innerText || rows[i].textContent || '').replace(/\s+/g, ' ').trim();
                if (!txt || txt.length < 2 || txt.length > 120) continue;
                txt = txt.replace(/\s*(?:分章下载|本章下载|下载|阅读)\s*/g, ' ').trim();
                if (txt) out.push(txt);
            }
            return out;
        """) or []
    except Exception:
        return []

    if not isinstance(raw_items, list) or len(raw_items) < 3:
        return []

    toc: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items:
        s = str(raw or "").strip()
        if not s:
            continue
        page_num: Optional[int] = None
        m_pg = re.search(r"[\s.…·\-_]+(\d{1,4})(?:\s*[-–—]\s*\d{1,4})?\s*$", s)
        if m_pg:
            page_num = int(m_pg.group(1))
            s = s[:m_pg.start()].strip()
        if not s or s in seen:
            continue
        seen.add(s)

        if re.match(r"^(?:第\s*[一二三四五六七八九十百零\d]+\s*[章篇部分]|[一二三四五六七八九十]+[、.．]|摘要|abstract|绪论|引言|结论|参考文献|致谢)", s, re.IGNORECASE):
            lvl = 1
        elif re.match(r"^(?:第\s*[一二三四五六七八九十百零\d]+\s*节|\d{1,2}\.\d{1,2}(?!\.\d)|[（(][一二三四五六七八九十]+[)）])", s):
            lvl = 2
        else:
            lvl = 3

        item: Dict[str, Any] = {
            "id": f"S{len(toc) + 1}",
            "index": len(toc) + 1,
            "level": lvl,
            "title": s,
        }
        if page_num is not None:
            item["page"] = page_num
        toc.append(item)

    return toc if len(toc) >= 3 else []


def _extract_pub_info(driver, detail: Dict[str, Any]):
    """从知网详情页提取期刊名、年份、卷、期、页码、DOI。

    知网详情页的出版信息分布在多个位置：
    - .top-tip / .list-identify: 期刊名 + 年卷期页（如 "2023,39(03):45-58"）
    - .top-tip a[href*=navi]: 期刊名链接
    - #catalog_ZCLK / .doc-top: DOI 等标识
    """
    # 1) 期刊名（从链接或 .top-tip 文本中提取）
    try:
        journal_el = driver.find_element(
            By.CSS_SELECTOR,
            ".top-tip a[href*='navi'], .list-identify a[href*='navi'], "
            ".top-tip a[href*='Journal'], a.journal-link"
        )
        journal = journal_el.text.strip().rstrip(". ．。")
        if journal and not detail.get("journal"):
            detail["journal"] = journal
    except (NoSuchElementException, Exception):
        pass

    # 2) 年/卷/期/页码 — 聚合 .top-tip 与 .total-inform（很多知网期刊页码写在 .total-inform 的「页码：65-76」而非 .top-tip）
    pub_text = ""
    try:
        pub_text = driver.execute_script(r"""
            var parts = [];
            document.querySelectorAll('.top-tip, .list-identify, .doc-top .info, .total-inform, .brief').forEach(function(el){
                var t = (el.innerText || el.textContent || '').trim();
                if (t) parts.push(t);
            });
            return parts.join(' \n ');
        """) or ""
    except Exception:
        try:
            tip_el = driver.find_element(By.CSS_SELECTOR, ".top-tip, .list-identify, .doc-top .info")
            pub_text = tip_el.text
        except (NoSuchElementException, Exception):
            pass

    if pub_text:
        year_m = re.search(r'((?:19|20)\d{2})\s*[,，年\s(（]', pub_text)
        if year_m and not detail.get("year"):
            detail["year"] = int(year_m.group(1))

        vol_m = re.search(r'(?:,|，)\s*(\d+)\s*\(', pub_text) or \
                re.search(r'第\s*(\d+)\s*卷', pub_text)
        if vol_m and not detail.get("volume"):
            detail["volume"] = vol_m.group(1)

        # 期号仅匹配数字或英文增刊代号（如 09 / S1 / Z1），严禁匹配刊名括号中的汉字副标题如 (中国传媒大学学报) / (人文社会科学版)
        issue_m = (
            re.search(r'(?:(?:19|20)\d{2}\s*(?:[,，]\s*\d+\s*)?)[(（]\s*([0-9A-Za-z]{1,6})\s*[)）]', pub_text)
            or re.search(r'[(（]\s*([0-9]{1,3}|[SZsz][0-9]{1,2})\s*[)）]', pub_text)
            or re.search(r'第\s*([0-9A-Za-z]{1,6})\s*期', pub_text)
            or re.search(r'No\.?\s*(\d+)', pub_text)
        )
        if issue_m and not detail.get("issue"):
            raw_iss = issue_m.group(1)
            detail["issue"] = (raw_iss.lstrip("0") or "0") if raw_iss.isdigit() else raw_iss

        # 页码：支持区间页 `45-58`、转页 `65-76+188` 及单页短文 `页码：45`
        pages_m = (
            re.search(r'页码\s*[:：]\s*(\d+(?:[-–—]\d+(?:\+\d+(?:[-–—]\d+)?)?)?)', pub_text)
            or re.search(r'\)\s*[:：]\s*(\d+(?:[-–—]\d+(?:\+\d+(?:[-–—]\d+)?)?)?)', pub_text)
            or re.search(r'(\d+[-–—]\d+(?:\+\d+(?:[-–—]\d+)?)?)\s*页', pub_text)
            or re.search(r'[Pp]\.?\s*(\d+[-–—]\d+)', pub_text)
        )
        if pages_m and not detail.get("pages"):
            detail["pages"] = pages_m.group(1).replace('–', '-').replace('—', '-')

    # 3) DOI — 多种策略提取
    if not detail.get("doi"):
        try:
            doi_el = driver.find_element(
                By.CSS_SELECTOR,
                ".doc-doi a, a[href*='doi.org'], .romark a[href*='doi.org']"
            )
            doi_text = doi_el.get_attribute("href") or doi_el.text
            doi_m = re.search(r'(10\.\d{4,}/[^\s]+)', doi_text)
            if doi_m:
                detail["doi"] = doi_m.group(1).rstrip(".")
        except (NoSuchElementException, Exception):
            pass

    if not detail.get("doi"):
        try:
            doi_text = driver.execute_script(r"""
                var els = document.querySelectorAll('.top-tip, .list-identify, .doc-top');
                for (var i = 0; i < els.length; i++) {
                    var m = els[i].textContent.match(/DOI[：:]\s*(10\.\d{4,}\/[^\s]+)/i);
                    if (m) return m[1];
                }
                var meta = document.querySelector('meta[name="citation_doi"]');
                if (meta) return meta.content;
                return '';
            """)
            if doi_text:
                detail["doi"] = doi_text.rstrip(".")
        except Exception:
            pass

    # 4) 优先提取 2026 版 kcms2 详情页隐藏 input 与 meta 结构化字段（最干净、无截断）
    try:
        hidden_info = driver.execute_script(r"""
            function val(ids) {
                for (var i = 0; i < ids.length; i++) {
                    var el = document.getElementById(ids[i]);
                    if (el && el.value && el.value.trim()) return el.value.trim();
                }
                return '';
            }
            var info = {
                filename: val(['param-filename', 'paramfilename']),
                dbcode: val(['paramdbcode', 'param-dbcode']),
                dbname: val(['paramdbname', 'param-dbname']),
                abstract_text: val(['abstract_text']),
                keywords_raw: val(['params-nvsmkeywors']),
                year: val(['article-year']),
                issue: val(['article-qi']),
                pages: val(['prite-page-num', 'page-pange']),
                online_first: false
            };
            var tip = document.querySelector('.top-tip, .wx-tit');
            if (tip && (tip.textContent || '').indexOf('网络首发') >= 0) {
                info.online_first = true;
            }
            var metas = document.querySelectorAll('meta[name]');
            metas.forEach(function(m) {
                var name = (m.getAttribute('name') || '').toLowerCase();
                if (name === 'citation_volume' && !info.volume) info.volume = m.content;
                if (name === 'citation_issue' && !info.issue) info.issue = m.content;
                if (name === 'citation_firstpage') info.firstpage = m.content;
                if (name === 'citation_lastpage') info.lastpage = m.content;
                if (name === 'citation_doi' && !info.doi) info.doi = m.content;
            });
            return info;
        """) or {}
        if isinstance(hidden_info, dict):
            fn = hidden_info.get("filename", "")
            dbc = hidden_info.get("dbcode", "")
            if fn and dbc:
                detail["filename"] = fn
                detail["dbcode"] = dbc
                detail["cnki_id"] = f"{dbc.upper()}:{fn.upper()}"
            elif fn:
                detail["filename"] = fn

            abs_hidden = hidden_info.get("abstract_text", "")
            if abs_hidden:
                import html as _html
                abs_hidden = re.sub(r"^\s*<正>\s*", "", _html.unescape(abs_hidden)).strip()
            if abs_hidden and len(abs_hidden) > len(detail.get("abstract", "")):
                detail["abstract"] = abs_hidden

            if not detail.get("keywords") and hidden_info.get("keywords_raw"):
                kws = [k.strip() for k in re.split(r"[;；]+", hidden_info["keywords_raw"]) if k.strip()]
                if kws:
                    detail["keywords"] = kws

            if hidden_info.get("year") and not detail.get("year"):
                try:
                    detail["year"] = int(str(hidden_info["year"])[:4])
                except ValueError:
                    pass
            if hidden_info.get("volume") and not detail.get("volume"):
                detail["volume"] = hidden_info["volume"]
            if hidden_info.get("issue") and not detail.get("issue"):
                detail["issue"] = str(hidden_info["issue"]).lstrip("0") or "0"
            if not detail.get("issue") and fn and str(dbc).upper().startswith("CJF"):
                m_fn = re.match(r"^[A-Za-z]{4}(?:19|20)\d{2}(\d{2}|[SZsz]\d)[0-9A-Za-z]+$", str(fn))
                if m_fn:
                    iss_code = m_fn.group(1).upper()
                    detail["issue"] = (iss_code.lstrip("0") or "0") if iss_code.isdigit() else iss_code
            if not detail.get("pages"):
                if hidden_info.get("pages"):
                    detail["pages"] = str(hidden_info["pages"]).replace("–", "-").replace("—", "-")
                else:
                    fp = hidden_info.get("firstpage", "")
                    lp = hidden_info.get("lastpage", "")
                    if fp and lp:
                        detail["pages"] = f"{fp}-{lp}"
                    elif fp:
                        detail["pages"] = fp
            if hidden_info.get("doi") and not detail.get("doi"):
                detail["doi"] = hidden_info["doi"].rstrip(".")
            if hidden_info.get("online_first") and not detail.get("pages"):
                detail["is_online_first"] = True
    except Exception:
        pass

    # 5) 终极兜底：若仍缺少页码且非网络首发，触发详情页自身的『引用』弹窗读取知网官方 GB/T 7714 文本提取页码
    if not detail.get("pages") and not detail.get("is_online_first"):
        try:
            from .search import _extract_pages_from_gbt7714
            clicked_q = driver.execute_script(r"""
                var btn = document.querySelector('li.btn-quote a, a.icon-quote, a[onclick*="getQuotes"]');
                if (btn && btn.offsetParent !== null) { btn.click(); return true; }
                return false;
            """)
            if clicked_q:
                time.sleep(0.8)
                gbt_txt = driver.execute_script(r"""
                    var rows = document.querySelectorAll('.quote-pop tr');
                    for (var i = 0; i < rows.length; i++) {
                        var l = rows[i].querySelector('.quote-l'), r = rows[i].querySelector('.quote-r');
                        if (l && r && l.textContent.indexOf('GB/T') >= 0) return r.textContent.trim();
                    }
                    var first = document.querySelector('.quote-pop .quote-r');
                    return first ? first.textContent.trim() : '';
                """) or ""
                driver.execute_script("document.querySelectorAll('.quote-pop, .layui-layer-shade').forEach(function(e){e.remove();});")
                if gbt_txt:
                    pg = _extract_pages_from_gbt7714(gbt_txt)
                    if pg:
                        detail["pages"] = pg
                    if not detail.get("gbt7714_raw"):
                        detail["gbt7714_raw"] = gbt_txt
        except Exception:
            pass


# ── HTML 全文阅读与稳定键缓存 ──────────────────────────

def _fulltext_cache_dir() -> Path:
    try:
        from config import cache_dir
        return cache_dir("fulltext", create=True)
    except ImportError:
        d = Path.cwd() / ".scholar-kit" / "fulltext"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _legacy_fulltext_cache_file(key: str) -> Path:
    return Path.cwd() / ".scholar-kit" / "fulltext" / f"{key}.json"


def _cache_key(url: str) -> str:
    """基于 URL 的兼容缓存键"""
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()


def _stable_paper_cache_keys(url: str, paper: Optional[Dict[str, Any]] = None) -> List[str]:
    """按稳定性优先级生成全文缓存键列表：
    1. `cnki_id` (`CJFQ:GLSJ202202013`) — 完全不受 `kcms2?v=...` 动态令牌变化影响
    2. 归一化 `title|first_author|year` MD5 — 列表页尚未拿到 `cnki_id` 时跨会话稳定命中
    3. 传统 `url` MD5 — 向后兼容已有缓存文件
    """
    keys: List[str] = []
    if isinstance(paper, dict):
        cid = str(paper.get("cnki_id") or "").strip()
        if cid:
            safe_cid = re.sub(r"[^A-Za-z0-9_-]", "_", cid)
            keys.append(f"id_{safe_cid}")
        title = re.sub(r"\W+", "", str(paper.get("title") or "")).lower()
        if title:
            authors = str(paper.get("authors") or "")
            first_au = re.split(r"[;；,，\s]+", authors.strip())[0] if authors.strip() else ""
            year = str(paper.get("year") or str(paper.get("date") or "")[:4] or "")
            meta_h = hashlib.md5(f"{title}|{first_au}|{year}".encode("utf-8")).hexdigest()
            keys.append(f"meta_{meta_h}")
    if url:
        u_key = _cache_key(url)
        if u_key not in keys:
            keys.append(u_key)
    return keys or [_cache_key(url)]


def _get_cache_ttl() -> int:
    try:
        from config import get as cfg_get
        return cfg_get("cache_ttl_days", 30)
    except ImportError:
        return int(os.environ.get("SCHOLAR_CACHE_TTL_DAYS", "30"))

_FULLTEXT_CACHE_TTL_DAYS = _get_cache_ttl()


def _load_cached_fulltext(url: str, paper: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    cdir = _fulltext_cache_dir()
    for key in _stable_paper_cache_keys(url, paper):
        cache_file = cdir / f"{key}.json"
        if not cache_file.exists():
            legacy = _legacy_fulltext_cache_file(key)
            if legacy.exists():
                cache_file = legacy
        if not cache_file.exists():
            continue
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if not data.get("fulltext"):
                continue
            ts = data.get("_cached_at", "")
            if ts and _FULLTEXT_CACHE_TTL_DAYS > 0:
                from datetime import datetime, timedelta
                try:
                    cached_at = datetime.fromisoformat(ts)
                    if datetime.now() - cached_at > timedelta(days=_FULLTEXT_CACHE_TTL_DAYS):
                        _log(f"[cnki-detail] 缓存已过期（>{_FULLTEXT_CACHE_TTL_DAYS}天），重新抓取")
                        continue
                except ValueError:
                    pass
            data["_cache_file"] = str(cache_file)
            return data
        except Exception as e:
            _log(f"[cnki-detail] 缓存读取失败: {e}")
    return None


def _save_cached_fulltext(url: str, data: Dict[str, Any], paper: Optional[Dict[str, Any]] = None) -> Path:
    from datetime import datetime
    data = dict(data)
    data["_cached_at"] = datetime.now().isoformat()
    primary_key = _stable_paper_cache_keys(url, paper)[0]
    cache_file = _fulltext_cache_dir() / f"{primary_key}.json"
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
    return cache_file


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


def _open_cnki_reading_tab(driver, href: str, referrer: str = "") -> Optional[str]:
    """在当前单标签页内原位携带详情页 Referer 导航至全文/原版阅读页。

    关键设计：既彻底免疫 Chrome 弹窗拦截，又避免 `switch_to.new_window` / `switch_to.window`
    触发底层 `Target.activateTarget` 导致后台隐藏浏览器无故弹到系统最前台抢焦点。
    """
    if not href:
        return None
    prev_url = ""
    try:
        prev_url = driver.current_url or ""
    except Exception:
        prev_url = ""
    ref = referrer or prev_url or "https://kns.cnki.net/"

    navigated = False
    if hasattr(driver, "execute_cdp_cmd"):
        try:
            driver.execute_cdp_cmd(
                "Page.navigate",
                {"url": href, "referrer": ref},
            )
            navigated = True
        except Exception:
            navigated = False
    if not navigated:
        _navigate_within_cnki(driver, href, base_delay=0.6)

    start_ts = time.time()
    deadline = start_ts + 15.0
    while time.time() < deadline:
        try:
            cur = driver.current_url or ""
            state = driver.execute_script("return document.readyState || '';") or ""
            elapsed = time.time() - start_ts
            # 单标签原位跳转时：必须等待 URL 真正离开旧详情页且完成 bar.cnki.net 鉴权中转跳转，避免在旧详情页上提前退出循环！
            left_prev = bool(cur and cur != "about:blank" and (cur != prev_url or href == prev_url))
            past_redirect_gate = ("bar.cnki.net" not in cur.lower()) or elapsed >= 5.5
            if left_prev and past_redirect_gate and state == "complete":
                break
        except Exception:
            pass
        time.sleep(0.3)
    time.sleep(1.5)
    return getattr(driver, "current_window_handle", "main") or "main"


def _find_flowpdf_href(driver) -> Optional[str]:
    """在知网详情页预先提取原版阅读链接（供单标签页原位切换兜底使用）。"""
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
        if href:
            return href
        dbcode = driver.execute_script("var e = document.getElementById('param-dbcode'); return e ? e.value : '';")
        dbname = driver.execute_script("var e = document.getElementById('param-dbname'); return e ? e.value : '';")
        filename = driver.execute_script("var e = document.getElementById('param-filename'); return e ? e.value : '';")
        if dbcode and filename:
            return f"https://kns.cnki.net/KXReader/Detail?DBCODE={dbcode}&DBNAME={dbname}&FILENAME={filename}"
    except Exception:
        pass
    return None


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

        if not _open_cnki_reading_tab(driver, href, referrer=detail_url):
            _log("[cnki-fulltext] 未能进入全文阅读页")
            return None

        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            _log("[cnki-fulltext] HTML 全文页加载超时")
            return None

        auth_wall = _detect_cnki_login_wall(driver)
        if auth_wall:
            _log(f"[cnki-fulltext] {auth_wall}")
            driver._cnki_auth_wall_reason = auth_wall
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

        # 等待 HTML 阅读器正文异步渲染就绪（防止 SPA 首帧尚未挂载段落即提前提取为空）
        render_deadline = time.time() + 8.0
        while time.time() < render_deadline:
            try:
                ready = driver.execute_script(r"""
                    var el = document.querySelector('.article-body, .main-text, .article, #content, .content, .doc');
                    if (el && (el.innerText || '').trim().length > 200) return true;
                    var ps = document.querySelectorAll('p');
                    var total = 0;
                    for (var i = 0; i < ps.length; i++) total += (ps[i].innerText || '').trim().length;
                    return total > 400;
                """)
                if ready or _detect_cnki_login_wall(driver):
                    break
            except Exception:
                pass
            time.sleep(0.4)

        return _extract_html_fulltext(driver)

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
        # 克隆正文容器并为 HTML 章节标题补充 Markdown 层级前缀，便于后续 --toc / --section 精确切片
        md_text = driver.execute_script(r"""
            var root = arguments[0].cloneNode(true);
            root.querySelectorAll('h1, h2, h3, h4, .section-title').forEach(function(h) {
                var tag = (h.tagName || '').toUpperCase();
                var prefix = tag === 'H1' ? '# ' : (tag === 'H2' ? '## ' : (tag === 'H3' || tag === 'H4' ? '### ' : '## '));
                var t = (h.textContent || '').trim();
                if (t && t.indexOf('#') !== 0) {
                    h.textContent = '\n' + prefix + t + '\n';
                }
            });
            return (root.innerText || root.textContent || '').trim();
        """, container) or ""
        if md_text:
            sections.append(md_text)
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


def _read_fulltext_flowpdf(
    driver,
    detail_url: str,
    *,
    toc_only: bool = False,
    max_chars: Optional[int] = None,
    preset_href: Optional[str] = None,
) -> Optional[str]:
    """KXReader 全文提取（硕博论文与无 HTML 期刊）。单标签原位导航 + 三级加速 + --toc/--max-chars 按需早停。"""
    driver._cnki_partial_fulltext = False

    try:
        href = preset_href or _find_flowpdf_href(driver)
        if not href:
            _log("[cnki-flowpdf] 未找到原版阅读链接，也无法构造 URL")
            return None

        _log(f"[cnki-flowpdf] 原版阅读链接: {href[:80]}")

        if not _open_cnki_reading_tab(driver, href, referrer=detail_url):
            _log("[cnki-flowpdf] 未能进入原版阅读页")
            return None

        auth_wall = _detect_cnki_login_wall(driver)
        if auth_wall:
            _log(f"[cnki-flowpdf] {auth_wall}")
            driver._cnki_auth_wall_reason = auth_wall
            return None

        try:
            WebDriverWait(driver, 18).until(
                EC.presence_of_element_located((By.TAG_NAME, "canvas"))
            )
        except TimeoutException:
            auth_wall_after = _detect_cnki_login_wall(driver)
            if auth_wall_after:
                _log(f"[cnki-flowpdf] {auth_wall_after}")
                driver._cnki_auth_wall_reason = auth_wall_after
            else:
                _log("[cnki-flowpdf] 页面加载超时（未发现 canvas）")
            return None

        driver = _handle_captcha(driver)

        total_pages = driver.execute_script(
            "return document.querySelectorAll('canvas').length;")
        if not total_pages:
            _log("[cnki-flowpdf] 未找到 canvas 元素，无法提取")
            return None

        scan_pages = total_pages
        if toc_only and total_pages > 18:
            scan_pages = 16
            driver._cnki_partial_fulltext = True
            _log(f"[cnki-flowpdf] 共 {total_pages} 页（--toc 目录快读模式仅扫描前 {scan_pages} 页）")
        else:
            _log(f"[cnki-flowpdf] 共 {total_pages} 页")

        # ── 方法 1: PDF.js API 直取（最快，~3s） ──────────────
        all_pages = {}
        try:
            driver.set_script_timeout(30)
            api_result = driver.execute_async_script("""
                var maxP = arguments[0];
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
                    var n = Math.min(doc.numPages, maxP);
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
            """, int(scan_pages))
            if api_result:
                all_pages = {int(k): v.strip() for k, v in api_result.items()
                             if v and v.strip()}
                if len(all_pages) >= scan_pages * 0.5:
                    _log(f"[cnki-flowpdf] PDF.js API 直取成功: "
                         f"{len(all_pages)}/{scan_pages} 页")
        except Exception as e:
            _log(f"[cnki-flowpdf] PDF.js API 不可用: {e}")

        # ── 方法 2: 批量滚动 + 一次性提取（支持 --max-chars 早停） ────
        stopped_for_max_chars = False
        if len(all_pages) < scan_pages * 0.5:
            _log("[cnki-flowpdf] 使用批量滚动模式...")
            char_goal = int(max_chars * 1.35) if (max_chars and max_chars > 0 and not toc_only) else 0
            for batch_end in range(5, scan_pages + 5, 5):
                target = min(batch_end, scan_pages) - 1
                driver.execute_script(f"""
                    var c = document.querySelectorAll('canvas');
                    if (c.length > {target})
                        c[{target}].scrollIntoView({{block: 'center'}});
                """)
                time.sleep(0.5)
                if batch_end % 50 == 0:
                    driver = _handle_captcha(driver)
                if char_goal and batch_end < scan_pages and batch_end % 10 == 0:
                    partial_now = _flowpdf_extract_all_textlayers(driver, min(batch_end, scan_pages))
                    if sum(len(v) for v in partial_now.values()) >= char_goal:
                        all_pages = partial_now
                        stopped_for_max_chars = True
                        driver._cnki_partial_fulltext = True
                        _log(f"[cnki-flowpdf] 已达 --max-chars 字数目标，提前在第 {min(batch_end, scan_pages)}/{total_pages} 页停止滚动")
                        break

            if not stopped_for_max_chars:
                time.sleep(2.5)
                all_pages = _flowpdf_extract_all_textlayers(driver, scan_pages)
            _log(f"[cnki-flowpdf] 批量提取: {len(all_pages)}/{scan_pages} 页")

        # ── 方法 3: 逐页补漏（仅处理缺失页） ─────────────────
        if not stopped_for_max_chars and 0 < len(all_pages) < scan_pages * 0.8:
            missing = [p for p in range(1, scan_pages + 1)
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
                all_pages = _flowpdf_extract_all_textlayers(driver, scan_pages)
                _log(f"[cnki-flowpdf] 补漏后: {len(all_pages)}/{scan_pages} 页")

        if not all_pages:
            _log("[cnki-flowpdf] 未能提取到任何文本")
            return None

        parts = [f"[第 {p} 页]\n{all_pages[p]}" for p in sorted(all_pages.keys())]
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

    except Exception as e:
        _log(f"[cnki-flowpdf] 提取失败: {e}")
        return None


# ── 本地已下载 PDF 极速提取（免网页滑块） ─────────────────

def _extract_local_pdf_fulltext(
    title: str,
    save_dir: Optional[str] = None,
    explicit_pdf: Optional[str] = None,
) -> Optional[str]:
    """若目标论文已关联本地 PDF（如 Zotero 附件）或在 `./papers/pdf` 下载过，直接提取带页码正文。"""
    matched_pdf: Optional[Path] = None
    if explicit_pdf:
        cand = Path(explicit_pdf)
        if cand.is_file() and cand.suffix.lower() == ".pdf":
            matched_pdf = cand

    if matched_pdf is None:
        if not title:
            return None
        title_clean = re.sub(r'[\s\-_：:""\'\'【】（）()]+', '', title).lower()
        if len(title_clean) < 4:
            return None
        core = title_clean[:min(len(title_clean), 20)]

        try:
            from config import get as cfg_get
            base_dir = Path(save_dir or cfg_get("save_dir", "./papers"))
        except ImportError:
            base_dir = Path(save_dir or "./papers")

        search_dirs = [base_dir / "pdf", base_dir]
        for d in search_dirs:
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.suffix.lower() != ".pdf" or not f.is_file():
                    continue
                fname_clean = re.sub(r"[\s\-_.]", "", f.stem).lower()
                if core in fname_clean:
                    matched_pdf = f
                    break
            if matched_pdf:
                break

    if not matched_pdf:
        return None

    pages_text: List[str] = []
    try:
        import pdfplumber
        with pdfplumber.open(str(matched_pdf)) as pdf:
            for idx, page in enumerate(pdf.pages, 1):
                txt = (page.extract_text() or "").strip()
                if txt:
                    pages_text.append(f"[第 {idx} 页]\n{txt}")
    except ImportError:
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(matched_pdf))
            for idx, page in enumerate(reader.pages, 1):
                txt = (page.extract_text() or "").strip()
                if txt:
                    pages_text.append(f"[第 {idx} 页]\n{txt}")
        except Exception as e:
            _log(f"[cnki-detail] 本地 PDF 读取失败 ({matched_pdf.name}): {e}")
            return None
    except Exception as e:
        _log(f"[cnki-detail] 本地 PDF 提取异常 ({matched_pdf.name}): {e}")
        return None

    combined = "\n\n".join(pages_text).strip()
    if len(combined) > 200:
        _log(f"[cnki-detail] 命中本地已下载 PDF ({matched_pdf.name})，直接提取 {len(combined)} 字（免网页全文滑块）")
        return combined
    return None


# ── 批量详情 ──────────────────────────────────────────

def batch_read_detail(
    papers: List[Dict[str, Any]],
    top_n: int = 5,
    fulltext: bool = False,
    toc_only: bool = False,
    max_chars: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """批量获取论文详情（摘要+可选全文），复用同一个浏览器会话。"""
    if not papers:
        return []

    targets = [p for p in papers if p.get("url")][:top_n]
    if not targets:
        _log("[cnki-detail] 没有可访问的论文 URL")
        return papers

    # 先检查缓存与本地 PDF，若全部命中则连浏览器都无需访问
    need_web_targets: List[Dict[str, Any]] = []
    for paper in targets:
        url = paper["url"]
        if fulltext:
            cached = _load_cached_fulltext(url, paper=paper)
            if cached:
                _log(f"[cnki-detail] 命中稳定键缓存: {paper.get('title', '')[:35]}")
                cache_path = cached.get("_cache_file", "")
                for k, v in cached.items():
                    if k.startswith("_"):
                        continue
                    if k == "fulltext":
                        paper["has_fulltext"] = True
                        paper["fulltext"] = v
                        paper["fulltext_length"] = len(v)
                        if cache_path:
                            paper["fulltext_cache"] = cache_path
                    elif v and not paper.get(k):
                        paper[k] = v
                continue
            local_ft = _extract_local_pdf_fulltext(paper.get("title", ""), explicit_pdf=paper.get("local_pdf"))
            if local_ft and paper.get("abstract"):
                saved = _save_cached_fulltext(url, {
                    "abstract": paper.get("abstract", ""),
                    "keywords": paper.get("keywords", []),
                    "fulltext": local_ft,
                    "fulltext_length": len(local_ft),
                }, paper=paper)
                paper["has_fulltext"] = True
                paper["fulltext"] = local_ft
                paper["fulltext_length"] = len(local_ft)
                paper["fulltext_cache"] = str(saved)
                continue
        else:
            # 非全文模式下，若会话中该文献已具备完整摘要与页码（或属学位论文/真网络首发），直接跳过重复打开网页
            has_valid_issue = bool(paper.get("issue")) and not any("\u4e00" <= ch <= "\u9fff" for ch in str(paper.get("issue", "")))
            if paper.get("abstract") and (
                (paper.get("pages") and (has_valid_issue or not paper.get("issue")))
                or paper.get("doc_type") in ("thesis", "master", "doctor")
                or (paper.get("is_online_first") and not paper.get("pages"))
            ):
                _log(f"[cnki-detail] 会话已含完整摘要与题录，跳过重复请求: {paper.get('title', '')[:35]}")
                continue
        need_web_targets.append(paper)

    if not need_web_targets:
        return papers

    _log(f"[cnki-detail] 将通过浏览器获取 {len(need_web_targets)} 篇论文的{'全文' if fulltext else '摘要/元数据'}")

    driver = None
    try:
        browser = _detect_browser()
        driver = _create_driver(browser=browser)

        _prepare_cnki_session(driver)
        _save_cookies(driver)
        auth_blocked_reason: Optional[str] = None

        for idx, paper in enumerate(need_web_targets):
            url = paper["url"]
            _log(f"\n[cnki-detail] === {idx + 1}/{len(need_web_targets)}: {paper.get('title', '')[:40]} ===")

            try:
                _navigate_within_cnki(driver, url)
                driver = _handle_captcha(driver)

                if _is_cnki_error_or_404(driver) and paper.get("title"):
                    from .search import refresh_cnki_url
                    refreshed = refresh_cnki_url(
                        driver,
                        title=paper.get("title", ""),
                        journal=paper.get("journal", ""),
                    )
                    if refreshed:
                        url = refreshed
                        paper["url"] = refreshed
                        _navigate_within_cnki(driver, url)
                        driver = _handle_captcha(driver)

                detail = _parse_detail_page(driver)
                for k, v in detail.items():
                    if v and not paper.get(k):
                        paper[k] = v
                if paper.get("pages"):
                    paper["is_online_first"] = False

                if fulltext:
                    if toc_only and isinstance(paper.get("native_toc"), list) and len(paper["native_toc"]) >= 3:
                        _log(f"[cnki-detail] 详情页直接提取到原生章节目录 ({len(paper['native_toc'])} 节)，跳过打开全文页")
                        continue

                    # 拿到 cnki_id 后再次检查是否命中稳定键缓存或本地 PDF
                    cached = _load_cached_fulltext(url, paper=paper)
                    if cached:
                        ft = cached["fulltext"]
                        paper["has_fulltext"] = True
                        paper["fulltext"] = ft
                        paper["fulltext_length"] = len(ft)
                        paper["fulltext_cache"] = cached.get("_cache_file", "")
                        continue

                    ft = _extract_local_pdf_fulltext(paper.get("title", ""), explicit_pdf=paper.get("local_pdf"))
                    if not ft and auth_blocked_reason:
                        paper["has_fulltext"] = False
                        paper["auth_required"] = True
                        paper["auth_reason"] = auth_blocked_reason
                        continue

                    driver._cnki_partial_fulltext = False
                    preset_flow_href = _find_flowpdf_href(driver)
                    if not ft:
                        ft = _read_fulltext_html(driver, url)
                    wall_now = getattr(driver, "_cnki_auth_wall_reason", None)
                    if not ft and not wall_now:
                        _log("[cnki-detail] HTML阅读不可用，尝试原版阅读(FlowPDF)...")
                        ft = _read_fulltext_flowpdf(
                            driver,
                            url,
                            toc_only=toc_only,
                            max_chars=max_chars,
                            preset_href=preset_flow_href,
                        )
                        wall_now = getattr(driver, "_cnki_auth_wall_reason", None)

                    if ft:
                        is_partial = bool(getattr(driver, "_cnki_partial_fulltext", False))
                        if not is_partial:
                            saved = _save_cached_fulltext(url, {
                                "cnki_id": paper.get("cnki_id", ""),
                                "abstract": paper.get("abstract", ""),
                                "keywords": paper.get("keywords", []),
                                "fulltext": ft,
                                "fulltext_length": len(ft),
                            }, paper=paper)
                            paper["fulltext_cache"] = str(saved)
                        paper["has_fulltext"] = True
                        paper["fulltext"] = ft
                        paper["fulltext_length"] = len(ft)
                        _log(f"[cnki-detail] 全文获取成功: {len(ft)} 字{'（按需早停片段，未覆盖全本缓存）' if is_partial else ''}")
                    elif wall_now:
                        auth_blocked_reason = str(wall_now)
                        paper["has_fulltext"] = False
                        paper["auth_required"] = True
                        paper["auth_reason"] = auth_blocked_reason
                        _log("[cnki-detail] 首篇已确认当前无全文权限，后续篇目仅提取免费摘要并跳过重复全文点击")
                    else:
                        _log("[cnki-detail] HTML阅读和原版阅读均失败，仅保留摘要")
                        paper["has_fulltext"] = False
                else:
                    _log(f"[cnki-detail] 摘要: {paper.get('abstract', '')[:60]}...")

            except Exception as e:
                _log(f"[cnki-detail] 获取详情失败: {e}")
                paper["detail_error"] = str(e)
                continue

            if idx < len(need_web_targets) - 1:
                time.sleep(0.8)

        return papers

    except Exception as e:
        _log(f"[cnki-detail] 批量详情获取失败: {e}")
        return papers
    finally:
        if driver is not None:
            _release_driver(driver)


# ── 知网原生双向引文网络 (`#refpartdiv` 引证文献 / 参考文献) ──────────────

def parse_cnki_citation_item(text: str, href: str = "") -> Optional[Dict[str, Any]]:
    """解析知网详情页 `#refpartdiv` 单条引文文本，例如:
    `[1] 现实需要如何推动经济学在中国的发展[J]. 黄少安.经济学动态,2021(05)`
    `[2] 数字普惠金融研究[D]. 李明.中央财经大学,2023`
    """
    raw = re.sub(r"\s+", " ", (text or "")).strip()
    if not raw:
        return None
    body = re.sub(r"^\[\d+\]\s*", "", raw).strip()
    if not body:
        return None

    title = ""
    doc_tag = "J"
    rest = ""

    m_tag = re.match(r"^(.+?)\[([A-Za-z/]+)\]\s*[.．]?\s*(.*)$", body)
    if m_tag:
        title = m_tag.group(1).strip()
        doc_tag = m_tag.group(2).upper()
        rest = m_tag.group(3).strip()
    else:
        parts = re.split(r"[.．]\s*", body, maxsplit=1)
        title = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""

    if not title:
        return None

    year = None
    volume = ""
    issue = ""
    pages = ""
    authors = ""
    journal = ""

    # 匹配 `2020, 36(10): 65-76+188` / `2020(05): 12-25` / `2023`
    y_m = re.search(
        r"((?:19|20)\d{2})"
        r"(?:\s*[,，]\s*(\d+))?"
        r"(?:\s*[\(（]\s*([A-Za-z0-9]+)\s*[\)）])?"
        r"(?:\s*[:：]\s*([A-Za-z0-9]+(?:\s*[-–—]\s*[A-Za-z0-9]+)?(?:\+\d+)?))?",
        rest,
    )
    if y_m:
        year = int(y_m.group(1))
        if y_m.group(2):
            volume = y_m.group(2).strip()
        if y_m.group(3):
            issue = y_m.group(3).lstrip("0") or "0"
        if y_m.group(4):
            pages = re.sub(r"\s*[-–—]\s*", "-", y_m.group(4).strip())
        before_year = rest[:y_m.start()].rstrip(" ,，.．:：")
    else:
        before_year = rest.rstrip(" ,，.．:：")

    if before_year:
        # 切分「作者.期刊/单位」：最后一个句点之后通常是期刊或学位授予单位
        dot_parts = [p.strip() for p in re.split(r"[.．]", before_year) if p.strip()]
        if len(dot_parts) >= 2:
            journal = dot_parts[-1]
            authors = ", ".join(dot_parts[:-1])
        else:
            authors = dot_parts[0]

    tag_to_doctype = {"J": "journal", "J/OL": "journal", "D": "thesis", "M": "book", "C": "conference", "N": "newspaper", "R": "report"}
    entry: Dict[str, Any] = {
        "title": title,
        "authors": authors,
        "journal": journal,
        "year": year,
        "doc_tag": doc_tag,
        "doc_type": tag_to_doctype.get(doc_tag, "journal"),
        "url": href or "",
        "raw": raw,
        "source": "CNKI",
    }
    if doc_tag in ("D", "M", "R") and journal:
        entry["publisher"] = journal
    if volume:
        entry["volume"] = volume
    if issue:
        entry["issue"] = issue
    if pages:
        entry["pages"] = pages
    return entry


def _extract_visible_citnet_items(driver, tab_kind: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    """提取当前激活且可见的 `#refpartdiv` 引文条目（按 `citations` / `references` 容器隔离并支持模块内翻页）。"""
    collected: List[Dict[str, Any]] = []
    seen_titles: set[str] = set()
    max_pages = max(1, (limit + 9) // 10)
    prefix = (
        f"nxgp-kcms-data-ref-{tab_kind}-"
        if tab_kind in ("citations", "references", "coreferences", "cocitations")
        else ""
    )

    for _page in range(max_pages):
        raw_items = driver.execute_script(r"""
            var prefix = arguments[0];
            var refDiv = document.querySelector('#refpartdiv');
            if (!refDiv) return [];
            var containers = prefix
                ? Array.from(refDiv.querySelectorAll('[id^="' + prefix + '"]'))
                : [refDiv];
            if (!containers.length) containers = [refDiv];
            var out = [];
            for (var c = 0; c < containers.length; c++) {
                var box = containers[c];
                if (prefix && box.offsetParent === null && box.querySelectorAll('li').length === 0) continue;
                var lis = box.querySelectorAll('li');
                for (var i = 0; i < lis.length; i++) {
                    var li = lis[i];
                    if (!prefix && li.offsetParent === null) continue;
                    var txt = (li.textContent || '').trim();
                    if (!/^\[\d+\]/.test(txt)) continue;
                    var a = li.querySelector('a[href]');
                    out.push({text: txt, href: a ? a.href : ''});
                }
            }
            return out;
        """, prefix) or []

        added_this_page = 0
        for item in raw_items:
            parsed = parse_cnki_citation_item(item.get("text", ""), item.get("href", ""))
            if not parsed:
                continue
            key = parsed["title"].lower()
            if key not in seen_titles:
                seen_titles.add(key)
                collected.append(parsed)
                added_this_page += 1
                if len(collected) >= limit:
                    return collected

        if len(collected) >= limit or added_this_page == 0:
            break

        clicked_next = driver.execute_script(r"""
            var prefix = arguments[0];
            var refDiv = document.querySelector('#refpartdiv');
            if (!refDiv) return false;
            var scope = prefix ? refDiv.querySelector('[id^="' + prefix + '"][class*="pageBar"]') : null;
            var links = (scope || refDiv).querySelectorAll('a.next, .pageBar a, a');
            for (var i = 0; i < links.length; i++) {
                var a = links[i];
                if (a.offsetParent === null) continue;
                var t = (a.textContent || '').trim();
                var cls = (a.className || '').toLowerCase();
                if ((t === '下一页' || cls.indexOf('next') >= 0) && cls.indexOf('disabled') < 0) {
                    a.click();
                    return true;
                }
            }
            return false;
        """, prefix)
        if not clicked_next:
            break
        time.sleep(1.0)

    return collected


def _activate_and_wait_citnet_tab(driver, tab_kind: str, timeout: float = 6.0) -> bool:
    """激活 `ul.module-tab li[data-id="<tab_kind>"]` 并等待对应 `nxgp-kcms-data-ref-<tab_kind>-*` 容器加载数据。"""
    clicked = driver.execute_script(r"""
        var kind = arguments[0];
        var sel = 'ul.module-tab li[data-id="' + kind + '"], ul.module-tab li.' + kind + ', #refpartdiv li.' + kind;
        var li = document.querySelector(sel);
        if (!li) return false;
        li.scrollIntoView({block: 'center'});
        li.click();
        if (typeof window.changeRefTypeTag === 'function') {
            try { window.changeRefTypeTag(li); } catch (e) {}
        }
        return true;
    """, tab_kind)
    if not clicked:
        return False

    prefix = f"nxgp-kcms-data-ref-{tab_kind}-"
    deadline = time.time() + timeout
    while time.time() < deadline:
        has_rows = driver.execute_script(r"""
            var prefix = arguments[0];
            var boxes = document.querySelectorAll('[id^="' + prefix + '"]');
            for (var i = 0; i < boxes.length; i++) {
                var lis = boxes[i].querySelectorAll('li');
                for (var j = 0; j < lis.length; j++) {
                    if (/^\[\d+\]/.test((lis[j].textContent || '').trim())) return true;
                }
            }
            return false;
        """, prefix)
        if has_rows:
            return True
        time.sleep(0.35)
    return False


def get_cnki_citations(
    target: str,
    direction: str = "both",
    limit: int = 20,
) -> Dict[str, Any]:
    """获取中文论文在知网详情页原生的双向引文网络（无需个人账号登录）：
    - `citing` (`li.citations` / `a#citations`): 引证文献（谁引用了它）
    - `references` (`li.references` / `a#references`): 参考文献（它引用了谁）
    支持传入知网详情页 URL、`cnki:<中文题名>` 或直接传入中文论文标题。
    """
    raw_target = (target or "").strip()
    if raw_target.lower().startswith("cnki:"):
        raw_target = raw_target[5:].strip()
    if not raw_target:
        return {"status": "error", "code": "NO_PAPER_ID", "message": "未提供知网论文 URL 或中文题名"}

    # 读取 7 天引文缓存
    cache_file: Optional[Path] = None
    try:
        from config import cache_dir
        from datetime import datetime, timedelta
        c_key = hashlib.md5(f"{raw_target}|{direction}|{limit}".encode("utf-8")).hexdigest()[:16]
        cache_file = cache_dir("cache", create=True) / f"cnki_citnet_{c_key}.json"
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            ts = cached.get("_cached_at", "")
            if ts and datetime.now() - datetime.fromisoformat(ts) <= timedelta(days=7):
                return cached["data"]
    except Exception:
        pass

    driver = None
    try:
        browser = _detect_browser()
        driver = _create_driver(browser=browser)

        _prepare_cnki_session(driver)

        detail_url = raw_target if raw_target.lower().startswith("http") else ""
        if not detail_url:
            from .search import refresh_cnki_url
            detail_url = refresh_cnki_url(driver, title=raw_target) or ""
            if not detail_url:
                return {
                    "status": "error",
                    "code": "RESOLVE_FAILED",
                    "message": f"未能在知网定位论文: {raw_target}",
                }

        cit_url = detail_url if "anchor=citnet" in detail_url else (
            f"{detail_url}&anchor=citnet" if "?" in detail_url else f"{detail_url}?anchor=citnet"
        )
        _navigate_within_cnki(driver, cit_url)
        driver = _handle_captcha(driver)

        if _is_cnki_error_or_404(driver) and not raw_target.lower().startswith("http"):
            return {"status": "error", "code": "CNKI_URL_EXPIRED", "message": "知网详情链接已失效 (404)"}

        paper_meta = _parse_detail_page(driver)
        paper_meta["url"] = detail_url
        paper_meta["source"] = "CNKI"

        # 滚动到引文网络区域触发懒加载
        driver.execute_script(r"""
            var ref = document.querySelector('#refpartdiv');
            if (ref) ref.scrollIntoView({block: 'center'});
            else window.scrollTo(0, document.body.scrollHeight * 0.7);
        """)

        deadline = time.time() + 8.0
        while time.time() < deadline:
            has_items = driver.execute_script(r"""
                var ref = document.querySelector('#refpartdiv');
                if (!ref) return false;
                var lis = ref.querySelectorAll('li');
                for (var i = 0; i < lis.length; i++) {
                    if (/^\[\d+\]/.test((lis[i].textContent || '').trim())) return true;
                }
                return false;
            """)
            if has_items:
                break
            time.sleep(0.4)

        tab_totals = driver.execute_script(r"""
            var mapEl = document.querySelector('#quoted-refcountpart, #refpartdiv');
            var txt = mapEl ? (mapEl.innerText || mapEl.textContent || '') : '';
            var mCit = txt.match(/(?:^|[^级])引证文献\s*\(\s*(\d+)\s*\)/);
            var mRef = txt.match(/(?:^|[^级])参考文献\s*\(\s*(\d+)\s*\)/);
            var mCoref = txt.match(/共引文献\s*\(\s*(\d+)\s*\)/);
            var mCocit = txt.match(/同被引文献\s*\(\s*(\d+)\s*\)/);
            return {
                citing_total: mCit ? parseInt(mCit[1], 10) : null,
                references_total: mRef ? parseInt(mRef[1], 10) : null,
                coreferences_total: mCoref ? parseInt(mCoref[1], 10) : null,
                cocitations_total: mCocit ? parseInt(mCocit[1], 10) : null
            };
        """) or {}

        citing_list: List[Dict[str, Any]] = []
        references_list: List[Dict[str, Any]] = []
        coref_list: List[Dict[str, Any]] = []
        cocited_list: List[Dict[str, Any]] = []

        if direction in ("citing", "both"):
            _activate_and_wait_citnet_tab(driver, "citations", timeout=6.0)
            citing_list = _extract_visible_citnet_items(driver, tab_kind="citations", limit=limit)

        if direction in ("cited", "both"):
            _activate_and_wait_citnet_tab(driver, "references", timeout=6.0)
            references_list = _extract_visible_citnet_items(driver, tab_kind="references", limit=limit)

        if direction == "coref":
            _activate_and_wait_citnet_tab(driver, "coreferences", timeout=6.0)
            coref_list = _extract_visible_citnet_items(driver, tab_kind="coreferences", limit=limit)

        if direction == "cocited":
            _activate_and_wait_citnet_tab(driver, "cocitations", timeout=6.0)
            cocited_list = _extract_visible_citnet_items(driver, tab_kind="cocitations", limit=limit)

        result = {
            "status": "success" if paper_meta.get("title") else "partial",
            "source": "CNKI",
            "paper": {
                "title": paper_meta.get("title", ""),
                "authors": paper_meta.get("authors", ""),
                "journal": paper_meta.get("journal", ""),
                "year": paper_meta.get("year"),
                "doi": paper_meta.get("doi", ""),
                "cnki_id": paper_meta.get("cnki_id", ""),
                "url": detail_url,
                "cited_by_count": tab_totals.get("citing_total") or len(citing_list),
                "references_total": tab_totals.get("references_total") or len(references_list),
                "coreferences_total": tab_totals.get("coreferences_total"),
                "cocitations_total": tab_totals.get("cocitations_total"),
            },
            "citing_count": len(citing_list),
            "references_count": len(references_list),
            "citing": citing_list,
            "references": references_list,
        }
        if direction == "coref":
            result["coreferences_count"] = len(coref_list)
            result["coreferences"] = coref_list
        elif direction == "cocited":
            result["cocitations_count"] = len(cocited_list)
            result["cocitations"] = cocited_list

        if cache_file and result["status"] == "success":
            try:
                from datetime import datetime
                cache_file.write_text(
                    json.dumps({"_cached_at": datetime.now().isoformat(), "data": result}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return result
    except Exception as e:
        return {"status": "error", "code": "CNKI_CITATIONS_FAILED", "message": str(e)}
    finally:
        if driver is not None:
            _release_driver(driver)
