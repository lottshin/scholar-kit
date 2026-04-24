"""cnki.search - 知网搜索、筛选、翻页与结果解析"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from .constants import (
    HAS_SELENIUM, CNKI_SEARCH_URL, CNKI_ADV_SEARCH_URL,
    REQUEST_INTERVAL, CNKI_SIDEBAR_CORES, _log,
)
from .driver import (
    _detect_browser, _create_driver, _load_cookies, _save_cookies,
    _handle_captcha, check_cnki_access,
)

if HAS_SELENIUM:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException,
        ElementClickInterceptedException,
    )


def _year_from_date(date_str: str) -> Optional[int]:
    """从日期字符串提取年份，如 '2024-03-15' → 2024。失败返回 None。"""
    try:
        return int(date_str[:4])
    except (ValueError, IndexError, TypeError):
        return None


def _filter_by_year(results: list, year_from: Optional[int], year_to: Optional[int]) -> list:
    """客户端年份过滤，用于 UI 侧边栏筛选失败时的兜底。无日期或解析失败的条目保留。"""
    if not results or (year_from is None and year_to is None):
        return results
    filtered = []
    for r in results:
        if not r.get("date"):
            filtered.append(r)
            continue
        y = _year_from_date(r["date"])
        if y is None:
            filtered.append(r)
            continue
        if year_from is not None and y < year_from:
            continue
        if year_to is not None and y > year_to:
            continue
        filtered.append(r)
    return filtered


# ── 搜索 ─────────────────────────────────────────────

def search_cnki(
    keyword: str,
    core: str = None,
    year_from: int = None,
    year_to: int = None,
    author: str = None,
    journal: str = None,
    doc_type: str = None,
    field: str = None,
    sort: str = "relevance",
    pages: int = 1,
    _keep_driver: bool = False,
):
    """
    知网文献搜索。

    Args:
        keyword:       搜索关键词
        core:          核心期刊筛选 (北大核心/CSSCI/AMI/WJCI/CSCD/EI)
        year_from:     起始年份
        year_to:       截止年份
        author:        作者
        journal:       期刊名
        doc_type:      文献类型 (master/doctor/thesis/journal/conference/newspaper)
        field:         搜索字段 (主题/篇名/关键词/摘要/全文/作者/来源)，默认主题
        sort:          排序方式 (relevance/date/citations)
        pages:         抓取页数
        _keep_driver:  内部参数。为 True 时搜索成功后不关闭浏览器，
                       返回 (results, driver) 元组供 batch_download_cnki 复用。

    Returns:
        _keep_driver=False（默认）: list[dict] — 论文信息列表
        _keep_driver=True 且成功:  tuple[list[dict], WebDriver] — (论文列表, 浏览器实例)
        搜索异常时始终返回:        list[dict] — 含 status=error 的单元素列表
    """
    _log(f"[cnki] 开始搜索: keyword={keyword}, core={core}, "
         f"year_from={year_from}, doc_type={doc_type}, field={field}")
    accessible, msg = check_cnki_access()
    _log(f"[cnki] 网络检测: accessible={accessible}, msg={msg}")
    if not accessible:
        if msg.startswith("SANDBOX_BLOCKED"):
            _log("[cnki] 沙盒限制导致预检失败，浏览器可能仍可访问，继续尝试...")
        else:
            return [{"status": "error", "code": "CNKI_UNREACHABLE", "message": msg}]

    driver = None
    should_quit = True
    try:
        browser = _detect_browser()
        _log(f"[cnki] 浏览器: {browser}，正在启动...")
        driver = _create_driver(browser=browser)

        driver.get("https://kns.cnki.net/")
        time.sleep(1)
        _load_cookies(driver)

        use_advanced = bool(author or journal or (field and field != "主题"))

        if use_advanced:
            _log(f"[cnki] 使用高级搜索: keyword={keyword}, field={field}, "
                 f"author={author}, journal={journal}")
            driver.get(CNKI_ADV_SEARCH_URL)
            time.sleep(REQUEST_INTERVAL)
            driver = _handle_captcha(driver)

            kw_field = field or "主题"
            conditions = []
            if keyword:
                conditions.append((kw_field, keyword))
            if author:
                conditions.append(("作者", author))
            if journal:
                conditions.append(("来源", journal))

            if not _advanced_search(driver, conditions):
                _log("[cnki] 高级搜索提交失败，回退到简单搜索")
                use_advanced = False

        if not use_advanced:
            if not keyword:
                code = "ADV_SEARCH_FALLBACK" if (author or journal) else "NO_KEYWORDS"
                msg = ("高级搜索未成功且无关键词可回退到简单搜索"
                       if (author or journal)
                       else "未提供搜索关键词")
                return [{"status": "error", "code": code, "message": msg}]
            _log(f"[cnki] 正在加载搜索页: {CNKI_SEARCH_URL}")
            driver.get(CNKI_SEARCH_URL)
            time.sleep(REQUEST_INTERVAL)
            driver = _handle_captcha(driver)

            _input_search_keyword(driver, keyword)

        time.sleep(REQUEST_INTERVAL)
        driver = _handle_captcha(driver)

        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "table.result-table-list, .result-table-list"))
            )
            _log("[cnki] 搜索结果已加载")
        except TimeoutException:
            _log("[cnki] [!] 等待结果表格超时，尝试继续...")

        if doc_type:
            doc_type_ok = _switch_to_doc_type(driver, doc_type)
            if doc_type_ok:
                driver = _handle_captcha(driver)
            else:
                _log(f"[cnki] 文献类型筛选 '{doc_type}' 未生效")

        core_filter_ok = False
        if core:
            core_filter_ok = _set_core_filter(driver, core)
            if not core_filter_ok:
                _log("[cnki] 侧边栏筛选失败，尝试切换到学术期刊数据库...")
                if _switch_to_journal_db(driver):
                    driver = _handle_captcha(driver)
                    core_filter_ok = _set_core_filter(driver, core)
            _log(f"[cnki] 核心期刊筛选: {'成功' if core_filter_ok else '未生效（结果可能包含非核心文献）'}")
        if year_from is not None or year_to is not None:
            _set_year_filter(driver, year_from, year_to)
        if sort and sort != "relevance":
            _set_sort(driver, sort)

        time.sleep(1)
        _log(f"[cnki] 筛选后页面: url={driver.current_url[:80]}  title={driver.title}")

        all_results = []
        for page_num in range(pages):
            _log(f"[cnki] 解析第 {page_num + 1}/{pages} 页...")
            if page_num > 0:
                _go_next_page(driver)
                driver = _handle_captcha(driver)

            page_results = _parse_search_results(driver)
            _log(f"[cnki] 第 {page_num + 1} 页获取 {len(page_results)} 条结果")
            all_results.extend(page_results)

            if not page_results:
                break

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
        return [{"status": "error", "code": "CNKI_SEARCH_FAILED",
                 "message": str(e)}]
    finally:
        if driver is not None:
            try:
                _save_cookies(driver)
                if should_quit:
                    driver.quit()
            except Exception:
                pass


def _dedupe_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按标题全局去重，保留字段更完整的条目；无标题条目原样保留。"""
    seen: Dict[str, Dict[str, Any]] = {}
    no_title: List[Dict[str, Any]] = []
    for r in results:
        key = r.get("title", "").lower().strip()
        if not key:
            no_title.append(r)
            continue
        if key in seen:
            old = seen[key]
            new_fields = sum(1 for v in r.values() if v)
            old_fields = sum(1 for v in old.values() if v)
            if new_fields > old_fields:
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
) -> Dict[str, Any]:
    """
    批量知网搜索：一次启动浏览器、一次验证，循环搜索多组关键词。

    Args:
        keywords:  关键词列表
        core:      核心期刊筛选
        author:    作者（对每组关键词生效）
        journal:   期刊名（对每组关键词生效）
        doc_type:  文献类型筛选
        field:     搜索字段
        year_from: 起始年份
        year_to:   截止年份
        sort:      排序方式
        pages:     每组关键词抓取页数

    Returns:
        {"status": "success", "results": [...], "errors": [...]}
    """
    if not keywords:
        return {"status": "error", "code": "NO_KEYWORDS", "message": "未提供关键词"}

    _log(f"[cnki-batch] 开始批量搜索: {len(keywords)} 组关键词")
    accessible, msg = check_cnki_access()
    if not accessible:
        if msg.startswith("SANDBOX_BLOCKED"):
            _log("[cnki-batch] 沙盒限制导致预检失败，浏览器可能仍可访问，继续尝试...")
        else:
            return {"status": "error", "code": "CNKI_UNREACHABLE", "message": msg}

    driver = None
    all_results = []
    errors = []
    core_filter_results: Dict[str, bool] = {}

    try:
        browser = _detect_browser()
        _log(f"[cnki-batch] 浏览器: {browser}，正在启动...")
        driver = _create_driver(browser=browser)

        driver.get("https://kns.cnki.net/")
        time.sleep(1)
        _load_cookies(driver)

        driver.get(CNKI_SEARCH_URL)
        time.sleep(REQUEST_INTERVAL)
        driver = _handle_captcha(driver)
        _save_cookies(driver)
        _log("[cnki-batch] 浏览器就绪，开始逐组搜索...")

        use_advanced = bool(author or journal or (field and field != "主题"))

        for idx, keyword in enumerate(keywords):
            _log(f"\n[cnki-batch] === 搜索 {idx + 1}/{len(keywords)}: {keyword} ===")

            try:
                target_url = CNKI_ADV_SEARCH_URL if use_advanced else CNKI_SEARCH_URL
                if idx > 0:
                    driver.get(target_url)
                    time.sleep(REQUEST_INTERVAL)
                    driver = _handle_captcha(driver)
                elif use_advanced:
                    driver.get(CNKI_ADV_SEARCH_URL)
                    time.sleep(REQUEST_INTERVAL)
                    driver = _handle_captcha(driver)

                if use_advanced:
                    kw_field = field or "主题"
                    conditions = []
                    if keyword:
                        conditions.append((kw_field, keyword))
                    if author:
                        conditions.append(("作者", author))
                    if journal:
                        conditions.append(("来源", journal))
                    if not _advanced_search(driver, conditions):
                        _log(f"[cnki-batch] {keyword}: 高级搜索失败，回退简单搜索")
                        driver.get(CNKI_SEARCH_URL)
                        time.sleep(REQUEST_INTERVAL)
                        driver = _handle_captcha(driver)
                        _input_search_keyword(driver, keyword)
                else:
                    _input_search_keyword(driver, keyword)
                time.sleep(REQUEST_INTERVAL)
                driver = _handle_captcha(driver)

                try:
                    WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, "table.result-table-list, .result-table-list"))
                    )
                except TimeoutException:
                    _log(f"[cnki-batch] 等待结果超时: {keyword}")

                if doc_type and idx == 0:
                    if _switch_to_doc_type(driver, doc_type):
                        driver = _handle_captcha(driver)

                kw_core_ok = False
                if core:
                    kw_core_ok = _set_core_filter(driver, core)
                    if not kw_core_ok:
                        _log(f"[cnki-batch] {keyword}: 侧边栏筛选失败，尝试切换学术期刊数据库...")
                        if _switch_to_journal_db(driver):
                            driver = _handle_captcha(driver)
                            kw_core_ok = _set_core_filter(driver, core)
                    core_filter_results[keyword] = kw_core_ok

                if year_from is not None or year_to is not None:
                    _set_year_filter(driver, year_from, year_to)

                if sort and sort != "relevance":
                    _set_sort(driver, sort)

                time.sleep(1)

                kw_results = []
                for page_num in range(pages):
                    if page_num > 0:
                        _go_next_page(driver)
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

        _log(f"\n[cnki-batch] 完成: 总计 {len(all_results)} 条, 去重/过滤后 {len(deduped)} 条, 错误 {len(errors)} 组")

        result: Dict[str, Any] = {
            "status": "partial" if errors else "success",
            "count": len(deduped),
            "results": deduped,
        }
        if errors:
            result["errors"] = errors
        if core and core_filter_results:
            failed_kws = [kw for kw, ok in core_filter_results.items() if not ok]
            if failed_kws:
                result["filter_warning"] = (
                    f"侧边栏筛选 '{core}' 未生效（结果可能包含非核心文献）: "
                    f"{', '.join(failed_kws)}"
                )
        return result

    except Exception as e:
        return {"status": "error", "code": "CNKI_BATCH_FAILED",
                "message": str(e)}
    finally:
        if driver is not None:
            try:
                _save_cookies(driver)
                driver.quit()
            except Exception:
                pass


