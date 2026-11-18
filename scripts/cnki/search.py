"""cnki.search - 知网搜索（含双引擎高级/专业检索）、前置筛选、AJAX 同步翻页与结果解析"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    CNKI_ADV_SEARCH_URL,
    CNKI_SEARCH_URL,
    CNKI_SIDEBAR_CORES,
    HAS_SELENIUM,
    REQUEST_INTERVAL,
    _log,
)
from .driver import (
    _create_driver,
    _detect_browser,
    _handle_captcha,
    _load_cookies,
    _release_driver,
    _safe_get,
    _save_cookies,
    check_cnki_access,
)
from .selectors import CNKI_SELECTORS, save_adv_dom_diagnosis

if HAS_SELENIUM:
    from selenium.common.exceptions import (
        ElementClickInterceptedException,
        NoSuchElementException,
        StaleElementReferenceException,
        TimeoutException,
    )
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait


# ── 字段映射与专业检索式编译器 ──────────────────────────────────────────

_ADV_FIELD_MAP: Dict[str, str] = {
    "主题": "SU",
    "篇关摘": "TKA",
    "篇名": "TI",
    "题名": "TI",
    "关键词": "KY",
    "摘要": "AB",
    "全文": "FT",
    "作者": "AU",
    "第一作者": "FI",
    "第一责任人": "FI",
    "通讯作者": "RP",
    "机构": "AF",
    "单位": "AF",
    "作者单位": "AF",
    "来源": "LY",
    "期刊": "LY",
    "文献来源": "LY",
    "期刊名称": "LY",
    "基金": "FU",
    "基金资助": "FU",
    "DOI": "DOI",
}

_VALID_FIELD_CODES = {
    "SU", "TKA", "TI", "KY", "AB", "FT",
    "AU", "FI", "RP", "AF", "LY", "FU", "DOI", "CLC",
}

# 新版 KNS8S #gradetxt 下拉菜单文本候选（用于多行表单兜底）
_CODE_TO_LABELS: Dict[str, Tuple[str, ...]] = {
    "SU": ("主题",),
    "TKA": ("篇关摘",),
    "TI": ("篇名", "题名"),
    "KY": ("关键词",),
    "AB": ("摘要",),
    "FT": ("全文",),
    "AU": ("作者",),
    "FI": ("第一作者", "第一责任人"),
    "RP": ("通讯作者",),
    "AF": ("作者单位", "机构", "单位"),
    "LY": ("文献来源", "期刊名称", "来源"),
    "FU": ("基金", "基金资助"),
    "DOI": ("DOI",),
}


def resolve_cnki_field_code(field: Optional[str]) -> str:
    """将中文检索字段名或代码统一解析为 KNS8S 标准字段代码。"""
    if not field:
        return "SU"
    cleaned = field.strip()
    if cleaned in _ADV_FIELD_MAP:
        return _ADV_FIELD_MAP[cleaned]
    upper = cleaned.upper()
    if upper in _VALID_FIELD_CODES:
        return upper
    raise ValueError(
        f"不支持的知网检索字段 '{field}'；可选: 主题/篇关摘/篇名/关键词/摘要/全文/作者/第一作者/机构/来源/基金/DOI"
    )


def _clean_cnki_value(val: str, *, strip_book_marks: bool = False) -> Tuple[str, bool]:
    """清理检索词中的危险单引号与书名号，返回 (cleaned_value, had_book_marks)。"""
    raw = (val or "").strip()
    had_book = raw.startswith("《") and raw.endswith("》") and len(raw) > 2
    if strip_book_marks:
        raw = raw.strip("《》〈〉").strip()
    # 剔除英文单引号，防止破坏 KNS8S 专业检索字符串字面量
    cleaned = raw.replace("'", " ").replace("\r", " ").replace("\n", " ").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned, had_book


def _normalize_raw_expression(expr: str) -> str:
    """规范化用户/Agent 传入的专业检索式（如将兼容写法 `AF%='...'` 转为知网 KNS8S 标准 `AF % '...'`）。"""
    s = (expr or "").strip()
    # KNS8S 专业检索模糊算符为 `%`（如 `AF % '清华大学'`），兼容转换误写的 `%=`
    s = re.sub(r"\b([A-Za-z]{2,4})\s*%=\s*'", r"\1 % '", s)
    return s


def _validate_raw_expression(expr: str) -> Optional[str]:
    """校验用户/Agent 传入的专业检索式基本语法合法性；合法返回 None，非法返回错误说明。"""
    s = (expr or "").strip()
    if not s:
        return "专业检索表达式为空"
    depth = 0
    in_quote = False
    for ch in s:
        if ch == "'":
            in_quote = not in_quote
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    return "检索式括号不匹配（右括号多余）"
    if in_quote:
        return "检索式单引号未闭合（知网专业检索值须使用成对英文单引号，如 SU='乡村振兴'）"
    if depth != 0:
        return "检索式括号未闭合"
    if re.search(r"\bYE\b", s, re.IGNORECASE):
        return "知网 KNS8S 专业检索不支持 YE 年份字段，请改用 --year-from / --year-to 参数设置出版年度"
    if re.search(r"\b(?:AND|OR|NOT)\s*\(\s*'", s, re.IGNORECASE):
        return (
            "AND/OR 后缺少字段代码（例如应写为 `TKA=('词1' + '词2') AND TKA=('词3' + '词4')` "
            "或同字段内写为 `TKA=('词1' + '词2') * ('词3' + '词4')`）"
        )
    return None


def build_cnki_expression(
    keyword: Optional[str] = None,
    *,
    field: Optional[str] = None,
    author: Optional[str] = None,
    first_author: Optional[str] = None,
    affiliation: Optional[str] = None,
    journal: Optional[str] = None,
    fund: Optional[str] = None,
    expr: Optional[str] = None,
    fuzzy: bool = False,
) -> str:
    """编译标准知网 KNS8S 专业检索表达式（精确用 `=`，模糊用 `%`）。"""
    clauses: List[str] = []

    if expr and expr.strip():
        norm_expr = _normalize_raw_expression(expr)
        err = _validate_raw_expression(norm_expr)
        if err:
            raise ValueError(err)
        clauses.append(norm_expr)

    if keyword and keyword.strip():
        code = resolve_cnki_field_code(field)
        val, _ = _clean_cnki_value(keyword)
        if val:
            if code in ("AU", "FI", "RP", "DOI"):
                clauses.append(f"{code}='{val}'")
            elif code in ("AF", "FU"):
                clauses.append(f"{code} % '{val}'")
            elif fuzzy:
                clauses.append(f"{code} % '{val}'")
            else:
                clauses.append(f"{code}='{val}'")

    if author and author.strip():
        val, _ = _clean_cnki_value(author)
        if val:
            clauses.append(f"AU='{val}'")

    if first_author and first_author.strip():
        val, _ = _clean_cnki_value(first_author)
        if val:
            clauses.append(f"FI='{val}'")

    if affiliation and affiliation.strip():
        val, _ = _clean_cnki_value(affiliation)
        if val:
            # 单位字段含院系后缀（如“清华大学公共管理学院”），使用 `%` 模糊包含防止漏检
            clauses.append(f"AF % '{val}'")

    if journal and journal.strip():
        val, had_book = _clean_cnki_value(journal, strip_book_marks=True)
        if val:
            if had_book and not fuzzy:
                clauses.append(f"LY='{val}'")
            else:
                clauses.append(f"LY % '{val}'")

    if fund and fund.strip():
        val, _ = _clean_cnki_value(fund)
        if val:
            clauses.append(f"FU % '{val}'")

    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0]
    wrapped = [f"({c})" if (" AND " in c.upper() or " OR " in c.upper() or "+" in c) else c for c in clauses]
    return " AND ".join(wrapped)


def _build_structured_conditions(
    keyword: Optional[str] = None,
    *,
    field: Optional[str] = None,
    author: Optional[str] = None,
    first_author: Optional[str] = None,
    affiliation: Optional[str] = None,
    journal: Optional[str] = None,
    fund: Optional[str] = None,
    fuzzy: bool = False,
) -> List[Tuple[str, str, bool]]:
    """生成用于多行高级检索表单兜底的 (field_code, cleaned_value, is_fuzzy) 列表。"""
    conds: List[Tuple[str, str, bool]] = []
    if keyword and keyword.strip():
        code = resolve_cnki_field_code(field)
        val, _ = _clean_cnki_value(keyword)
        if val:
            is_f = True if code in ("AF", "FU") else (False if code in ("AU", "FI", "RP", "DOI") else bool(fuzzy))
            conds.append((code, val, is_f))
    if author and author.strip():
        val, _ = _clean_cnki_value(author)
        if val:
            conds.append(("AU", val, False))
    if first_author and first_author.strip():
        val, _ = _clean_cnki_value(first_author)
        if val:
            conds.append(("FI", val, False))
    if affiliation and affiliation.strip():
        val, _ = _clean_cnki_value(affiliation)
        if val:
            conds.append(("AF", val, True))
    if journal and journal.strip():
        val, had_book = _clean_cnki_value(journal, strip_book_marks=True)
        if val:
            conds.append(("LY", val, not had_book or bool(fuzzy)))
    if fund and fund.strip():
        val, _ = _clean_cnki_value(fund)
        if val:
            conds.append(("FU", val, True))
    return conds


# ── 日期与客户端年份过滤 ────────────────────────────────────────────────

def _year_from_date(date_str: str) -> Optional[int]:
    try:
        return int(str(date_str)[:4])
    except (ValueError, IndexError, TypeError):
        return None


def _filter_by_year(results: list, year_from: Optional[int], year_to: Optional[int]) -> list:
    """客户端精确年份过滤兜底。"""
    if not results or (year_from is None and year_to is None):
        return results
    filtered = []
    for r in results:
        y = r.get("year") or (_year_from_date(r["date"]) if r.get("date") else None)
        if y is None:
            filtered.append(r)
            continue
        if year_from is not None and int(y) < year_from:
            continue
        if year_to is not None and int(y) > year_to:
            continue
        filtered.append(r)
    return filtered


# ── AJAX 表格状态快照与刷新同步 ─────────────────────────────────────────

def _capture_results_signature(driver) -> Tuple[Any, str]:
    """捕获当前结果表元素与状态指纹（页码标记 + 首行链接/标题），用于检测 AJAX 实际完成刷新。"""
    table_el = None
    try:
        tables = driver.find_elements(By.CSS_SELECTOR, "table.result-table-list, .result-table-list, #gridTable")
        if tables:
            table_el = tables[0]
    except Exception:
        pass

    sig = ""
    try:
        sig = driver.execute_script(r"""
            var mark = document.querySelector('.countPageMark, .pagerTitleCell');
            var firstLink = document.querySelector('table.result-table-list tbody tr td.name a, .result-table-list tbody tr .fz14');
            var total = document.querySelector('.pagerTitleCell em, span.pagerTitleCell');
            var mTxt = mark ? mark.textContent.trim() : '';
            var fTxt = firstLink ? ((firstLink.getAttribute('href') || '') + '|' + firstLink.textContent.trim()) : '';
            var tTxt = total ? total.textContent.trim() : '';
            return mTxt + '::' + tTxt + '::' + fTxt;
        """) or ""
    except Exception:
        pass
    return table_el, sig


def _wait_for_results_refresh(driver, prev_snapshot: Optional[Tuple[Any, str]] = None, timeout: float = 15.0) -> bool:
    """等待知网结果表完成首次加载或 AJAX 局部重绘。

    相比单纯 `presence_of_element_located`（在旧表尚未卸载时会 0.01s 假通过），
    本函数在提供 `prev_snapshot` 时会等待旧表变为 stale、或表格指纹变化、或出现无结果提示。
    """
    deadline = time.time() + timeout
    old_table, old_sig = prev_snapshot if prev_snapshot else (None, "")

    if old_table is not None or old_sig:
        changed = False
        while time.time() < deadline:
            try:
                if old_table is not None:
                    try:
                        _ = old_table.is_enabled()
                    except StaleElementReferenceException:
                        changed = True
                        break
                state = driver.execute_script(r"""
                    var loading = document.querySelector('.loading, .search-loading, .layui-layer-loading');
                    if (loading && loading.offsetParent !== null) return 'loading';
                    var empty = document.querySelector('.no-result, .empty-box, p.no-content');
                    if (empty && empty.offsetParent !== null) return 'empty';
                    var bodyTxt = (document.body && document.body.innerText) || '';
                    if (bodyTxt.indexOf('抱歉，暂无数据') >= 0 || bodyTxt.indexOf('未检索到相关记录') >= 0) return 'empty';
                    var mark = document.querySelector('.countPageMark, .pagerTitleCell');
                    var firstLink = document.querySelector('table.result-table-list tbody tr td.name a, .result-table-list tbody tr .fz14');
                    var total = document.querySelector('.pagerTitleCell em, span.pagerTitleCell');
                    var sig = (mark ? mark.textContent.trim() : '') + '::' +
                              (total ? total.textContent.trim() : '') + '::' +
                              (firstLink ? ((firstLink.getAttribute('href') || '') + '|' + firstLink.textContent.trim()) : '');
                    return 'sig:' + sig;
                """) or ""
                if state == "empty":
                    return True
                if state.startswith("sig:") and state[4:] and state[4:] != old_sig:
                    changed = True
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if not changed:
            _log("[cnki] 等待表格 AJAX 变化超时，继续解析当前可见结果")

    # 确保最终表格或无结果节点稳定就绪
    remaining = max(1.0, deadline - time.time())
    end_wait = time.time() + remaining
    while time.time() < end_wait:
        try:
            ready = driver.execute_script(r"""
                var loading = document.querySelector('.loading, .search-loading, .layui-layer-loading');
                if (loading && loading.offsetParent !== null) return false;
                if (document.querySelector('table.result-table-list tbody tr, .result-table-list tbody tr, #gridTable tbody tr')) return true;
                var empty = document.querySelector('.no-result, .empty-box, p.no-content');
                if (empty && empty.offsetParent !== null) return true;
                var bodyTxt = (document.body && document.body.innerText) || '';
                return (bodyTxt.indexOf('抱歉，暂无数据') >= 0 || bodyTxt.indexOf('未检索到相关记录') >= 0);
            """)
            if ready:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


# ── AdvSearch 前置筛选（文献类型 / 来源类别 / 发表时间范围） ─────────────

_DOCTYPE_TAB_MAP = {
    "journal": ("学术期刊", "期刊"),
    "master": ("硕士",),
    "doctor": ("博士",),
    "thesis": ("学位论文", "博硕士", "学位"),
    "conference": ("会议",),
    "newspaper": ("报纸",),
}


def _preselect_adv_doctype(driver, doc_type: Optional[str], core: Optional[str]) -> bool:
    """在高级检索页提交前预选顶部文献库（返回是否已精确锁定目标类型，无需搜后再切）。"""
    effective_type = doc_type
    if not effective_type and core:
        effective_type = "journal"
    if not effective_type:
        return True

    # AdvSearch 顶栏只有「学位论文」大类，硕士/博士若无页内细分复选框则需搜后补切一次
    top_targets = (
        ("学位论文", "博硕士", "学位")
        if effective_type in ("master", "doctor", "thesis")
        else _DOCTYPE_TAB_MAP.get(effective_type, ())
    )
    if not top_targets:
        return False

    js = r"""
    var targets = arguments[0];
    var subType = arguments[1];
    var navItems = document.querySelectorAll(
        '.doctype-menus li, .doctype-menus a, .header-nav li, .classify-menu li, ul[class*="doctype"] li'
    );
    var clickedTop = false;
    for (var i = 0; i < navItems.length; i++) {
        var el = navItems[i];
        var txt = (el.textContent || '').trim();
        for (var k = 0; k < targets.length; k++) {
            if (txt === targets[k] || txt.indexOf(targets[k]) >= 0) {
                var clickTarget = el.querySelector('a') || el;
                clickTarget.click();
                clickedTop = true;
                break;
            }
        }
        if (clickedTop) break;
    }
    return JSON.stringify({clickedTop: clickedTop, subType: subType});
    """
    try:
        raw = driver.execute_script(js, list(top_targets), effective_type)
        info = json.loads(raw) if raw else {}
        if info.get("clickedTop"):
            time.sleep(0.6)
            if effective_type in ("master", "doctor"):
                # 尝试在学位论文高级检索页内仅勾选博士或硕士子库
                want_label = "硕士" if effective_type == "master" else "博士"
                other_label = "博士" if effective_type == "master" else "硕士"
                sub_ok = driver.execute_script(r"""
                    var want = arguments[0], other = arguments[1];
                    var labels = document.querySelectorAll('label');
                    var hit = false;
                    for (var i = 0; i < labels.length; i++) {
                        var t = (labels[i].textContent || '').trim();
                        var cb = labels[i].querySelector('input[type="checkbox"]') ||
                                 (labels[i].htmlFor ? document.getElementById(labels[i].htmlFor) : null);
                        if (!cb || cb.offsetParent === null) continue;
                        if (t.indexOf(want) >= 0) {
                            if (!cb.checked) cb.click();
                            hit = true;
                        } else if (t.indexOf(other) >= 0) {
                            if (cb.checked) cb.click();
                        }
                    }
                    return hit;
                """, want_label, other_label)
                return bool(sub_ok)
            return bool(doc_type is None or effective_type in ("journal", "thesis", "conference", "newspaper"))
    except Exception as e:
        _log(f"[cnki-adv] 预选文献类型失败: {e}")
    return False


_ADV_CORE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "北大核心": ("HX", "北大核心", "中文核心期刊"),
    "CSSCI": ("CSI", "CSSCI", "中文社会科学引文索引"),
    "CSCD": ("CSD", "CSCD", "中国科学引文数据库"),
    "EI": ("EI", "EI来源期刊"),
    "SCI": ("SI", "SCI", "SCI来源期刊"),
    "WJCI": ("P12", "WJCI"),
    "AMI": ("P13", "AMI"),
}


def _preselect_adv_cores(driver, core: Optional[str]) -> bool:
    """在高级/专业检索页提交前直接勾选 `.extend-tit-labels` 来源类别复选框。

    注意：必须在切换完 `majorSearch`/`gradeSearch` Tab **之后**调用，因为切换检索模式 Tab
    会将来源类别重置回默认的 `全部期刊 (name="all")`。点击具体 checkbox input（而非外层 label）
    会自动取消 `全部期刊` 勾选。
    """
    if not core:
        return False
    core_names = _parse_core_input(core)
    if not core_names:
        return False

    alias_map = {name: list(_ADV_CORE_ALIASES.get(name, (name,))) for name in core_names}
    js = r"""
    var requested = arguments[0];
    var aliasMap = arguments[1];
    var matched = [];
    var cbs = document.querySelectorAll('.extend-tit-labels input[type="checkbox"], input[type="checkbox"]');
    for (var r = 0; r < requested.length; r++) {
        var targetName = requested[r];
        var aliases = aliasMap[targetName] || [targetName];
        var foundThis = false;
        for (var i = 0; i < cbs.length; i++) {
            var cb = cbs[i];
            if (cb.name === 'all') continue;
            var parentLabel = cb.closest('label') || (cb.id ? document.querySelector('label[for="' + cb.id + '"]') : null);
            var labelTxt = parentLabel ? (parentLabel.textContent || '').trim() : '';
            var keyAttr = (cb.getAttribute('key') || '').trim();
            var valAttr = (cb.getAttribute('value') || '').trim();
            if (cb.offsetParent === null && (!parentLabel || parentLabel.offsetParent === null)) continue;

            for (var a = 0; a < aliases.length; a++) {
                var al = aliases[a];
                if (keyAttr.toUpperCase() === al.toUpperCase() ||
                    valAttr.toUpperCase() === al.toUpperCase() ||
                    labelTxt === al || labelTxt.indexOf(al) >= 0) {
                    if (!cb.checked) {
                        cb.click();
                    }
                    foundThis = true;
                    break;
                }
            }
            if (foundThis) break;
        }
        if (foundThis) matched.push(targetName);
    }
    var checkedKeys = [];
    document.querySelectorAll('.extend-tit-labels input:checked').forEach(function(el) {
        checkedKeys.push((el.name || el.getAttribute('key') || '') + '=' + (el.value || ''));
    });
    return JSON.stringify({matched: matched, totalRequested: requested.length, checkedKeys: checkedKeys});
    """
    try:
        raw = driver.execute_script(js, core_names, alias_map)
        info = json.loads(raw) if raw else {}
        matched = info.get("matched", [])
        if len(matched) == len(core_names):
            _log(f"[cnki-adv] 已在高级检索表单前置勾选来源类别: {matched} ({info.get('checkedKeys')})")
            return True
        if matched:
            _log(f"[cnki-adv] 部分来源类别已前置勾选 {matched}，其余将在结果页侧边栏补筛")
    except Exception as e:
        _log(f"[cnki-adv] 前置勾选来源类别失败: {e}")
    return False


def _preset_adv_date_range(driver, year_from: Optional[int], year_to: Optional[int]) -> bool:
    """在高级/专业检索页提交前直接设置年份/日期范围。

    兼容两种 KNS8S 真实 DOM 形态：
    1. 学术期刊/子库形态 (`.tit-startend-yearbox`): 4 位年份框 `input[placeholder="起始年"]` / `input[placeholder="结束年"]`
    2. 总库形态 (`.tit-date-box`): 日历框 `#datebox0` (`YYYY-01-01`) / `#datebox1` (`YYYY-12-31`)
    注意：同样须在切换完 `majorSearch`/`gradeSearch` Tab 之后调用。
    """
    if year_from is None and year_to is None:
        return False

    current_year = time.localtime().tm_year
    y_start = str(year_from or 1915)
    y_end = str(year_to or current_year)
    start_date = f"{y_start}-01-01"
    end_date = f"{y_end}-12-31"

    js = r"""
    var yStart = arguments[0], yEnd = arguments[1], startDate = arguments[2], endDate = arguments[3];

    // 形态 1: 学术期刊等子库的 4 位「出版年度」框 (.tit-startend-yearbox)
    var yBoxStart = document.querySelector('.tit-startend-yearbox .sort.start input[type="text"], input[placeholder="起始年"]');
    var yBoxEnd = document.querySelector('.tit-startend-yearbox .sort.end input[type="text"], input[placeholder="结束年"]');
    if (yBoxStart && yBoxEnd && yBoxStart.offsetParent !== null) {
        yBoxStart.removeAttribute('readonly');
        yBoxEnd.removeAttribute('readonly');
        yBoxStart.value = yStart;
        yBoxEnd.value = yEnd;
        ['input', 'keyup', 'change', 'blur'].forEach(function(ev) {
            yBoxStart.dispatchEvent(new Event(ev, {bubbles: true}));
            yBoxEnd.dispatchEvent(new Event(ev, {bubbles: true}));
        });
        return JSON.stringify({ok: true, mode: 'yearbox', start: yBoxStart.value, end: yBoxEnd.value});
    }

    // 形态 2: 总库的「发表时间」日历框 (#datebox0 / #datebox1)
    var sInput = document.querySelector('#datebox0, .tit-date-box input.publishdate:first-of-type');
    var eInput = document.querySelector('#datebox1, .tit-date-box input.publishdate:last-of-type');
    if (sInput && eInput && sInput !== eInput && sInput.offsetParent !== null) {
        sInput.removeAttribute('readonly');
        eInput.removeAttribute('readonly');
        sInput.value = startDate;
        eInput.value = endDate;
        ['input', 'change', 'blur'].forEach(function(ev) {
            sInput.dispatchEvent(new Event(ev, {bubbles: true}));
            eInput.dispatchEvent(new Event(ev, {bubbles: true}));
        });
        return JSON.stringify({ok: true, mode: 'datebox', start: sInput.value, end: eInput.value});
    }

    return JSON.stringify({ok: false});
    """
    try:
        raw = driver.execute_script(js, y_start, y_end, start_date, end_date)
        info = json.loads(raw) if raw else {}
        if info.get("ok"):
            _log(f"[cnki-adv] 已前置设置时间范围: {info.get('start')} ~ {info.get('end')} ({info.get('mode')})")
            return True
    except Exception as e:
        _log(f"[cnki-adv] 前置设置时间范围失败: {e}")
    return False


# ── 双引擎高级检索提交（引擎 1: 专业检索 majorSearch；引擎 2: 新版 #gradetxt 表单） ─

def _click_adv_submit_button(driver) -> bool:
    btn_selectors = [
        (By.CSS_SELECTOR, "input.btn-search, button.btn-search"),
        (By.CSS_SELECTOR, ".search-buttons .btn-search, .search-btn"),
        (By.XPATH, "//input[@type='button' and contains(@value,'检索')]"),
        (By.XPATH, "//button[contains(text(),'检索')]"),
    ]
    for by, selector in btn_selectors:
        try:
            btns = driver.find_elements(by, selector)
            for btn in btns:
                if btn.is_displayed() and btn.is_enabled():
                    try:
                        btn.click()
                    except ElementClickInterceptedException:
                        driver.execute_script("arguments[0].click();", btn)
                    return True
        except Exception:
            continue
    return False


def _detect_cnki_expr_error(driver) -> Optional[str]:
    """检测提交专业检索式后页面是否弹出语法错误提示。"""
    try:
        msg = driver.execute_script(r"""
            var selectors = [
                '.layui-layer-content', '.error-tips', '.tip-info',
                '.major-tips-error', '.search-tips-error'
            ];
            var markers = ['语法', '表达式', '检索式', '不合法', '输入有误', '格式错误', '括号', '引号'];
            for (var s = 0; s < selectors.length; s++) {
                var nodes = document.querySelectorAll(selectors[s]);
                for (var i = 0; i < nodes.length; i++) {
                    if (nodes[i].offsetParent === null) continue;
                    var t = (nodes[i].textContent || '').trim();
                    for (var m = 0; m < markers.length; m++) {
                        if (t.indexOf(markers[m]) >= 0) return t.slice(0, 160);
                    }
                }
            }
            return '';
        """) or ""
        return msg or None
    except Exception:
        return None


def _submit_major_search(
    driver,
    expression: str,
    *,
    core: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> Dict[str, Any]:
    """引擎 1：切换至 KNS8S「专业检索 (majorSearch)」，再勾选来源类别/出版年度并提交检索式。"""
    if not expression:
        return {"ok": False, "reason": "empty_expression"}

    fill_only_js = r"""
    var expr = arguments[0];
    var ta = document.querySelector('textarea.textarea-major, .major-search textarea, #gradetxt textarea');
    if (!ta || ta.offsetParent === null) {
        var allTas = document.querySelectorAll('textarea');
        for (var j = 0; j < allTas.length; j++) {
            if (allTas[j].offsetParent !== null) {
                ta = allTas[j];
                break;
            }
        }
    }
    if (!ta) {
        return JSON.stringify({status: 'no_textarea'});
    }

    ta.focus();
    ta.value = '';
    ta.value = expr;
    ta.dispatchEvent(new Event('input', {bubbles: true}));
    ta.dispatchEvent(new Event('change', {bubbles: true}));
    ta.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
    return JSON.stringify({status: 'filled', valueLen: ta.value.length});
    """
    try:
        # 1. 先切换到「专业检索」Tab（注意：切换 Tab 会重置下方的来源类别与年度控件，因此筛选必须在切换后设置！）
        switched = driver.execute_script(r"""
            var t = document.querySelector('li[name="majorSearch"], [data-id="majorSearch"]');
            if (t && t.offsetParent !== null) {
                if (!t.classList.contains('active') && !t.classList.contains('cur')) t.click();
                return true;
            }
            var nodes = document.querySelectorAll('.search-classify-menu li, .search-tab li, li, span');
            for (var i = 0; i < nodes.length; i++) {
                if ((nodes[i].textContent || '').trim() === '专业检索' && nodes[i].offsetParent !== null) {
                    nodes[i].click();
                    return true;
                }
            }
            return false;
        """)
        time.sleep(0.4)

        # 2. 在专业检索面板内设置来源类别复选框与出版年度/发表时间
        core_ok = _preselect_adv_cores(driver, core) if core else False
        year_ok = _preset_adv_date_range(driver, year_from, year_to) if (year_from is not None or year_to is not None) else False

        # 3. 填入专业检索表达式
        raw = driver.execute_script(fill_only_js, expression)
        info = json.loads(raw) if raw else {}
        _log(f"[cnki-major] 专业检索式填充: expr={expression} -> {info}")
        if info.get("status") != "filled":
            return {"ok": False, "reason": info.get("status", "fill_failed"), "switched": switched}

        time.sleep(0.3)
        if not _click_adv_submit_button(driver):
            return {"ok": False, "reason": "no_submit_button"}

        time.sleep(0.8)
        syntax_err = _detect_cnki_expr_error(driver)
        if syntax_err:
            _log(f"[cnki-major] 知网返回检索式语法提示: {syntax_err}")
            return {"ok": False, "syntax_error": True, "reason": syntax_err}

        return {
            "ok": True,
            "engine": "majorSearch",
            "expression": expression,
            "core_preselected": core_ok,
            "year_preset": year_ok,
        }
    except Exception as e:
        _log(f"[cnki-major] 专业检索执行异常: {e}")
        return {"ok": False, "reason": str(e)}


def _submit_grade_search(
    driver,
    conditions: List[Tuple[str, str, bool]],
    *,
    core: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> Dict[str, Any]:
    """引擎 2（兜底）：新版 KNS8S `#gradetxt dd` 多条件表单提交。"""
    if not conditions:
        return {"ok": False, "reason": "no_conditions"}

    try:
        # 1. 确保切回「高级检索 (gradeSearch)」Tab
        driver.execute_script(r"""
            var t = document.querySelector('li[name="gradeSearch"], [data-id="gradeSearch"]');
            if (t && t.offsetParent !== null) {
                if (!t.classList.contains('active') && !t.classList.contains('cur')) t.click();
                return;
            }
            var nodes = document.querySelectorAll('.search-classify-menu li, .search-tab li, li, span');
            for (var i = 0; i < nodes.length; i++) {
                if ((nodes[i].textContent || '').trim() === '高级检索' && nodes[i].offsetParent !== null) {
                    nodes[i].click();
                    break;
                }
            }
        """)
        time.sleep(0.4)

        # 2. 切回 gradeSearch 后重新设置来源类别与出版年度
        core_ok = _preselect_adv_cores(driver, core) if core else False
        year_ok = _preset_adv_date_range(driver, year_from, year_to) if (year_from is not None or year_to is not None) else False

        # 3. 逐次点击 "+" 添加足够的条件行
        for idx in range(len(conditions)):
            row_count = driver.execute_script(r"""
                var rows = document.querySelectorAll('#gradetxt dd, .grade-search-content dd, .sort-item');
                var visible = 0;
                for (var i = 0; i < rows.length; i++) {
                    if (rows[i].querySelector('input[type="text"]')) visible++;
                }
                return visible;
            """) or 0
            if row_count > idx:
                continue
            added = driver.execute_script(r"""
                var addBtns = document.querySelectorAll(
                    '#gradetxt a.add-group, #gradetxt .add-group, #gradetxt [class*="add"], .btn-add, a[title*="添加"]'
                );
                for (var i = 0; i < addBtns.length; i++) {
                    if (addBtns[i].offsetParent !== null) {
                        addBtns[i].click();
                        return true;
                    }
                }
                return false;
            """)
            if added:
                time.sleep(0.25)

        payload = [
            {
                "code": code,
                "labels": list(_CODE_TO_LABELS.get(code, (code,))),
                "value": val,
                "fuzzy": bool(is_fuzzy),
            }
            for code, val, is_fuzzy in conditions
        ]

        fill_js = r"""
        var items = arguments[0];
        var rawRows = document.querySelectorAll('#gradetxt dd, .grade-search-content dd, .sort-item');
        var rows = [];
        for (var r = 0; r < rawRows.length; r++) {
            if (rawRows[r].querySelector('input[type="text"]')) rows.push(rawRows[r]);
        }
        if (rows.length < items.length) {
            return JSON.stringify({status: 'insufficient_rows', have: rows.length, need: items.length});
        }

        var report = [];
        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            var row = rows[i];
            var fieldSwitched = false;

            // 1A. 新版 KNS8S 第 2 行起首个 .reopt 是 AND/OR 逻辑符，必须定位含 li[data-val] 字段代码的那个 .reopt！
            var reopts = row.querySelectorAll('.sort.reopt, .reopt');
            var fieldReopt = null;
            for (var rp = 0; rp < reopts.length; rp++) {
                if (reopts[rp].querySelector('li[data-val="SU"], li[data-val="TI"], li[data-val="AU"]')) {
                    fieldReopt = reopts[rp];
                    break;
                }
            }
            if (!fieldReopt && reopts.length > 0) {
                fieldReopt = reopts.length >= 2 ? reopts[1] : reopts[0];
            }
            if (fieldReopt) {
                var trigger = fieldReopt.querySelector('.sort-default');
                if (trigger) { try { trigger.click(); } catch (e) {} }
                var lis = fieldReopt.querySelectorAll('.sort-list li, ul li');
                for (var j = 0; j < lis.length; j++) {
                    var dv = (lis[j].getAttribute('data-val') || '').trim().toUpperCase();
                    var lt = (lis[j].textContent || '').trim();
                    var labelHit = false;
                    for (var lb = 0; lb < item.labels.length; lb++) {
                        if (lt === item.labels[lb]) { labelHit = true; break; }
                    }
                    if (dv === item.code || labelHit) {
                        lis[j].click();
                        fieldSwitched = true;
                        break;
                    }
                }
            }

            // 1B. 旧版镜像原生 <select> 兜底
            if (!fieldSwitched) {
                var sel = row.querySelector('select');
                if (sel) {
                    var opts = sel.querySelectorAll('option');
                    for (var o = 0; o < opts.length; o++) {
                        var ov = (opts[o].value || '').trim().toUpperCase();
                        var ot = (opts[o].textContent || '').trim();
                        if (ov === item.code || item.labels.indexOf(ot) >= 0) {
                            sel.value = opts[o].value;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            fieldSwitched = true;
                            break;
                        }
                    }
                }
            }

            // 2. 精确 / 模糊下拉（定位含「精确」「模糊」的下拉）
            var wantMode = item.fuzzy ? '模糊' : '精确';
            var allSortLists = row.querySelectorAll('.sort-special, .reopt');
            for (var sp = 0; sp < allSortLists.length; sp++) {
                var modeLis = allSortLists[sp].querySelectorAll('li');
                for (var m = 0; m < modeLis.length; m++) {
                    if (!modeLis[m].getAttribute('data-val') && (modeLis[m].textContent || '').trim() === wantMode) {
                        try { modeLis[m].click(); } catch (e) {}
                        break;
                    }
                }
            }

            // 3. 填入检索词
            var inp = row.querySelector('input[data-tipid], input[type="text"][maxlength], input[type="text"]');
            if (!inp || !fieldSwitched) {
                report.push({code: item.code, fieldSwitched: fieldSwitched, hasInput: !!inp, status: 'failed'});
                return JSON.stringify({status: 'field_or_input_failed', report: report});
            }
            inp.focus();
            inp.value = '';
            inp.value = item.value;
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            inp.dispatchEvent(new Event('change', {bubbles: true}));
            report.push({code: item.code, status: 'filled'});
        }
        return JSON.stringify({status: 'ready', filled: report.length, report: report});
        """
        raw = driver.execute_script(fill_js, payload)
        info = json.loads(raw) if raw else {}
        _log(f"[cnki-grade] 多行高级检索表单填充结果: {info}")
        if info.get("status") != "ready":
            return {"ok": False, "reason": info.get("status", "grade_fill_failed"), "detail": info}

        time.sleep(0.3)
        if _click_adv_submit_button(driver):
            return {
                "ok": True,
                "engine": "gradeSearch",
                "core_preselected": core_ok,
                "year_preset": year_ok,
            }

        inputs = driver.find_elements(By.CSS_SELECTOR, "#gradetxt input[type='text'], input[type='text']")
        if inputs:
            inputs[-1].send_keys(Keys.RETURN)
            return {
                "ok": True,
                "engine": "gradeSearch",
                "core_preselected": core_ok,
                "year_preset": year_ok,
            }

        return {"ok": False, "reason": "no_submit_button"}
    except Exception as e:
        _log(f"[cnki-grade] 多行高级检索异常: {e}")
        return {"ok": False, "reason": str(e)}


def _advanced_search(
    driver,
    conditions: List[Tuple[str, str, bool]],
    *,
    expression: str = "",
    require_expr_only: bool = False,
    core: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> Dict[str, Any]:
    """统一执行知网高级检索：优先专业检索式 `majorSearch`，失败时自动回退 `#gradetxt` 多条件表单。"""
    if expression:
        major_res = _submit_major_search(
            driver,
            expression,
            core=core,
            year_from=year_from,
            year_to=year_to,
        )
        if major_res.get("ok"):
            return major_res
        if major_res.get("syntax_error") and require_expr_only:
            return major_res
        _log(f"[cnki-adv] 专业检索未成功 ({major_res.get('reason')})，尝试 #gradetxt 多行表单兜底...")

    if require_expr_only or not conditions:
        return {
            "ok": False,
            "syntax_error": False,
            "reason": "自定义 --expr 提交失败且无结构化字段可回退表单",
        }

    norm_conds: List[Tuple[str, str, bool]] = []
    for item in conditions:
        if len(item) == 2:
            f_name, f_val = item[0], item[1]
            norm_conds.append((resolve_cnki_field_code(f_name), f_val, False))
        else:
            norm_conds.append(item)
    return _submit_grade_search(
        driver,
        norm_conds,
        core=core,
        year_from=year_from,
        year_to=year_to,
    )


# ── 主搜索入口 (`search_cnki` / `batch_search_cnki`) ────────────────────

def refresh_cnki_url(
    driver,
    title: str,
    author: str = "",
    journal: str = "",
) -> Optional[str]:
    """当历史会话中的知网加密详情链接 (`kcms2?v=...`) 过期返回 404 时，通过精确题名重定位最新 URL。"""
    clean_title, _ = _clean_cnki_value(title)
    if not clean_title:
        return None
    try:
        expr = build_cnki_expression(
            keyword=clean_title,
            field="篇名",
            author=author or None,
            journal=f"《{journal}》" if journal and not journal.startswith("《") else (journal or None),
        )
        _safe_get(driver, CNKI_ADV_SEARCH_URL)
        time.sleep(max(1.2, REQUEST_INTERVAL - 1))
        driver = _handle_captcha(driver)
        res = _submit_major_search(driver, expr)
        if not res.get("ok") and journal:
            # 放宽期刊限定仅按精确题名重试
            expr = build_cnki_expression(keyword=clean_title, field="篇名")
            res = _submit_major_search(driver, expr)
        if not res.get("ok"):
            return None
        _wait_for_results_refresh(driver, prev_snapshot=None, timeout=12.0)
        page_rows = _parse_search_results(driver, cite_enrich=0)
        norm_want = re.sub(r"\W+", "", clean_title).lower()
        for row in page_rows:
            u = row.get("url", "")
            if not u:
                continue
            norm_got = re.sub(r"\W+", "", row.get("title", "")).lower()
            if norm_got == norm_want or norm_want in norm_got or norm_got in norm_want:
                _log(f"[cnki] 已重定位最新详情链接: {clean_title[:30]} -> {u[:80]}")
                return u
        if page_rows and page_rows[0].get("url"):
            return page_rows[0]["url"]
    except Exception as e:
        _log(f"[cnki] 重定位过期详情链接失败: {e}")
    return None


def search_cnki(
    keyword: str = "",
    core: str = None,
    year_from: int = None,
    year_to: int = None,
    author: str = None,
    journal: str = None,
    doc_type: str = None,
    field: str = None,
    sort: str = "relevance",
    pages: int = 1,
    cite_enrich: int = 0,
    affiliation: str = None,
    first_author: str = None,
    fund: str = None,
    expr: str = None,
    fuzzy: bool = False,
    per_page: Optional[int] = None,
    bulk_abstract: bool = False,
    _keep_driver: bool = False,
):
    """知网文献搜索（支持专业检索式、多字段精确/模糊限定、前置核心与年份过滤、50条/页扩容与批量题录摘要导出）。"""
    _log(
        f"[cnki] 开始搜索: keyword={keyword}, expr={expr}, field={field}, author={author}, "
        f"first_author={first_author}, affiliation={affiliation}, journal={journal}, "
        f"fund={fund}, core={core}, year={year_from}-{year_to}, doc_type={doc_type}, "
        f"fuzzy={fuzzy}, per_page={per_page}, bulk_abstract={bulk_abstract}"
    )

    try:
        expression = build_cnki_expression(
            keyword=keyword,
            field=field,
            author=author,
            first_author=first_author,
            affiliation=affiliation,
            journal=journal,
            fund=fund,
            expr=expr,
            fuzzy=fuzzy,
        )
        structured_conds = _build_structured_conditions(
            keyword=keyword,
            field=field,
            author=author,
            first_author=first_author,
            affiliation=affiliation,
            journal=journal,
            fund=fund,
            fuzzy=fuzzy,
        )
    except ValueError as ve:
        return [{"status": "error", "code": "CNKI_EXPR_SYNTAX_ERROR", "message": str(ve)}]

    if not expression:
        return [{"status": "error", "code": "NO_KEYWORDS", "message": "未提供搜索关键词、高级筛选字段或 --expr 检索式"}]

    accessible, msg = check_cnki_access()
    _log(f"[cnki] 网络检测: accessible={accessible}, msg={msg}")
    if not accessible:
        if msg.startswith("SANDBOX_BLOCKED"):
            _log("[cnki] 沙盒限制导致预检失败，浏览器可能仍可访问，继续尝试...")
        else:
            return [{"status": "error", "code": "CNKI_UNREACHABLE", "message": msg}]

    # 严格的语义限定条件（若失败绝不允许静默退回普通关键词搜索）
    has_strict_adv = bool(
        (expr and expr.strip())
        or author
        or first_author
        or affiliation
        or journal
        or fund
        or fuzzy
        or (field and resolve_cnki_field_code(field) != "SU")
    )
    # 若带核心期刊或起止年份，也优先走 AdvSearch 一次性前置筛选，减少结果页连续刷侧边栏
    prefer_adv_page = has_strict_adv or bool(core or year_from is not None or year_to is not None)

    driver = None
    should_quit = True
    try:
        browser = _detect_browser()
        _log(f"[cnki] 浏览器: {browser}，正在准备会话...")
        driver = _create_driver(browser=browser)

        _safe_get(driver, "https://kns.cnki.net/")
        time.sleep(0.6)
        _load_cookies(driver)

        doctype_done = False
        core_filter_ok = False
        year_preset_ok = False
        used_adv = False

        if prefer_adv_page:
            _safe_get(driver, CNKI_ADV_SEARCH_URL)
            time.sleep(max(1.2, REQUEST_INTERVAL - 1))
            driver = _handle_captcha(driver)

            doctype_done = _preselect_adv_doctype(driver, doc_type, core)

            adv_res = _advanced_search(
                driver,
                structured_conds,
                expression=expression,
                require_expr_only=bool(expr and expr.strip() and not structured_conds),
                core=core,
                year_from=year_from,
                year_to=year_to,
            )
            if adv_res.get("ok"):
                used_adv = True
                core_filter_ok = bool(adv_res.get("core_preselected"))
                year_preset_ok = bool(adv_res.get("year_preset"))
                _log(f"[cnki] 高级检索提交成功 (engine={adv_res.get('engine')}, expr={expression})")
            elif has_strict_adv:
                err_code = "CNKI_EXPR_SYNTAX_ERROR" if adv_res.get("syntax_error") else "CNKI_ADV_SEARCH_FAILED"
                diag_file = None
                if err_code == "CNKI_ADV_SEARCH_FAILED":
                    diag_file = save_adv_dom_diagnosis(driver, reason=str(adv_res.get("reason", "")))
                err_payload: Dict[str, Any] = {
                    "status": "error",
                    "code": err_code,
                    "message": f"知网高级检索提交失败（已拒绝静默丢弃筛选条件）: {adv_res.get('reason')}",
                    "expression": expression,
                }
                if diag_file:
                    err_payload["dom_diagnosis_file"] = diag_file
                return [err_payload]
            else:
                _log("[cnki] AdvSearch 普通主题提交未成功，安全回退普通搜索页 + 侧边栏筛选")

        if not used_adv:
            _safe_get(driver, CNKI_SEARCH_URL)
            time.sleep(REQUEST_INTERVAL)
            driver = _handle_captcha(driver)
            _input_search_keyword(driver, keyword)
            doctype_done = False
            core_filter_ok = False
            year_preset_ok = False

        time.sleep(max(1, REQUEST_INTERVAL - 1))
        driver = _handle_captcha(driver)
        _wait_for_results_refresh(driver, prev_snapshot=None, timeout=15.0)

        if doc_type and not doctype_done:
            if _switch_to_doc_type(driver, doc_type):
                driver = _handle_captcha(driver)
            else:
                _log(f"[cnki] 文献类型筛选 '{doc_type}' 未生效")

        if core and not core_filter_ok:
            core_filter_ok = _set_core_filter(driver, core)
            if not core_filter_ok and not doc_type:
                _log("[cnki] 侧边栏筛选失败，尝试切换到学术期刊数据库...")
                if _switch_to_journal_db(driver):
                    driver = _handle_captcha(driver)
                    core_filter_ok = _set_core_filter(driver, core)
            _log(f"[cnki] 核心期刊筛选: {'成功' if core_filter_ok else '未生效（结果可能包含非核心文献）'}")

        if (year_from is not None or year_to is not None) and not year_preset_ok:
            _set_year_filter(driver, year_from, year_to)

        if sort and sort not in ("relevance", "quality"):
            _set_sort(driver, sort)

        if per_page and per_page in (10, 20, 50) and per_page != 20:
            _set_per_page(driver, per_page)

        _log(f"[cnki] 筛选后页面: url={driver.current_url[:90]} title={driver.title}")

        cite_remaining = max(cite_enrich or 0, 0)
        all_results: List[Dict[str, Any]] = []
        for page_num in range(pages):
            _log(f"[cnki] 解析第 {page_num + 1}/{pages} 页...")
            if page_num > 0:
                if not _go_next_page(driver):
                    _log(f"[cnki] 已无下一页，停止在第 {page_num} 页")
                    break
                driver = _handle_captcha(driver)

            page_results = _parse_search_results(driver, cite_enrich=cite_remaining)
            if bulk_abstract and page_results:
                exported = _bulk_export_from_results_page(driver, max_items=len(page_results))
                if exported:
                    merged_cnt = _merge_bulk_export_into_results(page_results, exported)
                    _log(f"[cnki] 批量导出已补全 {merged_cnt}/{len(page_results)} 条摘要与卷期页码")
            cite_remaining = max(0, cite_remaining - len(page_results))
            _log(f"[cnki] 第 {page_num + 1} 页获取 {len(page_results)} 条结果")
            all_results.extend(page_results)
            if not page_results:
                break

        effective_dtype = doc_type or ("journal" if core else None)
        if effective_dtype and all_results:
            for r in all_results:
                if r.get("title") and not r.get("doc_type"):
                    r["doc_type"] = "thesis" if effective_dtype == "thesis" else effective_dtype

        if core and core_filter_ok and all_results:
            core_labels = _parse_core_input(core)
            for r in all_results:
                if r.get("title"):
                    r["is_core"] = True
                    r["core_type"] = ",".join(core_labels)

        all_results = _filter_by_year(all_results, year_from, year_to)

        if _keep_driver:
            should_quit = False
            return all_results, driver
        return all_results

    except Exception as e:
        return [{"status": "error", "code": "CNKI_SEARCH_FAILED", "message": str(e)}]
    finally:
        if driver is not None:
            if should_quit:
                _release_driver(driver)
            else:
                _save_cookies(driver)


def _dedupe_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按标题全局去重，保留字段更完整的条目。"""
    seen: Dict[str, Dict[str, Any]] = {}
    no_title: List[Dict[str, Any]] = []
    for r in results:
        key = r.get("title", "").lower().strip()
        if not key:
            no_title.append(r)
            continue
        if key in seen:
            old = seen[key]
            if sum(1 for v in r.values() if v) > sum(1 for v in old.values() if v):
                seen[key] = r
        else:
            seen[key] = r
    return list(seen.values()) + no_title


