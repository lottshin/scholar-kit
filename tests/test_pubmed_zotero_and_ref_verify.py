import argparse
import io
import json
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config  # noqa: E402
import evidence  # noqa: E402
import formatter  # noqa: E402
import literature  # noqa: E402
import search  # noqa: E402
import zotero  # noqa: E402
from cnki import search as cnki_search  # noqa: E402


def test_openalex_and_pubmed_preserve_volume_issue_and_pages(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "gh"))
    config.reset()

    def fake_http_get(url, params=None, headers=None):
        if "openalex.org" in url:
            return {
                "results": [{
                    "title": "Attention Is All You Need",
                    "authorships": [{"author": {"display_name": "Ashish Vaswani"}}],
                    "publication_year": 2017,
                    "doi": "https://doi.org/10.5555/3295222.3295349",
                    "primary_location": {"source": {"display_name": "NeurIPS"}},
                    "biblio": {"volume": "30", "issue": "1", "first_page": "5998", "last_page": "6008"},
                    "cited_by_count": 120000,
                    "open_access": {"is_oa": True, "oa_url": "https://arxiv.org/pdf/1706.03762"},
                    "abstract_inverted_index": {"Transformer": [0], "architecture": [1]},
                }]
            }
        if "europepmc" in url:
            return {
                "resultList": {
                    "result": [{
                        "title": "CRISPR-Cas9 gene editing in clinical therapy.",
                        "authorString": "Doudna JA, Charpentier E.",
                        "pubYear": "2024",
                        "journalInfo": {"volume": "386", "issue": "4", "journal": {"title": "Science"}},
                        "pageInfo": "412-420",
                        "pmid": "39000001",
                        "pmcid": "PMC1100001",
                        "doi": "10.1126/science.test001",
                        "citedByCount": 85,
                        "isOpenAccess": "Y",
                        "abstractText": "Clinical advances in CRISPR-Cas9 genome editing.",
                        "keywordList": {"keyword": ["CRISPR", "Gene Editing"]},
                    }]
                }
            }
        return None

    monkeypatch.setattr(search, "_http_get", fake_http_get)

    oa = search.search_openalex("attention", limit=1)
    assert oa[0]["volume"] == "30"
    assert oa[0]["issue"] == "1"
    assert oa[0]["pages"] == "5998-6008"
    assert "5998-6008" in formatter.format_gbt7714(oa[0], 1)

    pm = search.search_pubmed("CRISPR", limit=1)
    assert pm[0]["source"] == "PubMed"
    assert pm[0]["pmid"] == "39000001"
    assert pm[0]["volume"] == "386"
    assert pm[0]["issue"] == "4"
    assert pm[0]["pages"] == "412-420"
    assert "412-420" in formatter.format_gbt7714(pm[0], 1)


def test_gbt7714_overrides_pageless_raw_string_when_pages_are_enriched():
    paper = {
        "authors": "赵涛; 张智; 梁上坤",
        "title": "数字经济、创业活跃度与高质量发展",
        "journal": "管理世界",
        "year": 2020,
        "volume": "36",
        "issue": "10",
        "pages": "65-76+188",
        # 模拟早期引用弹窗缺失页码的旧缓存字符串
        "gbt7714_raw": "[1] 赵涛, 张智, 梁上坤. 数字经济、创业活跃度与高质量发展[J]. 管理世界, 2020(10).",
        "doc_type": "journal",
    }
    formatted = formatter.format_gbt7714(paper, 1)
    assert "65-76+188" in formatted


