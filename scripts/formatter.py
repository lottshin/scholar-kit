"""
formatter.py - 引用格式化与导出模块
支持: GB/T 7714-2015, 脚注格式, APA, MLA, Chicago, BibTeX, RIS, Markdown, JSON, Excel
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List


def _display_width(s: str) -> int:
    """估算字符串显示宽度：CJK 字符算 2，ASCII 算 1"""
    width = 0
    for c in s:
        width += 2 if ord(c) > 0x7F else 1
    return width


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip()


def _first_value(paper: dict, *keys: str) -> str:
    for key in keys:
        value = _as_text(paper.get(key))
        if value:
            return value
    return ""


def _clean_container_title(raw: str) -> str:
    """清洗 OpenAlex 等外部源中带有斜杠大小写双份重复（A/a）或末尾多余句点的脏刊名。"""
    s = _as_text(raw).strip().rstrip(". ．。")
    if not s:
        return ""
    if "/" in s:
        left, right = [part.strip().rstrip(". ．。") for part in s.split("/", 1)]
        if left and right and left.lower() == right.lower():
            s = left
    return s.rstrip(". ．。").strip()


def _bibliographic_container(paper: dict) -> str:
    """Return a publication/container title, never the data-source marker."""
    val = _first_value(
        paper,
        "journal",
        "journal_title",
        "venue",
        "publication",
        "publication_title",
        "source_title",
        "periodical",
        "container_title",
    )
    return _clean_container_title(val)


def _analytic_container(paper: dict) -> str:
    return _first_value(
        paper,
        "booktitle",
        "book_title",
        "conference",
        "conference_name",
        "proceedings",
        "proceedings_title",
        "container_title",
    )


def _has_cjk(text: str) -> bool:
    return any('\u4e00' <= c <= '\u9fff' for c in text)


def _clean_person_name(name: str) -> str:
    """剥离知网/PDF 提取时黏连在作者姓名末尾的机构角标数字、星号与通讯作者标注，并修正 OpenAlex 倒置中文名（如 '子萱 廖' -> '廖子萱'）。"""
    s = name.strip()
    if not s:
        return ""
    s = re.sub(r"[（(]\s*(?:通讯作者|通信作者|corresponding\s*author)\s*[)）]", "", s, flags=re.I)
    s = re.sub(r"(?<=[\u4e00-\u9fffA-Za-z.\-])(?:\d+(?:\s*[,，、]\s*\d+)*)?[*†‡§¶]+$", "", s)
    s = re.sub(r"(?<=[\u4e00-\u9fffA-Za-z.\-])\d+(?:\s*[,，、]\s*\d+)*$", "", s)
    s = s.strip()
    # 修正西文库把中文作者按 'Given Family' 空格倒置存储的情况（如 '子萱 廖' -> '廖子萱'，'烽 杨' -> '杨烽'）
    m_cjk_rev = re.match(r"^([\u4e00-\u9fff]{1,2})\s+([\u4e00-\u9fff]{1,2})$", s)
    if m_cjk_rev:
        p1, p2 = m_cjk_rev.group(1), m_cjk_rev.group(2)
        p2_is_sur = p2 in _COMMON_CJK_PINYIN or p2 in _COMPOUND_CJK_SURNAMES
        p1_is_sur = p1 in _COMMON_CJK_PINYIN or p1 in _COMPOUND_CJK_SURNAMES
        if (len(p1) == 2 and len(p2) == 1 and p2_is_sur) or (p2_is_sur and not p1_is_sur):
            return f"{p2}{p1}"
        return f"{p1}{p2}"
    return s


_WESTERN_PARTICLES = {"van", "von", "de", "del", "della", "di", "da", "du", "la", "le", "der", "den", "ter", "ten"}


def _split_authors(authors_raw: Any) -> list[str]:
    if isinstance(authors_raw, list):
        authors = []
        for item in authors_raw:
            if isinstance(item, dict):
                name = item.get("name") or " ".join(
                    _as_text(item.get(k)) for k in ("given", "family") if item.get(k)
                )
            else:
                name = _as_text(item)
            clean = _clean_person_name(name)
            if clean:
                authors.append(clean)
        return authors

    raw = _as_text(authors_raw)
    if not raw:
        return []
    # 先剥离姓名后紧跟的机构角标如 `陈斌开1,2`，避免其中的逗号被误切分为两名作者
    raw = re.sub(r"(?<=[\u4e00-\u9fffA-Za-z])(?:\d+(?:\s*[,，]\s*\d+)+)[*†‡]?", "", raw)

    parts = re.split(r'\s*(?:;|；|\band\b|&)\s*', raw)
    if len(parts) == 1 and ("，" in raw or "," in raw):
        comma_parts = [p.strip() for p in re.split(r'[，,]\s*', raw) if p.strip()]
        if not (len(comma_parts) == 2 and " " not in comma_parts[0] and " " not in comma_parts[1] and not _has_cjk(raw)):
            parts = comma_parts
    cleaned = [_clean_person_name(p) for p in parts]
    return [p for p in cleaned if p]


def _extract_western_parts(name: str) -> tuple[str, list[str]]:
    """统一拆解西文作者姓氏与名列表，兼容 'Ashish Vaswani'、'Vaswani, Ashish'、PubMed 'Doudna JA' 及介词姓 'Aad van der Vaart'。"""
    s = _clean_person_name(name)
    if not s:
        return "", []
    if "," in s:
        last, rest = [p.strip() for p in s.split(",", 1)]
        return last, [x for x in rest.split() if x]
    parts = s.split()
    if len(parts) == 1:
        return parts[0], []
    # PubMed / MEDLINE 风格：末词为 1-3 位纯大写字母缩写（如 "Ma X"、"Doudna JA"）
    if re.match(r"^[A-Z]{1,3}\.?$", parts[-1]):
        raw_init = re.sub(r"[^A-Z]", "", parts[-1])
        return " ".join(parts[:-1]), list(raw_init)
    # 识别西文姓氏前缀词（如 van der Vaart / de La Fontaine）
    surname_start = len(parts) - 1
    while surname_start > 1 and parts[surname_start - 1].lower() in _WESTERN_PARTICLES:
        surname_start -= 1
    return " ".join(parts[surname_start:]), parts[:surname_start]


def _hyphenated_initial(token: str, *, with_dot: bool = True) -> str:
    segs = [re.sub(r"[^A-Za-z]", "", sub) for sub in token.split("-")]
    segs = [sub[0].upper() + ("." if with_dot else "") for sub in segs if sub]
    return "-".join(segs)


def _format_person_gbt_western(name: str) -> str:
    """GB/T 7714 西文著者标准著录：姓前名缩写后且不加缩写点（如 Vaswani A 或 Doudna J A）。"""
    if _has_cjk(name):
        return _clean_person_name(name)
    last, givens = _extract_western_parts(name)
    if not last:
        return _clean_person_name(name)
    inits = [_hyphenated_initial(g, with_dot=False) for g in givens if re.sub(r"[^A-Za-z]", "", g)]
    return f"{last} {' '.join(inits)}".strip()


def _format_authors_gbt(authors_raw: Any, *, western_initials: bool = False) -> str:
    authors = _split_authors(authors_raw)
    if not authors:
        return "佚名"
    formatted = [
        (_format_person_gbt_western(a) if (western_initials and not _has_cjk(a)) else a)
        for a in authors
    ]
    if len(formatted) <= 3:
        return ", ".join(formatted)
    suffix = ", 等" if _has_cjk(authors[0]) else ", et al."
    return ", ".join(formatted[:3]) + suffix


def _initials(names: list[str]) -> str:
    letters = []
    for name in names:
        h = _hyphenated_initial(name, with_dot=True)
        if h:
            letters.append(h)
    return " ".join(letters)


def _format_person_apa(name: str) -> str:
    name = name.strip()
    if not name or _has_cjk(name):
        return name
    last, givens = _extract_western_parts(name)
    if not givens:
        return last or name
    return f"{last}, {_initials(givens)}".strip()


def _format_authors_apa(authors_raw: Any) -> str:
    authors = [_format_person_apa(a) for a in _split_authors(authors_raw)]
    authors = [a for a in authors if a]
    if not authors:
        return "Anonymous"
    if len(authors) == 1:
        return authors[0]
    if len(authors) == 2:
        return f"{authors[0]}, & {authors[1]}"
    if len(authors) <= 20:
        return ", ".join(authors[:-1]) + f", & {authors[-1]}"
    return ", ".join(authors[:19]) + f", ... {authors[-1]}"


def _format_person_inverted(name: str) -> str:
    name = name.strip()
    if not name or _has_cjk(name) or "," in name:
        return name
    parts = name.split()
    if len(parts) == 1:
        return name
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def _format_authors_mla(authors_raw: Any) -> str:
    authors = _split_authors(authors_raw)
    if not authors:
        return "Anonymous"
    if len(authors) == 1:
        return _format_person_inverted(authors[0])
    if len(authors) == 2:
        return f"{_format_person_inverted(authors[0])}, and {authors[1]}"
    return f"{_format_person_inverted(authors[0])}, et al."


def _format_authors_chicago(authors_raw: Any) -> str:
    authors = _split_authors(authors_raw)
    if not authors:
        return "Anonymous"
    first = _format_person_inverted(authors[0])
    if len(authors) == 1:
        return first
    if len(authors) == 2:
        return f"{first}, and {authors[1]}"
    if len(authors) == 3:
        return f"{first}, {authors[1]}, and {authors[2]}"
    return f"{first}, et al."


def _get_year(paper: dict, default: str = "") -> str:
    year = _first_value(paper, "year")
    if year:
        return year
    date = _first_value(paper, "date", "published", "publication_date")
    match = re.search(r"\d{4}", date)
    return match.group(0) if match else default


def _clean_pages(pages: str) -> str:
    pages = _as_text(pages)
    return re.sub(r"^(pp?\.|页码[:：])\s*", "", pages, flags=re.I)


def _doi_url(doi: str) -> str:
    doi = _as_text(doi)
    if not doi:
        return ""
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    return f"https://doi.org/{doi}"


def _append_identifier(ref: str, paper: dict) -> str:
    doi_url = _doi_url(_first_value(paper, "doi"))
    url = _first_value(paper, "url")
    identifier = doi_url or url
    if not identifier:
        return ref
    if identifier in ref:
        return ref
    return ref.rstrip(" .") + f". {identifier}."


def _finish_reference(ref: str) -> str:
    ref = re.sub(r"\s+", " ", ref).strip()
    ref = ref.replace("..", ".")
    if not ref:
        return ref
    return ref if ref[-1] in ".。?？!！" else ref + "."


def _renumber_raw_reference(raw: str, index: int) -> str:
    raw = raw.strip()
    if not raw:
        return raw
    if re.match(r"^\[\d+\]", raw):
        return re.sub(r"^\[\d+\]\s*", f"[{index}] ", raw, count=1)
    return f"[{index}] {raw}"


def _detect_doc_type(paper: dict) -> str:
    explicit = _first_value(
        paper, "doc_type", "document_type", "type", "resource_type",
        "literature_type", "category"
    ).lower()

    aliases = {
        "journal": "journal", "article": "journal", "periodical": "journal", "期刊": "journal",
        "newspaper": "newspaper", "报纸": "newspaper",
        "book": "book", "monograph": "book", "图书": "book", "专著": "book",
        "thesis": "thesis", "dissertation": "thesis", "master": "thesis",
        "doctor": "thesis", "硕士": "thesis", "博士": "thesis", "学位": "thesis",
        "conference": "conference", "proceedings": "conference", "会议": "conference",
        "chapter": "chapter", "incollection": "chapter", "析出": "chapter",
        "report": "report", "technical report": "report", "研究报告": "report",
        "standard": "standard", "标准": "standard",
        "patent": "patent", "专利": "patent",
        "webpage": "webpage", "website": "webpage", "online": "webpage", "网页": "webpage",
        "database": "database", "数据库": "database",
        "dataset": "dataset", "data set": "dataset", "数据集": "dataset",
        "preprint": "preprint", "arxiv": "preprint", "预印本": "preprint",
    }
    for key, value in aliases.items():
        if key in explicit:
            return value

    # 切勿拿 "report" / "book" / "online" 在刊名或篇名上做宽泛子串匹配（否则 Scientific Reports /《纺织报告》会被误判为 [R] 报告！）
    venue_and_url = " ".join(_first_value(paper, k).lower() for k in ("journal", "venue", "publication", "source_title", "url"))
    if "arxiv" in venue_and_url or "preprint" in venue_and_url:
        return "preprint"
    if any(k in venue_and_url for k in ("conference", "proceedings", "symposium", "workshop", "会议")):
        return "conference"
    if any(k in venue_and_url for k in ("dissertation", "学位论文")):
        return "thesis"

    if _first_value(paper, "patent_no", "patent_number"):
        return "patent"
    if _first_value(paper, "standard_no", "standard_number"):
        return "standard"
    if _first_value(paper, "booktitle", "book_title", "proceedings_title"):
        return "chapter"
    if _bibliographic_container(paper):
        return "journal"
    if _first_value(paper, "school", "degree", "thesis_type"):
        return "thesis"
    if _first_value(paper, "publisher"):
        return "book"
    if _first_value(paper, "url"):
        return "webpage"
    return "journal"


def _is_online_first_journal(doc_type: str, paper: dict) -> bool:
    """判定是否为知网等期刊「网络首发」文献（尚无正式期号与页码，按 GB/T 7714 著录为 `[J/OL]`）。"""
    if doc_type != "journal":
        return False
    if paper.get("pages") or paper.get("issue") or paper.get("number"):
        return False
    if paper.get("is_online_first"):
        return True
    date_str = _first_value(paper, "date", "published", "publication_date")
    return bool(re.search(r"\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}", date_str) or "网络首发" in date_str)


def _thesis_degree_kind(paper: dict) -> str:
    """精确区分博士/硕士学位论文（返回 'doctor' | 'master' | 'thesis'）。"""
    probe = " ".join(
        _first_value(paper, k).lower()
        for k in ("degree", "thesis_type", "doc_type", "document_type", "type", "category", "literature_type")
    )
    if any(t in probe for t in ("doctor", "phd", "ph.d", "博士", "cdmd:d")):
        return "doctor"
    if any(t in probe for t in ("master", "硕士", "cmfd")):
        return "master"
    return "thesis"


def _gbt_type_tag(doc_type: str, paper: dict) -> str:
    if _is_online_first_journal(doc_type, paper):
        return "J/OL"
    online = bool(_first_value(paper, "url")) and doc_type in {
        "webpage", "database", "dataset", "preprint"
    }
    tags = {
        "journal": "J",
        "newspaper": "N",
        "book": "M",
        "thesis": "D",
        "conference": "C",
        "chapter": "M",
        "report": "R",
        "standard": "S",
        "patent": "P",
        "webpage": "EB/OL",
        "database": "DB/OL",
        "dataset": "DS/OL",
        "preprint": "J/OL",
    }
    tag = tags.get(doc_type, "Z")
    if online and "/" not in tag:
        tag = f"{tag}/OL"
    return tag


# ── GB/T 7714-2015 ────────────────────────────────────

def _raw_gbt_has_pages(raw: str) -> bool:
    return bool(re.search(r"[:：]\s*\d+|,\s*\d+[-–—]\d+", raw or ""))


def format_gbt7714(paper: dict, index: int = 1) -> str:
    """
    GB/T 7714-2015 格式化（顺序编码制）
    按文献类型生成期刊、网络首发期刊 [J/OL]、专著、学位论文、会议析出、专利、电子文献等常见著录格式。
    """
    raw = _first_value(paper, "gbt7714_raw", "gbt7714")
    pages = _clean_pages(_first_value(paper, "pages"))
    # 若旧 gbt7714_raw 缺失页码而详情已补齐 pages，则改走完整字段组装，彻底消除漏页码
    if raw and (_raw_gbt_has_pages(raw) or not pages):
        return _renumber_raw_reference(raw, index)

    authors = _format_authors_gbt(paper.get("authors"), western_initials=True)
    title = _first_value(paper, "title")
    journal = _bibliographic_container(paper)
    container = _analytic_container(paper)
    year = _get_year(paper)
    date = _first_value(paper, "date", "published", "publication_date")
    volume = _first_value(paper, "volume")
    issue = _first_value(paper, "issue", "number")
    place = _first_value(paper, "place", "publisher_place", "location")
    publisher = _first_value(paper, "publisher", "institution", "school")
    accessed = _first_value(paper, "accessed", "access_date")
    url = _first_value(paper, "url")
    patent_no = _first_value(paper, "patent_no", "patent_number", "publication_number")
    standard_no = _first_value(paper, "standard_no", "standard_number")
    doc_type = _detect_doc_type(paper)
    type_tag = _gbt_type_tag(doc_type, paper)

    ref = f"[{index}] {authors}. {title}[{type_tag}]. "
    if doc_type == "journal" and type_tag == "J/OL":
        ref += journal or "刊名不详"
        ymd_m = re.search(r"\d{4}-\d{2}-\d{2}", date)
        pub_day = ymd_m.group(0) if ymd_m else ""
        if pub_day:
            ref += f"({pub_day})"
        elif year:
            ref += f", {year}"
        if accessed:
            ref += f"[{accessed}]"
        if url and not _first_value(paper, "doi"):
            ref += f". {url}"
    elif doc_type == "journal":
        ref += journal or "刊名不详"
        if year:
            ref += f", {year}"
        if volume:
            ref += f", {volume}"
        if issue:
            ref += f"({issue})"
        if pages:
            ref += f": {pages}"
    elif doc_type == "newspaper":
        ref += journal or "报纸名不详"
        if date or year:
            ref += f", {date or year}"
        if issue:
            ref += f"({issue})"
    elif doc_type in ("book", "thesis", "report", "standard"):
        if standard_no:
            ref += f"{standard_no}. "
        pub_bits = []
        if place:
            pub_bits.append(place)
        if publisher:
            pub_bits.append(publisher)
        if pub_bits:
            ref += ": ".join(pub_bits)
            if year:
                ref += f", {year}"
        elif year:
            ref += year
        if pages:
            ref += f": {pages}"
    elif doc_type in ("conference", "chapter"):
        if container:
            ref += f"//{container}. "
        elif journal:
            ref += f"{journal}, "
        pub_bits = []
        if place:
            pub_bits.append(place)
        if publisher:
            pub_bits.append(publisher)
        if pub_bits:
            ref += ": ".join(pub_bits)
            if year:
                ref += f", {year}"
        elif year:
            ref += year
        if pages:
            ref += f": {pages}"
    elif doc_type == "patent":
        if patent_no:
            ref += f"{patent_no}. "
        if date or year:
            ref += date or year
    else:
        site = publisher or journal
        if site:
            ref += f"{site}"
        if date or year:
            ref += f"({date or year})" if site else f"{date or year}"
        if accessed:
            ref += f"[{accessed}]"
        if url and not _first_value(paper, "doi"):
            ref += f". {url}" if (site or date or year) else url

    ref = _finish_reference(ref)
    doi = _first_value(paper, "doi")
    if doi and "doi" not in ref.lower():
        ref = ref.rstrip(" .") + f". DOI: {doi}."
    return ref


# ── 脚注格式（中文文科常用）────────────────────────────

def format_footnote(paper: dict, index: int = 1) -> str:
    """
    中文脚注格式（文科常用）
    ① 作者：《书名/文章名》，出版社/期刊名，年份，第X页。
    """
    authors = paper.get("authors", "佚名")
    title = paper.get("title", "")
    journal = _bibliographic_container(paper)
    year = paper.get("year", "")
    volume = paper.get("volume", "")
    issue = paper.get("issue", "")
    pages = paper.get("pages", "")

    circled = _circled_number(index)

    first_author = re.split(r'[;；,，]\s*', authors)[0].strip() if authors else "佚名"

    if journal:
        ref = f"{circled} {first_author}：「{title}」，《{journal}》"
        if year:
            ref += f"{year}年"
        if issue:
            ref += f"第{issue}期"
        if pages:
            ref += f"，第{pages}页"
        ref += "。"
    else:
        ref = f"{circled} {first_author}：《{title}》，{year}年。"

    return ref


def _circled_number(n: int) -> str:
    if 1 <= n <= 20:
        return chr(0x2460 + n - 1)
    return f"({n})"


# ── APA 第7版 ─────────────────────────────────────────

def format_apa(paper: dict, index: int = 1) -> str:
    doc_type = _detect_doc_type(paper)
    authors = _format_authors_apa(paper.get("authors"))
    year = _get_year(paper, "n.d.")
    title = _first_value(paper, "title")
    journal = _bibliographic_container(paper)
    container = _analytic_container(paper)
    volume = _first_value(paper, "volume")
    issue = _first_value(paper, "issue", "number")
    pages = _clean_pages(_first_value(paper, "pages"))
    publisher = _first_value(paper, "publisher", "institution", "school")
    patent_no = _first_value(paper, "patent_no", "patent_number", "publication_number")

    if doc_type == "journal":
        ref = f"{authors} ({year}). {title}. "
        ref += f"*{journal}*" if journal else "Periodical title missing"
        if volume:
            ref += f", *{volume}*"
        if issue:
            ref += f"({issue})"
        if pages:
            ref += f", {pages}"
        ref += "."
        return _append_identifier(ref, paper)

    if doc_type == "book":
        ref = f"{authors} ({year}). *{title}*."
        if publisher:
            ref += f" {publisher}."
        return _append_identifier(ref, paper)

    if doc_type in ("chapter", "conference"):
        ref = f"{authors} ({year}). {title}."
        if container:
            ref += f" In *{container}*"
            if pages:
                ref += f" (pp. {pages})"
            ref += "."
        if publisher:
            ref += f" {publisher}."
        return _append_identifier(ref, paper)

    if doc_type == "thesis":
        deg_kind = _thesis_degree_kind(paper)
        default_kind = "Master's thesis" if deg_kind == "master" else "Doctoral dissertation"
        kind = _first_value(paper, "degree", "thesis_type") or default_kind
        ref = f"{authors} ({year}). *{title}* [{kind}"
        if publisher:
            ref += f", {publisher}"
        ref += "]."
        return _append_identifier(ref, paper)

    if doc_type == "patent":
        ref = f"{authors} ({year}). {title}"
        if patent_no:
            ref += f" (Patent No. {patent_no})"
        ref += "."
        return _append_identifier(ref, paper)

    if doc_type == "standard":
        ref = f"{authors} ({year}). *{title}*"
        standard_no = _first_value(paper, "standard_no", "standard_number")
        if standard_no:
            ref += f" ({standard_no})"
        ref += "."
        if publisher:
            ref += f" {publisher}."
        return _append_identifier(ref, paper)

    site = publisher or journal
    ref = f"{authors} ({year}). {title}."
    if site:
        ref += f" {site}."
    return _append_identifier(ref, paper)


# ── MLA 第9版 ─────────────────────────────────────────

def format_mla(paper: dict, index: int = 1) -> str:
    doc_type = _detect_doc_type(paper)
    authors = _format_authors_mla(paper.get("authors"))
    title = _first_value(paper, "title")
    journal = _bibliographic_container(paper)
    container = _analytic_container(paper)
    year = _get_year(paper)
    date = _first_value(paper, "date", "published", "publication_date")
    volume = _first_value(paper, "volume")
    issue = _first_value(paper, "issue", "number")
    pages = _clean_pages(_first_value(paper, "pages"))
    publisher = _first_value(paper, "publisher", "institution", "school")
    patent_no = _first_value(paper, "patent_no", "patent_number", "publication_number")

    if doc_type == "journal":
        ref = f'{authors}. "{title}."'
        if journal:
            ref += f" {journal}"
        if volume:
            ref += f", vol. {volume}"
        if issue:
            ref += f", no. {issue}"
        if year:
            ref += f", {year}"
        if pages:
            ref += f", pp. {pages}"
        ref += "."
        return _append_identifier(ref, paper)

    if doc_type in ("book", "thesis", "report", "standard"):
        ref = f"{authors}. *{title}*."
        if publisher:
            ref += f" {publisher},"
        if year:
            ref += f" {year}"
        ref += "."
        return _append_identifier(ref, paper)

    if doc_type in ("chapter", "conference"):
        ref = f'{authors}. "{title}."'
        if container:
            ref += f" *{container}*,"
        if publisher:
            ref += f" {publisher},"
        if year:
            ref += f" {year},"
        if pages:
            ref += f" pp. {pages}"
        ref += "."
        return _append_identifier(ref, paper)

    if doc_type == "patent":
        ref = f'{authors}. "{title}."'
        if patent_no:
            ref += f" Patent {patent_no},"
        if date or year:
            ref += f" {date or year}"
        ref += "."
        return _append_identifier(ref, paper)

    ref = f'{authors}. "{title}."'
    if journal or publisher:
        ref += f" {journal or publisher},"
    if date or year:
        ref += f" {date or year},"
    return _append_identifier(ref, paper)


# ── Chicago ───────────────────────────────────────────

def format_chicago(paper: dict, index: int = 1) -> str:
    doc_type = _detect_doc_type(paper)
    authors = _format_authors_chicago(paper.get("authors"))
    title = _first_value(paper, "title")
    journal = _bibliographic_container(paper)
    container = _analytic_container(paper)
    year = _get_year(paper)
    date = _first_value(paper, "date", "published", "publication_date")
    volume = _first_value(paper, "volume")
    issue = _first_value(paper, "issue", "number")
    pages = _clean_pages(_first_value(paper, "pages"))
    publisher = _first_value(paper, "publisher", "institution", "school")
    place = _first_value(paper, "place", "publisher_place", "location")
    patent_no = _first_value(paper, "patent_no", "patent_number", "publication_number")

    if doc_type == "journal":
        ref = f'{authors}. "{title}."'
        if journal:
            ref += f" *{journal}*"
        if volume:
            ref += f" {volume}"
        if issue:
            ref += f", no. {issue}"
        if year:
            ref += f" ({year})"
        if pages:
            ref += f": {pages}"
        ref += "."
        return _append_identifier(ref, paper)

    if doc_type == "book":
        ref = f"{authors}. *{title}*."
        if place and publisher:
            ref += f" {place}: {publisher}, {year}."
        elif publisher:
            ref += f" {publisher}, {year}."
        elif year:
            ref += f" {year}."
        return _append_identifier(ref, paper)

    if doc_type in ("chapter", "conference"):
        ref = f'{authors}. "{title}."'
        if container:
            ref += f" In *{container}*"
        if pages:
            ref += f", {pages}"
        if publisher or year:
            ref += "."
            if place and publisher:
                ref += f" {place}: {publisher}, {year}."
            elif publisher:
                ref += f" {publisher}, {year}."
            elif year:
                ref += f" {year}."
        return _append_identifier(ref, paper)

    if doc_type == "thesis":
        deg_kind = _thesis_degree_kind(paper)
        default_kind = "Master's thesis" if deg_kind == "master" else "PhD diss."
        kind = _first_value(paper, "degree", "thesis_type") or default_kind
        ref = f'{authors}. "{title}." {kind}'
        if publisher:
            ref += f", {publisher}"
        if year:
            ref += f", {year}"
        ref += "."
        return _append_identifier(ref, paper)

    if doc_type == "patent":
        ref = f'{authors}. "{title}."'
        if patent_no:
            ref += f" Patent {patent_no}."
        if date or year:
            ref += f" {date or year}."
        return _append_identifier(ref, paper)

    ref = f'{authors}. "{title}."'
    if journal or publisher:
        ref += f" {journal or publisher}."
    if date or year:
        ref += f" {date or year}."
    return _append_identifier(ref, paper)


# ── BibTeX 导出 ───────────────────────────────────────

def _bibtex_escape(s: str) -> str:
    """转义 BibTeX 特殊字符"""
    s = str(s)
    for ch in ('\\', '{', '}', '%', '#', '$', '&', '_', '~', '^'):
        s = s.replace(ch, '\\' + ch)
    s = s.replace("\n", " ")
    return s


_COMMON_CJK_PINYIN = {
    "赵": "zhao", "钱": "qian", "孙": "sun", "李": "li", "周": "zhou", "吴": "wu", "郑": "zheng", "王": "wang",
    "冯": "feng", "陈": "chen", "褚": "chu", "卫": "wei", "蒋": "jiang", "沈": "shen", "韩": "han", "杨": "yang",
    "朱": "zhu", "秦": "qin", "尤": "you", "许": "xu", "何": "he", "吕": "lv", "施": "shi", "张": "zhang",
    "孔": "kong", "曹": "cao", "严": "yan", "华": "hua", "金": "jin", "魏": "wei", "陶": "tao", "姜": "jiang",
    "戚": "qi", "谢": "xie", "邹": "zou", "喻": "yu", "柏": "bai", "水": "shui", "窦": "dou", "章": "zhang",
    "云": "yun", "苏": "su", "潘": "pan", "葛": "ge", "奚": "xi", "范": "fan", "彭": "peng", "郎": "lang",
    "鲁": "lu", "韦": "wei", "昌": "chang", "马": "ma", "苗": "miao", "凤": "feng", "花": "hua", "方": "fang",
    "俞": "yu", "任": "ren", "袁": "yuan", "柳": "liu", "酆": "feng", "鲍": "bao", "史": "shi", "唐": "tang",
    "费": "fei", "廉": "lian", "岑": "cen", "薛": "xue", "雷": "lei", "贺": "he", "倪": "ni", "汤": "tang",
    "滕": "teng", "殷": "yin", "罗": "luo", "毕": "bi", "郝": "hao", "邬": "wu", "安": "an", "常": "chang",
    "乐": "le", "于": "yu", "时": "shi", "傅": "fu", "皮": "pi", "卞": "bian", "齐": "qi", "康": "kang",
    "伍": "wu", "余": "yu", "元": "yuan", "卜": "bu", "顾": "gu", "孟": "meng", "平": "ping", "黄": "huang",
    "和": "he", "穆": "mu", "萧": "xiao", "肖": "xiao", "尹": "yin", "姚": "yao", "邵": "shao", "湛": "zhan",
    "汪": "wang", "祁": "qi", "毛": "mao", "禹": "yu", "狄": "di", "米": "mi", "贝": "bei", "明": "ming",
    "臧": "zang", "计": "ji", "伏": "fu", "成": "cheng", "戴": "dai", "谈": "tan", "宋": "song", "茅": "mao",
    "庞": "pang", "熊": "xiong", "纪": "ji", "舒": "shu", "屈": "qu", "项": "xiang", "祝": "zhu", "董": "dong",
    "梁": "liang", "杜": "du", "阮": "ruan", "蓝": "lan", "闵": "min", "席": "xi", "季": "ji", "麻": "ma",
    "强": "qiang", "贾": "jia", "路": "lu", "娄": "lou", "危": "wei", "江": "jiang", "童": "tong", "颜": "yan",
    "郭": "guo", "梅": "mei", "盛": "sheng", "林": "lin", "刁": "diao", "钟": "zhong", "徐": "xu", "邱": "qiu",
    "骆": "luo", "高": "gao", "夏": "xia", "蔡": "cai", "田": "tian", "樊": "fan", "胡": "hu", "凌": "ling",
    "霍": "huo", "虞": "yu", "万": "wan", "支": "zhi", "柯": "ke", "昝": "zan", "管": "guan", "卢": "lu",
    "莫": "mo", "经": "jing", "房": "fang", "裘": "qiu", "缪": "miao", "干": "gan", "解": "xie", "应": "ying",
    "宗": "zong", "丁": "ding", "宣": "xuan", "贲": "ben", "邓": "deng", "郁": "yu", "单": "shan", "杭": "hang",
    "洪": "hong", "包": "bao", "诸": "zhu", "左": "zuo", "石": "shi", "崔": "cui", "吉": "ji", "钮": "niu",
    "龚": "gong", "程": "cheng", "嵇": "ji", "邢": "xing", "滑": "hua", "裴": "pei", "陆": "lu", "荣": "rong",
    "翁": "weng", "荀": "xun", "羊": "yang", "於": "yu", "惠": "hui", "甄": "zhen", "曲": "qu", "家": "jia",
    "封": "feng", "芮": "rui", "羿": "yi", "储": "chu", "靳": "jin", "汲": "ji", "邴": "bing", "糜": "mi",
    "松": "song", "井": "jing", "段": "duan", "富": "fu", "巫": "wu", "乌": "wu", "焦": "jiao", "巴": "ba",
    "弓": "gong", "牧": "mu", "隗": "kui", "山": "shan", "谷": "gu", "车": "che", "侯": "hou", "宓": "mi",
    "蓬": "peng", "全": "quan", "郗": "xi", "班": "ban", "仰": "yang", "秋": "qiu", "仲": "zhong", "伊": "yi",
    "宫": "gong", "宁": "ning", "仇": "qiu", "栾": "luan", "暴": "bao", "甘": "gan", "钭": "tou", "厉": "li",
    "戎": "rong", "祖": "zu", "武": "wu", "符": "fu", "刘": "liu", "景": "jing", "詹": "zhan", "束": "shu",
    "龙": "long", "叶": "ye", "幸": "xing", "司": "si", "韶": "shao", "郜": "gao", "黎": "li", "蓟": "ji",
    "薄": "bo", "印": "yin", "宿": "su", "白": "bai", "怀": "huai", "蒲": "pu", "邰": "tai", "从": "cong",
    "鄂": "e", "索": "suo", "咸": "xian", "籍": "ji", "赖": "lai", "卓": "zhuo", "蔺": "lin", "屠": "tu",
    "蒙": "meng", "池": "chi", "乔": "qiao", "阴": "yin", "胥": "xu", "能": "neng", "苍": "cang", "双": "shuang",
    "闻": "wen", "莘": "shen", "党": "dang", "翟": "zhai", "谭": "tan", "贡": "gong", "劳": "lao", "逄": "pang",
    "姬": "ji", "申": "shen", "扶": "fu", "堵": "du", "冉": "ran", "宰": "zai", "郦": "li", "雍": "yong",
    "却": "que", "璩": "qu", "桑": "sang", "桂": "gui", "濮": "pu", "牛": "niu", "寿": "shou", "通": "tong",
    "边": "bian", "扈": "hu", "燕": "yan", "冀": "ji", "郏": "jia", "浦": "pu", "尚": "shang", "农": "nong",
    "温": "wen", "别": "bie", "庄": "zhuang", "晏": "yan", "柴": "chai", "瞿": "qu", "阎": "yan", "闫": "yan",
    "充": "chong", "慕": "mu", "连": "lian", "茹": "ru", "习": "xi", "宦": "huan", "艾": "ai", "鱼": "yu",
    "容": "rong", "向": "xiang", "古": "gu", "易": "yi", "慎": "shen", "戈": "ge", "廖": "liao", "庾": "yu",
    "终": "zhong", "暨": "ji", "居": "ju", "衡": "heng", "步": "bu", "都": "du", "耿": "geng", "满": "man",
    "弘": "hong", "匡": "kuang", "国": "guo", "文": "wen", "寇": "kou", "广": "guang", "禄": "lu", "阙": "que",
    "东": "dong", "欧": "ou", "殳": "shu", "沃": "wo", "利": "li", "蔚": "wei", "越": "yue", "夔": "kui",
    "隆": "long", "师": "shi", "巩": "gong", "厍": "she", "聂": "nie", "晁": "chao", "勾": "gou", "敖": "ao",
    "融": "rong", "冷": "leng", "訾": "zi", "辛": "xin", "阚": "kan", "那": "na", "简": "jian", "饶": "rao",
    "空": "kong", "曾": "zeng", "毋": "wu", "沙": "sha", "乜": "nie", "养": "yang", "鞠": "ju", "须": "xu",
    "丰": "feng", "巢": "chao", "关": "guan", "蒯": "kuai", "相": "xiang", "查": "zha", "后": "hou", "荆": "jing",
    "红": "hong", "游": "you", "竺": "zhu", "权": "quan", "逯": "lu", "盖": "gai", "益": "yi", "桓": "huan",
    "公": "gong", "岳": "yue", "帅": "shuai", "缑": "gou", "亢": "kang", "况": "kuang", "晋": "jin", "楚": "chu",
    "鄢": "yan", "法": "fa", "汝": "ru", "涂": "tu", "钦": "qin", "归": "gui", "海": "hai", "羊": "yang",
    "商": "shang", "牟": "mou", "佘": "she", "佴": "nai", "伯": "bo", "赏": "shang", "墨": "mo", "哈": "ha",
    "谯": "qiao", "笪": "da", "年": "nian", "爱": "ai", "阳": "yang", "佟": "tong", "覃": "qin", "粟": "su",
}


_COMPOUND_CJK_SURNAMES = {
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟", "闻人",
    "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政", "濮阳", "公冶",
    "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于",
    "司空", "闾丘", "子车", "亓官", "司寇", "巫马", "公西", "颛孙", "壤驷", "公良",
    "漆雕", "乐正", "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐", "段干", "百里",
    "呼延", "东郭", "南门", "羊舌", "微生", "梁丘", "左丘", "东门", "西门", "第五",
}


def _to_ascii_slug(raw: str, fallback: str = "ref") -> str:
    """将中英文作者名或标题转换为纯 ASCII 标识符，确保传统 BibTeX / pdflatex 100% 可编译。"""
    s = str(raw or "").strip()
    if not s:
        return fallback
    ascii_only = re.sub(r"[^a-zA-Z0-9]", "", s).lower()
    if ascii_only and not _has_cjk(s):
        return ascii_only[:20]

    parts: list[str] = []
    for ch in s:
        if ch.isascii() and ch.isalnum():
            parts.append(ch.lower())
        elif ch in _COMMON_CJK_PINYIN:
            parts.append(_COMMON_CJK_PINYIN[ch])
        elif "\u4e00" <= ch <= "\u9fff":
            # 生僻汉字使用确定性短十六进制编码保底，杜绝非 ASCII 字符泄漏进 citekey
            parts.append(f"u{ord(ch):04x}")
        if sum(len(x) for x in parts) >= 16:
            break
    slug = "".join(parts)
    slug = re.sub(r"[^a-z0-9]", "", slug)
    if slug and slug[0].isdigit():
        slug = "p" + slug
    return slug[:20] or fallback


def to_bibtex_with_keys(papers: List[Dict[str, Any]]) -> tuple[str, List[str]]:
    entries = []
    ordered_keys: List[str] = []
    used_keys: set[str] = set()
    for i, p in enumerate(papers):
        base_key = p.get("citekey") or _bibtex_key(p, i)
        # 确保单次导出的所有 BibTeX key 均为纯 ASCII 且全局唯一
        clean_key = re.sub(r"[^a-zA-Z0-9_:-]", "", str(base_key)) or f"ref_{i + 1}"
        key = clean_key
        suffix_n = 2
        while key in used_keys:
            key = f"{clean_key}_{suffix_n}"
            suffix_n += 1
        used_keys.add(key)
        ordered_keys.append(key)

        doc_type = _detect_doc_type(p)
        deg_kind = _thesis_degree_kind(p) if doc_type == "thesis" else ""
        bib_type = {
            "journal": "article",
            "newspaper": "article",
            "book": "book",
            "thesis": "mastersthesis" if deg_kind == "master" else "phdthesis",
            "conference": "inproceedings",
            "chapter": "incollection",
            "report": "techreport",
            "standard": "manual",
            "patent": "misc",
            "webpage": "online",
            "database": "online",
            "dataset": "dataset",
            "preprint": "article",
        }.get(doc_type, "article")

        fields = []
        if p.get("title"):
            fields.append("  title = {{{}}}".format(_bibtex_escape(p["title"])))
        if p.get("authors"):
            bib_authors = _split_authors(p["authors"])
            author_str = " and ".join(_bibtex_escape(a) for a in bib_authors) if bib_authors else _bibtex_escape(p["authors"])
            fields.append("  author = {{{}}}".format(author_str))
        if p.get("year"):
            fields.append("  year = {{{}}}".format(p["year"]))
        pub_org = _first_value(p, "school", "institution", "publisher")
        if doc_type == "thesis" and (pub_org or p.get("journal")):
            fields.append("  school = {{{}}}".format(_bibtex_escape(pub_org or p["journal"])))
        elif doc_type in ("book", "chapter") and pub_org:
            fields.append("  publisher = {{{}}}".format(_bibtex_escape(pub_org)))
        elif doc_type == "report" and pub_org:
            fields.append("  institution = {{{}}}".format(_bibtex_escape(pub_org)))
        if p.get("journal") and doc_type not in ("thesis", "book"):
            fields.append("  journal = {{{}}}".format(_bibtex_escape(p["journal"])))
        if p.get("volume"):
            fields.append("  volume = {{{}}}".format(p["volume"]))
        if p.get("issue"):
            fields.append("  number = {{{}}}".format(p["issue"]))
        if p.get("pages"):
            fields.append("  pages = {{{}}}".format(p["pages"]))
        if p.get("doi"):
            fields.append("  doi = {{{}}}".format(p["doi"]))
        if p.get("url"):
            fields.append("  url = {{{}}}".format(p["url"]))

        entry = "@{}{{{},\n".format(bib_type, key) + ",\n".join(fields) + "\n}"
        entries.append(entry)

    return "\n\n".join(entries), ordered_keys


def to_bibtex(papers: List[Dict[str, Any]]) -> str:
    return to_bibtex_with_keys(papers)[0]


def _bibtex_key(paper: dict, index: int) -> str:
    authors = _split_authors(paper.get("authors"))
    first_author = authors[0] if authors else "anon"
    if _has_cjk(first_author):
        cjk_chars = "".join(c for c in first_author if "\u4e00" <= c <= "\u9fff")
        surname = cjk_chars[:2] if cjk_chars[:2] in _COMPOUND_CJK_SURNAMES else (cjk_chars[:1] or first_author[:1])
    elif "," in first_author:
        surname = first_author.split(",", 1)[0].strip()
    elif " " in first_author:
        parts = first_author.split()
        # 兼容 PubMed/Vancouver 格式 "Ma X" / "Doudna JA"（末词为大写缩写时姓氏在首词）
        if re.match(r"^(?:[A-Z]\.?){1,3}$", parts[-1]):
            surname = parts[0].strip()
        else:
            surname = parts[-1].strip()
    else:
        surname = first_author
    author_slug = _to_ascii_slug(surname, fallback="anon")
    year = _get_year(paper, "")
    # 附加标题首词/编码短后缀，兼顾可读性与唯一性（如 zhao2020_glsj 或 vaswani2017_attention）
    cid = str(paper.get("cnki_id") or "")
    if ":" in cid:
        venue_slug = re.sub(r"[^a-z0-9]", "", cid.split(":", 1)[1][:4].lower())
    else:
        title_words = re.findall(r"[A-Za-z]{4,}", str(paper.get("title") or ""))
        venue_slug = title_words[0].lower()[:10] if title_words else f"{index + 1}"
    return f"{author_slug}{year}_{venue_slug}" if year else f"{author_slug}_{venue_slug}"


# ── RIS 导出 ──────────────────────────────────────────

def to_ris(papers: list[dict]) -> str:
    entries = []
    for p in papers:
        doc_type = _detect_doc_type(p)
        ris_type = {
            "journal": "JOUR",
            "newspaper": "NEWS",
            "book": "BOOK",
            "thesis": "THES",
            "conference": "CONF",
            "chapter": "CHAP",
            "report": "RPRT",
            "standard": "STND",
            "patent": "PAT",
            "webpage": "ELEC",
            "database": "DBASE",
            "dataset": "DATA",
            "preprint": "JOUR",
        }.get(doc_type, "JOUR")

        lines = [f"TY  - {ris_type}"]
        if p.get("title"):
            lines.append(f"TI  - {p['title'].replace(chr(10), ' ').replace(chr(13), '')}")
        if p.get("authors"):
            for author in re.split(r'[;；]\s*', p["authors"]):
                if author.strip():
                    lines.append(f"AU  - {author.strip()}")
        if p.get("year"):
            lines.append(f"PY  - {p['year']}")
        if p.get("journal"):
            lines.append(f"JO  - {p['journal']}")
        if p.get("volume"):
            lines.append(f"VL  - {p['volume']}")
        if p.get("issue"):
            lines.append(f"IS  - {p['issue']}")
        if p.get("pages"):
            sp, ep = _split_pages(p["pages"])
            if sp:
                lines.append(f"SP  - {sp}")
            if ep:
                lines.append(f"EP  - {ep}")
        if p.get("doi"):
            lines.append(f"DO  - {p['doi']}")
        if p.get("url"):
            lines.append(f"UR  - {p['url']}")
        if p.get("abstract"):
            lines.append(f"AB  - {p['abstract'].replace(chr(10), ' ').replace(chr(13), '')}")
        lines.append("ER  - ")
        entries.append("\n".join(lines))

    return "\n\n".join(entries)


def _split_pages(pages: str) -> tuple[str, str]:
    m = re.match(r'(\d+)\s*[-–—]\s*(\d+)', pages)
    if m:
        return m.group(1), m.group(2)
    return pages, ""


# ── Markdown 导出 ─────────────────────────────────────

def _md_escape(s: str) -> str:
    """转义 Markdown 表格中的特殊字符"""
    return str(s).replace("|", "\\|").replace("\n", " ")


def to_markdown_table(papers: List[Dict[str, Any]]) -> str:
    lines = [
        "| # | 标题 | 作者 | 期刊 | 年份 | 被引 | 来源 |",
        "|---|------|------|------|------|------|------|",
    ]
    for i, p in enumerate(papers, 1):
        title = _md_escape(p.get("title", "")[:50])
        authors = _md_escape(p.get("authors", "")[:30])
        journal = _md_escape(_bibliographic_container(p)[:20])
        year = p.get("year", "")
        cited = p.get("cited_by", "")
        source = p.get("source", "")
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            i, title, authors, journal, year, cited, source
        ))

    return "\n".join(lines)


# ── JSON 导出 ─────────────────────────────────────────

def to_json(papers: list[dict], indent: int = 2) -> str:
    return json.dumps(papers, ensure_ascii=False, indent=indent)


# ── Excel 导出 ────────────────────────────────────────

def to_excel(papers: List[Dict[str, Any]], filepath: str):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        raise RuntimeError("openpyxl 未安装。请运行: pip install openpyxl")

    wb = Workbook()
    ws = wb.active
    ws.title = "文献检索结果"

    headers = ["序号", "标题", "作者", "期刊", "年份", "被引次数", "DOI", "来源", "URL"]
    ws.append(headers)

    header_font = Font(bold=True)
    for col in range(1, len(headers) + 1):
        ws.cell(row=1, column=col).font = header_font

    for i, p in enumerate(papers, 1):
        ws.append([
            i,
            p.get("title", ""),
            p.get("authors", ""),
            _bibliographic_container(p),
            p.get("year", ""),
            p.get("cited_by", ""),
            p.get("doi", ""),
            p.get("source", ""),
            p.get("url", ""),
        ])

    for col in ws.columns:
        max_length = max(_display_width(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_length + 4, 60)

    wb.save(filepath)


# ── GB/T 7714 著者-出版年制 & 中文经管/社科顶刊体例 ─────────

# 多音姓氏 → GB2312 一级字库同音代表字映射（确保 GB2312 原生拼音序在多音姓氏上也 100% 准确）
_POLYPHONIC_SURNAME_HOMOPHONE = {
    "曾": "赠", "单": "善", "解": "谢", "仇": "球", "查": "楂",
    "区": "欧", "朴": "瓢", "覃": "秦", "翟": "宅", "缪": "妙",
    "盖": "葛", "阚": "看", "重": "崇", "秘": "必", "乐": "跃",
    "员": "云", "种": "崇", "繁": "婆", "乜": "聂", "冼": "显",
}

_COMPOUND_POLYPHONIC_HOMOPHONE = {
    "长孙": "张孙", "万俟": "莫奇", "尉迟": "玉迟", "澹台": "谈台", "皇甫": "黄甫",
}


def _cjk_char_sort_token(ch: str) -> tuple[int, int]:
    """利用国标 GB2312 一级汉字（3755 常用字）严格按汉语拼音升序编码的特性生成零依赖拼音排序键。"""
    if ch.isascii():
        return (0, ord(ch.lower()))
    try:
        b = ch.encode("gb2312")
        if len(b) == 2 and 0xB0 <= b[0] <= 0xD7:
            return (1, (b[0] << 8) | b[1])
        if len(b) == 2:
            # 二级生僻字若在姓氏拼音表内，转为拼音首字母区间再比较
            py = _COMMON_CJK_PINYIN.get(ch)
            if py:
                return (1, _cjk_char_sort_token(_PINYIN_INITIAL_EXEMPLAR.get(py[0], "啊"))[1] + ord(py[-1]))
            return (2, (b[0] << 8) | b[1])
    except UnicodeEncodeError:
        py = _COMMON_CJK_PINYIN.get(ch)
        if py:
            return (1, _cjk_char_sort_token(_PINYIN_INITIAL_EXEMPLAR.get(py[0], "啊"))[1])
    return (3, ord(ch))


_PINYIN_INITIAL_EXEMPLAR = {
    "a": "啊", "b": "芭", "c": "擦", "d": "搭", "e": "蛾", "f": "发", "g": "噶",
    "h": "哈", "j": "击", "k": "喀", "l": "垃", "m": "妈", "n": "拿", "o": "哦",
    "p": "啪", "q": "期", "r": "然", "s": "撒", "t": "塌", "w": "挖", "x": "昔",
    "y": "压", "z": "匝",
}


def _cjk_text_pinyin_key(text: str, *, is_person_name: bool = False) -> list[tuple[int, int]]:
    s = re.sub(r"\s+", "", text or "")
    if not s:
        return [(9, 0)]
    if is_person_name:
        if s[:2] in _COMPOUND_POLYPHONIC_HOMOPHONE:
            s = _COMPOUND_POLYPHONIC_HOMOPHONE[s[:2]] + s[2:]
        elif s[0] in _POLYPHONIC_SURNAME_HOMOPHONE:
            s = _POLYPHONIC_SURNAME_HOMOPHONE[s[0]] + s[1:]
    return [_cjk_char_sort_token(ch) for ch in s[:24]]


def _is_chinese_paper(paper: dict) -> bool:
    return _has_cjk(_first_value(paper, "title")) or _has_cjk(_first_value(paper, "authors"))


def _in_text_author_label(paper: dict, *, parenthetical: bool = False) -> str:
    authors = _split_authors(paper.get("authors"))
    if not authors:
        return "佚名" if _is_chinese_paper(paper) else "Anonymous"
    if _has_cjk(authors[0]) or _is_chinese_paper(paper):
        if len(authors) == 1:
            return authors[0]
        if len(authors) == 2:
            return f"{authors[0]}和{authors[1]}"
        return f"{authors[0]}等"
    surnames = [_extract_western_parts(a)[0] or a for a in authors]
    if len(surnames) == 1:
        return surnames[0]
    if len(surnames) == 2:
        conj = "&" if parenthetical else "and"
        return f"{surnames[0]} {conj} {surnames[1]}"
    return f"{surnames[0]} et al."


def prepare_author_year_papers(papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按著者-出版年制规则重排（中文按姓氏汉语拼音升序在前，西文按姓氏字母升序在后），
    并对相同文内著者标签+同一年份的多篇文献自动分配 `2020a`、`2020b` 区分后缀。
    """
    decorated: List[tuple[tuple, int, Dict[str, Any]]] = []
    for orig_idx, p in enumerate(papers):
        item = dict(p)
        item["_orig_index"] = orig_idx + 1
        is_cn = _is_chinese_paper(item)
        authors = _split_authors(item.get("authors"))
        first_au = authors[0] if authors else ""
        year_s = _get_year(item, "")
        try:
            year_num = int(re.match(r"\d{4}", year_s).group(0)) if re.match(r"\d{4}", year_s) else 9999
        except Exception:
            year_num = 9999

        if is_cn:
            au_key = _cjk_text_pinyin_key(first_au, is_person_name=True)
            all_au_key = _cjk_text_pinyin_key("".join(authors), is_person_name=False)
            ti_key = _cjk_text_pinyin_key(_first_value(item, "title"))
            sort_tuple = (0, au_key, all_au_key, year_num, ti_key, orig_idx)
        else:
            last, givens = _extract_western_parts(first_au)
            au_str = f"{last.lower()} {' '.join(g.lower() for g in givens)}".strip()
            ti_str = _first_value(item, "title").lower()
            sort_tuple = (1, au_str, year_num, ti_str, orig_idx)

        decorated.append((sort_tuple, orig_idx, item))

    decorated.sort(key=lambda x: x[0])
    ordered = [x[2] for x in decorated]

    # 统计 (语种组, 文内著者标准键, 年份) 出现次数，超 1 篇时赋予 a, b, c...
    from collections import Counter
    group_counts: Counter = Counter()
    for item in ordered:
        yr = _get_year(item, "")
        if yr:
            gk = (_is_chinese_paper(item), _in_text_author_label(item, parenthetical=False).lower(), yr)
            group_counts[gk] += 1

    group_seen: Counter = Counter()
    for item in ordered:
        yr = _get_year(item, "")
        if yr:
            gk = (_is_chinese_paper(item), _in_text_author_label(item, parenthetical=False).lower(), yr)
            if group_counts[gk] > 1:
                idx_in_grp = group_seen[gk]
                group_seen[gk] += 1
                suffix = chr(ord("a") + idx_in_grp) if idx_in_grp < 26 else f"_{idx_in_grp + 1}"
                item["_year_display"] = f"{yr}{suffix}"
            else:
                item["_year_display"] = yr
        else:
            item["_year_display"] = "出版年不详" if _is_chinese_paper(item) else "n.d."

    return ordered


