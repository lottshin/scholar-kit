from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from docx import Document

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from cnki.detail import _extract_pub_info, batch_read_detail  # noqa: E402
import doctor  # noqa: E402
import docx_tools  # noqa: E402
import evidence  # noqa: E402
import formatter  # noqa: E402
import search as search_mod  # noqa: E402


class _FakePubDriver:
    def __init__(self, pub_text: str, journal_text: str = ""):
        self._pub_text = pub_text
        self._journal_text = journal_text

    def find_element(self, by, selector):
        if "navi" in selector and self._journal_text:
            return type("El", (), {"text": self._journal_text})()
        raise Exception("not found")

    def find_elements(self, by, selector):
        return []

    def execute_script(self, script, *args):
        return self._pub_text


def test_cnki_detail_issue_never_captures_journal_subtitle_and_skips_cached(monkeypatch):
    # P12: 现代传播(中国传媒大学学报) 与 西南民族大学学报(人文社会科学版) 绝不能被当作期号抓进 issue
    drv1 = _FakePubDriver(
        "现代传播(中国传媒大学学报) . 2021, 43 (09) \n 页码：98-103",
        "现代传播(中国传媒大学学报)",
    )
    d1: dict = {}
    _extract_pub_info(drv1, d1)
    assert d1["journal"] == "现代传播(中国传媒大学学报)"
    assert d1["volume"] == "43"
    assert d1["issue"] == "9"
    assert d1["pages"] == "98-103"

    drv2 = _FakePubDriver(
        "西南民族大学学报(人文社会科学版) . 2024, 45 第11期 \n 页码：144-152",
        "西南民族大学学报(人文社会科学版)",
    )
    d2: dict = {}
    _extract_pub_info(drv2, d2)
    assert d2["issue"] == "11"
    assert d2["pages"] == "144-152"

    # P11: 已含摘要与有效页码的知网文献在 fulltext=False 时应直接跳过，不启动浏览器
    def _forbid_browser(*args, **kwargs):
        raise AssertionError("Should not launch browser when abstract + pages are already present")

    monkeypatch.setattr("cnki.detail._create_driver", _forbid_browser)
    already_cached = [{
        "title": "非遗短视频传播研究",
        "source": "cnki",
        "url": "https://kns.cnki.net/kcms2/article/abstract?v=abc",
        "abstract": "本文探讨非遗短视频的符号重构机制与传播路径。",
        "issue": "9",
        "pages": "98-103",
    }]
    res = batch_read_detail(already_cached, top_n=1, fulltext=False)
    assert res[0]["pages"] == "98-103"


def test_formatter_scientific_reports_j_tag_western_initials_and_name_cleaning():
    # P13 + P18: Scientific Reports 不能因含 report 子串被误判为 [R] 丢刊名，且西文作者名应缩写为首字母
    sci_rep = {
        "title": "Emotional resonance and cross-cultural acceptance of Chinese intangible cultural heritage short videos",
        "authors": "Han Cao; David Wei Feng",
        "journal": "Scientific Reports",
        "year": 2024,
        "volume": "14",
        "issue": "1",
        "pages": "24512",
        "doc_type": "journal",
    }
    gb_line = formatter.format_gbt7714(sci_rep, 1)
    assert "[J]. Scientific Reports, 2024, 14(1): 24512." in gb_line
    assert "[R]" not in gb_line
    assert "Cao H, Feng D W." in gb_line

    # P6 + P18: OpenAlex 倒序中文名、刊名尾句号、斜杠重复刊名、_renumber_raw_reference 空格修正
    oa_cn = {
        "title": "Cross-cultural communication of Xiang embroidery",
        "authors": "子萱 廖; 战花 赵; 烽 杨",
        "journal": "Advances in journalism and communication/Advances in journalism and communication.",
        "year": 2023,
        "volume": "11",
        "issue": "3",
        "pages": "45-52",
    }
    gb_oa = formatter.format_gbt7714(oa_cn, 2)
    assert gb_oa.startswith("[2] 廖子萱, 赵战花, 杨烽.")
    assert "Advances in journalism and communication, 2023, 11(3): 45-52." in gb_oa
    assert ".." not in gb_oa
    assert "/" not in gb_oa

    assert formatter._renumber_raw_reference("[1]杨红.题名[J].现代传播,2021(9):1-6.", 5).startswith("[5] 杨红.")