# ── 输入与筛选 ────────────────────────────────────────

def _input_search_keyword(driver, keyword: str):
    """在知网搜索页面输入关键词并提交"""
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
    time.sleep(0.5)

    btn_selectors = [
        "input.search-btn",
        ".search-btn",
        "input.btnSearch",
        "button.search-btn",
    ]
    for sel in btn_selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script("arguments[0].click();", btn)
            return
        except (NoSuchElementException, ElementClickInterceptedException):
            continue

    search_input.send_keys(Keys.RETURN)


# 知网高级搜索字段下拉值映射
_ADV_FIELD_MAP = {
    "主题": "SU",
    "篇名": "TI",
    "关键词": "KY",
    "摘要": "AB",
    "全文": "FT",
    "作者": "AU",
    "第一作者": "FI",
    "机构": "AF",
    "来源": "LY",
    "基金": "FU",
}


def _advanced_search(
    driver,
    conditions: list[tuple[str, str]],
) -> bool:
    """在知网高级搜索页面填写多条件表单并提交。

    Args:
        driver:     已在高级搜索页的浏览器实例
        conditions: [(字段名, 值), ...]，字段名为中文（主题/作者/来源等）

    Returns:
        是否成功提交搜索
    """
    if not conditions:
        return False

    _log(f"[cnki-adv] 高级搜索条件: {conditions}")

    set_and_submit_js = r"""
    var conditions = arguments[0];
    var fieldMap = arguments[1];
    var results = [];

    // 获取所有条件行
    var rows = document.querySelectorAll('.sort-list .sort-item, .search-sort .sort-item, [class*="sort-item"]');
    if (rows.length === 0) {
        // 备选：通过 input name 模式定位
        var inputs = document.querySelectorAll('input[name*="txt_"]');
        if (inputs.length === 0) {
            return JSON.stringify({status: 'no_rows', message: '未找到高级搜索条件行'});
        }
    }

    for (var i = 0; i < conditions.length; i++) {
        var fieldName = conditions[i][0];
        var value = conditions[i][1];
        var fieldCode = fieldMap[fieldName] || fieldName;

        if (i > 0) {
            // 点击"添加条件"按钮增加新行
            var addBtns = document.querySelectorAll('.btn-add, .add-btn, a[class*="add"], button[class*="add"]');
            for (var ab = 0; ab < addBtns.length; ab++) {
                if (addBtns[ab].offsetParent !== null) {
                    addBtns[ab].click();
                    break;
                }
            }
        }

        // 重新获取条件行（可能新增了）
        rows = document.querySelectorAll('.sort-list .sort-item, .search-sort .sort-item, [class*="sort-item"]');
        var rowIdx = Math.min(i, rows.length - 1);
        if (rowIdx < 0) {
            results.push({field: fieldName, status: 'no_row'});
            continue;
        }
        var row = rows[rowIdx];

        // 设置字段类型下拉
        var select = row.querySelector('select, [name*="fieldtype"], [name*="sField"]');
        if (select) {
            var options = select.querySelectorAll('option');
            for (var j = 0; j < options.length; j++) {
                if (options[j].value === fieldCode ||
                    options[j].textContent.trim() === fieldName) {
                    select.value = options[j].value;
                    select.dispatchEvent(new Event('change', {bubbles: true}));
                    break;
                }
            }
        }

        // 填写搜索值
        var input = row.querySelector('input[type="text"], input[name*="txt_"], input.search-input');
        if (input) {
            input.value = '';
            input.value = value;
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new Event('change', {bubbles: true}));
            results.push({field: fieldName, value: value, status: 'filled'});
        } else {
            results.push({field: fieldName, status: 'no_input'});
        }
    }

    var filledCount = 0;
    for (var k = 0; k < results.length; k++) {
        if (results[k].status === 'filled') filledCount++;
    }
    if (filledCount === 0) {
        return JSON.stringify({status: 'no_filled', rows: results, message: '所有条件均未成功填入'});
    }
    return JSON.stringify({status: 'ready', filled: filledCount, rows: results});
    """

    try:
        raw = driver.execute_script(set_and_submit_js, conditions, _ADV_FIELD_MAP)
        info = json.loads(raw) if raw else {}
        _log(f"[cnki-adv] 表单填写结果: {json.dumps(info, ensure_ascii=False)}")

        if info.get("status") != "ready":
            _log(f"[cnki-adv] 表单填写失败: {info}")
            return False

        time.sleep(0.5)

        btn_selectors = [
            (By.CSS_SELECTOR, "input.btn-search, button.btn-search"),
            (By.CSS_SELECTOR, ".search-btn"),
            (By.XPATH, "//input[@type='button' and contains(@value,'检索')]"),
            (By.XPATH, "//button[contains(text(),'检索')]"),
        ]
        for by, selector in btn_selectors:
            try:
                btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((by, selector))
                )
                btn.click()
                _log("[cnki-adv] 已点击检索按钮")
                return True
            except (TimeoutException, NoSuchElementException):
                continue

        _log("[cnki-adv] 未找到检索按钮，尝试 Enter 键提交")
        rows = driver.find_elements(By.CSS_SELECTOR, "input[type='text']")
        if rows:
            rows[-1].send_keys(Keys.RETURN)
            return True

        return False

    except Exception as e:
        _log(f"[cnki-adv] 高级搜索失败: {e}")
        return False