def format_gbt7714_author_year(paper: dict, index: int = 1) -> str:
    """GB/T 7714-2015 第 9.2 节 著者-出版年制文末参考文献格式（无序号，年份紧随著者之后）。"""
    authors = _format_authors_gbt(paper.get("authors"), western_initials=True)
    year_disp = _as_text(paper.get("_year_display")) or _get_year(paper) or ("出版年不详" if _is_chinese_paper(paper) else "n.d.")
    title = _first_value(paper, "title")
    journal = _bibliographic_container(paper)
    container = _analytic_container(paper)
    date = _first_value(paper, "date", "published", "publication_date")
    volume = _first_value(paper, "volume")
    issue = _first_value(paper, "issue", "number")
    pages = _clean_pages(_first_value(paper, "pages"))
    place = _first_value(paper, "place", "publisher_place", "location")
    publisher = _first_value(paper, "publisher", "institution", "school")
    accessed = _first_value(paper, "accessed", "access_date")
    url = _first_value(paper, "url")
    doc_type = _detect_doc_type(paper)
    type_tag = _gbt_type_tag(doc_type, paper)

    ref = f"{authors}, {year_disp}. {title}[{type_tag}]. "
    if doc_type == "journal" and type_tag == "J/OL":
        ref += journal or "刊名不详"
        ymd_m = re.search(r"\d{4}-\d{2}-\d{2}", date)
        if ymd_m:
            ref += f"({ymd_m.group(0)})"
        if accessed:
            ref += f"[{accessed}]"
        if url and not _first_value(paper, "doi"):
            ref += f". {url}"
    elif doc_type == "journal":
        ref += journal or "刊名不详"
        if volume and issue:
            ref += f", {volume}({issue})"
        elif volume:
            ref += f", {volume}"
        elif issue:
            ref += f"({issue})"
        if pages:
            ref += f": {pages}"
    elif doc_type in ("book", "thesis", "report", "standard"):
        pub_bits = [b for b in (place, publisher) if b]
        if pub_bits:
            ref += ": ".join(pub_bits)
        if pages:
            ref += f": {pages}" if pub_bits else pages
    elif doc_type in ("conference", "chapter"):
        if container:
            ref += f"//{container}. "
        elif journal:
            ref += f"{journal}"
        pub_bits = [b for b in (place, publisher) if b]
        if pub_bits:
            ref += ": ".join(pub_bits)
        if pages:
            ref += f": {pages}"
    else:
        site = publisher or journal
        if site:
            ref += f"{site}"
        if pages:
            ref += f": {pages}" if site else pages
        if url and not _first_value(paper, "doi"):
            ref += f". {url}" if (site or pages) else url

    ref = _finish_reference(ref)
    doi = _first_value(paper, "doi")
    if doi and "doi" not in ref.lower():
        ref = ref.rstrip(" .") + f". DOI: {doi}."
    return ref