def test_write_docx_renders_native_three_line_tables_and_hanging_refs(tmp_path):
    md = (
        "# 符号博弈与文化调适\n\n"
        "## 一、媒介形态对比\n\n"
        "表1 传统线下展演与短视频媒介文化对比\n\n"
        "| 比较维度 | 传统线下展演 | 短视频媒介文化 |\n"
        "| :--- | :--- | :--- |\n"
        "| 时空场域 | 地方性在场仪式 | 算法流脱域传播[1] |\n"
        "| 符号结构 | 完整技艺叙事 | 高光奇观切片[2] |\n\n"
        "## 参考文献\n\n"
        "[1] 杨红. 短视频与非遗传播[J]. 现代传播, 2021, 43(9): 98-103.\n"
        "[2] 祁庆富. 论非遗保护的本真性原则[J]. 民族艺术, 2018(3): 12-19.\n"
    )
    docx_path = tmp_path / "out.docx"

    res = docx_tools._write_docx_from_markdown(md, docx_path)
    assert res["status"] == "success"
    assert res["tables"] == 1

    doc = Document(str(docx_path))
    assert len(doc.tables) == 1
    tbl = doc.tables[0]
    assert len(tbl.rows) == 3
    assert tbl.cell(0, 0).text.strip() == "比较维度"
    assert "算法流脱域传播[1]" in tbl.cell(1, 2).text

    tbl_xml = tbl._tbl.xml
    assert "tblBorders" in tbl_xml
    assert 'w:sz="12"' in tbl_xml

    para_texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert any(t.startswith("[1] 杨红.") for t in para_texts)
    assert not any("| :---" in t for t in para_texts)


def test_validate_handles_abstract_tables_commentary_and_cross_lingual_citations():
    papers = [
        {
            "title": "非遗短视频的仪式传播与文化认同建构",
            "authors": "杨红; 张明",
            "year": 2021,
            "journal": "现代传播",
            "abstract": "本研究考察非遗短视频在跨媒介转换中的视觉奇观化剪辑、仪式感重构与受众文化认同机制。",
            "keywords": ["非遗短视频", "视觉奇观", "文化认同"],
            "pages": "98-103",
        },
        {
            "title": "Social semiotics and multimodal framing of Chinese intangible heritage on TikTok",
            "authors": "David Wei Feng; Yan Wang",
            "year": 2023,
            "journal": "Social Semiotics",
            "abstract": "Using multimodal discourse analysis, this article examines how TikTok short videos recontextualize Chinese intangible cultural heritage symbols for global audiences.",
            "keywords": ["multimodal", "TikTok", "intangible heritage"],
            "pages": "310-328",
        },
        {
            "title": "湖湘传统手工艺数字化保护路径探讨",
            "authors": "李四",
            "year": 2022,
            "journal": "民族艺术",
            "abstract": "探讨非遗短视频语境下湘绣与滩头年画数字化存档。",
            "keywords": ["湘绣", "滩头年画", "非遗短视频"],
            "pages": "40-48",
        },
    ]
    md = (
        "# 符号博弈与文化调适\n\n"
        "**摘要**：本文围绕非遗短视频出海面临的奇观化与碎片化困境展开理论分析与机制建构。\n\n"
        "**关键词**：非遗短视频；跨文化传播；符号博弈；文化调适\n\n"
        "## 一、文献回顾与问题提出\n\n"
        "表1 核心分析维度对照\n\n"
        "| 维度 | 内涵 |\n"
        "| --- | --- |\n"
        "| 符号层 | 视觉奇观化 |\n\n"
        "非遗短视频通过视觉奇观化剪辑重构了传统仪式的媒介呈现并影响受众文化认同[1]。"
        "由此可见，技术可供性在降低接触门槛的同时也深刻改变了地方性知识的编码秩序。"
        "在海外 TikTok 语境下，多模态符号再语境化成为连接本土非遗与异质受众的关键机制[2]。\n\n"
        "大量实证调查数据显示，高达87.6%的海外受众在3秒内划走非遗视频。\n"
    )
    candidates = evidence._review_candidates(papers, "非遗短视频跨文化传播")
    report = evidence._validate_writing(md, candidates, candidates)
    counts = report["support_counts"]
    # 摘要、关键词、表头不应进入 unsupported；同段作者推演句应归为 commentary；跨语种引用应为 medium
    assert counts.get("commentary", 0) >= 1
    assert counts.get("medium", 0) >= 1
    # 未挂引文却声称“大量实证调查数据显示...”的句子必须被抓出为 unsupported
    assert counts.get("unsupported", 0) == 1
    unsup_examples = [it for iss in report["issues"] if iss["type"] == "unsupported_claim" for it in iss["examples"]]
    assert len(unsup_examples) == 1
    assert "87.6%" in unsup_examples[0]["claim"]
    unused_issues = [iss for iss in report["issues"] if iss["type"] == "unused_relevant_evidence"]
    assert unused_issues and unused_issues[0]["count"] == 1