def batch_search_cnki(
    keywords: List[str],
    core: str = None,
    author: str = None,
    journal: str = None,
    doc_type: str = None,
    field: str = None,
    year_from: int = None,
    year_to: int = None,
    sort: str = "relevance",
    pages: int = 1,
    affiliation: str = None,
    first_author: str = None,
    fund: str = None,
    fuzzy: bool = False,
) -> Dict[str, Any]:
    """批量知网搜索：一次启动浏览器，循环执行每组关键词（完整保留每轮文献类型、高级字段与核心筛选）。"""
    if not keywords:
        return {"status": "error", "code": "NO_KEYWORDS", "message": "未提供关键词"}

    try:
        if field:
            resolve_cnki_field_code(field)
    except ValueError as ve:
        return {"status": "error", "code": "CNKI_EXPR_SYNTAX_ERROR", "message": str(ve)}

    _log(f"[cnki-batch] 开始批量搜索: {len(keywords)} 组关键词")
    accessible, msg = check_cnki_access()
    if not accessible:
        if msg.startswith("SANDBOX_BLOCKED"):
            _log("[cnki-batch] 沙盒限制导致预检失败，浏览器可能仍可访问，继续尝试...")
        else:
            return {"status": "error", "code": "CNKI_UNREACHABLE", "message": msg}

    has_strict_adv = bool(
        author or first_author or affiliation or journal or fund or fuzzy
        or (field and resolve_cnki_field_code(field) != "SU")
    )
    use_adv_page = has_strict_adv or bool(core or year_from is not None or year_to is not None)
    entry_url = CNKI_ADV_SEARCH_URL if use_adv_page else CNKI_SEARCH_URL

    driver = None
    all_results = []
    errors = []
    core_filter_results: Dict[str, bool] = {}

    try:
        browser = _detect_browser()
        driver = _create_driver(browser=browser)

        _safe_get(driver, "https://kns.cnki.net/")
        time.sleep(0.6)
        _load_cookies(driver)

        for idx, keyword in enumerate(keywords):
            _log(f"\n[cnki-batch] === 搜索 {idx + 1}/{len(keywords)}: {keyword} ===")
            try:
                _safe_get(driver, entry_url)
                time.sleep(REQUEST_INTERVAL)
                driver = _handle_captcha(driver)
                if idx == 0:
                    _save_cookies(driver)

                doctype_done = False
                kw_core_ok = False
                year_preset_ok = False
                submitted = False

                if use_adv_page:
                    doctype_done = _preselect_adv_doctype(driver, doc_type, core)
                    expr_str = build_cnki_expression(
                        keyword=keyword,
                        field=field,
                        author=author,
                        first_author=first_author,
                        affiliation=affiliation,
                        journal=journal,
                        fund=fund,
                        fuzzy=fuzzy,
                    )
                    conds = _build_structured_conditions(
                        keyword=keyword,
                        field=field,
                        author=author,
                        first_author=first_author,
                        affiliation=affiliation,
                        journal=journal,
                        fund=fund,
                        fuzzy=fuzzy,
                    )
                    adv_res = _advanced_search(
                        driver,
                        conds,
                        expression=expr_str,
                        core=core,
                        year_from=year_from,
                        year_to=year_to,
                    )
                    if adv_res.get("ok"):
                        submitted = True
                        kw_core_ok = bool(adv_res.get("core_preselected"))
                        year_preset_ok = bool(adv_res.get("year_preset"))
                    elif has_strict_adv:
                        errors.append({
                            "keyword": keyword,
                            "code": "CNKI_ADV_SEARCH_FAILED",
                            "error": f"高级检索条件提交失败: {adv_res.get('reason')}",
                        })
                        continue

                if not submitted:
                    if driver.current_url != CNKI_SEARCH_URL:
                        driver.get(CNKI_SEARCH_URL)
                        time.sleep(REQUEST_INTERVAL)
                        driver = _handle_captcha(driver)
                    _input_search_keyword(driver, keyword)
                    doctype_done = False
                    kw_core_ok = False
                    year_preset_ok = False

                time.sleep(max(1, REQUEST_INTERVAL - 1))
                driver = _handle_captcha(driver)
                _wait_for_results_refresh(driver, prev_snapshot=None, timeout=15.0)

                # 每轮关键词均保持 doc_type，修复原先仅 idx==0 生效的缺陷
                if doc_type and not doctype_done:
                    if _switch_to_doc_type(driver, doc_type):
                        driver = _handle_captcha(driver)

                if core:
                    if not kw_core_ok:
                        kw_core_ok = _set_core_filter(driver, core)
                        if not kw_core_ok and not doc_type:
                            if _switch_to_journal_db(driver):
                                driver = _handle_captcha(driver)
                                kw_core_ok = _set_core_filter(driver, core)
                    core_filter_results[keyword] = kw_core_ok

                if (year_from is not None or year_to is not None) and not year_preset_ok:
                    _set_year_filter(driver, year_from, year_to)

                if sort and sort not in ("relevance", "quality"):
                    _set_sort(driver, sort)

                kw_results = []
                for page_num in range(pages):
                    if page_num > 0:
                        if not _go_next_page(driver):
                            break
                        driver = _handle_captcha(driver)
                    page_results = _parse_search_results(driver)
                    _log(f"[cnki-batch] {keyword} 第 {page_num + 1} 页: {len(page_results)} 条")
                    kw_results.extend(page_results)
                    if not page_results:
                        break

                if core and kw_core_ok and kw_results:
                    core_labels = _parse_core_input(core)
                    for r in kw_results:
                        if r.get("title"):
                            r["is_core"] = True
                            r["core_type"] = ",".join(core_labels)

                all_results.extend(kw_results)
                _log(f"[cnki-batch] {keyword}: 获取 {len(kw_results)} 条")

            except Exception as e:
                err_msg = f"搜索 '{keyword}' 失败: {e}"
                _log(f"[cnki-batch] {err_msg}")
                errors.append({"keyword": keyword, "code": "CNKI_SEARCH_FAILED", "error": str(e)})
                continue

        deduped = _dedupe_results(all_results)
        deduped = _filter_by_year(deduped, year_from, year_to)

        status = "error" if (errors and not deduped) else ("partial" if errors else "success")
        result: Dict[str, Any] = {
            "status": status,
            "count": len(deduped),
            "results": deduped,
        }
        if status == "error" and errors:
            result["code"] = errors[0].get("code", "CNKI_BATCH_FAILED")
            result["message"] = errors[0].get("error", "批量搜索失败")
        if errors:
            result["errors"] = errors
        if core and core_filter_results:
            failed_kws = [kw for kw, ok in core_filter_results.items() if not ok]
            if failed_kws:
                result["filter_warning"] = (
                    f"侧边栏筛选 '{core}' 未生效（结果可能包含非核心文献）: {', '.join(failed_kws)}"
                )
        return result

    except Exception as e:
        return {"status": "error", "code": "CNKI_BATCH_FAILED", "message": str(e)}
    finally:
        if driver is not None:
            _release_driver(driver)


