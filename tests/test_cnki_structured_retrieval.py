from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config  # noqa: E402
import literature  # noqa: E402
from cnki import driver as cnki_driver  # noqa: E402
from cnki import search as cnki_search  # noqa: E402
from cnki.query import (  # noqa: E402
    ERR_INVALID_EXPRESSION,
    ERR_TOO_MANY_CONDITIONS,
    CnkiCondition,
    CnkiQueryError,
    compile_conditions_to_expression,
    join_terms,
    parse_condition,
    validate_conditions,
    validate_professional_expression,
    validate_value_syntax,
)


def test_parse_and_compile_structured_conditions_with_parentheses():
    assert parse_condition("篇名|乡村振兴").field == "篇名"
    assert parse_condition("来源|中国社会科学").field == "文献来源"
    assert parse_condition("机构|清华大学").field == "作者单位"

    expr = compile_conditions_to_expression([
        CnkiCondition(field="篇名", value="生成式AI"),
        CnkiCondition(field="摘要", value="品牌信任", logic="或者"),
        CnkiCondition(field="关键词", value="广告", logic="并且"),
    ])
    assert expr == "(TI % 生成式AI OR AB % 品牌信任) AND KY % 广告"

    not_expr = compile_conditions_to_expression([
        CnkiCondition(field="篇名", value="大数据"),
        CnkiCondition(field="篇名", value="大数据集", logic="不含"),
    ])
    assert not_expr == "(TI % 大数据 NOT TI % 大数据集)"


def test_condition_validation_limits_and_theme_match_rules():
    with pytest.raises(CnkiQueryError) as exc_many:
        validate_conditions([CnkiCondition(field="篇名", value="x") for _ in range(11)])
    assert exc_many.value.code == ERR_TOO_MANY_CONDITIONS

    with pytest.raises(CnkiQueryError) as exc_theme:
        validate_conditions([CnkiCondition(field="主题", value="人工智能", match="精确")])
    assert exc_theme.value.code == ERR_INVALID_EXPRESSION

    with pytest.raises(CnkiQueryError):
        validate_value_syntax("人工智能+AIGC")

    validate_value_syntax("人工智能 + AIGC")
    validate_value_syntax("COVID-19")
    assert join_terms(["digital library", "information service"], "*") == "'digital library' * 'information service'"
    assert validate_professional_expression("SU %= 知识管理 AND AU = 邱均平") == "SU %= 知识管理 AND AU = 邱均平"


def test_build_cnki_expression_preserves_same_field_boolean_operators():
    expr = cnki_search.build_cnki_expression(keyword="生成式AI + AIGC", field="主题")
    assert expr == "SU %= (生成式AI + AIGC)"
    assert "SU='+'" not in expr

    plain = cnki_search.build_cnki_expression(keyword="新质生产力 高质量发展", field="主题")
    assert plain == "SU='新质生产力' AND SU='高质量发展'"


def test_cross_language_readback_and_total_hits_helpers():
    class FakeHelperDriver:
        def __init__(self, checked=True, readback="SU %= 知识管理", hits="共找到 1,280 条结果"):
            self.checked = checked
            self.readback = readback
            self.hits = hits

        def execute_script(self, script, *args):
            if 'data-id="EN"' in script:
                want = bool(args[0])
                before = self.checked
                self.checked = want
                return json.dumps({"found": True, "before": before, "after": self.checked})
            if "textarea.textarea-major" in script:
                return self.readback
            if "pagerTitleCell" in script:
                return self.hits
            return ""

    drv = FakeHelperDriver(checked=True)
    assert cnki_search._set_cross_language(drv, False) is False
    assert cnki_search._verify_professional_expression(drv, "SU   %=  知识管理")["verified"] is True
    assert cnki_search._verify_professional_expression(drv, "SU %= 信息茧房")["verified"] is False
    assert cnki_search._read_total_hits(drv) == 1280


def test_ensure_local_proxy_bypass_preserves_existing_hosts(monkeypatch):
    if os.name != "nt":
        monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "example.edu")
    cnki_driver._ensure_local_proxy_bypass()
    no_proxy = os.environ.get("NO_PROXY", "")
    for host in ("example.edu", "127.0.0.1", "localhost", "::1"):
        assert host in no_proxy


def test_cmd_search_rejects_cnki_only_flags_on_api_source_and_echoes_retrieval(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "home"))
    config.reset()

    base_args = dict(
        query="digital",
        source="openalex",
        limit=5,
        core=None,
        exclude_warning=False,
        doc_type=None,
        field=None,
        year_from=None,
        year_to=None,
        author=None,
        first_author=None,
        affiliation=None,
        journal=None,
        fund=None,
        expr=None,
        professional=None,
        condition=["篇名|并且|模糊|人工智能"],
        match=None,
        cross_language=None,
        fuzzy=False,
        author_filter=None,
        journal_filter=None,
        field_of_study=None,
        page=1,
        sort="relevance",
        pages=1,
        per_page=None,
        bulk_abstract=False,
        no_bulk_abstract=True,
        enable_fallback=False,
        async_search=False,
        export=None,
        output=None,
        download=False,
        enrich=0,
        cite_enrich=0,
        append=False,
        project=None,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        literature.cmd_search(argparse.Namespace(**base_args))
    err = json.loads(buf.getvalue())
    assert err["status"] == "error"
    assert err["code"] == "CNKI_OPTION_SOURCE_MISMATCH"

    def fake_search_cnki(**kwargs):
        if kwargs.get("meta") is not None:
            kwargs["meta"]["retrieval"] = {
                "mode": "advanced",
                "expression": "TI % 人工智能",
                "verified": True,
                "total_hits": 42,
                "applied": {"cross_language": False},
            }
        return [{"title": "人工智能治理研究", "authors": "张三", "journal": "中国社会科学", "year": 2025, "source": "CNKI"}]

    monkeypatch.setattr(literature, "search_cnki", fake_search_cnki)
    cnki_args = dict(base_args, source="cnki", query="", cross_language="off")
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        literature.cmd_search(argparse.Namespace(**cnki_args))
    out = json.loads(buf2.getvalue())
    assert out["status"] == "success"
    assert out["retrieval"]["verified"] is True
    assert out["retrieval"]["total_hits"] == 42
    assert out["retrieval"]["applied"]["cross_language"] is False