def test_bibtex_generates_pure_ascii_unique_citekeys_and_read_paper_supports_tex(tmp_path):
    papers = [
        {"title": "数字经济与高质量发展", "authors": "赵涛; 张智", "year": 2020, "journal": "管理世界", "cnki_id": "CJFQ:GLSJ202010009"},
        {"title": "数字经济第二篇同作者同年", "authors": "赵涛", "year": 2020, "journal": "管理世界", "cnki_id": "CJFQ:GLSJ202011002"},
        {"title": "CRISPR-Cas9 clinical therapy", "authors": "Doudna JA, Charpentier E", "year": 2024, "journal": "Science"},
        {"title": "生僻姓氏测试", "authors": "禤某某", "year": 2023, "journal": "经济学动态"},
    ]
    bib = formatter.to_bibtex(papers)
    keys = []
    for line in bib.splitlines():
        if line.startswith("@"):
            k = line.split("{", 1)[1].rstrip(",")
            keys.append(k)
            assert k.isascii(), f"BibTeX key must be pure ASCII for pdflatex/bibtex: {k}"

    assert keys[0] == "zhao2020_glsj"
    assert keys[1] == "zhao2020_glsj_2"
    assert keys[2] == "doudna2024_crispr"
    assert keys[3] == "u79a42023_4"
    assert "author = {赵涛 and 张智}" in bib
    assert len(set(keys)) == 4

    tex_file = tmp_path / "manuscript.tex"
    tex_file.write_text(
        "% Header comment\n\\section{Introduction}\n\nDigital economy improves productivity \\cite{zhaotao2020_glsj}.\n",
        encoding="utf-8",
    )
    out = io.StringIO()
    with redirect_stdout(out):
        literature.cmd_read_paper(argparse.Namespace(filepath=str(tex_file), numbered=True, output=None, raw=False))
    data = json.loads(out.getvalue())
    assert data["status"] == "success"
    assert "% Header comment" not in data["text"]
    assert "[P2] Digital economy improves productivity" in data["text"]


def test_cnki_clean_title_and_verify_refs_reconciles_renamed_title_via_author(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "gh"))
    config.reset()

    # 1. 验证角标噪词清理
    assert cnki_search._clean_scraped_cnki_title("生成式人工智能治理研究 网络首发") == "生成式人工智能治理研究"
    assert cnki_search._clean_scraped_cnki_title("数据要素市场化配置 增强出版 OA") == "数据要素市场化配置"

    # 2. 验证『作者是对的，但题名因回译/改名略有不同导致按篇名搜不到，按作者可找回并补齐页码』
    drifted_title = "数字经济、创业活力与中国城市高质量发展研究"
    authentic_title = "数字经济、创业活跃度与高质量发展——来自中国城市的经验证据"
    assert cnki_search._title_similarity(drifted_title, authentic_title) >= 0.55

    evidence._save_session([{
        "title": drifted_title,
        "authors": "赵涛; 张智",
        "journal": "管理世界",
        "year": 2020,
        "source": "CNKI",
    }], project="verify_demo")

    monkeypatch.setattr(
        literature,
        "reconcile_cnki_paper",
        lambda title, author="", journal="", year=None: {
            "matched": True,
            "method": "author_reconciled",
            "title_changed": True,
            "similarity": 0.69,
            "original_title": title,
            "paper": {
                "title": authentic_title,
                "authors": "赵涛; 张智; 梁上坤",
                "journal": "管理世界",
                "year": 2020,
                "url": "https://kns.cnki.net/kcms2/article/abstract?v=real_token",
                "is_core": True,
                "source": "CNKI",
            },
        },
    )

    def fake_enrich(papers, top_n=5, fulltext=False):
        for p in papers:
            p["volume"] = "36"
            p["issue"] = "10"
            p["pages"] = "65-76"
        return papers

    monkeypatch.setattr(literature, "batch_read_detail", fake_enrich)

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        literature.cmd_verify_refs(argparse.Namespace(
            indices=None,
            fix_session=True,
            project="verify_demo",
        ))
    res = json.loads(out.getvalue())
    assert res["summary"]["title_reconciled"] == 1
    assert res["fixed_session"] is True
    assert res["results"][0]["authentic_title"] == authentic_title
    assert res["results"][0]["pages"] == "65-76"

    fixed = evidence._load_session("verify_demo")[0]
    assert fixed["title"] == authentic_title
    assert fixed["original_unverified_title"] == drifted_title
    assert fixed["pages"] == "65-76"