# ── 普通搜索框与结果页交互（带 AJAX 同步等待） ─────────────────────────

def _input_search_keyword(driver, keyword: str) -> None:
    selectors = [
        (By.CSS_SELECTOR, "#txt_search"),
        (By.CSS_SELECTOR, "input.search-input"),
        (By.ID, "txt_SearchText"),
        (By.CSS_SELECTOR, "input[name='txt_1_value1']"),
    ]
    search_input = None
    for by, selector in selectors:
        try:
            search_input = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((by, selector))
            )
            break
        except TimeoutException:
            continue

    if not search_input:
        raise RuntimeError("无法找到知网搜索输入框")

    search_input.clear()
    search_input.send_keys(keyword)
    time.sleep(0.3)

    for sel in ("input.search-btn", ".search-btn", "input.btnSearch", "button.search-btn"):
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script("arguments[0].click();", btn)
            return
        except (NoSuchElementException, ElementClickInterceptedException):
            continue

    search_input.send_keys(Keys.RETURN)


def _set_year_filter(driver, year_from: int = None, year_to: int = None) -> None:
    """结果页侧边栏「年度」筛选（逐项勾选并等待 AJAX 刷新，避免单次循环连点丢失）。"""
    if year_from is None and year_to is None:
        return

    current_year = time.localtime().tm_year
    y_from = year_from or 1900
    y_to = year_to or current_year

    try:
        try:
            WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.CSS_SELECTOR, "dt.tit")))
        except TimeoutException:
            _log("[cnki] 侧边栏未加载，将依赖客户端过滤")
            return

        expand_result = driver.execute_script(r"""
            var dts = document.querySelectorAll('dt.tit');
            for (var i = 0; i < dts.length; i++) {
                if ((dts[i].textContent || '').indexOf('年度') >= 0) {
                    var dl = dts[i].closest('dl');
                    if (dl && dl.classList.contains('off')) {
                        dts[i].click();
                        return 'expanded';
                    }
                    return 'already_open';
                }
            }
            return 'not_found';
        """)
        if expand_result == "not_found":
            return
        if expand_result == "expanded":
            time.sleep(1.0)

        # 读取侧边栏列出的年份；若跨度过大或部分年份不在侧边栏 Top 10 中，避免误排除未列出年份
        avail_years = driver.execute_script(r"""
            var dts = document.querySelectorAll('dt.tit');
            for (var i = 0; i < dts.length; i++) {
                if ((dts[i].textContent || '').indexOf('年度') >= 0) {
                    var dl = dts[i].closest('dl');
                    if (!dl) return [];
                    var items = dl.querySelectorAll('dd li');
                    var ys = [];
                    for (var j = 0; j < items.length; j++) {
                        var a = items[j].querySelector('a');
                        var txt = a ? a.textContent.trim() : items[j].textContent.trim();
                        var n = parseInt(txt, 10);
                        if (!isNaN(n)) ys.push(n);
                    }
                    return ys;
                }
            }
            return [];
        """) or []

        target_years = [y for y in avail_years if y_from <= int(y) <= y_to]
        # 仅当目标年份数量合理（<=4）且请求的是单年或近几年连续区间时在侧边栏逐个勾选，否则交由客户端过滤防止漏掉非 Top10 年份
        if not target_years or len(target_years) > 4:
            _log(f"[cnki] 侧边栏年份 {target_years} 交由客户端精确过滤 ({y_from}-{y_to})")
            return

        for yr in target_years:
            snap = _capture_results_signature(driver)
            clicked = driver.execute_script(r"""
                var targetYr = arguments[0];
                var dts = document.querySelectorAll('dt.tit');
                for (var i = 0; i < dts.length; i++) {
                    if ((dts[i].textContent || '').indexOf('年度') >= 0) {
                        var dl = dts[i].closest('dl');
                        if (!dl) return false;
                        var items = dl.querySelectorAll('dd li');
                        for (var j = 0; j < items.length; j++) {
                            var cb = items[j].querySelector('input[type=checkbox]');
                            var a = items[j].querySelector('a');
                            var txt = a ? a.textContent.trim() : items[j].textContent.trim();
                            if (parseInt(txt, 10) === targetYr && cb && !cb.checked) {
                                cb.click();
                                return true;
                            }
                        }
                    }
                }
                return false;
            """, int(yr))
            if clicked:
                _wait_for_results_refresh(driver, prev_snapshot=snap, timeout=12.0)
    except Exception as e:
        _log(f"[cnki] 年份筛选异常: {e}，将依赖客户端过滤")


