"""
sections.py - 学术长文与知网硕博学位论文目录结构识别与按章节精准切片模块

核心设计：
1. 自动过滤学位论文前面的「目录页（虚线/页码引导符）」与重复短桩行，避免把目录行误当成正文；
2. 两遍扫描推断文档自身的编号体系（学位论文 `第X章` 体系 / 中文社科 `一、（一）1.` 体系 / 自然科学与英文 `1 / 1.1 / 1.1.1` 体系）；
3. 父章节（如 `第3章 研究设计`）切片时自动包含下辖全部子节（`3.1`、`3.2`...），合并重叠区间并保留 `[第 N 页]` 页码锚点。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_PAGE_MARKER_RE = re.compile(r"^\[第\s*(\d+)\s*页\]$")

# 目录页特征：标题头或行尾带点线/长空白引导页码（如 `第1章 绪论 ........... 1` 或 `3.2 模型设定   28`）
_TOC_HEADER_RE = re.compile(r"^(?:目\s*录|contents|table\s+of\s+contents)$", re.IGNORECASE)
_TOC_LEADER_LINE_RE = re.compile(
    r"(?:[.\u2026·•⋯\-_]{3,}|\u3000{2,}|\s{4,})\s*(?:[IVXLCDM]+|\d+)\s*$",
    re.IGNORECASE,
)
_CHAPTER_TRAILING_PAGE_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百零\d]+\s*[章篇节]|[一二三四五六七八九十]+、|\d+(?:\.\d+){0,2})\s*.+?\s+\d{1,3}$"
)

_STANDALONE_MAJOR_TITLES = {
    "摘要", "内容提要", "abstract", "executive summary",
    "引言", "前言", "绪论", "导论", "introduction",
    "文献综述", "相关工作", "related work", "literature review", "background",
    "理论分析", "理论分析与研究假说", "理论机制与研究假说", "研究假说",
    "研究设计", "数据与方法", "材料与方法", "研究方法", "模型设定", "识别策略",
    "methodology", "methods", "materials and methods", "experimental setup", "data and methodology",
    "实证分析", "实证结果", "实证结果与分析", "实验结果", "结果与分析", "结果与讨论",
    "results", "empirical results", "experiments", "results and discussion", "discussion",
    "稳健性检验", "内生性检验", "机制检验", "机制分析", "异质性分析", "进一步分析", "拓展性分析",
    "研究结论", "结论", "结论与启示", "结论与政策建议", "结论与展望", "结语",
    "conclusion", "conclusions", "concluding remarks",
    "参考文献", "references", "bibliography",
    "附录", "致谢", "appendix", "acknowledgments", "acknowledgements",
}

# 学术概念同义词扩展（当用户搜 `--section "方法"` 而原文叫 `三、研究设计与识别策略` 时自动命中）
_SECTION_CONCEPT_ALIASES: Dict[str, Tuple[str, ...]] = {
    "引言": ("引言", "前言", "绪论", "导论", "问题的提出", "introduction"),
    "综述": ("文献综述", "文献回顾", "相关研究", "相关工作", "literature", "related work"),
    "理论": ("理论", "假说", "假设", "机制", "theoretical", "hypothesis", "framework"),
    "方法": ("研究设计", "研究方法", "数据", "样本", "变量", "模型", "识别策略", "材料与方法", "method", "design", "setup"),
    "研究设计": ("研究设计", "模型设定", "变量", "数据来源", "样本选择", "识别策略", "research design", "methodology"),
    "实证": ("实证", "回归", "估计结果", "基准结果", "检验结果", "实验结果", "empirical", "results", "experiments"),
    "稳健性": ("稳健性", "内生性", "工具变量", "安慰剂", "平行趋势", "robustness", "endogeneity"),
    "机制": ("机制", "中介", "渠道", "调节", "异质性", "进一步分析", "mechanism", "channel", "heterogeneity"),
    "结论": ("结论", "启示", "政策建议", "结语", "研究展望", "conclusion", "implication", "discussion"),
}


def _norm_heading_key(title: str) -> str:
    s = re.sub(r"^#+\s*", "", title or "").strip().lower()
    s = re.sub(r"[\s\u3000:：、.．\-—_（）()\[\]]+", "", s)
    return s


def _looks_like_non_heading(line: str) -> bool:
    """排除普通正文长句、表格数值行、公式编号或页眉噪词。"""
    s = line.strip()
    if not s or len(s) < 2 or len(s) > 68:
        return True
    if _PAGE_MARKER_RE.match(s):
        return True
    # 以句末或分句标点结尾的几乎必为正文句子或图注说明
    if s[-1] in "。！？；，,;:：":
        return True
    if "|" in s or "\t" in s:
        return True
    # 回归表格常见多组显著性数字行
    if len(re.findall(r"-?\d+\.\d+\*{0,3}", s)) >= 2:
        return True
    # 图表标题如 `表1 基准回归结果` / `图 2.1 技术路线图` 不作为章节切分点
    if re.match(r"^(?:表|图|附图|附表|Table|Figure|Fig\.)\s*\d", s, re.IGNORECASE):
        return True
    # 高校学位论文常见奇偶页眉（如 `清华大学博士学位论文`）
    if re.match(r"^[\u4e00-\u9fff\s]{2,20}(?:大学|学院|研究院)\s*(?:博士|硕士|研究生)?学位论文$", s):
        return True
    return False


def _detect_raw_heading(line: str) -> Optional[Tuple[str, str]]:
    """识别单行候选标题，返回 `(kind, clean_title)`。"""
    raw = line.strip()
    md_m = re.match(r"^(#{1,4})\s+(.+)$", raw)
    if md_m:
        hashes, body = md_m.group(1), md_m.group(2).strip()
        if 1 < len(body) <= 80:
            lvl_kind = f"md_{min(len(hashes), 3)}"
            return lvl_kind, body

    if _looks_like_non_heading(raw):
        return None

    compact_cjk = re.sub(r"\s+", "", raw).lower()
    if compact_cjk in _STANDALONE_MAJOR_TITLES or raw.lower() in _STANDALONE_MAJOR_TITLES:
        return "standalone", re.sub(r"\s+", " ", raw)

    # 1. 学位论文/专著章标题：`第一章 绪论` / `第 4 章 实证分析`
    if re.match(r"^第\s*[一二三四五六七八九十百零\d]+\s*[章篇部分](?:$|[\s\u3000:：、.\-]+.+)", raw):
        return "chapter", re.sub(r"\s+", " ", raw)

    # 2. `第一节 ...`
    if re.match(r"^第\s*[一二三四五六七八九十百零\d]+\s*节(?:$|[\s\u3000:：、.\-]+.+)", raw):
        return "section_cn", re.sub(r"\s+", " ", raw)

    # 3. 中文一级序号：`一、引言` / `一 、 引 言`
    if re.match(r"^[一二三四五六七八九十]{1,3}\s*[、.．]\s*\S+", raw):
        return "cn_num", re.sub(r"\s+", " ", raw)

    # 4. 中文括号二级序号：`（一）理论分析` / `( 二 ) 变量说明`
    if re.match(r"^[（(]\s*[一二三四五六七八九十]{1,3}\s*[)）]\s*\S+", raw):
        return "paren_cn", re.sub(r"\s+", " ", raw)

    # 5. 罗马数字一级标题：`II. RELATED WORK`
    if re.match(r"^(?:I|II|III|IV|V|VI|VII|VIII|IX|X)\.\s+[A-Z][\w\s\-:,/()]{2,55}$", raw):
        return "roman", re.sub(r"\s+", " ", raw)

    # 6. 多级阿拉伯数字：`3.2.1 内生性检验` / `3.2.1. Experimental Setup`
    if re.match(r"^\d{1,2}\.\d{1,2}\.\d{1,2}(?!\.\d)\.?\s*(?=[\u4e00-\u9fffA-Za-z]).{2,55}$", raw):
        return "arabic_3", re.sub(r"\s+", " ", raw)

    # 7. 二级阿拉伯数字：`3.2 模型设定` / `1.1. 研究背景` / `3.2 Experimental Setup`（排除 `3.14%` 等小数）
    if re.match(r"^\d{1,2}\.\d{1,2}(?!\.\d)\.?\s*(?=[\u4e00-\u9fffA-Z]).{2,55}$", raw):
        return "arabic_2", re.sub(r"\s+", " ", raw)

    # 8. 单级阿拉伯数字：`1 引言` / `1. Introduction` / `1、样本选择`（限制长度并排除含长句助词的列表项）
    m_ar1 = re.match(r"^(?:[1-9]|1[0-5])(?:\s+|\.\s+|[、．]\s*)(?=[\u4e00-\u9fffA-Z])(.{2,42})$", raw)
    if m_ar1:
        rest = m_ar1.group(1)
        if not any(p in rest for p in ("，", "。", "；", "因为", "所以", "表明", "显示", "如下")):
            return "arabic_1", re.sub(r"\s+", " ", raw)

    return None


def parse_fulltext_sections(fulltext: str) -> List[Dict[str, Any]]:
    """解析学术全文的层级章节目录，返回含章节层级、起始页码、字数与行区间的列表。"""
    if not fulltext or not fulltext.strip():
        return []

    lines = fulltext.splitlines()
    raw_candidates: List[Dict[str, Any]] = []
    current_page: Optional[int] = None
    last_page_marker_idx = -999
    in_toc_block = False
    toc_block_start = -1

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        pm = _PAGE_MARKER_RE.match(stripped)
        if pm:
            current_page = int(pm.group(1))
            last_page_marker_idx = idx
            continue

        if _TOC_HEADER_RE.match(re.sub(r"\s+", "", stripped)):
            in_toc_block = True
            toc_block_start = idx
            continue

        # 跳过带虚线/空白引导页码的目录行
        if _TOC_LEADER_LINE_RE.search(stripped):
            continue
        if in_toc_block and (idx - toc_block_start) <= 180 and _CHAPTER_TRAILING_PAGE_RE.match(stripped):
            continue
        # 一旦遇到长段落（>90字）说明已进入正文，解除目录区标记
        if in_toc_block and len(stripped) > 90:
            in_toc_block = False

        detected = _detect_raw_heading(stripped)
        if detected:
            kind, clean_title = detected
            raw_candidates.append({
                "line_idx": idx,
                "kind": kind,
                "title": clean_title,
                "page": current_page,
                "near_page_top": (idx - last_page_marker_idx) <= 3,
            })

    if not raw_candidates:
        return []

    # 第一遍统计编号体系，动态映射 level (1 / 2 / 3)
    kinds = {c["kind"] for c in raw_candidates}
    has_chapter = "chapter" in kinds
    has_cn_num = "cn_num" in kinds

    def _map_level(kind: str) -> int:
        if kind == "md_1":
            return 1
        if kind == "md_2":
            return 2 if "md_1" in kinds else 1
        if kind == "md_3":
            return 3 if "md_1" in kinds else 2
        if kind in ("chapter", "standalone", "roman"):
            return 1
        if has_chapter:
            # 硕博学位论文体系：第X章(1) -> 第X节 / X.Y / 一、(2) -> X.Y.Z / （一） / 1.(3)
            if kind in ("section_cn", "arabic_2", "cn_num"):
                return 2
            return 3
        if has_cn_num:
            # 中文社科期刊体系：一、(1) -> （一） / X.Y(2) -> 1.(3)
            if kind == "cn_num":
                return 1
            if kind in ("paren_cn", "arabic_2"):
                return 2
            return 3
        # 自然科学/英文期刊体系：1 Introduction(1) -> 1.1(2) -> 1.1.1(3)
        if kind == "arabic_1":
            return 1
        if kind == "arabic_2":
            return 2
        return 3

    for c in raw_candidates:
        c["level"] = _map_level(c["kind"])

    # 计算相邻标题之间的直接正文字符数，剔除残留在前部目录页的无点线短桩标题
    for i, c in enumerate(raw_candidates):
        next_line = raw_candidates[i + 1]["line_idx"] if i + 1 < len(raw_candidates) else len(lines)
        body_lines = [
            ln.strip() for ln in lines[c["line_idx"] + 1:next_line]
            if ln.strip() and not _PAGE_MARKER_RE.match(ln.strip())
        ]
        c["direct_chars"] = sum(len(x) for x in body_lines)
        c["norm_key"] = _norm_heading_key(c["title"])

    later_substantive_keys = {
        c["norm_key"] for c in raw_candidates if c["direct_chars"] >= 80 and c["norm_key"]
    }
    seen_substantive: set[str] = set()
    accepted_keys: set[str] = set()
    numbered_kinds = {"chapter", "section_cn", "cn_num", "roman", "arabic_1", "arabic_2", "arabic_3"}
    filtered: List[Dict[str, Any]] = []
    for i, c in enumerate(raw_candidates):
        nk = c["norm_key"]
        # 1. 过滤 PDF 后续页面顶部重复出现的同章页眉（如每页顶部的 `第1章 绪论`）
        if nk and nk in accepted_keys and (c["kind"] in numbered_kinds or c.get("near_page_top")):
            continue
        # 2. 若后面存在同名且有实质正文的章节，当前却几乎无正文（且没有紧邻下级子节），判定为目录页残留项
        has_immediate_child = (
            i + 1 < len(raw_candidates)
            and raw_candidates[i + 1]["level"] > c["level"]
            and (raw_candidates[i + 1]["line_idx"] - c["line_idx"]) <= 8
        )
        if (
            nk in later_substantive_keys
            and nk not in seen_substantive
            and c["direct_chars"] < 80
            and not has_immediate_child
        ):
            continue
        if c["direct_chars"] >= 80:
            seen_substantive.add(nk)
        if nk:
            accepted_keys.add(nk)
        filtered.append(c)

    if not filtered:
        return []

    # 计算包含子章节的完整子树行区间 `[start_line, end_line)` 与字数
    sections: List[Dict[str, Any]] = []
    for i, sec in enumerate(filtered):
        lvl = sec["level"]
        end_line = len(lines)
        for j in range(i + 1, len(filtered)):
            if filtered[j]["level"] <= lvl:
                end_line = filtered[j]["line_idx"]
                break
        subtree_chars = sum(
            len(ln.strip())
            for ln in lines[sec["line_idx"]:end_line]
            if ln.strip() and not _PAGE_MARKER_RE.match(ln.strip())
        )
        item: Dict[str, Any] = {
            "id": f"S{i + 1}",
            "index": i + 1,
            "level": lvl,
            "title": sec["title"],
            "chars": subtree_chars,
            "_start_line": sec["line_idx"],
            "_end_line": end_line,
        }
        if sec.get("page") is not None:
            item["page"] = sec["page"]
        sections.append(item)

    return sections


def public_toc(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """剥离内部行号字段，生成面向 CLI JSON 输出的清爽目录数组。"""
    return [
        {k: v for k, v in s.items() if not k.startswith("_")}
        for s in sections
    ]


def extract_frontmatter_toc(fulltext: str) -> List[Dict[str, Any]]:
    """从学位论文/长文前部『目录』页（带点线或页码尾标）直接提取整本论文的章节层级与页码。
    专门用于 FlowPDF 大部头学位论文仅抽取前 16 页即可秒级生成完整目录的场景。
    """
    if not fulltext or not fulltext.strip():
        return []
    lines = fulltext.splitlines()
    entries: List[Dict[str, Any]] = []
    in_toc = False
    toc_start = -1
    seen_titles: set[str] = set()

    leader_extract_re = re.compile(
        r"^(.+?)(?:[.\u2026·•⋯\-_]{2,}|\u3000{2,}|\s{2,})([IVXLCDM]+|\d{1,4})\s*$",
        re.IGNORECASE,
    )
    plain_trailing_page_re = re.compile(
        r"^((?:第\s*[一二三四五六七八九十百零\d]+\s*[章篇节]|[一二三四五六七八九十]+、|\d{1,2}(?:\.\d{1,2}){0,2})\s*\S.*?)\s+(\d{1,4})\s*$"
    )

    for idx, raw in enumerate(lines):
        s = raw.strip()
        if not s or _PAGE_MARKER_RE.match(s):
            continue
        if _TOC_HEADER_RE.match(re.sub(r"\s+", "", s)):
            in_toc = True
            toc_start = idx
            continue
        if in_toc and (idx - toc_start) > 260:
            break
        if in_toc and len(s) > 95 and not _TOC_LEADER_LINE_RE.search(s):
            break

        m = leader_extract_re.match(s)
        if not m and in_toc:
            m = plain_trailing_page_re.match(s)
        if not m:
            continue

        raw_title = re.sub(r"[.\u2026·•⋯\-_]+\s*$", "", m.group(1)).strip()
        raw_page = m.group(2).strip()
        if not raw_title or len(raw_title) < 2 or len(raw_title) > 72:
            continue

        detected = _detect_raw_heading(raw_title)
        if detected:
            kind, clean_title = detected
        elif in_toc and raw_title in _STANDALONE_MAJOR_TITLES:
            kind, clean_title = "standalone", raw_title
        else:
            continue

        norm = _norm_heading_key(clean_title)
        if not norm or norm in seen_titles:
            continue
        seen_titles.add(norm)

        page_num: Optional[int] = int(raw_page) if raw_page.isdigit() else None
        entries.append({"kind": kind, "title": clean_title, "page": page_num})

    if len(entries) < 3:
        return []

    kinds = {e["kind"] for e in entries}
    has_chapter = "chapter" in kinds
    has_cn_num = "cn_num" in kinds

    out: List[Dict[str, Any]] = []
    for i, e in enumerate(entries):
        k = e["kind"]
        if k in ("chapter", "standalone", "roman"):
            lvl = 1
        elif has_chapter:
            lvl = 2 if k in ("section_cn", "arabic_2", "cn_num") else 3
        elif has_cn_num:
            lvl = 1 if k == "cn_num" else (2 if k in ("paren_cn", "arabic_2") else 3)
        else:
            lvl = 1 if k == "arabic_1" else (2 if k == "arabic_2" else 3)
        item: Dict[str, Any] = {
            "id": f"S{i + 1}",
            "index": i + 1,
            "level": lvl,
            "title": e["title"],
        }
        if e.get("page") is not None:
            item["page"] = e["page"]
        out.append(item)
    return out


def _match_section_query(sec: Dict[str, Any], query: str) -> bool:
    q = query.strip()
    if not q:
        return False
    if q.upper() == sec["id"].upper() or (q.isdigit() and int(q) == sec["index"]):
        return True
    q_norm = _norm_heading_key(q)
    t_norm = _norm_heading_key(sec["title"])
    if q_norm and q_norm in t_norm:
        return True
    return False


def slice_fulltext_sections(
    fulltext: str,
    section_spec: str,
    max_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """根据逗号/分号分隔的章节查询词精准切片全文，自动包含下辖子节并合并重叠区间。"""
    sections = parse_fulltext_sections(fulltext)
    toc = public_toc(sections)
    queries = [q.strip() for q in re.split(r"[,，;；]+", section_spec or "") if q.strip()]

    if not sections or not queries:
        sliced = fulltext or ""
        truncated = False
        if max_chars and max_chars > 0 and len(sliced) > max_chars:
            sliced = sliced[:max_chars].rsplit("\n", 1)[0] + "\n\n[... 已按 --max-chars 截断 ...]"
            truncated = True
        return {
            "matched": False,
            "matched_sections": [],
            "toc": toc,
            "text": sliced,
            "chars": len(sliced),
            "truncated": truncated,
        }

    matched_indices: List[int] = []
    for q in queries:
        direct_hits = [idx for idx, s in enumerate(sections) if _match_section_query(s, q)]
        if not direct_hits:
            # 尝试学术概念同义词扩展
            q_clean = re.sub(r"\s+", "", q).lower()
            aliases = _SECTION_CONCEPT_ALIASES.get(q_clean, ())
            for alias in aliases:
                alias_norm = _norm_heading_key(alias)
                direct_hits.extend(
                    idx for idx, s in enumerate(sections)
                    if alias_norm and alias_norm in _norm_heading_key(s["title"])
                )
        for h in direct_hits:
            if h not in matched_indices:
                matched_indices.append(h)

    if not matched_indices:
        return {
            "matched": False,
            "matched_sections": [],
            "toc": toc,
            "text": "",
            "chars": 0,
            "truncated": False,
            "warning": f"未找到匹配 '{section_spec}' 的章节，请参考 toc 字段中的章节标题或 S序号重新指定",
        }

    matched_indices.sort()
    lines = fulltext.splitlines()

    # 合并重叠或被父章节覆盖的行区间
    intervals: List[Tuple[int, int, Optional[int]]] = []
    for idx in matched_indices:
        s = sections[idx]
        st, ed, pg = s["_start_line"], s["_end_line"], s.get("page")
        if intervals and st <= intervals[-1][1]:
            prev_st, prev_ed, prev_pg = intervals[-1]
            intervals[-1] = (prev_st, max(prev_ed, ed), prev_pg)
        else:
            intervals.append((st, ed, pg))

    chunks: List[str] = []
    for st, ed, pg in intervals:
        block = "\n".join(lines[st:ed]).strip()
        if pg is not None and not block.startswith("[第"):
            block = f"[第 {pg} 页]\n{block}"
        if block:
            chunks.append(block)

    combined = "\n\n".join(chunks)
    truncated = False
    if max_chars and max_chars > 0 and len(combined) > max_chars:
        cut = combined[:max_chars]
        if "\n" in cut:
            cut = cut.rsplit("\n", 1)[0]
        combined = cut + "\n\n[... 已按 --max-chars 截断 ...]"
        truncated = True

    return {
        "matched": True,
        "matched_sections": [toc[idx] for idx in matched_indices],
        "toc": toc,
        "text": combined,
        "chars": len(combined),
        "truncated": truncated,
    }
