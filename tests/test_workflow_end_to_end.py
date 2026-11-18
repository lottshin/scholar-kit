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
from workflows import render_workflow_argv  # noqa: E402


def test_cite_and_export_indices_preserve_manuscript_order_and_only_enrich_subset(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "global_home"))
    config.reset()

    evidence._save_session([
        {"title": "第一篇未引用", "authors": "甲", "journal": "期刊A", "year": 2021, "url": "https://kns.cnki.net/kcms2/article/abstract?v=1", "source": "CNKI"},
        {"title": "第二篇后引用", "authors": "乙", "journal": "期刊B", "year": 2022, "url": "https://kns.cnki.net/kcms2/article/abstract?v=2", "source": "CNKI"},
        {"title": "第三篇先引用", "authors": "丙", "journal": "期刊C", "year": 2023, "url": "https://kns.cnki.net/kcms2/article/abstract?v=3", "source": "CNKI"},
    ], project="demo")

    enriched_urls = []

    def fake_batch_read(papers, top_n=5, fulltext=False):
        for p in papers:
            enriched_urls.append(p["url"])
            p["issue"] = "4"
            p["pages"] = "20-35"
        return papers

    monkeypatch.setattr(literature, "batch_read_detail", fake_batch_read)

    # 正文先引用第 3 篇，再引用第 2 篇；第 1 篇未引用，绝不应去抓第 1 篇详情页
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        literature.cmd_cite(argparse.Namespace(
            style="gbt7714",
            indices="3,2",
            keep_numbers=False,
            raw=False,
            project="demo",
        ))
    data = json.loads(out.getvalue())
    lines = data["references"].splitlines()

    assert len(enriched_urls) == 2
    assert "https://kns.cnki.net/kcms2/article/abstract?v=1" not in enriched_urls
    assert lines[0].startswith("[1] 丙. 第三篇先引用[J].")
    assert lines[1].startswith("[2] 乙. 第二篇后引用[J].")

    # --keep-numbers 保留课题库原始序号 [3] 与 [2]
    out_keep = io.StringIO()
    with redirect_stdout(out_keep), redirect_stderr(io.StringIO()):
        literature.cmd_cite(argparse.Namespace(
            style="gbt7714",
            indices="3,2",
            keep_numbers=True,
            raw=False,
            project="demo",
        ))
    keep_lines = json.loads(out_keep.getvalue())["references"].splitlines()
    assert keep_lines[0].startswith("[3] 丙.")
    assert keep_lines[1].startswith("[2] 乙.")

    # export --indices 同样按传入顺序导出子集
    out_exp = io.StringIO()
    with redirect_stdout(out_exp):
        literature.cmd_export(argparse.Namespace(
            export_format="bibtex",
            indices="3,2",
            output=None,
            raw=False,
            project="demo",
        ))
    exp_data = json.loads(out_exp.getvalue())
    assert exp_data["count"] == 2
    assert exp_data["content"].index("第三篇先引用") < exp_data["content"].index("第二篇后引用")


def test_library_curates_tags_notes_dedupe_and_remove(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "global_home"))
    config.reset()

    evidence._save_session([
        {"title": "数字经济与高质量发展", "authors": "赵涛", "year": 2020, "cnki_id": "CJFQ:GLSJ202010009", "source": "CNKI"},
        {"title": "数字经济与高质量发展", "authors": "赵涛", "year": 2020, "cnki_id": "CJFQ:GLSJ202010009", "abstract": "补充摘要内容", "source": "CNKI"},
        {"title": "无关噪音文献", "authors": "路人", "year": 2019, "source": "CNKI"},
        {"title": "制度型开放研究", "authors": "李四", "year": 2024, "source": "CNKI"},
    ], project="curate")

    out = io.StringIO()
    with redirect_stdout(out):
        literature.cmd_library(argparse.Namespace(
            project="curate",
            limit=None,
            indices="1",
            tag="核心实证,基准模型",
            clear_tags=False,
            note="主回归机制参考",
            keep=None,
            remove="3",
            dedupe=True,
        ))

    res = json.loads(out.getvalue())
    assert res["count"] == 2
    assert res["results"][0]["tags"] == ["核心实证", "基准模型"]
    assert res["results"][0]["note"] == "主回归机制参考"
    # 去重后合并了第 2 条的摘要且剔除了第 3 条无关文献
    reloaded = evidence._load_session("curate")
    assert len(reloaded) == 2
    assert reloaded[0]["abstract"] == "补充摘要内容"
    assert all(p["title"] != "无关噪音文献" for p in reloaded)


