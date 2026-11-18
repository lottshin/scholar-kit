import argparse
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config  # noqa: E402
import literature  # noqa: E402
from cnki import search as cnki_search  # noqa: E402
from cnki.search import build_cnki_expression  # noqa: E402


def test_build_cnki_expression_supports_tka_author_affiliation_journal_and_fund():
    expr = build_cnki_expression(
        keyword="乡村振兴",
        field="篇关摘",
        author="张三",
        first_author="李四",
        affiliation="清华大学",
        journal="《中国社会科学》",
        fund="国家社科基金",
    )
    assert expr == (
        "TKA='乡村振兴' AND AU='张三' AND FI='李四' "
        "AND AF % '清华大学' AND LY='中国社会科学' AND FU % '国家社科基金'"
    )


def test_build_cnki_expression_journal_without_book_marks_uses_fuzzy_and_sanitizes_quotes():
    expr = build_cnki_expression(
        keyword="China's economy",
        journal="清华大学学报",
        fuzzy=True,
    )
    assert expr == "SU % 'China s economy' AND LY % '清华大学学报'"


def test_build_cnki_expression_combines_custom_expr_and_rejects_invalid_ye_operator():
    combined = build_cnki_expression(
        expr="TKA=('人工智能' + '大模型') AND TKA=('治理' + '监管')",
        journal="管理世界",
    )
    assert combined == "(TKA=('人工智能' + '大模型') AND TKA=('治理' + '监管')) AND LY % '管理世界'"

    # 兼容转换 AF%='...' 为 KNS8S 标准 AF % '...'
    compat = build_cnki_expression(expr="AF%='北京大学' AND TI='数字经济'")
    assert compat == "AF % '北京大学' AND TI='数字经济'"

    with pytest.raises(ValueError, match="YE"):
        build_cnki_expression(expr="SU='数字经济' AND YE>=2022")

    with pytest.raises(ValueError, match="缺少字段代码"):
        build_cnki_expression(expr="TKA=('数字经济' + '数据要素') AND ('乡村振兴' + '县域治理')")

    with pytest.raises(ValueError, match="单引号"):
        build_cnki_expression(expr="SU='未闭合")


class FakeAdvDriver:
    def __init__(self, *, major_ok=True, grade_ok=True, syntax_err=""):
        self.major_ok = major_ok
        self.grade_ok = grade_ok
        self.syntax_err = syntax_err
        self.current_url = "https://kns.cnki.net/kns8s/AdvSearch"
        self.title = "高级检索-中国知网"
        self.visited = []
        self.quit_called = False
        self.scripts = []

    def get(self, url):
        self.visited.append(url)
        self.current_url = url

    def get_cookies(self):
        return []

    def quit(self):
        self.quit_called = True

    def find_elements(self, by, selector):
        class FakeBtn:
            def is_displayed(self):
                return True

            def is_enabled(self):
                return True

            def click(self):
                return None

        if "btn-search" in selector or "检索" in selector:
            return [FakeBtn()]
        return []

    def execute_script(self, script, *args):
        self.scripts.append((script, args))
        if "no_textarea" in script:
            if self.major_ok:
                return json.dumps({"status": "filled", "switched": True, "valueLen": len(args[0])})
            return json.dumps({"status": "no_textarea", "switched": False})
        if "markers = ['语法'" in script:
            return self.syntax_err
        if "insufficient_rows" in script:
            if self.grade_ok:
                return json.dumps({"status": "ready", "filled": len(args[0])})
            return json.dumps({"status": "insufficient_rows", "have": 0, "need": len(args[0])})
        if "return visible;" in script:
            return 3
        if "clickedTop" in script:
            return json.dumps({"clickedTop": True, "subType": args[1]})
        if "totalRequested" in script:
            return json.dumps({"matched": args[0], "totalRequested": len(args[0])})
        if "mode: 'datebox'" in script:
            return json.dumps({"ok": True, "mode": "datebox"})
        if "抱歉，暂无数据" in script:
            return True
        return ""


