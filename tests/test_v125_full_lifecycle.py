import argparse
import io
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config  # noqa: E402
import evidence  # noqa: E402
import literature  # noqa: E402
import research_tools  # noqa: E402
from workflows import render_workflow_argv  # noqa: E402


def test_extract_paper_facets_comparison_matrix_and_cluster_divergence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "gh"))
    config.reset()

    papers = [
        {
            "title": "数字经济与企业绿色技术创新",
            "authors": "赵涛; 张智",
            "journal": "管理世界",
            "year": 2022,
            "core_type": "北大核心;CSSCI",
            "keywords": ["数字经济", "绿色创新"],
            "abstract": (
                "在全球低碳转型背景下，数字化浪潮备受关注。"
                "本文基于2011—2021年中国286个地级市面板数据，采用多期双重差分模型检验政策效应。"
                "实证结果表明，数字经济显著促进了企业绿色技术创新，缓解融资约束是核心机制。"
            ),
            "source": "CNKI",
        },
        {
            "title": "资源约束下数字化转型的绿色挤出风险",
            "authors": "李四; 王五",
            "journal": "中国工业经济",
            "year": 2024,
            "core_type": "北大核心;CSSCI",
            "keywords": ["数字经济", "绿色创新"],
            "abstract": (
                "利用沪深A股制造业上市公司数据与工具变量法检验转型阵痛。"
                "研究发现，短期高强度算力投入显著抑制了中小企业绿色创新研发投入。"
            ),
            "source": "CNKI",
        },
    ]
    evidence._save_session(papers, project="div_proj")

    f1 = evidence.extract_paper_facets(papers[0])
    assert "双重差分" in f1["research_method"]
    assert "2011—2021年中国286个地级市面板数据" in f1["data_sample"]
    assert "显著促进了企业绿色技术创新" in f1["core_finding"]
    assert "全球低碳转型背景" not in f1["core_finding"]
    assert f1["effect_polarity"] == "positive"

    out = {}
    literature.cmd_review(
        argparse.Namespace(
            topic="数字经济与绿色创新",
            project="div_proj",
            limit=10,
            output=None,
            auto_detail=False,
            detail_top_n=5,
            cluster=True,
            gaps=True,
            verify_gaps=False,
            raw=False,
        ),
        # cmd_review in literature wraps evidence.cmd_review
    ) if False else evidence.cmd_review(
        argparse.Namespace(
            topic="数字经济与绿色创新",
            project="div_proj",
            limit=10,
            output=None,
            auto_detail=False,
            detail_top_n=5,
            cluster=True,
            gaps=True,
            verify_gaps=False,
            raw=False,
        ),
        output_fn=out.update,
    )

    assert len(out["comparison_matrix"]) == 2
    assert "## 文献要素对比矩阵" in out["markdown"]
    assert any(c.get("divergence_note") for c in out["clusters"])


def test_review_live_gap_verification_warns_on_crowded_pseudo_gap(monkeypatch):
    monkeypatch.setattr(
        evidence,
        "_probe_openalex_hit_count",
        lambda q, yr: {
            "database": "OpenAlex",
            "query": q,
            "year_range": f"{yr}-2026",
            "total_hits": 420,
            "verdict": "already_crowded",
        },
    )
    gaps = [{
        "title": "跨情境比较或系统整合视角较少",
        "dimension": "比较/整合",
        "matched_count": 0,
        "total": 10,
        "evidence_indices": [],
        "basis": "当前库未见比较文献。",
    }]
    verified = evidence._verify_gaps_live(gaps, "digital economy")
    assert verified[0]["search_proof"]["total_hits"] == 420
    assert "严禁直接断言学界空白" in verified[0]["basis"]