def test_citations_snowball_saves_directly_into_project_session(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "global_home"))
    config.reset()

    monkeypatch.setattr(
        literature,
        "get_cnki_citations",
        lambda pid, direction="both", limit=20: {
            "status": "success",
            "source": "CNKI",
            "paper": {"title": pid},
            "citing_count": 1,
            "references_count": 1,
            "citing": [{"title": "2026前沿施引论文", "authors": "王五", "journal": "中国工业经济", "year": 2026, "doc_tag": "J", "url": "https://kns.cnki.net/kcms2/article/abstract?v=c1", "source": "CNKI"}],
            "references": [{"title": "2019经典基础论文", "authors": "郭峰", "journal": "经济学(季刊)", "year": 2019, "doc_tag": "J", "url": "https://kns.cnki.net/kcms2/article/abstract?v=r1", "source": "CNKI"}],
        },
    )

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        literature.cmd_citations(argparse.Namespace(
            paper_id="数字经济、创业活跃度与高质量发展",
            direction="both",
            limit=10,
            save="both",
            append=True,
            project="snowball_proj",
        ))

    payload = json.loads(out.getvalue())
    assert payload["saved_to_session"]["saved_count"] == 2
    saved = evidence._load_session("snowball_proj")
    assert len(saved) == 2
    assert "引证前沿" in saved[0]["tags"]
    assert "参考溯源" in saved[1]["tags"]


def test_import_pdf_directory_populates_session_and_supports_read_detail_fulltext(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "global_home"))
    config.reset()

    pdf_folder = tmp_path / "user_pdfs"
    pdf_folder.mkdir()
    (pdf_folder / "数字经济赋能乡村振兴机制研究_陈晓红.pdf").write_bytes(b"%PDF-1.4 mock")

    class FakePage:
        def extract_text(self):
            return (
                "2024年第5期 管理世界\n"
                "摘 要：本文基于县域面板数据系统检验数字经济赋能乡村振兴的作用机制与空间溢出效应。\n"
                "关键词：数字经济；乡村振兴；县域治理\n"
                "一、引言\n数字基础设施建设正在深刻改变农村要素配置方式。"
            )

    class FakePDF:
        metadata = {"Title": "Microsoft Word - 文档1.docx"}
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

    out_imp = io.StringIO()
    with redirect_stdout(out_imp):
        literature.cmd_import(argparse.Namespace(
            filepath=str(pdf_folder),
            append=False,
            project="pdf_lib",
        ))
    imp_data = json.loads(out_imp.getvalue())
    assert imp_data["status"] == "success"
    assert imp_data["import_type"] == "pdf"
    assert imp_data["count"] == 1
    p = imp_data["results"][0]
    assert p["title"] == "数字经济赋能乡村振兴机制研究"
    assert p["authors"] == "陈晓红"
    assert p["year"] == 2024
    assert "数字经济" in p["keywords"]
    assert "县域面板数据" in p["abstract"]

    # 导入后的本地 PDF 可直接通过 read-detail --fulltext 提取带页码正文
    out_rd = io.StringIO()
    with redirect_stdout(out_rd), redirect_stderr(io.StringIO()):
        literature.cmd_read_detail(argparse.Namespace(
            top_n=5,
            indices="1",
            fulltext=True,
            project="pdf_lib",
        ))
    rd_data = json.loads(out_rd.getvalue())
    assert rd_data["status"] == "success"
    assert "[第 1 页]" in rd_data["results"][0]["fulltext"]