def _switch_to_doc_type(driver, doc_type: str) -> bool:
    """点击知网搜索结果页顶部的文献类型标签，并等待表格 AJAX 刷新完成。"""
    keywords = _DOCTYPE_TAB_MAP.get(doc_type)
    if not keywords:
        _log(f"[cnki] 未知文献类型: {doc_type}")
        return False

    snap = _capture_results_signature(driver)
    switch_js = r"""
    var keywords = arguments[0];
    var tabs = document.querySelectorAll('.doctype-menus a[name="classify"], .doctype-menus a, .doctype-menus li a');
    var available = [];
    for (var i = 0; i < tabs.length; i++) {
        var t = (tabs[i].textContent || '').trim();
        available.push(t);
        for (var k = 0; k < keywords.length; k++) {
            if (t.indexOf(keywords[k]) >= 0) {
                if (tabs[i].classList.contains('cur') || (tabs[i].parentElement && tabs[i].parentElement.classList.contains('cur'))) {
                    return JSON.stringify({status: 'already_active', text: t});
                }
                tabs[i].click();
                return JSON.stringify({status: 'clicked', text: t, classid: tabs[i].getAttribute('classid')});
            }
        }
    }
    return JSON.stringify({status: 'not_found', available: available});
    """
    try:
        raw = driver.execute_script(switch_js, list(keywords))
        info = json.loads(raw) if raw else {}
        _log(f"[cnki] 切换文献类型 '{doc_type}': {json.dumps(info, ensure_ascii=False)}")
        if info.get("status") == "already_active":
            return True
        if info.get("status") == "clicked":
            _wait_for_results_refresh(driver, prev_snapshot=snap, timeout=15.0)
            return True
    except Exception as e:
        _log(f"[cnki] 切换文献类型失败: {e}")
    return False


