from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import docx_tools  # noqa: E402
import literature  # noqa: E402
import search as search_mod  # noqa: E402
import venues  # noqa: E402


def test_venues_offline_ssci_sci_ccf_and_cas_warning_detection():
    # 1. SSCI 期刊按 ISSN 精准识别
    p_ssci = {
        "title": "YouTube and the social archiving of intangible heritage",
        "journal": "New Media & Society",
        "issn": ["1461-4448", "1461-7315"],
        "cited_by": 120,
        "doi": "10.1177/1461444812469598",
        "abstract": "x" * 260,
        "source": "OpenAlex",
    }
    venues.annotate_venue_quality(p_ssci)
    assert "SSCI" in p_ssci["venue_tags"]
    assert p_ssci["is_core"] is True
    assert p_ssci["venue_tier"] == "core"

    # 2. CCF-A 顶会与顶刊识别（含 DBLP rec/ URL 与会议全称）
    p_ccf = {
        "title": "Deep Residual Learning for Image Recognition",
        "journal": "IEEE Conference on Computer Vision and Pattern Recognition (CVPR)",
        "url": "https://dblp.org/rec/conf/cvpr/HeZRS16",
        "source": "DBLP",
    }
    venues.annotate_venue_quality(p_ccf)
    assert "CCF-A" in p_ccf["venue_tags"]
    assert p_ccf["is_core"] is True

    # 3. 2025 在榜中科院国际预警期刊（强制标为 is_warning=True 且剔除核心资格、扣减质量分）
    p_warn = {
        "title": "A wireless sensor network study",
        "journal": "Wireless Personal Communications",
        "issn": ["0929-6212"],
        "cited_by": 20,
        "doi": "10.1007/s11277-025-0001",
        "abstract": "x" * 260,
        "source": "OpenAlex",
    }
    venues.annotate_venue_quality(p_warn)
    assert p_warn["is_warning"] is True
    assert p_warn["is_core"] is False
    assert p_warn["venue_tier"] == "cas_warning"
    assert p_warn["cas_warning"]["year"] == 2025
    assert search_mod.calculate_quality_score(p_ssci) >= search_mod.calculate_quality_score(p_warn) + 30

    # 4. 2023 曾列但 2024/2025 已调出期刊：仅记录历史提示，不误判为当前在榜预警
    p_hist = {
        "title": "Structural performance of timber buildings",
        "journal": "Buildings",
        "issn": ["2075-5309"],
        "source": "OpenAlex",
    }
    venues.annotate_venue_quality(p_hist)
    assert not p_hist.get("is_warning")
    assert "2023" in p_hist.get("cas_warning_history", "")

    # 5. 国际核心筛选令牌匹配与预警剔除
    wanted = venues.parse_international_core_tokens("北大核心,CSSCI,SSCI,CCF-A")
    assert wanted == {"SSCI", "CCF-A"}
    assert venues.matches_international_core(p_ssci, wanted) is True
    assert venues.matches_international_core(p_ccf, wanted) is True
    assert venues.matches_international_core(p_warn, set()) is False


