import argparse
import io
import json
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
import sections  # noqa: E402


def test_gbt7714_author_year_pinyin_sorting_polyphonic_surname_and_year_suffix():
    papers = [
        # 西文应排在全部中文文献之后，且按姓氏字母序 Doudna -> Vaswani
        {
            "title": "Attention Is All You Need",
            "authors": "Ashish Vaswani; Noam Shazeer; Niki Parmar; Jakob Uszkoreit",
            "year": 2017,
            "journal": "NeurIPS",
            "volume": "30",
            "issue": "1",
            "pages": "5998-6008",
        },
        {
            "title": "CRISPR-Cas9 clinical therapy",
            "authors": "Doudna JA, Charpentier E",
            "year": 2024,
            "journal": "Science",
            "volume": "386",
            "issue": "4",
            "pages": "412-420",
        },
        # 赵涛同年两篇应自动标 2020a / 2020b
        {
            "title": "数字经济、创业活跃度与高质量发展",
            "authors": "赵涛; 张智; 梁上坤",
            "year": 2020,
            "journal": "管理世界",
            "volume": "36",
            "issue": "10",
            "pages": "65-76",
        },
        {
            "title": "国家级大数据综合试验区与城市创新",
            "authors": "赵涛; 张智; 李明",
            "year": 2020,
            "journal": "经济研究",
            "volume": "55",
            "issue": "8",
            "pages": "112-128",
        },
        # 多音姓氏：单(shan) 应排在 陈(chen) 之后、曾(zeng) 之前；曾(zeng) 应排在 赵(zhao) 之前（而非按 ceng 排在陈之后）
        {
            "title": "平台治理与数据产权",
            "authors": "曾明",
            "year": 2022,
            "journal": "法学研究",
            "issue": "3",
            "pages": "20-38",
        },
        {
            "title": "人工智能与劳动力市场极化",
            "authors": "陈斌开; 李实",
            "year": 2021,
            "journal": "经济研究",
            "issue": "5",
            "pages": "4-20",
        },
        {
            "title": "县域金融供给研究",
            "authors": "单华",
            "year": 2019,
            "journal": "金融研究",
            "issue": "6",
            "pages": "50-66",
        },
    ]

    ref_text = formatter.generate_reference_list(papers, "gbt7714-author-year")
    lines = ref_text.splitlines()
    assert len(lines) == 7

    # 验证排序：陈(chen) -> 单(shan) -> 曾(zeng) -> 赵(zhao 2020a) -> 赵(zhao 2020b) -> Doudna -> Vaswani
    assert lines[0].startswith("陈斌开, 李实, 2021.")
    assert lines[1].startswith("单华, 2019.")
    assert lines[2].startswith("曾明, 2022.")
    assert "2020a." in lines[3] and lines[3].startswith("赵涛")
    assert "2020b." in lines[4] and lines[4].startswith("赵涛")
    assert lines[5].startswith("Doudna J A, Charpentier E, 2024.")
    assert lines[6].startswith("Vaswani A, Shazeer N, Parmar N, et al., 2017.")