def _switch_to_journal_db(driver) -> bool:
    return _switch_to_doc_type(driver, "journal")


def _parse_core_input(core: str) -> list[str]:
    items = [s.strip() for s in core.split(",") if s.strip()]
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _set_core_filter(driver, core: str) -> bool:
    """结果页侧边栏 `dl[groupid="LYBSM"]` 来源类别筛选（每次勾选后同步等待表格实际刷新）。"""
    core_names = _parse_core_input(core)
    if not core_names:
        return False

    unknown = [n for n in core_names if n not in CNKI_SIDEBAR_CORES]
    if unknown:
        _log(f"[cnki] 提示: 以下来源类别不在常见列表中: {unknown}（仍尝试匹配）")

    click_one_js = r"""
    var coreName = arguments[0];
    var lybsm = document.querySelector('dl[groupid="LYBSM"]');
    if (lybsm) {
        if (lybsm.classList.contains('off')) {
            var dt = lybsm.querySelector('dt');
            if (dt) dt.click();
        }
        var links = lybsm.querySelectorAll('a');
        for (var i = 0; i < links.length; i++) {
            var title = (links[i].getAttribute('title') || '').trim();
            var text = (links[i].textContent || '').trim();
            if (title === coreName || text === coreName || text.indexOf(coreName) >= 0) {
                var cb = links[i].querySelector('input[type="checkbox"]');
                if (cb && cb.checked) {
                    return JSON.stringify({status:'already_checked', text: text});
                }
                links[i].scrollIntoView({block: 'center'});
                links[i].click();
                return JSON.stringify({status:'clicked', text: text});
            }
        }
        var avail = [];
        for (var j = 0; j < links.length; j++) {
            avail.push((links[j].getAttribute('title') || links[j].textContent || '').trim());
        }
        return JSON.stringify({status:'type_not_found', available: avail});
    }
    return JSON.stringify({status:'no_lybsm'});
    """

    matched_any = False
    for name in core_names:
        for attempt in range(4):
            if attempt > 0:
                time.sleep(1.5 + attempt * 2)
            try:
                snap = _capture_results_signature(driver)
                raw = driver.execute_script(click_one_js, name)
                info = json.loads(raw) if raw else {}
                status = info.get("status", "error")

                if status == "clicked":
                    _log(f"[cnki] 已勾选侧边栏来源: {name}")
                    _wait_for_results_refresh(driver, prev_snapshot=snap, timeout=15.0)
                    matched_any = True
                    break
                if status == "already_checked":
                    matched_any = True
                    break
                if status == "no_lybsm":
                    break
                if status == "type_not_found":
                    if info.get("available"):
                        break
                    continue
            except Exception as e:
                _log(f"[cnki] 核心筛选脚本执行失败: {e}")
                break

    return matched_any