def _format_western_econ_authors(authors_raw: Any) -> str:
    """经济学英文文献著者格式：Vaswani, A., N. Shazeer, and N. Parmar"""
    authors = _split_authors(authors_raw)
    if not authors:
        return "Anonymous"
    if len(authors) > 3:
        first_last, first_givens = _extract_western_parts(authors[0])
        p0 = f"{first_last}, {_initials(first_givens)}".strip(", ")
        rest = []
        for a in authors[1:3]:
            last, givens = _extract_western_parts(a)
            inits = _initials(givens)
            rest.append(f"{inits} {last}".strip())
        return ", ".join([p0] + rest) + ", et al."
    first_last, first_givens = _extract_western_parts(authors[0])
    p0 = f"{first_last}, {_initials(first_givens)}".strip(", ")
    if len(authors) == 1:
        return p0
    others = []
    for a in authors[1:]:
        last, givens = _extract_western_parts(a)
        inits = _initials(givens)
        others.append(f"{inits} {last}".strip())
    if len(authors) == 2:
        return f"{p0}, and {others[0]}"
    return f"{p0}, {others[0]}, and {others[1]}"


def format_chinese_econ(paper: dict, index: int = 1) -> str:
    """国内经管/社科顶刊通用体例（《经济研究》《管理世界》《中国工业经济》风格）：
    - 中文：赵涛、张智、梁上坤，2020：《数字经济、创业活跃度与高质量发展——来自中国城市的经验证据》，《管理世界》第10期。
    - 英文：Vaswani, A., N. Shazeer, and N. Parmar, 2017, "Attention Is All You Need", NeurIPS, 30(1), 5998-6008.
    """
    year_disp = _as_text(paper.get("_year_display")) or _get_year(paper) or ("出版年不详" if _is_chinese_paper(paper) else "n.d.")
    title = _first_value(paper, "title")
    journal = _bibliographic_container(paper)
    container = _analytic_container(paper)
    volume = _first_value(paper, "volume")
    issue = _first_value(paper, "issue", "number")
    pages = _clean_pages(_first_value(paper, "pages"))
    publisher = _first_value(paper, "publisher", "institution", "school")
    doc_type = _detect_doc_type(paper)

    if _is_chinese_paper(paper):
        authors = _split_authors(paper.get("authors"))
        if not authors:
            au_str = "佚名"
        elif len(authors) <= 3:
            au_str = "、".join(authors)
        else:
            au_str = "、".join(authors[:3]) + "等"

        ref = f"{au_str}，{year_disp}：《{title}》"
        if doc_type == "journal":
            if journal:
                ref += f"，《{journal}》"
            if issue:
                ref += f"第{issue.lstrip('0') or issue}期"
            elif volume:
                ref += f"第{volume}卷"
            elif paper.get("is_online_first"):
                ref += "（网络首发）"
        elif doc_type == "thesis":
            deg_kind = _thesis_degree_kind(paper)
            deg_label = "博士学位论文" if deg_kind == "doctor" else ("硕士学位论文" if deg_kind == "master" else "学位论文")
            inst = publisher or journal
            if inst:
                ref += f"，{inst}{deg_label}"
            else:
                ref += f"，{deg_label}"
        elif doc_type == "book":
            if publisher:
                ref += f"，{publisher}"
        else:
            if journal or container or publisher:
                ref += f"，《{journal or container or publisher}》"
        return ref.rstrip("。.") + "。"

    # 西文条目
    au_en = _format_western_econ_authors(paper.get("authors"))
    venue = journal or container or publisher
    bits = [au_en, str(year_disp), f'"{title}"']
    if venue:
        bits.append(venue)
    if volume and issue:
        bits.append(f"{volume}({issue})")
    elif volume:
        bits.append(volume)
    elif issue:
        bits.append(f"No. {issue}")
    if pages:
        bits.append(pages)
    return ", ".join(b for b in bits if b).rstrip(".") + "."