def _set_year_filter(driver, year_from: int = None, year_to: int = None):
    """通过侧边栏「年度」分组中的 checkbox 筛选年份。"""
    if year_from is None and year_to is None:
        return

    current_year = time.localtime().tm_year
    y_from = year_from or 1900
    y_to = year_to or current_year

    try:
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "dt.tit"))
            )
        except TimeoutException:
            _log("[cnki] 侧边栏未加载，将依赖客户端过滤")
            return

        expand_js = """
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
        """
        expand_result = driver.execute_script(expand_js)
        _log(f"[cnki] 年度面板展开: {expand_result}")

        if expand_result == "not_found":
            _log("[cnki] 侧边栏未找到「年度」分组，将依赖客户端过滤")
            return

        if expand_result == "expanded":
            time.sleep(2)

        click_js = """
        var yFrom = arguments[0], yTo = arguments[1];
        var dts = document.querySelectorAll('dt.tit');
        var targetDl = null;
        for (var i = 0; i < dts.length; i++) {
            if ((dts[i].textContent || '').indexOf('年度') >= 0) {
                targetDl = dts[i].closest('dl');
                break;
            }
        }
        if (!targetDl) return JSON.stringify({stage:'no_dl'});

        var items = targetDl.querySelectorAll('dd li');
        var clicked = [], values = [];
        for (var j = 0; j < items.length; j++) {
            var cb = items[j].querySelector('input[type=checkbox]');
            var a = items[j].querySelector('a');
            var txt = a ? a.textContent.trim() : items[j].textContent.trim();
            values.push(txt);
            var yearNum = parseInt(txt, 10);
            if (!isNaN(yearNum) && yearNum >= yFrom && yearNum <= yTo && cb) {
                cb.click();
                clicked.push(yearNum);
            }
        }
        return JSON.stringify({stage: clicked.length > 0 ? 'clicked' : 'no_match',
                               total: items.length, values: values.slice(0,10), clicked: clicked});
        """
        result_json = driver.execute_script(click_js, y_from, y_to)
        _log(f"[cnki] 年份筛选结果: {result_json}")

        if result_json:
            info = json.loads(result_json)
            if info.get("clicked"):
                _log(f"[cnki] 已勾选年份: {info['clicked']}")
                time.sleep(2)
                try:
                    WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, "table.result-table-list, .result-table-list"))
                    )
                except TimeoutException:
                    _log("[cnki] 年份筛选后等待结果超时，继续...")
                time.sleep(1)
            else:
                _log(f"[cnki] 年份范围 {y_from}-{y_to} 未匹配到选项 (可用: {info.get('values', [])})")
    except Exception as e:
        _log(f"[cnki] 年份筛选失败: {e}，将依赖客户端过滤")