def _set_sort(driver, sort: str) -> None:
    """切换排序方式并等待结果表 AJAX 实际刷新完成。"""
    sort_id_map = {
        "date": "PT",
        "citations": "CF",
        "downloads": "DFR",
        "relevance": "FFD",
        "comprehensive": "ZH",
    }
    sort_text_map = {
        "date": "发表时间",
        "citations": "被引",
        "downloads": "下载",
        "relevance": "相关度",
        "comprehensive": "综合",
    }
    sort_id = sort_id_map.get(sort)
    sort_text = sort_text_map.get(sort)
    if not sort_id:
        return

    snap = _capture_results_signature(driver)
    try:
        li = driver.find_element(By.CSS_SELECTOR, f"#orderList li#{sort_id}")
        if "cur" in (li.get_attribute("class") or ""):
            return
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", li)
        try:
            li.click()
        except ElementClickInterceptedException:
            driver.execute_script("arguments[0].click();", li)
        _log(f"[cnki] 已切换排序: {sort_text} (#{sort_id})")
        _wait_for_results_refresh(driver, prev_snapshot=snap, timeout=15.0)
        return
    except NoSuchElementException:
        pass

    try:
        li = driver.find_element(By.XPATH, f"//ul[@id='orderList']/li[contains(text(),'{sort_text}')]")
        driver.execute_script("arguments[0].click();", li)
        _wait_for_results_refresh(driver, prev_snapshot=snap, timeout=15.0)
    except NoSuchElementException:
        _log(f"[cnki] 未找到排序选项: {sort_text}")


