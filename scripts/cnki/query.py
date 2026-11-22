"""cnki.query - 知网检索条件模型与检索式校验（纯函数，不触网、不依赖 Selenium）

语法依据：知网官方检索手册 1.2.1 / 1.2.2 / 1.2.5。
本模块只做确定性解析与校验，不判断检索式的学术合理性。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

# 手册 1.2.2.3：最多支持 10 个检索项组合
MAX_CONDITIONS = 10
# 手册 1.2.2.10：检索框输入内容不得超过 120 个字符
MAX_VALUE_LENGTH = 120

ERR_INVALID_EXPRESSION = "CNKI_INVALID_EXPRESSION"
ERR_TOO_MANY_CONDITIONS = "CNKI_TOO_MANY_CONDITIONS"

# 手册 1.2.2.4：高级检索提供 16 个检索项
FIELDS = (
    "主题", "篇关摘", "关键词", "篇名", "全文", "作者", "第一作者", "通讯作者",
    "作者单位", "基金", "摘要", "小标题", "参考文献", "分类号", "文献来源", "DOI",
)

# 兼容既有 --field 取值与 _ADV_FIELD_MAP 的中文别名
FIELD_ALIASES = {
    "来源": "文献来源",
    "期刊": "文献来源",
    "期刊名称": "文献来源",
    "机构": "作者单位",
    "单位": "作者单位",
    "基金资助": "基金",
    "题名": "篇名",
    "第一责任人": "第一作者",
}

LOGIC_VALUES = ("并且", "或者", "不含")
LOGIC_ALIASES = {
    "并且": "并且", "与": "并且", "and": "并且",
    "或者": "或者", "或": "或者", "or": "或者",
    "不含": "不含", "非": "不含", "not": "不含",
}

MATCH_VALUES = ("精确", "模糊")
MATCH_ALIASES = {
    "精确": "精确", "exact": "精确",
    "模糊": "模糊", "fuzzy": "模糊",
}

# 手册 1.2.2.8：主题只提供相关度匹配，没有精确/模糊可选
RELATED_ONLY_FIELDS = frozenset({"主题"})

_OPERATOR_CHARS = ("*", "+", "-")
_QUOTE_CHARS = ("'", '"')
_SPECIAL_NEEDING_QUOTES = set("*+-()/%=")


class CnkiQueryError(ValueError):
    """检索条件或表达式不合法。携带稳定错误码，供 CLI 转为结构化 JSON。"""

    def __init__(self, code: str, message: str, *, condition: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.condition = condition


@dataclass(frozen=True)
class CnkiCondition:
    """一条高级检索条件。logic 对第一条条件无意义（知网首行无逻辑下拉）。"""

    field: str
    value: str
    logic: str = "并且"
    match: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "value": self.value,
            "logic": self.logic,
            "match": self.match,
        }


def canonical_field(name: str) -> str:
    key = (name or "").strip()
    key = FIELD_ALIASES.get(key, key)
    if key not in FIELDS:
        raise CnkiQueryError(
            ERR_INVALID_EXPRESSION,
            f"未知检索项: {name!r}。可用检索项: {'、'.join(FIELDS)}",
        )
    return key


def canonical_logic(name: Optional[str]) -> str:
    if name is None or not str(name).strip():
        return "并且"
    key = str(name).strip()
    value = LOGIC_ALIASES.get(key) or LOGIC_ALIASES.get(key.lower())
    if value is None:
        raise CnkiQueryError(
            ERR_INVALID_EXPRESSION,
            f"未知逻辑关系: {name!r}。可用: 并且/或者/不含（或 AND/OR/NOT）",
        )
    return value


def canonical_match(name: Optional[str]) -> Optional[str]:
    if name is None or not str(name).strip():
        return None
    key = str(name).strip()
    value = MATCH_ALIASES.get(key) or MATCH_ALIASES.get(key.lower())
    if value is None:
        raise CnkiQueryError(
            ERR_INVALID_EXPRESSION,
            f"未知匹配方式: {name!r}。可用: 精确/模糊（或 exact/fuzzy）",
        )
    return value


def quote_term(term: str) -> str:
    """按手册 1.2.2.10：含空格或特殊符号的检索词须用英文半角引号括起。"""
    text = (term or "").strip()
    if not text:
        raise CnkiQueryError(ERR_INVALID_EXPRESSION, "检索词不能为空")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in _QUOTE_CHARS:
        return text
    needs_quote = any(ch.isspace() for ch in text) or any(
        ch in _SPECIAL_NEEDING_QUOTES for ch in text
    )
    if not needs_quote:
        return text
    if "'" in text and '"' in text:
        raise CnkiQueryError(
            ERR_INVALID_EXPRESSION,
            f"检索词同时含单双引号，无法安全引用: {term!r}",
        )
    quote = '"' if "'" in text else "'"
    return f"{quote}{text}{quote}"


def join_terms(terms: Sequence[str], operator: str) -> str:
    """用 `*`/`+`/`-` 组合同字段检索值，保证运算符前后各空一个字节。"""
    if operator not in _OPERATOR_CHARS:
        raise CnkiQueryError(
            ERR_INVALID_EXPRESSION, f"同字段组合运算符只能是 *、+、-，收到: {operator!r}"
        )
    items = [quote_term(t) for t in terms if (t or "").strip()]
    if not items:
        raise CnkiQueryError(ERR_INVALID_EXPRESSION, "同字段组合至少需要一个检索词")
    return f" {operator} ".join(items)


def normalize_value(value: str) -> str:
    """只做安全的空白整理：去掉首尾空白、压缩运算符周围的多余空白。"""
    text = (value or "").strip()
    if not text:
        return text
    return re.sub(r"\s+([*+\-])\s+", r" \1 ", text)


def _scan_unquoted(text: str):
    """逐字符产出 (index, char, prev_char, next_char)，跳过引号内内容。"""
    quote: Optional[str] = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in _QUOTE_CHARS:
            quote = ch
            continue
        prev_ch = text[i - 1] if i > 0 else ""
        next_ch = text[i + 1] if i + 1 < len(text) else ""
        yield i, ch, prev_ch, next_ch


def _is_cjk(ch: str) -> bool:
    return bool(ch) and "\u4e00" <= ch <= "\u9fff"


def has_same_field_operators(value: str) -> bool:
    """检测检索值中是否包含同字段布尔运算符 (* / + / 括号 / 带空格或中文的 -)。"""
    text = (value or "").strip()
    if not text:
        return False
    for _i, ch, prev_ch, next_ch in _scan_unquoted(text):
        if ch in ("*", "+", "(", ")"):
            return True
        if ch == "-" and ((prev_ch.isspace() and next_ch.isspace()) or (_is_cjk(prev_ch) and _is_cjk(next_ch))):
            return True
    return False


def validate_value_syntax(value: str) -> None:
    """校验同字段组合运算的空格与长度，防止把错误检索式静默提交给知网。"""
    if len(value) > MAX_VALUE_LENGTH:
        raise CnkiQueryError(
            ERR_INVALID_EXPRESSION,
            f"单个检索框内容不得超过 {MAX_VALUE_LENGTH} 字符，当前 {len(value)} 字符。"
            "请拆分为多个条件，或改用 --professional / --expr 专业检索。",
        )
    for _i, ch, prev_ch, next_ch in _scan_unquoted(value):
        if ch not in _OPERATOR_CHARS:
            continue
        if ch == "-" and not (_is_cjk(prev_ch) and _is_cjk(next_ch)):
            continue
        if (prev_ch and not prev_ch.isspace()) or (next_ch and not next_ch.isspace()):
            raise CnkiQueryError(
                ERR_INVALID_EXPRESSION,
                f"同字段组合运算符 {ch!r} 前后需各空一个字节: {value!r}。"
                f"若该符号属于检索词本身（如 '2+3'），请用英文半角单引号括起该词。",
            )


def validate_conditions(conditions: Sequence[CnkiCondition]) -> List[CnkiCondition]:
    """校验并返回规范化后的条件列表。"""
    if not conditions:
        raise CnkiQueryError(ERR_INVALID_EXPRESSION, "至少需要一个检索条件")
    if len(conditions) > MAX_CONDITIONS:
        raise CnkiQueryError(
            ERR_TOO_MANY_CONDITIONS,
            f"知网高级检索最多支持 {MAX_CONDITIONS} 个检索项，当前 {len(conditions)} 个。"
            "请合并同字段条件（用 *、+、-），或改用 --professional / --expr 专业检索。",
        )
    normalized: List[CnkiCondition] = []
    for cond in conditions:
        field_name = canonical_field(cond.field)
        logic = canonical_logic(cond.logic)
        match = canonical_match(cond.match)
        if match and field_name in RELATED_ONLY_FIELDS:
            raise CnkiQueryError(
                ERR_INVALID_EXPRESSION,
                "主题检索只提供相关度匹配，不能指定精确/模糊。"
                "请改用篇名/关键词/摘要等检索项，或省略匹配方式。",
                condition=cond.to_dict(),
            )
        value = normalize_value(cond.value)
        if not value:
            raise CnkiQueryError(
                ERR_INVALID_EXPRESSION, "检索词不能为空", condition=cond.to_dict()
            )
        validate_value_syntax(value)
        normalized.append(
            CnkiCondition(field=field_name, value=value, logic=logic, match=match)
        )
    return normalized


def parse_condition(text: str) -> CnkiCondition:
    """解析 `--condition` 参数：支持 `字段|值`、`字段|逻辑|值`、`字段|逻辑|匹配|值`。"""
    raw = (text or "").strip()
    if not raw:
        raise CnkiQueryError(ERR_INVALID_EXPRESSION, "--condition 不能为空")
    parts = raw.split("|", 3)
    if len(parts) == 2:
        field_name, value = parts
        logic, match = "并且", None
    elif len(parts) == 3:
        field_name, logic, value = parts
        match = None
    elif len(parts) == 4:
        field_name, logic, match, value = parts
    else:
        raise CnkiQueryError(
            ERR_INVALID_EXPRESSION,
            f"--condition 格式应为 字段|值、字段|逻辑|值 或 字段|逻辑|匹配|值，收到: {text!r}",
        )
    return validate_conditions(
        [CnkiCondition(field=field_name, value=value, logic=logic, match=match or None)]
    )[0]


def build_legacy_conditions(
    keyword: Optional[str],
    field: Optional[str] = None,
    author: Optional[str] = None,
    journal: Optional[str] = None,
    match: Optional[str] = None,
) -> List[CnkiCondition]:
    """把既有 query/--field/--author/--journal 参数映射为条件模型。"""
    conditions: List[CnkiCondition] = []
    if keyword and keyword.strip():
        conditions.append(
            CnkiCondition(field=field or "主题", value=keyword, match=match)
        )
    if author and author.strip():
        conditions.append(CnkiCondition(field="作者", value=author))
    if journal and journal.strip():
        conditions.append(CnkiCondition(field="文献来源", value=journal))
    return conditions


_PRO_FIELD_PATTERN = re.compile(r"[A-Z]{1,4}\s*(?:%=|=|%)")
_PRO_COMPARE_PATTERN = re.compile(r"\b(?:YE|CF)\b\s*(?:BETWEEN|>=|<=|>|<)", re.I)

_PRO_FIELD_CODES = {
    "主题": "SU", "篇关摘": "TKA", "关键词": "KY", "篇名": "TI", "全文": "FT",
    "作者": "AU", "第一作者": "FI", "通讯作者": "RP", "作者单位": "AF",
    "基金": "FU", "摘要": "AB", "小标题": "CO", "参考文献": "RF",
    "分类号": "CLC", "文献来源": "JN", "DOI": "DOI",
}


def _pro_match_operator(field_name: str, match: Optional[str]) -> str:
    """主题只能用相关匹配 %=；未指定匹配方式时默认模糊，避免完全一致导致 0 命中。"""
    if field_name in RELATED_ONLY_FIELDS:
        return "%="
    return "=" if match == "精确" else "%"


def _pro_value(value: str) -> str:
    """按手册 1.2.5 处理检索值：复合运算用括号，含空格的词组用单引号。"""
    text = (value or "").strip()
    if not text:
        raise CnkiQueryError(ERR_INVALID_EXPRESSION, "检索词不能为空")
    if has_same_field_operators(text):
        return text
    if any(ch.isspace() for ch in text):
        return "'" + text.replace("'", "") + "'"
    return text


def compile_conditions_to_expression(conditions: Sequence[CnkiCondition]) -> str:
    """把结构化条件编译为知网专业检索表达式（对同优先级 AND/OR/NOT 自动加括号保护）。"""
    validated = validate_conditions(conditions)

    def render(cond: CnkiCondition) -> str:
        code = _PRO_FIELD_CODES[cond.field]
        operator = _pro_match_operator(cond.field, cond.match)
        return f"{code} {operator} {_pro_value(cond.value)}"

    segments: List[str] = [render(validated[0])]
    for cond in validated[1:]:
        current = render(cond)
        if cond.logic == "并且":
            segments.append(current)
        elif cond.logic == "或者":
            segments[-1] = f"({segments[-1]} OR {current})"
        else:
            segments[-1] = f"({segments[-1]} NOT {current})"
    expression = " AND ".join(segments)
    return validate_professional_expression(expression)


def validate_professional_expression(expression: str) -> str:
    """校验专业检索表达式的基本形态，不改写表达式语义。"""
    text = (expression or "").strip()
    if not text:
        raise CnkiQueryError(ERR_INVALID_EXPRESSION, "专业检索表达式不能为空")
    if "\n" in text or "\r" in text:
        raise CnkiQueryError(
            ERR_INVALID_EXPRESSION, "专业检索表达式必须为单行，请去掉换行"
        )
    if text.count("(") != text.count(")"):
        raise CnkiQueryError(
            ERR_INVALID_EXPRESSION, "专业检索表达式的英文圆括号不配对"
        )
    if text.count("'") % 2:
        raise CnkiQueryError(
            ERR_INVALID_EXPRESSION, "专业检索表达式的英文单引号不配对"
        )
    if not (_PRO_FIELD_PATTERN.search(text) or _PRO_COMPARE_PATTERN.search(text)):
        raise CnkiQueryError(
            ERR_INVALID_EXPRESSION,
            "专业检索表达式需包含字段代码与匹配运算符，例如 SU %= 知识管理 "
            "或 KY = 数据挖掘 AND AU = 邱均平",
        )
    return text
