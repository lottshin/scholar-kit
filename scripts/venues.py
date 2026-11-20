"""
venues.py - 国际学术期刊与会议质量离线识别引擎

内置轻量级离线压缩索引（scripts/data/venue_index.json.gz，约 224 KB）：
  1. Web of Science 核心合集（SCI / SSCI / A&HCI / ESCI，源自 Wikidata P8875/P236 CC0 公共领域数据，30,300+ ISSN）
  2. 中国计算机学会推荐国际学术会议和期刊目录（CCF-A / CCF-B / CCF-C，2026 第七版）
  3. 中国科学院文献情报中心《国际期刊预警名单》（严格遵循官方“不跨年简单累加”原则：
     2024–2025 在榜期刊标记为当前预警 `is_warning=True` 并附预警原因；2023 曾上榜但后续已调出期刊仅作历史提示）
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

_INDEX_CACHE: Optional[Dict[str, Any]] = None

_WOS_BITS = (
    (1, "SCI"),
    (2, "SSCI"),
    (4, "AHCI"),
    (8, "ESCI"),
)

_CN_CORE_TOKENS = {"北大核心", "CSSCI", "CSCD", "AMI", "WJCI", "EI"}


def _norm_issn(raw: Any) -> str:
    s = re.sub(r"[^0-9Xx]", "", str(raw or "")).upper()
    return s if len(s) == 8 else ""


def _extract_issns(paper: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for key in ("issn", "issns", "eissn", "issn_l"):
        val = paper.get(key)
        if not val:
            continue
        items = val if isinstance(val, (list, tuple, set)) else re.split(r"[,;/\s]+", str(val))
        for it in items:
            ni = _norm_issn(it)
            if ni and ni not in out:
                out.append(ni)
    return out


def _norm_venue_title(raw: Any) -> str:
    s = str(raw or "").strip()
    if "/" in s:
        left, right = [p.strip() for p in s.split("/", 1)]
        if left and right and left.lower() == right.lower():
            s = left
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^the\s+", "", s)
    return s


def _norm_conf_title_candidates(raw: Any) -> List[str]:
    """对会议录名称剥离年份、届数与 Proceedings of 等前后缀以匹配 CCF 标准全称。"""
    base = _norm_venue_title(raw)
    if not base:
        return []
    cands = [base]
    cleaned = re.sub(r"\b(?:19|20)\d{2}\b", " ", base)
    cleaned = re.sub(r"\b\d+(?:st|nd|rd|th)\b", " ", cleaned)
    cleaned = re.sub(
        r"^(?:proceedings\s+of\s+(?:the\s+)?|annual\s+|ieee\s+cvf\s+|ieee\s+|acm\s+|international\s+)+",
        "",
        cleaned,
    ).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if cleaned and cleaned not in cands:
        cands.append(cleaned)
    with_intl = f"international {cleaned}".strip()
    if cleaned and with_intl not in cands:
        cands.append(with_intl)
    return cands


def load_venue_index() -> Dict[str, Any]:
    global _INDEX_CACHE
    if _INDEX_CACHE is not None:
        return _INDEX_CACHE
    gz_path = Path(__file__).resolve().parent / "data" / "venue_index.json.gz"
    if not gz_path.is_file():
        _INDEX_CACHE = {}
        return _INDEX_CACHE
    try:
        with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
            _INDEX_CACHE = json.load(fh)
    except Exception:
        _INDEX_CACHE = {}
    return _INDEX_CACHE


def annotate_venue_quality(paper: Dict[str, Any]) -> Dict[str, Any]:
    """原地为单篇论文补充国际核心收录标签（SCI/SSCI/AHCI/ESCI/CCF）及中科院预警期刊标识。"""
    if not isinstance(paper, dict):
        return paper

    idx = load_venue_index()
    if not idx:
        return paper

    issns = _extract_issns(paper)
    raw_venue = str(paper.get("journal") or paper.get("venue") or paper.get("booktitle") or "").strip()
    nt = _norm_venue_title(raw_venue)
    url = str(paper.get("url") or "")

    issn_wos = idx.get("issn_wos") or {}
    title_wos = idx.get("title_wos") or {}
    ccf_journals = idx.get("ccf_journals") or {}
    ccf_confs = idx.get("ccf_confs") or {}
    ccf_abbr_confs = idx.get("ccf_abbr_confs") or {}
    ccf_dblp = idx.get("ccf_dblp") or {}
    cas_active_issn = idx.get("cas_active_issn") or {}
    cas_active_title = idx.get("cas_active_title") or {}
    cas_hist_issn = idx.get("cas_hist_issn") or {}
    cas_hist_title = idx.get("cas_hist_title") or {}

    tags: List[str] = []

    # 1. WoS 核心合集（ISSN 精确优先，无 ISSN 时回退规范化刊名）
    wos_mask = 0
    for ni in issns:
        wos_mask |= int(issn_wos.get(ni, 0))
    if not wos_mask and nt:
        wos_mask = int(title_wos.get(nt, 0))
    for bit, label in _WOS_BITS:
        if wos_mask & bit:
            tags.append(label)
    # 若已命中 SCI/SSCI/AHCI，不再冗余叠加预备库 ESCI
    if "ESCI" in tags and any(t in tags for t in ("SCI", "SSCI", "AHCI")):
        tags.remove("ESCI")

    # 2. CCF 推荐目录（DBLP 路径 -> 期刊规范全称 -> 会议规范全称/括号缩写）
    ccf_rank = ""
    if "dblp" in url:
        m_dblp = re.search(r"(?:db|rec)/((?:journals|conf)/[^/#?]+)", url)
        if m_dblp:
            ccf_rank = ccf_dblp.get(m_dblp.group(1).rstrip("/"), "")
    if not ccf_rank and nt:
        ccf_rank = ccf_journals.get(nt, "")
    if not ccf_rank and raw_venue:
        for cand in _norm_conf_title_candidates(raw_venue):
            if cand in ccf_confs:
                ccf_rank = ccf_confs[cand]
                break
        if not ccf_rank:
            for m_ab in re.finditer(r"\(([A-Z]{4,12})\)", raw_venue):
                ab = m_ab.group(1)
                if ab in ccf_abbr_confs:
                    ccf_rank = ccf_abbr_confs[ab]
                    break
    if ccf_rank:
        tags.append(f"CCF-{ccf_rank}")

    # 3. 中科院国际期刊预警名单（区分在榜预警 vs 历史已调出）
    active_warn = None
    for ni in issns:
        if ni in cas_active_issn:
            active_warn = cas_active_issn[ni]
            break
    if active_warn is None and nt and nt in cas_active_title:
        active_warn = cas_active_title[nt]

    hist_warn = None
    if active_warn is None:
        for ni in issns:
            if ni in cas_hist_issn:
                hist_warn = cas_hist_issn[ni]
                break
        if hist_warn is None and nt and nt in cas_hist_title:
            hist_warn = cas_hist_title[nt]

    if active_warn:
        yr = active_warn.get("year")
        reason = active_warn.get("reason", "")
        warn_tag = f"CAS预警({yr})"
        tags.append(warn_tag)
        paper["is_warning"] = True
        paper["cas_warning"] = {
            "year": yr,
            "reason": reason,
            "status": "active",
            "label": f"中科院预警({yr}:{reason})" if reason else f"中科院预警({yr})",
        }
        paper["is_core"] = False
        paper["venue_tier"] = "cas_warning"
    else:
        paper.pop("is_warning", None)
        if hist_warn:
            paper["cas_warning_history"] = f"曾列中科院预警({hist_warn.get('year')},已调出)"

    if tags:
        existing_tags = [t for t in (paper.get("venue_tags") or []) if isinstance(t, str)]
        for t in tags:
            if t not in existing_tags:
                existing_tags.append(t)
        paper["venue_tags"] = existing_tags

    # 非预警且命中高水平国际索引（SCI / SSCI / AHCI / CCF-A / CCF-B）时标记为核心来源
    high_intl = [t for t in tags if t in ("SCI", "SSCI", "AHCI", "CCF-A", "CCF-B")]
    if not active_warn and high_intl:
        paper["is_core"] = True
        prev_core = [c.strip() for c in str(paper.get("core_type") or "").split(",") if c.strip()]
        for t in high_intl:
            if t not in prev_core:
                prev_core.append(t)
        paper["core_type"] = ",".join(prev_core)
        paper["venue_tier"] = "core"
    elif not active_warn and any(t in tags for t in ("ESCI", "CCF-C")) and paper.get("venue_tier") in (None, "", "general"):
        paper["venue_tier"] = "indexed"

    return paper


def parse_international_core_tokens(core_raw: Optional[str]) -> Set[str]:
    """提取 --core 参数中的国际核心筛选令牌（忽略仅面向知网的 北大核心/CSSCI 等令牌）。"""
    if not core_raw:
        return set()
    wanted: Set[str] = set()
    for part in re.split(r"[,，;/\s]+", str(core_raw)):
        tok = part.strip()
        if not tok or tok in _CN_CORE_TOKENS:
            continue
        up = tok.upper().replace("A&HCI", "AHCI")
        if up in ("SCI", "SCIE"):
            wanted.add("SCI")
        elif up == "SSCI":
            wanted.add("SSCI")
        elif up == "AHCI":
            wanted.add("AHCI")
        elif up == "ESCI":
            wanted.add("ESCI")
        elif up == "CCF":
            wanted.update({"CCF-A", "CCF-B", "CCF-C"})
        elif up in ("CCF-A", "CCF-B", "CCF-C"):
            wanted.add(up)
        elif up in ("CCFA", "CCFB", "CCFC"):
            wanted.add(f"CCF-{up[-1]}")
        elif up in ("CORE", "WOS", "JCR"):
            wanted.update({"SCI", "SSCI", "AHCI", "CCF-A", "CCF-B"})
    return wanted


def matches_international_core(paper: Dict[str, Any], wanted_tokens: Set[str]) -> bool:
    """判断单篇论文是否满足国际核心过滤（在榜预警期刊一律剔除）。"""
    if paper.get("is_warning"):
        return False
    if not wanted_tokens:
        return True
    p_tags = set(paper.get("venue_tags") or [])
    return bool(p_tags & wanted_tokens)