def test_read_paper_numbered_and_patch_docx_dry_run_and_table_support(tmp_path):
    from docx import Document

    docx_file = tmp_path / "draft.docx"
    doc = Document()
    doc.add_paragraph("第一章 绪论")
    doc.add_paragraph("数字经济显著提升了生产效率。")
    doc.add_paragraph("数字经济显著提升了生产效率。")  # 故意重复句子测试段落锚点消歧
    tbl = doc.add_table(rows=1, cols=2)
    tbl.rows[0].cells[0].text = "旧指标名称"
    tbl.rows[0].cells[1].text = "0.245***"
    doc.save(str(docx_file))

    out_read = io.StringIO()
    with redirect_stdout(out_read):
        literature.cmd_read_paper(argparse.Namespace(
            filepath=str(docx_file),
            numbered=True,
            output=None,
            raw=False,
        ))
    read_data = json.loads(out_read.getvalue())
    assert "[P3] 数字经济显著提升了生产效率。" in read_data["text"]
    assert "[T1R1] 旧指标名称 | 0.245***" in read_data["text"]

    patch_file = tmp_path / "patch.json"
    out_docx = tmp_path / "patched.docx"
    patch_file.write_text(json.dumps({
        "patches": [
            {"paragraph": "P3", "find": "数字经济显著提升了生产效率。", "replace": "数字经济通过要素配置优化提升了全要素生产率[1]。"},
            {"paragraph": "T1R1", "find": "旧指标名称", "replace": "核心解释变量"},
        ],
        "footnotes": [],
        "append_references": ["[1] 赵涛, 等. 数字经济与高质量发展[J]. 管理世界, 2020."],
    }, ensure_ascii=False), encoding="utf-8")

    # 1. --dry-run 预检通过且绝不写盘
    out_dry = io.StringIO()
    with redirect_stdout(out_dry):
        literature.cmd_patch_docx(argparse.Namespace(
            filepath=str(docx_file),
            patch=str(patch_file),
            output=str(out_docx),
            dry_run=True,
        ))
    dry_res = json.loads(out_dry.getvalue())
    assert dry_res["status"] == "success"
    assert dry_res["dry_run"] is True
    assert dry_res["replaced"] == 2
    assert not out_docx.exists()

    # 2. 正式写盘：仅修改 P3（保留 P2 原句不动）并成功修改表格单元格 T1R1
    out_apply = io.StringIO()
    with redirect_stdout(out_apply):
        literature.cmd_patch_docx(argparse.Namespace(
            filepath=str(docx_file),
            patch=str(patch_file),
            output=str(out_docx),
            dry_run=False,
        ))
    apply_res = json.loads(out_apply.getvalue())
    assert apply_res["status"] == "success"
    saved_doc = Document(str(out_docx))
    assert saved_doc.paragraphs[1].text == "数字经济显著提升了生产效率。"
    assert saved_doc.paragraphs[2].text == "数字经济通过要素配置优化提升了全要素生产率[1]。"
    assert saved_doc.tables[0].rows[0].cells[0].text == "核心解释变量"


def test_new_cnki_and_snowball_workflow_templates_and_optional_empty_vars():
    cnki_cmds = render_workflow_argv(
        "cnki_literature_review",
        {"topic": "新质生产力", "core_type": "CSSCI", "project": "xzl"},
    )
    assert cnki_cmds[0][:2] == ["search", "新质生产力"]
    assert "--core" in cnki_cmds[0]
    assert cnki_cmds[2][0] == "review"
    assert "--auto-detail" in cnki_cmds[2]

    # 空字符串可选过滤变量自动跳过，不传入空 --journal-filter ""
    hq_cmds = render_workflow_argv(
        "high_quality_screening",
        {"topic": "graphrag", "year_from": "2024", "journal": ""},
    )
    assert "--journal-filter" not in hq_cmds[0]
