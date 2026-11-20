"""
docx_tools.py - 论文文件读取（.docx/.txt/.md）、PDF 元数据提取与 Word 学术排版/补丁工具
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def _default_output(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_read_paper(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    """读取用户论文文件（.docx / .txt / .md），输出 UTF-8 纯文本（支持 --numbered 段落锚点）。"""
    filepath = Path(args.filepath)
    if not filepath.exists():
        output_fn({"status": "error", "code": "FILE_NOT_FOUND", "message": f"文件不存在: {args.filepath}"})
        return

    suffix = filepath.suffix.lower()
    numbered = bool(getattr(args, "numbered", False))
    toc_only = bool(getattr(args, "toc", False))
    section_spec = getattr(args, "section", None)
    max_chars = getattr(args, "max_chars", None)
    text = ""
    raw_for_sections = ""
    anchors: List[Dict[str, str]] = []

    if suffix == ".docx":
        try:
            from docx import Document
        except ImportError:
            output_fn({"status": "error", "code": "MISSING_DEPENDENCY", "message": "缺少 python-docx 依赖"})
            return
        try:
            doc = Document(str(filepath))
            parts: List[str] = []
            sec_parts: List[str] = []
            for p_idx, p in enumerate(doc.paragraphs, 1):
                stripped = p.text.strip()
                if stripped:
                    pid = f"P{p_idx}"
                    parts.append(f"[{pid}] {p.text}" if numbered else p.text)
                    st_name = str(getattr(p.style, "name", "") or "").lower()
                    if st_name.startswith("heading 1") or "标题 1" in st_name:
                        sec_parts.append(f"# {stripped}")
                    elif st_name.startswith("heading 2") or "标题 2" in st_name:
                        sec_parts.append(f"## {stripped}")
                    elif st_name.startswith("heading 3") or "标题 3" in st_name:
                        sec_parts.append(f"### {stripped}")
                    else:
                        sec_parts.append(p.text)
                    if numbered:
                        anchors.append({"id": pid, "text": stripped[:120]})
            for t_idx, table in enumerate(doc.tables, 1):
                for r_idx, row in enumerate(table.rows, 1):
                    row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if row_texts:
                        tid = f"T{t_idx}R{r_idx}"
                        joined = " | ".join(row_texts)
                        parts.append(f"[{tid}] {joined}" if numbered else joined)
                        sec_parts.append(joined)
                        if numbered:
                            anchors.append({"id": tid, "text": joined[:120]})
            text = "\n\n".join(parts)
            raw_for_sections = "\n\n".join(sec_parts)
        except Exception as e:
            output_fn({"status": "error", "code": "DOCX_PARSE_FAILED", "message": f"docx 解析失败: {e}"})
            return

    elif suffix in (".txt", ".md", ".markdown", ".tex"):
        decoded = False
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb2312", "gb18030"):
            try:
                text = filepath.read_text(encoding=enc)
                decoded = True
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if not decoded:
            output_fn({"status": "error", "code": "ENCODING_ERROR", "message": "无法识别文件编码"})
            return
        if suffix == ".tex":
            # 过滤纯注释行以提取干净的正文段落结构
            text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("%"))
            raw_for_sections = re.sub(
                r"\\(chapter|section)\*?\{([^}]+)\}", r"# \2",
                re.sub(r"\\subsection\*?\{([^}]+)\}", r"## \1", text),
            )
        else:
            raw_for_sections = text
        if numbered and text:
            blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
            parts = []
            for b_idx, blk in enumerate(blocks, 1):
                bid = f"P{b_idx}"
                parts.append(f"[{bid}] {blk}")
                anchors.append({"id": bid, "text": blk.strip()[:120]})
            text = "\n\n".join(parts)

    elif suffix == ".pdf":
        pdf_ft = extract_fulltext_from_pdf_file(filepath)
        if not pdf_ft:
            output_fn({
                "status": "error",
                "code": "PDF_EXTRACT_FAILED",
                "message": f"无法从 PDF 提取文本（可能是纯扫描件或缺少 pdfplumber/pypdf）: {filepath}",
            })
            return
        text = pdf_ft
        raw_for_sections = pdf_ft
    else:
        output_fn({"status": "error", "code": "UNSUPPORTED_FORMAT", "message": f"不支持的文件格式: {suffix}"})
        return

    if toc_only or section_spec or max_chars:
        from sections import parse_fulltext_sections, public_toc, slice_fulltext_sections
        if toc_only and not section_spec:
            toc = public_toc(parse_fulltext_sections(raw_for_sections))
            if args.raw:
                print("\n".join(
                    f"[S{s['index']}] {'  ' * (s['level'] - 1)}{s['title']}"
                    + (f" (第{s['page']}页, {s['chars']}字)" if s.get("page") else f" ({s['chars']}字)")
                    for s in toc
                ))
            else:
                output_fn({
                    "status": "success",
                    "file": str(filepath),
                    "total_chars": len(raw_for_sections),
                    "section_count": len(toc),
                    "toc": toc,
                })
            return
        sliced_res = slice_fulltext_sections(raw_for_sections, section_spec or "", max_chars=max_chars)
        text = sliced_res["text"] if section_spec else sliced_res["text"]
        if not args.output and not args.raw:
            payload_sec: Dict[str, Any] = {
                "status": "success" if (not section_spec or sliced_res["matched"]) else "warning",
                "file": str(filepath),
                "total_chars": len(raw_for_sections),
                "chars": len(text),
                "toc": sliced_res["toc"],
                "matched_sections": sliced_res["matched_sections"],
                "truncated": sliced_res["truncated"],
                "text": text,
            }
            if sliced_res.get("warning"):
                payload_sec["warning"] = sliced_res["warning"]
            output_fn(payload_sec)
            return

    char_count = len(text)
    para_count = len(anchors) if numbered else (text.count("\n\n") + 1 if text else 0)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        output_fn({
            "status": "success",
            "message": f"已提取到: {args.output}",
            "chars": char_count,
            "paragraphs": para_count,
            "numbered": numbered,
        })
    else:
        if args.raw:
            print(text)
        else:
            payload: Dict[str, Any] = {
                "status": "success",
                "chars": char_count,
                "paragraphs": para_count,
                "text": text,
            }
            if numbered:
                payload["numbered"] = True
                payload["anchors"] = anchors
            output_fn(payload)


def _extract_doi_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(?:DOI|doi)[：:\s]*\s*(10\.\d{4,}/[^\s\"'<>]+)", text)
    if m:
        return m.group(1).rstrip(".,;)")
    m2 = re.search(r"\b(10\.\d{4,}/[^\s\"'<>]+)", text)
    if m2:
        return m2.group(1).rstrip(".,;)")
    return None


_JUNK_PDF_META_RE = re.compile(
    r"^(?:untitled|microsoft\s+word|wps\s+office|adobe\s+acrobat|cajviewer|document|\d+|.*\.docx?|.*\.pdf)$",
    re.IGNORECASE,
)


def _clean_pdf_meta_field(val: Any) -> str:
    s = str(val or "").strip()
    if not s or _JUNK_PDF_META_RE.match(s):
        return ""
    return s


def extract_fulltext_from_pdf_file(filepath: Path) -> Optional[str]:
    """从指定 PDF 文件提取带 `[第 N 页]` 标记的全文（优先 pdfplumber，回退 pypdf）。"""
    if not filepath.is_file():
        return None
    pages_out: List[str] = []
    try:
        import pdfplumber
        with pdfplumber.open(str(filepath)) as pdf:
            for idx, page in enumerate(pdf.pages, 1):
                t = (page.extract_text() or "").strip()
                if t:
                    pages_out.append(f"[第 {idx} 页]\n{t}")
    except ImportError:
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(filepath))
            for idx, page in enumerate(reader.pages, 1):
                t = (page.extract_text() or "").strip()
                if t:
                    pages_out.append(f"[第 {idx} 页]\n{t}")
        except Exception:
            return None
    except Exception:
        return None
    combined = "\n\n".join(pages_out).strip()
    return combined if combined else None


def extract_pdf_bibliography(
    filepath: Path,
    crossref_resolver: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """从本地单篇中英文 PDF 提取结构化题录（DOI/Crossref + 中文知网首页摘要/关键词/年卷期页识别），供 `import` 写入课题库。"""
    PdfReader = None
    pdfplumber = None
    try:
        import pdfplumber as _pdfplumber
        pdfplumber = _pdfplumber
    except ImportError:
        try:
            from pypdf import PdfReader as _PdfReader
            PdfReader = _PdfReader
        except ImportError:
            return {
                "status": "error",
                "code": "MISSING_DEPENDENCY",
                "message": "缺少 pdfplumber 或 pypdf 依赖，请运行 pip install pdfplumber",
            }

    title = ""
    authors = ""
    subject = ""
    first_pages_text: List[str] = []
    doi: Optional[str] = None

    if pdfplumber is not None:
        with pdfplumber.open(str(filepath)) as pdf:
            meta = pdf.metadata or {}
            title = _clean_pdf_meta_field(meta.get("Title"))
            authors = _clean_pdf_meta_field(meta.get("Author"))
            subject = _clean_pdf_meta_field(meta.get("Subject"))
            if subject:
                doi = _extract_doi_from_text(subject)
            for page in pdf.pages[:3]:
                pt = page.extract_text() or ""
                if pt.strip():
                    first_pages_text.append(pt)
                if not doi and pt:
                    doi = _extract_doi_from_text(pt)
    else:
        reader = PdfReader(str(filepath))
        meta = reader.metadata or {}
        title = _clean_pdf_meta_field(getattr(meta, "title", ""))
        authors = _clean_pdf_meta_field(getattr(meta, "author", ""))
        subject = _clean_pdf_meta_field(getattr(meta, "subject", ""))
        for i in range(min(3, len(reader.pages))):
            pt = reader.pages[i].extract_text() or ""
            if pt.strip():
                first_pages_text.append(pt)
            if not doi and pt:
                doi = _extract_doi_from_text(pt)

    head_text = "\n".join(first_pages_text[:2])
    entry: Dict[str, Any] = {
        "title": title,
        "authors": authors,
        "journal": "",
        "year": None,
        "abstract": "",
        "keywords": [],
        "local_pdf": str(filepath.resolve()),
        "has_fulltext": True,
        "source": "Local-PDF",
    }

    if doi:
        entry["doi"] = doi
        if crossref_resolver is None:
            try:
                from search import resolve_crossref as crossref_resolver
            except ImportError:
                crossref_resolver = None
        if crossref_resolver:
            try:
                cr = crossref_resolver(doi)
                if isinstance(cr, dict):
                    for k in ("title", "authors", "journal", "year", "volume", "issue", "pages"):
                        if cr.get(k) and not entry.get(k):
                            entry[k] = cr[k]
            except Exception:
                pass

    # 中文/英文首页文本启发式提取（针对知网下载 PDF 元数据为空的场景）
    if head_text:
        abs_m = re.search(
            r"(?:摘\s*要|内容提要|Abstract)\s*[：:.\s]\s*(.+?)(?=\n\s*(?:关键词|关\s*键\s*词|中图分类号|Keywords|Key\s*words|一、|1\s+引言|1\.))",
            head_text,
            re.DOTALL | re.IGNORECASE,
        )
        if abs_m and not entry["abstract"]:
            entry["abstract"] = re.sub(r"\s+", " ", abs_m.group(1)).strip()[:1500]

        kw_m = re.search(
            r"(?:关键词|关\s*键\s*词|Keywords|Key\s*words)\s*[：:]\s*([^\n]+)",
            head_text,
            re.IGNORECASE,
        )
        if kw_m and not entry["keywords"]:
            kws = [k.strip() for k in re.split(r"[;；,，、\u3000]{1,}|\s{2,}", kw_m.group(1)) if 1 < len(k.strip()) <= 25]
            if kws:
                entry["keywords"] = kws[:10]

        if not entry.get("year"):
            ym = re.search(r"((?:19|20)\d{2})\s*年\s*第?\s*(\d{1,2})?\s*期", head_text) or re.search(
                r"\b((?:19|20)\d{2})\s*[,，]\s*(\d+)\s*\(\s*(\d+)\s*\)", head_text
            )
            if ym:
                entry["year"] = int(ym.group(1))
                if ym.lastindex and ym.lastindex >= 3 and ym.group(3):
                    entry["volume"] = ym.group(2)
                    entry["issue"] = ym.group(3).lstrip("0") or "0"
                elif ym.lastindex and ym.lastindex >= 2 and ym.group(2):
                    entry["issue"] = ym.group(2).lstrip("0") or "0"
            else:
                y_any = re.search(r"\b(20[0-2]\d|19[89]\d)\b", head_text[:600])
                if y_any:
                    entry["year"] = int(y_any.group(1))

    # 文件名回退提取（如知网默认文件名 `数字经济与高质量发展_赵涛.pdf`）
    if not entry["title"]:
        stem = re.sub(r"\s*\(\d+\)$", "", filepath.stem).strip()
        if "_" in stem:
            t_part, a_part = stem.rsplit("_", 1)
            if len(t_part.strip()) >= 4 and 2 <= len(a_part.strip()) <= 15:
                entry["title"] = t_part.strip()
                if not entry["authors"]:
                    entry["authors"] = a_part.strip()
        if not entry["title"]:
            entry["title"] = stem

    return entry


def import_pdfs_from_path(
    target: Path,
    crossref_resolver: Optional[Callable[[str], Any]] = None,
) -> List[Dict[str, Any]]:
    """支持导入单个 PDF 文件或整个 PDF 文件夹（递归扫描 `.pdf`）。"""
    pdf_files: List[Path] = []
    if target.is_file() and target.suffix.lower() == ".pdf":
        pdf_files = [target]
    elif target.is_dir():
        pdf_files = sorted(p for p in target.rglob("*.pdf") if p.is_file())
    if not pdf_files:
        return [{"status": "error", "code": "NO_PDF_FOUND", "message": f"未在路径中找到 PDF 文件: {target}"}]

    entries: List[Dict[str, Any]] = []
    for pdf_file in pdf_files:
        item = extract_pdf_bibliography(pdf_file, crossref_resolver=crossref_resolver)
        if item.get("status") == "error" and item.get("code") == "MISSING_DEPENDENCY":
            return [item]
        if item.get("title"):
            entries.append(item)
    return entries


def cmd_pdf_meta(
    args,
    output_fn: Callable[[Any], None] = _default_output,
    crossref_resolver: Optional[Callable[[str], Any]] = None,
) -> None:
    """从 PDF 文件中提取元数据（优先 pypdf，自动回退 requirements.txt 中的 pdfplumber）。"""
    filepath = Path(args.filepath)
    if not filepath.exists():
        output_fn({"status": "error", "code": "FILE_NOT_FOUND", "message": f"文件不存在: {args.filepath}"})
        return

    if filepath.suffix.lower() != ".pdf":
        output_fn({"status": "error", "code": "NOT_PDF", "message": "仅支持 PDF 文件"})
        return

    PdfReader = None
    pdfplumber = None
    try:
        from pypdf import PdfReader as _PdfReader
        PdfReader = _PdfReader
    except ImportError:
        try:
            import pdfplumber as _pdfplumber
            pdfplumber = _pdfplumber
        except ImportError:
            output_fn({
                "status": "error",
                "code": "MISSING_DEPENDENCY",
                "message": "缺少 pdfplumber 或 pypdf 依赖，请运行 pip install pdfplumber",
            })
            return

    try:
        result: Dict[str, Any] = {"status": "success", "file": str(filepath)}
        doi: Optional[str] = None

        if PdfReader is not None:
            reader = PdfReader(str(filepath))
            meta = reader.metadata or {}
            if getattr(meta, "title", None):
                result["title"] = meta.title
            if getattr(meta, "author", None):
                result["authors"] = meta.author
            if getattr(meta, "subject", None):
                result["subject"] = meta.subject

            if hasattr(reader, "xmp_metadata") and reader.xmp_metadata:
                xmp = reader.xmp_metadata
                if hasattr(xmp, "dc_identifier") and xmp.dc_identifier:
                    idents = xmp.dc_identifier if isinstance(xmp.dc_identifier, list) else [xmp.dc_identifier]
                    for ident in idents:
                        doi = _extract_doi_from_text(str(ident or ""))
                        if doi:
                            break

            if not doi:
                for page_num in range(min(3, len(reader.pages))):
                    page_text = reader.pages[page_num].extract_text() or ""
                    doi = _extract_doi_from_text(page_text)
                    if doi:
                        break
        else:
            with pdfplumber.open(str(filepath)) as pdf:
                meta = pdf.metadata or {}
                if meta.get("Title"):
                    result["title"] = str(meta["Title"])
                if meta.get("Author"):
                    result["authors"] = str(meta["Author"])
                if meta.get("Subject"):
                    result["subject"] = str(meta["Subject"])
                    doi = _extract_doi_from_text(str(meta["Subject"]))

                if not doi:
                    for page in pdf.pages[:3]:
                        page_text = page.extract_text() or ""
                        doi = _extract_doi_from_text(page_text)
                        if doi:
                            break

        if doi:
            result["doi"] = doi
            if crossref_resolver is None:
                from search import resolve_crossref as crossref_resolver
            crossref_data = crossref_resolver(doi)
            if crossref_data:
                result["crossref"] = crossref_data

        output_fn(result)
    except Exception as e:
        output_fn({"status": "error", "code": "PDF_READ_FAILED", "message": str(e)})


def _get_or_create_footnotes_part(doc):
    """获取或创建符合 OOXML 标准的 footnotes XmlPart。返回 (footnotes_element, max_id)。"""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.part import XmlPart
    from docx.oxml.ns import qn
    from lxml import etree

    try:
        ftn_part = doc.part.part_related_by(RT.FOOTNOTES)
        if isinstance(ftn_part, XmlPart):
            ftn_element = ftn_part.element
        else:
            ftn_element = etree.fromstring(ftn_part.blob)
            ftn_part.__class__ = XmlPart
            ftn_part._element = ftn_element
    except KeyError:
        ftn_xml = (
            '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<w:footnote w:type="separator" w:id="0">'
            '<w:p><w:r><w:separator/></w:r></w:p>'
            '</w:footnote>'
            '<w:footnote w:type="continuationSeparator" w:id="1">'
            '<w:p><w:r><w:continuationSeparator/></w:r></w:p>'
            '</w:footnote>'
            '</w:footnotes>'
        )
        ftn_element = etree.fromstring(ftn_xml)
        from docx.opc.constants import CONTENT_TYPE as CT
        from docx.opc.packuri import PackURI

        ftn_part = XmlPart(
            PackURI("/word/footnotes.xml"),
            CT.WML_FOOTNOTES,
            ftn_element,
            doc.part.package,
        )
        doc.part.relate_to(ftn_part, RT.FOOTNOTES)

    max_id = 1
    for fn in ftn_element.findall(qn("w:footnote")):
        fid = fn.get(qn("w:id"))
        if fid and fid.isdigit():
            max_id = max(max_id, int(fid))

    return ftn_element, max_id


def _add_footnote_to_element(ftn_element, fn_id_counter, run_element, fn_text):
    """在 run_element 后插入脚注引用，并在 footnotes 部件中添加脚注内容。返回新 id。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    fn_id_counter[0] += 1
    fid = fn_id_counter[0]

    footnote_el = OxmlElement("w:footnote")
    footnote_el.set(qn("w:id"), str(fid))
    fn_p = OxmlElement("w:p")

    fn_r_ref = OxmlElement("w:r")
    fn_rpr = OxmlElement("w:rPr")
    fn_rstyle = OxmlElement("w:rStyle")
    fn_rstyle.set(qn("w:val"), "FootnoteReference")
    fn_rpr.append(fn_rstyle)
    fn_r_ref.append(fn_rpr)
    fn_r_ref.append(OxmlElement("w:footnoteRef"))
    fn_p.append(fn_r_ref)

    fn_r_text = OxmlElement("w:r")
    fn_t = OxmlElement("w:t")
    fn_t.set(qn("xml:space"), "preserve")
    fn_t.text = " " + fn_text
    fn_r_text.append(fn_t)
    fn_p.append(fn_r_text)

    footnote_el.append(fn_p)
    ftn_element.append(footnote_el)

    ref_run = OxmlElement("w:r")
    ref_rpr = OxmlElement("w:rPr")
    ref_style = OxmlElement("w:rStyle")
    ref_style.set(qn("w:val"), "FootnoteReference")
    ref_rpr.append(ref_style)
    ref_run.append(ref_rpr)
    ref_mark = OxmlElement("w:footnoteReference")
    ref_mark.set(qn("w:id"), str(fid))
    ref_run.append(ref_mark)
    run_element.addnext(ref_run)

    return fid