def test_chinese_econ_style_and_in_text_citations(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "gh"))
    config.reset()

    papers = [
        {
            "title": "数字经济、创业活跃度与高质量发展",
            "authors": "赵涛; 张智; 梁上坤",
            "year": 2020,
            "journal": "管理世界",
            "issue": "10",
            "pages": "65-76",
        },
        {
            "title": "国家级大数据试验区与企业全要素生产率",
            "authors": "赵涛; 梁上坤; 王五; 赵六",
            "year": 2020,
            "journal": "经济研究",
            "issue": "08",
            "pages": "100-116",
        },
        {
            "title": "Attention Is All You Need",
            "authors": "Ashish Vaswani; Noam Shazeer; Niki Parmar",
            "year": 2017,
            "journal": "NeurIPS",
            "volume": "30",
            "issue": "1",
            "pages": "5998-6008",
        },
    ]
    evidence._save_session(papers, project="econ_proj")

    out = io.StringIO()
    with redirect_stdout(out):
        literature.cmd_cite(argparse.Namespace(
            style="chinese-econ",
            indices=None,
            keep_numbers=False,
            in_text=True,
            no_enrich=True,
            raw=False,
            project="econ_proj",
        ))
    res = json.loads(out.getvalue())
    assert res["status"] == "success"
    ref_lines = res["references"].splitlines()
    # 经管顶刊体例：顿号分隔作者、冒号书名号、第X期（自动去前导 0）
    assert ref_lines[0] == "赵涛、梁上坤、王五等，2020a：《国家级大数据试验区与企业全要素生产率》，《经济研究》第8期。"
    assert ref_lines[1] == "赵涛、张智、梁上坤，2020b：《数字经济、创业活跃度与高质量发展》，《管理世界》第10期。"
    assert ref_lines[2] == 'Vaswani, A., N. Shazeer, and N. Parmar, 2017, "Attention Is All You Need", NeurIPS, 30(1), 5998-6008.'

    # 验证正文内嵌引文合并（同作者同年自动合并为 赵涛等，2020a，2020b）
    assert res["in_text"]["combined"] == "（赵涛等，2020a，2020b；Vaswani et al., 2017）"
    assert res["in_text"]["items"][0]["narrative"] == "赵涛等（2020a）"


def test_dissertation_toc_ignores_frontmatter_dot_leaders_and_slices_subtree():
    # 构造带前置『目录』页（既有虚线页码，也有无虚线短桩行）的模拟博士学位论文全文
    thesis_ft = "\n".join([
        "[第 1 页]",
        "摘 要",
        "本文系统考察数字基础设施建设对制造业企业绿色技术创新的影响效应及其传导机制。" * 4,
        "[第 3 页]",
        "目 录",
        "第1章 绪论 ........................................ 1",
        "第2章 文献综述与理论假说 .......................... 12",
        "第3章 研究设计与识别策略   25",
        "3.1 模型设定与变量定义 ............................ 25",
        "3.2 数据来源与描述性统计 .......................... 31",
        "第4章 实证结果与稳健性检验 ........................ 38",
        "[第 5 页]",
        "第1章 绪论",
        "在双碳目标与数字中国战略交汇的背景下，理解数字化如何赋能绿色转型具有重要学术价值与现实意义。" * 4,
        "[第 16 页]",
        "第2章 文献综述与理论假说",
        "国内外学者围绕环境规制、数字金融与企业绿色创新开展了丰富讨论，但鲜有文献关注算力基础设施。" * 4,
        "[第 29 页]",
        "第3章 研究设计与识别策略",
        "本章基于准自然实验框架构建多期双重差分模型，并对核心被解释变量与控制变量进行界定。" * 3,
        "3.1 模型设定与变量定义",
        "基准双向固定效应双重差分模型设定如下，被解释变量为企业当年绿色发明专利申请数量对数。" * 4,
        "[第 35 页]",
        "3.2 数据来源与描述性统计",
        "样本选取 2011—2023 年沪深 A 股制造业上市公司，剔除 ST、金融行业与核心财务指标缺失观测。" * 4,
        "[第 42 页]",
        "第4章 实证结果与稳健性检验",
        "表1 基准回归结果",
        "0.142***  0.031  0.138***  0.029",
        "基准回归系数在 1% 水平显著为正，表明数字基础设施显著促进企业绿色创新。" * 4,
    ])

    parsed = sections.parse_fulltext_sections(thesis_ft)
    toc = sections.public_toc(parsed)
    titles = [t["title"] for t in toc]

    # 目录页虚线与短桩行必须被完全过滤，只保留正文的真实章节，且页码对应真实正文页（如第3章在第29页而非第3页）
    assert titles == [
        "摘 要",
        "第1章 绪论",
        "第2章 文献综述与理论假说",
        "第3章 研究设计与识别策略",
        "3.1 模型设定与变量定义",
        "3.2 数据来源与描述性统计",
        "第4章 实证结果与稳健性检验",
    ]
    ch3 = next(t for t in toc if "第3章" in t["title"])
    assert ch3["level"] == 1
    assert ch3["page"] == 29

    # 切一级章『第3章』时必须自动包含下辖子节 3.1 与 3.2，但不混入第4章
    s3 = sections.slice_fulltext_sections(thesis_ft, "第3章")
    assert s3["matched"] is True
    assert "3.1 模型设定与变量定义" in s3["text"]
    assert "3.2 数据来源与描述性统计" in s3["text"]
    assert "第4章 实证结果" not in s3["text"]
    assert s3["text"].startswith("[第 29 页]")

    # 概念同义词扩展：搜『方法』自动命中『研究设计』章节
    s_method = sections.slice_fulltext_sections(thesis_ft, "方法")
    assert s_method["matched"] is True
    assert "双向固定效应双重差分模型" in s_method["text"]