def test_bilingual_dedupe_and_humanities_gap_methods():
    papers = [
        {
            "title": "非物质文化遗产短视频跨文化传播策略研究",
            "authors": "赵战花; 杨烽",
            "year": 2023,
            "journal": "当代传播",
            "abstract": "深入剖析非遗短视频采用民族志与多模态话语分析及跨文化个案比较研究方法的传播路径。",
            "pages": "60-65",
        },
        {
            "title": "Research on Cross-cultural Communication Strategies of Intangible Cultural Heritage Short Videos",
            "authors": "战花 赵; 烽 杨",
            "year": 2023,
            "journal": "Contemporary Communication",
            "doi": "10.1234/cc.2023.01",
        },
        {
            "title": "另一篇同作者同年发表的独立中文论文：数字记忆与乡村叙事",
            "authors": "赵战花; 杨烽",
            "year": 2023,
            "journal": "中国出版",
            "abstract": "采用符号学与文本分析做跨媒介对比考察乡村记忆。",
            "pages": "12-18",
        },
    ]
    deduped, removed = evidence._dedupe_library_papers(papers)
    assert removed == 1
    assert len(deduped) == 2
    assert deduped[0]["doi"] == "10.1234/cc.2023.01"

    # P10: 民族志/多模态话语分析/符号学/跨文化比较均已被正确识别为方法与比较支撑，不会误报“实证偏少”空白
    gaps = evidence._review_gaps(deduped, "非遗短视频")
    gap_dims = {g["dimension"] for g in gaps}
    assert "方法支撑" not in gap_dims
    assert "比较/整合" not in gap_dims


def test_cite_from_file_and_atomic_rewrite_output(tmp_path):
    ws = tmp_path / "ws"
    proj_dir = ws / ".scholar-kit" / "projects" / "demo"
    proj_dir.mkdir(parents=True)

    surnames = ["赵", "钱", "孙", "李", "周", "吴", "郑", "王", "冯", "陈"]
    givens = ["甲", "乙", "丙", "丁"]
    # 构造 40 篇课题库文献，验证 [35, 2] -> [1, 2] 且原本的 [1] 映射为 [3] 绝不发生级联覆盖冲突
    session_papers = []
    for idx in range(1, 41):
        author_name = surnames[(idx - 1) % 10] + givens[(idx - 1) // 10]
        session_papers.append({
            "title": f"测试学术论文第{idx}号",
            "authors": author_name,
            "journal": "中国社会科学",
            "year": 2020 + (idx % 5),
            "volume": "40",
            "issue": str((idx % 12) + 1),
            "pages": f"{idx * 2}-{idx * 2 + 5}",
            "doc_type": "journal",
        })
    (proj_dir / "session.json").write_text(json.dumps(session_papers, ensure_ascii=False), encoding="utf-8")

    draft = (
        "# 测试论文\n\n"
        "首处复合引用课题库第35与第2篇[35, 2]，随后引用课题库第1篇[1]，"
        "再次复引第35篇[35]，以及连续区间[10-12]，正文中出现的年份如[2024]不应被改动。\n\n"
        "## 参考文献\n\n"
        "[1] 旧参考文献条目应被自动替换.\n"
    )
    draft_p = ws / "draft.md"
    final_p = ws / "final.md"
    draft_p.write_text(draft, encoding="utf-8")

    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "literature.py"),
        "cite",
        "--project", "demo",
        "--from-file", str(draft_p),
        "--rewrite-output", str(final_p),
        "--no-enrich",
        "--style", "gbt7714",
    ]
    proc = subprocess.run(cmd, cwd=str(ws), capture_output=True, text=True, check=True)
    payload = json.loads(proc.stdout)
    assert payload["status"] == "success"
    assert payload["selected_indices"] == [35, 2, 1, 10, 11, 12]
    assert payload["count"] == 6

    rewritten = final_p.read_text(encoding="utf-8")
    assert "首处复合引用课题库第35与第2篇[1, 2]，随后引用课题库第1篇[3]，再次复引第35篇[1]，以及连续区间[4-6]" in rewritten
    assert "[2024]" in rewritten
    assert "旧参考文献条目" not in rewritten
    assert "[1] 周丁. 测试学术论文第35号[J]." in rewritten
    assert "[3] 赵甲. 测试学术论文第1号[J]." in rewritten