def _para_replace_text(paragraph, find_text, replace_text):
    """段落级文本替换：支持单 run 与跨 run 精确切割替换，保留其余 run 格式。"""
    for run in paragraph.runs:
        if find_text in run.text:
            run.text = run.text.replace(find_text, replace_text, 1)
            return True

    runs = paragraph.runs
    if not runs:
        return False

    boundaries = []
    pos = 0
    for run in runs:
        end = pos + len(run.text)
        boundaries.append((pos, end, run))
        pos = end

    full = "".join(r.text for r in runs)
    if find_text not in full:
        return False

    idx = full.index(find_text)
    end_idx = idx + len(find_text)

    first_i = last_i = None
    for i, (s, e, _) in enumerate(boundaries):
        if first_i is None and s <= idx < e:
            first_i = i
        if s < end_idx <= e:
            last_i = i
            break

    if first_i is None or last_i is None:
        return False

    fs, _fe, first_run = boundaries[first_i]
    ls, _le, last_run = boundaries[last_i]

    if first_i == last_i:
        offset = idx - fs
        first_run.text = first_run.text[:offset] + replace_text + first_run.text[offset + len(find_text):]
    else:
        first_run.text = first_run.text[:idx - fs] + replace_text
        for j in range(first_i + 1, last_i):
            boundaries[j][2].text = ""
        last_run.text = last_run.text[end_idx - ls:]

    return True