def test_advanced_search_prefers_major_falls_back_to_grade_and_never_silently_drops_filters(monkeypatch):
    # 1. majorSearch 不可用时自动降级到新版 #gradetxt 多行表单
    drv_fallback = FakeAdvDriver(major_ok=False, grade_ok=True)
    res_fb = cnki_search._advanced_search(
        drv_fallback,
        [("AU", "张三", False), ("LY", "管理世界", True)],
        expression="AU='张三' AND LY%='管理世界'",
    )
    assert res_fb["ok"] is True
    assert res_fb["engine"] == "gradeSearch"

    # 2. 两个高级引擎都失败时，search_cnki 必须返回明确错误码，绝不静默退回普通全库搜索
    drv_fail = FakeAdvDriver(major_ok=False, grade_ok=False)
    monkeypatch.setattr(cnki_search, "check_cnki_access", lambda: (True, "ok"))
    monkeypatch.setattr(cnki_search, "_detect_browser", lambda: "chrome")
    monkeypatch.setattr(cnki_search, "_create_driver", lambda browser=None: drv_fail)
    monkeypatch.setattr(cnki_search, "_load_cookies", lambda d: None)
    monkeypatch.setattr(cnki_search, "_save_cookies", lambda d: None)
    monkeypatch.setattr(cnki_search, "_handle_captcha", lambda d: d)
    monkeypatch.setattr(cnki_search, "REQUEST_INTERVAL", 0)

    out = cnki_search.search_cnki(keyword="乡村振兴", journal="中国社会科学")
    assert len(out) == 1
    assert out[0]["status"] == "error"
    assert out[0]["code"] == "CNKI_ADV_SEARCH_FAILED"
    assert "dom_diagnosis_file" in out[0]
    assert Path(out[0]["dom_diagnosis_file"]).exists()
    assert cnki_search.CNKI_SEARCH_URL not in drv_fail.visited


def test_cnki_selectors_registry_and_dom_diagnosis_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "gh"))
    config.reset()

    from cnki.selectors import CNKI_SELECTORS, save_adv_dom_diagnosis

    assert "adv_search" in CNKI_SELECTORS
    assert "major_textarea" in CNKI_SELECTORS["adv_search"]
    assert "detail" in CNKI_SELECTORS
    assert "download" in CNKI_SELECTORS

    drv = FakeAdvDriver(major_ok=False, grade_ok=False)
    diag_path = save_adv_dom_diagnosis(drv, reason="unit_test_failure")
    assert diag_path is not None
    saved = json.loads(Path(diag_path).read_text(encoding="utf-8"))
    assert saved["reason"] == "unit_test_failure"
    assert saved["url"] == "https://kns.cnki.net/kns8s/AdvSearch"


def test_cli_search_allows_omitting_positional_query_when_author_or_expr_given(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "gh"))
    config.reset()

    captured = {}

    def fake_search_cnki(**kwargs):
        captured.update(kwargs)
        return [{
            "title": "数字经济与县域治理",
            "authors": "张三",
            "journal": "管理世界",
            "year": 2025,
            "url": "https://kns.cnki.net/kcms2/article/abstract?v=1",
            "source": "CNKI",
        }]

    monkeypatch.setattr(literature, "search_cnki", fake_search_cnki)
    monkeypatch.setattr(sys, "argv", [
        "literature.py", "search",
        "--expr", "TKA=('数字经济' + '数据要素')",
        "--affiliation", "北京大学",
        "--core", "CSSCI",
    ])

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        literature.main()

    payload = json.loads(out.getvalue())
    assert payload["status"] == "success"
    assert captured["keyword"] == ""
    assert captured["expr"] == "TKA=('数字经济' + '数据要素')"
    assert captured["affiliation"] == "北京大学"
    assert captured["core"] == "CSSCI"