def test_validate_catches_polarity_reversal_year_mismatch_and_unbacked_stats(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "gh"))
    config.reset()

    papers = [{
        "title": "数字经济与企业绿色技术创新",
        "authors": "赵涛; 张智",
        "journal": "管理世界",
        "year": 2020,
        "keywords": ["数字经济", "绿色创新"],
        "abstract": "实证结果表明，数字经济显著促进了企业绿色技术创新。",
        "source": "CNKI",
    }]
    evidence._save_session(papers, project="val_proj")

    draft = tmp_path / "bad_draft.md"
    draft.write_text(
        "# 文献综述\n\n"
        "赵涛等（2022）[1]的实证表明，数字经济显著抑制了企业绿色技术创新。\n\n"
        "此外，调查覆盖问卷 680 份，回归系数为 0.342 (p<0.01)，样本量 N=680。\n",
        encoding="utf-8",
    )

    res = {}
    evidence.cmd_validate(
        argparse.Namespace(
            project="val_proj",
            topic="数字经济",
            limit=10,
            file=str(draft),
            allow_primary_data=False,
        ),
        output_fn=res.update,
    )
    issue_types = {iss["type"] for iss in res["issues"]}
    assert res["status"] == "warning"
    assert "polarity_conflict" in issue_types
    assert "year_mismatch" in issue_types
    assert "unbacked_statistics" in issue_types

    # 开启 --allow-primary-data 后，作者自有实证统计不再误报 unbacked_statistics
    res_allow = {}
    evidence.cmd_validate(
        argparse.Namespace(
            project="val_proj",
            topic="数字经济",
            limit=10,
            file=str(draft),
            allow_primary_data=True,
        ),
        output_fn=res_allow.update,
    )
    allow_issue_types = {iss["type"] for iss in res_allow["issues"]}
    assert "unbacked_statistics" not in allow_issue_types
    assert "polarity_conflict" in allow_issue_types


