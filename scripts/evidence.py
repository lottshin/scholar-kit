"""
evidence.py - 会话/课题文献库、学科中立的主题聚类、研究空白信号、证据骨架与论断校验

职责定位（「脚本是手，Agent 是脑」）：
- 脚本负责确定性证据整理（相关性初筛、基于语料真实关键词的主题聚类、可量化研究空白统计、引用编号与词项支撑核查）
- 正式学术综述撰写与润色优先由 Agent 基于 `review` 返回的结构化证据包及 `references/style-audit.md` 完成，再用 `validate` 闭环核查
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cnki import batch_read_detail
from config import workspace_state_dir
from docx_tools import _write_docx_from_markdown
from formatter import generate_reference_list


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
            seen_keys = set()
            merged = []
            for p in existing + papers:
                key = p.get("doi") or p.get("url") or p.get("title", "")
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    merged.append(p)
                elif not key:
                    merged.append(p)
            papers = merged
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
    return evidence


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
        claims = []
        for item in items:
            p = item["paper"]
            abstract = str(p.get("abstract") or "").strip()
            if abstract:
                claims.append(abstract[:90])
            evidence.append({
                "index": item["index"],
                "title": p.get("title", ""),
                "trace_status": "abstract" if abstract else "metadata_only",
                "abstract_excerpt": abstract[:160],
            })
        result.append({
            "label": label,
            "count": len(items),
            "papers": evidence,
            "synthesis": "；".join(claims[:3]) if claims else "",
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

    # 3. 实证/实验/数据集评估线索
    empirical_terms = (
        "实证", "实验", "数据集", "问卷", "调研", "访谈", "田野", "回归", "样本", "临床",
        "empirical", "experiment", "dataset", "benchmark", "survey", "trial", "cohort", "evaluation",
    )
    empirical_hits = []
    # 4. 比较/跨情境/综述与元分析线索
    comparative_terms = (
        "比较", "对比", "跨国", "跨区域", "元分析", "系统综述", "消融",
        "comparative", "comparison", "cross-national", "meta-analysis", "systematic review", "ablation",
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
            "title": "实证数据或实验评估文献偏少",
            "dimension": "实证/评估",
            "matched_count": len(empirical_hits),
            "total": total,
            "evidence_indices": empirical_hits[:10],
            "basis": f"当前文献库 {total} 篇中，明确包含实验、样本、数据集或调研线索的文献约 {len(empirical_hits)} 篇。",
        })

    if len(comparative_hits) <= max(1, total // 10):
        gaps.append({
            "title": "横向比较或系统评估视角较少",
            "dimension": "比较/整合",
            "matched_count": len(comparative_hits),
            "total": total,
            "evidence_indices": comparative_hits[:10],
            "basis": f"当前文献库 {total} 篇中，涉及对比实验、跨情境比较或元分析的文献约 {len(comparative_hits)} 篇。",
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
        lines.extend(["", "## 二、主要研究脉络与主题分支"])
        for cluster in cluster_list:
            indices = _cluster_evidence_indices(cluster)
            if cluster.get("synthesis"):
                body = f"代表性摘要线索包括：{cluster['synthesis']}"
            else:
                body = "该组文献目前缺少摘要文本，仅能作为题录线索列出，具体结论待核对原文后补入"
            lines.extend([
                f"### {cluster['label']}（{cluster['count']} 篇）",
                f"该分支共 {cluster['count']} 篇相关文献{indices}。{body}。",
                "",
            ])
        lines.extend(["## 三、样本覆盖局限与补检方向"])
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

    selected = [item["paper"] for item in targets]
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


def _sentence_evidence_indices(text: str) -> List[int]:
    return [int(i) for i in re.findall(r"\[(\d+)\]", text)]


def _validation_body(markdown: str) -> str:
    body = re.sub(r"## 参考文献[\s\S]*$", "", markdown)
    body = re.sub(r"## [一二三四五六七八九十、]*段落证据映射[\s\S]*$", "", body)
    return body


def _claim_sentences(markdown: str) -> List[Dict[str, Any]]:
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
        if stripped.startswith("-"):
            stripped = stripped.lstrip("- ").strip()
        for raw in re.split(r"(?<=[。！？.!?])\s*", stripped):
            sentence = raw.strip()
            if len(sentence) < 12:
                continue
            claims.append({
                "section": section,
                "claim": sentence[:260],
                "evidence_indices": _sentence_evidence_indices(sentence),
            })
    return claims


def _paper_validation_text(paper: Dict[str, Any]) -> str:
    keywords = " ".join(_normalize_keywords(paper.get("keywords")))
    tags = " ".join(str(t) for t in (paper.get("tags") or []))
    parts = [
        paper.get("title", ""),
        paper.get("journal", ""),
        keywords,
        tags,
        paper.get("note", ""),
        paper.get("abstract", ""),
        paper.get("fulltext", ""),
    ]
    return " ".join(str(part) for part in parts if part).lower()


def _claim_terms(claim: str) -> List[str]:
    terms: List[str] = []
    stopwords = {
        "研究", "文献", "相关", "现有", "显示", "因此", "这一", "进行", "通过",
        "围绕", "当前", "方面", "中的", "需要", "判断", "证据", "分支", "本轮",
    }
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", claim.lower()):
        if token not in _GENERIC_STOPWORDS and token not in terms:
            terms.append(token)
    for phrase in re.findall(r"[一-鿿]{2,}", claim):
        if phrase in stopwords:
            continue
        for size in (6, 5, 4, 3, 2):
            if len(phrase) < size:
                continue
            for i in range(0, len(phrase) - size + 1):
                token = phrase[i:i + size]
                if token in stopwords:
                    continue
                if token not in terms:
                    terms.append(token)
                if len(terms) >= 24:
                    return terms
    return terms[:24]


def _support_for_claim(claim: Dict[str, Any], evidence_by_index: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    indices = claim.get("evidence_indices", [])
    if not indices:
        return {
            **claim,
            "support_level": "unsupported",
            "matched_indices": [],
            "matched_terms": [],
            "note": "未标注证据编号 [n]",
        }

    invalid = [i for i in indices if i not in evidence_by_index]
    retracted = [i for i in indices if i in evidence_by_index and "retracted" in evidence_by_index[i].get("flags", [])]
    if invalid or retracted:
        return {
            **claim,
            "support_level": "invalid",
            "matched_indices": [],
            "matched_terms": [],
            "invalid_indices": invalid,
            "retracted_indices": retracted,
            "note": "存在越界编号或疑似撤稿文献",
        }

    c_terms = _claim_terms(claim["claim"])
    matched_terms: List[str] = []
    matched_indices: List[int] = []
    has_abstract = False

    for idx in indices:
        ev = evidence_by_index[idx]
        paper = ev["paper"]
        if paper.get("abstract") or paper.get("fulltext"):
            has_abstract = True
        p_text = _paper_validation_text(paper)
        hits = [t for t in c_terms if _term_in_text(t, p_text)]
        if hits:
            matched_indices.append(idx)
            for h in hits:
                if h not in matched_terms:
                    matched_terms.append(h)

    if not has_abstract:
        level = "needs_fulltext_check"
    elif len(matched_terms) >= 2:
        level = "strong"
    elif len(matched_terms) == 1:
        level = "medium"
    else:
        level = "weak"

    return {
        **claim,
        "support_level": level,
        "matched_indices": matched_indices,
        "matched_terms": matched_terms[:8],
    }


def _validate_writing(markdown: str, selected: List[Dict[str, Any]]) -> Dict[str, Any]:
    evidence_by_index = {item["index"]: item for item in selected}
    claims = _claim_sentences(markdown)
    claim_results = [_support_for_claim(c, evidence_by_index) for c in claims]

    counts: Counter[str] = Counter(r["support_level"] for r in claim_results)
    cited_indices = {idx for c in claims for idx in c.get("evidence_indices", []) if idx in evidence_by_index}
    usable_indices = [
        item["index"] for item in selected
        if item.get("relevance", 0) > 0 and "retracted" not in item.get("flags", [])
    ]
    unused_usable = [idx for idx in usable_indices if idx not in cited_indices]

    unsupported_claims = [r for r in claim_results if r["support_level"] == "unsupported"]
    weak_claims = [r for r in claim_results if r["support_level"] in ("weak", "needs_fulltext_check")]
    invalid_claims = [r for r in claim_results if r["support_level"] == "invalid"]

    issues = []
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
            "indices": unused_usable[:12],
            "message": "部分高相关证据未进入正文论证，可按需补充",
        })

    score = 100
    score -= min(counts.get("unsupported", 0) * 8, 32)
    score -= min(counts.get("weak", 0) * 6, 30)
    score -= min(counts.get("needs_fulltext_check", 0) * 5, 25)
    score -= min(counts.get("invalid", 0) * 18, 45)
    score = max(score, 0)
    status = "success" if score >= 80 and not invalid_claims else "warning"

    recommendations = []
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
    selected, clusters, gaps = _review_write_inputs(papers, topic, limit)
    if args.file:
        markdown = Path(args.file).read_text(encoding="utf-8")
    else:
        markdown = _build_review_draft(topic, selected, clusters, gaps, mode="draft")
    validation = _validate_writing(markdown, selected)
    validation.update({
        "project": _session_project(args),
        "topic": topic,
        "checked_file": args.file,
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
    evidence = [_paper_evidence(item["paper"], item["index"]) for item in selected]
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
        doi = str(p.get("doi") or "").lower().strip()
        norm_title = re.sub(r"[^\w\s]", "", re.sub(r"\s+", " ", str(p.get("title") or "").lower().strip()))

        hit_idx = None
        for k in (f"cid:{cid}" if cid else "", f"doi:{doi}" if doi else "", f"title:{norm_title}" if norm_title else ""):
            if k and k in index_by_key:
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
            for k in (f"cid:{cid}" if cid else "", f"doi:{doi}" if doi else "", f"title:{norm_title}" if norm_title else ""):
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