def generate_in_text_citations(papers: List[Dict[str, Any]], style: str = "gbt7714-author-year") -> Dict[str, Any]:
    """生成与参考文献列表一一对应的正文内嵌引文（句中叙述式 + 句末括号式 + 多篇合并括号式）。"""
    style_l = (style or "").lower()
    is_author_year = style_l in ("gbt7714-author-year", "author-year", "chinese-econ", "econ", "apa", "mla", "chicago")
    working = (
        prepare_author_year_papers(papers)
        if style_l in ("gbt7714-author-year", "author-year", "chinese-econ", "econ")
        else [dict(p, _orig_index=i + 1, _year_display=_get_year(p, "n.d.")) for i, p in enumerate(papers)]
    )

    items: List[Dict[str, Any]] = []
    grouped_for_combined: List[tuple[bool, str, List[str]]] = []

    for idx, p in enumerate(working, 1):
        is_cn = _is_chinese_paper(p)
        yr = _as_text(p.get("_year_display")) or _get_year(p, "n.d.")
        if not is_author_year:
            items.append({
                "index": idx,
                "title": _first_value(p, "title"),
                "narrative": f"{_in_text_author_label(p)}[{idx}]",
                "parenthetical": f"[{idx}]",
            })
            continue

        nar_au = _in_text_author_label(p, parenthetical=False)
        par_au = _in_text_author_label(p, parenthetical=True)
        if is_cn:
            narrative = f"{nar_au}（{yr}）"
            parenthetical = f"（{par_au}，{yr}）"
        else:
            narrative = f"{nar_au} ({yr})"
            parenthetical = f"({par_au}, {yr})"

        items.append({
            "orig_index": p.get("_orig_index", idx),
            "title": _first_value(p, "title"),
            "year": yr,
            "narrative": narrative,
            "parenthetical": parenthetical,
        })

        # 合并多篇括号引用时，同作者不同年份合并为 `赵涛等，2020a，2020b`
        if grouped_for_combined and grouped_for_combined[-1][0] == is_cn and grouped_for_combined[-1][1] == par_au:
            grouped_for_combined[-1][2].append(yr)
        else:
            grouped_for_combined.append((is_cn, par_au, [yr]))

    if not is_author_year:
        combined = f"[{', '.join(str(i + 1) for i in range(len(working)))}]" if working else ""
    else:
        any_cn = any(g[0] for g in grouped_for_combined)
        seg_strs = []
        for is_cn, au, yrs in grouped_for_combined:
            if is_cn:
                seg_strs.append(f"{au}，{'，'.join(yrs)}")
            else:
                seg_strs.append(f"{au}, {', '.join(yrs)}")
        if not seg_strs:
            combined = ""
        elif any_cn:
            combined = f"（{'；'.join(seg_strs)}）"
        else:
            combined = f"({'; '.join(seg_strs)})"

    return {
        "style": style,
        "combined": combined,
        "items": items,
    }