_DOCTYPE_TAB_MAP = {
    "journal": ("学术期刊", "期刊"),
    "master": ("硕士",),
    "doctor": ("博士",),
    "thesis": ("学位", "学位论文"),
    "conference": ("会议",),
    "newspaper": ("报纸",),
}


def _switch_to_doc_type(driver, doc_type: str) -> bool:
    """点击知网搜索结果顶部的文献类型标签。

    doc_type 可选: journal / master / doctor / thesis / conference / newspaper
    """
    keywords = _DOCTYPE_TAB_MAP.get(doc_type)
    if not keywords:
        _log(f"[cnki] 未知文献类型: {doc_type}，可选: {list(_DOCTYPE_TAB_MAP.keys())}")
        return False

    switch_js = r"""
    var keywords = arguments[0];
    var tabs = document.querySelectorAll('.doctype-menus a[name="classify"], .doctype-menus a');
    var available = [];
    for (var i = 0; i < tabs.length; i++) {
        var t = (tabs[i].textContent || '').trim();
        available.push(t);
        for (var k = 0; k < keywords.length; k++) {
            if (t.indexOf(keywords[k]) >= 0) {
                tabs[i].click();
                return JSON.stringify({status: 'clicked', text: t,
                    classid: tabs[i].getAttribute('classid')});
            }
        }
    }
    return JSON.stringify({status: 'not_found', available: available});
    """
    try:
        raw = driver.execute_script(switch_js, list(keywords))
        info = json.loads(raw) if raw else {}
        _log(f"[cnki] 切换文献类型 '{doc_type}': {json.dumps(info, ensure_ascii=False)}")
        if info.get("status") == "clicked":
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "table.result-table-list, .result-table-list"))
                )
            except TimeoutException:
                _log("[cnki] 切换文献类型后等待结果超时，继续...")
            time.sleep(1)
            return True
        else:
            _log(f"[cnki] 未找到文献类型标签 '{doc_type}'，可用: {info.get('available', [])}")
    except Exception as e:
        _log(f"[cnki] 切换文献类型失败: {e}")
    return False


