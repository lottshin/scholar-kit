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


def test_cite_latex_rewrite_and_bibtex_output(tmp_path, monkeypatch):
    ws = tmp_path / "tex_ws"
    proj_dir = ws / ".scholar-kit" / "projects" / "tex_demo"
    proj_dir.mkdir(parents=True)

    papers = [
        {
            "title": "未被引用的无关文献一",
            "authors": "张三",
            "journal": "经济研究",
            "year": 2021,
            "pages": "1-10",
            "doc_type": "journal",
        },
        {
            "title": "数字经济、创业活跃度与高质量发展",
            "authors": "赵涛; 张智; 梁上坤",
            "journal": "管理世界",
            "year": 2020,
            "volume": "36",
            "issue": "10",
            "pages": "65-76",
            "doc_type": "journal",
        },
        {
            "title": "Deep Residual Learning for Image Recognition",
            "authors": "Kaiming He; Xiangyu Zhang; Shaoqing Ren; Jian Sun",
            "journal": "CVPR",
            "year": 2016,
            "pages": "770-778",
            "doi": "10.1109/CVPR.2016.90",
            "doc_type": "conference",
        },
    ]
    (proj_dir / "session.json").write_text(json.dumps(papers, ensure_ascii=False), encoding="utf-8")

    draft_tex = (
        "\\documentclass[12pt]{article}\n"
        "% 注释中的序号 [1] 绝不能被提取或改写\n"
        "\\begin{document}\n"
        "\\begin{enumerate}\n"
        "\\item[1] 列表项可选参数不应当作引文；数字经济推动高质量发展[2]，残差网络提升了视觉表征[2, 3]。\n"
        "\\end{enumerate}\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{refs}\n"
        "\\end{document}\n"
    )
    draft_p = ws / "draft.tex"
    final_p = ws / "main.tex"
    bib_p = ws / "refs.bib"
    draft_p.write_text(draft_tex, encoding="utf-8")

    monkeypatch.chdir(ws)
    out = {}
    monkeypatch.setattr(literature, "_output", out.update)
    literature.cmd_cite(
        argparse.Namespace(
            style="gbt7714",
            indices=None,
            from_file=str(draft_p),
            rewrite_output=str(final_p),
            bibtex_output=str(bib_p),
            keep_numbers=False,
            in_text=False,
            no_enrich=True,
            raw=False,
            project="tex_demo",
        )
    )

    assert out["status"] == "success"
    assert out["selected_indices"] == [2, 3]
    keys = out["cite_keys"]
    assert len(keys) == 2
    assert all(k.isascii() for k in keys)

    rewritten_tex = final_p.read_text(encoding="utf-8")
    assert "\\documentclass[12pt]{article}" in rewritten_tex
    assert "% 注释中的序号 [1] 绝不能被提取或改写" in rewritten_tex
    assert "\\item[1] 列表项可选参数不应当作引文" in rewritten_tex
    assert f"推动高质量发展\\cite{{{keys[0]}}}" in rewritten_tex
    assert f"提升了视觉表征\\cite{{{keys[0]}, {keys[1]}}}" in rewritten_tex

    bib_txt = bib_p.read_text(encoding="utf-8")
    assert f"@article{{{keys[0]}," in bib_txt
    assert f"@inproceedings{{{keys[1]}," in bib_txt
    assert "未被引用的无关文献一" not in bib_txt


def test_smoke_upstream_probes_and_dblp_bot_gate_graceful_handling(monkeypatch):
    import smoke_upstream

    monkeypatch.setattr(search_mod, "search_openalex", lambda *a, **k: [{"title": "Deep Learning"}])
    monkeypatch.setattr(search_mod, "search_pubmed", lambda *a, **k: [{"title": "CRISPR-Cas9"}])
    monkeypatch.setattr(search_mod, "resolve_crossref", lambda *a, **k: {"title": "Deep learning", "pages": "436-444"})
    monkeypatch.setattr(search_mod, "search_arxiv", lambda *a, **k: [{"title": "Attention Is All You Need"}])

    rep = smoke_upstream.run_all_probes()
    assert rep["status"] == "success"
    assert rep["passed"] == 4
    assert rep["failed"] == 0

    # 当 DBLP 返回含 "result" 字样的非 JSON HTML 验证页时不得抛出 AttributeError
    monkeypatch.setattr(search_mod, "_http_get", lambda *a, **k: "<html>Making sure you're not a bot! result</html>")
    monkeypatch.setattr(search_mod, "_cache_get", lambda *a, **k: None)
    assert search_mod.search_dblp("ResNet", limit=2) == []