# ── 引用列表生成 ──────────────────────────────────────

def generate_reference_list(papers: list[dict], style: str = "gbt7714") -> str:
    """生成完整参考文献列表"""
    style = (style or "gbt7714").lower()
    if style in ("gbt7714-author-year", "author-year"):
        ordered = prepare_author_year_papers(papers)
        return "\n".join(format_gbt7714_author_year(p, i + 1) for i, p in enumerate(ordered))
    if style in ("chinese-econ", "econ"):
        ordered = prepare_author_year_papers(papers)
        return "\n".join(format_chinese_econ(p, i + 1) for i, p in enumerate(ordered))

    formatters = {
        "gbt7714": format_gbt7714,
        "gb": format_gbt7714,
        "footnote": format_footnote,
        "apa": format_apa,
        "mla": format_mla,
        "chicago": format_chicago,
    }
    formatter = formatters.get(style, format_gbt7714)
    refs = [formatter(p, i + 1) for i, p in enumerate(papers)]
    return "\n".join(refs)


def citation_preview(paper: dict) -> str:
    """生成简略引用预览（无需完整元数据也能输出）。
    格式: 作者. 题名[J]. 期刊, 年, 卷(期): 页码.
    """
    authors = paper.get("authors", "")
    if authors:
        parts = re.split(r'[;；]\s*', authors)
        if len(parts) > 2:
            first = parts[0].strip()
            suffix = "等" if _has_cjk(first) else "et al."
            authors = f"{first} {suffix}"
        else:
            authors = "; ".join(p.strip() for p in parts)

    title = paper.get("title", "")
    journal = _bibliographic_container(paper)
    year = paper.get("year", "")
    volume = paper.get("volume", "")
    issue = paper.get("issue", "")
    pages = paper.get("pages", "")

    ref = f"{authors}. {title}[J]. " if authors else f"{title}[J]. "
    if journal:
        ref += f"{journal}, "
    if year:
        ref += f"{year}"
    if volume:
        ref += f", {volume}"
    if issue:
        ref += f"({issue})"
    if pages:
        ref += f": {pages}"
    ref += "."
    if not pages and not paper.get("is_online_first") and _detect_doc_type(paper) == "journal":
        ref += " [页码待cite补全]"
    return ref