def test_local_zotero_sqlite_reader_extracts_items_collections_and_pdf_attachments(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "gh"))
    config.reset()

    z_dir = tmp_path / "Zotero"
    pdf_dir = z_dir / "storage" / "ATTKEY01"
    pdf_dir.mkdir(parents=True)
    pdf_path = pdf_dir / "zhao2020.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 zotero attachment")

    db_path = z_dir / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE itemTypes (itemTypeID INT PRIMARY KEY, typeName TEXT);
        CREATE TABLE items (itemID INT PRIMARY KEY, itemTypeID INT, key TEXT, dateModified TEXT);
        CREATE TABLE deletedItems (itemID INT PRIMARY KEY);
        CREATE TABLE fields (fieldID INT PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemDataValues (valueID INT PRIMARY KEY, value TEXT);
        CREATE TABLE itemData (itemID INT, fieldID INT, valueID INT);
        CREATE TABLE creators (creatorID INT PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INT);
        CREATE TABLE itemCreators (itemID INT, creatorID INT, orderIndex INT);
        CREATE TABLE tags (tagID INT PRIMARY KEY, name TEXT);
        CREATE TABLE itemTags (itemID INT, tagID INT);
        CREATE TABLE collections (collectionID INT PRIMARY KEY, collectionName TEXT);
        CREATE TABLE collectionItems (collectionID INT, itemID INT);
        CREATE TABLE itemAttachments (itemID INT PRIMARY KEY, parentItemID INT, path TEXT, contentType TEXT);

        INSERT INTO itemTypes VALUES (1, 'journalArticle'), (2, 'attachment');
        INSERT INTO items VALUES (10, 1, 'ITEM0001', '2026-11-18'), (11, 2, 'ATTKEY01', '2026-11-18');
        INSERT INTO fields VALUES (1, 'title'), (2, 'publicationTitle'), (3, 'date'), (4, 'volume'), (5, 'issue'), (6, 'pages'), (7, 'DOI'), (8, 'extra');
        INSERT INTO itemDataValues VALUES
            (101, '数字经济与高质量发展'),
            (102, '管理世界'),
            (103, '2020-10-15'),
            (104, '36'),
            (105, '10'),
            (106, '65-76'),
            (107, '10.19744/j.cnki.11-1235/f.2020.0154'),
            (108, 'Citation Key: zhao2020digital');
        INSERT INTO itemData VALUES (10,1,101),(10,2,102),(10,3,103),(10,4,104),(10,5,105),(10,6,106),(10,7,107),(10,8,108);
        INSERT INTO creators VALUES (1, '涛', '赵', 0);
        INSERT INTO itemCreators VALUES (10, 1, 0);
        INSERT INTO tags VALUES (1, '核心精读');
        INSERT INTO itemTags VALUES (10, 1);
        INSERT INTO collections VALUES (5, '博士论文_数字经济');
        INSERT INTO collectionItems VALUES (5, 10);
        INSERT INTO itemAttachments VALUES (11, 10, 'storage:zhao2020.pdf', 'application/pdf');
    """)
    conn.commit()
    conn.close()

    out = io.StringIO()
    with redirect_stdout(out):
        literature.cmd_zotero(argparse.Namespace(
            query="数字经济",
            collection="博士论文",
            data_dir=str(z_dir),
            limit=10,
            list_collections=False,
            save=True,
            append=False,
            project="zot_proj",
        ))
    res = json.loads(out.getvalue())
    assert res["status"] == "success"
    assert res["count"] == 1
    item = res["results"][0]
    assert item["authors"] == "赵涛"
    assert item["pages"] == "65-76"
    assert item["citekey"] == "zhao2020digital"
    assert item["local_pdf"] == str(pdf_path.resolve())
    assert len(evidence._load_session("zot_proj")) == 1