def _set_per_page(driver, per_page: int = 50) -> bool:
    """将知网列表页单页展示条数切换为 10/20/50（默认 20，切 50 后单页吞吐提升 2.5 倍）。"""
    target = str(per_page)
    try:
        current = driver.execute_script(r"""
            var cur = document.querySelector('#perPageDiv .sort-default span, #perPageDiv .sort-default');
            return cur ? cur.textContent.trim() : '';
        """) or ""
        if current == target:
            return True

        snap = _capture_results_signature(driver)
        clicked = driver.execute_script(r"""
            var target = arguments[0];
            var box = document.querySelector('#perPageDiv');
            if (!box) return false;
            var trigger = box.querySelector('.sort-default');
            if (trigger) { try { trigger.click(); } catch (e) {} }
            var items = box.querySelectorAll('ul.sort-list li, li');
            for (var i = 0; i < items.length; i++) {
                var dv = (items[i].getAttribute('data-val') || '').trim();
                var txt = (items[i].textContent || '').trim();
                if (dv === target || txt === target) {
                    var a = items[i].querySelector('a') || items[i];
                    a.click();
                    return true;
                }
            }
            return false;
        """, target)
        if clicked:
            _log(f"[cnki] 已切换单页显示条数: {target}")
            _wait_for_results_refresh(driver, prev_snapshot=snap, timeout=15.0)
            return True
    except Exception as e:
        _log(f"[cnki] 切换单页条数失败: {e}")
    return False


def _bulk_export_from_results_page(driver, max_items: int = 50) -> List[Dict[str, Any]]:
    """尝试勾选当前页结果并通过知网 `Refworks` 题录导出一次性拿全摘要、关键词与卷期页码。

    注意：2026 版知网题录导出在仅校园网 IP 登录、未登录个人账号时会重定向至 `login.cnki.net`。
    本函数会在 <1s 内自动检测该重定向，立即关窗清理复选框并返回空列表，绝不阻塞主流程。
    """
    from .download import _parse_refworks_export

    main_handle = driver.current_window_handle
    before_handles = set(driver.window_handles)
    new_handle = None
    try:
        checked = driver.execute_script(r"""
            var limit = arguments[0];
            var cbs = document.querySelectorAll('table.result-table-list input.cbItem, td.seq input[type="checkbox"]');
            var cnt = 0;
            for (var i = 0; i < cbs.length && cnt < limit; i++) {
                if (!cbs[i].checked) cbs[i].click();
                cnt++;
            }
            var exp = document.querySelector('a[exporttype="Refworks"]');
            if (cnt > 0 && exp) {
                exp.click();
                return cnt;
            }
            return 0;
        """, int(max_items))
        if not checked:
            return []

        deadline = time.time() + 4.0
        while time.time() < deadline:
            diff = set(driver.window_handles) - before_handles
            if diff:
                new_handle = diff.pop()
                break
            time.sleep(0.2)

        if not new_handle:
            return []

        driver.switch_to.window(new_handle)
        raw_export = ""
        poll_end = time.time() + 6.0
        while time.time() < poll_end:
            cur_url = (driver.current_url or "").lower()
            cur_title = driver.title or ""
            if "login.cnki.net" in cur_url or "登录" in cur_title:
                _log("[cnki-export] 批量题录导出需知网个人账号登录（当前仅机构 IP），自动跳过并回退详情补全")
                return []
            raw_export = driver.execute_script(r"""
                var sels = ['#litotxt', '.literature-list', '#result', 'pre', 'textarea'];
                for (var i = 0; i < sels.length; i++) {
                    var el = document.querySelector(sels[i]);
                    if (el) {
                        var t = (el.value || el.innerText || el.textContent || '').trim();
                        if (t.indexOf('T1 ') >= 0 || t.indexOf('RT ') >= 0) return t;
                    }
                }
                var bodyTxt = (document.body && document.body.innerText) || '';
                if (bodyTxt.indexOf('T1 ') >= 0) return bodyTxt;
                return '';
            """) or ""
            if raw_export:
                break
            time.sleep(0.35)

        if not raw_export:
            return []
        return _parse_refworks_export(raw_export)
    except Exception as e:
        _log(f"[cnki-export] 批量题录导出异常: {e}")
        return []
    finally:
        if new_handle:
            try:
                if new_handle in driver.window_handles:
                    driver.switch_to.window(new_handle)
                    driver.close()
            except Exception:
                pass
        try:
            driver.switch_to.window(main_handle)
            driver.execute_script(r"""
                var clearBtn = document.querySelector('a.clearAll, #clearChecked, .BatchOper a[href*="clear"]');
                if (clearBtn) { clearBtn.click(); return; }
                document.querySelectorAll('table.result-table-list input.cbItem:checked').forEach(function(cb){ cb.click(); });
            """)
        except Exception:
            pass