def test_prepare_cnki_session_skips_reload_when_warm_and_preserves_fresh_cookies(tmp_path, monkeypatch):
    from cnki import driver as drv_mod

    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(
        json.dumps([
            {"name": "SID_kns", "value": "stale_from_disk"},
            {"name": "extra_ck", "value": "disk_val"},
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(drv_mod, "_cookie_path", lambda for_write=False: cookie_file)
    monkeypatch.setattr(drv_mod, "_browser_activity_file", lambda: tmp_path / "browser-last-active.txt")

    added = []
    nav_urls = []

    class _WarmDrv:
        _scholar_was_warm = True
        current_url = "https://kns.cnki.net/kns8s/AdvSearch"
        title = "高级检索-中国知网"

        def get(self, u):
            nav_urls.append(u)

        def get_cookies(self):
            return [{"name": "SID_kns", "value": "fresh_live_cookie"}]

        def add_cookie(self, ck):
            added.append(ck)

    warm = _WarmDrv()
    drv_mod._prepare_cnki_session(warm)
    # 热复用且已在 cnki.net：绝不重刷首页，也不注入旧 cookie
    assert nav_urls == []
    assert added == []
    assert (tmp_path / "browser-last-active.txt").is_file()

    # 冷启动（about:blank）：访问首页一次，且 merge_only 保留内存中已有的 SID_kns，仅补入缺失的 extra_ck
    warm._scholar_was_warm = False
    warm.current_url = "about:blank"
    monkeypatch.setattr("time.sleep", lambda *_: None)
    drv_mod._prepare_cnki_session(warm)
    assert nav_urls == ["https://kns.cnki.net/"]
    assert [c["name"] for c in added] == ["extra_ck"]


def test_navigate_within_cnki_uses_same_site_location_href_for_referer(monkeypatch, tmp_path):
    from cnki import driver as drv_mod

    monkeypatch.setattr(drv_mod, "_browser_activity_file", lambda: tmp_path / "act.txt")
    monkeypatch.setattr("time.sleep", lambda *_: None)

    js_calls = []
    get_calls = []

    class _NavDrv:
        current_url = "https://kns.cnki.net/kns8s/AdvSearch"

        def execute_script(self, script, *args):
            js_calls.append((script, args))

        def get(self, u):
            get_calls.append(u)

    nd = _NavDrv()
    target = "https://kns.cnki.net/kcms2/article/abstract?v=123"
    drv_mod._navigate_within_cnki(nd, target)
    assert len(js_calls) == 1
    assert "window.location.href" in js_calls[0][0]
    assert js_calls[0][1] == (target,)
    assert get_calls == []


def test_detect_cnki_login_wall_short_circuits_fulltext_and_download(monkeypatch):
    from cnki import detail as det_mod
    from cnki import download as dl_mod
    from cnki import driver as drv_mod

    class _LoginTabDrv:
        current_url = "https://login.cnki.net/login/?platform=kns"
        title = "欢迎登录-中国知网"
        window_handles = ["main", "login_popup"]
        current_window_handle = "main"

        def switch_to(self):
            return self

        def close(self):
            self.window_handles.remove("login_popup")

        def execute_script(self, *a, **k):
            return ""

    ldrv = _LoginTabDrv()
    ldrv.switch_to = type("Sw", (), {"window": lambda self, h: setattr(ldrv, "current_window_handle", h)})()

    reason = drv_mod._detect_cnki_login_wall(ldrv)
    assert reason and "登录" in reason

    monkeypatch.setattr("time.sleep", lambda *_: None)
    dl_reason = dl_mod._check_post_download_auth_wall(ldrv, {"main"}, "main")
    assert dl_reason and "登录" in dl_reason
    assert "login_popup" not in ldrv.window_handles

    # 验证 batch_read_detail 首篇遇到全文登录墙后短路后续篇目的全文点击，但仍完整保留所有篇目的免费摘要
    html_attempts = []

    class _BatchDrv:
        current_url = "https://kns.cnki.net/kns8s/AdvSearch"
        _scholar_was_warm = True
        title = "高级检索-中国知网"

        def execute_script(self, *a, **k):
            return None

    bdrv = _BatchDrv()
    monkeypatch.setattr(det_mod, "_detect_browser", lambda: "chrome")
    monkeypatch.setattr(det_mod, "_create_driver", lambda browser=None: bdrv)
    monkeypatch.setattr(det_mod, "_prepare_cnki_session", lambda d: None)
    monkeypatch.setattr(det_mod, "_save_cookies", lambda d: None)
    monkeypatch.setattr(det_mod, "_navigate_within_cnki", lambda d, u, **k: None)
    monkeypatch.setattr(det_mod, "_handle_captcha", lambda d: d)
    monkeypatch.setattr(det_mod, "_is_cnki_error_or_404", lambda d: False)
    monkeypatch.setattr(det_mod, "_parse_detail_page", lambda d: {"abstract": "免费摘要正常提取", "pages": "10-18"})
    monkeypatch.setattr(det_mod, "_load_cached_fulltext", lambda *a, **k: None)
    monkeypatch.setattr(det_mod, "_extract_local_pdf_fulltext", lambda *a, **k: None)
    monkeypatch.setattr(det_mod, "_release_driver", lambda d: None)

    def _fake_read_html(drv, u):
        html_attempts.append(u)
        drv._cnki_auth_wall_reason = "当前未登录知网机构账号"
        return None

    monkeypatch.setattr(det_mod, "_read_fulltext_html", _fake_read_html)

    papers = [
        {"title": f"测试论文{i}", "source": "CNKI", "url": f"https://kns.cnki.net/kcms2/article/abstract?v={i}"}
        for i in range(1, 5)
    ]
    det_mod.batch_read_detail(papers, top_n=4, fulltext=True)
    # 4 篇全部仅在第 1 篇尝试了 1 次 HTML 全文点击即短路，且 4 篇免费摘要与页码全部正常拿到
    assert len(html_attempts) == 1
    assert all(p.get("abstract") == "免费摘要正常提取" for p in papers)
    assert all(p.get("auth_required") is True for p in papers)


def test_browser_idle_timeout_env_override(monkeypatch):
    from cnki import driver as drv_mod

    monkeypatch.setenv("SCHOLAR_BROWSER_IDLE_MINUTES", "15")
    assert drv_mod._idle_timeout_seconds() == 900
    monkeypatch.setenv("SCHOLAR_BROWSER_IDLE_MINUTES", "0")
    assert drv_mod._idle_timeout_seconds() == 0


def test_open_cnki_reading_tab_bypasses_popup_blocker_with_referrer(monkeypatch):
    from cnki import detail as det_mod

    monkeypatch.setattr("time.sleep", lambda *_: None)
    cdp_calls = []

    class _TabDrv:
        current_url = "https://kns.cnki.net/kcms2/article/abstract?v=detail123"
        current_window_handle = "detail_tab"
        window_handles = ["detail_tab"]

        def __init__(self):
            class _Sw:
                def new_window(self, kind):
                    raise AssertionError("new_window must NOT be called (would trigger Target.activateTarget foreground steal)")

                def window(self, h):
                    raise AssertionError("switch_to.window must NOT be called (would trigger Target.activateTarget foreground steal)")

            self.switch_to = _Sw()

        def execute_cdp_cmd(self, cmd, params):
            cdp_calls.append((cmd, params))
            self.current_url = params["url"]
            return {}

        def execute_script(self, script, *args):
            if "readyState" in script:
                return "complete"
            raise AssertionError("window.open should not be used")

    drv = _TabDrv()
    handle = det_mod._open_cnki_reading_tab(drv, "https://kns.cnki.net/kcms2/article/html?v=html456")
    assert handle == "detail_tab"
    assert cdp_calls == [
        (
            "Page.navigate",
            {
                "url": "https://kns.cnki.net/kcms2/article/html?v=html456",
                "referrer": "https://kns.cnki.net/kcms2/article/abstract?v=detail123",
            },
        )
    ]


def test_refworks_export_page_range_no_duplicate_sp():
    from cnki.download import _parse_refworks_export

    raw = (
        "RT Journal Article\n"
        "T1 数字经济与高质量发展\n"
        "A1 赵涛\n"
        "JF 管理世界\n"
        "YR 2020\n"
        "VO 36\n"
        "IS 10\n"
        "SP 65\n"
        "OP 65-76\n"
        "AB 第一行摘要内容\n"
        "续行摘要内容\n"
        "K1 数字经济;高质量发展\n"
        "ER\n"
        "RT Journal Article\n"
        "T1 第二篇测试论文\n"
        "SP 10\n"
        "EP 22\n"
        "ER\n"
    )
    items = _parse_refworks_export(raw)
    assert len(items) == 2
    assert items[0]["pages"] == "65-76"
    assert items[0]["abstract"] == "第一行摘要内容 续行摘要内容"
    assert items[1]["pages"] == "10-22"


def test_auth_cnki_no_false_positive_on_header_buttons_and_detects_personal_login():
    from cnki import driver as drv_mod

    class _UnauthHeaderDrv:
        current_url = "https://kns.cnki.net/"
        title = "中国知网"

        def execute_script(self, script):
            if "innerText" in script and "document.body" in script:
                return "总库 检索 登录 个人登录 机构登录 IP登录 注册"
            return ""

        def get_cookies(self):
            return [{"name": "Ecp_ClientId", "value": "abc"}]

    unauth = _UnauthHeaderDrv()
    assert drv_mod._has_cnki_institution_access(unauth) is False
    assert drv_mod._has_cnki_personal_login(unauth) is False
    assert drv_mod._has_cnki_any_access(unauth) is False

    class _PersonalLoggedDrv(_UnauthHeaderDrv):
        def get_cookies(self):
            return [{"name": "Ecp_LoginStuts", "value": '{"IsAutoLogin":true,"UserName":"u123","ShowName":"张同学"}'}]

    personal = _PersonalLoggedDrv()
    assert drv_mod._has_cnki_personal_login(personal) is True
    assert drv_mod._has_cnki_any_access(personal, institution=None) is True
    # 若显式要求特定高校机构认证，个人登录不应冒充该机构
    assert drv_mod._has_cnki_any_access(personal, institution="北京大学") is False


def test_download_self_heals_expired_kcms2_url_and_open_download_tab(monkeypatch):
    from cnki import detail as det_mod, download as dl_mod, search as s_mod

    navigated = []
    monkeypatch.setattr(det_mod, "_is_cnki_error_or_404", lambda d: True)
    monkeypatch.setattr(s_mod, "refresh_cnki_url", lambda d, **kw: "https://kns.cnki.net/kcms2/article/abstract?v=fresh999")
    monkeypatch.setattr(dl_mod, "_navigate_within_cnki", lambda d, u, **kw: navigated.append(u))
    monkeypatch.setattr(dl_mod, "_handle_captcha", lambda d: d)

    hint = {"title": "数字经济与高质量发展", "journal": "管理世界", "url": "https://kns.cnki.net/kcms2/article/abstract?v=expired"}
    healed = dl_mod._heal_expired_download_url(object(), hint["url"], hint)
    assert healed.endswith("fresh999")
    assert hint["url"].endswith("fresh999")
    assert navigated == ["https://kns.cnki.net/kcms2/article/abstract?v=fresh999"]


def test_thesis_frontmatter_toc_and_partial_cache_guard(monkeypatch):
    from cnki import detail as det_mod
    from sections import extract_frontmatter_toc

    frontmatter = (
        "[第 4 页]\n"
        "目 录\n"
        "第一章 绪论 .................... 1\n"
        "1.1 研究背景与意义 ............. 2\n"
        "1.2 文献综述 ................... 8\n"
        "第二章 理论基础与研究假说 ...... 21\n"
        "2.1 核心概念界定 ............... 22\n"
        "第三章 研究设计与实证检验 ...... 45\n"
        "第四章 研究结论与政策建议 ...... 98\n"
    )
    fm_toc = extract_frontmatter_toc(frontmatter)
    assert len(fm_toc) == 7
    assert fm_toc[0]["level"] == 1 and fm_toc[0]["page"] == 1
    assert fm_toc[1]["level"] == 2 and fm_toc[1]["page"] == 2
    assert fm_toc[-1]["page"] == 98

    # 验证详情页已提取到原生分章目录时，toc_only=True 零标签页直接完成，绝不打开 HTML/FlowPDF
    opened_tabs = []
    saved_caches = []

    class _DummyDrv:
        current_url = "https://kns.cnki.net/kns8s/AdvSearch"
        _scholar_was_warm = True

    monkeypatch.setattr(det_mod, "_detect_browser", lambda: "chrome")
    monkeypatch.setattr(det_mod, "_create_driver", lambda browser=None: _DummyDrv())
    monkeypatch.setattr(det_mod, "_prepare_cnki_session", lambda d: None)
    monkeypatch.setattr(det_mod, "_save_cookies", lambda d: None)
    monkeypatch.setattr(det_mod, "_navigate_within_cnki", lambda d, u, **k: None)
    monkeypatch.setattr(det_mod, "_handle_captcha", lambda d: d)
    monkeypatch.setattr(det_mod, "_is_cnki_error_or_404", lambda d: False)
    monkeypatch.setattr(
        det_mod,
        "_parse_detail_page",
        lambda d: {
            "abstract": "博士论文摘要",
            "native_toc": [
                {"id": "S1", "index": 1, "level": 1, "title": "第一章 绪论", "page": 1},
                {"id": "S2", "index": 2, "level": 1, "title": "第二章 实证分析", "page": 35},
                {"id": "S3", "index": 3, "level": 1, "title": "第三章 结论", "page": 88},
            ],
        },
    )
    monkeypatch.setattr(det_mod, "_load_cached_fulltext", lambda *a, **k: None)
    monkeypatch.setattr(det_mod, "_read_fulltext_html", lambda *a, **k: opened_tabs.append("html"))
    monkeypatch.setattr(det_mod, "_read_fulltext_flowpdf", lambda *a, **k: opened_tabs.append("flowpdf"))
    monkeypatch.setattr(det_mod, "_save_cached_fulltext", lambda *a, **k: saved_caches.append(1))
    monkeypatch.setattr(det_mod, "_release_driver", lambda d: None)

    papers = [{"title": "测试博士论文", "source": "CNKI", "url": "https://kns.cnki.net/kcms2/article/abstract?v=phd1"}]
    det_mod.batch_read_detail(papers, top_n=1, fulltext=True, toc_only=True)
    assert opened_tabs == []
    assert saved_caches == []
    assert len(papers[0]["native_toc"]) == 3


def test_shared_browser_file_lock_serializes_and_respects_busy_on_reaper(tmp_path, monkeypatch):
    import config
    from cnki import driver as drv_mod

    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "home"))
    config.reset()
    try:
        fh1 = drv_mod._acquire_browser_lock(19333, timeout=1.0)
        assert fh1 not in (None, False)
        # 非阻塞二次获取应立即返回 False（证明互斥锁生效）
        fh2 = drv_mod._acquire_browser_lock(19333, timeout=0.0)
        assert fh2 is False
        # 空闲回收器在锁被占用时必须返回 busy 跳过，绝不强关正在工作的浏览器
        busy_res = drv_mod.close_shared_browser(19333, respect_lock=True)
        assert busy_res["closed"] is False
        assert busy_res.get("reason") == "busy"
        drv_mod._release_browser_lock(fh1)
        fh3 = drv_mod._acquire_browser_lock(19333, timeout=0.5)
        assert fh3 not in (None, False)
        drv_mod._release_browser_lock(fh3)
    finally:
        config.reset()


def test_cdp_tab_cleanup_never_calls_switch_to_window(monkeypatch):
    from cnki import driver as drv_mod

    closed_targets = []

    class _ReusedSilentDrv:
        _scholar_reused = True
        _scholar_lock_fh = None
        current_window_handle = "CDwindow-AAA"
        window_handles = ["CDwindow-AAA", "CDwindow-BBB"]

        class _Sw:
            def window(self, h):
                raise AssertionError("switch_to.window must NOT be called during CDP tab cleanup")

        switch_to = _Sw()

        def get_cookies(self):
            return []

        def execute_cdp_cmd(self, cmd, params):
            if cmd == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"type": "page", "targetId": "AAA"},
                        {"type": "page", "targetId": "BBB"},
                    ]
                }
            if cmd == "Target.closeTarget":
                closed_targets.append(params["targetId"])
                return {}
            return {}

    monkeypatch.setattr(drv_mod, "_save_cookies", lambda d: None)
    drv_mod._release_driver(_ReusedSilentDrv())
    assert closed_targets == ["BBB"]