def test_write_docx_superscript_abstract_kaiti_caption_centering_and_footer_page_field(tmp_path):
    md = (
        "# 论文总标题应居中黑体\n\n"
        "**摘要**：本文考察非遗短视频跨文化传播机制与符号调适路径。\n\n"
        "**关键词**：非遗短视频；跨文化传播；符号博弈\n\n"
        "**Abstract**: This study investigates cross-cultural short video communication.\n\n"
        "**Keywords**: intangible heritage; short video\n\n"
        "## 一、引言\n\n"
        "流媒体平台推动了跨国视听流动[1, 2]，相关政策自[2024]年起持续深化[3-5]。\n\n"
        "表1 媒介形态特征对照\n\n"
        "| 维度 | 传统展演 | 数字短视频 |\n"
        "| --- | --- | --- |\n"
        "| 传播场域 | 在场仪式 | 算法分发[1] |\n\n"
        "## 参考文献\n\n"
        "[1] 李勇. 新媒体非遗跨文化传播效果分析[J]. 图书馆论坛, 2023, 44(2): 92-102.\n"
    )
    out_docx = tmp_path / "formatted.docx"
    res = docx_tools._write_docx_from_markdown(md, out_docx)
    assert res["status"] == "success"
    assert res["tables"] == 1
    assert res["superscript_citations"] == 3  # 正文 [1, 2]、[3-5] 与表格 [1]；[2024] 与文末 [1] 不转上标

    doc = Document(str(out_docx))
    # 标题居中
    assert doc.paragraphs[0].alignment == WD_ALIGN_PARAGRAPH.CENTER

    # 中文摘要：首 run 为加粗黑体“摘要：”，次 run 为楷体正文
    abs_p = next(p for p in doc.paragraphs if p.text.startswith("摘要："))
    assert abs_p.runs[0].bold is True
    assert abs_p.runs[0]._r.rPr.rFonts.get(qn("w:eastAsia")) == "黑体"
    assert abs_p.runs[1]._r.rPr.rFonts.get(qn("w:eastAsia")) == "楷体"

    # 表题居中且防孤行 keep_with_next
    cap_p = next(p for p in doc.paragraphs if p.text.startswith("表1"))
    assert cap_p.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert cap_p.paragraph_format.keep_with_next is True

    # 正文角标上标 vs 年份 [2024] 非上标 vs 参考文献 [1] 非上标
    body_p = next(p for p in doc.paragraphs if "流媒体平台推动了" in p.text)
    sup_texts = [r.text for r in body_p.runs if r.font.superscript]
    normal_texts = "".join(r.text for r in body_p.runs if not r.font.superscript)
    assert sup_texts == ["[1, 2]", "[3-5]"]
    assert "[2024]" in normal_texts

    ref_p = doc.paragraphs[-1]
    assert ref_p.text.startswith("[1] 李勇.")
    assert not any(r.font.superscript for r in ref_p.runs)

    # 三线表跨页重复表头 tblHeader + 页脚动态 PAGE 域
    assert "tblHeader" in doc.tables[0].rows[0]._tr.xml
    footer_xml = doc.sections[0].footer._element.xml
    assert "PAGE" in footer_xml