def _merge_bulk_export_into_results(page_results: List[Dict[str, Any]], exported: List[Dict[str, Any]]) -> int:
    """按归一化题名将 Refworks 导出字段合并回搜索结果行。"""
    def _norm(s: str) -> str:
        return re.sub(r"\W+", "", (s or "")).lower()

    exp_map = {_norm(item.get("title", "")): item for item in exported if item.get("title")}
    merged = 0
    for r in page_results:
        item = exp_map.get(_norm(r.get("title", "")))
        if not item:
            continue
        hit = False
        for k in ("abstract", "keywords", "volume", "issue", "pages", "doi", "institutions"):
            if item.get(k) and not r.get(k):
                r[k] = item[k]
                hit = True
        if hit:
            merged += 1
    return merged


def _go_next_page(driver) -> bool:
    """翻到下一页并等待新页结果加载完成；若已是末页或无下一页按钮则返回 False。"""
    snap = _capture_results_signature(driver)
    try:
        btns = driver.find_elements(By.CSS_SELECTOR, "a#PageNext, #PageNext, a.next")
        target_btn = None
        for b in btns:
            cls = (b.get_attribute("class") or "").lower()
            if "disabled" in cls or "off" in cls:
                continue
            if b.is_displayed():
                target_btn = b
                break
        if target_btn is None:
            return False

        try:
            target_btn.click()
        except ElementClickInterceptedException:
            driver.execute_script("arguments[0].click();", target_btn)

        _wait_for_results_refresh(driver, prev_snapshot=snap, timeout=15.0)
        return True
    except Exception as e:
        _log(f"[cnki] 翻页失败: {e}")
        return False


def _extract_pages_from_gbt7714(ref: str) -> str:
    ref = ref.strip()
    if not ref:
        return ""
    patterns = (
        r"[,，]\s*(\d+(?:[-–—]\d+)?)\s*\[(?:19|20)\d{2}",
        r"\)\s*[:：]\s*(\d+(?:[-–—]\d+)?)",
        r"(?:19|20)\d{2}[^.。]*[:：]\s*(\d+(?:[-–—]\d+)?)",
        r"[,，]\s*(\d+[-–—]\d+)(?=\.|。|$)",
    )
    for pattern in patterns:
        matches = re.findall(pattern, ref)
        if matches:
            return matches[-1].replace("–", "-").replace("—", "-")
    return ""


# ── 结果解析 ────────────────────────────────────────────────────────────

def _parse_search_results(driver, cite_enrich: int = 0) -> list[dict]:
    results = []
    rows = []
    for selector in CNKI_SELECTORS["results"]["rows"]:
        rows = driver.find_elements(By.CSS_SELECTOR, selector)
        if rows:
            break

    for idx, row in enumerate(rows):
        try:
            result = _parse_single_row(row)
            if result and result.get("title"):
                if idx < cite_enrich:
                    try:
                        _enrich_row_from_quote_popup(driver, row, result)
                    except Exception as e:
                        _log(f"[cnki] 引用补全跳过: {e}")
                results.append(result)
        except Exception as e:
            _log(f"[cnki] 单行解析失败: {e}")
            continue
    return results


def _enrich_row_from_quote_popup(driver, row, result: Dict[str, Any]) -> None:
    try:
        quote_btn = row.find_element(By.CSS_SELECTOR, "a.icon-quote")
    except NoSuchElementException:
        return

    try:
        driver.execute_script("arguments[0].click();", quote_btn)
        WebDriverWait(driver, 8).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, ".quote-pop"))
        )
        gbt_text = driver.execute_script(r"""
            var rows = document.querySelectorAll('.quote-pop tr');
            for (var i = 0; i < rows.length; i++) {
                var left = rows[i].querySelector('.quote-l');
                var right = rows[i].querySelector('.quote-r');
                if (left && right && left.textContent.indexOf('GB/T') >= 0) {
                    return right.textContent.trim();
                }
            }
            var first = document.querySelector('.quote-pop .quote-r');
            return first ? first.textContent.trim() : '';
        """) or ""
        if gbt_text:
            result["gbt7714_raw"] = gbt_text
            pages = _extract_pages_from_gbt7714(gbt_text)
            if pages and not result.get("pages"):
                result["pages"] = pages
    except Exception as e:
        _log(f"[cnki] 引用弹窗解析失败: {e}")
    finally:
        try:
            close_btn = driver.find_element(By.CSS_SELECTOR, ".quote-pop .layui-layer-close")
            driver.execute_script("arguments[0].click();", close_btn)
            time.sleep(0.2)
        except Exception:
            try:
                driver.execute_script("document.querySelectorAll('.quote-pop').forEach(function(e){e.remove();});")
            except Exception:
                pass


_ROW_DATA_DOCTYPE_MAP = {
    "期刊": "journal",
    "网络首发": "journal",
    "硕士": "master",
    "博士": "doctor",
    "会议": "conference",
    "中国会议": "conference",
    "国际会议": "conference",
    "报纸": "newspaper",
    "图书": "book",
    "专著": "book",
    "标准": "standard",
    "专利": "patent",
    "成果": "report",
}


def _parse_single_row(row) -> dict:
    result = {
        "title": "",
        "authors": "",
        "journal": "",
        "date": "",
        "cited_by": 0,
        "downloads": 0,
        "url": "",
        "is_core": False,
        "core_type": "",
        "source": "CNKI",
    }

    try:
        title_el = row.find_element(By.CSS_SELECTOR, "td.name a, .fz14")
        result["title"] = title_el.text.strip()
        result["url"] = title_el.get_attribute("href") or ""
    except NoSuchElementException:
        return result

    try:
        author_el = row.find_element(By.CSS_SELECTOR, "td.author, .author")
        result["authors"] = author_el.text.strip()
    except NoSuchElementException:
        pass

    try:
        source_el = row.find_element(By.CSS_SELECTOR, "td.source, .source")
        result["journal"] = source_el.text.strip()
    except NoSuchElementException:
        pass

    try:
        date_el = row.find_element(By.CSS_SELECTOR, "td.date, .date")
        result["date"] = date_el.text.strip()
        year_m = re.search(r"(?:19|20)\d{2}", result["date"])
        if year_m:
            result["year"] = int(year_m.group(0))
        if re.search(r"\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}", result["date"]) or "网络首发" in result["date"]:
            result["is_online_first"] = True
    except NoSuchElementException:
        pass

    try:
        data_el = row.find_element(By.CSS_SELECTOR, "td.data, .data")
        data_txt = (data_el.text or "").strip()
        if "网络首发" in data_txt:
            result["is_online_first"] = True
        for cn_label, dtype in _ROW_DATA_DOCTYPE_MAP.items():
            if cn_label in data_txt:
                result["doc_type"] = dtype
                break
    except NoSuchElementException:
        pass

    try:
        cite_el = row.find_element(By.CSS_SELECTOR, "td.quote, .quote, td.count")
        cite_digits = re.sub(r"[^\d]", "", cite_el.text or "")
        if cite_digits:
            result["cited_by"] = int(cite_digits)
    except (NoSuchElementException, ValueError):
        pass

    try:
        dl_el = row.find_element(By.CSS_SELECTOR, "td.download, .download")
        dl_digits = re.sub(r"[^\d]", "", dl_el.text or "")
        if dl_digits:
            result["downloads"] = int(dl_digits)
    except (NoSuchElementException, ValueError):
        pass

    try:
        mark_els = row.find_elements(By.CSS_SELECTOR, "td.name .marktip, td.name span")
        for m_el in mark_els:
            if "网络首发" in (m_el.text or ""):
                result["is_online_first"] = True
                break
    except Exception:
        pass

    try:
        badge_els = row.find_elements(
            By.CSS_SELECTOR,
            "span[class*='icon'], em[class*='icon'], i[class*='icon'], .badge, .tag",
        )
        detected_cores = set()
        for badge in badge_els:
            cls = (badge.get_attribute("class") or "").lower()
            txt = (badge.text or "").strip()
            if "cssci" in cls or txt == "CSSCI" or "南大核心" in txt:
                detected_cores.add("CSSCI")
            elif "pku" in cls or "北大核心" in cls or txt in ("北大核心", "核心"):
                detected_cores.add("北大核心")
            elif "cscd" in cls or txt == "CSCD":
                detected_cores.add("CSCD")
            elif "sci" in cls or txt == "SCI":
                detected_cores.add("SCI")
            elif "ei" in cls or txt == "EI":
                detected_cores.add("EI")
            elif "科技核心" in cls or "统计源" in cls or txt in ("科技核心", "统计源"):
                detected_cores.add("科技核心")
        if detected_cores:
            result["is_core"] = True
            result["core_type"] = ";".join(sorted(detected_cores))
    except Exception:
        pass

    return result
