"""Chinese thesis format audit and DOCX formatting helpers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .numbering import apply_heading_numbering
from .ooxml import apply_headers_footers, format_note_parts
from .roles import classify_paragraph_roles, style_name_for_role
from .spec import CM_PER_CHAR, deep_update, default_spec, load_spec_file, parse_text_spec

TOLERANCE_CM = 0.08
TOLERANCE_PT = 0.2


def load_spec_from_inputs(
    spec_path: Optional[str] = None,
    spec_text: Optional[str] = None,
    template_path: Optional[str] = None,
) -> Dict[str, Any]:
    spec = default_spec()
    if template_path:
        deep_update(spec, extract_template_spec(Path(template_path)))
    if spec_path:
        deep_update(spec, load_spec_file(Path(spec_path)))
    if spec_text:
        deep_update(spec, parse_text_spec(spec_text))
    return spec


def extract_template_spec(template_path: Path) -> Dict[str, Any]:
    if not template_path.exists():
        raise ValueError(f"模板文件不存在: {template_path}")
    if template_path.suffix.lower() != ".docx":
        raise ValueError("模板必须是 .docx 文件；旧版 .doc 请先转换为 .docx")
    doc = _open_docx(template_path)
    actual = _drop_none_values(extract_document_spec(doc))
    actual["source"] = [str(template_path)]
    return actual


def audit_document(docx_path: Path, spec: Dict[str, Any]) -> Dict[str, Any]:
    doc = _open_docx(docx_path)
    actual = extract_document_spec(doc)
    modified, unchanged, needs = _compare_specs(actual, spec)
    markdown = render_markdown_report(docx_path, modified, unchanged, needs, mode="audit")
    return {
        "status": "success",
        "mode": "audit",
        "document": str(docx_path),
        "spec": spec,
        "actual": actual,
        "modified": modified,
        "unchanged": unchanged,
        "needs_confirmation": needs,
        "markdown": markdown,
    }


def format_document(docx_path: Path, output_path: Path, spec: Dict[str, Any]) -> Dict[str, Any]:
    if docx_path.resolve() == output_path.resolve():
        return {"status": "error", "code": "OUTPUT_OVERWRITES_SOURCE", "message": "输出文件不能覆盖原文件"}
    before = docx_path.read_bytes()
    doc = _open_docx(docx_path)
    actual = extract_document_spec(doc)
    modified, unchanged, needs = _compare_specs(actual, spec)
    applied = _apply_spec(doc, spec)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    preserved = before == docx_path.read_bytes()
    markdown = render_markdown_report(docx_path, modified, unchanged, needs, mode="format", output=output_path)
    return {
        "status": "success",
        "mode": "format",
        "document": str(docx_path),
        "output": str(output_path),
        "original_preserved": preserved,
        "modified": modified,
        "unchanged": unchanged,
        "needs_confirmation": needs,
        "applied_changes": applied,
        "markdown": markdown,
    }


def cmd_format_audit(args, *, output_fn: Callable[[Any], None]) -> None:
    docx_path = Path(args.filepath)
    if not docx_path.exists():
        output_fn({"status": "error", "code": "FILE_NOT_FOUND", "message": f"文件不存在: {docx_path}"})
        return
    if docx_path.suffix.lower() != ".docx":
        output_fn({"status": "error", "code": "UNSUPPORTED_FORMAT", "message": "仅支持 .docx 文件；旧版 .doc 请先另存为 .docx"})
        return
    try:
        spec = load_spec_from_inputs(
            spec_path=getattr(args, "spec", None),
            spec_text=getattr(args, "spec_text", None),
            template_path=getattr(args, "template", None),
        )
        result = audit_document(docx_path, spec)
        result = write_report(result, getattr(args, "output", None), getattr(args, "format", "json"))
    except ValueError as exc:
        output_fn({"status": "error", "code": "INVALID_FORMAT_SPEC", "message": str(exc)})
        return
    except RuntimeError as exc:
        output_fn({"status": "error", "code": "DOCX_DEPENDENCY_MISSING", "message": str(exc)})
        return
    if getattr(args, "raw", False):
        print(result.get("markdown", ""))
        return
    output_fn(result)


def cmd_format_docx(args, *, output_fn: Callable[[Any], None]) -> None:
    docx_path = Path(args.filepath)
    if not docx_path.exists():
        output_fn({"status": "error", "code": "FILE_NOT_FOUND", "message": f"文件不存在: {docx_path}"})
        return
    if docx_path.suffix.lower() != ".docx":
        output_fn({"status": "error", "code": "UNSUPPORTED_FORMAT", "message": "仅支持 .docx 文件；旧版 .doc 请先另存为 .docx"})
        return
    out_str = getattr(args, "output", None)
    output_path = Path(out_str) if out_str else docx_path.with_name(f"{docx_path.stem}_formatted.docx")
    try:
        spec = load_spec_from_inputs(
            spec_path=getattr(args, "spec", None),
            spec_text=getattr(args, "spec_text", None),
            template_path=getattr(args, "template", None),
        )
        result = format_document(docx_path, output_path, spec)
        if result.get("status") == "success":
            result = write_report(
                result,
                getattr(args, "report_output", None),
                getattr(args, "report_format", "json"),
            )
    except ValueError as exc:
        output_fn({"status": "error", "code": "INVALID_FORMAT_SPEC", "message": str(exc)})
        return
    except RuntimeError as exc:
        output_fn({"status": "error", "code": "DOCX_DEPENDENCY_MISSING", "message": str(exc)})
        return
    if getattr(args, "raw", False) and result.get("status") == "success":
        print(result.get("markdown", ""))
        return
    output_fn(result)


def extract_document_spec(doc) -> Dict[str, Any]:
    section = doc.sections[0]
    return {
        "paper_size": _paper_size_name(section.page_width, section.page_height),
        "margins_cm": {
            "top": _cm(section.top_margin),
            "bottom": _cm(section.bottom_margin),
            "left": _cm(section.left_margin),
            "right": _cm(section.right_margin),
        },
        "body": _style_rule(doc, "Normal"),
        "headings": {
            "level1": _style_rule(doc, "Heading 1"),
            "level2": _style_rule(doc, "Heading 2"),
            "level3": _style_rule(doc, "Heading 3"),
        },
    }


def render_markdown_report(
    docx_path: Path,
    modified: List[Dict[str, Any]],
    unchanged: List[Dict[str, Any]],
    needs: List[Dict[str, Any]],
    mode: str,
    output: Optional[Path] = None,
) -> str:
    title = "学位论文格式审计报告" if mode == "audit" else "学位论文格式化报告"
    lines = [f"# {title}", "", f"- 原文档：{docx_path}"]
    if output:
        lines.append(f"- 输出文档：{output}")
    lines.extend(["", "## 已修改/将修改", *_format_items(modified)])
    lines.extend(["", "## 未修改/已符合", *_format_items(unchanged)])
    lines.extend(["", "## 需人工确认", *_format_items(needs)])
    return "\n".join(lines).rstrip() + "\n"


def write_report(result: Dict[str, Any], output_path: Optional[str], report_format: str) -> Dict[str, Any]:
    if not output_path:
        return result
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if report_format == "markdown":
        path.write_text(result.get("markdown", ""), encoding="utf-8")
    else:
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["report_output"] = str(path)
    return result


def _compare_specs(actual: Dict[str, Any], spec: Dict[str, Any]):
    checks = _build_checks(actual, spec)
    modified: List[Dict[str, Any]] = []
    unchanged: List[Dict[str, Any]] = []
    needs = _manual_confirmation_items()
    for item in checks:
        if item["actual"] is None:
            needs.append({**item, "reason": "无法从文档自动判断"})
        elif _values_match(item["actual"], item["expected"]):
            unchanged.append({**item, "status": "unchanged"})
        else:
            modified.append({**item, "status": "modified"})
    return modified, unchanged, needs


def _build_checks(actual: Dict[str, Any], spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    checks = [{"id": "paper_size", "label": "纸张大小", "actual": actual.get("paper_size"), "expected": spec.get("paper_size")}]
    for key, label in {"top": "上边距", "bottom": "下边距", "left": "左边距", "right": "右边距"}.items():
        checks.append(_check(f"margin_{key}", label, actual.get("margins_cm", {}).get(key), spec.get("margins_cm", {}).get(key)))
    for key, label in {"font_name": "正文字体", "font_size_pt": "正文字号", "line_spacing": "正文行距"}.items():
        checks.append(_check(f"body_{key}", label, actual.get("body", {}).get(key), spec.get("body", {}).get(key)))
    checks.append(_check("body_first_line_indent", "正文首行缩进", actual.get("body", {}).get("first_line_indent_chars"), spec.get("body", {}).get("first_line_indent_chars")))
    for level, label in {"level1": "一级标题字号", "level2": "二级标题字号", "level3": "三级标题字号"}.items():
        checks.append(_check(f"{level}_font_size", label, actual.get("headings", {}).get(level, {}).get("font_size_pt"), spec.get("headings", {}).get(level, {}).get("font_size_pt")))
    return checks


def _check(item_id: str, label: str, actual: Any, expected: Any) -> Dict[str, Any]:
    return {"id": item_id, "label": label, "actual": actual, "expected": expected}


def _drop_none_values(data: Any) -> Any:
    if isinstance(data, dict):
        cleaned = {key: _drop_none_values(value) for key, value in data.items() if value is not None}
        return {key: value for key, value in cleaned.items() if value != {}}
    return data


def _apply_spec(doc, spec: Dict[str, Any]) -> Dict[str, Any]:
    _apply_section(doc.sections[0], spec)
    header_footer = apply_headers_footers(doc, spec)
    _apply_style(doc, "Normal", spec.get("body", {}))
    for style_name, rule in [("Heading 1", "level1"), ("Heading 2", "level2"), ("Heading 3", "level3")]:
        _apply_style(doc, style_name, spec.get("headings", {}).get(rule, {}))
    role_pairs = classify_paragraph_roles(doc.paragraphs)
    applied = {
        "paragraphs": 0,
        "roles": {},
        "header_footer": header_footer,
        "numbered_headings": apply_heading_numbering(role_pairs, spec),
    }
    for paragraph, role in role_pairs:
        style_name = style_name_for_role(role)
        if style_name:
            paragraph.style = style_name
        _apply_paragraph(paragraph, _rule_for_role(role, spec))
        applied["paragraphs"] += 1
        applied["roles"][role] = applied["roles"].get(role, 0) + 1
    applied["notes"] = format_note_parts(doc, spec.get("footnotes", {}), spec.get("endnotes", {}))
    return applied


def _apply_section(section, spec: Dict[str, Any]) -> None:
    from docx.shared import Cm

    margins = spec.get("margins_cm", {})
    for attr, key in [("top_margin", "top"), ("bottom_margin", "bottom"), ("left_margin", "left"), ("right_margin", "right")]:
        if margins.get(key) is not None:
            setattr(section, attr, Cm(float(margins[key])))
    if spec.get("paper_size") == "A4":
        section.page_width, section.page_height = Cm(21), Cm(29.7)


def _apply_style(doc, style_name: str, rule: Dict[str, Any]) -> None:
    try:
        style = doc.styles[style_name]
    except KeyError:
        return
    _apply_font(style.font, style._element, rule)
    pf = style.paragraph_format
    if rule.get("line_spacing") is not None:
        pf.line_spacing = float(rule["line_spacing"])
    if rule.get("first_line_indent_chars") is not None:
        from docx.shared import Cm

        pf.first_line_indent = Cm(float(rule["first_line_indent_chars"]) * CM_PER_CHAR)


def _apply_paragraph(paragraph, rule: Dict[str, Any]) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm

    alignment = {"center": WD_ALIGN_PARAGRAPH.CENTER, "left": WD_ALIGN_PARAGRAPH.LEFT}.get(rule.get("alignment"))
    if alignment is not None:
        paragraph.alignment = alignment
    if rule.get("line_spacing") is not None:
        paragraph.paragraph_format.line_spacing = float(rule["line_spacing"])
    if rule.get("hanging_indent_chars") is not None:
        width = Cm(float(rule["hanging_indent_chars"]) * CM_PER_CHAR)
        paragraph.paragraph_format.left_indent = width
        paragraph.paragraph_format.first_line_indent = -width
    elif rule.get("first_line_indent_chars") is not None:
        paragraph.paragraph_format.first_line_indent = Cm(float(rule["first_line_indent_chars"]) * CM_PER_CHAR)
    for run in paragraph.runs:
        _apply_font(run.font, run._element, rule)


def _rule_for_role(role: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    if role in {"level1", "references_heading"}:
        return spec.get("headings", {}).get("level1", {})
    if role == "level2":
        return spec.get("headings", {}).get("level2", {})
    if role == "level3":
        return spec.get("headings", {}).get("level3", {})
    if role == "figure_caption":
        return spec.get("captions", {}).get("figure", spec.get("body", {}))
    if role == "table_caption":
        return spec.get("captions", {}).get("table", spec.get("body", {}))
    if role in {"abstract", "keywords", "references"}:
        return spec.get(role, spec.get("body", {}))
    return spec.get("body", {})


def _apply_font(font, element, rule: Dict[str, Any]) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    if rule.get("font_name"):
        font.name = rule["font_name"]
        rpr = element.get_or_add_rPr()
        rpr.get_or_add_rFonts().set(qn("w:eastAsia"), rule["font_name"])
    if rule.get("font_size_pt") is not None:
        font.size = Pt(float(rule["font_size_pt"]))
    if rule.get("bold") is not None:
        font.bold = bool(rule["bold"])


def _style_rule(doc, style_name: str) -> Dict[str, Any]:
    try:
        style = doc.styles[style_name]
    except KeyError:
        return {}
    return {
        "font_name": _font_name(style.font),
        "font_size_pt": _pt(style.font.size),
        "bold": style.font.bold,
        "line_spacing": style.paragraph_format.line_spacing,
        "first_line_indent_chars": _indent_chars(style.paragraph_format.first_line_indent),
    }


def _font_name(font) -> Optional[str]:
    from docx.oxml.ns import qn

    if font.name:
        return font.name
    try:
        rfonts = font._element.rFonts
        return rfonts.get(qn("w:eastAsia")) or rfonts.get(qn("w:ascii")) if rfonts is not None else None
    except AttributeError:
        return None


def _manual_confirmation_items() -> List[Dict[str, Any]]:
    labels = ["封面格式", "目录自动生成", "图表题注", "学校专属页眉页脚", "参考文献逐条类型校验"]
    return [{"id": "manual_" + str(i), "label": label, "reason": "高风险或学校专属规则，需人工确认"} for i, label in enumerate(labels, 1)]


def _format_items(items: List[Dict[str, Any]]) -> List[str]:
    if not items:
        return ["- 无"]
    return [f"- {i['label']}：{i.get('actual', '未知')} -> {i.get('expected', i.get('reason', ''))}" for i in items]


def _values_match(actual: Any, expected: Any) -> bool:
    if expected is None:
        return True
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        tolerance = TOLERANCE_CM if actual < 10 and expected < 10 else TOLERANCE_PT
        return abs(float(actual) - float(expected)) <= tolerance
    return str(actual).lower() == str(expected).lower()


def _paper_size_name(width, height) -> str:
    w, h = sorted([_cm(width), _cm(height)])
    return "A4" if abs(w - 21.0) <= 0.2 and abs(h - 29.7) <= 0.2 else "custom"


def _cm(value) -> Optional[float]:
    return round(float(value.cm), 2) if value is not None else None


def _pt(value) -> Optional[float]:
    return round(float(value.pt), 1) if value is not None else None


def _indent_chars(value) -> Optional[float]:
    return round(float(value.cm) / CM_PER_CHAR, 1) if value is not None else None


def _open_docx(path: Path):
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("缺少 python-docx 依赖") from exc
    return Document(str(path))