def test_cmd_read_detail_supports_toc_only_and_section_slicing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "gh"))
    config.reset()

    cache_f = config.global_state_dir(create=True) / "fulltext" / "thesis_demo.json"
    cache_f.parent.mkdir(parents=True, exist_ok=True)
    full_body = "\n".join([
        "[第 1 页]",
        "一、引言",
        "关于平台经济反垄断的既有研究主要聚焦于双边市场定价理论与效率抗辩。" * 5,
        "[第 6 页]",
        "二、理论模型与研究假说",
        "（一）数据要素驱动的跨界传导机制",
        "拥有海量用户交互数据的平台企业可通过算法推荐强化网络外部性壁垒。" * 5,
        "[第 12 页]",
        "三、实证检验与结果分析",
        "基于多期面板数据的回归估计显示，互操作性监管显著提升了中小开发者创新进入率。" * 5,
    ])
    cache_f.write_text(json.dumps({"fulltext": full_body}, ensure_ascii=False), encoding="utf-8")

    evidence._save_session([{
        "title": "平台经济互操作性监管研究",
        "authors": "李四",
        "journal": "管理世界",
        "year": 2024,
        "url": "https://kns.cnki.net/kcms2/article/abstract?v=demo123",
        "source": "CNKI",
        "has_fulltext": True,
        "fulltext_length": len(full_body),
        "fulltext_cache": str(cache_f),
    }], project="slice_proj")

    monkeypatch.setattr(literature, "batch_read_detail", lambda papers, top_n=5, fulltext=False: papers)

    # 1. --toc 模式：只返回结构化目录，不把正文塞进 JSON
    out_toc = io.StringIO()
    with redirect_stdout(out_toc), redirect_stderr(io.StringIO()):
        literature.cmd_read_detail(argparse.Namespace(
            top_n=1, indices="1", fulltext=False, toc=True, section=None, max_chars=None, project="slice_proj"
        ))
    res_toc = json.loads(out_toc.getvalue())
    entry_toc = res_toc["results"][0]
    assert entry_toc["toc_only"] is True
    assert "fulltext" not in entry_toc
    assert len(entry_toc["toc"]) == 4

    # 2. --section 模式：只提取『实证检验』章节
    out_sec = io.StringIO()
    with redirect_stdout(out_sec), redirect_stderr(io.StringIO()):
        literature.cmd_read_detail(argparse.Namespace(
            top_n=1, indices="1", fulltext=False, toc=False, section="实证检验", max_chars=None, project="slice_proj"
        ))
    res_sec = json.loads(out_sec.getvalue())
    entry_sec = res_sec["results"][0]
    assert "互操作性监管显著提升了中小开发者创新进入率" in entry_sec["fulltext"]
    assert "一、引言" not in entry_sec["fulltext"]
    assert entry_sec["sliced_length"] < entry_sec["fulltext_length"]


