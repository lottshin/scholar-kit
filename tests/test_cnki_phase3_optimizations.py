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
import evidence  # noqa: E402
import formatter  # noqa: E402
import literature  # noqa: E402
import search  # noqa: E402
from cnki import detail as cnki_detail  # noqa: E402
from cnki import download as cnki_download  # noqa: E402
from cnki import driver as cnki_driver  # noqa: E402


def test_cnki_quality_score_is_calibrated_against_english_preprints():
    cssci_paper = {
        "title": "数字经济、创业活跃度与高质量发展",
        "journal": "管理世界",
        "year": 2020,
        "cited_by": 3482,
        "downloads": 92000,
        "is_core": True,
        "core_type": "北大核心;CSSCI",
        "source": "CNKI",
    }
    arxiv_preprint = {
        "title": "A New Preprint Without Citations",
        "abstract": "x" * 520,
        "doi": "10.48550/arXiv.2501.00001",
        "keywords": ["LLM"],
        "is_oa": True,
        "cited_by": 0,
        "year": 2025,
        "source": "arXiv",
    }
    non_core_cnki = {
        "title": "地方数字政务探索",
        "journal": "某普通月刊",
        "year": 2024,
        "cited_by": 0,
        "downloads": 18,
        "is_core": False,
        "source": "CNKI",
    }

    cssci_score = search.calculate_quality_score(cssci_paper)
    arxiv_score = search.calculate_quality_score(arxiv_preprint)
    non_core_score = search.calculate_quality_score(non_core_cnki)

    assert cssci_score >= 80.0
    assert cssci_score > arxiv_score
    assert cssci_score > non_core_score + 40.0


def test_gbt7714_formats_cnki_online_first_as_j_ol():
    online_first = {
        "authors": "王雪; 李明",
        "title": "生成式人工智能赋能乡村治理的机制研究",
        "journal": "中国行政管理",
        "date": "2026-11-09 17:33",
        "year": 2026,
        "url": "https://kns.cnki.net/kcms2/article/abstract?v=abc123",
        "doc_type": "journal",
        "is_online_first": True,
    }
    ref = formatter.format_gbt7714(online_first, 1)
    assert "[J/OL]" in ref
    assert "中国行政管理(2026-11-09)" in ref
    assert "https://kns.cnki.net/kcms2/article/abstract?v=abc123" in ref


def test_stable_fulltext_cache_survives_ephemeral_v_token_rotation(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "global_home"))
    config.reset()

    paper_v1 = {
        "title": "数字经济与高质量发展",
        "authors": "赵涛; 张智",
        "year": 2020,
        "cnki_id": "CJFQ:GLSJ202010005",
    }
    url_v1 = "https://kns.cnki.net/kcms2/article/abstract?v=OLD_TOKEN_111"
    url_v2 = "https://kns.cnki.net/kcms2/article/abstract?v=NEW_TOKEN_999"

    cnki_detail._save_cached_fulltext(
        url_v1,
        {"abstract": "摘要内容", "keywords": ["数字经济"], "fulltext": "正文内容" * 80},
        paper=paper_v1,
    )

    # 即使新会话里 v= token 完全变化、且尚未获取 cnki_id，也能通过归一化题名+作者+年份或 cnki_id 命中缓存
    paper_v2_with_id = {"title": "数字经济与高质量发展", "cnki_id": "CJFQ:GLSJ202010005"}
    hit_by_id = cnki_detail._load_cached_fulltext(url_v2, paper=paper_v2_with_id)
    assert hit_by_id is not None
    assert "正文内容" in hit_by_id["fulltext"]

    # 测试无 cnki_id 时按 title|first_author|year 稳定键命中
    paper_no_id = {"title": "乡村数字治理转型研究", "authors": "陈晓红; 李四", "year": 2024}
    cnki_detail._save_cached_fulltext(
        "https://kns.cnki.net/kcms2/article/abstract?v=TOKEN_A",
        {"abstract": "摘要B", "fulltext": "乡村治理全文" * 60},
        paper=paper_no_id,
    )
    hit_by_meta = cnki_detail._load_cached_fulltext(
        "https://kns.cnki.net/kcms2/article/abstract?v=TOKEN_B",
        paper={"title": "乡村数字治理转型研究", "authors": "陈晓红", "date": "2024-05-10"},
    )
    assert hit_by_meta is not None
    assert "乡村治理全文" in hit_by_meta["fulltext"]