def test_data_table_summary_corr_regression_and_comparison_modes(tmp_path):
    csv_f = tmp_path / "panel.csv"
    csv_f.write_text(
        "firm,tfp,dig,lev\n"
        "A,1.10,0.20,0.45\n"
        "B,1.35,0.42,0.40\n"
        "C,1.58,0.61,0.38\n"
        "D,1.82,0.79,0.33\n"
        "E,2.05,0.95,0.29\n"
        "F,2.30,1.12,0.25\n",
        encoding="utf-8",
    )

    # 1. summary -> docx 三线表
    docx_out = tmp_path / "table1.docx"
    sum_res = {}
    research_tools.cmd_data_table(
        argparse.Namespace(
            filepath=str(csv_f),
            type="summary",
            columns=None,
            digits=3,
            lang="zh",
            caption="表1 主要变量描述性统计",
            format="docx",
            output=str(docx_out),
            raw=False,
        ),
        output_fn=sum_res.update,
    )
    assert sum_res["status"] == "success"
    assert docx_out.exists()
    assert "tfp" in sum_res["markdown"]

    # 2. corr -> 显著性星号与 LaTeX booktabs
    corr_res = {}
    research_tools.cmd_data_table(
        argparse.Namespace(
            filepath=str(csv_f),
            type="corr",
            columns="tfp,dig,lev",
            digits=3,
            lang="en",
            caption="Table 2 Correlation Matrix",
            format="latex",
            output=None,
            raw=False,
        ),
        output_fn=corr_res.update,
    )
    assert "***" in corr_res["markdown"]
    assert r"\toprule" in corr_res["latex"]

    # 3. regression -> 双行系数+标准误
    reg_f = tmp_path / "reg.json"
    reg_f.write_text(
        json.dumps({
            "models": [
                {
                    "name": "(1) OLS",
                    "n": 2860,
                    "r2": 0.312,
                    "controls": {"企业固定效应": "否", "年份固定效应": "是"},
                    "coefs": {"DID": {"coef": 0.0912, "se": 0.0201}},
                },
                {
                    "name": "(2) FE",
                    "n": 2860,
                    "r2": 0.548,
                    "controls": {"企业固定效应": "是", "年份固定效应": "是"},
                    "coefs": {"DID": {"coef": 0.0764, "se": 0.0185}},
                },
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    reg_res = {}
    research_tools.cmd_data_table(
        argparse.Namespace(
            filepath=str(reg_f),
            type="regression",
            columns=None,
            digits=4,
            lang="zh",
            caption="表3 基准回归结果",
            format="markdown",
            output=None,
            raw=False,
        ),
        output_fn=reg_res.update,
    )
    assert "0.0912***" in reg_res["markdown"]
    assert "(0.0201)" in reg_res["markdown"]
    assert "2860" in reg_res["markdown"]

    # 4. comparison -> 自动区分高优指标 (Acc ↑) 与低优指标 (RMSE ↓)
    cmp_f = tmp_path / "bench.csv"
    cmp_f.write_text(
        "Method,Accuracy,RMSE\n"
        "Baseline,81.2,0.45\n"
        "RunnerUp,86.5,0.31\n"
        "Ours,89.4,0.22\n",
        encoding="utf-8",
    )
    cmp_res = {}
    research_tools.cmd_data_table(
        argparse.Namespace(
            filepath=str(cmp_f),
            type="comparison",
            columns=None,
            digits=2,
            lang="en",
            caption="",
            format="latex",
            output=None,
            raw=False,
        ),
        output_fn=cmp_res.update,
    )
    assert "**89.40**" in cmp_res["markdown"]
    assert "**0.22**" in cmp_res["markdown"]
    assert r"\textbf{89.40}" in cmp_res["latex"]
    assert r"\underline{0.31}" in cmp_res["latex"]


def test_match_journal_recommends_tiers_and_quarantines_warning_journals(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path / "gh"))
    config.reset()

    evidence._save_session([
        {"title": "论文A", "journal": "管理世界", "core_type": "北大核心;CSSCI", "cited_by": 320, "source": "CNKI"},
        {"title": "论文B", "journal": "中国软科学", "core_type": "北大核心;CSSCI", "cited_by": 45, "source": "CNKI"},
        {"title": "论文C", "journal": "中国软科学", "core_type": "北大核心;CSSCI", "cited_by": 38, "source": "CNKI"},
        {"title": "Paper D", "journal": "Computers and Electrical Engineering", "cited_by": 12, "source": "OpenAlex"},
        {"title": "Paper E", "journal": "Frontiers in Earth Science", "cited_by": 5, "source": "OpenAlex"},
    ], project="sub_proj")

    res = {}
    research_tools.cmd_match_journal(
        argparse.Namespace(
            project="sub_proj",
            from_file=None,
            topic=None,
            live=False,
            lang="all",
            limit=10,
            raw=False,
        ),
        output_fn=res.update,
    )
    assert res["status"] == "success"
    rec_map = {r["journal"]: r for r in res["recommendations"]}
    warn_names = [w["journal"] for w in res["excluded_warning_venues"]]
    assert "管理世界" in rec_map
    assert res["recommendations"][0]["tier"] == "tier_1_flagship"
    assert "Computers and Electrical Engineering" in warn_names
    assert "Computers and Electrical Engineering" not in rec_map
    assert rec_map["Frontiers in Earth Science"]["tier"] == "tier_3_broad_core"
    assert "曾列中科院预警" in rec_map["Frontiers in Earth Science"].get("cas_warning_history", "")


def test_rebuttal_parses_comments_matches_docx_anchors_and_dry_runs_patch(tmp_path):
    ms_docx = tmp_path / "manuscript.docx"
    doc = Document()
    doc.add_heading("数字化转型与企业全要素生产率", level=1)
    doc.add_paragraph("引言部分回顾了宏观经济增长文献，但尚未充分讨论近年人工智能应用文献。")
    doc.add_paragraph("实证部分采用双向固定效应模型检验数字化转型对企业生产率的影响，但尚未引入工具变量处理反向因果。")
    doc.save(str(ms_docx))

    comments_txt = tmp_path / "reviewers.txt"
    comments_txt.write_text(
        "审稿专家一：\n"
        "本文研究了数字化转型与生产率问题，具有一定参考价值。\n"
        "1. 文献综述部分缺少近三年关于人工智能应用的最新文献，请补充对话并明确边际贡献。\n"
        "2. 实证部分存在反向因果导致的内生性问题，建议补充工具变量检验与稳健性检验。\n\n"
        "Reviewer #2:\n"
        "1) Please clarify the dataset sample construction and add a descriptive statistics table.\n",
        encoding="utf-8",
    )

    patch_json = tmp_path / "rebuttal_patch.json"
    out_letter = tmp_path / "response_letter.docx"
    res = {}
    research_tools.cmd_rebuttal(
        argparse.Namespace(
            filepath=str(comments_txt),
            manuscript=str(ms_docx),
            title="数字化转型与企业全要素生产率",
            lang="zh",
            format="docx",
            output=str(out_letter),
            init_patch=str(patch_json),
            raw=False,
        ),
        output_fn=res.update,
    )
    assert res["status"] == "success"
    assert res["comment_count"] == 3
    assert out_letter.exists()
    assert patch_json.exists()

    # 生成的补丁模板必须能直接通过 patch-docx --dry-run 预检！
    dry_out = io.StringIO()
    with redirect_stdout(dry_out):
        literature.cmd_patch_docx(argparse.Namespace(
            filepath=str(ms_docx),
            patch=str(patch_json),
            output=str(tmp_path / "revised.docx"),
            dry_run=True,
        ))
    dry_payload = json.loads(dry_out.getvalue())
    assert dry_payload["status"] == "success"

    # 新工作流模板渲染校验
    wf_argv = render_workflow_argv(
        "peer_review_rebuttal",
        {
            "comments_file": "rev.txt",
            "manuscript": "draft.docx",
            "output_file": "reply.docx",
            "patch_file": "patch.json",
        },
    )
    assert wf_argv[0][:2] == ["rebuttal", "rev.txt"]


def test_regression_flat_schema_and_significance_stars_preserved_in_latex_and_docx(tmp_path):
    spec_path = tmp_path / "flat_reg.json"
    spec_path.write_text(json.dumps({
        "models": [
            {
                "name": "(1) OLS",
                "coefs": {"Digital": 0.1426, "Size": 0.0834},
                "ses": {"Digital": 0.0312, "Size": 0.0215},
                "n": 18420,
                "r2": 0.284,
                "fe": {"Firm FE": "Yes", "Year FE": "Yes"},
            },
            {
                "name": "(2) IV-2SLS",
                "coefs": {"Digital": 0.1685, "Size": 0.0791},
                "ses": {"Digital": 0.0485, "Size": 0.0209},
                "n": 18420,
                "r2": 0.317,
                "fe": {"Firm FE": "Yes", "Year FE": "Yes"},
            },
        ]
    }), encoding="utf-8")

    docx_path = tmp_path / "reg_table.docx"
    out = {}
    research_tools.cmd_data_table(
        argparse.Namespace(
            filepath=str(spec_path),
            type="regression",
            columns=None,
            digits=4,
            lang="zh",
            caption="表3 基准回归",
            format="docx",
            output=str(docx_path),
            raw=False,
        ),
        output_fn=out.update,
    )
    assert out["status"] == "success"
    assert "0.1426***" in out["markdown"]
    assert "(0.0312)" in out["markdown"]
    assert "Firm FE" in out["markdown"]
    # 显著性星号 ***、**、* 在 LaTeX 表注与 Word 表注中均不得被 Markdown 加粗正则破坏
    assert "***、**、*" in out["latex"]
    assert r"\textbf{*、" not in out["latex"]

    doc = Document(str(docx_path))
    all_para_text = "\n".join(p.text for p in doc.paragraphs)
    assert "***、**、*" in all_para_text
    assert len(doc.tables) == 1
    tbl_xml = doc.tables[0]._tbl.xml
    assert 'w:type="fixed"' in tbl_xml
    assert 'w:w="8311"' in tbl_xml


def test_skill_md_frontmatter_conforms_to_agent_skills_spec():
    skill_md = Path(__file__).resolve().parent.parent / "SKILL.md"
    txt = skill_md.read_text(encoding="utf-8")
    assert txt.startswith("---\n")
    fm = txt.split("---", 2)[1]
    assert "name: scholar-kit" in fm
    assert "license: MIT" in fm
    m = re.search(r"description:\s*>-\n((?:  .*\n)+)", fm)
    assert m is not None
    desc = " ".join(line.strip() for line in m.group(1).splitlines())
    assert 50 <= len(desc) <= 1024, f"SKILL.md description 长度超出 Agent Skills 1024 字符规范: {len(desc)}"