def test_read_paper_preserves_interleaved_table_order_and_patch_docx_replaces_refs_with_superscript(tmp_path):
    orig_path = tmp_path / "draft.docx"
    patched_path = tmp_path / "patched.docx"
    patch_json = tmp_path / "patch.json"

    doc = Document()
    doc.add_heading("测试初稿", level=1)
    doc.add_paragraph("初稿正文第一段尚未标注上标引用。")
    tbl = doc.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "维度"
    tbl.cell(0, 1).text = "机制"
    tbl.cell(1, 0).text = "路径"
    tbl.cell(1, 1).text = "旧表格表述"
    doc.add_heading("参考文献", level=1)
    doc.add_paragraph("[1] 旧文献缺页码条目[J]. 管理世界, 2020.")
    doc.save(str(orig_path))

    # 验证 read-paper 严格保持「正文 -> 表格 -> 参考文献」真实排版顺序，不再将表格甩到参考文献之后
    captured_read = {}
    docx_tools.cmd_read_paper(
        argparse.Namespace(filepath=str(orig_path), numbered=True, toc=False, section=None, max_chars=None, output=None, raw=False),
        output_fn=captured_read.update,
    )
    txt = captured_read["text"]
    assert txt.index("[T1R2]") < txt.index("参考文献")

    patch_payload = {
        "superscript_citations": True,
        "patches": [
            {
                "paragraph": "P2",
                "find": "初稿正文第一段尚未标注上标引用。",
                "replace": "改写后的正文第一段已补齐规范上标引用[1, 2]。",
            },
            {
                "paragraph": "T1R2",
                "find": "旧表格表述",
                "replace": "新表格机制[1]",
            },
        ],
        "replace_references": [
            "[1] 赵涛, 张智, 梁上坤. 数字经济、创业活跃度与高质量发展[J]. 管理世界, 2020, 36(10): 65-76.",
            "[2] 谢春. 非遗传承人的传播实践与文化空间再造[J]. 现代传播(中国传媒大学学报), 2021, 43(9): 98-103.",
        ],
    }
    patch_json.write_text(json.dumps(patch_payload, ensure_ascii=False), encoding="utf-8")

    captured_patch = {}
    docx_tools.cmd_patch_docx(
        argparse.Namespace(filepath=str(orig_path), patch=str(patch_json), output=str(patched_path), dry_run=False),
        output_fn=captured_patch.update,
    )
    assert captured_patch["status"] == "success"
    assert captured_patch["replaced"] == 2
    assert captured_patch["references_replaced"] == 2

    reloaded = Document(str(patched_path))
    p2 = reloaded.paragraphs[1]
    assert [r.text for r in p2.runs if r.font.superscript] == ["[1, 2]"]
    cell_p = reloaded.tables[0].cell(1, 1).paragraphs[0]
    assert [r.text for r in cell_p.runs if r.font.superscript] == ["[1]"]
    ref_texts = [p.text for p in reloaded.paragraphs if p.text.startswith("[")]
    assert len(ref_texts) == 2
    assert "65-76." in ref_texts[0]
    assert "旧文献缺页码条目" not in "\n".join(ref_texts)


def test_verify_refs_from_file_parses_docx_and_reconciles_english_via_raw_author(tmp_path, monkeypatch):
    docx_p = tmp_path / "manuscript.docx"
    doc = Document()
    doc.add_heading("Manuscript", level=1)
    doc.add_paragraph("Body paragraph citing [1].")
    doc.add_heading("参考文献", level=1)
    doc.add_paragraph("[1] Pietrobruno S. Social archiving of intangible heritage on YouTube[J]. New Media & Society, 2013.")
    doc.save(str(docx_p))

    seen_filters = []

    def _fake_http_get(url, params=None, headers=None, timeout=15):
        seen_filters.append((params or {}).get("filter", ""))
        return {
            "results": [
                {
                    "title": "YouTube and the social archiving of intangible heritage",
                    "publication_year": 2013,
                    "cited_by_count": 123,
                    "doi": "https://doi.org/10.1177/1461444812469598",
                    "authorships": [{"author": {"display_name": "Sheenagh Pietrobruno"}}],
                    "biblio": {"volume": "15", "issue": "8", "first_page": "1259", "last_page": "1276"},
                    "abstract_inverted_index": {"YouTube": [0], "archiving": [1]},
                    "primary_location": {
                        "source": {
                            "display_name": "New Media & Society",
                            "issn": ["1461-4448"],
                            "host_organization_name": "SAGE Publications",
                        }
                    },
                }
            ]
        }

    monkeypatch.setattr(search_mod, "_http_get", _fake_http_get)
    monkeypatch.setattr(search_mod, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(search_mod, "_cache_set", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)

    out = {}
    monkeypatch.setattr(literature, "_output", out.update)
    literature.cmd_verify_refs(
        argparse.Namespace(from_file=str(docx_p), indices=None, fix_session=True, project="rev_proj")
    )

    assert any("raw_author_name.search:Pietrobruno" in f for f in seen_filters)
    assert out["summary"]["title_reconciled"] == 1
    assert out["results"][0]["authentic_title"] == "YouTube and the social archiving of intangible heritage"
    assert out["results"][0]["pages"] == "1259-1276"
    assert "YouTube and the social archiving of intangible heritage[J]. New Media & Society, 2013, 15(8): 1259-1276." in out["formatted_references"][0]