def _find_run_containing(paragraph, text):
    """在段落中找到包含指定文本末尾字符的 run._element。"""
    if not text:
        return None

    for run in paragraph.runs:
        if text in run.text:
            return run._element

    runs = paragraph.runs
    if not runs:
        return None

    boundaries = []
    pos = 0
    for run in runs:
        end = pos + len(run.text)
        boundaries.append((pos, end, run))
        pos = end

    full = "".join(r.text for r in runs)
    if text not in full:
        return None

    end_idx = full.index(text) + len(text)
    target = end_idx - 1
    for start, end, run in boundaries:
        if start <= target < end:
            return run._element

    return runs[-1]._element


def _write_docx_from_markdown(md_text: str, output_path: Path) -> Dict[str, Any]:
    """Markdown 文本 → 学术格式 .docx"""
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.shared import Cm, Pt
    except ImportError:
        return {"status": "error", "code": "MISSING_DEPENDENCY", "message": "缺少 python-docx 依赖"}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()

    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(3.17)
    section.right_margin = Cm(3.17)

    style = doc.styles["Normal"]
    font = style.font
    font.name = "Times New Roman"
    font.size = Pt(12)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    pf = style.paragraph_format
    pf.line_spacing = 1.5
    pf.space_after = Pt(0)
    pf.first_line_indent = Cm(0.74)

    for level in range(1, 4):
        hs = doc.styles[f"Heading {level}"]
        hs.font.name = "Times New Roman"
        hs.font.size = Pt(16 - level * 2)
        hs.font.bold = True
        hs.element.rPr.rFonts.set(qn("w:eastAsia"), "黑体")

    lines = md_text.split("\n")
    footnotes_map: Dict[str, str] = {}
    body_lines: List[str] = []
    ref_section_lines: List[str] = []
    in_ref_section = False
    warnings: List[str] = []

    for line in lines:
        fn_def = re.match(r"^\[\^(\d+)\]:\s*(.+)$", line)
        if fn_def:
            footnotes_map[fn_def.group(1)] = fn_def.group(2)
            continue
        if re.match(r"^#{1,3}\s*(参考文献|References)", line, re.IGNORECASE):
            in_ref_section = True
            continue
        if in_ref_section:
            if line.strip():
                ref_section_lines.append(line.strip())
            continue
        body_lines.append(line)

    ftn_element, max_id = _get_or_create_footnotes_part(doc)
    fn_counter = [max_id]

    def _parse_inline(paragraph, text):
        pattern = re.compile(r"(\*\*(.+?)\*\*|\*(.+?)\*|\[\^(\d+)\])")
        last = 0
        for m in pattern.finditer(text):
            if m.start() > last:
                paragraph.add_run(text[last:m.start()])
            if m.group(2):
                paragraph.add_run(m.group(2)).bold = True
            elif m.group(3):
                paragraph.add_run(m.group(3)).italic = True
            elif m.group(4):
                fn_id = m.group(4)
                fn_text_content = footnotes_map.get(fn_id, "")
                if not fn_text_content:
                    warnings.append(f"[^{fn_id}] 无对应脚注定义，脚注内容为空")
                r = paragraph.add_run("")
                _add_footnote_to_element(ftn_element, fn_counter, r._element, fn_text_content)
            last = m.end()
        if last < len(text):
            paragraph.add_run(text[last:])

    for line in body_lines:
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            p = doc.add_paragraph()
            p.style = doc.styles[f"Heading {level}"]
            _parse_inline(p, heading.group(2).strip())
            continue
        ul_match = re.match(r"^[-*]\s+(.+)$", line)
        if ul_match:
            try:
                p = doc.add_paragraph(style="List Bullet")
            except KeyError:
                p = doc.add_paragraph()
            _parse_inline(p, ul_match.group(1).strip())
            continue
        ol_match = re.match(r"^\d{1,3}[.)]\s+(.+)$", line)
        if ol_match:
            try:
                p = doc.add_paragraph(style="List Number")
            except KeyError:
                p = doc.add_paragraph()
            _parse_inline(p, ol_match.group(1).strip())
            continue
        if not line.strip():
            continue
        p = doc.add_paragraph()
        _parse_inline(p, line.strip())

    if ref_section_lines:
        doc.add_heading("参考文献", level=1)
        for ref_line in ref_section_lines:
            ref_line = re.sub(r"^\[\d+\]\s*", "", ref_line)
            ref_line = re.sub(r"^[-•]\s*", "", ref_line)
            p = doc.add_paragraph(ref_line)
            p.paragraph_format.first_line_indent = Cm(-0.74)
            p.paragraph_format.left_indent = Cm(0.74)

    try:
        doc.save(str(output_path))
    except Exception as e:
        return {"status": "error", "code": "IO_ERROR", "message": f"保存失败: {e}"}

    result: Dict[str, Any] = {
        "status": "success" if not warnings else "warning",
        "message": f"已生成: {output_path}",
        "output": str(output_path),
        "footnotes": fn_counter[0] - max_id,
        "references": len(ref_section_lines),
    }
    if warnings:
        result["warnings"] = warnings
    return result