def _switch_to_journal_db(driver) -> bool:
    """点击「学术期刊」数据库标签（兼容旧调用）。"""
    return _switch_to_doc_type(driver, "journal")


def _parse_core_input(core: str) -> list[str]:
    """将逗号分隔的字符串拆分为选项列表，去重保序。

    Agent 负责将用户意图翻译为知网侧边栏的实际选项名（北大核心/CSSCI/AMI/WJCI/CSCD/EI），
    脚本只做机械拆分和点击，不做语义解析。
    """
    items = [s.strip() for s in core.split(",") if s.strip()]
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _set_core_filter(driver, core: str) -> bool:
    """设置核心期刊筛选——支持逗号分隔多选。

    通过 dl[groupid='LYBSM'] 定位侧边栏来源类别面板，逐个点击需要的选项。
    知网侧边栏为复选框，点击后页面刷新但面板保留，可继续点击下一个。
    """
    core_names = _parse_core_input(core)
    if not core_names:
        _log(f"[cnki] 核心期刊参数为空: {core}")
        return False

    unknown = [n for n in core_names if n not in CNKI_SIDEBAR_CORES]
    if unknown:
        _log(f"[cnki] 警告: 以下选项可能不在知网侧边栏中: {unknown}，"
             f"知网已知选项: {CNKI_SIDEBAR_CORES}（仍会尝试点击）")

    _log(f"[cnki] 核心期刊筛选目标: {core_names}")

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

    var dls = document.querySelectorAll('dl[groupid]');
    var gids = [];
    for (var k = 0; k < dls.length; k++) gids.push(dls[k].getAttribute('groupid'));
    return JSON.stringify({status:'no_lybsm', dl_groupids: gids});
    """

    clicked_any = False

    for name in core_names:
        success = False
        for attempt in range(4):
            if attempt > 0:
                wait = 2 + attempt * 3
                time.sleep(wait)
            try:
                raw = driver.execute_script(click_one_js, name)
                info = json.loads(raw) if raw else {}
                status = info.get("status", "error")

                if status == "clicked":
                    _log(f"[cnki] 已勾选: {name}")
                    try:
                        WebDriverWait(driver, 15).until(
                            EC.presence_of_element_located(
                                (By.CSS_SELECTOR,
                                 "table.result-table-list, .result-table-list"))
                        )
                    except TimeoutException:
                        pass
                    clicked_any = True
                    success = True
                    break
                elif status == "already_checked":
                    _log(f"[cnki] 已选中（跳过）: {name}")
                    success = True
                    break
                elif status == "no_lybsm":
                    _log("[cnki] 来源类别面板不存在")
                    break
                elif status == "type_not_found":
                    avail = info.get("available", [])
                    if avail:
                        _log(f"[cnki] LYBSM 中无 {name}，可用: {avail}")
                        break
                    _log(f"[cnki] LYBSM 面板链接未加载，等待重试 ({attempt+1}/4)...")
                    continue
            except Exception as e:
                _log(f"[cnki] 核心筛选脚本执行失败: {e}")
                break

        if not success:
            _log(f"[cnki] 核心期刊筛选未成功: {name}")

    if clicked_any:
        _log(f"[cnki] 核心期刊筛选完成")
    return clicked_any


def _set_sort(driver, sort: str):
    """设置排序方式（kns8s 排序栏 #orderList li）"""
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

    try:
        li = driver.find_element(By.CSS_SELECTOR, f"#orderList li#{sort_id}")
        if "cur" in (li.get_attribute("class") or ""):
            _log(f"[cnki] 排序已经是 {sort_text}，无需切换")
            return
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", li)
        time.sleep(0.3)
        li.click()
        _log(f"[cnki] 已点击排序: {sort_text} (#{sort_id})")
        time.sleep(1.5)
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "table.result-table-list, .result-table-list"))
            )
        except TimeoutException:
            _log("[cnki] 排序后等待结果超时，继续...")
        return
    except NoSuchElementException:
        pass

    try:
        li = driver.find_element(
            By.XPATH, f"//ul[@id='orderList']/li[contains(text(),'{sort_text}')]"
        )
        li.click()
        _log(f"[cnki] 已通过 XPath 点击排序: {sort_text}")
        time.sleep(1.5)
    except NoSuchElementException:
        _log(f"[cnki] 未找到排序选项: {sort_text}")


