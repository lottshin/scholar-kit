"""
evidence.py - 会话/课题文献库、学科中立的主题聚类、研究空白信号、证据骨架与论断校验

职责定位（「脚本是手，Agent 是脑」）：
- 脚本负责确定性证据整理（相关性初筛、基于语料真实关键词的主题聚类、可量化研究空白统计、引用编号与词项支撑核查）
- 正式学术综述撰写与润色优先由 Agent 基于 `review` 返回的结构化证据包及 `references/style-audit.md` 完成，再用 `validate` 闭环核查
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cnki import batch_read_detail
from config import allowed_fulltext_dirs, workspace_state_dir
from docx_tools import _write_docx_from_markdown
from formatter import _extract_western_parts, _split_authors, generate_reference_list


def _default_output(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _is_cnki_paper(paper: dict) -> bool:
    url = paper.get("url", "")
    source = paper.get("source", "")
    return "cnki" in url.lower() or source in ("CNKI", "CNKI-export")


def _safe_project_name(project: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._- 一-鿿" else "_" for c in project.strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "default"


def _project_dir(project: str, create: bool = False) -> Path:
    d = workspace_state_dir(create=create) / "projects" / _safe_project_name(project)
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _session_file(project: Optional[str] = None, create: bool = False) -> Path:
    if project:
        return _project_dir(project, create=create) / "session.json"
    return workspace_state_dir(create=create) / "session.json"


def _session_project(args) -> Optional[str]:
    return getattr(args, "project", None) or None


def _save_session(papers: list, append: bool = False, project: Optional[str] = None) -> None:
    session_file = _session_file(project, create=True)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    if append and session_file.exists():
        try:
            existing = json.loads(session_file.read_text(encoding="utf-8"))
            papers, _ = _dedupe_library_papers(existing + papers)
        except Exception:
            pass
    session_file.write_text(json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_session(project: Optional[str] = None) -> list:
    session_file = _session_file(project, create=False)
    if not session_file.exists():
        return []
    try:
        return json.loads(session_file.read_text(encoding="utf-8"))
    except Exception:
        return []


def _paper_year(paper: Dict[str, Any]) -> Any:
    return paper.get("year") or str(paper.get("date", ""))[:4]


def _paper_summary(paper: Dict[str, Any], index: int) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "index": index,
        "title": paper.get("title", ""),
        "authors": paper.get("authors", ""),
        "journal": paper.get("journal", ""),
        "year": _paper_year(paper),
        "cited_by": paper.get("cited_by", 0),
        "source": paper.get("source", ""),
        "tags": paper.get("tags", []),
        "note": paper.get("note", ""),
    }
    if paper.get("downloads"):
        summary["downloads"] = paper["downloads"]
    if paper.get("local_pdf"):
        summary["local_pdf"] = paper["local_pdf"]
    return summary


def parse_indices_spec(raw: str, total: int, *, preserve_order: bool = True) -> List[int]:
    """解析 1-based 序号表达式（如 `'9,2,5'` 或 `'1,3-6'`）为 0-based 下标列表。"""
    indices: List[int] = []
    seen: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo_i, hi_i = int(lo_s.strip()), int(hi_s.strip())
            step = 1 if hi_i >= lo_i else -1
            seq = range(lo_i - 1, hi_i if step > 0 else hi_i - 2, step)
            for i in seq:
                if 0 <= i < total and i not in seen:
                    seen.add(i)
                    indices.append(i)
        else:
            i = int(part) - 1
            if 0 <= i < total and i not in seen:
                seen.add(i)
                indices.append(i)
    return indices if preserve_order else sorted(indices)


def _normalize_keywords(raw_keywords: Any) -> List[str]:
    if not raw_keywords:
        return []
    if isinstance(raw_keywords, list):
        items = [str(k) for k in raw_keywords]
    else:
        items = re.split(r"[;；,，、/|]+", str(raw_keywords))
    cleaned: List[str] = []
    for item in items:
        token = item.strip(" \t\r\n，。；;、,.()（）[]【】《》<>\"'")
        if len(token) >= 2 and token not in cleaned:
            cleaned.append(token)
    return cleaned


def _paper_evidence(paper: Dict[str, Any], index: int) -> Dict[str, Any]:
    abstract = str(paper.get("abstract") or "").strip()
    keywords = _normalize_keywords(paper.get("keywords"))
    evidence = {
        "index": index,
        "title": paper.get("title", ""),
        "authors": paper.get("authors", ""),
        "journal": paper.get("journal", ""),
        "year": _paper_year(paper),
        "source": paper.get("source", ""),
        "doi": paper.get("doi", ""),
        "url": paper.get("url", ""),
        "keywords": keywords[:8],
        "abstract_excerpt": abstract[:220],
        "trace_status": "abstract" if abstract else "metadata_only",
    }
    if paper.get("pages"):
        evidence["pages"] = paper.get("pages")
    evidence.update(extract_paper_facets(paper))
    return evidence


_METHOD_RULES: List[tuple[str, str]] = [
    (r"双重差分|多期\s*DID|\bDID\b|difference-in-differences|准自然实验", "双重差分 (DID)"),
    (r"工具变量|\b2SLS\b|\bIV\b|instrumental variable", "工具变量 (IV)"),
    (r"断点回归|\bRDD\b|regression discontinuity", "断点回归 (RDD)"),
    (r"合成控制|synthetic control", "合成控制法"),
    (r"空间杜宾|空间计量|空间溢出|\bSDM\b|\bSAR\b|spatial econometr", "空间计量"),
    (r"倾向得分匹配|\bPSM\b|propensity score", "倾向得分匹配 (PSM)"),
    (r"定性比较|\bfsQCA\b|\bQCA\b|组态分析", "定性比较分析 (QCA)"),
    (r"扎根理论|grounded theory", "扎根理论"),
    (r"民族志|田野调查|参与式观察|深度访谈|ethnograph|fieldwork|in-depth interview", "田野调查/质性访谈"),
    (r"多案例|单案例|典型案例|个案研究|case study", "案例研究"),
    (r"结构方程|\bSEM\b|问卷调查|李克特|survey|questionnaire", "问卷调查/SEM"),
    (r"随机对照|\bRCT\b|临床试验|randomized controlled", "随机对照试验 (RCT)"),
    (r"元分析|系统综述|\bmeta-analysis\b|systematic review|\bPRISMA\b", "元分析/系统综述"),
    (r"消融实验|基线对比|\bablation\b|\bbenchmark\b|fine-tuning|微调", "实验基准/消融对比"),
    (r"面板数据|固定效应|双向固定|panel data|fixed effect", "面板固定效应"),
    (r"符号学|话语分析|文本分析|内容分析|多模态|discourse analysis|semiotic|multimodal", "文本/多模态话语分析"),
    (r"博弈论|演化博弈|数理模型|理论模型|game theory|theoretical model", "数理/博弈建模"),
]

_FINDING_CUE_RE = re.compile(
    r"(?:研究发现|结果表明|实证表明|实证显示|实证结果|本文发现|检验发现|分析表明|研究结论|发现[：:，,]|表明[：:，,]|"
    r"results?\s+(?:show|indicate|demonstrate|suggest|reveal)|we\s+(?:find|show|demonstrate)|findings?\s+(?:suggest|indicate|show))",
    re.IGNORECASE,
)

_SAMPLE_CUE_PATTERNS = [
    re.compile(r"((?:19|20)\d{2}\s*[—\-~至]+\s*(?:19|20)\d{2}\s*年[^，。；,.]{2,28}(?:数据|样本|面板|企业|城市|省份|县域))"),
    re.compile(r"((?:沪深\s*A\s*股|中国\s*A\s*股|制造业|重污染)[^，。；,.]{0,18}(?:上市公司|企业))"),
    re.compile(r"\b(CHARLS|CFPS|CGSS|CHFS|CMDS|CSMAR|Wind|CNRDS|ImageNet|MMLU|GSM8K|HumanEval|MIMIC-(?:III|IV)|UK\s+Biobank)\b", re.I),
    re.compile(r"(\d{2,6}\s*(?:家|个|份|名|例|篇)[^，。；,.]{1,16}(?:企业|城市|地级市|县|样本|问卷|受访者|患者))"),
    re.compile(r"\b([Nn]\s*=\s*[\d,]{2,9})\b"),
]

_POS_POLARITY_RE = re.compile(
    r"(?:显著促进|显著提升|显著增强|显著提高|显著推动|正向影响|赋能|显著改善|有效缓解|显著降低.{0,8}(?:风险|成本|碳排放|贫困)|"
    r"significantly\s+(?:increase|improve|enhance|promote|boost|facilitate|positively)|positive\s+(?:effect|impact|relationship))",
    re.IGNORECASE,
)
_NEG_POLARITY_RE = re.compile(
    r"(?:显著抑制|显著阻碍|显著削弱|负向影响|显著挤出|加剧了|显著恶化|不利于|"
    r"significantly\s+(?:inhibit|reduce|decrease|hinder|impede|suppress|negatively)|negative\s+(?:effect|impact|relationship))",
    re.IGNORECASE,
)
_NONLINEAR_POLARITY_RE = re.compile(
    r"(?:倒\s*U\s*[型形]|U\s*[型形]|非线性|门槛效应|边际递减|先升后降|先抑后扬|双刃剑|"
    r"inverted[- ]u|u-shaped|non-?linear|threshold\s+effect)",
    re.IGNORECASE,
)


def _detect_text_polarity(text: str) -> str:
    if not text:
        return "mixed_or_neutral"
    if _NONLINEAR_POLARITY_RE.search(text):
        return "nonlinear"
    has_pos = bool(_POS_POLARITY_RE.search(text))
    has_neg = bool(_NEG_POLARITY_RE.search(text))
    if has_pos and not has_neg:
        return "positive"
    if has_neg and not has_pos:
        return "negative"
    return "mixed_or_neutral"


def extract_paper_facets(paper: Dict[str, Any]) -> Dict[str, Any]:
    """从单篇论文的题名、摘要与关键词中确定性抽取结构化学术要素（方法、样本、核心发现句、效应极性）。"""
    title = str(paper.get("title") or "").strip()
    abstract = str(paper.get("abstract") or "").strip()
    kw_str = " ".join(_normalize_keywords(paper.get("keywords")))
    blob = f"{title} {kw_str} {abstract}"

    methods: List[str] = []
    for pat, label in _METHOD_RULES:
        if re.search(pat, blob, re.IGNORECASE):
            if label not in methods:
                methods.append(label)
        if len(methods) >= 2:
            break
    method_str = " / ".join(methods) if methods else ("理论/规范或待核原文" if not abstract else "综合分析")

    sample_str = ""
    for sp in _SAMPLE_CUE_PATTERNS:
        m = sp.search(blob)
        if m:
            sample_str = m.group(1).strip()
            break

    core_finding = ""
    if abstract:
        protected = re.sub(r"(?<=\d)\.(?=\d)", "\u2024", abstract)
        sents = [s.replace("\u2024", ".").strip() for s in re.split(r"(?<=[。！？.!?])\s*", protected) if len(s.strip()) >= 14]
        cue_sents = [s for s in sents if _FINDING_CUE_RE.search(s)]
        if cue_sents:
            core_finding = " ".join(cue_sents[:2])
        elif len(sents) >= 2:
            core_finding = sents[-1]
        elif sents:
            core_finding = sents[0]
        if len(core_finding) > 165:
            core_finding = core_finding[:162].rstrip("，,;； ") + "…"

    polarity = _detect_text_polarity(core_finding or abstract)
    polarity_label = {
        "positive": "正向促进 (+)",
        "negative": "负向抑制 (-)",
        "nonlinear": "非线性/门槛 (∩/∪)",
        "mixed_or_neutral": "综合/中性",
    }[polarity]

    return {
        "research_method": method_str,
        "data_sample": sample_str or "-",
        "core_finding": core_finding,
        "effect_polarity": polarity,
        "polarity_label": polarity_label,
    }


def _venue_badge(paper: Dict[str, Any]) -> str:
    parts: List[str] = []
    if paper.get("core_type"):
        parts.append(str(paper["core_type"]).replace(";", "/"))
    for vt in (paper.get("venue_tags") or []):
        if vt not in parts:
            parts.append(str(vt))
    if paper.get("is_warning"):
        parts.append("预警")
    return ",".join(parts[:3])


def build_comparison_matrix(selected: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    """生成可供文献综述直接引用的结构化『文献要素对比矩阵』（JSON + Markdown 三线表）。"""
    matrix: List[Dict[str, Any]] = []
    md_rows: List[List[str]] = []
    for item in selected:
        p = item["paper"]
        idx = item["index"]
        facets = extract_paper_facets(p)
        authors = _split_authors(p.get("authors"))
        first_au = authors[0] if authors else "佚名"
        if len(authors) > 1:
            first_au = f"{first_au}等" if any("\u4e00" <= c <= "\u9fff" for c in first_au) else f"{first_au} et al."
        yr = _paper_year(p) or "n.d."
        journal = str(p.get("journal") or p.get("source") or "-").strip()
        badge = _venue_badge(p)
        venue_col = f"{journal} ({badge})" if badge else journal
        finding_col = facets["core_finding"] or "（仅题录，待核对原文）"
        row_obj = {
            "index": idx,
            "author_year": f"{first_au} ({yr})",
            "title": p.get("title", ""),
            "venue": venue_col,
            **facets,
        }
        matrix.append(row_obj)
        md_rows.append([
            f"[{idx}]",
            f"{first_au} ({yr})",
            venue_col[:26],
            facets["research_method"],
            facets["data_sample"][:24],
            f"{facets['polarity_label']}：{finding_col[:72]}",
        ])

    if not md_rows:
        return [], ""
    lines = [
        "| 序号 | 作者(年份) | 来源/级别 | 研究方法 | 数据/样本 | 效应方向与核心结论 |",
        "| --- | --- | --- | --- | --- | --- |",
        *["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in md_rows],
    ]
    return matrix, "\n".join(lines)


_GENERIC_STOPWORDS = {
    "the", "and", "of", "in", "on", "for", "to", "with", "from", "via", "a", "an",
    "by", "at", "as", "is", "are", "using", "based", "study", "analysis", "research",
    "review", "new", "approach", "toward", "towards", "case", "application",
    "研究", "分析", "探讨", "综述", "基于", "视域", "视角", "路径", "机制", "问题",
    "影响", "应用", "发展", "现状", "对策", "思考", "初探", "浅析", "我国", "中国",
}


def _term_in_text(term: str, lowered_text: str) -> bool:
    t = term.strip().lower()
    if not t:
        return False
    if re.fullmatch(r"[a-z0-9_-]+", t):
        if len(t) <= 3:
            return bool(re.search(rf"\b{re.escape(t)}\b", lowered_text))
        return bool(re.search(rf"\b{re.escape(t)}", lowered_text))
    return t in lowered_text


def _review_query_terms(topic: str) -> List[str]:
    normalized = topic.lower()
    for sep in "：:，,；;、/\\()（）[]【】《》<>\"'\n\t":
        normalized = normalized.replace(sep, " ")
    terms: List[str] = []
    for token in normalized.split():
        token = token.strip(" .-_—")
        if len(token) >= 2 and token not in _GENERIC_STOPWORDS and token not in terms:
            terms.append(token)
    for cjk_chunk in re.findall(r"[一-鿿]{2,}", topic):
        if cjk_chunk not in _GENERIC_STOPWORDS and cjk_chunk not in terms:
            terms.append(cjk_chunk)
        for size in (4, 3, 2):
            if len(cjk_chunk) > size:
                for i in range(len(cjk_chunk) - size + 1):
                    sub = cjk_chunk[i:i + size]
                    if sub not in _GENERIC_STOPWORDS and sub not in terms:
                        terms.append(sub)
    compact = topic.lower().replace(" ", "")
    if len(compact) >= 2 and compact not in terms:
        terms.append(compact)
    return terms[:20]


def _extract_paper_theme_candidates(paper: Dict[str, Any], topic: str) -> List[str]:
    topic_norm = topic.strip().lower()
    candidates: List[str] = []

    for kw in _normalize_keywords(paper.get("keywords")):
        kw_low = kw.lower()
        if kw_low == topic_norm or kw_low in _GENERIC_STOPWORDS:
            continue
        if len(kw) <= 28 and kw not in candidates:
            candidates.append(kw)

    title = str(paper.get("title") or "")
    for eng_phrase in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", title):
        low = eng_phrase.lower()
        if low not in _GENERIC_STOPWORDS and low != topic_norm and low not in [c.lower() for c in candidates]:
            candidates.append(eng_phrase)

    for seg in re.split(r"[：:——\-—、，,（）()《》“”\"'\s]+", title):
        seg = re.sub(r"(的|与|及|中|下|上|对|在|论|探析|研究|分析|探讨|综述|基于|视角|视域)$", "", seg.strip())
        seg = re.sub(r"^(基于|面向|关于|试论|浅析|论)", "", seg).strip()
        if 2 <= len(seg) <= 12 and seg.lower() != topic_norm and seg not in _GENERIC_STOPWORDS:
            if seg not in candidates:
                candidates.append(seg)

    return candidates[:8]


def _review_quality_flags(paper: Dict[str, Any], relevance: int) -> List[str]:
    title = str(paper.get("title") or "")
    source = str(paper.get("source") or "")
    flags = []
    if "retracted" in title.lower() or "撤稿" in title:
        flags.append("retracted")
    if relevance <= 0:
        flags.append("low_relevance")
    if not paper.get("abstract"):
        flags.append("needs_fulltext_check")
    if source in ("CNKI", "CNKI-export") and not paper.get("abstract"):
        flags.append("cnki_read_detail_recommended")
    return flags


def _review_candidates(papers: List[Dict[str, Any]], topic: str) -> List[Dict[str, Any]]:
    query_terms = _review_query_terms(topic)
    candidates = []
    for idx, paper in enumerate(papers, 1):
        title = str(paper.get("title") or "").lower()
        abstract = str(paper.get("abstract") or "").lower()
        keywords = " ".join(_normalize_keywords(paper.get("keywords"))).lower()
        journal = str(paper.get("journal") or "").lower()
        score = 0
        matched = []
        for term in query_terms:
            hit = False
            if _term_in_text(term, title):
                score += 4
                hit = True
            if _term_in_text(term, keywords):
                score += 3
                hit = True
            if _term_in_text(term, abstract):
                score += 2
                hit = True
            if _term_in_text(term, journal):
                score += 1
                hit = True
            if hit:
                matched.append(term)
        if not query_terms:
            score = 1
        flags = _review_quality_flags(paper, score)
        candidates.append({
            "index": idx,
            "paper": paper,
            "relevance": score,
            "matched_terms": matched[:8],
            "flags": flags,
        })
    candidates.sort(
        key=lambda item: (
            "retracted" not in item["flags"],
            item["relevance"],
            bool(item["paper"].get("abstract")),
            int(item["paper"].get("cited_by") or 0),
        ),
        reverse=True,
    )
    return candidates


def _select_review_papers(papers: List[Dict[str, Any]], topic: str, limit: int) -> List[Dict[str, Any]]:
    candidates = _review_candidates(papers, topic)
    relevant = [item for item in candidates if item["relevance"] > 0 and "retracted" not in item["flags"]]
    selected_pool = relevant or candidates
    return selected_pool[:limit]


def _review_clusters(selected: List[Dict[str, Any]], topic: str) -> List[Dict[str, Any]]:
    """基于文献库真实关键词与题名短语动态生成学科中立聚类。"""
    if not selected:
        return []

    canonical_display: Dict[str, str] = {}
    doc_freq: Counter[str] = Counter()
    per_item_themes: List[List[str]] = []

    for item in selected:
        cands = _extract_paper_theme_candidates(item["paper"], topic)
        norm_cands: List[str] = []
        for c in cands:
            key = c.lower()
            canonical_display.setdefault(key, c)
            if key not in norm_cands:
                norm_cands.append(key)
        per_item_themes.append(norm_cands)
        for key in norm_cands:
            doc_freq[key] += 1

    # 优先选至少出现 2 次的高频主题；语料较小时退化为前 4 个代表性主题
    top_keys = [k for k, cnt in doc_freq.most_common(6) if cnt >= 2]
    if not top_keys:
        top_keys = [k for k, _ in doc_freq.most_common(min(4, len(doc_freq)))]

    clusters_map: Dict[str, List[Dict[str, Any]]] = {}
    for item, item_keys in zip(selected, per_item_themes):
        chosen_label = None
        for tk in top_keys:
            if tk in item_keys:
                chosen_label = canonical_display[tk]
                break
        if chosen_label is None:
            text_low = " ".join(
                str(item["paper"].get(k, "")) for k in ("title", "abstract")
            ).lower()
            for tk in top_keys:
                if _term_in_text(tk, text_low):
                    chosen_label = canonical_display[tk]
                    break
        if chosen_label is None:
            chosen_label = canonical_display[item_keys[0]] if item_keys else "基础脉络与综合线索"
        clusters_map.setdefault(chosen_label, []).append(item)

    # 若单篇散落过多（>5 个簇），将仅含 1 篇的尾部簇合并入「综合与拓展线索」
    if len(clusters_map) > 5:
        ordered = sorted(clusters_map.items(), key=lambda kv: len(kv[1]), reverse=True)
        head = dict(ordered[:4])
        tail_items: List[Dict[str, Any]] = []
        for _lbl, items in ordered[4:]:
            tail_items.extend(items)
        if tail_items:
            head.setdefault("综合与拓展线索", []).extend(tail_items)
        clusters_map = head

    result = []
    for label, items in sorted(clusters_map.items(), key=lambda kv: len(kv[1]), reverse=True):
        evidence = []
        findings = []
        methods_counter: Counter[str] = Counter()
        pos_indices: List[int] = []
        neg_or_nl_indices: List[int] = []
        for item in items:
            p = item["paper"]
            abstract = str(p.get("abstract") or "").strip()
            facets = extract_paper_facets(p)
            if facets["core_finding"]:
                findings.append(f"[{item['index']}] {facets['core_finding']}")
            if facets["research_method"] and "待核原文" not in facets["research_method"]:
                methods_counter[facets["research_method"]] += 1
            if facets["effect_polarity"] == "positive":
                pos_indices.append(item["index"])
            elif facets["effect_polarity"] in ("negative", "nonlinear"):
                neg_or_nl_indices.append(item["index"])
            evidence.append({
                "index": item["index"],
                "title": p.get("title", ""),
                "trace_status": "abstract" if abstract else "metadata_only",
                "research_method": facets["research_method"],
                "data_sample": facets["data_sample"],
                "effect_polarity": facets["effect_polarity"],
                "core_finding": facets["core_finding"],
                "abstract_excerpt": abstract[:160],
            })
        top_methods = "、".join(m for m, _ in methods_counter.most_common(3))
        divergence_note = ""
        if pos_indices and neg_or_nl_indices:
            pos_str = "".join(f"[{i}]" for i in pos_indices[:4])
            neg_str = "".join(f"[{i}]" for i in neg_or_nl_indices[:4])
            divergence_note = f"存在学术争鸣/结论分化：文献{pos_str}报告正向促进效应，而文献{neg_str}指出负向抑制或非线性门槛边界。"
        synth_parts = []
        if top_methods:
            synth_parts.append(f"主流方法采用{top_methods}")
        if findings:
            synth_parts.append("核心结论：" + "；".join(findings[:3]))
        if divergence_note:
            synth_parts.append(divergence_note)
        result.append({
            "label": label,
            "count": len(items),
            "dominant_methods": [m for m, _ in methods_counter.most_common(3)],
            "divergence_note": divergence_note or None,
            "papers": evidence,
            "synthesis": "。".join(synth_parts) if synth_parts else "",
        })
    return result


def _review_gaps(papers: List[Dict[str, Any]], topic: str) -> List[Dict[str, Any]]:
    """基于文献库客观统计生成跨学科通用的覆盖度与研究空白提示。"""
    total = len(papers)
    if total == 0:
        return []

    gaps: List[Dict[str, Any]] = []
    current_year = datetime.now().year

    # 1. 摘要/全文可追溯性缺口
    with_abstract = [i for i, p in enumerate(papers, 1) if str(p.get("abstract") or "").strip()]
    missing_abstract_count = total - len(with_abstract)
    if missing_abstract_count >= max(2, total // 3):
        gaps.append({
            "title": "摘要与原文证据待补全",
            "dimension": "证据完整性",
            "matched_count": len(with_abstract),
            "total": total,
            "evidence_indices": with_abstract[:10],
            "basis": f"当前文献库 {total} 篇中有 {missing_abstract_count} 篇仅有题录而无摘要（可追溯摘要 {len(with_abstract)} 篇），建议先执行 read-detail 补全核心文献。",
        })

    # 2. 近 3 年前沿覆盖度
    recent_indices = []
    for i, p in enumerate(papers, 1):
        try:
            y = int(str(_paper_year(p))[:4])
            if y >= current_year - 2:
                recent_indices.append(i)
        except (TypeError, ValueError):
            continue
    if len(recent_indices) <= max(1, total // 6):
        gaps.append({
            "title": "近年最新文献覆盖偏少",
            "dimension": "时间新近性",
            "matched_count": len(recent_indices),
            "total": total,
            "evidence_indices": recent_indices[:10],
            "basis": f"当前文献库 {total} 篇中，近 3 年（>={current_year - 2}）文献仅 {len(recent_indices)} 篇，建议叠加 --year-from {current_year - 2} 补检最新进展。",
        })

    # 3. 实证/田野/质性与量化研究方法线索（兼顾理工实验与人文社科田野/文本/符号/多模态方法）
    empirical_terms = (
        "实证", "实验", "数据集", "问卷", "调研", "访谈", "田野", "民族志", "扎根", "个案", "案例",
        "多模态", "话语分析", "文本分析", "符号", "回归", "样本", "临床",
        "empirical", "experiment", "dataset", "benchmark", "survey", "trial", "cohort", "evaluation",
        "ethnography", "case study", "multimodal", "discourse", "grounded theory",
    )
    empirical_hits = []
    # 4. 比较/跨文化/跨情境与系统整合线索
    comparative_terms = (
        "比较", "对比", "跨国", "跨区域", "跨文化", "跨媒介", "元分析", "系统综述", "组态", "消融",
        "comparative", "comparison", "cross-national", "cross-cultural", "meta-analysis", "systematic review", "ablation",
    )
    comparative_hits = []
    cjk_count = 0

    for i, p in enumerate(papers, 1):
        blob = " ".join(str(p.get(k, "")) for k in ("title", "abstract", "keywords")).lower()
        if any(_term_in_text(t, blob) for t in empirical_terms):
            empirical_hits.append(i)
        if any(_term_in_text(t, blob) for t in comparative_terms):
            comparative_hits.append(i)
        if re.search(r"[一-鿿]", str(p.get("title") or "")):
            cjk_count += 1

    if len(empirical_hits) <= max(1, total // 8):
        gaps.append({
            "title": "实证调研或深描个案文献偏少",
            "dimension": "方法支撑",
            "matched_count": len(empirical_hits),
            "total": total,
            "evidence_indices": empirical_hits[:10],
            "basis": f"当前文献库 {total} 篇中，明确包含实证、田野、多模态文本分析或实验样本线索的文献约 {len(empirical_hits)} 篇。",
        })

    if len(comparative_hits) <= max(1, total // 10):
        gaps.append({
            "title": "跨情境比较或系统整合视角较少",
            "dimension": "比较/整合",
            "matched_count": len(comparative_hits),
            "total": total,
            "evidence_indices": comparative_hits[:10],
            "basis": f"当前文献库 {total} 篇中，涉及跨文化/跨情境比较、组态分析或系统综述的文献约 {len(comparative_hits)} 篇。",
        })

    if total >= 5 and (cjk_count == 0 or cjk_count == total):
        lang_missing = "英文国际文献（OpenAlex / Semantic Scholar / arXiv）" if cjk_count == total else "中文文献（CNKI / NSSD）"
        gaps.append({
            "title": "单语种文献来源集中",
            "dimension": "语种覆盖",
            "matched_count": 0,
            "total": total,
            "evidence_indices": [],
            "basis": f"当前 {total} 篇文献全部集中于单一语种，若课题涉及国内外对比，可补充检索{lang_missing}。",
        })

    if not gaps:
        gaps.append({
            "title": "细分结论仍需结合全文核验",
            "dimension": "原文核验",
            "matched_count": len(with_abstract),
            "total": total,
            "evidence_indices": with_abstract[:8],
            "basis": f"当前 {total} 篇文献在主要维度均有覆盖，建议对核心文献抓取全文核对研究设计细节与边界条件。",
        })
    return gaps


def _probe_openalex_hit_count(query: str, year_from: int) -> Optional[Dict[str, Any]]:
    """向 OpenAlex 发起轻量级 `per-page=1` 探测，秒级获取全球真实命中篇数，用于为研究空白提供可验证检索证据。"""
    import urllib.parse
    import urllib.request

    params = {
        "search": query,
        "filter": f"from_publication_date:{year_from}-01-01",
        "per-page": "1",
    }
    headers = {"User-Agent": "scholar-kit/1.25 (mailto:scholar-kit@users.noreply.github.com)"}
    payload: Optional[Dict[str, Any]] = None
    try:
        import requests
        r = requests.get("https://api.openalex.org/works", params=params, headers=headers, timeout=6)
        if r.status_code == 200:
            payload = r.json()
    except Exception:
        payload = None

    if payload is None:
        url = f"https://api.openalex.org/works?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception:
            return None

    try:
        cnt = int((payload.get("meta") or {}).get("count", 0))
        verdict = "confirmed_sparse" if cnt < 15 else ("moderate_space" if cnt < 80 else "already_crowded")
        return {
            "database": "OpenAlex",
            "query": query,
            "year_range": f"{year_from}-{datetime.now().year}",
            "total_hits": cnt,
            "verdict": verdict,
        }
    except Exception:
        return None


def _verify_gaps_live(gaps: List[Dict[str, Any]], topic: str) -> List[Dict[str, Any]]:
    """对候选空白维度执行真实外部数据库检索计数验证（响应 SKILL.md 硬性规则2：研究空白须附检索证据）。"""
    yr_from = datetime.now().year - 4
    dim_query_suffix = {
        "时间新近性": topic,
        "方法支撑": f"{topic} empirical experiment",
        "比较/整合": f"{topic} comparative cross-cultural meta-analysis",
        "语种覆盖": topic,
    }
    verified: List[Dict[str, Any]] = []
    for g in gaps:
        g_copy = dict(g)
        dim = g.get("dimension", "")
        if dim in ("证据完整性", "原文核验"):
            verified.append(g_copy)
            continue
        q = dim_query_suffix.get(dim, f"{topic} {g.get('title', '')}")
        proof = _probe_openalex_hit_count(q, yr_from)
        if proof:
            g_copy["search_proof"] = proof
            if proof["verdict"] == "already_crowded" and g.get("dimension") in ("方法支撑", "比较/整合"):
                g_copy["basis"] = (
                    f"{g_copy['basis']} 【外部核验纠偏：OpenAlex 检索 `{q}` ({proof['year_range']}) 实际命中 {proof['total_hits']} 篇，"
                    "说明国际学界并非空白，仅当前会话样本未覆盖，严禁直接断言学界空白！】"
                )
            else:
                g_copy["basis"] = (
                    f"{g_copy['basis']} 【外部检索证据：{proof['database']} 检索 `{q}` ({proof['year_range']}) 命中 {proof['total_hits']} 篇 ({proof['verdict']})】"
                )
        verified.append(g_copy)
    return verified


def _build_review_markdown(
    topic: str,
    selected: List[Dict[str, Any]],
    all_count: int,
    candidates: List[Dict[str, Any]],
    clusters: Optional[List[Dict[str, Any]]] = None,
    gaps: Optional[List[Dict[str, Any]]] = None,
) -> str:
    sources = Counter((item["paper"].get("source") or "未知来源") for item in selected)
    source_summary = "、".join(f"{k} {v} 篇" for k, v in sources.items()) or "无"
    with_abstract = [item for item in selected if item["paper"].get("abstract")]
    metadata_only = [item for item in selected if not item["paper"].get("abstract")]
    excluded = [item for item in candidates if "retracted" in item["flags"] or item["relevance"] <= 0][:8]

    lines = [
        f"# {topic} 文献综述证据包",
        "",
        "## 检索证据概览",
        f"- 会话文献总数：{all_count} 篇；纳入本轮分析：{len(selected)} 篇（{source_summary}）",
        f"- 证据可追溯状态：含摘要 {len(with_abstract)} 篇，仅题录（待核对原文）{len(metadata_only)} 篇",
        "",
        "## 推荐精读文献",
    ]
    for item in selected[:8]:
        p = item["paper"]
        year = _paper_year(p) or "年份未获取"
        journal = p.get("journal") or "来源未获取"
        status = "摘要可追溯" if p.get("abstract") else ("本地PDF可追溯" if p.get("local_pdf") else "待核对原文")
        tags_str = f" [标签: {', '.join(p['tags'])}]" if p.get("tags") else ""
        note_str = f"（批注：{p['note']}）" if p.get("note") else ""
        lines.append(f"- [{item['index']}] {p.get('title', '未获取')}（{journal}, {year}）— {status}{tags_str}{note_str}")

    _, matrix_md = build_comparison_matrix(selected[:12])
    if matrix_md:
        lines.extend(["", "## 文献要素对比矩阵（方法 · 样本 · 核心结论）", "", matrix_md])

    if clusters:
        lines.extend(["", "## 主题聚类线索"])
        for cluster in clusters:
            idx_str = "".join(f"[{p['index']}]" for p in cluster.get("papers", [])[:8])
            summary = cluster.get("synthesis") or "当前簇主要由题录构成，需补充摘要或原文后再提炼具体结论"
            lines.append(f"### {cluster['label']}（{cluster['count']} 篇）{idx_str}")
            lines.append(f"- 证据摘录：{summary}")

    if gaps:
        lines.extend(["", "## 覆盖度与研究空白提示（仅基于当前会话统计）"])
        for gap in gaps:
            idx_str = "".join(f"[{i}]" for i in gap.get("evidence_indices", [])[:6])
            suffix = f" 关联序号：{idx_str}" if idx_str else ""
            lines.append(f"- **{gap['title']}**：{gap['basis']}{suffix}")

    if metadata_only:
        lines.extend(["", "## 待核对原文（缺摘要）"])
        for item in metadata_only[:10]:
            lines.append(f"- [{item['index']}] {item['paper'].get('title', '未获取')}")

    if excluded:
        lines.extend(["", "## 建议剔除或降权条目（撤稿 / 低相关）"])
        for item in excluded:
            reason = "疑似撤稿" if "retracted" in item["flags"] else "主题词未直接命中"
            lines.append(f"- [{item['index']}] {item['paper'].get('title', '未获取')}（{reason}）")

    return "\n".join(lines).rstrip() + "\n"


def _paper_write_excerpt(paper: Dict[str, Any], max_len: int = 120) -> str:
    abstract = str(paper.get("abstract") or "").strip()
    if not abstract:
        return ""
    cleaned = " ".join(abstract.split())
    return cleaned[:max_len] + ("…" if len(cleaned) > max_len else "")


def _evidence_indices(items: List[Dict[str, Any]], max_items: int = 6) -> str:
    indices = [f"[{item['index']}]" for item in items[:max_items] if item.get("index")]
    return "".join(indices)


def _cluster_evidence_indices(cluster: Dict[str, Any], max_items: int = 6) -> str:
    indices = [f"[{p['index']}]" for p in cluster.get("papers", [])[:max_items] if p.get("index")]
    return "".join(indices)


def _build_review_outline(topic: str, clusters: List[Dict[str, Any]], gaps: List[Dict[str, Any]]) -> str:
    lines = [f"# {topic} 文献综述大纲", "", "## 一、研究问题与检索范围"]
    intro_indices = _cluster_evidence_indices(clusters[0]) if clusters else ""
    lines.append(f"- 界定“{topic}”的核心概念边界、分析对象与文献时间跨度{intro_indices}。")
    lines.append("- 说明文献检索数据库、纳入标准及摘要/全文可追溯情况。")
    lines.extend(["", "## 二、主要研究脉络与主题分支"])
    if clusters:
        for i, cluster in enumerate(clusters, 1):
            indices = _cluster_evidence_indices(cluster)
            lines.append(f"- {i}. {cluster['label']}（{cluster['count']} 篇）：梳理核心观点、研究设计与共识分歧{indices}。")
    else:
        lines.append("- 当前文献库尚未形成稳定主题分支，建议先扩展检索。")
    lines.extend(["", "## 三、现有研究局限与后续切入点"])
    for gap in gaps[:4]:
        indices = "".join(f"[{i}]" for i in gap.get("evidence_indices", [])[:5]) or "（需补检索）"
        lines.append(f"- {gap['title']}：{gap['basis']} 线索 {indices}。")
    lines.extend(["", "## 四、段落证据映射", "- 正文每个实质判断须保留 [序号] 映射，缺摘要条目须标注“待核对原文”。"])
    return "\n".join(lines)


def _build_review_section(
    topic: str,
    selected: List[Dict[str, Any]],
    clusters: List[Dict[str, Any]],
    gaps: List[Dict[str, Any]],
    section: str,
) -> str:
    section_key = section.strip() if section else "文献综述"
    usable = [item for item in selected if item.get("relevance", 0) > 0 and "retracted" not in item.get("flags", [])]
    if section_key in ("研究背景", "背景", "问题提出", "研究问题与检索范围"):
        indices = _evidence_indices(usable)
        sources = sorted({item["paper"].get("source", "未获取") or "未获取" for item in usable}) or ["未获取"]
        years = sorted(
            {int(str(_paper_year(item["paper"]))[:4]) for item in usable if str(_paper_year(item["paper"]))[:4].isdigit()}
        )
        year_span = f"{years[0]}—{years[-1]} 年" if len(years) >= 2 else (f"{years[0]} 年" if years else "年份待核对")
        return "\n".join([
            f"# {topic}：研究问题与文献概览",
            "",
            f"当前围绕“{topic}”纳入的高相关文献共 {len(usable)} 篇（发表跨度 {year_span}，检索源涵盖 {'、'.join(sources)}）{indices}。"
            f"文献主要分布于{'、'.join(c['label'] for c in clusters[:4]) or '基础议题'}等方向；"
            f"其中含摘要可追溯文献 {sum(1 for x in usable if x['paper'].get('abstract'))} 篇，其余题录条目需补充原文后核实具体结论。",
            "",
            "## 段落证据映射",
            *[
                f"- [{item['index']}] {item['paper'].get('title', '未获取')}："
                f"{'摘要可追溯' if item['paper'].get('abstract') else '待核对原文'}，相关性分 {item.get('relevance', 0)}"
                for item in usable
            ],
        ])
    if section_key in ("研究不足", "不足", "未来方向", "后续方向", "现有研究局限与后续切入点"):
        lines = [f"# {topic}：现有研究局限与后续切入点", ""]
        for gap in gaps[:4]:
            indices = "".join(f"[{i}]" for i in gap.get("evidence_indices", [])[:5]) or "（当前库无直接命中）"
            lines.append(
                f"{gap['title']}：{gap['basis']} 相关线索序号：{indices}。"
                "该结论仅反映本轮检索样本覆盖度，定稿前需通过扩展检索词与跨库核验加以确认。"
            )
        return "\n\n".join(lines)
    for cluster in clusters:
        if section_key in cluster["label"] or cluster["label"] in section_key:
            indices = _cluster_evidence_indices(cluster)
            body = cluster.get("synthesis") or "该分支当前主要由题录构成，需在抓取摘要或全文后再提炼研究结论"
            return "\n".join([
                f"# {topic}：{cluster['label']}",
                "",
                f"“{cluster['label']}”分支共包含 {cluster['count']} 篇文献{indices}。摘要线索显示：{body}。"
                "成稿时应对照各篇研究对象、样本量与边界条件做比较评述，对仅含题录的条目标注“待核对原文”。",
                "",
                "## 段落证据映射",
                *[
                    f"- [{paper['index']}] {paper.get('title', '未获取')}："
                    f"{'摘要可追溯' if paper.get('trace_status') == 'abstract' else '待核对原文'}"
                    for paper in cluster.get("papers", [])
                ],
            ])
    return _build_review_draft(topic, selected, clusters, gaps, mode="draft")


def _build_review_draft(
    topic: str,
    selected: List[Dict[str, Any]],
    clusters: List[Dict[str, Any]],
    gaps: List[Dict[str, Any]],
    mode: str = "draft",
    section: str = "",
) -> str:
    usable = [item for item in selected if item.get("relevance", 0) > 0 and "retracted" not in item.get("flags", [])]
    cluster_list = clusters or _review_clusters(usable, topic)
    if mode == "outline":
        return _build_review_outline(topic, cluster_list, gaps)
    if mode == "section" or section:
        return _build_review_section(topic, selected, cluster_list, gaps, section)

    lines = [f"# {topic} 文献综述证据初稿", "", "## 一、文献分布与检索范围"]
    if not usable:
        lines.append("当前文献库中尚未形成足够高相关、可追溯的文献基础，建议扩大检索或补充摘要后再生成综述初稿。")
    else:
        intro_indices = _evidence_indices(usable)
        sources = sorted({item["paper"].get("source", "未获取") or "未获取" for item in usable})
        abstract_cnt = sum(1 for item in usable if item["paper"].get("abstract"))
        lines.append(
            f"本轮围绕“{topic}”共筛选出 {len(usable)} 篇相关文献（来源：{'、'.join(sources)}），"
            f"其中 {abstract_cnt} 篇具备摘要支撑，{len(usable) - abstract_cnt} 篇仅含题录待核对原文{intro_indices}。"
            f"按关键词与题名聚类，现有文献集中于{'、'.join(c['label'] for c in cluster_list[:4])}等议题。"
        )
        lines.extend(["", "## 二、结构化文献对比矩阵", ""])
        _, draft_matrix_md = build_comparison_matrix(usable[:12])
        if draft_matrix_md:
            lines.append(draft_matrix_md)
        lines.extend(["", "## 三、主要研究脉络与主题分支"])
        for cluster in cluster_list:
            indices = _cluster_evidence_indices(cluster)
            if cluster.get("synthesis"):
                body = cluster["synthesis"]
            else:
                body = "该组文献目前缺少摘要文本，仅能作为题录线索列出，具体结论待核对原文后补入"
            lines.extend([
                f"### {cluster['label']}（{cluster['count']} 篇）",
                f"该分支共 {cluster['count']} 篇相关文献{indices}。{body}。",
                "",
            ])
        lines.extend(["## 四、样本覆盖局限与补检方向"])
        for i, gap in enumerate(gaps[:4], 1):
            indices = "".join(f"[{idx}]" for idx in gap.get("evidence_indices", [])[:5]) or "（当前库无直接命中）"
            lines.append(
                f"{i}. {gap['title']}：{gap['basis']} 关联序号：{indices}。"
            )
    lines.extend(["", "## 四、段落证据映射"])
    for item in usable:
        p = item["paper"]
        status = "摘要可追溯" if p.get("abstract") else "待核对原文"
        excerpt = _paper_write_excerpt(p, 80)
        excerpt_text = f"；摘要线索：{excerpt}" if excerpt else ""
        lines.append(f"- [{item['index']}] {p.get('title', '未获取')}：{status}，相关性分 {item.get('relevance', 0)}{excerpt_text}")
    return "\n".join(lines)


def _auto_detail_for_review(
    papers: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    detail_top_n: int,
    project: Optional[str],
) -> Dict[str, Any]:
    targets = []
    for item in candidates:
        paper = item["paper"]
        if item.get("relevance", 0) <= 0 or "retracted" in item.get("flags", []):
            continue
        if _is_cnki_paper(paper) and paper.get("url") and not paper.get("abstract"):
            targets.append(item)
        if len(targets) >= detail_top_n:
            break
    if not targets:
        return {"attempted": 0, "updated": 0, "indices": []}

    # 传入浅拷贝，防止 batch_read_detail 原地修改字典后导致 before_had_abstract 误判为 True（修复 updated=0 失真）
    selected = [dict(item["paper"]) for item in targets]
    enriched = batch_read_detail(papers=selected, top_n=len(selected), fulltext=False)
    enriched_map = {p.get("url", ""): p for p in enriched if isinstance(p, dict) and p.get("url")}
    updated = list(papers)
    updated_count = 0
    for item in targets:
        idx = item["index"] - 1
        url = updated[idx].get("url", "")
        detail = enriched_map.get(url)
        if not detail:
            continue
        before_had_abstract = bool(updated[idx].get("abstract"))
        merged = dict(updated[idx])
        for k, v in detail.items():
            if k == "fulltext":
                continue
            if v:
                merged[k] = v
        if merged.get("abstract") and not before_had_abstract:
            updated_count += 1
        updated[idx] = merged
    _save_session(updated, project=project)
    return {
        "attempted": len(targets),
        "updated": updated_count,
        "indices": [item["index"] for item in targets],
    }


def _review_write_inputs(papers: List[Dict[str, Any]], topic: str, limit: int) -> tuple:
    selected = _select_review_papers(papers, topic, limit)
    clusters = _review_clusters(selected, topic)
    gaps = _review_gaps(papers, topic)
    return selected, clusters, gaps


def _append_references(markdown: str, selected: List[Dict[str, Any]], style: str = "gbt7714") -> str:
    papers = [item["paper"] for item in selected]
    refs = generate_reference_list(papers, style)
    if refs:
        markdown = markdown.rstrip() + "\n\n## 参考文献\n" + refs.strip() + "\n"
    return markdown


def _sentence_evidence_indices(
    text: str,
    evidence_by_index: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[int]:
    """同时解析顺序编码制（`[1]`、`[1, 3]`、`[2-5]`）与著者-出版年制（`赵涛等（2020）`、`Vaswani et al. (2017)`）。"""
    found: List[int] = []

    # 1. 顺序编码方括号：支持单号、逗号分隔与连字符区间 `[1, 3-5, 9]`
    for grp in re.findall(r"\[(\d+(?:\s*[,，\-–—]\s*\d+)*)\]", text):
        for seg in re.split(r"\s*[,，]\s*", grp):
            seg = seg.strip()
            if not seg:
                continue
            m_rng = re.match(r"^(\d+)\s*[\-–—]\s*(\d+)$", seg)
            if m_rng:
                lo, hi = int(m_rng.group(1)), int(m_rng.group(2))
                if 0 < lo <= hi and (hi - lo) <= 30:
                    for x in range(lo, hi + 1):
                        if x not in found:
                            found.append(x)
            elif seg.isdigit():
                val = int(seg)
                if val not in found:
                    found.append(val)

    # 2. 著者-出版年制匹配（当传入文献池时自动按第一作者姓名/姓氏 + 4位年份映射回序号）
    if evidence_by_index:
        for idx, ev in evidence_by_index.items():
            p = ev.get("paper") or {}
            yr_raw = str(p.get("year") or p.get("date") or "")
            ym = re.search(r"\b((?:19|20)\d{2})\b", yr_raw)
            if not ym:
                continue
            yr = ym.group(1)
            if yr not in text:
                continue
            authors = _split_authors(p.get("authors"))
            if not authors:
                continue
            a0 = authors[0]
            if any("\u4e00" <= c <= "\u9fff" for c in a0):
                # 中文：匹配 `赵涛（2020` / `赵涛等（2020` / `赵涛和张智（2020` / `（赵涛等，2020`
                pat = rf"{re.escape(a0)}(?:[^。！？.!?（）()]{{0,12}})?[（(，,\s]{yr}[a-z]?"
                if re.search(pat, text):
                    if idx not in found:
                        found.append(idx)
            else:
                surname, _ = _extract_western_parts(a0)
                if surname and len(surname) >= 2:
                    pat_en = rf"\b{re.escape(surname)}\b(?:[^。！？!?()（）]{{0,28}})?[(（,，\s]{yr}[a-z]?"
                    if re.search(pat_en, text, re.IGNORECASE):
                        if idx not in found:
                            found.append(idx)

    return found


def _validation_body(markdown: str) -> str:
    body = re.sub(r"## 参考文献[\s\S]*$", "", markdown)
    body = re.sub(r"## [一二三四五六七八九十、]*段落证据映射[\s\S]*$", "", body)
    return body


# 需要强制挂引文的典型文献/经验指称提示词（若句子含此类词却未挂任何引文，属于真正的无出处指称，必须标 unsupported）
_UNATTRIBUTED_EMPIRICAL_MARKERS = (
    "研究表明", "研究发现", "实证表明", "实证显示", "调查显示", "数据显示", "实验表明",
    "学者指出", "已有研究", "既有研究", "学界普遍认为", "文献表明",
    "studies show", "research indicates", "empirical results show", "previous studies",
)


def _claim_sentences(
    markdown: str,
    evidence_by_index: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    body = _validation_body(markdown)
    claims = []
    section = "正文"
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip()
            continue
        # 跳过摘要、关键词、Markdown 表格行与图表题注（避免结构性元数据被误判为无证据论断）
        if stripped.startswith("|") or re.match(r"^(?:\*{0,2})(?:摘\s*要|关键词|关\s*键\s*词|Abstract|Keywords)\s*(?:\*{0,2})\s*[：:]", stripped, re.I):
            continue
        if re.match(r"^(?:表|图|Table|Figure|Fig\.)\s*\d", stripped, re.I):
            continue
        if stripped.startswith("-"):
            stripped = stripped.lstrip("- ").strip()

        para_has_citation = bool(_sentence_evidence_indices(stripped, evidence_by_index))
        # 保护西文常见缩写点（如 et al., e.g., i.e., vs.）与小数点（如 87.6%、p<0.01）不被误切句
        protected = re.sub(
            r"\b(et\s+al|e\.g|i\.e|vs|cf|fig|eq|vol|no|pp)\.",
            lambda m: m.group(1) + "\u2024",
            stripped,
            flags=re.IGNORECASE,
        )
        protected = re.sub(r"(?<=\d)\.(?=\d)", "\u2024", protected)
        for raw in re.split(r"(?<=[。！？.!?])\s*", protected):
            sentence = raw.replace("\u2024", ".").strip()
            if len(sentence) < 12:
                continue
            ev_idx = _sentence_evidence_indices(sentence, evidence_by_index)
            needs_strict_cite = any(m in sentence.lower() for m in _UNATTRIBUTED_EMPIRICAL_MARKERS)
            claims.append({
                "section": section,
                "claim": sentence[:260],
                "evidence_indices": ev_idx,
                "_para_has_citation": para_has_citation,
                "_needs_strict_cite": needs_strict_cite,
            })
    return claims


def _load_cached_ft_snippet(paper: Dict[str, Any], limit_chars: int = 30000) -> str:
    if paper.get("fulltext"):
        return str(paper["fulltext"])[:limit_chars]
    cache_p = paper.get("fulltext_cache")
    if not cache_p:
        return ""
    try:
        resolved = Path(cache_p).resolve()
        if any(resolved == d or d in resolved.parents for d in allowed_fulltext_dirs()):
            data = json.loads(resolved.read_text(encoding="utf-8"))
            return str(data.get("fulltext") or "")[:limit_chars]
    except Exception:
        pass
    return ""


def _paper_validation_text(paper: Dict[str, Any]) -> str:
    keywords = " ".join(_normalize_keywords(paper.get("keywords")))
    tags = " ".join(str(t) for t in (paper.get("tags") or []))
    ft_snip = _load_cached_ft_snippet(paper)
    parts = [
        paper.get("title", ""),
        paper.get("journal", ""),
        keywords,
        tags,
        paper.get("note", ""),
        paper.get("abstract", ""),
        ft_snip,
    ]
    return " ".join(str(part) for part in parts if part).lower()


def _claim_terms(claim: str) -> List[str]:
    terms: List[str] = []
    stopwords = {
        "研究", "文献", "相关", "现有", "显示", "因此", "这一", "进行", "通过",
        "围绕", "当前", "方面", "中的", "需要", "判断", "证据", "分支", "本轮",
        "以及", "可以", "成为", "具有", "不仅", "而且", "如果", "其中", "对于",
    }
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", claim.lower()):
        if token not in _GENERIC_STOPWORDS and token not in terms:
            terms.append(token)
    phrases = [p for p in re.findall(r"[一-鿿]{2,}", claim) if p not in stopwords]
    # 外层按词长 (3, 2, 4) 轮询全句各分句，避免首个长分句的 5/6-gram 耗尽配额而漏掉句中/句末核心术语
    for size in (3, 2, 4):
        for phrase in phrases:
            if len(phrase) < size:
                continue
            step = 1 if size <= 3 else 2
            for i in range(0, len(phrase) - size + 1, step):
                token = phrase[i:i + size]
                if token in stopwords:
                    continue
                if token not in terms:
                    terms.append(token)
                if len(terms) >= 96:
                    return terms
    return terms[:96]


_SIGNPOST_MARKERS = (
    "本文", "本章", "本节", "笔者", "见表", "见图", "如下表", "如下图",
    "研究脉络", "理论缝隙", "如下四", "如下三", "大体形成了", "呈现出以下",
)


_CONTRASTIVE_CLAIM_RE = re.compile(
    r"(?:然而|相反|但|不过|不同于|并非|质疑|争议|分化|另一方面|contrary|however|whereas|unlike|although|while)",
    re.IGNORECASE,
)

_HARD_STAT_CLAIM_RE = re.compile(
    r"(?:\b[Nn]\s*=\s*\d{2,}|\bp\s*[<≤=]\s*0\.\d+|\bR\^?2\s*=\s*0\.\d+|"
    r"问卷.{0,10}\d{2,}\s*份|回收率.{0,6}\d+(?:\.\d+)?%|回归系数.{0,8}-?\d+\.\d+)",
    re.IGNORECASE,
)


def _check_year_mismatch_in_claim(claim_txt: str, indices: List[int], evidence_by_index: Dict[int, Dict[str, Any]]) -> Optional[str]:
    """检测正文中类似 `赵涛等（2022）[1]` 或 `Smith (2021)[2]` 的显式年份是否与所引文献真实发表年份冲突。"""
    for m in re.finditer(r"[（(]\s*((?:19|20)\d{2})[a-z]?\s*[)）]\s*(?:\^\s*)?\[(\d+)\]", claim_txt):
        yr_in_text = int(m.group(1))
        ref_idx = int(m.group(2))
        if ref_idx in evidence_by_index:
            p = evidence_by_index[ref_idx].get("paper") or {}
            true_yr = _paper_year(p)
            if true_yr and str(true_yr)[:4].isdigit() and int(str(true_yr)[:4]) != yr_in_text:
                return f"正文标注年份 ({yr_in_text}) 与文献 [{ref_idx}] 真实发表年份 ({true_yr}) 不一致"
    return None


def _support_for_claim(claim: Dict[str, Any], evidence_by_index: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    clean_claim = {k: v for k, v in claim.items() if not k.startswith("_")}
    indices = claim.get("evidence_indices", [])
    claim_txt_plain = str(claim.get("claim") or "")
    has_unbacked_stat = bool(not indices and _HARD_STAT_CLAIM_RE.search(claim_txt_plain))
    if not indices:
        if has_unbacked_stat:
            return {
                **clean_claim,
                "support_level": "unsupported",
                "unbacked_statistic": True,
                "matched_indices": [],
                "matched_terms": [],
                "note": "包含未挂引文的具体统计数值/样本量，请核实是否来自真实一手数据（防统计幻觉）",
            }
        # 兼容 style-audit.md：同段已有引文支撑的作者评述句、章节导引/图表引出句（如“本文首先…”“见表1”）及结语对策推论句，只要不含“研究表明/数据显示”等无出处经验指称，记为 commentary 不扣分
        sec_name = str(claim.get("section") or "")
        is_synthesis_sec = any(k in sec_name for k in ("结语", "结论", "路径", "对策", "建议", "启示", "讨论"))
        is_signpost = any(m in claim_txt_plain for m in _SIGNPOST_MARKERS)
        if not claim.get("_needs_strict_cite") and (claim.get("_para_has_citation") or is_synthesis_sec or is_signpost):
            return {
                **clean_claim,
                "support_level": "commentary",
                "matched_indices": [],
                "matched_terms": [],
                "note": "章节导引、同段作者评述或路径推论句",
            }
        return {
            **clean_claim,
            "support_level": "unsupported",
            "matched_indices": [],
            "matched_terms": [],
            "note": "未标注证据编号 [n]",
        }

    invalid = [i for i in indices if i not in evidence_by_index]
    retracted = [i for i in indices if i in evidence_by_index and "retracted" in evidence_by_index[i].get("flags", [])]
    if invalid or retracted:
        return {
            **clean_claim,
            "support_level": "invalid",
            "matched_indices": [],
            "matched_terms": [],
            "invalid_indices": invalid,
            "retracted_indices": retracted,
            "note": "存在越界编号或疑似撤稿文献",
        }

    claim_txt = claim["claim"]
    claim_is_cjk = any("\u4e00" <= c <= "\u9fff" for c in claim_txt)
    c_terms = _claim_terms(claim_txt)
    matched_terms: List[str] = []
    matched_indices: List[int] = []
    has_abstract = False
    has_cross_lingual_abstract = False

    claim_polarity = _detect_text_polarity(claim_txt)
    paper_polarities: List[Tuple[int, str, str]] = []

    for idx in indices:
        ev = evidence_by_index[idx]
        paper = ev["paper"]
        p_text = _paper_validation_text(paper)
        paper_has_abs = bool(paper.get("abstract") or paper.get("fulltext") or paper.get("fulltext_cache"))
        if paper_has_abs:
            has_abstract = True
            paper_is_cjk = any("\u4e00" <= c <= "\u9fff" for c in str(paper.get("title") or "") + str(paper.get("abstract") or ""))
            if claim_is_cjk != paper_is_cjk:
                has_cross_lingual_abstract = True
            p_facets = extract_paper_facets(paper)
            paper_polarities.append((idx, p_facets["effect_polarity"], p_facets["core_finding"]))
        hits = [t for t in c_terms if _term_in_text(t, p_text)]
        if hits:
            matched_indices.append(idx)
            for h in hits:
                if h not in matched_terms:
                    matched_terms.append(h)

    year_mismatch = _check_year_mismatch_in_claim(claim_txt, indices, evidence_by_index)
    polarity_conflict_note = None
    if (
        claim_polarity in ("positive", "negative")
        and paper_polarities
        and not _CONTRASTIVE_CLAIM_RE.search(claim_txt)
    ):
        opposite = "negative" if claim_polarity == "positive" else "positive"
        if all(pp[1] == opposite for pp in paper_polarities):
            ref_i, _, orig_finding = paper_polarities[0]
            polarity_conflict_note = (
                f"结论方向疑似写反：正文表述为『{claim_polarity}』，但所引文献 [{ref_i}] 原文核心结论为『{opposite}』（{orig_finding[:80]}）"
            )

    if polarity_conflict_note:
        level = "contradicted"
    elif not has_abstract:
        level = "needs_fulltext_check"
    elif len(matched_terms) >= 2:
        level = "strong"
    elif len(matched_terms) == 1:
        level = "medium"
    elif has_cross_lingual_abstract:
        # 中文正文引用已获取英文摘要的外文文献时，跨语种字面词块不重合属正常现象，避免误判 weak
        level = "medium"
        matched_indices = list(indices)
    else:
        level = "weak"

    out_res: Dict[str, Any] = {
        **clean_claim,
        "support_level": level,
        "matched_indices": matched_indices,
        "matched_terms": matched_terms[:8],
    }
    if polarity_conflict_note:
        out_res["polarity_conflict"] = polarity_conflict_note
    if year_mismatch:
        out_res["year_mismatch"] = year_mismatch
    return out_res


def _validate_writing(
    markdown: str,
    selected: List[Dict[str, Any]],
    all_candidates: Optional[List[Dict[str, Any]]] = None,
    *,
    allow_primary_data: bool = False,
) -> Dict[str, Any]:
    pool = all_candidates if all_candidates is not None else selected
    evidence_by_index = {item["index"]: item for item in pool}
    claims = _claim_sentences(markdown, evidence_by_index)
    claim_results = [_support_for_claim(c, evidence_by_index) for c in claims]
    if allow_primary_data:
        for r in claim_results:
            if r.get("unbacked_statistic"):
                r["support_level"] = "commentary"
                r.pop("unbacked_statistic", None)
                r["note"] = "已声明包含作者一手实证样本/实验结果"

    counts: Counter[str] = Counter(r["support_level"] for r in claim_results)
    cited_indices = {idx for c in claims for idx in c.get("evidence_indices", []) if idx in evidence_by_index}
    usable_indices = [
        item["index"] for item in selected
        if item.get("relevance", 0) > 0 and "retracted" not in item.get("flags", [])
    ]
    unused_usable = [idx for idx in usable_indices if idx not in cited_indices]

    unsupported_claims = [r for r in claim_results if r["support_level"] == "unsupported"]
    contradicted_claims = [r for r in claim_results if r["support_level"] == "contradicted"]
    year_mismatches = [r for r in claim_results if r.get("year_mismatch")]
    unbacked_stats = [r for r in claim_results if r.get("unbacked_statistic")]
    weak_claims = [r for r in claim_results if r["support_level"] in ("weak", "needs_fulltext_check")]
    invalid_claims = [r for r in claim_results if r["support_level"] == "invalid"]

    issues = []
    if contradicted_claims:
        issues.append({
            "type": "polarity_conflict",
            "count": len(contradicted_claims),
            "examples": contradicted_claims[:5],
            "message": "部分论断与所引文献原文的正负向结论极性相反（疑似张冠李戴或结论写反）",
        })
    if year_mismatches:
        issues.append({
            "type": "year_mismatch",
            "count": len(year_mismatches),
            "examples": year_mismatches[:5],
            "message": "正文著者-出版年标注与所引文献真实年份不一致",
        })
    if unbacked_stats:
        issues.append({
            "type": "unbacked_statistics",
            "count": len(unbacked_stats),
            "examples": unbacked_stats[:5],
            "message": "检测到未挂引文的统计检验/样本量数值（若确属一手实证数据可传 --allow-primary-data）",
        })
    if unsupported_claims:
        issues.append({
            "type": "unsupported_claim",
            "count": len(unsupported_claims),
            "examples": unsupported_claims[:8],
            "message": "部分实质论断未附证据编号",
        })
    if weak_claims:
        issues.append({
            "type": "weak_or_metadata_only_support",
            "count": len(weak_claims),
            "examples": weak_claims[:8],
            "message": "部分论断与引用证据匹配较弱，或需核对原文",
        })
    if invalid_claims:
        issues.append({
            "type": "invalid_support",
            "count": len(invalid_claims),
            "examples": invalid_claims[:5],
            "message": "部分论断引用了无效或高风险证据",
        })
    if unused_usable:
        issues.append({
            "type": "unused_relevant_evidence",
            "count": len(unused_usable),
            "indices": unused_usable[:12],
            "message": "部分高相关证据未进入正文论证，可按需补充",
        })

    score = 100
    score -= min(counts.get("contradicted", 0) * 20, 50)
    score -= min(len(year_mismatches) * 8, 24)
    score -= min(counts.get("unsupported", 0) * 8, 32)
    score -= min(counts.get("weak", 0) * 6, 30)
    score -= min(counts.get("needs_fulltext_check", 0) * 5, 25)
    score -= min(counts.get("invalid", 0) * 18, 45)
    score = max(score, 0)
    status = "success" if score >= 80 and not invalid_claims and not contradicted_claims else "warning"

    recommendations = []
    if contradicted_claims:
        recommendations.append("立即核对 polarity_conflict 条目：正文结论的正负方向与所引文献原文相反，请修正表述或更换正确引文。")
    if year_mismatches:
        recommendations.append("修正正文中与参考文献真实年份不一致的年份标注。")
    if unbacked_stats:
        recommendations.append("核查未挂引文的样本量/回归显著性数值，严禁虚构一手实证数据（若确有真实数据集请加 --allow-primary-data）。")
    if unsupported_claims:
        recommendations.append("为未附编号的论断补充 [证据序号]，或删除无法由当前文献库支撑的判断。")
    if weak_claims:
        recommendations.append("对弱匹配或缺摘要证据执行 read-detail/read-detail --fulltext，并收紧过强表述。")
    if invalid_claims:
        recommendations.append("移除无效编号或疑似撤稿文献，改用摘要可追溯的高相关文献。")
    if unused_usable:
        recommendations.append("检查未使用的高相关文献，必要时补充到相应段落。")

    return {
        "status": status,
        "score": score,
        "evidence_count": len(selected),
        "cited_evidence_count": len(cited_indices),
        "checked_claims": len(claim_results),
        "support_counts": dict(counts),
        "claim_results": claim_results[:30],
        "issues": issues,
        "recommendations": recommendations,
    }


def _topic_methods(cluster_label: str, gap_title: str) -> List[str]:
    methods = ["系统性文献综述", "比较分析"]
    merged = f"{cluster_label} {gap_title}".lower()
    if any(k in merged for k in ("实证", "实验", "评估", "empirical", "experiment")):
        methods = ["实证检验 / 对照实验", "公开数据集或样本构建", "稳健性检验"]
    elif any(k in merged for k in ("比较", "跨", "语种", "comparative")):
        methods = ["跨情境/跨库比较分析", "多案例对比", "元分析"]
    return methods


def _build_topics(topic: str, papers: List[Dict[str, Any]], limit: int = 8) -> List[Dict[str, Any]]:
    candidates = _review_candidates(papers, topic)
    selected = [
        item for item in candidates
        if item.get("relevance", 0) > 0 and "retracted" not in item.get("flags", [])
    ][:max(limit, 1)]
    clusters = _review_clusters(selected, topic)
    gaps = _review_gaps(papers, topic)
    suggestions = []

    for cluster in clusters[:4]:
        cluster_indices = [paper["index"] for paper in cluster.get("papers", [])[:5]]
        matched_gap = None
        for gap in gaps:
            if gap.get("evidence_indices") and (set(cluster_indices) & set(gap.get("evidence_indices", []))):
                matched_gap = gap
                break
        if matched_gap is None and gaps:
            matched_gap = gaps[min(len(suggestions), len(gaps) - 1)]
        gap_title = matched_gap.get("title", "需进一步明确研究空白") if matched_gap else "需进一步明确研究空白"
        evidence_indices = sorted(
            set(cluster_indices + (matched_gap.get("evidence_indices", [])[:5] if matched_gap else []))
        ) or cluster_indices

        risks = []
        if not evidence_indices:
            risks.append("当前文献库证据不足，需先补充检索")
        if any(paper.get("trace_status") == "metadata_only" for paper in cluster.get("papers", [])):
            risks.append("部分证据仅有题录信息，需补摘要或原文")
        if matched_gap and matched_gap.get("matched_count", 0) == 0:
            risks.append("该方向当前库无直接命中文献，不能直接断言真实研究空白")

        suggestions.append({
            "title": f"{topic}：{cluster['label']}方向的机制与评估",
            "rationale": (
                f"当前文献库中“{cluster['label']}”聚类包含 {cluster.get('count', 0)} 篇线索；"
                f"{matched_gap.get('basis', '需结合更多检索证据判断研究空间') if matched_gap else '需结合更多检索证据判断研究空间'}"
            ),
            "evidence_indices": evidence_indices[:8],
            "possible_methods": _topic_methods(cluster["label"], gap_title),
            "risks": risks or ["需在开题前继续核对核心文献和原文证据"],
            "followup_search": [topic, cluster["label"], gap_title],
        })

    if not suggestions:
        suggestions.append({
            "title": f"{topic}的研究现状与问题界定",
            "rationale": f"当前文献库共有 {len(papers)} 篇记录，但高相关聚类不足，适合先扩大检索范围。",
            "evidence_indices": [],
            "possible_methods": ["系统性文献检索", "关键词同义扩展", "题录与摘要筛选"],
            "risks": ["证据基础不足，暂不宜直接收窄实证假设"],
            "followup_search": [topic],
        })
    return suggestions[:limit]


def cmd_write(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    papers = _load_session(_session_project(args))
    if not papers:
        output_fn({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可写作的文献，请先执行 search、batch-search 或 import"})
        return
    topic = args.topic or _session_project(args) or "当前课题"
    limit = min(args.limit or 12, len(papers))
    selected, clusters, gaps = _review_write_inputs(papers, topic, limit)
    mode = getattr(args, "mode", "draft") or "draft"
    section = getattr(args, "section", "") or ""
    if section and mode == "draft":
        mode = "section"
    markdown = _build_review_draft(topic, selected, clusters, gaps, mode=mode, section=section)
    if args.with_citations:
        markdown = _append_references(markdown, selected, args.citation_style or "gbt7714")
    validation = _validate_writing(markdown, selected) if getattr(args, "validate", False) else None

    output_path = Path(args.output) if args.output else None
    if args.format == "docx":
        if output_path is None:
            output_path = Path("review.docx")
        docx_res = _write_docx_from_markdown(markdown, output_path)
        if docx_res.get("status") == "error":
            output_fn(docx_res)
            return
        res: Dict[str, Any] = {
            **docx_res,
            "project": _session_project(args),
            "topic": topic,
            "mode": mode,
            "count": len(selected),
            "generation_mode": "evidence_scaffold",
            "agent_hint": "脚本生成结构化证据骨架；正式学术成稿建议由 Agent 基于 review 证据与 style-audit.md 深化后运行 validate",
        }
        if validation:
            res["validation"] = validation
        output_fn(res)
        return

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")

    if args.raw:
        print(markdown)
    else:
        payload: Dict[str, Any] = {
            "status": "success",
            "project": _session_project(args),
            "topic": topic,
            "mode": mode,
            "format": args.format,
            "count": len(selected),
            "output_file": str(output_path) if output_path else None,
            "generation_mode": "evidence_scaffold",
            "agent_hint": "脚本生成结构化证据骨架；正式学术成稿建议由 Agent 基于 review 证据与 style-audit.md 深化后运行 validate",
            "clusters": clusters,
            "gaps": gaps,
            "markdown": markdown,
        }
        if validation:
            payload["validation"] = validation
        output_fn(payload)


def cmd_topics(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    papers = _load_session(_session_project(args))
    if not papers:
        output_fn({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可生成选题的数据，请先执行 search、batch-search 或 import"})
        return
    topic = args.topic or _session_project(args) or "当前课题"
    suggestions = _build_topics(topic, papers, args.limit or 8)
    if getattr(args, "verify_gaps", False):
        yr_from = datetime.now().year - 4
        for s in suggestions:
            q = " ".join(s.get("followup_search", [])[:2]) or topic
            proof = _probe_openalex_hit_count(q, yr_from)
            if proof:
                s["search_proof"] = proof
    output_fn({
        "status": "success",
        "project": _session_project(args),
        "topic": topic,
        "total": len(papers),
        "count": len(suggestions),
        "topics": suggestions,
    })


def cmd_validate(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    papers = _load_session(_session_project(args))
    if not papers:
        output_fn({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可校验的文献，请先执行 search、batch-search 或 import"})
        return
    topic = args.topic or _session_project(args) or "当前课题"
    limit = min(args.limit or 12, len(papers))
    all_candidates = _review_candidates(papers, topic)
    selected, clusters, gaps = _review_write_inputs(papers, topic, limit)
    target_file = getattr(args, "file", None) or getattr(args, "filepath", None)
    if target_file:
        fpath = Path(target_file)
        if not fpath.exists():
            output_fn({"status": "error", "code": "FILE_NOT_FOUND", "message": f"待校验文稿不存在: {fpath}"})
            return
        if fpath.suffix.lower() in (".docx", ".pdf"):
            from docx_tools import cmd_read_paper
            doc_out: Dict[str, Any] = {}
            cmd_read_paper(
                argparse.Namespace(filepath=str(fpath), numbered=False, toc=False, section=None, max_chars=None, output=None, raw=False),
                output_fn=doc_out.update,
            )
            if doc_out.get("status") == "error":
                output_fn(doc_out)
                return
            markdown = str(doc_out.get("content") or doc_out.get("text") or "")
        else:
            markdown = fpath.read_text(encoding="utf-8")
    else:
        markdown = _build_review_draft(topic, selected, clusters, gaps, mode="draft")
    validation = _validate_writing(
        markdown,
        selected,
        all_candidates=all_candidates,
        allow_primary_data=bool(getattr(args, "allow_primary_data", False)),
    )
    validation.update({
        "project": _session_project(args),
        "topic": topic,
        "checked_file": str(target_file) if target_file else None,
    })
    output_fn(validation)


def cmd_review(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    project = _session_project(args)
    papers = _load_session(project)
    if not papers:
        output_fn({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可生成综述的数据，请先执行 search、batch-search 或 import"})
        return

    topic = args.topic or project or "当前课题"
    candidates = _review_candidates(papers, topic)
    auto_detail = None
    if getattr(args, "auto_detail", False):
        auto_detail = _auto_detail_for_review(papers, candidates, getattr(args, "detail_top_n", 5) or 5, project)
        if auto_detail.get("updated", 0) > 0:
            papers = _load_session(project)
            candidates = _review_candidates(papers, topic)

    limit = min(args.limit or 12, len(papers))
    selected = _select_review_papers(papers, topic, limit)
    clusters = _review_clusters(selected, topic) if getattr(args, "cluster", False) else None
    gaps = _review_gaps(papers, topic) if getattr(args, "gaps", False) else None
    if gaps and getattr(args, "verify_gaps", False):
        gaps = _verify_gaps_live(gaps, topic)
    evidence = [_paper_evidence(item["paper"], item["index"]) for item in selected]
    comparison_matrix, _ = build_comparison_matrix(selected)
    markdown = _build_review_markdown(topic, selected, len(papers), candidates, clusters, gaps)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
    if args.raw:
        print(markdown)
    else:
        output_fn({
            "status": "success",
            "project": project,
            "topic": topic,
            "count": len(selected),
            "total": len(papers),
            "output_file": args.output,
            "auto_detail": auto_detail,
            "comparison_matrix": comparison_matrix,
            "clusters": clusters,
            "gaps": gaps,
            "evidence": evidence,
            "markdown": markdown,
        })


def cmd_projects(_args, output_fn: Callable[[Any], None] = _default_output) -> None:
    base = workspace_state_dir(create=False) / "projects"
    projects = []
    if base.exists():
        for project_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            papers = _load_session(project_dir.name)
            projects.append({
                "name": project_dir.name,
                "count": len(papers),
                "session_file": str(project_dir / "session.json"),
            })
    output_fn({"status": "success", "count": len(projects), "projects": projects})


def _dedupe_library_papers(papers: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], int]:
    """对课题文献库执行跨源智能去重，合并重复条目的缺失字段、tags 与 note。"""
    merged_list: List[Dict[str, Any]] = []
    index_by_key: Dict[str, int] = {}
    removed = 0

    for p in papers:
        cid = str(p.get("cnki_id") or "").upper().strip()
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(p.get("doi") or "").lower().strip())
        raw_t = str(p.get("title") or "").lower().strip()
        is_cjk_t = any("\u4e00" <= c <= "\u9fff" for c in raw_t)
        if is_cjk_t:
            norm_title = re.sub(r"\W+", "", raw_t)
        else:
            norm_title = re.sub(r"[^\w\s]", "", re.sub(r"\s+", " ", raw_t)).strip()

        # 双语同篇去重键：若中英文双语同篇（如中文期刊同时被 OpenAlex 收录英文题录），第一作者姓名汉字相同 + 第二作者汉字相同 + 同年
        au_list = _split_authors(p.get("authors"))
        cjk_authors_sig = "|".join(
            "".join(sorted(c for c in a if "\u4e00" <= c <= "\u9fff"))
            for a in au_list[:2]
            if any("\u4e00" <= c <= "\u9fff" for c in a)
        )
        yr_str = str(_paper_year(p) or "")
        bilingual_key = f"bi_au:{cjk_authors_sig}:{yr_str}" if (len(cjk_authors_sig) >= 4 and yr_str) else ""

        hit_idx = None
        for k in (
            f"cid:{cid}" if cid else "",
            f"doi:{doi}" if doi else "",
            f"title:{norm_title}" if norm_title else "",
            bilingual_key,
        ):
            if k and k in index_by_key:
                # bilingual_key 仅在一中一英时触发合并，避免同作者同年发表的两篇不同中文论文被误合并！
                if k == bilingual_key:
                    prev_is_cjk = any("\u4e00" <= c <= "\u9fff" for c in str(merged_list[index_by_key[k]].get("title") or ""))
                    if prev_is_cjk == is_cjk_t:
                        continue
                hit_idx = index_by_key[k]
                break

        if hit_idx is not None:
            removed += 1
            base = merged_list[hit_idx]
            for field_k, field_v in p.items():
                if field_k == "tags" and field_v:
                    existing_tags = list(base.get("tags") or [])
                    for t in field_v:
                        if t not in existing_tags:
                            existing_tags.append(t)
                    base["tags"] = existing_tags
                elif field_k == "note" and field_v:
                    if not base.get("note"):
                        base["note"] = field_v
                    elif field_v not in base["note"]:
                        base["note"] = f"{base['note']}; {field_v}"
                elif field_v and not base.get(field_k):
                    base[field_k] = field_v
        else:
            new_idx = len(merged_list)
            merged_list.append(dict(p))
            for k in (
                f"cid:{cid}" if cid else "",
                f"doi:{doi}" if doi else "",
                f"title:{norm_title}" if norm_title else "",
                bilingual_key,
            ):
                if k:
                    index_by_key[k] = new_idx

    return merged_list, removed


def cmd_library(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    project = _session_project(args)
    papers = _load_session(project)
    if not papers:
        output_fn({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可查看或整理的文献，请先执行 search、batch-search 或 import"})
        return

    actions_taken: List[str] = []
    dirty = False

    # 1. 标注（在删除/筛选前按当前序号执行，符合直觉）
    target_indices_raw = getattr(args, "indices", None)
    tag_raw = getattr(args, "tag", None)
    clear_tags = getattr(args, "clear_tags", False)
    note_raw = getattr(args, "note", None)

    if tag_raw is not None or clear_tags or note_raw is not None:
        if not target_indices_raw:
            output_fn({
                "status": "error",
                "code": "MISSING_INDICES",
                "message": "使用 --tag / --clear-tags / --note 时请通过 --indices 指定目标文献序号（如 --indices 1,3）",
            })
            return
        try:
            ann_idx = parse_indices_spec(target_indices_raw, len(papers), preserve_order=True)
        except ValueError:
            ann_idx = []
        if not ann_idx:
            output_fn({"status": "error", "code": "INVALID_INDICES", "message": f"无效序号 '{target_indices_raw}'（当前共 {len(papers)} 篇）"})
            return

        new_tags = [t.strip() for t in re.split(r"[,，;；]+", tag_raw) if t.strip()] if tag_raw else []
        for idx in ann_idx:
            if clear_tags:
                papers[idx]["tags"] = []
            if new_tags:
                cur_tags = list(papers[idx].get("tags") or [])
                for t in new_tags:
                    if t not in cur_tags:
                        cur_tags.append(t)
                papers[idx]["tags"] = cur_tags
            if note_raw is not None:
                papers[idx]["note"] = note_raw.strip()
        dirty = True
        actions_taken.append(f"annotated:{len(ann_idx)}")

    # 2. 精选保留 (--keep) 或 剔除噪音 (--remove)
    keep_raw = getattr(args, "keep", None)
    remove_raw = getattr(args, "remove", None)
    if keep_raw and remove_raw:
        output_fn({"status": "error", "code": "CONFLICTING_OPTIONS", "message": "--keep 与 --remove 不能同时指定"})
        return

    if keep_raw:
        try:
            keep_idx = parse_indices_spec(keep_raw, len(papers), preserve_order=True)
        except ValueError:
            keep_idx = []
        if not keep_idx:
            output_fn({"status": "error", "code": "INVALID_INDICES", "message": f"无效保留序号 '{keep_raw}'（当前共 {len(papers)} 篇）"})
            return
        before_n = len(papers)
        papers = [papers[i] for i in keep_idx]
        dirty = True
        actions_taken.append(f"kept:{len(papers)}/{before_n}")

    elif remove_raw:
        try:
            rem_set = set(parse_indices_spec(remove_raw, len(papers), preserve_order=False))
        except ValueError:
            rem_set = set()
        if not rem_set:
            output_fn({"status": "error", "code": "INVALID_INDICES", "message": f"无效剔除序号 '{remove_raw}'（当前共 {len(papers)} 篇）"})
            return
        papers = [p for i, p in enumerate(papers) if i not in rem_set]
        dirty = True
        actions_taken.append(f"removed:{len(rem_set)}")

    # 3. 跨轮/跨源去重 (--dedupe)
    if getattr(args, "dedupe", False):
        papers, dedup_removed = _dedupe_library_papers(papers)
        if dedup_removed > 0:
            dirty = True
        actions_taken.append(f"deduped:{dedup_removed}")

    if dirty:
        _save_session(papers, project=project)

    limit = args.limit or len(papers)
    rows = [_paper_summary(p, i + 1) for i, p in enumerate(papers[:limit])]
    out: Dict[str, Any] = {
        "status": "success",
        "project": project,
        "count": len(papers),
        "results": rows,
    }
    if actions_taken:
        out["actions"] = actions_taken
    output_fn(out)