def test_formatter_cleans_affiliation_superscripts_particles_and_master_vs_phd_bibtex():
    papers = [
        {"title": "单作者角标测试", "authors": "陈斌开1,2", "year": 2021, "journal": "经济研究", "issue": "5", "pages": "4-20"},
        {"title": "多作者角标与星号", "authors": "赵涛1,2; 张智1*(通讯作者)", "year": 2020, "journal": "管理世界", "issue": "10", "pages": "65-76"},
        {"title": "博士学位论文测试", "authors": "王五", "year": 2023, "publisher": "清华大学", "doc_type": "doctor"},
        {"title": "硕士学位论文测试", "authors": "李四", "year": 2022, "publisher": "北京大学", "doc_type": "master"},
        {"title": "Asymptotic Statistics", "authors": "Aad van der Vaart; Marie-Curie Dupont", "year": 2019, "journal": "Econometrica", "volume": "87", "issue": "1", "pages": "1-35"},
    ]

    ay = formatter.generate_reference_list(papers, "gbt7714-author-year")
    # 无卷号时 (issue) 直接紧贴刊名，不出现多余逗号；机构角标 1,2/* 被干净剥离
    assert "陈斌开, 2021. 单作者角标测试[J]. 经济研究(5): 4-20." in ay
    assert "赵涛, 张智, 2020." in ay
    assert "van der Vaart A, Dupont M-C, 2019." in ay

    econ = formatter.generate_reference_list(papers, "chinese-econ")
    assert "清华大学博士学位论文。" in econ
    assert "北京大学硕士学位论文。" in econ

    bib = formatter.to_bibtex(papers[2:4])
    assert "@phdthesis{" in bib and "school = {清华大学}" in bib
    assert "@mastersthesis{" in bib and "school = {北京大学}" in bib


def test_sections_suppress_pdf_running_headers_and_support_trailing_dot_subsections():
    ft = "\n".join([
        "[第 1 页]",
        "清华大学博士学位论文",
        "第 1 章  绪 论",
        "本研究关注制造业数字化转型的微观机理与政策效应，具有重要的理论与现实意义。" * 4,
        "[第 2 页]",
        "第1章 绪论",
        "进一步地，现有研究对于供应链溢出效应仍缺乏系统性的因果识别证据。" * 4,
        "[第 3 页]",
        "1.1. 研究背景与意义",
        "全球新一轮科技革命加速演进，工业互联网与人工智能深度嵌入生产制造流程。" * 4,
        "[第 4 页]",
        "第1章 绪论",
        "这里是 1.1 节跨页延续的第二段内容，绝不应该被页眉切断或识别为新的一章！" * 4,
        "[第 5 页]",
        "第 2 章  理 论 分 析",
        "本章从交易成本经济学与资源基础观双重视角构建理论分析框架。" * 4,
    ])
    toc = sections.public_toc(sections.parse_fulltext_sections(ft))
    assert [t["title"] for t in toc] == ["第 1 章 绪 论", "1.1. 研究背景与意义", "第 2 章 理 论 分 析"]
    s11 = sections.slice_fulltext_sections(ft, "1.1")
    assert "跨页延续的第二段内容" in s11["text"]