def test_local_pdf_fastpath_extracts_fulltext_without_opening_browser(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "global_home"))
    config.reset()

    pdf_dir = tmp_path / "papers" / "pdf"
    pdf_dir.mkdir(parents=True)
    (pdf_dir / "数字经济驱动产业升级机制研究_张三.pdf").write_bytes(b"%PDF-1.4 mock")

    class FakePage:
        def extract_text(self):
            return "数字经济通过提升资源配置效率与技术创新能力显著促进产业结构升级。" * 10

    class FakePDF:
        pages = [FakePage(), FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pdfplumber":
            return SimpleNamespace(open=lambda _p: FakePDF())
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    browser_called = []
    monkeypatch.setattr(cnki_detail, "_create_driver", lambda *a, **kw: browser_called.append(1))

    papers = [{
        "title": "数字经济驱动产业升级机制研究",
        "abstract": "已有摘要",
        "url": "https://kns.cnki.net/kcms2/article/abstract?v=xyz",
        "source": "CNKI",
    }]
    enriched = cnki_detail.batch_read_detail(papers, top_n=1, fulltext=True)

    assert browser_called == []
    assert enriched[0]["has_fulltext"] is True
    assert "[第 1 页]" in enriched[0]["fulltext"]


def test_parse_cnki_citation_item_and_cli_routing(monkeypatch):
    parsed_j = cnki_detail.parse_cnki_citation_item(
        "[1] 现实需要如何推动经济学在中国的发展[J]. 黄少安.经济学动态,2021(05)",
        "https://kns.cnki.net/kcms2/article/abstract?v=cit1",
    )
    assert parsed_j["title"] == "现实需要如何推动经济学在中国的发展"
    assert parsed_j["doc_tag"] == "J"
    assert parsed_j["authors"] == "黄少安"
    assert parsed_j["journal"] == "经济学动态"
    assert parsed_j["year"] == 2021
    assert parsed_j["issue"] == "5"

    parsed_d = cnki_detail.parse_cnki_citation_item(
        "[2] 数字普惠金融对城乡收入差距的影响研究[D]. 李明.中央财经大学,2023",
        "https://kns.cnki.net/kcms2/article/abstract?v=cit2",
    )
    assert parsed_d["doc_tag"] == "D"
    assert parsed_d["journal"] == "中央财经大学"
    assert parsed_d["year"] == 2023

    cnki_calls = []
    monkeypatch.setattr(
        literature,
        "get_cnki_citations",
        lambda pid, direction="both", limit=20: cnki_calls.append((pid, direction, limit)) or {
            "status": "success",
            "source": "CNKI",
            "paper": {"title": pid},
            "citing_count": 1,
            "references_count": 1,
            "citing": [parsed_j],
            "references": [parsed_d],
        },
    )

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        literature.cmd_citations(argparse.Namespace(
            paper_id="数字经济、创业活跃度与高质量发展",
            direction="both",
            limit=5,
        ))
    payload = json.loads(out.getvalue())
    assert cnki_calls == [("数字经济、创业活跃度与高质量发展", "both", 5)]
    assert payload["source"] == "CNKI"
    assert payload["citing_count"] == 1


def test_cmd_cite_uses_single_batch_read_detail_and_persists_session(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "global_home"))
    config.reset()

    evidence._save_session([
        {
            "title": "论文一",
            "authors": "甲",
            "journal": "中国社会科学",
            "year": 2023,
            "url": "https://kns.cnki.net/kcms2/article/abstract?v=1",
            "source": "CNKI",
        },
        {
            "title": "论文二",
            "authors": "乙",
            "journal": "管理世界",
            "year": 2024,
            "url": "https://kns.cnki.net/kcms2/article/abstract?v=2",
            "source": "CNKI",
        },
        {
            "title": "网络首发论文",
            "authors": "丙",
            "journal": "经济研究",
            "date": "2026-11-10 09:30",
            "year": 2026,
            "url": "https://kns.cnki.net/kcms2/article/abstract?v=3",
            "is_online_first": True,
            "source": "CNKI",
        },
    ])

    batch_calls = []

    def fake_batch_read(papers, top_n=5, fulltext=False):
        batch_calls.append(len(papers))
        for idx, p in enumerate(papers, 1):
            p["issue"] = str(idx)
            p["pages"] = f"{idx * 10}-{idx * 10 + 12}"
        return papers

    monkeypatch.setattr(literature, "batch_read_detail", fake_batch_read)
    monkeypatch.setattr(literature, "get_detail", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("Should not call get_detail in loop")))

    out1 = io.StringIO()
    with redirect_stdout(out1), redirect_stderr(io.StringIO()):
        literature.cmd_cite(argparse.Namespace(style="gbt7714", raw=False, project=None))
    res1 = json.loads(out1.getvalue())

    # 只对前 2 篇非网络首发缺页码论文发起 1 次单会话批量补全，且网络首发论文不误报缺页码警告
    assert batch_calls == [2]
    assert "incomplete_indices" not in res1
    assert "[J/OL]" in res1["references"]

    # 第二次调用 cite（例如切换 APA），因已写回 session.json，零次浏览器调用！
    out2 = io.StringIO()
    with redirect_stdout(out2), redirect_stderr(io.StringIO()):
        literature.cmd_cite(argparse.Namespace(style="apa", raw=False, project=None))
    assert batch_calls == [2]