def test_openalex_title_abstract_default_venue_tier_and_doctor_tag_version(monkeypatch):
    captured_params = []

    def _fake_http_get(url, params=None, headers=None, timeout=15):
        captured_params.append((url, dict(params or {})))
        return {
            "results": [
                {
                    "title": "Social semiotics of intangible heritage on TikTok",
                    "publication_year": 2023,
                    "cited_by_count": 28,
                    "doi": "https://doi.org/10.1080/10350330.2023.1",
                    "authorships": [{"author": {"display_name": "Yan Wang"}}],
                    "biblio": {"volume": "33", "issue": "4", "first_page": "10", "last_page": "25"},
                    "abstract_inverted_index": {"Multimodal": [0], "semiotics": [1]},
                    "primary_location": {
                        "source": {
                            "display_name": "Social Semiotics",
                            "host_organization_name": "Taylor & Francis",
                            "is_in_doaj": False,
                        }
                    },
                },
                {
                    "title": "Short video marketing note",
                    "publication_year": 2024,
                    "cited_by_count": 0,
                    "doi": "https://doi.org/10.23977/mcr.2024.01",
                    "authorships": [{"author": {"display_name": "San Zhang"}}],
                    "biblio": {"volume": "7", "issue": "1", "first_page": "1", "last_page": "5"},
                    "abstract_inverted_index": {"Short": [0], "video": [1]},
                    "primary_location": {
                        "source": {
                            "display_name": "Media and Communication Research",
                            "host_organization_name": "Clausius Scientific Press",
                            "is_in_doaj": False,
                        }
                    },
                },
            ]
        }

    monkeypatch.setattr(search_mod, "_http_get", _fake_http_get)
    monkeypatch.setattr(search_mod, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(search_mod, "_cache_set", lambda *a, **k: None)

    items = search_mod.search_openalex("intangible heritage TikTok", limit=5)
    assert len(captured_params) == 1
    assert "title_and_abstract.search:intangible heritage TikTok" in captured_params[0][1].get("filter", "")
    assert "search" not in captured_params[0][1]
    assert items[0]["venue_tier"] == "mainstream"
    assert items[0]["publisher"] == "Taylor & Francis"
    assert items[1]["venue_tier"] == "general"

    # P3: doctor.check_update 当 release 停留在旧版 v1.1.0 而最新 tag 为 v1.25.0 时应取二者更高版本
    def _fake_fetch(url, timeout=5):
        if "/releases/latest" in url:
            return {"tag_name": "v1.1.0"}
        return [{"name": "v1.25.0"}]

    upd = doctor.check_update("1.20.0", fetch_fn=_fake_fetch)
    assert upd["latest"] == "1.25.0"
    assert upd["update_available"] is True


def test_show_browser_unminimizes_via_cdp_and_activates_macos_pid(monkeypatch):
    from cnki import driver as drv_mod

    cdp_calls = []
    osascript_calls = []

    class _FakeMinimizedDriver:
        current_window_handle = "win-main"
        capabilities = {"browserName": "chrome"}

        def execute_cdp_cmd(self, cmd, params):
            cdp_calls.append((cmd, params))
            if cmd == "Browser.getWindowForTarget":
                return {"windowId": 42}
            return {}

        def switch_to(self):
            return self

        def execute_script(self, s):
            return None

    fake_drv = _FakeMinimizedDriver()
    fake_drv.switch_to = type("Sw", (), {"window": lambda self, h: None})()

    monkeypatch.setattr(drv_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        "subprocess.check_output",
        lambda *a, **k: " 54321 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222\n"
                        " 54322 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome Helper --type=renderer --remote-debugging-port=9222\n",
    )

    def _fake_run(cmd, *a, **k):
        osascript_calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "ok\n"})()

    monkeypatch.setattr("subprocess.run", _fake_run)

    drv_mod._show_browser(fake_drv)

    assert ("Browser.getWindowForTarget", {}) in cdp_calls
    assert any(
        c == "Browser.setWindowBounds" and p.get("bounds", {}).get("windowState") == "normal"
        for c, p in cdp_calls
    )
    assert ("Page.bringToFront", {}) in cdp_calls
    assert len(osascript_calls) == 1
    assert "runningApplicationWithProcessIdentifier(54321)" in osascript_calls[0][-1]
    assert "activateWithOptions(3)" in osascript_calls[0][-1]
