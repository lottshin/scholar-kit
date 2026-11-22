"""Paragraph role detection for Chinese thesis formatting."""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

RolePair = Tuple[object, str]


def classify_paragraph_roles(paragraphs: Iterable[object]) -> List[RolePair]:
    roles: List[RolePair] = []
    in_references = False
    for paragraph in paragraphs:
        text = _normalize(paragraph.text)
        role = _role_from_style(paragraph) or _role_from_text(text)
        if role == "references_heading":
            in_references = True
            roles.append((paragraph, "references_heading"))
            continue
        if in_references and text and not _is_structural_heading(text):
            roles.append((paragraph, "references"))
            continue
        if role in {"level1", "abstract"}:
            in_references = False
        roles.append((paragraph, role or "body"))
    return roles


def style_name_for_role(role: str) -> Optional[str]:
    return {"level1": "Heading 1", "level2": "Heading 2", "level3": "Heading 3"}.get(role)


def _role_from_style(paragraph) -> Optional[str]:
    name = (getattr(paragraph.style, "name", "") or "").lower()
    if "heading 1" in name:
        return "level1"
    if "heading 2" in name:
        return "level2"
    if "heading 3" in name:
        return "level3"
    return None


def _role_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    if re.match(r"^图\s*[\d一二三四五六七八九十]+(?:[-－.]\d+)?\s+", text):
        return "figure_caption"
    if re.match(r"^表\s*[\d一二三四五六七八九十]+(?:[-－.]\d+)?\s+", text):
        return "table_caption"
    if re.match(r"^摘\s*要\s*[:：]?", text):
        return "abstract"
    if re.match(r"^关\s*键\s*词\s*[:：]?", text):
        return "keywords"
    if re.match(r"^(参考文献|参考资料)\s*$", text):
        return "references_heading"
    if re.match(r"^第[一二三四五六七八九十百千0-9]+[章节篇]\s*", text):
        return "level1"
    if re.match(r"^[一二三四五六七八九十]+[、.．]\s*", text):
        return "level1"
    if re.match(r"^\d+\.\d+\.\d+\s+", text):
        return "level3"
    if re.match(r"^\d+\.\d+\s+", text):
        return "level2"
    if re.match(r"^（[一二三四五六七八九十]+）", text):
        return "level2"
    if re.match(r"^\d+[、.．]\s*", text):
        return "level3"
    return None


def _is_structural_heading(text: str) -> bool:
    return (_role_from_text(text) or "") in {"level1", "level2", "level3", "abstract", "keywords"}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())