def test_refworks_export_parses_abstract_keywords_and_institutions():
    sample = """RT Journal Article
T1 数字经济赋能新质生产力研究
A1 张三
A1 李四
JF 管理世界
YR 2025
VO 41
IS 03
SP 15
OP 32
K1 数字经济;新质生产力;高质量发展
AB 本文系统考察数字经济赋能新质生产力的内在机制与实证效应。
AD 清华大学经济管理学院;北京大学国家发展研究院
DO 10.19744/j.cnki.11-1235/f.2025.0030
ER
"""
    parsed = cnki_download._parse_refworks_export(sample)
    assert len(parsed) == 1
    item = parsed[0]
    assert item["pages"] == "15-32"
    assert item["abstract"].startswith("本文系统考察")
    assert item["keywords"] == ["数字经济", "新质生产力", "高质量发展"]
    assert len(item["institutions"]) == 2


def test_release_driver_keeps_reused_browser_alive_and_quits_owned_driver(monkeypatch):
    monkeypatch.setattr(cnki_driver, "_save_cookies", lambda _d: None)

    stopped = []
    quit_called = []

    class ReusedDriver:
        _scholar_reused = True
        window_handles = ["h1", "h2"]
        service = SimpleNamespace(stop=lambda: stopped.append(True))

        def __init__(self):
            self.closed_tabs = []

        @property
        def switch_to(self):
            return SimpleNamespace(window=lambda h: setattr(self, "_cur", h))

        def close(self):
            self.closed_tabs.append(getattr(self, "_cur", ""))

        def quit(self):
            quit_called.append(True)

    d = ReusedDriver()
    cnki_driver._release_driver(d)
    assert d.closed_tabs == ["h2"]
    assert stopped == [True]
    assert quit_called == []


def test_find_local_driver_prefers_highest_cached_version(monkeypatch, tmp_path):
    se_cache = tmp_path / "selenium"
    bin_name = "chromedriver.exe" if sys.platform == "win32" else "chromedriver"
    old_drv = se_cache / "chromedriver" / "mac-arm64" / "155.0.8059.84" / bin_name
    new_drv = se_cache / "chromedriver" / "mac-arm64" / "157.0.8103.79" / bin_name
    old_drv.parent.mkdir(parents=True)
    new_drv.parent.mkdir(parents=True)
    old_drv.write_text("old", encoding="utf-8")
    new_drv.write_text("new", encoding="utf-8")

    monkeypatch.setenv("SE_CACHE_PATH", str(se_cache))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty_localappdata"))
    monkeypatch.delenv("SCHOLAR_DRIVER_PATH", raising=False)
    monkeypatch.setattr(cnki_driver.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cnki_driver.Path, "home", lambda: tmp_path / "empty_home")

    chosen = cnki_driver._find_local_driver("chrome")
    assert chosen == str(new_drv)

