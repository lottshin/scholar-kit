import argparse
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config  # noqa: E402
import docx_tools  # noqa: E402
import evidence  # noqa: E402
import literature  # noqa: E402
import search  # noqa: E402
import search_async  # noqa: E402
from cnki import driver as cnki_driver  # noqa: E402


def _make_search_args(**overrides):
    defaults = {
        "query": "graph neural network",
        "source": "all",
        "limit": 5,
        "core": None,
        "doc_type": None,
        "field": None,
        "year_from": None,
        "year_to": None,
        "author": None,
        "journal": None,
        "author_filter": None,
        "journal_filter": None,
        "field_of_study": None,
        "page": 1,
        "sort": "relevance",
        "pages": 1,
        "enable_fallback": False,
        "async_search": False,
        "export": None,
        "output": None,
        "download": False,
        "enrich": 0,
        "cite_enrich": 0,
        "append": False,
        "project": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_search_async_all_does_not_reinvoke_sync_sources(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "global_home"))
    config.reset()

    monkeypatch.setattr(
        literature,
        "search_cnki",
        lambda **kwargs: [{"status": "error", "code": "CNKI_UNREACHABLE", "message": "offline"}],
    )

    sync_calls = []
    for fn_name in (
        "search_openalex",
        "search_semantic_scholar",
        "search_arxiv",
        "search_nssd",
        "search_dblp",
        "search_base",
    ):
        monkeypatch.setattr(
            literature,
            fn_name,
            lambda *a, _n=fn_name, **kw: sync_calls.append(_n) or [],
        )

    async_calls = []

    def fake_search_all_sync(**kwargs):
        async_calls.append(kwargs)
        return {
            "results": [{"title": "Async Paper", "source": "OpenAlex", "year": 2025, "doi": "10.1000/async"}],
            "sources_used": ["openalex"],
            "elapsed_ms": 12,
        }

    monkeypatch.setattr(search_async, "search_all_sync", fake_search_all_sync)

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        literature.cmd_search(_make_search_args(source="all", async_search=True, author_filter="Hinton", page=2))

    payload = json.loads(out.getvalue())
    assert len(async_calls) == 1
    assert async_calls[0]["author"] == "Hinton"
    assert async_calls[0]["page"] == 2
    assert sync_calls == [], f"Sync sources should not run after async_search: {sync_calls}"
    assert payload["count"] == 1
    assert payload["results"][0]["title"] == "Async Paper"


def test_cmd_search_enable_fallback_does_not_double_call_primary(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "global_home"))
    config.reset()

    direct_s2_calls = []
    monkeypatch.setattr(
        literature,
        "search_semantic_scholar",
        lambda *a, **kw: direct_s2_calls.append(1) or [],
    )
    monkeypatch.setattr(
        literature,
        "search_with_fallback",
        lambda **kwargs: {
            "source": "openalex",
            "original_source": "semantic",
            "fallback": True,
            "results": [{"title": "Fallback Paper", "source": "OpenAlex", "year": 2024}],
        },
    )

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        literature.cmd_search(_make_search_args(source="semantic", enable_fallback=True))

    payload = json.loads(out.getvalue())
    assert direct_s2_calls == []
    assert payload["status"] == "success"
    assert payload["results"][0]["title"] == "Fallback Paper"


def test_search_async_errors_go_to_stderr_not_stdout(monkeypatch):
    async def failing_source(*args, **kwargs):
        raise RuntimeError("boom 429")

    async def ok_source(*args, **kwargs):
        return [{"title": "Clean JSON Paper", "source": "arXiv"}]

    monkeypatch.setattr(search_async, "search_openalex_async", failing_source)
    monkeypatch.setattr(search_async, "search_arxiv_async", ok_source)

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        res = search_async.search_all_sync("llm", limit=2, sources=["openalex", "arxiv"])

    assert out.getvalue() == ""
    assert "boom 429" in err.getvalue()
    assert res["sources_used"] == ["arxiv"]
    assert res["results"][0]["title"] == "Clean JSON Paper"


