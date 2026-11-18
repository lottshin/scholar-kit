"""
cnki.selectors - 知网页面 CSS/DOM 选择器集中注册表与改版自检探针

设计目的:
1. 将分散在 search / detail / download 中的 CSS 选择器统一收敛到 `CNKI_SELECTORS`，
   知网前端微调类名时只需改这一处注册表，无需翻阅上千行自动化流程代码。
2. 提供脱敏 DOM 结构快照 (`save_adv_dom_diagnosis`) 与 5 秒契约自检 (`probe_cnki_dom_contracts`)：
   当高级检索失败或执行 `check --cnki-dom` 时，自动输出各核心锚点健康状态及当前页面控件结构摘要，
   方便 Agent 或开发者秒级定位改版位置。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .constants import CNKI_ADV_SEARCH_URL, _log


CNKI_SELECTORS: Dict[str, Dict[str, Any]] = {
    "adv_search": {
        "doctype_nav": '.doctype-menus li, .doctype-menus a, .header-nav li, .classify-menu li, ul[class*="doctype"] li',
        "major_tab": 'li[name="majorSearch"], [data-id="majorSearch"]',
        "grade_tab": 'li[name="gradeSearch"], [data-id="gradeSearch"]',
        "major_textarea": "textarea.textarea-major, .major-search textarea, #gradetxt textarea",
        "grade_rows": "#gradetxt dd, .grade-search-content dd, .sort-item",
        "grade_add_row": '#gradetxt a.add-group, #gradetxt .add-group, #gradetxt [class*="add"], .btn-add, a[title*="添加"]',
        "core_checkboxes": '.extend-tit-labels input[type="checkbox"], input[type="checkbox"]',
        "yearbox_start": '.tit-startend-yearbox .sort.start input[type="text"], input[placeholder="起始年"]',
        "yearbox_end": '.tit-startend-yearbox .sort.end input[type="text"], input[placeholder="结束年"]',
        "datebox_start": "#datebox0, .tit-date-box input.publishdate:first-of-type",
        "datebox_end": "#datebox1, .tit-date-box input.publishdate:last-of-type",
        "submit_buttons": (
            "input.btn-search, button.btn-search",
            ".search-buttons .btn-search, .search-btn",
        ),
        "syntax_error": (
            ".layui-layer-content",
            ".error-tips",
            ".tip-info",
            ".major-tips-error",
            ".search-tips-error",
        ),
    },
    "simple_search": {
        "inputs": (
            "#txt_search",
            "input.search-input",
            "#txt_SearchText",
            "input[name='txt_1_value1']",
        ),
        "submit_buttons": (
            "input.search-btn",
            ".search-btn",
            "input.btnSearch",
            "button.search-btn",
        ),
    },
    "results": {
        "table": "table.result-table-list, .result-table-list, #gridTable",
        "rows": (
            "table.result-table-list tbody tr",
            ".result-table-list tbody tr",
            "#gridTable tbody tr",
        ),
        "row_title": "td.name a, .fz14",
        "row_author": "td.author, .author",
        "row_source": "td.source, .source",
        "row_date": "td.date, .date",
        "row_data": "td.data, .data",
        "row_cite": "td.quote, .quote, td.count",
        "row_download": "td.download, .download",
        "cb_item": "input.cbItem, td.seq input[type='checkbox']",
        "per_page_div": "#perPageDiv",
        "export_refworks": 'a[exporttype="Refworks"]',
        "quote_btn": "a.icon-quote",
        "quote_popup": ".quote-pop",
        "next_page": "a#PageNext, #PageNext, a.next",
        "sidebar_core": 'dl[groupid="LYBSM"]',
    },
    "detail": {
        "title": ".wx-tit h1, h1.title, h1[class*='title'], h1",
        "abstract": "#ChDivSummary, .abstract-text, span#ChDivSummary",
        "keywords": ".keywords a, p.keywords a",
        "authors": ".author a, h3.author a",
        "orgs": ".orgn a, .organ a",
        "funds": ".fund a, p.fund a",
        "citnet_box": "#refpartdiv",
    },
    "download": {
        "pdf_btn_id": "pdfDown",
        "caj_btn_id": "cajDown",
    },
}


def _diagnosis_output_path() -> Path:
    try:
        from config import global_state_dir
        return global_state_dir(create=True) / "cnki-dom-diagnosis.json"
    except ImportError:
        d = Path.cwd() / ".scholar-kit"
        d.mkdir(parents=True, exist_ok=True)
        return d / "cnki-dom-diagnosis.json"


def capture_adv_dom_diagnosis(driver, reason: str = "") -> Dict[str, Any]:
    """提取当前知网页面脱敏控件结构摘要（不含 Cookie 与个人账号信息）。"""
    snapshot: Dict[str, Any] = {
        "captured_at": datetime.now().isoformat(),
        "reason": reason,
        "url": "",
        "title": "",
    }
    try:
        snapshot["url"] = getattr(driver, "current_url", "") or ""
        snapshot["title"] = getattr(driver, "title", "") or ""
        dom_summary = driver.execute_script(r"""
            function vis(el) { return !!(el && el.offsetParent !== null); }
            function brief(el) {
                return {
                    tag: el.tagName,
                    id: el.id || '',
                    cls: (el.className && typeof el.className === 'string') ? el.className.slice(0, 80) : '',
                    name: el.getAttribute('name') || '',
                    key: el.getAttribute('key') || '',
                    dataId: el.getAttribute('data-id') || '',
                    type: el.getAttribute('type') || '',
                    placeholder: (el.getAttribute('placeholder') || '').slice(0, 40),
                    text: ((el.value && el.type === 'button') ? el.value : (el.textContent || '')).trim().slice(0, 30),
                    vis: vis(el)
                };
            }
            return {
                doctypeTabs: Array.from(document.querySelectorAll('.doctype-menus li, .doctype-menus a, .header-nav li')).slice(0, 12).map(brief),
                modeTabs: Array.from(document.querySelectorAll('.search-classify-menu li, [name*="Search"]')).slice(0, 10).map(brief),
                textareas: Array.from(document.querySelectorAll('textarea')).slice(0, 5).map(brief),
                gradeRowCount: document.querySelectorAll('#gradetxt dd').length,
                visibleInputs: Array.from(document.querySelectorAll('input[type="text"], input.publishdate')).filter(vis).slice(0, 15).map(brief),
                checkboxes: Array.from(document.querySelectorAll('input[type="checkbox"]')).filter(vis).slice(0, 20).map(brief),
                buttons: Array.from(document.querySelectorAll('input[type="button"], button')).filter(vis).slice(0, 10).map(brief)
            };
        """)
        if isinstance(dom_summary, dict):
            snapshot["dom"] = dom_summary
    except Exception as e:
        snapshot["capture_error"] = str(e)
    return snapshot


def save_adv_dom_diagnosis(driver, reason: str = "") -> Optional[str]:
    """保存脱敏 DOM 结构诊断 JSON，返回文件路径。"""
    try:
        data = capture_adv_dom_diagnosis(driver, reason=reason)
        out = _diagnosis_output_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _log(f"[cnki-dom] 已保存页面控件诊断快照: {out}")
        return str(out)
    except Exception as e:
        _log(f"[cnki-dom] 保存诊断快照失败: {e}")
        return None


def probe_cnki_dom_contracts() -> Dict[str, Any]:
    """执行只读的知网高级检索页面 DOM 契约巡检（约 5 秒，不提交重度查询）。"""
    from .driver import _create_driver, _detect_browser, _handle_captcha, _load_cookies, _release_driver, _safe_get

    sels = CNKI_SELECTORS["adv_search"]
    driver = None
    try:
        browser = _detect_browser()
        driver = _create_driver(browser=browser)
        _safe_get(driver, "https://kns.cnki.net/")
        time.sleep(0.5)
        _load_cookies(driver)

        _safe_get(driver, CNKI_ADV_SEARCH_URL)
        time.sleep(1.5)
        driver = _handle_captcha(driver)

        probe_js = r"""
        var sels = arguments[0];
        function hasVis(sel) {
            var nodes = document.querySelectorAll(sel);
            for (var i = 0; i < nodes.length; i++) {
                if (nodes[i].offsetParent !== null) return true;
            }
            return false;
        }
        var contracts = {};
        contracts.doctype_tabs = hasVis(sels.doctype_nav);
        contracts.grade_search_rows = document.querySelectorAll(sels.grade_rows).length > 0;
        contracts.zongku_datebox = hasVis(sels.datebox_start) && hasVis(sels.datebox_end);
        contracts.major_search_tab = hasVis(sels.major_tab);

        // 切换到学术期刊 + 专业检索检查子库契约
        var navs = document.querySelectorAll(sels.doctype_nav);
        for (var n = 0; n < navs.length; n++) {
            if ((navs[n].textContent || '').trim().indexOf('学术期刊') >= 0) {
                (navs[n].querySelector('a') || navs[n]).click();
                break;
            }
        }
        return contracts;
        """
        part1 = driver.execute_script(probe_js, sels) or {}
        time.sleep(0.8)

        part2 = driver.execute_script(r"""
        var sels = arguments[0];
        function hasVis(sel) {
            var nodes = document.querySelectorAll(sel);
            for (var i = 0; i < nodes.length; i++) {
                if (nodes[i].offsetParent !== null) return true;
            }
            return false;
        }
        var mTab = document.querySelector(sels.major_tab);
        if (mTab && mTab.offsetParent !== null) mTab.click();
        var submitHit = false;
        for (var s = 0; s < sels.submit_buttons.length; s++) {
            if (hasVis(sels.submit_buttons[s])) { submitHit = true; break; }
        }
        var hx = document.querySelector('input[key="HX"]');
        var csi = document.querySelector('input[key="CSI"]');
        return {
            major_textarea: hasVis(sels.major_textarea),
            journal_core_checkboxes: !!(hx && csi),
            journal_yearbox: hasVis(sels.yearbox_start) && hasVis(sels.yearbox_end),
            submit_button: submitHit
        };
        """, sels) or {}

        contracts = {**part1, **part2}
        failed = [k for k, ok in contracts.items() if not ok]
        status = "ok" if not failed else ("degraded" if contracts.get("major_textarea") and contracts.get("submit_button") else "broken")

        result: Dict[str, Any] = {
            "status": status,
            "url": driver.current_url,
            "contracts": contracts,
            "failed_contracts": failed,
        }
        if failed:
            diag_path = save_adv_dom_diagnosis(driver, reason=f"failed_contracts:{','.join(failed)}")
            if diag_path:
                result["diagnosis_file"] = diag_path
        return result
    except Exception as e:
        return {
            "status": "error",
            "message": f"知网 DOM 契约巡检异常: {e}",
            "contracts": {},
            "failed_contracts": ["browser_or_network_error"],
        }
    finally:
        if driver is not None:
            _release_driver(driver)
