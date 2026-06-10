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


def _bibliographic_container(paper: dict) -> str:
    """Return a publication/container title, never the data-source marker."""
    return _first_value(
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
            if name:
                authors.append(name.strip())
        return authors

    raw = _as_text(authors_raw)
    if not raw:
        return []

    parts = re.split(r'\s*(?:;|；|\band\b|&)\s*', raw)
    if len(parts) == 1 and ("，" in raw or "," in raw):
        comma_parts = [p.strip() for p in re.split(r'[，,]\s*', raw) if p.strip()]
        if not (len(comma_parts) == 2 and " " not in comma_parts[0] and " " not in comma_parts[1]):
            parts = comma_parts
    return [p.strip() for p in parts if p.strip()]


def _format_authors_gbt(authors_raw: Any) -> str:
    authors = _split_authors(authors_raw)
    if not authors:
        return "佚名"
    if len(authors) <= 3:
        return ", ".join(authors)
    suffix = ", 等" if _has_cjk(authors[0]) else ", et al."
    return ", ".join(authors[:3]) + suffix


def _initials(names: list[str]) -> str:
    letters = []
    for name in names:
        clean = re.sub(r"[^A-Za-z-]", "", name)
        if clean:
            letters.append(clean[0].upper() + ".")
    return " ".join(letters)


def _format_person_apa(name: str) -> str:
    name = name.strip()
    if not name or _has_cjk(name):
        return name
    if "," in name:
        last, rest = [p.strip() for p in name.split(",", 1)]
        initials = _initials(rest.split())
        return f"{last}, {initials}".strip()
    parts = name.split()
    if len(parts) == 1:
        return name
    return f"{parts[-1]}, {_initials(parts[:-1])}".strip()


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
        return re.sub(r"^\[\d+\]", f"[{index}]", raw, count=1)
    return f"[{index}] {raw}"


def _detect_doc_type(paper: dict) -> str:
    explicit = _first_value(
        paper, "doc_type", "document_type", "type", "resource_type",
        "literature_type", "category"
    ).lower()
    combined = " ".join(
        _first_value(paper, key).lower()
        for key in (
            "doc_type", "document_type", "type", "literature_type",
            "category", "journal", "venue", "publication", "source_title",
            "title", "url"
        )
    )

    aliases = {
        "journal": "journal", "article": "journal", "periodical": "journal", "期刊": "journal",
        "newspaper": "newspaper", "报纸": "newspaper",
        "book": "book", "monograph": "book", "图书": "book", "专著": "book",
        "thesis": "thesis", "dissertation": "thesis", "master": "thesis",
        "doctor": "thesis", "硕士": "thesis", "博士": "thesis", "学位": "thesis",
        "conference": "conference", "proceedings": "conference", "会议": "conference",
        "chapter": "chapter", "incollection": "chapter", "析出": "chapter",
        "report": "report", "technical report": "report", "报告": "report",
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
    for key, value in aliases.items():
        if key in combined:
            return value

    if _first_value(paper, "patent_no", "patent_number"):
        return "patent"
    if _first_value(paper, "standard_no", "standard_number"):
        return "standard"
    if _first_value(paper, "booktitle", "book_title", "proceedings_title"):
        return "chapter"
    if _bibliographic_container(paper):
        return "journal"
    if _first_value(paper, "publisher"):
        return "book"
    if _first_value(paper, "url"):
        return "webpage"
    return "journal"


def _gbt_type_tag(doc_type: str, paper: dict) -> str:
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

def format_gbt7714(paper: dict, index: int = 1) -> str:
    """
    GB/T 7714-2015 格式化（顺序编码制）
    按文献类型生成期刊、专著、学位论文、会议析出、专利、电子文献等常见著录格式。
    """
    raw = _first_value(paper, "gbt7714_raw", "gbt7714")
    if raw:
        return _renumber_raw_reference(raw, index)

    authors = _format_authors_gbt(paper.get("authors"))
    title = _first_value(paper, "title")
    journal = _bibliographic_container(paper)
    container = _analytic_container(paper)
    year = _get_year(paper)
    date = _first_value(paper, "date", "published", "publication_date")
    volume = _first_value(paper, "volume")
    issue = _first_value(paper, "issue", "number")
    pages = _clean_pages(_first_value(paper, "pages"))
    place = _first_value(paper, "place", "publisher_place", "location")
    publisher = _first_value(paper, "publisher", "institution", "school")
    accessed = _first_value(paper, "accessed", "access_date")
    url = _first_value(paper, "url")
    patent_no = _first_value(paper, "patent_no", "patent_number", "publication_number")
    standard_no = _first_value(paper, "standard_no", "standard_number")
    doc_type = _detect_doc_type(paper)
    type_tag = _gbt_type_tag(doc_type, paper)

    ref = f"[{index}] {authors}. {title}[{type_tag}]. "
    if doc_type == "journal":
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
        if publisher:
            ref += f"{publisher}. "
        if date or year:
            ref += f"({date or year})"
        if accessed:
            ref += f"[{accessed}]"
        if url:
            ref += f". {url}"

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
        kind = _first_value(paper, "degree", "thesis_type") or "Doctoral dissertation"
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
        kind = _first_value(paper, "degree", "thesis_type") or "PhD diss."
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


def to_bibtex(papers: List[Dict[str, Any]]) -> str:
    entries = []
    for i, p in enumerate(papers):
        key = _bibtex_key(p, i)
        doc_type = _detect_doc_type(p)
        bib_type = {
            "journal": "article",
            "newspaper": "article",
            "book": "book",
            "thesis": "phdthesis",
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
            fields.append("  author = {{{}}}".format(_bibtex_escape(p["authors"])))
        if p.get("year"):
            fields.append("  year = {{{}}}".format(p["year"]))
        if p.get("journal"):
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

    return "\n\n".join(entries)


def _bibtex_key(paper: dict, index: int) -> str:
    first_author = re.split(r'[;；,，\s]\s*', paper.get("authors", "unknown"))[0]
    first_author = re.sub(r'[^a-zA-Z\u4e00-\u9fff]', '', first_author)
    year = paper.get("year", "")
    return f"{first_author}{year}_{index}"


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


# ── 引用列表生成 ──────────────────────────────────────

def generate_reference_list(papers: list[dict], style: str = "gbt7714") -> str:
    """生成完整参考文献列表"""
    style = (style or "gbt7714").lower()
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
