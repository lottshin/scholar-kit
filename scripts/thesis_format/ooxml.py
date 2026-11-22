"""Low-level OOXML helpers for thesis formatting."""
from __future__ import annotations

from typing import Dict


def add_bottom_border(paragraph, color: str = "000000", size: int = 6) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    ppr = paragraph._p.get_or_add_pPr()
    pbdr = ppr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        ppr.append(pbdr)
    bottom = pbdr.find(qn("w:bottom"))
    if bottom is None:
        bottom = OxmlElement("w:bottom")
        pbdr.append(bottom)
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color)


def add_page_number(paragraph, prefix: str = "第 ", suffix: str = " 页") -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    paragraph.add_run(prefix)
    run = paragraph.add_run()
    for fld_type, text in [("begin", None), (None, " PAGE "), ("separate", None), (None, "1"), ("end", None)]:
        if fld_type:
            fld = OxmlElement("w:fldChar")
            fld.set(qn("w:fldCharType"), fld_type)
            run._r.append(fld)
        else:
            node = OxmlElement("w:instrText" if text == " PAGE " else "w:t")
            node.text = text
            run._r.append(node)
    paragraph.add_run(suffix)


def apply_headers_footers(doc, spec: Dict) -> Dict[str, int]:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    counts = {"headers": 0, "footers": 0, "separator_lines": 0}
    for section in doc.sections:
        if spec.get("header_text"):
            paragraph = _first_paragraph(section.header)
            _replace_paragraph_text(paragraph, str(spec["header_text"]))
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            if spec.get("header_separator_line"):
                add_bottom_border(paragraph)
                counts["separator_lines"] += 1
            counts["headers"] += 1
        if spec.get("footer_page_numbers"):
            paragraph = _first_paragraph(section.footer)
            _clear_paragraph_content(paragraph)
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            add_page_number(paragraph)
            counts["footers"] += 1
    return counts


def format_note_parts(doc, footnote_rule: Dict, endnote_rule: Dict) -> Dict[str, int]:
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    counts = {"footnotes": _format_note_part(doc, RT.FOOTNOTES, footnote_rule)}
    endnote_rel = getattr(
        RT,
        "ENDNOTES",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes",
    )
    counts["endnotes"] = _format_note_part(doc, endnote_rel, endnote_rule)
    return counts


def _first_paragraph(container):
    if container.paragraphs:
        return container.paragraphs[0]
    return container.add_paragraph()


def _replace_paragraph_text(paragraph, text: str) -> None:
    _clear_paragraph_content(paragraph)
    paragraph.add_run(text)


def _clear_paragraph_content(paragraph) -> None:
    from docx.oxml.ns import qn

    for child in list(paragraph._p):
        if child.tag in {qn("w:r"), qn("w:hyperlink")}:
            paragraph._p.remove(child)


def _format_note_part(doc, rel_type, rule: Dict) -> int:
    from docx.oxml.ns import qn

    try:
        part = doc.part.part_related_by(rel_type)
    except KeyError:
        return 0
    root = _part_root(part)
    count = 0
    for paragraph in root.findall(".//" + qn("w:p")):
        _set_paragraph_spacing(paragraph, rule)
    for run in root.findall(".//" + qn("w:r")):
        _set_run_rule(run, rule)
        count += 1
    _write_part_root(part, root)
    return count


def _part_root(part):
    if hasattr(part, "element"):
        return part.element
    from lxml import etree

    return etree.fromstring(part.blob)


def _write_part_root(part, root) -> None:
    if hasattr(part, "element"):
        return
    from lxml import etree

    part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _set_paragraph_spacing(paragraph, rule: Dict) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    if rule.get("line_spacing") is None:
        return
    ppr = paragraph.find(qn("w:pPr"))
    if ppr is None:
        ppr = OxmlElement("w:pPr")
        paragraph.insert(0, ppr)
    spacing = ppr.find(qn("w:spacing"))
    if spacing is None:
        spacing = OxmlElement("w:spacing")
        ppr.append(spacing)
    spacing.set(qn("w:lineRule"), "auto")
    spacing.set(qn("w:line"), str(int(float(rule["line_spacing"]) * 240)))


def _set_run_rule(run, rule: Dict) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    rpr = run.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        run.insert(0, rpr)
    if rule.get("font_name"):
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.append(rfonts)
        for attr in ("w:eastAsia", "w:ascii", "w:hAnsi"):
            rfonts.set(qn(attr), rule["font_name"])
    if rule.get("font_size_pt") is not None:
        size = rpr.find(qn("w:sz"))
        if size is None:
            size = OxmlElement("w:sz")
            rpr.append(size)
        size.set(qn("w:val"), str(int(float(rule["font_size_pt"]) * 2)))
