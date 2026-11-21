"""
research_tools.py - 科研执行中后段工具集（实证数据三线表/LaTeX制表、投稿目标选刊匹配、审稿意见逐条返修闭环）
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from docx_tools import _write_docx_from_markdown, cmd_read_paper
from evidence import _load_session, _session_project
from venues import annotate_venue_quality


def _read_docx(filepath: Path, numbered: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    cmd_read_paper(
        argparse.Namespace(filepath=str(filepath), numbered=numbered, toc=False, section=None, max_chars=None, output=None, raw=False),
        output_fn=out.update,
    )
    return out


def _default_output(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ── 1. 纯标准库高精度 Student-t 双尾 p 值计算（零 scipy 依赖） ─────────────

def _betacf(a: float, b: float, x: float) -> float:
    max_iter = 200
    eps = 3e-12
    fpmin = 1e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_two_tailed_p(t_stat: float, df: int) -> float:
    """计算自由度 df 下的双尾 Student-t 分布 p 值（纯标准库实现，精度优于 1e-10）。"""
    if df <= 0 or math.isnan(t_stat):
        return 1.0
    if math.isinf(t_stat):
        return 0.0
    x = float(df) / (float(df) + t_stat * t_stat)
    return max(0.0, min(1.0, _regularized_incomplete_beta(0.5 * df, 0.5, x)))


def _sig_stars(p_val: Optional[float]) -> str:
    if p_val is None:
        return ""
    if p_val < 0.01:
        return "***"
    if p_val < 0.05:
        return "**"
    if p_val < 0.10:
        return "*"
    return ""


# ── 2. 实证/实验数据转出版级三线表与 LaTeX booktabs (`data-table`) ─────────

_LOWER_IS_BETTER_RE = re.compile(
    r"(?:\bloss\b|\bmae\b|\bmse\b|\brmse\b|\bmape\b|\bwer\b|\bcer\b|\bperplexity\b|\bppl\b|\bfid\b|"
    r"\berr(?:or)?\b|\blatency\b|误差|错误率|时延|耗时|困惑度)",
    re.IGNORECASE,
)


def _load_tabular_input(filepath: Path) -> Tuple[str, Any]:
    if not filepath.exists():
        raise FileNotFoundError(f"数据文件不存在: {filepath}")
    suffix = filepath.suffix.lower()
    raw_text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            raw_text = filepath.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if raw_text is None:
        raise ValueError(f"无法解码文件: {filepath}")

    if suffix == ".json":
        obj = json.loads(raw_text)
        if isinstance(obj, dict) and isinstance(obj.get("models"), list):
            return "regression_json", obj
        if isinstance(obj, list) and all(isinstance(r, dict) for r in obj):
            return "rows", obj
        raise ValueError("JSON 数据须为行对象数组 `[{\"x\": 1, ...}]` 或回归模型结构 `{\"models\": [...]}`")

    delim = "\t" if suffix == ".tsv" else ","
    reader = csv.DictReader(raw_text.splitlines(), delimiter=delim)
    rows = [dict(r) for r in reader if any(str(v or "").strip() for v in r.values())]
    return "rows", rows


def _extract_numeric_columns(rows: List[Dict[str, Any]], requested_cols: Optional[List[str]] = None) -> Dict[str, List[Optional[float]]]:
    if not rows:
        return {}
    candidate_cols = requested_cols or list(rows[0].keys())
    numeric_map: Dict[str, List[Optional[float]]] = {}
    for col in candidate_cols:
        vals: List[Optional[float]] = []
        valid_cnt = 0
        non_empty_cnt = 0
        for r in rows:
            s = str(r.get(col, "") if r.get(col) is not None else "").strip().replace(",", "")
            if s == "" or s.lower() in ("na", "nan", "null", "none", "."):
                vals.append(None)
                continue
            non_empty_cnt += 1
            try:
                v = float(s)
                if math.isfinite(v):
                    vals.append(v)
                    valid_cnt += 1
                else:
                    vals.append(None)
            except ValueError:
                vals.append(None)
        if non_empty_cnt > 0 and valid_cnt / non_empty_cnt >= 0.7:
            numeric_map[col] = vals
    return numeric_map


def _escape_latex(text: str) -> str:
    s = str(text)
    repl = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
    }
    out = "".join(repl.get(ch, ch) for ch in s)
    # 恢复加粗与下划线标记（用于对比表最优/次优指标）
    out = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", out)
    out = re.sub(r"<u>(.+?)</u>", r"\\underline{\1}", out)
    return out


def _render_markdown_table(headers: List[str], rows: List[List[str]], caption: str = "", note: str = "") -> str:
    lines: List[str] = []
    if caption:
        lines.extend([caption, ""])
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] + ["---:" for _ in headers[1:]]) + " |")
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def _render_booktabs_latex(headers: List[str], rows: List[List[str]], caption: str = "", note: str = "") -> str:
    col_spec = "l" + "r" * max(0, len(headers) - 1)
    lines = [
        r"\begin{table}[htbp]",
        r"  \centering",
    ]
    if caption:
        lines.append(f"  \\caption{{{_escape_latex(caption)}}}")
    lines.append(f"  \\begin{{tabular}}{{{col_spec}}}")
    lines.append(r"    \toprule")
    lines.append("    " + " & ".join(_escape_latex(h) for h in headers) + r" \\")
    lines.append(r"    \midrule")
    for r in rows:
        lines.append("    " + " & ".join(_escape_latex(c) for c in r) + r" \\")
    lines.append(r"    \bottomrule")
    lines.append(r"  \end{tabular}")
    if note:
        lines.append(f"  \\\\[4pt]\\footnotesize{{{_escape_latex(note)}}}")
    lines.append(r"\end{table}")
    return "\n".join(lines)


def build_summary_table(
    rows: List[Dict[str, Any]],
    *,
    cols: Optional[List[str]] = None,
    digits: int = 3,
    lang: str = "zh",
    caption: str = "",
) -> Dict[str, Any]:
    num_map = _extract_numeric_columns(rows, cols)
    if not num_map:
        raise ValueError("未检测到可统计的数值列")

    headers = (
        ["变量", "样本量 (N)", "均值 (Mean)", "标准差 (SD)", "最小值 (Min)", "中位数 (Median)", "最大值 (Max)"]
        if lang == "zh"
        else ["Variable", "N", "Mean", "SD", "Min", "Median", "Max"]
    )
    cap = caption or ("表1 主要变量描述性统计" if lang == "zh" else "Table 1 Descriptive Statistics")
    table_rows: List[List[str]] = []
    stats_json: List[Dict[str, Any]] = []
    fmt = f"{{:.{digits}f}}"

    for var_name, series in num_map.items():
        clean = [v for v in series if v is not None]
        if not clean:
            continue
        n = len(clean)
        mean_v = statistics.mean(clean)
        sd_v = statistics.stdev(clean) if n >= 2 else 0.0
        min_v = min(clean)
        med_v = statistics.median(clean)
        max_v = max(clean)
        table_rows.append([
            var_name,
            str(n),
            fmt.format(mean_v),
            fmt.format(sd_v),
            fmt.format(min_v),
            fmt.format(med_v),
            fmt.format(max_v),
        ])
        stats_json.append({
            "variable": var_name,
            "n": n,
            "mean": round(mean_v, digits + 2),
            "sd": round(sd_v, digits + 2),
            "min": round(min_v, digits + 2),
            "median": round(med_v, digits + 2),
            "max": round(max_v, digits + 2),
        })

    return {
        "table_type": "summary",
        "headers": headers,
        "rows": table_rows,
        "caption": cap,
        "note": "",
        "stats": stats_json,
    }


def build_correlation_table(
    rows: List[Dict[str, Any]],
    *,
    cols: Optional[List[str]] = None,
    digits: int = 3,
    lang: str = "zh",
    caption: str = "",
) -> Dict[str, Any]:
    num_map = _extract_numeric_columns(rows, cols)
    var_names = list(num_map.keys())
    if len(var_names) < 2:
        raise ValueError("相关系数矩阵至少需要 2 个数值变量")

    headers = ([("变量" if lang == "zh" else "Variable")] + [f"({i})" for i in range(1, len(var_names) + 1)])
    cap = caption or ("表2 主要变量 Pearson 相关系数矩阵" if lang == "zh" else "Table 2 Pearson Correlation Matrix")
    note = (
        "注：***、**、* 分别表示在 1%、5%、10% 统计水平上显著（双尾检验）。"
        if lang == "zh"
        else "Note: ***, **, and * denote two-tailed significance at the 1%, 5%, and 10% levels, respectively."
    )
    fmt = f"{{:.{digits}f}}"
    table_rows: List[List[str]] = []
    pairs_json: List[Dict[str, Any]] = []

    for i, vi in enumerate(var_names):
        row_cells = [f"({i + 1}) {vi}"]
        for j, vj in enumerate(var_names):
            if j > i:
                row_cells.append("")
                continue
            if i == j:
                row_cells.append("1.000" if digits == 3 else fmt.format(1.0))
                continue
            paired = [
                (a, b) for a, b in zip(num_map[vi], num_map[vj])
                if a is not None and b is not None
            ]
            n = len(paired)
            if n < 3:
                row_cells.append("-")
                continue
            xs = [p[0] for p in paired]
            ys = [p[1] for p in paired]
            mx, my = statistics.mean(xs), statistics.mean(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            syy = sum((y - my) ** 2 for y in ys)
            if sxx <= 0 or syy <= 0:
                row_cells.append("0.000")
                continue
            r_val = sum((x - mx) * (y - my) for x, y in paired) / math.sqrt(sxx * syy)
            r_val = max(-1.0, min(1.0, r_val))
            if abs(r_val) >= 1.0 - 1e-12:
                p_val = 0.0
            else:
                t_stat = r_val * math.sqrt((n - 2) / max(1e-15, 1.0 - r_val * r_val))
                p_val = student_t_two_tailed_p(t_stat, n - 2)
            stars = _sig_stars(p_val)
            row_cells.append(f"{fmt.format(r_val)}{stars}")
            pairs_json.append({"var1": vi, "var2": vj, "n": n, "r": round(r_val, 4), "p": round(p_val, 5), "stars": stars})
        table_rows.append(row_cells)

    return {
        "table_type": "corr",
        "headers": headers,
        "rows": table_rows,
        "caption": cap,
        "note": note,
        "pairs": pairs_json,
    }


def build_regression_table(spec: Dict[str, Any], *, digits: int = 4, lang: str = "zh", caption: str = "") -> Dict[str, Any]:
    models = spec.get("models") or []
    if not models:
        raise ValueError("回归规格缺少 models 列表")
    model_names = [str(m.get("name") or f"({i + 1})") for i, m in enumerate(models)]
    headers = [("变量" if lang == "zh" else "Variables")] + model_names

    var_order: List[str] = []
    control_order: List[str] = []
    for m in models:
        for v in (m.get("coefs") or {}).keys():
            if v not in var_order:
                var_order.append(v)
        for c in (m.get("controls") or {}).keys():
            if c not in control_order:
                control_order.append(c)

    fmt = f"{{:.{digits}f}}"
    use_t = any(
        "t" in info and "se" not in info
        for m in models for info in (m.get("coefs") or {}).values() if isinstance(info, dict)
    )
    table_rows: List[List[str]] = []

    for var in var_order:
        coef_row = [var]
        sub_row = [""]
        for m in models:
            info = (m.get("coefs") or {}).get(var)
            if not isinstance(info, dict) or info.get("coef") is None:
                coef_row.append("")
                sub_row.append("")
                continue
            c_val = float(info["coef"])
            p_val = float(info["p"]) if info.get("p") is not None else None
            if p_val is None and info.get("se") and float(info["se"]) > 0:
                t_auto = c_val / float(info["se"])
                p_val = student_t_two_tailed_p(t_auto, int(m.get("n") or 1000) - 2)
            elif p_val is None and info.get("t") is not None:
                p_val = student_t_two_tailed_p(float(info["t"]), int(m.get("n") or 1000) - 2)
            stars = info.get("stars") if info.get("stars") is not None else _sig_stars(p_val)
            coef_row.append(f"{fmt.format(c_val)}{stars}")
            if info.get("se") is not None:
                sub_row.append(f"({fmt.format(float(info['se']))})")
            elif info.get("t") is not None:
                sub_row.append(f"[{fmt.format(float(info['t']))}]")
            else:
                sub_row.append("")
        table_rows.append(coef_row)
        if any(sub_row[1:]):
            table_rows.append(sub_row)

    for ctrl in control_order:
        table_rows.append([ctrl] + [str((m.get("controls") or {}).get(ctrl, "")) for m in models])

    if any(m.get("n") is not None for m in models):
        n_label = "观测值 (N)" if lang == "zh" else "Observations"
        table_rows.append([n_label] + [str(m.get("n", "")) for m in models])
    if any(m.get("r2") is not None for m in models):
        r2_label = "R² / Adj. R²"
        table_rows.append([r2_label] + [fmt.format(float(m["r2"])) if m.get("r2") is not None else "" for m in models])

    paren_desc = "括号内为中括号 t 统计量" if use_t else "圆括号内为稳健标准误"
    note = spec.get("note") or (
        f"注：{paren_desc}；***、**、* 分别表示在 1%、5%、10% 水平上显著。"
        if lang == "zh"
        else "Note: Robust standard errors in parentheses; ***, **, and * indicate significance at the 1%, 5%, and 10% levels."
    )
    cap = caption or spec.get("caption") or ("表3 回归估计结果" if lang == "zh" else "Table 3 Regression Results")
    return {
        "table_type": "regression",
        "headers": headers,
        "rows": table_rows,
        "caption": cap,
        "note": note,
    }


def build_comparison_table(
    rows: List[Dict[str, Any]],
    *,
    cols: Optional[List[str]] = None,
    digits: int = 2,
    lang: str = "zh",
    caption: str = "",
) -> Dict[str, Any]:
    """理工/AI 实验对比与消融表：自动识别模型/方法名首列，并对各指标列自动加粗最优值、下划线次优值。"""
    if not rows:
        raise ValueError("对比表数据为空")
    all_cols = cols or list(rows[0].keys())
    label_col = all_cols[0]
    metric_cols = all_cols[1:]
    num_map = _extract_numeric_columns(rows, metric_cols)

    best_idx_map: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    for mc, vals in num_map.items():
        clean_unique = sorted({v for v in vals if v is not None}, reverse=not bool(_LOWER_IS_BETTER_RE.search(mc)))
        best_v = clean_unique[0] if clean_unique else None
        second_v = clean_unique[1] if len(clean_unique) >= 2 else None
        best_idx_map[mc] = (best_v, second_v)

    fmt = f"{{:.{digits}f}}"
    headers = [label_col] + [
        f"{mc} (↓)" if (mc in num_map and _LOWER_IS_BETTER_RE.search(mc)) else (f"{mc} (↑)" if mc in num_map else mc)
        for mc in metric_cols
    ]
    table_rows: List[List[str]] = []
    for r_idx, r in enumerate(rows):
        cells = [str(r.get(label_col, ""))]
        for mc in metric_cols:
            if mc not in num_map or num_map[mc][r_idx] is None:
                cells.append(str(r.get(mc, "")))
                continue
            val = num_map[mc][r_idx]
            assert val is not None
            txt = fmt.format(val)
            best_v, second_v = best_idx_map.get(mc, (None, None))
            if best_v is not None and abs(val - best_v) < 1e-12:
                txt = f"**{txt}**"
            elif second_v is not None and abs(val - second_v) < 1e-12:
                txt = f"<u>{txt}</u>"
            cells.append(txt)
        table_rows.append(cells)

    cap = caption or ("表4 实验对比与消融结果" if lang == "zh" else "Table 4 Quantitative Comparison Results")
    note = (
        "注：加粗表示该指标最优结果，下划线表示次优结果。"
        if lang == "zh"
        else "Note: Bold indicates the best result and underline indicates the second-best result."
    )
    return {
        "table_type": "comparison",
        "headers": headers,
        "rows": table_rows,
        "caption": cap,
        "note": note,
    }


def cmd_data_table(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    try:
        kind, data = _load_tabular_input(Path(args.filepath))
    except Exception as e:
        output_fn({"status": "error", "code": "DATA_LOAD_ERROR", "message": str(e)})
        return

    req_type = getattr(args, "type", "auto") or "auto"
    cols = [c.strip() for c in str(args.columns).split(",") if c.strip()] if getattr(args, "columns", None) else None
    lang = getattr(args, "lang", "zh") or "zh"
    digits = getattr(args, "digits", None)
    caption = getattr(args, "caption", "") or ""

    try:
        if kind == "regression_json" or req_type == "regression":
            if kind != "regression_json":
                raise ValueError("--type regression 需要传入包含 `{\"models\": [...]}` 结构的 JSON 文件")
            built = build_regression_table(data, digits=digits if digits is not None else 4, lang=lang, caption=caption)
        else:
            effective_type = "summary" if req_type == "auto" else req_type
            if effective_type == "summary":
                built = build_summary_table(data, cols=cols, digits=digits if digits is not None else 3, lang=lang, caption=caption)
            elif effective_type == "corr":
                built = build_correlation_table(data, cols=cols, digits=digits if digits is not None else 3, lang=lang, caption=caption)
            elif effective_type == "comparison":
                built = build_comparison_table(data, cols=cols, digits=digits if digits is not None else 2, lang=lang, caption=caption)
            else:
                raise ValueError(f"不支持的表格类型: {effective_type}")
    except Exception as e:
        output_fn({"status": "error", "code": "TABLE_BUILD_ERROR", "message": str(e)})
        return

    md_text = _render_markdown_table(built["headers"], built["rows"], built["caption"], built["note"])
    tex_text = _render_booktabs_latex(built["headers"], built["rows"], built["caption"], built["note"])
    out_fmt = getattr(args, "format", "markdown") or "markdown"
    out_path = Path(args.output) if getattr(args, "output", None) else None

    if out_fmt == "docx":
        target_docx = out_path or Path("academic_table.docx")
        docx_md = re.sub(r"<u>(.+?)</u>", r"\1", md_text)
        docx_res = _write_docx_from_markdown(docx_md, target_docx)
        if docx_res.get("status") == "error":
            output_fn(docx_res)
            return
        output_fn({
            **docx_res,
            "table_type": built["table_type"],
            "markdown": md_text,
            "latex": tex_text,
        })
        return

    rendered = tex_text if out_fmt == "latex" else md_text
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")

    if getattr(args, "raw", False):
        print(rendered)
    else:
        output_fn({
            "status": "success",
            "table_type": built["table_type"],
            "format": out_fmt,
            "output_file": str(out_path) if out_path else None,
            "caption": built["caption"],
            "row_count": len(built["rows"]),
            "markdown": md_text,
            "latex": tex_text,
        })


# ── 3. 目标投稿期刊智能匹配与预警避坑 (`match-journal`) ─────────────────────

_CN_FLAGSHIP_JOURNALS = {
    "中国社会科学", "经济研究", "管理世界", "社会学研究", "哲学研究", "法学研究", "中国法学",
    "文学评论", "历史研究", "教育研究", "新闻与传播研究", "政治学研究", "世界经济", "经济学(季刊)",
    "金融研究", "中国工业经济", "数量经济技术经济研究", "管理科学学报", "心理学报", "计算机学报",
    "软件学报", "自动化学报", "中国科学:信息科学", "中华医学杂志",
}

_EN_FLAGSHIP_JOURNALS = {
    "nature", "science", "cell", "proceedings of the national academy of sciences",
    "new england journal of medicine", "lancet", "jama", "bmj",
    "american economic review", "quarterly journal of economics", "journal of political economy",
    "econometrica", "review of economic studies", "journal of finance",
    "journal of financial economics", "review of financial studies",
    "academy of management journal", "academy of management review",
    "strategic management journal", "administrative science quarterly",
    "mis quarterly", "information systems research", "management science",
    "operations research", "marketing science", "journal of marketing",
    "journal of consumer research", "journal of international business studies",
    "journal of accounting and economics", "journal of accounting research", "accounting review",
    "american sociological review", "american journal of sociology",
    "american political science review", "research policy",
    "ieee transactions on pattern analysis and machine intelligence",
    "journal of machine learning research", "communications of the acm",
}


def _extract_papers_from_manuscript_refs(filepath: Path) -> List[Dict[str, Any]]:
    from literature import _extract_bibliography_from_doc_text, _read_any_doc_text
    doc_txt = _read_any_doc_text(filepath)
    return [r for r in _extract_bibliography_from_doc_text(doc_txt) if r.get("journal")]


def cmd_match_journal(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    project = _session_project(args)
    papers: List[Dict[str, Any]] = []
    if getattr(args, "from_file", None):
        fpath = Path(args.from_file)
        if not fpath.exists():
            output_fn({"status": "error", "code": "FILE_NOT_FOUND", "message": f"文稿不存在: {fpath}"})
            return
        papers.extend(_extract_papers_from_manuscript_refs(fpath))
    papers.extend(_load_session(project))

    topic = getattr(args, "topic", None) or project or ""
    if getattr(args, "live", False) and topic:
        try:
            from search import search_openalex
            live_en = search_openalex(topic, limit=25, sort="citations", year_from=2021)
            if isinstance(live_en, list):
                papers.extend(p for p in live_en if isinstance(p, dict) and p.get("journal"))
        except Exception:
            pass

    if not papers:
        output_fn({
            "status": "error",
            "code": "NO_CANDIDATE_PAPERS",
            "message": "未找到可用于匹配母刊的文献记录，请先建立课题库、传入 --from-file <文稿>，或叠加 --topic <方向> --live",
        })
        return

    venue_groups: Dict[str, Dict[str, Any]] = {}
    for p in papers:
        j_raw = str(p.get("journal") or "").strip().rstrip(".。")
        if not j_raw or len(j_raw) < 2:
            continue
        # 过滤学位论文授予高校名称与预印本平台
        if re.search(r"(?:大学|学院|研究院|arXiv|SSRN|bioRxiv|medRxiv|Research Square)", j_raw, re.I):
            continue
        enriched_p = annotate_venue_quality(dict(p))
        key = j_raw.lower()
        grp = venue_groups.setdefault(key, {
            "journal": j_raw,
            "cited_in_project_count": 0,
            "sample_titles": [],
            "tags": set(),
            "is_core": False,
            "is_warning": False,
            "warning_detail": None,
            "warning_history": None,
            "is_cjk": any("\u4e00" <= c <= "\u9fff" for c in j_raw),
            "avg_citations": [],
        })
        grp["cited_in_project_count"] += 1
        t = str(enriched_p.get("title") or "").strip()
        if t and t not in grp["sample_titles"] and len(grp["sample_titles"]) < 3:
            grp["sample_titles"].append(t)
        for c_tag in str(enriched_p.get("core_type") or "").replace(";", ",").split(","):
            if c_tag.strip():
                grp["tags"].add(c_tag.strip())
        for v_tag in (enriched_p.get("venue_tags") or []):
            vt = str(v_tag).strip()
            if vt and not vt.startswith("CAS预警"):
                grp["tags"].add(vt)
        if enriched_p.get("is_warning"):
            grp["is_warning"] = True
            grp["warning_detail"] = enriched_p.get("cas_warning")
        else:
            if enriched_p.get("cas_warning_history"):
                grp["warning_history"] = enriched_p.get("cas_warning_history")
            if enriched_p.get("is_core") or grp["tags"]:
                grp["is_core"] = True
        if enriched_p.get("cited_by") is not None:
            try:
                grp["avg_citations"].append(int(enriched_p["cited_by"]))
            except (TypeError, ValueError):
                pass

    lang_pref = getattr(args, "lang", "all") or "all"
    recommendations: List[Dict[str, Any]] = []
    excluded_warnings: List[Dict[str, Any]] = []

    for grp in venue_groups.values():
        if lang_pref == "zh" and not grp["is_cjk"]:
            continue
        if lang_pref == "en" and grp["is_cjk"]:
            continue
        tags_sorted = sorted(grp["tags"])
        avg_cite = round(statistics.mean(grp["avg_citations"]), 1) if grp["avg_citations"] else 0.0
        item = {
            "journal": grp["journal"],
            "language": "zh" if grp["is_cjk"] else "en",
            "project_frequency": grp["cited_in_project_count"],
            "indexing": tags_sorted,
            "avg_paper_citations": avg_cite,
            "sample_papers": grp["sample_titles"],
        }
        if grp["is_warning"]:
            item["cas_warning"] = grp["warning_detail"]
            excluded_warnings.append(item)
            continue
        if grp["warning_history"]:
            item["cas_warning_history"] = grp["warning_history"]

        norm_en_j = re.sub(r"^the\s+", "", re.sub(r"[^a-z0-9]+", " ", grp["journal"].lower().replace("&", " and ")).strip())
        is_flagship = (
            not grp["warning_history"]
            and (
                grp["journal"] in _CN_FLAGSHIP_JOURNALS
                or norm_en_j in _EN_FLAGSHIP_JOURNALS
                or "CCF-A" in grp["tags"]
            )
        )
        if is_flagship:
            item["tier"] = "tier_1_flagship"
            item["tier_label"] = "冲刺顶刊/顶会"
        elif grp["is_core"] and not grp["warning_history"] and (
            grp["cited_in_project_count"] >= 2 or {"CSSCI", "北大核心", "CSCD", "SCI", "SSCI", "AHCI", "CCF-B"} & grp["tags"]
        ):
            item["tier"] = "tier_2_mainstream_core"
            item["tier_label"] = "主投高契合核心"
        elif grp["is_core"]:
            item["tier"] = "tier_3_broad_core"
            item["tier_label"] = "稳健拓展核心（留意历史预警记录）" if grp["warning_history"] else "稳健拓展核心"
        else:
            item["tier"] = "tier_4_general"
            item["tier_label"] = "普通学术期刊（待核实核心目录）"

        tier_rank = {"tier_1_flagship": 0, "tier_2_mainstream_core": 1, "tier_3_broad_core": 2, "tier_4_general": 3}[item["tier"]]
        item["_sort_key"] = (tier_rank, -grp["cited_in_project_count"], -avg_cite)
        recommendations.append(item)

    recommendations.sort(key=lambda x: x.pop("_sort_key"))
    top_k = getattr(args, "limit", 12) or 12
    recommendations = recommendations[:top_k]

    md_rows = [
        [
            r["tier_label"],
            r["journal"],
            ", ".join(r["indexing"]) or "待核对",
            str(r["project_frequency"]),
            r["sample_papers"][0][:38] if r["sample_papers"] else "-",
        ]
        for r in recommendations
    ]
    md = _render_markdown_table(
        ["投稿梯度", "候选期刊", "收录体系", "课题引文频次", "同源代表文献"],
        md_rows,
        caption=f"目标投稿期刊匹配建议（共筛查 {len(venue_groups)} 本母刊，自动拦截预警期刊 {len(excluded_warnings)} 本）",
    )

    if getattr(args, "raw", False):
        print(md)
    else:
        output_fn({
            "status": "success",
            "project": project,
            "topic": topic or None,
            "total_venues_analyzed": len(venue_groups),
            "recommended_count": len(recommendations),
            "excluded_warning_count": len(excluded_warnings),
            "recommendations": recommendations,
            "excluded_warning_venues": excluded_warnings,
            "markdown": md,
        })


# ── 4. 审稿意见拆解与点对点返修闭环 (`rebuttal`) ──────────────────────────

_REVIEWER_HEADER_RE = re.compile(
    r"^(?:#+\s*)?(?:"
    r"Reviewer\s*#?\s*(\d+|[A-Z])|"
    r"Referee\s*#?\s*(\d+|[A-Z])|"
    r"审稿(?:专家|人)\s*([一二三四五六1-9A-Z])|"
    r"外审专家\s*([一二三四五六1-9A-Z])|"
    r"(Associate\s+Editor|Editor(?:-in-Chief)?|编辑部|主编|责编)"
    r")\s*[:：\-—]?\s*(.*)$",
    re.IGNORECASE,
)

_COMMENT_ITEM_START_RE = re.compile(
    r"^(?:"
    r"(?:Major|Minor)?\s*(?:Comment|Point|Question|Issue)\s*#?\s*\d+\s*[:：.)]|"
    r"(?:问题|意见|建议)\s*[一二三四五六七八九十\d]+\s*[、:：.)]|"
    r"[（(]\s*[一二三四五六七八九十\d]+\s*[)）]\s*|"
    r"\d{1,2}\s*[.、)）]\s+(?=\S)|"
    r"[-*•]\s+(?=\S)"
    r")",
    re.IGNORECASE,
)


def _classify_reviewer_comment(text: str) -> Tuple[str, str, str]:
    low = text.lower()
    if any(k in low for k in (
        "内生性", "工具变量", "稳健性", "平行趋势", "安慰剂", "样本选择", "识别", "模型设定",
        "endogeneity", "instrumental", "robustness", "identification", "confounder", "selection bias", "p-value",
    )):
        return (
            "methodology_robustness",
            "方法识别与稳健性检验",
            "补充稳健性/内生性检验结果，可用 `data-table --type regression` 生成标准补充检验三线表并附于正文或附录",
        )
    if any(k in low for k in (
        "实验", "消融", "对比基线", "数据集", "样本量", "统计表",
        "ablation", "baseline", "benchmark", "dataset", "experiment", "hyperparameter",
    )):
        return (
            "experiment_data",
            "补充实验/数据与基线对比",
            "补充实验数据并用 `data-table --type comparison` 或 `summary` 生成对比矩阵",
        )
    if any(k in low for k in (
        "文献", "综述", "引用", "近三年", "前沿", "边际贡献", "创新点",
        "literature", "related work", "cite", "citation", "missing reference", "novelty", "contribution",
    )):
        return (
            "literature_positioning",
            "文献对话与边际贡献界定",
            "用 `search --core ... --year-from 2024` 补检最新权威文献，经 `verify-refs` 核验后补入综述与引言",
        )
    if any(k in low for k in (
        "机制", "理论", "假说", "渠道", "中介", "调节", "异质性", "为什么",
        "mechanism", "theoretical", "hypothesis", "channel", "mediating", "heterogeneity",
    )):
        return (
            "theory_mechanism",
            "理论推演与作用机制深化",
            "在理论分析与机制检验章节细化因果链条，运行 `validate --file` 确认新增论述与引文极性一致",
        )
    return (
        "clarity_formatting",
        "学术表达、结构与排版规范",
        "结合 `references/style-audit.md` 精修段落表述，用 `patch-docx --dry-run` 预检锚点后无损回写文稿",
    )


def parse_reviewer_comments(raw_text: str) -> List[Dict[str, Any]]:
    """将非结构化的审稿邮件/审稿意见文本拆解为带唯一编号 (ED-C1, R1-C1, R2-C1...) 的结构化条目列表。"""
    current_role = "R1"
    current_label = "审稿专家 1 (Reviewer #1)"
    role_counts: Counter[str] = Counter()
    items: List[Dict[str, Any]] = []
    buf: List[str] = []

    def _flush_buf() -> None:
        if not buf:
            return
        merged = " ".join(s.strip() for s in buf if s.strip()).strip()
        buf.clear()
        if len(merged) < 12:
            return
        # 过滤纯寒暄或审稿总结客套导语（无具体问题或建议动词）
        if len(merged) < 120 and re.search(
            r"^(?:This paper (?:studies|investigates|examines)|Thank you for|I have read|本文(?:主要)?研究|感谢作者)",
            merged,
            re.I,
        ) and not any(q in merged for q in ("?", "？", "however", "建议", "需要", "不足", "补充", "clarify", "should")):
            return
        cleaned = _COMMENT_ITEM_START_RE.sub("", merged).strip()
        if len(cleaned) < 8:
            return
        role_counts[current_role] += 1
        cid = f"{current_role}-C{role_counts[current_role]}"
        c_type, c_label, c_tool_hint = _classify_reviewer_comment(cleaned)
        items.append({
            "id": cid,
            "reviewer_code": current_role,
            "reviewer": current_label,
            "category": c_type,
            "category_label": c_label,
            "comment": cleaned,
            "recommended_action": c_tool_hint,
        })

    cn_num_map = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6"}
    for raw_line in raw_text.splitlines():
        s = raw_line.strip()
        if not s:
            _flush_buf()
            continue
        m_hdr = _REVIEWER_HEADER_RE.match(s)
        if m_hdr:
            _flush_buf()
            ed = m_hdr.group(5)
            rev_id = m_hdr.group(1) or m_hdr.group(2) or m_hdr.group(3) or m_hdr.group(4)
            if ed:
                current_role = "ED"
                current_label = "编辑部 / 主编 (Editor)"
            else:
                norm_id = cn_num_map.get(str(rev_id), str(rev_id).upper())
                current_role = f"R{norm_id}"
                current_label = f"审稿专家 {norm_id} (Reviewer #{norm_id})"
            tail = (m_hdr.group(6) or "").strip()
            if len(tail) >= 15:
                buf.append(tail)
            continue

        if _COMMENT_ITEM_START_RE.match(s):
            _flush_buf()
            buf.append(s)
        else:
            buf.append(s)

    _flush_buf()
    return items


def _match_manuscript_anchors(comments: List[Dict[str, Any]], manuscript_path: Path) -> List[Dict[str, Any]]:
    """当提供原稿时，按关键词重叠为每条审稿意见自动定位最相关的原稿段落锚点 `[Pn]`，生成可执行的 patch-docx 模板。"""
    ext = manuscript_path.suffix.lower()
    if ext == ".docx":
        doc_info = _read_docx(manuscript_path, numbered=True)
        if doc_info.get("status") == "error":
            return []
        numbered_text = doc_info.get("text", "")
    else:
        raw_lines = [ln.strip() for ln in manuscript_path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
        numbered_text = "\n".join(f"[P{i + 1}] {ln}" for i, ln in enumerate(raw_lines))

    paras: List[Tuple[str, str]] = []
    for ln in numbered_text.splitlines():
        m = re.match(r"^\[(P\d+|T\d+R\d+)\]\s*(.+)$", ln.strip())
        if m:
            paras.append((m.group(1), m.group(2).strip()))

    patch_stubs: List[Dict[str, Any]] = []
    for item in comments:
        tokens = set(re.findall(r"[\u4e00-\u9fff]{2,4}|[A-Za-z][A-Za-z0-9_-]{3,}", item["comment"].lower()))
        best_pid, best_txt, best_score = "", "", 0
        for pid, ptxt in paras:
            if len(ptxt) < 15:
                continue
            pt_low = ptxt.lower()
            sc = sum(1 for tk in tokens if tk in pt_low)
            if sc > best_score:
                best_score, best_pid, best_txt = sc, pid, ptxt
        if best_pid and best_score >= 1:
            item["suggested_anchor"] = best_pid
            item["anchor_preview"] = best_txt[:90]
            first_sent = re.split(r"(?<=[。！？.!?])", best_txt)[0].strip()
            if first_sent:
                patch_stubs.append({
                    "comment_id": item["id"],
                    "paragraph": best_pid,
                    "find": first_sent,
                    "replace": first_sent,
                })
    return patch_stubs


def build_rebuttal_markdown(comments: List[Dict[str, Any]], *, lang: str = "zh", manuscript_title: str = "") -> str:
    is_en = lang == "en"
    title_str = f"《{manuscript_title}》" if (manuscript_title and not is_en) else (f'"{manuscript_title}"' if manuscript_title else "")
    if is_en:
        lines = [
            f"# Point-by-Point Response to Reviewers {title_str}".strip(),
            "",
            "Dear Editor and Reviewers,",
            "",
            "We sincerely thank the Editor and the anonymous reviewers for their constructive comments and suggestions. "
            "Below we provide a point-by-point response to each comment and indicate the exact revisions made in the manuscript.",
            "",
            "## Summary of Major Revisions",
            "",
        ]
        summary_headers = ["ID", "Reviewer", "Category", "Revision Status & Location"]
        summary_rows = [
            [c["id"], c["reviewer"], c["category"], c.get("suggested_anchor") or "Revised in manuscript"]
            for c in comments
        ]
        lines.append(_render_markdown_table(summary_headers, summary_rows))
        lines.append("")
        cur_rev = ""
        for c in comments:
            if c["reviewer"] != cur_rev:
                cur_rev = c["reviewer"]
                lines.extend([f"## {cur_rev}", ""])
            anchor_note = f" (Target Anchor: `{c['suggested_anchor']}`)" if c.get("suggested_anchor") else ""
            lines.extend([
                f"### [{c['id']}] ({c['category']})",
                f"**Reviewer's Comment:** {c['comment']}",
                "",
                f"**Authors' Response:** [TODO: Address `{c['id']}` — {c['recommended_action']}]",
                "",
                f"**Changes in Manuscript{anchor_note}:** [TODO: Quote exact revised text & section/page]",
                "",
            ])
        return "\n".join(lines)

    lines = [
        f"# {title_str}审稿意见逐条修改说明（Response Letter）",
        "",
        "尊敬的主编与审稿专家：",
        "",
        "非常感谢编辑部与各位匿名审稿专家对本文提出的宝贵建设性意见！我们已针对每一条审稿意见进行了逐项核查、补充论证与全文修订。现将修改总览与逐条回复汇报如下：",
        "",
        "## 一、修改总览对照表",
        "",
    ]
    summary_headers = ["意见编号", "来源角色", "问题归类", "原稿建议定位锚点"]
    summary_rows = [
        [c["id"], c["reviewer"], c["category_label"], c.get("suggested_anchor") or "待填入章节/页码"]
        for c in comments
    ]
    lines.append(_render_markdown_table(summary_headers, summary_rows))
    lines.extend(["", "## 二、逐条审稿意见回复与修订摘录", ""])

    cur_rev = ""
    for c in comments:
        if c["reviewer"] != cur_rev:
            cur_rev = c["reviewer"]
            lines.extend([f"### {cur_rev}", ""])
        anchor_hint = f"（原稿候选锚点：`{c['suggested_anchor']}` — {c.get('anchor_preview', '')}）" if c.get("suggested_anchor") else ""
        lines.extend([
            f"#### 【{c['id']}】{c['category_label']}",
            f"- **【专家意见】**：{c['comment']}",
            f"- **【作者回复】**：感谢专家的深刻指正！[待结合研究内容补入具体回应；建议执行：{c['recommended_action']}]",
            f"- **【正文修改位置与修订摘录】**：{anchor_hint}[待粘贴修订后的正文段落摘录]",
            "",
        ])
    return "\n".join(lines)


def cmd_rebuttal(args, output_fn: Callable[[Any], None] = _default_output) -> None:
    cpath = Path(args.filepath)
    if not cpath.exists():
        output_fn({"status": "error", "code": "FILE_NOT_FOUND", "message": f"审稿意见文件不存在: {cpath}"})
        return

    if cpath.suffix.lower() == ".docx":
        d_res = _read_docx(cpath, numbered=False)
        if d_res.get("status") == "error":
            output_fn(d_res)
            return
        raw_text = d_res.get("text", "")
    else:
        raw_text = cpath.read_text(encoding="utf-8", errors="ignore")

    comments = parse_reviewer_comments(raw_text)
    if not comments:
        output_fn({
            "status": "error",
            "code": "NO_COMMENTS_PARSED",
            "message": "未能从文件中拆分出审稿意见条目，请确认文件包含审稿文本",
        })
        return

    patch_stubs: List[Dict[str, Any]] = []
    if getattr(args, "manuscript", None):
        mpath = Path(args.manuscript)
        if mpath.exists():
            patch_stubs = _match_manuscript_anchors(comments, mpath)

    init_patch_path = Path(args.init_patch) if getattr(args, "init_patch", None) else None
    if init_patch_path:
        init_patch_path.parent.mkdir(parents=True, exist_ok=True)
        init_patch_path.write_text(
            json.dumps(
                {
                    "_instruction": "将每项 replace 修改为修订后的新句子后，先运行 patch-docx <原稿.docx> --patch 本文件 --dry-run 预检，再正式生成修订稿",
                    "patches": patch_stubs,
                    "footnotes": [],
                    "append_references": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    cjk_chars = sum(1 for ch in raw_text if "\u4e00" <= ch <= "\u9fff")
    lang = getattr(args, "lang", "auto") or "auto"
    if lang == "auto":
        lang = "zh" if cjk_chars >= 20 else "en"

    md = build_rebuttal_markdown(comments, lang=lang, manuscript_title=getattr(args, "title", "") or "")
    out_fmt = getattr(args, "format", "markdown") or "markdown"
    out_path = Path(args.output) if getattr(args, "output", None) else None

    if out_fmt == "docx":
        target_docx = out_path or Path("response_letter.docx")
        docx_res = _write_docx_from_markdown(md, target_docx)
        if docx_res.get("status") == "error":
            output_fn(docx_res)
            return
        output_fn({
            **docx_res,
            "comment_count": len(comments),
            "comments": comments,
            "patch_template_file": str(init_patch_path) if init_patch_path else None,
        })
        return

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md + "\n", encoding="utf-8")

    if getattr(args, "raw", False):
        print(md)
    else:
        cat_counts = dict(Counter(c["category"] for c in comments))
        output_fn({
            "status": "success",
            "comment_count": len(comments),
            "category_distribution": cat_counts,
            "output_file": str(out_path) if out_path else None,
            "patch_template_file": str(init_patch_path) if init_patch_path else None,
            "comments": comments,
            "markdown": md,
        })