def test_clean_cache_all_preserves_browser_profile_projects_and_protected_files(monkeypatch, tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    gh = tmp_path / "global_home"
    monkeypatch.chdir(ws)
    monkeypatch.setenv("SCHOLAR_HOME", str(gh))
    monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED", raising=False)
    config.reset()

    # 全局登录态与待清理缓存
    profile_db = gh / "browser-profile" / "Default" / "Cookies"
    profile_db.parent.mkdir(parents=True)
    profile_db.write_text("sqlite-cookie-db", encoding="utf-8")
    (gh / "cookies.json").write_text("[]", encoding="utf-8")
    (gh / "config.json").write_text('{"request_interval": 5}', encoding="utf-8")
    api_cache = gh / "cache" / "openalex_123.json"
    api_cache.parent.mkdir(parents=True)
    api_cache.write_text('{"_cached_at": "2026-01-01T00:00:00", "results": []}', encoding="utf-8")

    # 工作区会话与课题文献库
    evidence._save_session([{"title": "Workspace Paper"}], project="my_topic")
    evidence._save_session([{"title": "Default Session"}])

    out = io.StringIO()
    with redirect_stdout(out):
        literature.cmd_clean_cache(argparse.Namespace(clean_all=True, dry_run=False))

    report = json.loads(out.getvalue())
    assert report["status"] == "success"
    assert report["deleted"] >= 1
    assert not api_cache.exists()
    assert profile_db.exists(), "browser-profile must never be deleted by clean-cache --all"
    assert (gh / "cookies.json").exists()
    assert (gh / "config.json").exists()
    assert (ws / ".scholar-kit" / "session.json").exists()
    assert (ws / ".scholar-kit" / "projects" / "my_topic" / "session.json").exists()
    assert (ws / ".scholar-kit" / ".gitignore").read_text(encoding="utf-8").strip() == "*"


def test_user_agent_is_not_overridden_in_headed_mode_and_matches_os_in_headless(monkeypatch):
    assert cnki_driver._build_user_agent("chrome", headless=False) is None
    assert cnki_driver._build_user_agent("edge", headless=False) is None

    monkeypatch.setattr(cnki_driver.sys, "platform", "darwin")
    mac_chrome_ua = cnki_driver._build_user_agent("chrome", headless=True)
    assert "Macintosh" in mac_chrome_ua
    assert "Edg/" not in mac_chrome_ua

    mac_edge_ua = cnki_driver._build_user_agent("edge", headless=True)
    assert "Macintosh" in mac_edge_ua
    assert "Edg/" in mac_edge_ua


def test_review_clusters_are_domain_agnostic_and_do_not_misclassify_letter_x():
    cs_papers = [
        {
            "title": "FlashAttention: Fast and Memory-Efficient Exact Attention",
            "abstract": "We optimize matrix multiplication and IO complexity on GPUs.",
            "keywords": ["Transformer", "CUDA", "Attention"],
            "source": "arXiv",
            "year": 2024,
        },
        {
            "title": "Quantized Matrix Multiplication for Large Transformer Models",
            "abstract": "Experiments on next-token prediction show 2x speedup.",
            "keywords": ["Transformer", "Quantization"],
            "source": "OpenAlex",
            "year": 2025,
        },
    ]
    selected = evidence._select_review_papers(cs_papers, "Transformer attention", limit=10)
    clusters = evidence._review_clusters(selected, "Transformer attention")
    labels = [c["label"] for c in clusters]

    assert "平台机制与社交媒体" not in labels
    assert "国家形象建构与官方叙事" not in labels
    assert any("transformer" in lbl.lower() or "attention" in lbl.lower() for lbl in labels)


def test_pdf_meta_supports_pdfplumber_fallback_when_pypdf_missing(monkeypatch, tmp_path):
    pdf_file = tmp_path / "sample.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy")

    class FakePage:
        def extract_text(self):
            return "DOI: 10.1038/s41586-024-00001-x"

    class FakePDF:
        metadata = {"Title": "Fallback PDF Title", "Author": "Alice; Bob"}
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("no pypdf")
        if name == "pdfplumber":
            return SimpleNamespace(open=lambda _p: FakePDF())
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    out = io.StringIO()
    docx_tools.cmd_pdf_meta(
        argparse.Namespace(filepath=str(pdf_file)),
        output_fn=lambda d: out.write(json.dumps(d, ensure_ascii=False)),
        crossref_resolver=lambda doi: {"journal": "Nature", "doi": doi},
    )
    data = json.loads(out.getvalue())
    assert data["status"] == "success"
    assert data["title"] == "Fallback PDF Title"
    assert data["doi"] == "10.1038/s41586-024-00001-x"
    assert data["crossref"]["journal"] == "Nature"
