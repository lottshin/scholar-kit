import asyncio
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import search  # noqa: E402
import search_async  # noqa: E402


def test_semantic_alias_uses_semantic_scholar_fallback_chain(monkeypatch):
    monkeypatch.setattr(search, "search_semantic_scholar", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        search,
        "search_openalex",
        lambda *args, **kwargs: [{"title": "fallback result", "source": "OpenAlex"}],
    )

    result = search.search_with_fallback("graph neural networks", "semantic", limit=1)

    assert result["fallback"] is True
    assert result["source"] == "openalex"
    assert result["original_source"] == "semantic"
    assert result["results"][0]["source"] == "OpenAlex"


@pytest.mark.parametrize(
    ("source_name", "function_name", "paper_source"),
    [
        ("nssd", "search_nssd", "NSSD"),
        ("dblp", "search_dblp", "DBLP"),
        ("base", "search_base", "BASE"),
    ],
)
def test_fallback_dispatch_uses_source_specific_signatures(
    monkeypatch, source_name, function_name, paper_source
):
    def primary(query, limit=10, year_from=None, year_to=None):
        return [{"title": f"{paper_source} paper", "source": paper_source}]

    monkeypatch.setattr(search, function_name, primary)

    result = search.search_with_fallback(
        "topic",
        source_name,
        limit=1,
        sort="date",
        field="title",
        page=2,
    )

    assert result == {
        "source": source_name,
        "results": [{"title": f"{paper_source} paper", "source": paper_source}],
        "fallback": False,
    }


def test_search_all_includes_dblp_and_base(monkeypatch):
    def source_result(source_name):
        return [{"title": f"{source_name} title", "source": source_name, "cited_by": 0}]

    monkeypatch.setattr(search.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(search, "search_openalex", lambda *args, **kwargs: source_result("OpenAlex"))
    monkeypatch.setattr(search, "search_semantic_scholar", lambda *args, **kwargs: source_result("Semantic Scholar"))
    monkeypatch.setattr(search, "search_arxiv", lambda *args, **kwargs: source_result("arXiv"))
    monkeypatch.setattr(search, "search_nssd", lambda *args, **kwargs: source_result("NSSD"))
    monkeypatch.setattr(search, "search_dblp", lambda *args, **kwargs: source_result("DBLP"))
    monkeypatch.setattr(search, "search_base", lambda *args, **kwargs: source_result("BASE"))

    results = search.search_all("topic", limit=3)

    assert {paper["source"] for paper in results} == {
        "OpenAlex",
        "Semantic Scholar",
        "arXiv",
        "NSSD",
        "DBLP",
        "BASE",
    }


def test_async_search_all_includes_base_by_default(monkeypatch):
    def async_source(source_name):
        async def _run(*args, **kwargs):
            return [{"title": f"{source_name} title", "source": source_name}]

        return _run

    monkeypatch.setattr(search_async, "search_openalex_async", async_source("OpenAlex"))
    monkeypatch.setattr(search_async, "search_semantic_scholar_async", async_source("Semantic Scholar"))
    monkeypatch.setattr(search_async, "search_arxiv_async", async_source("arXiv"))
    monkeypatch.setattr(search_async, "search_nssd_async", async_source("NSSD"))
    monkeypatch.setattr(search_async, "search_dblp_async", async_source("DBLP"))
    monkeypatch.setattr(search_async, "search_base_async", async_source("BASE"), raising=False)

    result = asyncio.run(search_async.search_all_async("topic", limit=1))

    assert "base" in result["sources_used"]
    assert any(paper["source"] == "BASE" for paper in result["results"])