# ── 统一导出入口 ──────────────────────────────────────

def export_papers(papers: list[dict], fmt: str, output: str = None) -> str | dict:
    """
    统一导出入口。

    Args:
        papers: 论文列表
        fmt:    导出格式 (bibtex/ris/markdown/json/excel/gbt7714/footnote/apa/mla/chicago)
        output: 输出文件路径（可选）

    Returns:
        格式化后的字符串（excel 除外）
    """
    if fmt == "excel":
        path = output or "papers_export.xlsx"
        to_excel(papers, path)
        return f"已导出到 {path}"

    generators = {
        "bibtex": lambda: to_bibtex(papers),
        "ris": lambda: to_ris(papers),
        "markdown": lambda: to_markdown_table(papers),
        "json": lambda: to_json(papers),
        "gbt7714": lambda: generate_reference_list(papers, "gbt7714"),
        "gb": lambda: generate_reference_list(papers, "gbt7714"),
        "gbt7714-author-year": lambda: generate_reference_list(papers, "gbt7714-author-year"),
        "author-year": lambda: generate_reference_list(papers, "gbt7714-author-year"),
        "chinese-econ": lambda: generate_reference_list(papers, "chinese-econ"),
        "econ": lambda: generate_reference_list(papers, "chinese-econ"),
        "footnote": lambda: generate_reference_list(papers, "footnote"),
        "apa": lambda: generate_reference_list(papers, "apa"),
        "mla": lambda: generate_reference_list(papers, "mla"),
        "chicago": lambda: generate_reference_list(papers, "chicago"),
    }

    gen = generators.get(fmt)
    if not gen:
        return {"status": "error", "code": "UNSUPPORTED_EXPORT_FORMAT", "message": f"不支持的格式: {fmt}"}

    content = gen()

    if output:
        Path(output).write_text(content, encoding="utf-8")
        return f"已导出到 {output}"

    return content