def test_validate_supports_multi_brackets_ranges_author_year_and_full_session_pool():
    session_20 = [
        {"title": f"背景文献 {i}", "authors": f"路人{i}", "year": 2020, "journal": "测试刊", "abstract": "一般背景介绍" * 6}
        for i in range(1, 19)
    ] + [
        {"title": "数字经济、创业活跃度与高质量发展", "authors": "赵涛; 张智; 梁上坤", "year": 2020, "journal": "管理世界", "abstract": "数字经济通过提升城市创业活跃度显著促进高质量发展。"},
        {"title": "Attention Is All You Need", "authors": "Ashish Vaswani; Noam Shazeer", "year": 2017, "journal": "NeurIPS", "abstract": "Self-attention Transformer architecture replaces recurrence in sequence modeling."},
    ]
    cand = evidence._review_candidates(session_20, "数字经济")

    draft_brackets = "## 综述\n数字经济通过提升城市创业活跃度促进高质量发展[1, 19]。Transformer 自注意力架构替代了循环建模[19-20]。\n"
    v1 = evidence._validate_writing(draft_brackets, cand[:12], all_candidates=cand)
    assert v1["support_counts"].get("invalid", 0) == 0
    assert v1["support_counts"].get("unsupported", 0) == 0

    draft_ay = "## 综述\n赵涛等（2020）指出数字经济通过提升城市创业活跃度显著促进高质量发展。此外，Transformer 自注意力架构有效替代了循环网络（Vaswani et al., 2017）。\n"
    v2 = evidence._validate_writing(draft_ay, cand[:12], all_candidates=cand)
    assert v2["score"] == 100
    assert v2["support_counts"].get("unsupported", 0) == 0


def test_cnki_citation_parser_and_cross_source_dedup_merge_fields():
    from cnki import detail as cnki_detail
    import search

    parsed_j = cnki_detail.parse_cnki_citation_item("[3] 数字经济与高质量发展[J]. 赵涛,张智.管理世界,2020,36(10):65-76+188")
    assert parsed_j["volume"] == "36"
    assert parsed_j["issue"] == "10"
    assert parsed_j["pages"] == "65-76+188"

    parsed_d = cnki_detail.parse_cnki_citation_item("[2] 数字普惠金融研究[D]. 李明.中央财经大学,2023")
    assert parsed_d["doc_type"] == "thesis"
    assert parsed_d["publisher"] == "中央财经大学"

    merged = search.deduplicate_results([
        {"title": "数字经济、创业活跃度与高质量发展——来自中国城市的经验证据", "authors": "赵涛", "year": 2020, "is_core": True, "source": "CNKI"},
        {"title": "数字经济 创业活跃度与高质量发展: 来自中国城市的经验证据", "authors": "赵涛; 张智", "year": 2020, "pages": "65-76", "source": "NSSD"},
        {"title": "Attention Is All You Need.", "authors": "Vaswani A", "year": 2017, "doi": "https://doi.org/10.5555/3295222.3295349", "source": "OpenAlex"},
        {"title": "Attention is all you need", "authors": "Ashish Vaswani", "year": 2017, "doi": "10.5555/3295222.3295349", "pages": "5998-6008", "source": "Semantic Scholar"},
    ])
    assert len(merged) == 2
    assert merged[0]["pages"] == "65-76" and merged[0]["is_core"] is True
    assert merged[1]["pages"] == "5998-6008" and merged[1]["doi"] == "10.5555/3295222.3295349"


def test_cnki_space_separated_cjk_keywords_compile_to_and_clauses():
    from cnki.search import build_cnki_expression

    expr = build_cnki_expression("新质生产力 高质量发展", field="篇关摘")
    assert expr == "TKA='新质生产力' AND TKA='高质量发展'"

    # 英文多词短语保持原整体短语，不按空格误拆
    en_expr = build_cnki_expression("digital economy")
    assert en_expr == "SU='digital economy'"


def test_preprint_author_year_has_no_double_dot_and_detail_pages_clear_online_first():
    preprint = {
        "title": "Attention Is All You Need",
        "authors": "Ashish Vaswani; Noam Shazeer; Niki Parmar; Jakob Uszkoreit",
        "year": 2017,
        "journal": "arXiv (Cornell University)",
        "doc_type": "preprint",
        "doi": "10.48550/arxiv.1706.03762",
        "url": "https://openalex.org/W2626778328",
    }
    ay_ref = formatter.format_gbt7714_author_year(preprint)
    seq_ref = formatter.format_gbt7714(preprint, 1)
    assert ". ." not in ay_ref
    assert ". ." not in seq_ref
    assert "arXiv (Cornell University)" in ay_ref



