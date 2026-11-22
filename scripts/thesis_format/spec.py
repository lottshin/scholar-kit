"""Specification parsing for Chinese thesis formatting."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

CM_PER_CHAR = 0.37
STYLE_MARKERS = [
    "一级标题", "二级标题", "三级标题", "正文", "摘要", "关键词", "参考文献",
    "图题", "图注", "表题", "表注", "脚注", "尾注", "页眉", "页脚",
]


def default_spec() -> Dict[str, Any]:
    return {
        "paper_size": "A4",
        "margins_cm": {"top": 2.5, "bottom": 2.5, "left": 3.0, "right": 2.5},
        "body": {
            "font_name": "宋体",
            "font_size_pt": 12,
            "line_spacing": 1.5,
            "first_line_indent_chars": 2,
        },
        "headings": {
            "level1": {"font_name": "黑体", "font_size_pt": 16, "bold": True, "alignment": "center"},
            "level2": {"font_name": "黑体", "font_size_pt": 14, "bold": True, "alignment": "left"},
            "level3": {"font_name": "黑体", "font_size_pt": 12, "bold": True, "alignment": "left"},
        },
        "abstract": {"font_name": "宋体", "font_size_pt": 12, "line_spacing": 1.5},
        "keywords": {"font_name": "宋体", "font_size_pt": 12, "line_spacing": 1.5},
        "references": {"font_name": "宋体", "font_size_pt": 10.5, "line_spacing": 1.0},
        "captions": {
            "figure": {"font_name": "宋体", "font_size_pt": 10.5, "alignment": "center"},
            "table": {"font_name": "宋体", "font_size_pt": 10.5, "alignment": "center"},
        },
        "footnotes": {"font_name": "宋体", "font_size_pt": 10.5, "line_spacing": 1.0},
        "endnotes": {"font_name": "宋体", "font_size_pt": 10.5, "line_spacing": 1.0},
        "header_text": None,
        "header_separator_line": False,
        "footer_page_numbers": True,
        "heading_numbering": {"enabled": False},
        "source": ["default_cn_thesis"],
    }


def load_spec_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ValueError(f"格式规范文件不存在: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return parse_text_spec(text)
    if not isinstance(data, dict):
        raise ValueError("格式规范 JSON 顶层必须是对象")
    data.setdefault("source", []).append(str(path))
    return data


def parse_text_spec(text: str) -> Dict[str, Any]:
    spec: Dict[str, Any] = {"source": ["text_spec"]}
    margins = _parse_margins(text)
    if margins:
        spec["margins_cm"] = margins
    body = _parse_named_style(text, "正文")
    if body:
        spec["body"] = body
    headings = _parse_heading_styles(text)
    if headings:
        spec["headings"] = headings
    for marker, key in [("摘要", "abstract"), ("关键词", "keywords"), ("参考文献", "references")]:
        if marker not in text:
            continue
        rule = _parse_named_style(text, marker)
        if key == "references":
            hanging = _parse_hanging_indent(_window_after(text, marker))
            if hanging is not None:
                rule["hanging_indent_chars"] = hanging
        if rule:
            spec[key] = rule
    captions = _parse_caption_styles(text)
    if captions:
        spec["captions"] = captions
    for marker, key in [("脚注", "footnotes"), ("尾注", "endnotes")]:
        if marker not in text:
            continue
        rule = _parse_named_style(text, marker)
        if rule:
            spec[key] = rule
    header = _parse_header_text(text)
    if header:
        spec["header_text"] = header
    if _wants_header_separator(text):
        spec["header_separator_line"] = True
    footer = _parse_footer_page_numbers(text)
    if footer is not None:
        spec["footer_page_numbers"] = footer
    numbering = _parse_heading_numbering(text)
    if numbering:
        spec["heading_numbering"] = numbering
    return spec


def deep_update(target: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_update(target[key], value)
        elif key == "source":
            target.setdefault("source", [])
            target["source"].extend(value if isinstance(value, list) else [value])
        else:
            target[key] = deepcopy(value)
    return target


def _parse_margins(text: str) -> Dict[str, float]:
    mapping = {"上": "top", "下": "bottom", "左": "left", "右": "right"}
    result: Dict[str, float] = {}
    for label, key in mapping.items():
        match = re.search(label + r"[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(?:cm|厘米)", text)
        if match:
            result[key] = float(match.group(1))
    return result


def _parse_named_style(text: str, name: str) -> Dict[str, Any]:
    window = _window_after(text, name)
    rule: Dict[str, Any] = {}
    for font_name in ["宋体", "仿宋", "黑体", "楷体", "微软雅黑", "Times New Roman"]:
        if font_name in window:
            rule["font_name"] = font_name
    size = _parse_font_size(window)
    if size:
        rule["font_size_pt"] = size
    spacing_match = re.search(r"(\d+(?:\.\d+)?)\s*倍(?:行距)?", window)
    if spacing_match:
        rule["line_spacing"] = float(spacing_match.group(1))
    elif "一倍半" in window:
        rule["line_spacing"] = 1.5
    elif "单倍" in window:
        rule["line_spacing"] = 1.0
    if "首行缩进" in window:
        indent = _parse_first_line_indent(window)
        if indent is not None:
            rule["first_line_indent_chars"] = indent
    if "居中" in window:
        rule["alignment"] = "center"
    elif "左对齐" in window or "居左" in window:
        rule["alignment"] = "left"
    return rule


def _parse_heading_styles(text: str) -> Dict[str, Dict[str, Any]]:
    headings: Dict[str, Dict[str, Any]] = {}
    for label, key in [("一级标题", "level1"), ("二级标题", "level2"), ("三级标题", "level3")]:
        if label not in text:
            continue
        rule = _parse_named_style(text, label)
        if "居中" in _window_after(text, label):
            rule["alignment"] = "center"
        if rule:
            headings[key] = rule
    return headings


def _parse_font_size(text: str) -> Optional[float]:
    sizes = {
        "小四": 12, "四号": 14, "三号": 16, "小三": 15,
        "二号": 22, "小二": 18, "五号": 10.5, "小五": 9,
    }
    for label, value in sizes.items():
        if label in text:
            return value
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:pt|磅)", text)
    return float(match.group(1)) if match else None


def _parse_first_line_indent(text: str) -> Optional[float]:
    match = re.search(r"首行缩进[^0-9]{0,6}(\d+(?:\.\d+)?)\s*字符", text)
    return float(match.group(1)) if match else None


def _parse_hanging_indent(text: str) -> Optional[float]:
    match = re.search(r"悬挂缩进[^0-9]{0,6}(\d+(?:\.\d+)?)\s*字符", text)
    return float(match.group(1)) if match else None


def _parse_caption_styles(text: str) -> Dict[str, Dict[str, Any]]:
    captions: Dict[str, Dict[str, Any]] = {}
    for labels, key in [(["图题", "图注"], "figure"), (["表题", "表注"], "table")]:
        for label in labels:
            if label not in text:
                continue
            rule = _parse_named_style(text, label)
            if rule:
                captions[key] = rule
                break
    return captions


def _parse_header_text(text: str) -> Optional[str]:
    match = re.search(r"页眉(?:为|[：:])\s*([^\n，；。;]+)", text)
    return match.group(1).strip() if match else None


def _wants_header_separator(text: str) -> bool:
    if "页眉" not in text:
        return False
    header_window = _window_after(text, "页眉", 120)
    return "分割线" in header_window or "下划线" in header_window or "横线" in header_window


def _parse_footer_page_numbers(text: str) -> Optional[bool]:
    footer_window = _window_after(text, "页脚", 120)
    if "页脚" not in footer_window:
        return None
    if "不要页码" in footer_window or "无页码" in footer_window:
        return False
    return True if "页码" in footer_window or "页数" in footer_window else None


def _parse_heading_numbering(text: str) -> Dict[str, Any]:
    if "标题" not in text:
        return {}
    wants = "自动编号" in text or "标题序号" in text or "各级标题序号" in text
    if wants and ("第一章" in text or "1.1" in text):
        return {"enabled": True, "level1": "chapter_cn", "level2": "decimal", "level3": "decimal"}
    return {}


def _window_after(text: str, marker: str, length: int = 80) -> str:
    idx = text.find(marker)
    if idx < 0:
        return text
    end = idx + length
    for other in STYLE_MARKERS:
        if other == marker:
            continue
        pos = text.find(other, idx + len(marker))
        if pos >= 0:
            end = min(end, pos)
    for delimiter in ["。", "；", ";", "\n"]:
        pos = text.find(delimiter, idx + len(marker))
        if pos >= 0:
            end = min(end, pos)
    return text[idx:end]