def _go_next_page(driver):
    """翻到下一页"""
    try:
        next_btn = driver.find_element(By.CSS_SELECTOR, "a.next, #PageNext, a[id='PageNext']")
        next_btn.click()
        time.sleep(2)
    except (NoSuchElementException, ElementClickInterceptedException):
        pass


# ── 结果解析 ──────────────────────────────────────────

def _parse_search_results(driver) -> list[dict]:
    """解析当前页的搜索结果"""
    results = []

    row_selectors = [
        "table.result-table-list tbody tr",
        ".result-table-list tbody tr",
        "#gridTable tbody tr",
    ]

    rows = []
    for selector in row_selectors:
        rows = driver.find_elements(By.CSS_SELECTOR, selector)
        if rows:
            break

    for row in rows:
        try:
            result = _parse_single_row(row)
            if result and result.get("title"):
                results.append(result)
        except Exception:
            continue

    return results


def _parse_single_row(row) -> dict:
    """解析单行搜索结果"""
    result = {
        "title": "",
        "authors": "",
        "journal": "",
        "date": "",
        "cited_by": 0,
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
    except NoSuchElementException:
        pass

    try:
        cite_el = row.find_element(By.CSS_SELECTOR, "td.quote, .quote, td.count")
        cite_text = cite_el.text.strip()
        if cite_text.isdigit():
            result["cited_by"] = int(cite_text)
    except (NoSuchElementException, ValueError):
        pass

    # 行内核心期刊标记（弱信号：CNKI 搜索结果页未必显示这些标签，
    # 但保留检测以便将来 CNKI 改版时自动生效）
    try:
        badge_els = row.find_elements(By.CSS_SELECTOR,
            "span[class*='icon'], em[class*='icon'], i[class*='icon'], .badge, .tag")
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