def cmd_write_docx(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    """Markdown 文件 → 学术格式 .docx"""
    md_path = Path(args.filepath)
    if not md_path.exists():
        output_fn({"status": "error", "code": "FILE_NOT_FOUND", "message": f"文件不存在: {args.filepath}"})
        return

    md_text = None
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb2312", "gb18030"):
        try:
            md_text = md_path.read_text(encoding=enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if md_text is None:
        output_fn({"status": "error", "code": "ENCODING_ERROR", "message": "文件编码无法识别"})
        return

    output_path = Path(args.output) if args.output else md_path.with_suffix(".docx")
    output_fn(_write_docx_from_markdown(md_text, output_path))


def _resolve_target_paragraphs(doc, anchor: Any) -> List[Any]:
    """根据可选段落/单元格锚点（如 `'P12'`、`12`、`'T1R2'`）返回候选段落对象列表。"""
    if anchor is not None and str(anchor).strip() != "":
        raw = str(anchor).strip().upper()
        if raw.isdigit():
            idx = int(raw) - 1
            return [doc.paragraphs[idx]] if 0 <= idx < len(doc.paragraphs) else []
        m_p = re.match(r"^P(\d+)$", raw)
        if m_p:
            idx = int(m_p.group(1)) - 1
            return [doc.paragraphs[idx]] if 0 <= idx < len(doc.paragraphs) else []
        m_t = re.match(r"^T(\d+)R(\d+)$", raw)
        if m_t:
            t_i, r_i = int(m_t.group(1)) - 1, int(m_t.group(2)) - 1
            if 0 <= t_i < len(doc.tables) and 0 <= r_i < len(doc.tables[t_i].rows):
                cell_paras = []
                for cell in doc.tables[t_i].rows[r_i].cells:
                    cell_paras.extend(cell.paragraphs)
                return cell_paras
            return []
        return []

    # 未指定锚点时：先遍历正文段落，再遍历表格单元格段落
    all_paras = list(doc.paragraphs)
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                for cp in cell.paragraphs:
                    if cp not in all_paras:
                        all_paras.append(cp)
    return all_paras


def cmd_patch_docx(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    """在现有 .docx 上打补丁：支持段落锚点定位、表格单元格替换与 --dry-run 零写入预检。"""
    try:
        from docx import Document
        from docx.shared import Cm
    except ImportError:
        output_fn({"status": "error", "code": "MISSING_DEPENDENCY", "message": "缺少 python-docx 依赖"})
        return

    docx_path = Path(args.filepath)
    if not docx_path.exists():
        output_fn({"status": "error", "code": "FILE_NOT_FOUND", "message": f"文件不存在: {args.filepath}"})
        return

    patch_path = Path(args.patch)
    if not patch_path.exists():
        output_fn({"status": "error", "code": "FILE_NOT_FOUND", "message": f"补丁文件不存在: {args.patch}"})
        return

    try:
        patch_data = json.loads(patch_path.read_text(encoding="utf-8"))
    except Exception as e:
        output_fn({"status": "error", "code": "PATCH_PARSE_FAILED", "message": f"补丁 JSON 解析失败: {e}"})
        return

    if not isinstance(patch_data, dict):
        output_fn({"status": "error", "code": "PATCH_PARSE_FAILED", "message": "补丁文件顶层必须是 JSON 对象"})
        return

    patches = patch_data.get("patches", [])
    footnotes_list = patch_data.get("footnotes", [])
    append_refs = patch_data.get("append_references", [])
    dry_run = bool(getattr(args, "dry_run", False))

    if not isinstance(patches, list) or not isinstance(footnotes_list, list) or not isinstance(append_refs, list):
        output_fn({
            "status": "error",
            "code": "PATCH_PARSE_FAILED",
            "message": "patches / footnotes / append_references 必须是数组",
        })
        return

    output_path = Path(args.output) if args.output else docx_path.with_name(
        docx_path.stem + "_patched" + docx_path.suffix
    )

    doc = Document(str(docx_path))
    stats = {"replaced": 0, "not_found": 0, "ambiguous": 0, "footnotes_added": 0, "references_appended": 0}
    warnings: List[str] = []

    ftn_element, max_id = _get_or_create_footnotes_part(doc)
    fn_counter = [max_id]

    for patch in patches:
        find_text = patch.get("find", "")
        replace_text = patch.get("replace", "")
        anchor = patch.get("paragraph")
        if not find_text:
            continue
        candidates = _resolve_target_paragraphs(doc, anchor)
        matching_paras = [p for p in candidates if find_text in p.text]
        if len(matching_paras) > 1 and not anchor:
            stats["ambiguous"] += 1
            short = f"{find_text[:25]}..." if len(find_text) > 25 else find_text
            warnings.append(f'发现 {len(matching_paras)} 处重复匹配（默认仅替换首处，建议配合 read-paper --numbered 指定 "paragraph": "P..."）: "{short}"')

        found = False
        for para in matching_paras:
            if _para_replace_text(para, find_text, replace_text):
                stats["replaced"] += 1
                found = True
                break
        if not found:
            stats["not_found"] += 1
            prefix = f"[{anchor}] " if anchor else ""
            warnings.append(
                f'{prefix}未找到替换目标: "{find_text[:30]}..."' if len(find_text) > 30 else f'{prefix}未找到替换目标: "{find_text}"'
            )

    for fn_entry in footnotes_list:
        after_text = fn_entry.get("after", "")
        fn_text = fn_entry.get("text", "")
        anchor = fn_entry.get("paragraph")
        if not after_text or not fn_text:
            continue
        candidates = _resolve_target_paragraphs(doc, anchor)
        found = False
        for para in candidates:
            if after_text not in para.text:
                continue
            run_el = _find_run_containing(para, after_text)
            if run_el is not None:
                _add_footnote_to_element(ftn_element, fn_counter, run_el, fn_text)
                stats["footnotes_added"] += 1
                found = True
                break
        if not found:
            warnings.append(
                f'脚注定位失败: "{after_text[:30]}"' if len(after_text) > 30 else f'脚注定位失败: "{after_text}"'
            )

    if append_refs:
        if not dry_run:
            existing_ref_heading = None
            for para in doc.paragraphs:
                if para.text.strip() in ("参考文献", "References") and para.style.name.startswith("Heading"):
                    existing_ref_heading = para
            if existing_ref_heading is None:
                doc.add_paragraph()
                try:
                    ref_heading = doc.add_paragraph("参考文献")
                    ref_heading.style = doc.styles["Heading 1"]
                except KeyError:
                    ref_heading = doc.add_paragraph("参考文献")
                    ref_heading.runs[0].bold = True
            for ref_text in append_refs:
                p = doc.add_paragraph(ref_text)
                p.paragraph_format.first_line_indent = Cm(-0.74)
                p.paragraph_format.left_indent = Cm(0.74)
        stats["references_appended"] = len(append_refs)

    has_issues = stats["not_found"] > 0 or len(warnings) > 0
    if dry_run:
        res_dry: Dict[str, Any] = {
            "status": "partial" if has_issues else "success",
            "dry_run": True,
            "message": "补丁预检通过（未写盘）" if not has_issues else "补丁预检发现未命中或多处重复项（未写盘）",
            "target_output": str(output_path),
            **stats,
        }
        if warnings:
            res_dry["warnings"] = warnings
        output_fn(res_dry)
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.save(str(output_path))
    except Exception as e:
        output_fn({"status": "error", "code": "IO_ERROR", "message": f"保存失败: {e}"})
        return

    result: Dict[str, Any] = {
        "status": "partial" if has_issues else "success",
        "message": f"已保存: {output_path}",
        "output": str(output_path),
        **stats,
    }
    if warnings:
        result["warnings"] = warnings
    output_fn(result)
