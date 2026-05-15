"""
formatter.py - 引用格式化与导出模块
支持: GB/T 7714-2015, 脚注格式, APA, BibTeX, RIS, Markdown, JSON, Excel
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


# ── GB/T 7714-2015 ────────────────────────────────────

def format_gbt7714(paper: dict, index: int = 1) -> str:
    """
    GB/T 7714-2015 格式化（顺序编码制）
    [1] 作者. 题名[文献类型标识]. 刊名, 年, 卷(期): 页码.
    """
    authors = paper.get("authors", "佚名")
    title = paper.get("title", "")
    journal = paper.get("journal", "")
    year = paper.get("year", "")
    volume = paper.get("volume", "")
    issue = paper.get("issue", "")
    pages = paper.get("pages", "")
    doi = paper.get("doi", "")

    doc_type = _detect_doc_type(paper)
    type_tag = {"journal": "J", "book": "M", "thesis": "D",
                "conference": "C", "preprint": "J/OL", "webpage": "EB/OL"}.get(doc_type, "J")

    authors_formatted = _format_authors_gbt(authors)

    ref = f"[{index}] {authors_formatted}. {title}[{type_tag}]. "
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

    if doi:
        ref += f" DOI: {doi}."

    return ref


def _has_cjk(text: str) -> bool:
    return any('\u4e00' <= c <= '\u9fff' for c in text)


def _format_authors_gbt(authors_str: str) -> str:
    if not authors_str:
        return "佚名"
    separators = re.split(r'[;；,，&]\s*', authors_str)
    authors = [a.strip() for a in separators if a.strip()]
    if len(authors) <= 3:
        return ", ".join(authors)
    suffix = ", 等" if _has_cjk(authors[0]) else ", et al."
    return ", ".join(authors[:3]) + suffix


def _detect_doc_type(paper: dict) -> str:
    journal = paper.get("journal", "").lower()
    source = paper.get("source", "").lower()
    if "arxiv" in journal or "preprint" in journal:
        return "preprint"
    if "thesis" in source or "dissertation" in source:
        return "thesis"
    if "conference" in source or "proceedings" in source:
        return "conference"
    if paper.get("journal"):
        return "journal"
    return "journal"


# ── 脚注格式（中文文科常用）────────────────────────────

def format_footnote(paper: dict, index: int = 1) -> str:
    """
    中文脚注格式（文科常用）
    ① 作者：《书名/文章名》，出版社/期刊名，年份，第X页。
    """
    authors = paper.get("authors", "佚名")
    title = paper.get("title", "")
    journal = paper.get("journal", "")
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
    authors = paper.get("authors", "")
    year = paper.get("year", "n.d.")
    title = paper.get("title", "")
    journal = paper.get("journal", "")
    volume = paper.get("volume", "")
    issue = paper.get("issue", "")
    pages = paper.get("pages", "")
    doi = paper.get("doi", "")

    authors_formatted = _format_authors_apa(authors)
    ref = f"{authors_formatted} ({year}). {title}. "
    if journal:
        ref += f"*{journal}*"
        if volume:
            ref += f", *{volume}*"
        if issue:
            ref += f"({issue})"
        if pages:
            ref += f", {pages}"
        ref += "."
    if doi:
        ref += f" https://doi.org/{doi}"

    return ref


def _format_authors_apa(authors_str: str) -> str:
    if not authors_str:
        return "Anonymous"
    separators = re.split(r'[;；,，]\s*', authors_str)
    authors = [a.strip() for a in separators if a.strip()]
    if len(authors) == 1:
        return authors[0]
    if len(authors) == 2:
        return f"{authors[0]}, & {authors[1]}"
    if len(authors) <= 20:
        return ", ".join(authors[:-1]) + f", & {authors[-1]}"
    return ", ".join(authors[:19]) + f", ... {authors[-1]}"


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
        bib_type = {"journal": "article", "book": "book", "thesis": "phdthesis",
                     "conference": "inproceedings", "preprint": "article"}.get(doc_type, "article")

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
        ris_type = {"journal": "JOUR", "book": "BOOK", "thesis": "THES",
                     "conference": "CONF", "preprint": "JOUR"}.get(doc_type, "JOUR")

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
        journal = _md_escape(p.get("journal", "")[:20])
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
            p.get("journal", ""),
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
    formatters = {
        "gbt7714": format_gbt7714,
        "gb": format_gbt7714,
        "footnote": format_footnote,
        "apa": format_apa,
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
    journal = paper.get("journal", "")
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
        fmt:    导出格式 (bibtex/ris/markdown/json/excel/gbt7714/footnote/apa)
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
    }

    gen = generators.get(fmt)
    if not gen:
        return {"status": "error", "code": "UNSUPPORTED_EXPORT_FORMAT", "message": f"不支持的格式: {fmt}"}

    content = gen()

    if output:
        Path(output).write_text(content, encoding="utf-8")
        return f"已导出到 {output}"

    return content
