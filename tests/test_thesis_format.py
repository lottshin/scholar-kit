from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from docx import Document
from docx.shared import Cm, Pt

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import literature  # noqa: E402
from thesis_format import audit_document, format_document, load_spec_from_inputs  # noqa: E402


def _make_sample_thesis(path: Path) -> Path:
    doc = Document()
    sec = doc.sections[0]
    sec.top_margin = Cm(2.0)
    sec.left_margin = Cm(2.0)
    doc.styles["Normal"].font.size = Pt(10.5)

    doc.add_paragraph("一、绪论")
    doc.add_paragraph("（一）研究背景")
    doc.add_paragraph("这是一段待格式化的学位论文正文内容。")
    doc.add_paragraph("表 1 样本统计")
    doc.add_paragraph("参考文献")
    doc.add_paragraph("[1] 张三. 人工智能与社会治理[J]. 中国社会科学, 2025(1): 10-25.")
    doc.save(str(path))
    return path


def test_audit_and_format_docx_preserves_source_and_applies_spec(tmp_path: Path):
    src = _make_sample_thesis(tmp_path / "draft.docx")
    spec = load_spec_from_inputs(
        spec_text=(
            "上边距2.5cm，左边距3.0cm；正文小四宋体1.5倍行距首行缩进2字符；"
            "一级标题三号黑体居中；标题自动编号第一章 1.1；页眉：硕士学位论文，带分割线；页脚加页码；"
            "参考文献五号宋体悬挂缩进2字符。"
        )
    )

    audit = audit_document(src, spec)
    assert audit["status"] == "success"
    modified_ids = {item["id"] for item in audit["modified"]}
    assert "margin_top" in modified_ids
    assert "margin_left" in modified_ids
    assert "body_font_size_pt" in modified_ids

    # Refuse to overwrite original file
    overwrite_err = format_document(src, src, spec)
    assert overwrite_err["status"] == "error"
    assert overwrite_err["code"] == "OUTPUT_OVERWRITES_SOURCE"

    out_docx = tmp_path / "draft_formatted.docx"
    fmt = format_document(src, out_docx, spec)
    assert fmt["status"] == "success"
    assert fmt["original_preserved"] is True
    assert out_docx.is_file()
    assert fmt["applied_changes"]["numbered_headings"] >= 2
    assert fmt["applied_changes"]["header_footer"]["headers"] == 1

    formatted_doc = Document(str(out_docx))
    texts = [p.text for p in formatted_doc.paragraphs]
    assert texts[0] == "第一章 绪论"
    assert texts[1] == "1.1 研究背景"
    re_audit = audit_document(out_docx, spec)
    re_modified_ids = {item["id"] for item in re_audit["modified"]}
    assert "margin_top" not in re_modified_ids
    assert "body_font_size_pt" not in re_modified_ids


def test_cli_format_audit_and_format_docx_commands(tmp_path: Path):
    src = _make_sample_thesis(tmp_path / "thesis.docx")
    report_md = tmp_path / "audit.md"

    buf_audit = io.StringIO()
    with redirect_stdout(buf_audit):
        literature.cmd_format_audit(
            argparse.Namespace(
                filepath=str(src),
                template=None,
                spec=None,
                spec_text="正文小四宋体1.5倍行距",
                format="markdown",
                output=str(report_md),
                raw=False,
            )
        )
    audit_json = json.loads(buf_audit.getvalue())
    assert audit_json["status"] == "success"
    assert report_md.is_file()

    buf_fmt = io.StringIO()
    with redirect_stdout(buf_fmt):
        literature.cmd_format_docx(
            argparse.Namespace(
                filepath=str(src),
                template=None,
                spec=None,
                spec_text="正文小四宋体1.5倍行距",
                output=None,
                report_output=None,
                report_format="json",
                raw=False,
            )
        )
    fmt_json = json.loads(buf_fmt.getvalue())
    assert fmt_json["status"] == "success"
    assert Path(fmt_json["output"]).name == "thesis_formatted.docx"
    assert Path(fmt_json["output"]).is_file()
