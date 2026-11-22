"""Conservative heading numbering for Chinese thesis documents."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Tuple

RolePair = Tuple[object, str]
CN_NUMBERS = "零一二三四五六七八九十"


def apply_heading_numbering(role_pairs: Iterable[RolePair], spec: Dict[str, Any]) -> int:
    rule = spec.get("heading_numbering", {})
    if not rule.get("enabled"):
        return 0
    counters = {"level1": 0, "level2": 0, "level3": 0}
    changed = 0
    for paragraph, role in role_pairs:
        prefix = _next_prefix(role, counters)
        if not prefix:
            continue
        title = _strip_heading_prefix(paragraph.text)
        new_text = f"{prefix} {title}".strip()
        if paragraph.text != new_text:
            _replace_paragraph_text(paragraph, new_text)
            changed += 1
    return changed


def _next_prefix(role: str, counters: Dict[str, int]) -> str:
    if role == "level1":
        counters["level1"] += 1
        counters["level2"] = 0
        counters["level3"] = 0
        return f"第{_to_chinese_number(counters['level1'])}章"
    if role == "level2" and counters["level1"]:
        counters["level2"] += 1
        counters["level3"] = 0
        return f"{counters['level1']}.{counters['level2']}"
    if role == "level3" and counters["level1"] and counters["level2"]:
        counters["level3"] += 1
        return f"{counters['level1']}.{counters['level2']}.{counters['level3']}"
    return ""


def _strip_heading_prefix(text: str) -> str:
    patterns = [
        r"^第[一二三四五六七八九十百千万\d]+[章节篇]\s*",
        r"^\d+(?:\.\d+){0,2}[、.．]?\s*",
        r"^[一二三四五六七八九十]+[、.．]\s*",
        r"^（[一二三四五六七八九十]+）\s*",
    ]
    stripped = text.strip()
    for pattern in patterns:
        stripped = re.sub(pattern, "", stripped)
    return stripped


def _replace_paragraph_text(paragraph, text: str) -> None:
    if not paragraph.runs:
        paragraph.add_run(text)
        return
    paragraph.runs[0].text = text
    for run in paragraph.runs[1:]:
        run.text = ""


def _to_chinese_number(value: int) -> str:
    if value <= 10:
        return CN_NUMBERS[value]
    if value < 20:
        return "十" + CN_NUMBERS[value % 10]
    if value < 100:
        tens, ones = divmod(value, 10)
        return CN_NUMBERS[tens] + "十" + (CN_NUMBERS[ones] if ones else "")
    return str(value)
