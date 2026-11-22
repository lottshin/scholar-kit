"""
literature.py - Scholar Kit 统一 CLI 入口 (v1.25.1)

模块职责分层:
  - literature.py     : CLI 参数解析、搜索/下载/引文/核验/导出路由编排
  - config.py         : 全局 (~/.scholar-kit) 与工作区 (./.scholar-kit) 状态及配置加载
  - evidence.py       : 会话/课题库、对比矩阵、结论极性/统计核验与外部检索空白实证
  - research_tools.py : 实证数据三线表/LaTeX制表、目标投稿选刊匹配、审稿意见返修与补丁联动
  - docx_tools.py     : .docx/.txt/.md/.tex/.pdf 论文读取（含段落锚点与章节切片）、本地 PDF 题录导入、Word 交稿排版与补丁预检
  - sections.py       : 学术长文与硕博学位论文目录结构识别及按章节精准切片
  - venues.py         : 国际期刊与会议质量离线识别（SCI/SSCI/A&HCI/ESCI、CCF 2026 推荐目录、中科院预警期刊）
  - zotero.py         : 本机 Zotero SQLite / 本地 HTTP 零依赖直连与 PDF 附件映射
  - doctor.py         : 环境自检 (`check`)、PEP 668 隔离虚拟环境引导与缓存清理 (`clean-cache`)
"""

from __future__ import annotations

__version__ = "1.25.1"

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

# macOS 系统 Python 链接 LibreSSL 时，urllib3 会打印 NotOpenSSLWarning，统一静默以保护 JSON 输出。
warnings.filterwarnings("ignore", message=r".*OpenSSL.*")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", errors="replace", buffering=1)
    sys.stderr = open(sys.stderr.fileno(), mode="w", encoding="utf-8", errors="replace", buffering=1)

_script_dir = str(Path(__file__).parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from cnki import (  # noqa: E402
    _release_driver,
    authenticate_cnki,
    batch_download_cnki,
    batch_read_detail,
    batch_search_cnki,
    check_cnki_access,
    close_shared_browser,
    download_cnki,
    get_cnki_citations,
    get_detail,
    parse_cnki_export,
    reconcile_cnki_paper,
    search_cnki,
)
from config import allowed_fulltext_dirs, cache_dir, get as cfg_get, workspace_state_dir  # noqa: E402
import doctor as _doctor  # noqa: E402
from docx_tools import (  # noqa: E402
    _add_footnote_to_element,
    _find_run_containing,
    _get_or_create_footnotes_part,
    _para_replace_text,
    _write_docx_from_markdown,
    cmd_patch_docx as _cmd_patch_docx,
    cmd_pdf_meta as _cmd_pdf_meta,
    cmd_read_paper as _cmd_read_paper,
    cmd_write_docx as _cmd_write_docx,
    extract_fulltext_from_pdf_file,
    import_pdfs_from_path,
)
from evidence import (  # noqa: E402
    _is_cnki_paper,
    _load_session,
    _project_dir,
    _safe_project_name,
    _save_session,
    _session_file,
    _session_project,
    cmd_library as _cmd_library,
    cmd_projects as _cmd_projects,
    cmd_review as _cmd_review,
    cmd_topics as _cmd_topics,
    cmd_validate as _cmd_validate,
    cmd_write as _cmd_write,
    parse_indices_spec,
)
from formatter import citation_preview, export_papers, generate_in_text_citations, generate_reference_list  # noqa: E402
from research_tools import (  # noqa: E402
    cmd_data_table as _cmd_data_table,
    cmd_match_journal as _cmd_match_journal,
    cmd_rebuttal as _cmd_rebuttal,
)
from sections import extract_frontmatter_toc, parse_fulltext_sections, public_toc, slice_fulltext_sections  # noqa: E402
from thesis_format import cmd_format_audit as _cmd_format_audit, cmd_format_docx as _cmd_format_docx  # noqa: E402
from search import (  # noqa: E402
    analyze_trends,
    calculate_quality_score,
    deduplicate_results,
    get_citations,
    resolve_crossref,
    resolve_unpaywall,
    search_all,
    search_arxiv,
    search_base,
    search_dblp,
    search_nssd,
    search_openalex,
    search_pubmed,
    search_semantic_scholar,
    search_with_fallback,
)
from zotero import query_local_zotero  # noqa: E402

CITATION_STYLE_CHOICES = [
    "gbt7714", "gb",
    "gbt7714-author-year", "author-year",
    "chinese-econ", "econ",
    "apa", "mla", "chicago", "footnote",
]


def _output(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _enhance_error(error_dict: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """增强错误信息，添加恢复步骤与 Agent 指导。"""
    code = error_dict.get("code", "UNKNOWN_ERROR")
    enhanced = error_dict.copy()
    q = (context or {}).get("query", "<query>")

    if code == "CNKI_UNREACHABLE":
        enhanced["recovery"] = {
            "auto_recoverable": True,
            "steps": [
                {"action": "retry_with_escalation", "command": "check --fix", "description": "检查环境与网络授权"},
                {"action": "fallback_to_api", "command": f"search '{q}' --source openalex", "description": "非知网专属任务可回退 OpenAlex"},
            ],
            "agent_instruction": "先通过宿主标准授权流程执行 step 1；确系校外无知网权限且非知网专属任务时再执行 step 2",
        }
        enhanced["user_guidance"] = "请确认已连接校园网/VPN，或运行 auth-cnki 完成校外认证"
    elif code == "DRIVER_MISSING":
        enhanced["recovery"] = {
            "auto_recoverable": True,
            "steps": [{"action": "auto_download_driver", "command": "check --fix", "description": "联网自动准备浏览器驱动"}],
            "agent_instruction": "在具备网络权限的环境下执行 check --fix",
        }
        enhanced["user_guidance"] = "首次使用知网需联网自动下载匹配的 WebDriver"
    elif code == "SANDBOX_BLOCKED":
        enhanced["recovery"] = {
            "auto_recoverable": True,
            "steps": [{"action": "request_host_permission", "command": "check --fix", "description": "通过宿主平台授权网络/浏览器访问"}],
            "agent_instruction": "通过宿主平台的标准提权/审批流程重跑命令",
        }
        enhanced["user_guidance"] = "沙盒限制了网络或浏览器访问，请批准权限请求"
    elif code == "API_RATE_LIMIT":
        enhanced["recovery"] = {
            "auto_recoverable": True,
            "steps": [
                {"action": "switch_source", "command": f"search '{q}' --source openalex --enable-fallback", "description": "切换备用数据源"},
            ],
            "agent_instruction": "等待后重试，或使用 --enable-fallback 自动切换数据源",
        }
        enhanced["user_guidance"] = "API 触发速率限制，请稍后重试或切换数据源"
    elif code == "CNKI_ADV_SEARCH_FAILED":
        enhanced["recovery"] = {
            "auto_recoverable": False,
            "steps": [
                {"action": "check_cnki_page", "command": "auth-cnki --keep-browser", "description": "检查知网登录态或安全验证"},
            ],
            "agent_instruction": "高级检索筛选条件未能提交，请勿将普通未过滤结果当作限定结果；提示用户检查知网状态或简化筛选条件重试",
        }
        enhanced["user_guidance"] = "知网高级检索表单未能提交，已停止以避免返回未按条件过滤的结果"
    elif code == "CNKI_EXPR_SYNTAX_ERROR":
        enhanced["recovery"] = {
            "auto_recoverable": True,
            "steps": [
                {"action": "fix_expression", "command": "search --expr \"TKA='关键词' AND LY%='期刊名'\"", "description": "修正知网专业检索式语法"},
            ],
            "agent_instruction": "检查单引号闭合、括号配对，年份请使用 --year-from/--year-to 而非 YE= / YE>=",
        }
        enhanced["user_guidance"] = "知网专业检索式语法有误，请修正后重试"
    elif code == "NO_RESULTS":
        enhanced["recovery"] = {
            "auto_recoverable": True,
            "steps": [
                {"action": "try_synonyms", "command": "search '<synonym>' --source <same_source>", "description": "尝试同义词或中英文切换"},
                {"action": "broaden_search", "command": f"search '{q}' --source all --async-search", "description": "扩展到全源并发检索"},
            ],
            "agent_instruction": "尝试同义词、放宽年份过滤，或改用 --source all --async-search",
        }
        enhanced["user_guidance"] = "未找到匹配结果，建议换用同义词或放宽筛选条件"
    return enhanced


# ── CNKI 搜索缓存（优先全局缓存目录，兼容旧版工作区缓存） ──────────────────

def _cnki_cache_key(args) -> str:
    cond_list = getattr(args, "condition", None) or []
    parts = [
        args.query or "",
        getattr(args, "expr", "") or "",
        getattr(args, "professional", "") or "",
        ";".join(str(c) for c in cond_list),
        getattr(args, "match", "") or "",
        str(getattr(args, "cross_language", "") or ""),
        getattr(args, "core", "") or "",
        str(getattr(args, "year_from", "") or ""),
        str(getattr(args, "year_to", "") or ""),
        getattr(args, "author", "") or "",
        getattr(args, "first_author", "") or "",
        getattr(args, "affiliation", "") or "",
        getattr(args, "journal", "") or "",
        getattr(args, "fund", "") or "",
        "1" if getattr(args, "fuzzy", False) else "0",
        getattr(args, "doc_type", "") or "",
        getattr(args, "field", "") or "",
        getattr(args, "sort", "relevance") or "relevance",
        str(getattr(args, "pages", 1) or 1),
        str(getattr(args, "per_page", "") or ""),
        "1" if getattr(args, "bulk_abstract", False) else "0",
    ]
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


def _cnki_cache_get(args) -> Optional[list]:
    from datetime import datetime, timedelta

    key = _cnki_cache_key(args)
    candidates = [
        cache_dir("cache", create=False) / f"cnki_{key}.json",
        workspace_state_dir(create=False) / "cache" / f"cnki_{key}.json",
    ]
    ttl = cfg_get("cache_ttl_days", 30)
    for cache_file in candidates:
        if not cache_file.exists():
            continue
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            ts = data.get("_cached_at", "")
            if ts and ttl > 0:
                if datetime.now() - datetime.fromisoformat(ts) > timedelta(days=ttl):
                    continue
            return data.get("results")
        except Exception:
            continue
    return None


def _cnki_cache_set(args, results: list) -> None:
    from datetime import datetime

    cdir = cache_dir("cache", create=True)
    cache_file = cdir / f"cnki_{_cnki_cache_key(args)}.json"
    data = {"_cached_at": datetime.now().isoformat(), "results": results}
    try:
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ── search / batch-search 命令 ──────────────────────────────────────────

def cmd_search(args) -> None:
    source = args.source or "cnki"
    if source not in ("cnki", "openalex", "semantic", "arxiv", "pubmed", "nssd", "dblp", "base", "all"):
        _output({"status": "error", "code": "UNKNOWN_SOURCE", "message": f"未知数据源: {source}"})
        return

    want_download = getattr(args, "download", False)
    if want_download and source != "cnki":
        _output({"status": "error", "code": "DOWNLOAD_SOURCE_MISMATCH", "message": "--download 仅支持 --source cnki"})
        return

    cond_args = getattr(args, "condition", None)
    pro_arg = getattr(args, "professional", None)
    match_arg = getattr(args, "match", None)
    cross_lang_raw = getattr(args, "cross_language", None)
    cross_lang_bool: Optional[bool] = (
        True if cross_lang_raw == "on" else (False if cross_lang_raw == "off" else None)
    )
    if source not in ("cnki", "all") and any([cond_args, pro_arg, match_arg, cross_lang_raw]):
        _output({
            "status": "error",
            "code": "CNKI_OPTION_SOURCE_MISMATCH",
            "message": "--condition / --professional / --match / --cross-language 仅支持知网数据源 (--source cnki)",
        })
        return

    results: List[Dict[str, Any]] = []
    reuse_driver = None
    cnki_error = None
    cnki_meta: Dict[str, Any] = {}

    if source in ("cnki", "all"):
        cached_cnki = _cnki_cache_get(args) if not want_download else None
        if cached_cnki is not None:
            print("[cnki] 使用缓存结果", file=sys.stderr)
            results.extend(cached_cnki)
            cnki_meta["retrieval"] = {"mode": "cached"}
        else:
            keep = want_download and source == "cnki"
            bulk_abs = getattr(args, "bulk_abstract", False)
            no_bulk_abs = getattr(args, "no_bulk_abstract", False)
            req_per_page = getattr(args, "per_page", None)
            if req_per_page is None and ((args.limit and args.limit > 20) or bulk_abs):
                req_per_page = 50
            cnki_kw: Dict[str, Any] = {}
            if req_per_page is not None:
                cnki_kw["per_page"] = req_per_page
            if no_bulk_abs:
                cnki_kw["bulk_abstract"] = False
            elif bulk_abs:
                cnki_kw["bulk_abstract"] = True
            if cond_args:
                cnki_kw["conditions"] = list(cond_args)
            if pro_arg is not None:
                cnki_kw["professional"] = pro_arg
            if match_arg is not None:
                cnki_kw["match"] = match_arg
            if cross_lang_bool is not None:
                cnki_kw["cross_language"] = cross_lang_bool
            cnki_kw["meta"] = cnki_meta
            cnki_core_only = None
            if args.core:
                cn_parts = [
                    c.strip() for c in str(args.core).split(",")
                    if c.strip() in ("北大核心", "CSSCI", "AMI", "WJCI", "CSCD", "EI")
                ]
                cnki_core_only = ",".join(cn_parts) if cn_parts else None
            cnki_ret = search_cnki(
                keyword=args.query or "",
                core=cnki_core_only,
                year_from=args.year_from,
                year_to=args.year_to,
                author=args.author,
                first_author=getattr(args, "first_author", None),
                affiliation=getattr(args, "affiliation", None),
                journal=args.journal,
                fund=getattr(args, "fund", None),
                expr=getattr(args, "expr", None),
                fuzzy=getattr(args, "fuzzy", False),
                doc_type=getattr(args, "doc_type", None),
                field=getattr(args, "field", None),
                sort=args.sort or "relevance",
                pages=args.pages or 1,
                cite_enrich=getattr(args, "cite_enrich", 0),
                _keep_driver=keep,
                **cnki_kw,
            )
            if keep and isinstance(cnki_ret, tuple):
                cnki_results, reuse_driver = cnki_ret
            else:
                cnki_results = cnki_ret

            if cnki_results and not (len(cnki_results) == 1 and cnki_results[0].get("status") == "error"):
                results.extend(cnki_results)
                _cnki_cache_set(args, cnki_results)
            elif cnki_results and cnki_results[0].get("status") == "error":
                if source == "cnki":
                    err_out = _enhance_error(cnki_results[0], {"query": args.query})
                    if cnki_meta.get("retrieval"):
                        err_out["retrieval"] = cnki_meta["retrieval"]
                    _output(err_out)
                    if reuse_driver:
                        _release_driver(reuse_driver)
                    return
                cnki_error = cnki_results[0]

    try:
        from venues import annotate_venue_quality, matches_international_core, parse_international_core_tokens
        intl_core_tokens = parse_international_core_tokens(args.core)
        exclude_warning = bool(getattr(args, "exclude_warning", False)) or bool(intl_core_tokens)

        has_keyword = bool(args.query and args.query.strip())
        if source != "cnki" and not has_keyword:
            _output({"status": "error", "code": "NO_KEYWORDS", "message": "API 数据源检索须提供搜索关键词 query"})
            return
        base_api_limit = args.limit if args.limit is not None else 10
        api_limit = min(max(base_api_limit * 3, 40), 50) if (intl_core_tokens or exclude_warning) else base_api_limit

        field_param = "default"
        if getattr(args, "field", None):
            field_map = {"篇名": "title", "摘要": "abstract", "主题": "default"}
            field_param = field_map.get(args.field, "default")

        author_filter = getattr(args, "author_filter", None)
        journal_filter = getattr(args, "journal_filter", None)
        field_of_study = getattr(args, "field_of_study", None)
        page = getattr(args, "page", 1)
        enable_fallback = getattr(args, "enable_fallback", False)
        async_search = getattr(args, "async_search", False)

        # 三路互斥分发：并发全源 / 单源自动降级 / 常规逐源检索
        if has_keyword and async_search and source == "all":
            from search_async import search_all_sync

            print("[async] 使用异步并发搜索...", file=sys.stderr)
            async_result = search_all_sync(
                query=args.query,
                limit=api_limit,
                year_from=args.year_from,
                year_to=args.year_to,
                sort=args.sort or "relevance",
                sources=["openalex", "semantic_scholar", "arxiv", "nssd", "dblp", "base"],
                field=field_param,
                journal=journal_filter,
                author=author_filter,
                field_of_study=field_of_study,
                page=page,
            )
            results.extend(async_result.get("results", []))
            print(
                f"[async] 完成，耗时 {async_result.get('elapsed_ms', 0)} ms，"
                f"使用数据源: {', '.join(async_result.get('sources_used', []))}",
                file=sys.stderr,
            )

        elif has_keyword and enable_fallback and source in ("openalex", "semantic", "arxiv", "pubmed", "nssd", "dblp", "base"):
            fallback_result = search_with_fallback(
                query=args.query,
                primary_source=source,
                limit=api_limit,
                year_from=args.year_from,
                year_to=args.year_to,
                sort=args.sort or "relevance",
                field=field_param,
                journal=journal_filter,
                author=author_filter,
                field_of_study=field_of_study,
                page=page,
            )
            if fallback_result.get("results"):
                results.extend(fallback_result["results"])
                if fallback_result.get("fallback"):
                    print(
                        f"[fallback] {fallback_result['original_source']} 无结果，已切换到 {fallback_result['source']}",
                        file=sys.stderr,
                    )
            elif fallback_result.get("error"):
                print(f"[fallback] {fallback_result['error']}", file=sys.stderr)

        elif has_keyword:
            if source == "pubmed":
                results.extend(search_pubmed(
                    args.query, limit=api_limit,
                    year_from=args.year_from, year_to=args.year_to,
                    sort=args.sort or "relevance",
                    field=field_param, journal=journal_filter,
                    author=author_filter, page=page,
                ))
            if source in ("openalex", "all"):
                results.extend(search_openalex(
                    args.query, limit=api_limit,
                    year_from=args.year_from, year_to=args.year_to,
                    sort=args.sort or "relevance",
                    field=field_param, journal=journal_filter,
                    author=author_filter, field_of_study=field_of_study, page=page,
                ))
            if source in ("semantic", "all"):
                results.extend(search_semantic_scholar(
                    args.query, limit=api_limit,
                    year_from=args.year_from, year_to=args.year_to,
                    sort=args.sort or "relevance",
                    field=field_param, journal=journal_filter,
                    author=author_filter, field_of_study=field_of_study, page=page,
                ))
            if source in ("arxiv", "all"):
                results.extend(search_arxiv(
                    args.query, limit=api_limit, sort_by=args.sort or "relevance",
                    year_from=args.year_from, year_to=args.year_to, page=page,
                ))
            if source in ("nssd", "all"):
                results.extend(search_nssd(
                    args.query, limit=api_limit,
                    year_from=args.year_from, year_to=args.year_to,
                ))
            if source in ("dblp", "all"):
                results.extend(search_dblp(
                    args.query, limit=api_limit,
                    year_from=args.year_from, year_to=args.year_to,
                ))
            if source in ("base", "all"):
                results.extend(search_base(
                    args.query, limit=api_limit,
                    year_from=args.year_from, year_to=args.year_to,
                ))

        deduped = deduplicate_results(results)
        for paper in deduped:
            if not _is_cnki_paper(paper):
                annotate_venue_quality(paper)
            paper["quality_score"] = calculate_quality_score(paper)

        if intl_core_tokens or exclude_warning:
            filtered_deduped = []
            for p in deduped:
                if _is_cnki_paper(p):
                    filtered_deduped.append(p)
                elif intl_core_tokens:
                    if matches_international_core(p, intl_core_tokens):
                        filtered_deduped.append(p)
                elif exclude_warning and p.get("is_warning"):
                    continue
                else:
                    filtered_deduped.append(p)
            deduped = filtered_deduped

        if args.sort == "citations":
            deduped.sort(key=lambda x: x.get("cited_by", 0), reverse=True)
        elif args.sort == "downloads":
            deduped.sort(key=lambda x: x.get("downloads", 0), reverse=True)
        elif args.sort == "date":
            deduped.sort(key=lambda x: x.get("year") or 0, reverse=True)
        elif args.sort == "quality":
            deduped.sort(key=lambda x: x.get("quality_score", 0), reverse=True)

        per_p = getattr(args, "per_page", None) or 20
        if args.limit is not None:
            effective_limit = args.limit
        elif args.pages and args.pages > 1 and source in ("cnki", "all"):
            effective_limit = args.pages * per_p
        else:
            effective_limit = max(20, per_p)
        deduped = deduped[:effective_limit]

        enrich_n = getattr(args, "enrich", 0) or 0
        bulk_abs = getattr(args, "bulk_abstract", False)
        if bulk_abs and enrich_n <= 0:
            # 若校园网未登录个人账号导致批量题录导出被跳过，自动用单会话详情补全前 5 篇摘要
            missing_abs = [p for p in deduped if _is_cnki_paper(p) and p.get("url") and not p.get("abstract")]
            if missing_abs:
                enrich_n = min(5, len(missing_abs))

        if enrich_n > 0:
            to_enrich = [
                p for p in deduped
                if _is_cnki_paper(p)
                and p.get("url")
                and (not p.get("pages") and not p.get("is_online_first") or (bulk_abs and not p.get("abstract")))
            ][:enrich_n]
            if to_enrich:
                print(f"[enrich] 正在单会话批量补全 {len(to_enrich)} 篇论文的详情与卷期页码...", file=sys.stderr)
                batch_read_detail(papers=to_enrich, top_n=len(to_enrich), fulltext=False)
                for paper in deduped:
                    paper["quality_score"] = calculate_quality_score(paper)

        _save_session(deduped, append=getattr(args, "append", False), project=_session_project(args))

        if len(deduped) == 0:
            no_results_error: Dict[str, Any] = {
                "status": "warning",
                "code": "NO_RESULTS",
                "message": f"未找到匹配 '{args.query or getattr(args, 'professional', '') or getattr(args, 'expr', '')}' 的结果",
                "count": 0,
                "results": [],
            }
            if cnki_meta.get("retrieval"):
                no_results_error["retrieval"] = cnki_meta["retrieval"]
            _output(_enhance_error(no_results_error, {"query": args.query}))
            return

        for p in deduped:
            p["citation_preview"] = citation_preview(p)

        search_output: Dict[str, Any] = {"status": "success", "count": len(deduped), "results": deduped}
        if cnki_meta.get("retrieval"):
            search_output["retrieval"] = cnki_meta["retrieval"]
        if cnki_error and source == "all":
            search_output["cnki_error"] = {"code": cnki_error.get("code"), "message": cnki_error.get("message")}
            search_output["status"] = "partial"
        if args.export:
            if args.export in ("gbt7714", "gb", "apa", "mla", "chicago", "footnote", "bibtex", "ris"):
                _enrich_bibliography_subset(deduped, deduped, _session_project(args))
            content = export_papers(deduped, args.export, args.output)
            if isinstance(content, dict) and content.get("status") == "error":
                search_output["export_error"] = content
            else:
                search_output.update({"format": args.export, "output_file": args.output, "content": content})

        if want_download and reuse_driver:
            dl_dir = getattr(args, "download_dir", None) or cfg_get("save_dir", "./papers")
            dl_top_n = getattr(args, "download_top_n", None)
            dl_papers = deduped[:dl_top_n] if dl_top_n else deduped
            dl_urls = [p.get("url") for p in dl_papers if isinstance(p, dict) and p.get("url")]
            if dl_urls:
                dl_format = getattr(args, "download_file_format", "pdf") or "pdf"
                dl_hints = _paper_lookup_by_url(dl_papers)
                try:
                    dl_result = batch_download_cnki(
                        dl_urls,
                        save_dir=dl_dir,
                        file_format=dl_format,
                        _driver=reuse_driver,
                        paper_hints=dl_hints,
                    )
                except TypeError:
                    dl_result = batch_download_cnki(
                        dl_urls,
                        save_dir=dl_dir,
                        file_format=dl_format,
                        _driver=reuse_driver,
                    )
                fallback_format = getattr(args, "download_fallback_format", None)
                if fallback_format:
                    failed_urls = [
                        err.get("url") for err in (dl_result.get("errors") or [])
                        if isinstance(err, dict)
                        and err.get("url")
                        and err.get("code") == "DOWNLOAD_BTN_NOT_FOUND"
                    ]
                    if failed_urls:
                        fallback_result = batch_download_cnki(
                            failed_urls,
                            save_dir=dl_dir,
                            file_format=fallback_format,
                            _driver=reuse_driver,
                        )
                        dl_result = _merge_fallback_download(dl_result, fallback_result)
                if not getattr(args, "download_no_report", False):
                    dl_result = attach_download_report(
                        dl_result,
                        save_dir=dl_dir,
                        session_papers=dl_papers,
                        requested_urls=dl_urls,
                        citation_style=getattr(args, "download_citation_style", "gbt7714") or "gbt7714",
                        file_format=dl_format,
                        report_output=getattr(args, "download_report_output", None),
                    )
                reuse_driver = None
                search_output["download"] = dl_result
            else:
                search_output["download"] = {
                    "status": "warning",
                    "code": "NO_DOWNLOAD_URLS",
                    "message": "搜索结果中无可下载 URL",
                }

        _output(search_output)
    finally:
        if reuse_driver is not None:
            _release_driver(reuse_driver)


def cmd_batch_search(args) -> None:
    """批量知网搜索：浏览器只启动一次，循环搜索多个关键词。"""
    keywords = list(args.queries) if args.queries else []

    if args.query_file:
        qf = Path(args.query_file)
        if not qf.exists():
            _output({"status": "error", "code": "FILE_NOT_FOUND", "message": f"关键词文件不存在: {args.query_file}"})
            return
        qf_text = None
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb2312", "gb18030"):
            try:
                qf_text = qf.read_text(encoding=enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if qf_text is None:
            _output({"status": "error", "code": "ENCODING_ERROR", "message": f"关键词文件编码无法识别: {args.query_file}"})
            return
        for line in qf_text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                keywords.append(line)

    if not keywords:
        _output({"status": "error", "code": "NO_KEYWORDS", "message": "未提供关键词"})
        return

    batch_cross_raw = getattr(args, "cross_language", None)
    batch_cross_bool: Optional[bool] = (
        True if batch_cross_raw == "on" else (False if batch_cross_raw == "off" else None)
    )
    batch_kw: Dict[str, Any] = {}
    if getattr(args, "match", None) is not None:
        batch_kw["match"] = getattr(args, "match")
    if batch_cross_bool is not None:
        batch_kw["cross_language"] = batch_cross_bool

    result = batch_search_cnki(
        keywords=keywords,
        core=args.core,
        author=getattr(args, "author", None),
        first_author=getattr(args, "first_author", None),
        affiliation=getattr(args, "affiliation", None),
        journal=getattr(args, "journal", None),
        fund=getattr(args, "fund", None),
        fuzzy=getattr(args, "fuzzy", False),
        doc_type=getattr(args, "doc_type", None),
        field=getattr(args, "field", None),
        year_from=args.year_from,
        year_to=args.year_to,
        sort=args.sort or "relevance",
        pages=args.pages or 1,
        bulk_abstract=not getattr(args, "no_bulk_abstract", False),
        **batch_kw,
    )

    if result.get("status") in ("success", "partial") and result.get("results"):
        _save_session(result.get("results") or [], append=args.append, project=_session_project(args))

    if args.export and result.get("results"):
        content = export_papers(result["results"], args.export, args.output)
        if isinstance(content, dict) and content.get("status") == "error":
            _output(content)
            return
        export_output = {
            "status": result.get("status", "success"),
            "count": len(result["results"]),
            "format": args.export,
            "output_file": args.output,
            "content": content,
        }
        if result.get("errors"):
            export_output["errors"] = result["errors"]
        _output(export_output)
    else:
        _output(result)


# ── 下载与下载清单报告 ──────────────────────────────────────────────────

def cmd_download(args) -> None:
    target = args.target
    save_dir = args.dir if args.dir != "./papers" else cfg_get("save_dir", "./papers")

    if args.doi:
        unpaywall = resolve_unpaywall(args.doi)
        if unpaywall and unpaywall.get("oa_url"):
            _output({
                "status": "success",
                "method": "unpaywall_oa",
                "url": unpaywall["oa_url"],
                "message": f"找到 OA 链接: {unpaywall['oa_url']}",
            })
            return

        crossref = resolve_crossref(args.doi)
        meta = {
            "title": (crossref or {}).get("title", ""),
            "authors": (crossref or {}).get("authors", ""),
            "journal": (crossref or {}).get("journal", ""),
            "doi": args.doi,
        }
        if target and "cnki.net" in target:
            _output(download_cnki(target, save_dir=save_dir, file_format=args.file_format or "pdf"))
            return
        _output({
            "status": "error",
            "code": "OA_NOT_FOUND",
            "message": "Unpaywall 未找到 OA 版本，无知网 URL 可回退",
            "metadata": meta,
        })
        return

    if target and "cnki.net" in target:
        sess_papers = _load_session(getattr(args, "project", None))
        matched_p = next((p for p in sess_papers if isinstance(p, dict) and p.get("url") == target), {})
        _output(download_cnki(
            target,
            save_dir=save_dir,
            file_format=args.file_format or "pdf",
            expected_title=matched_p.get("title", ""),
            expected_journal=matched_p.get("journal", ""),
        ))
    elif target:
        _output({"status": "error", "code": "UNSUPPORTED_URL", "message": "目前仅支持知网 URL 直接下载"})
    else:
        _output({"status": "error", "code": "NO_DOWNLOAD_TARGET", "message": "请提供下载目标（URL 或 --doi）"})


def _download_report_path(save_dir: str, report_output: Optional[str] = None) -> str:
    if report_output:
        return report_output
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return str(Path(save_dir) / f"download_report_{stamp}.md")


def _paper_lookup_by_url(papers: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {p["url"]: p for p in papers if isinstance(p, dict) and p.get("url")}


def _download_item_to_paper(item: Dict[str, Any], lookup: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    paper = dict(lookup.get(item.get("url", ""), {}))
    for key in ("url", "title"):
        if item.get(key) and not paper.get(key):
            paper[key] = item[key]
    if not paper.get("title"):
        paper["title"] = item.get("filename") or item.get("url") or "未获取题名"
    return paper


def build_download_report(
    result: Dict[str, Any],
    session_papers: Optional[List[Dict[str, Any]]] = None,
    requested_urls: Optional[List[str]] = None,
    citation_style: str = "gbt7714",
    file_format: str = "pdf",
) -> Dict[str, Any]:
    lookup = _paper_lookup_by_url(session_papers or [])
    ok_items = result.get("results") or []
    error_items = result.get("errors") or []
    if not error_items and result.get("status") == "error" and requested_urls:
        ok_urls = {item.get("url") for item in ok_items if isinstance(item, dict)}
        error_items = [
            {"url": url, "code": result.get("code"), "error": result.get("message") or result.get("code") or "下载失败"}
            for url in requested_urls if url not in ok_urls
        ]

    downloaded = [_download_item_to_paper(item, lookup) for item in ok_items if isinstance(item, dict)]
    failed = [_download_item_to_paper(item, lookup) for item in error_items if isinstance(item, dict)]

    downloaded_refs = generate_reference_list(downloaded, citation_style).splitlines() if downloaded else []
    failed_refs = generate_reference_list(failed, citation_style).splitlines() if failed else []

    lines = [
        "# 文献下载清单",
        "",
        f"- 请求格式: {file_format.upper()}",
        f"- 引用格式: {citation_style.upper()}",
        f"- 已下载: {len(downloaded)}",
        f"- 未下载: {len(failed)}",
        "",
        "## 已下载",
        "",
    ]
    if downloaded_refs:
        for idx, ref in enumerate(downloaded_refs):
            item = ok_items[idx] if idx < len(ok_items) and isinstance(ok_items[idx], dict) else {}
            actual_format = item.get("format") or file_format
            requested = item.get("requested_format") or file_format
            filename = item.get("filename")
            suffix_parts = [f"格式：{actual_format.upper()}"]
            if actual_format != requested or item.get("fallback_used"):
                suffix_parts.append(f"由 {requested.upper()} 降级")
            if filename:
                suffix_parts.append(f"文件：{filename}")
            lines.append(f"{ref}（{'；'.join(suffix_parts)}）")
    else:
        lines.append("无")

    lines.extend(["", "## 未下载", ""])
    if failed_refs:
        for idx, ref in enumerate(failed_refs):
            item = error_items[idx] if idx < len(error_items) and isinstance(error_items[idx], dict) else {}
            reason = item.get("error") or item.get("message") or item.get("code") or "未获取失败原因"
            lines.append(f"{ref}（原因：{reason}）")
    else:
        lines.append("无")

    return {
        "citation_style": citation_style,
        "file_format": file_format,
        "downloaded_references": downloaded_refs,
        "failed_references": failed_refs,
        "markdown": "\n".join(lines).rstrip() + "\n",
    }


def attach_download_report(
    result: Dict[str, Any],
    save_dir: str,
    session_papers: Optional[List[Dict[str, Any]]] = None,
    requested_urls: Optional[List[str]] = None,
    citation_style: str = "gbt7714",
    file_format: str = "pdf",
    report_output: Optional[str] = None,
) -> Dict[str, Any]:
    report = build_download_report(
        result,
        session_papers=session_papers,
        requested_urls=requested_urls,
        citation_style=citation_style,
        file_format=file_format,
    )
    output_path = _download_report_path(save_dir, report_output)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(report["markdown"], encoding="utf-8")
    enriched = dict(result)
    enriched["download_report"] = {
        "path": output_path,
        "citation_style": report["citation_style"],
        "file_format": report["file_format"],
        "downloaded_references": report["downloaded_references"],
        "failed_references": report["failed_references"],
    }
    return enriched


def _merge_fallback_download(primary: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(primary)
    primary_results = list(primary.get("results") or [])
    fallback_results = list(fallback.get("results") or [])
    fallback_urls = {item.get("url") for item in fallback_results if isinstance(item, dict)}
    primary_requested_format = primary.get("requested_format")

    for item in fallback_results:
        if isinstance(item, dict):
            item = dict(item)
            if primary_requested_format:
                item["requested_format"] = primary_requested_format
            item["fallback_used"] = True
            primary_results.append(item)

    remaining_errors = [
        err for err in (primary.get("errors") or [])
        if not isinstance(err, dict) or err.get("url") not in fallback_urls
    ]
    remaining_errors.extend(fallback.get("errors") or [])

    merged["results"] = primary_results
    merged["errors"] = remaining_errors or None
    merged["count"] = len(primary_results)
    if remaining_errors and primary_results:
        merged["status"] = "partial"
    elif remaining_errors:
        merged["status"] = "error"
        merged["code"] = primary.get("code") or fallback.get("code") or "CNKI_BATCH_DOWNLOAD_FAILED"
    else:
        merged["status"] = "success"
        merged.pop("code", None)
    merged["fallback"] = {
        "attempted": bool(fallback_results or fallback.get("errors")),
        "status": fallback.get("status"),
        "format": fallback.get("requested_format"),
        "count": len(fallback_results),
    }
    return merged


def cmd_batch_download(args) -> None:
    urls = list(args.urls) if args.urls else []
    session_data: List[Dict[str, Any]] = []

    if args.from_session:
        session_data = _load_session(_session_project(args))
        if not session_data:
            _output({"status": "error", "code": "NO_SESSION", "message": "没有搜索记录，请先执行 search 或 batch-search"})
            return
        top_n = args.top_n or len(session_data)
        urls.extend([p.get("url") for p in session_data[:top_n] if isinstance(p, dict) and p.get("url")])

    if not urls:
        _output({"status": "error", "code": "NO_URLS", "message": "未提供下载 URL（可用 --from-session 从上次搜索结果读取）"})
        return

    save_dir = args.dir if args.dir != "./papers" else cfg_get("save_dir", "./papers")
    hints = _paper_lookup_by_url(session_data or _load_session(_session_project(args)))
    try:
        result = batch_download_cnki(
            urls=urls,
            save_dir=save_dir,
            file_format=args.file_format or "pdf",
            paper_hints=hints,
        )
    except TypeError:
        result = batch_download_cnki(urls=urls, save_dir=save_dir, file_format=args.file_format or "pdf")
    fallback_format = getattr(args, "fallback_format", None)
    if fallback_format:
        failed_urls = [
            err.get("url") for err in (result.get("errors") or [])
            if isinstance(err, dict) and err.get("url") and err.get("code") == "DOWNLOAD_BTN_NOT_FOUND"
        ]
        if failed_urls:
            fallback_result = batch_download_cnki(urls=failed_urls, save_dir=save_dir, file_format=fallback_format)
            result = _merge_fallback_download(result, fallback_result)
    if not getattr(args, "no_report", False):
        result = attach_download_report(
            result,
            save_dir=save_dir,
            session_papers=session_data,
            requested_urls=urls,
            citation_style=getattr(args, "citation_style", "gbt7714") or "gbt7714",
            file_format=args.file_format or "pdf",
            report_output=getattr(args, "report_output", None),
        )
    _output(result)


# ── 详情、认证、导出、引用、导入、引文、趋势 ────────────────────────────

def cmd_detail(args) -> None:
    if not args.url:
        _output({"status": "error", "code": "NO_URL", "message": "请提供知网论文详情页 URL"})
        return
    _output(get_detail(args.url))


def cmd_auth_cnki(args) -> None:
    _output(authenticate_cnki(
        auth_url=args.auth_url,
        verify_url=args.verify_url,
        institution=args.institution,
        wait_seconds=args.wait_seconds,
        captcha_timeout=args.captcha_timeout,
        direct_domains=args.direct_domain,
        debug_snapshot=args.debug_snapshot,
        keep_browser=args.keep_browser,
        force=args.force,
    ))


def _enrich_bibliography_subset(
    selected_papers: List[Dict[str, Any]],
    all_papers: List[Dict[str, Any]],
    project: Optional[str],
) -> bool:
    """对选中论文子集统一执行单会话知网详情补全与 Crossref 卷期页码补全，若发生变更则持久化写回会话。"""
    session_dirty = False
    cnki_need_enrich = [
        p for p in selected_papers
        if _is_cnki_paper(p)
        and p.get("url")
        and not p.get("pages")
        and not p.get("is_online_first")
        and "cnki.net" in p.get("url", "")
    ]
    if cnki_need_enrich:
        print(f"[cite] 正在单会话批量补全 {len(cnki_need_enrich)} 篇知网论文的卷期页码...", file=sys.stderr)
        batch_read_detail(papers=cnki_need_enrich, top_n=len(cnki_need_enrich), fulltext=False)
        session_dirty = True

    for i, p in enumerate(selected_papers):
        if p.get("doi") and (not p.get("volume") or not p.get("pages")) and not _is_cnki_paper(p):
            if i > 0:
                time.sleep(0.5)
            crossref_data = resolve_crossref(p["doi"])
            if crossref_data:
                for k, v in crossref_data.items():
                    if v and not p.get(k):
                        p[k] = v
                        session_dirty = True

    if session_dirty and all_papers:
        _save_session(all_papers, project=project)
    return session_dirty


def cmd_export(args) -> None:
    project = _session_project(args)
    all_papers = _load_session(project)
    if not all_papers:
        _output({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可导出的数据，请先执行 search、batch-search 或 import"})
        return
    indices_raw = getattr(args, "indices", None)
    if indices_raw:
        try:
            pick_idx = parse_indices_spec(indices_raw, len(all_papers), preserve_order=True)
        except ValueError:
            pick_idx = []
        if not pick_idx:
            _output({"status": "error", "code": "INVALID_INDICES", "message": f"无效序号 '{indices_raw}'，当前共 {len(all_papers)} 篇"})
            return
        papers = [all_papers[i] for i in pick_idx]
    else:
        papers = all_papers

    bib_formats = set(CITATION_STYLE_CHOICES) | {"bibtex", "ris"}
    if args.export_format in bib_formats and not getattr(args, "no_enrich", False):
        _enrich_bibliography_subset(papers, all_papers, project)

    result = export_papers(papers, args.export_format, args.output)
    if isinstance(result, dict) and result.get("status") == "error":
        _output(result)
        return
    if args.raw:
        print(result)
    else:
        _output({
            "status": "success",
            "project": project,
            "count": len(papers),
            "format": args.export_format,
            "output_file": args.output,
            "content": result,
        })


def _apply_original_numbers_to_refs(ref_list: str, orig_numbers: List[int]) -> str:
    import re as _re
    lines = ref_list.splitlines()
    if len(lines) != len(orig_numbers):
        return ref_list
    out_lines = []
    for line, orig_no in zip(lines, orig_numbers):
        if _re.match(r"^\[\d+\]", line):
            out_lines.append(_re.sub(r"^\[\d+\]", f"[{orig_no}]", line, count=1))
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


_DOC_CITE_BRACKET_RE = r"(?<!\^)\[(\d+(?:\s*[,，\-–—]\s*\d+)*)\](?!\()"


def _strip_trailing_bibliography(doc_text: str) -> str:
    import re as _re
    return _re.split(r"\n#{1,3}\s*(?:参考文献|References|Bibliography)\b", doc_text, maxsplit=1)[0].rstrip()


def _expand_bracket_tokens(spec: str, max_index: int) -> Optional[List[int]]:
    """解析单个方括号内的 1-based 文献序号串；若任一数值超出 1..max_index（例如年份 [2024]），返回 None。"""
    import re as _re
    out: List[int] = []
    for seg in _re.split(r"[,，]", spec):
        seg = seg.strip()
        if not seg:
            continue
        m_rng = _re.match(r"^(\d+)\s*[\-–—]\s*(\d+)$", seg)
        if m_rng:
            lo, hi = int(m_rng.group(1)), int(m_rng.group(2))
            if not (1 <= lo <= hi <= max_index and (hi - lo) <= 50):
                return None
            out.extend(range(lo, hi + 1))
        elif seg.isdigit():
            val = int(seg)
            if not (1 <= val <= max_index):
                return None
            out.append(val)
        else:
            return None
    return out or None


def _format_sequential_bracket(nums: List[int]) -> str:
    """将重映射后的序号列表格式化为符合 GB/T 7714 的方括号角标（连续>=3项压缩为 a-b）。"""
    dedup: List[int] = []
    for n in nums:
        if n not in dedup:
            dedup.append(n)
    # 保持原括号内顺序；若恰好单调递增则合并连续 >=3 的区间
    if dedup == sorted(dedup) and len(dedup) >= 3:
        parts: List[str] = []
        i = 0
        while i < len(dedup):
            j = i
            while j + 1 < len(dedup) and dedup[j + 1] == dedup[j] + 1:
                j += 1
            if j - i >= 2:
                parts.append(f"{dedup[i]}-{dedup[j]}")
            else:
                for k in range(i, j + 1):
                    parts.append(str(dedup[k]))
            i = j + 1
        return f"[{', '.join(parts)}]"
    return f"[{', '.join(map(str, dedup))}]"


def _extract_pick_indices_from_doc(doc_text: str, total_papers: int, *, is_tex: bool = False) -> List[int]:
    """按正文首次出现顺序提取 0-based 课题库文献索引列表（自动剥离文末已有参考文献节，兼容 LaTeX 注释与可选宏参数）。"""
    import re as _re
    body = _strip_trailing_bibliography(doc_text)
    seen: List[int] = []
    lines = body.splitlines() if is_tex else [body]
    for ln in lines:
        scan_txt = _re.split(r"(?<!\\)%", ln, maxsplit=1)[0] if is_tex else ln
        for m in _re.finditer(_DOC_CITE_BRACKET_RE, scan_txt):
            if is_tex and _re.search(r"\\[A-Za-z@]+\s*$", scan_txt[:m.start()]):
                continue
            expanded = _expand_bracket_tokens(m.group(1), total_papers)
            if not expanded:
                continue
            for one_based in expanded:
                idx0 = one_based - 1
                if idx0 not in seen:
                    seen.append(idx0)
    return seen


def _rewrite_tex_with_citekeys(
    tex_text: str,
    pick_idx: List[int],
    cite_keys: List[str],
    total_papers: int,
) -> str:
    """单遍原子将 LaTeX 正文中的课题库 [序号] 替换为 \\cite{key1, key2}（自动保护 % 注释行与 \\item[1] 等可选宏参数）。"""
    import re as _re
    key_map = {orig_0based + 1: ck for orig_0based, ck in zip(pick_idx, cite_keys)}
    out_lines: List[str] = []
    for ln in tex_text.splitlines():
        parts = _re.split(r"((?<!\\)%.*$)", ln, maxsplit=1)
        code_part = parts[0]
        comment_part = parts[1] if len(parts) > 1 else ""

        def _repl_tex(m) -> str:
            if _re.search(r"\\[A-Za-z@]+\s*$", code_part[:m.start()]):
                return m.group(0)
            expanded = _expand_bracket_tokens(m.group(1), total_papers)
            if not expanded or any(x not in key_map for x in expanded):
                return m.group(0)
            dedup_keys: List[str] = []
            for x in expanded:
                k = key_map[x]
                if k not in dedup_keys:
                    dedup_keys.append(k)
            return f"\\cite{{{', '.join(dedup_keys)}}}"

        rewritten_code = _re.sub(_DOC_CITE_BRACKET_RE, _repl_tex, code_part)
        out_lines.append(rewritten_code + comment_part)
    trailing_nl = "\n" if tex_text.endswith("\n") else ""
    return "\n".join(out_lines) + trailing_nl


def _rewrite_doc_with_sequential_refs(
    doc_text: str,
    pick_idx: List[int],
    total_papers: int,
    ref_list: str,
) -> str:
    """单遍原子重映射正文中的课题库 [序号] 为 [1]..[N]（零级联冲突），并追加 ## 参考文献。"""
    import re as _re
    body = _strip_trailing_bibliography(doc_text)
    remap = {orig_0based + 1: new_pos for new_pos, orig_0based in enumerate(pick_idx, 1)}

    def _repl(m) -> str:
        expanded = _expand_bracket_tokens(m.group(1), total_papers)
        if not expanded or any(x not in remap for x in expanded):
            return m.group(0)
        mapped = [remap[x] for x in expanded]
        return _format_sequential_bracket(mapped)

    rewritten_body = _re.sub(_DOC_CITE_BRACKET_RE, _repl, body)
    return f"{rewritten_body}\n\n## 参考文献\n\n{ref_list}\n"


def _read_any_doc_text(fp: Path) -> str:
    """读取 .md/.txt/.tex/.docx/.pdf 纯文本内容（.tex 原样读取以完整保留 LaTeX 导言区与注释）。"""
    if fp.suffix.lower() in (".md", ".markdown", ".txt", ".tex"):
        return fp.read_text(encoding="utf-8")
    captured: Dict[str, Any] = {}
    fake_args = argparse.Namespace(
        filepath=str(fp), numbered=False, toc=False, section=None, max_chars=None, output=None, raw=False
    )
    _cmd_read_paper(fake_args, output_fn=lambda payload: captured.update(payload if isinstance(payload, dict) else {}))
    return str(captured.get("text") or "")


def cmd_cite(args) -> None:
    project = _session_project(args)
    all_papers = _load_session(project)
    if not all_papers:
        _output({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可格式化的数据，请先执行 search、batch-search 或 import"})
        return

    from_file = getattr(args, "from_file", None)
    rewrite_output = getattr(args, "rewrite_output", None)
    bibtex_output = getattr(args, "bibtex_output", None)
    source_doc_text: Optional[str] = None
    is_tex_source = False
    indices_raw = getattr(args, "indices", None)

    if from_file:
        fp = Path(from_file)
        if not fp.exists():
            _output({"status": "error", "code": "FILE_NOT_FOUND", "message": f"未找到文稿文件: {from_file}"})
            return
        is_tex_source = fp.suffix.lower() == ".tex"
        source_doc_text = _read_any_doc_text(fp)
        pick_idx = _extract_pick_indices_from_doc(source_doc_text, len(all_papers), is_tex=is_tex_source)
        if not pick_idx:
            _output({
                "status": "error",
                "code": "NO_CITATIONS_IN_FILE",
                "message": f"未在 {from_file} 正文中检测到合法的 [序号] 角标（课题库范围 1-{len(all_papers)}）",
            })
            return
        indices_raw = ",".join(str(i + 1) for i in pick_idx)
    elif indices_raw:
        try:
            pick_idx = parse_indices_spec(indices_raw, len(all_papers), preserve_order=True)
        except ValueError:
            pick_idx = []
        if not pick_idx:
            _output({
                "status": "error",
                "code": "INVALID_INDICES",
                "message": f"无效序号 '{indices_raw}'，会话共 {len(all_papers)} 篇（序号 1-{len(all_papers)}）",
            })
            return
    else:
        pick_idx = list(range(len(all_papers)))

    selected_papers = [all_papers[i] for i in pick_idx]
    if not getattr(args, "no_enrich", False):
        _enrich_bibliography_subset(selected_papers, all_papers, project)

    from formatter import prepare_author_year_papers, to_bibtex_with_keys
    enriched = [all_papers[orig_i] for orig_i in pick_idx]
    incomplete_indices: List[int] = []
    keep_numbers = bool(getattr(args, "keep_numbers", False))
    style = args.style or "gbt7714"
    check_list = (
        prepare_author_year_papers(enriched)
        if style in ("gbt7714-author-year", "author-year", "chinese-econ", "econ")
        else [dict(p, _orig_pos=pick_idx[pos] + 1) for pos, p in enumerate(enriched)]
    )
    for out_pos, p in enumerate(check_list, 1):
        doc_type = str(p.get("doc_type") or "journal")
        # 中文经管体例（《经济研究》《管理世界》）中文期刊通常只标期号，不强求页码；其余样式检查缺页码
        if (
            doc_type == "journal"
            and not p.get("pages")
            and not p.get("is_online_first")
            and not (style in ("chinese-econ", "econ") and p.get("issue") and any("\u4e00" <= c <= "\u9fff" for c in str(p.get("title", ""))))
        ):
            orig_label = pick_idx[p["_orig_index"] - 1] + 1 if "_orig_index" in p else p.get("_orig_pos", out_pos)
            incomplete_indices.append(orig_label if keep_numbers else out_pos)

    ref_list = generate_reference_list(enriched, style)
    if keep_numbers and indices_raw and style in ("gbt7714", "gb"):
        ref_list = _apply_original_numbers_to_refs(ref_list, [i + 1 for i in pick_idx])

    bibtex_path: Optional[str] = None
    cite_keys: Optional[List[str]] = None
    is_tex_rewrite = bool(rewrite_output and (is_tex_source or Path(rewrite_output).suffix.lower() == ".tex"))
    if bibtex_output or is_tex_rewrite:
        bib_content, cite_keys = to_bibtex_with_keys(enriched)
        if bibtex_output:
            bib_p = Path(bibtex_output)
            bib_p.parent.mkdir(parents=True, exist_ok=True)
            bib_p.write_text(bib_content + "\n", encoding="utf-8")
            bibtex_path = str(bib_p)

    rewritten_path: Optional[str] = None
    if rewrite_output:
        if source_doc_text is None:
            _output({"status": "error", "code": "MISSING_FROM_FILE", "message": "--rewrite-output 需配合 --from-file <draft.md/tex> 使用"})
            return
        out_p = Path(rewrite_output)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        if is_tex_rewrite and cite_keys is not None:
            final_doc = _rewrite_tex_with_citekeys(source_doc_text, pick_idx, cite_keys, len(all_papers))
        elif style in ("gbt7714", "gb") and not keep_numbers:
            final_doc = _rewrite_doc_with_sequential_refs(source_doc_text, pick_idx, len(all_papers), ref_list)
        else:
            final_doc = f"{_strip_trailing_bibliography(source_doc_text)}\n\n## 参考文献\n\n{ref_list}\n"
        out_p.write_text(final_doc, encoding="utf-8")
        rewritten_path = str(out_p)

    want_in_text = bool(getattr(args, "in_text", False)) or style in (
        "gbt7714-author-year", "author-year", "chinese-econ", "econ", "apa", "mla", "chicago"
    )
    in_text_info = generate_in_text_citations(enriched, style) if want_in_text else None

    if args.raw:
        if getattr(args, "in_text", False) and in_text_info:
            print(f"# 正文合并引用: {in_text_info['combined']}")
            for it in in_text_info["items"]:
                print(f"# 句中: {it['narrative']} | 句末: {it['parenthetical']}  <- {it['title'][:36]}")
            print()
        print(ref_list)
    else:
        out: Dict[str, Any] = {
            "status": "success",
            "project": project,
            "style": style,
            "count": len(enriched),
            "references": ref_list,
        }
        if in_text_info is not None:
            out["in_text"] = in_text_info
        if indices_raw:
            out["selected_indices"] = [i + 1 for i in pick_idx]
        if from_file:
            if is_tex_rewrite and cite_keys is not None:
                out["citekey_mapping"] = {str(orig_i + 1): ck for orig_i, ck in zip(pick_idx, cite_keys)}
            else:
                out["index_mapping"] = {str(orig_i + 1): new_no for new_no, orig_i in enumerate(pick_idx, 1)}
        if rewritten_path:
            out["rewritten_file"] = rewritten_path
        if bibtex_path:
            out["bibtex_file"] = bibtex_path
        if cite_keys is not None:
            out["cite_keys"] = cite_keys
        if incomplete_indices:
            out["incomplete_indices"] = incomplete_indices
            out["warnings"] = [f"第 {', '.join(map(str, incomplete_indices))} 条期刊文献缺少页码信息，核稿时可配合 --cite-enrich 补全"]
        _output(out)


def cmd_import(args) -> None:
    target = Path(args.filepath)
    if not target.exists():
        _output({"status": "error", "code": "FILE_NOT_FOUND", "message": f"文件或目录不存在: {args.filepath}"})
        return

    is_pdf_target = target.is_dir() or target.suffix.lower() == ".pdf"
    if is_pdf_target:
        results = import_pdfs_from_path(target, crossref_resolver=resolve_crossref)
    else:
        results = parse_cnki_export(args.filepath)

    if results and not (len(results) == 1 and results[0].get("status") == "error"):
        for p in results:
            p["quality_score"] = calculate_quality_score(p)
        project = _session_project(args)
        do_append = bool(getattr(args, "append", False))
        _save_session(results, append=do_append, project=project)
        _output({
            "status": "success",
            "project": project,
            "append": do_append,
            "import_type": "pdf" if is_pdf_target else "bibliography",
            "count": len(results),
            "results": results,
        })
    else:
        _output(results[0] if results else {"status": "error", "code": "IMPORT_PARSE_FAILED", "message": "解析失败"})


def _parse_indices(raw: str, total: int) -> List[int]:
    return parse_indices_spec(raw, total, preserve_order=True)


def _is_allowed_fulltext_cache(cache_path: Path) -> bool:
    resolved = cache_path.resolve()
    for allowed_dir in allowed_fulltext_dirs():
        try:
            resolved.relative_to(allowed_dir)
            return True
        except ValueError:
            continue
    return False


def cmd_read_detail(args) -> None:
    papers = _load_session(_session_project(args))
    if not papers:
        _output({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可读取的论文，请先执行 search、batch-search 或 import"})
        return

    toc_only = bool(getattr(args, "toc", False))
    section_spec = getattr(args, "section", None)
    max_chars = getattr(args, "max_chars", None)
    do_fulltext = bool(args.fulltext or toc_only or section_spec)
    indices_raw = getattr(args, "indices", None)

    if indices_raw:
        try:
            pick_idx = _parse_indices(indices_raw, len(papers))
        except ValueError:
            pick_idx = []
        if not pick_idx:
            _output({
                "status": "error",
                "code": "INVALID_INDICES",
                "message": f"无效序号 '{indices_raw}'，会话共 {len(papers)} 篇（序号 1-{len(papers)}）",
            })
            return
        selected = [papers[i] for i in pick_idx]
        label = f"第 {indices_raw} 篇"
    else:
        top_n = args.top_n or 5
        pick_idx = list(range(min(top_n, len(papers))))
        selected = [papers[i] for i in pick_idx]
        label = f"前 {len(selected)} 篇"

    print(f"[read-detail] 会话共 {len(papers)} 篇，将获取{label}的{'全文' if do_fulltext else '摘要'}", file=sys.stderr)

    cnki_selected = [p for p in selected if _is_cnki_paper(p)]
    local_pdf_selected = [p for p in selected if not _is_cnki_paper(p) and p.get("local_pdf")]
    api_selected = [p for p in selected if not _is_cnki_paper(p) and not p.get("local_pdf")]

    if not cnki_selected and not local_pdf_selected:
        _output({
            "status": "warning",
            "code": "NO_SESSION_DATA",
            "message": "所选论文中无知网或本地 PDF 论文。API 源论文请直接使用搜索时返回的摘要",
            "count": len(api_selected),
            "results": api_selected,
        })
        return

    if cnki_selected:
        extra_rd_kw: Dict[str, Any] = {}
        if toc_only and not section_spec:
            extra_rd_kw["toc_only"] = True
        if max_chars and not section_spec:
            extra_rd_kw["max_chars"] = max_chars
        try:
            batch_read_detail(
                papers=cnki_selected,
                top_n=len(cnki_selected),
                fulltext=do_fulltext,
                **extra_rd_kw,
            )
        except TypeError:
            batch_read_detail(papers=cnki_selected, top_n=len(cnki_selected), fulltext=do_fulltext)

    if local_pdf_selected and do_fulltext:
        for lp in local_pdf_selected:
            ft = extract_fulltext_from_pdf_file(Path(lp["local_pdf"]))
            if ft:
                lp["has_fulltext"] = True
                lp["fulltext"] = ft
                lp["fulltext_length"] = len(ft)

    # 按会话原位置更新元数据（剥离大体积正文后持久化）
    updated = list(papers)
    for i in pick_idx:
        updated[i] = {k: v for k, v in updated[i].items() if k != "fulltext"}
    _save_session(updated, project=_session_project(args))

    results = []
    for p in selected:
        entry: Dict[str, Any] = {
            "title": p.get("title", ""),
            "authors": p.get("authors", ""),
            "journal": p.get("journal", ""),
            "date": p.get("date", "") or str(p.get("year") or ""),
            "abstract": p.get("abstract", ""),
            "keywords": p.get("keywords", []),
            "has_fulltext": p.get("has_fulltext", False),
            "fulltext_length": p.get("fulltext_length", 0),
        }
        if p.get("local_pdf"):
            entry["local_pdf"] = p["local_pdf"]
        if p.get("fulltext_cache"):
            entry["fulltext_cache"] = p["fulltext_cache"]

        ft_content = ""
        if do_fulltext and p.get("fulltext"):
            ft_content = p["fulltext"]
        elif do_fulltext and p.get("fulltext_cache"):
            try:
                cache_path = Path(p["fulltext_cache"])
                if _is_allowed_fulltext_cache(cache_path):
                    cache_data = json.loads(cache_path.resolve().read_text(encoding="utf-8"))
                    ft_content = cache_data.get("fulltext", "")
            except Exception:
                ft_content = ""

        if do_fulltext and ft_content:
            parsed_secs = parse_fulltext_sections(ft_content)
            toc = public_toc(parsed_secs)
            if toc_only:
                fm_toc = extract_frontmatter_toc(ft_content)
                if len(fm_toc) > len(toc) and (len(toc) < 4 or len(fm_toc) >= len(toc) * 2):
                    toc = fm_toc
            entry["toc"] = toc
            if toc_only and not section_spec:
                entry["toc_only"] = True
            elif section_spec or max_chars:
                sliced = slice_fulltext_sections(ft_content, section_spec or "", max_chars=max_chars)
                entry["matched_sections"] = sliced["matched_sections"]
                entry["sliced_length"] = sliced["chars"]
                entry["truncated"] = sliced["truncated"]
                if sliced.get("warning"):
                    entry["section_warning"] = sliced["warning"]
                entry["fulltext"] = sliced["text"]
            else:
                entry["fulltext"] = ft_content
        elif do_fulltext and toc_only and not section_spec and isinstance(p.get("native_toc"), list) and p["native_toc"]:
            entry["toc"] = p["native_toc"]
            entry["toc_only"] = True
            entry["toc_source"] = "cnki_detail_catalog"
        elif do_fulltext:
            entry["fulltext"] = ""
            if p.get("auth_required"):
                entry["auth_required"] = True
        results.append(entry)

    auth_reasons = [str(p.get("auth_reason") or "") for p in selected if p.get("auth_required")]
    out_detail: Dict[str, Any] = {
        "status": "partial" if (do_fulltext and auth_reasons) else "success",
        "count": len(results),
        "results": results,
    }
    if do_fulltext and auth_reasons:
        out_detail["code"] = "CNKI_AUTH_REQUIRED"
        out_detail["auth_warning"] = (
            auth_reasons[0]
            or "当前未检测到知网全文阅读权限，已保留全部论文免费摘要并跳过后续无效全文点击；如需全文请连接校园网/VPN 或运行 auth-cnki"
        )
    _output(out_detail)


def _is_cnki_citation_target(paper_id: str) -> bool:
    pid = (paper_id or "").strip()
    low = pid.lower()
    if "cnki.net" in low or low.startswith("cnki:"):
        return True
    if low.startswith(("10.", "doi:", "http://", "https://", "arxiv:", "w")):
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in pid)


def _normalize_snowball_items(items: List[Dict[str, Any]], relation: str) -> List[Dict[str, Any]]:
    tag_map = {"J": "journal", "D": "thesis", "M": "book", "C": "conference", "N": "newspaper", "R": "report"}
    normalized: List[Dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict) or not raw.get("title"):
            continue
        p = dict(raw)
        if "citation_count" in p and "cited_by" not in p:
            p["cited_by"] = p.get("citation_count", 0)
        p.setdefault("cited_by", 0)
        if p.get("doc_tag") and not p.get("doc_type"):
            p["doc_type"] = tag_map.get(str(p["doc_tag"]).upper(), "journal")
        cur_tags = list(p.get("tags") or [])
        rel_tag = "引证前沿" if relation == "citing" else "参考溯源"
        if rel_tag not in cur_tags:
            cur_tags.append(rel_tag)
        p["tags"] = cur_tags
        p["quality_score"] = calculate_quality_score(p)
        normalized.append(p)
    return normalized


def _maybe_save_citations_to_session(
    args,
    citing: List[Dict[str, Any]],
    references: List[Dict[str, Any]],
    extra_items: Optional[List[Dict[str, Any]]] = None,
    extra_relation: str = "",
) -> Optional[Dict[str, Any]]:
    save_mode = getattr(args, "save", None)
    if not save_mode:
        return None
    to_save: List[Dict[str, Any]] = []
    if save_mode in ("citing", "both"):
        to_save.extend(_normalize_snowball_items(citing, "citing"))
    if save_mode in ("cited", "both"):
        to_save.extend(_normalize_snowball_items(references, "cited"))
    if save_mode in ("coref", "cocited") and extra_items:
        to_save.extend(_normalize_snowball_items(extra_items, extra_relation or save_mode))
    to_save = deduplicate_results(to_save)
    project = _session_project(args)
    do_append = bool(getattr(args, "append", False))
    if to_save:
        _save_session(to_save, append=do_append, project=project)
    return {
        "mode": save_mode,
        "saved_count": len(to_save),
        "append": do_append,
        "project": project,
    }


def cmd_citations(args) -> None:
    paper_id = args.paper_id
    if not paper_id:
        _output({"status": "error", "code": "NO_PAPER_ID", "message": "请提供论文标识（DOI、知网 URL、中文题名或 arXiv ID）"})
        return
    direction = args.direction or "both"
    limit = args.limit or 20

    if _is_cnki_citation_target(paper_id):
        print(f"[citations] 查询知网论文 '{paper_id[:50]}' 的引文网络（方向: {direction}）...", file=sys.stderr)
        cnki_res = get_cnki_citations(paper_id, direction=direction, limit=limit)
        if cnki_res.get("status") != "error":
            extra = cnki_res.get("coreferences") or cnki_res.get("cocitations") or []
            saved_info = _maybe_save_citations_to_session(
                args,
                cnki_res.get("citing", []),
                cnki_res.get("references", []),
                extra_items=extra,
                extra_relation=direction,
            )
            if saved_info is not None:
                cnki_res = {**cnki_res, "saved_to_session": saved_info}
        _output(cnki_res)
        return

    if direction in ("coref", "cocited"):
        _output({
            "status": "error",
            "code": "UNSUPPORTED_DIRECTION",
            "message": f"方向 '{direction}'（共引/同被引文献）目前仅知网中文论文支持；英文论文请使用 citing / cited / both",
        })
        return

    print(f"[citations] 查询 {paper_id} 的引文网络（方向: {direction}）...", file=sys.stderr)
    result = get_citations(paper_id, direction=direction, limit=limit)
    if "error" in result:
        _output({"status": "error", "code": "RESOLVE_FAILED", "message": result["error"]})
        return
    paper = result.get("paper", {})
    citing = result.get("citing", [])
    references = result.get("references", [])
    out: Dict[str, Any] = {
        "status": "success" if paper else "partial",
        "source": "Semantic Scholar",
        "paper": paper,
        "citing_count": len(citing),
        "references_count": len(references),
        "citing": citing,
        "references": references,
    }
    saved_info = _maybe_save_citations_to_session(args, citing, references)
    if saved_info is not None:
        out["saved_to_session"] = saved_info
    _output(out)


def cmd_zotero(args) -> None:
    res = query_local_zotero(
        query=args.query or "",
        collection=args.collection or "",
        data_dir=args.data_dir,
        limit=args.limit or 30,
    )
    if getattr(args, "list_collections", False) and res.get("status") == "success":
        _output({
            "status": "success",
            "backend": res.get("backend"),
            "data_dir": res.get("data_dir"),
            "collections": res.get("collections", []),
        })
        return
    if res.get("status") == "success" and getattr(args, "save", False) and res.get("results"):
        papers = res["results"]
        for p in papers:
            p["quality_score"] = calculate_quality_score(p)
        project = _session_project(args)
        do_append = bool(getattr(args, "append", False))
        _save_session(papers, append=do_append, project=project)
        res["saved_to_session"] = {"count": len(papers), "append": do_append, "project": project}
    _output(res)


def _parse_single_reference_line(raw_line: str) -> Optional[Dict[str, Any]]:
    """解析单行 GB/T 7714、中文经管书名号体例或 APA 格式的参考文献文本为结构化字典。"""
    import re as _re
    s = raw_line.strip()
    if not s or len(s) < 10:
        return None
    s = _re.sub(r"^(?:\[\d+\]|\(\d+\)|\d+[.、)）])\s*", "", s).strip()
    if not s:
        return None

    doi = ""
    m_doi = _re.search(r"(?:https?://(?:dx\.)?doi\.org/|DOI\s*[:：]\s*)(10\.\d{4,9}/[^\s,，。；]+)", s, _re.I)
    if m_doi:
        doi = m_doi.group(1).rstrip(".。;；)")
        s = (s[:m_doi.start()] + " " + s[m_doi.end():]).strip()
    s = _re.sub(r"https?://\S+", "", s).strip().rstrip(".。")

    def _clean_authors_field(au_str: str) -> str:
        au_str = _re.sub(r"[,，;；、\s]*(?:等|et\s+al\.?)\s*$", "", au_str.strip(), flags=_re.I)
        if any("\u4e00" <= c <= "\u9fff" for c in au_str):
            parts = [p.strip() for p in _re.split(r"[,，;；、]+", au_str) if p.strip()]
            return "; ".join(parts)
        return au_str

    # 1. GB/T 7714 带 [J] / [D] / [M] / [C] / [J/OL] 文献类型标识
    m_gb = _re.match(r"^([^.。]+?)\s*[.．]\s*(.+?)\s*\[([A-Z]{1,2}(?:/[A-Z]{2})?)\]\s*[.．,，]?\s*(.*)$", s)
    if m_gb:
        authors = _clean_authors_field(m_gb.group(1))
        title = m_gb.group(2).strip()
        tag = m_gb.group(3).upper()
        tail = m_gb.group(4).strip()
        dtype = "thesis" if tag.startswith("D") else ("book" if tag.startswith("M") else ("conference" if tag.startswith("C") else "journal"))
        year = None
        ym = _re.search(r"\b((?:19|20)\d{2})\b", tail)
        if ym:
            year = int(ym.group(1))
        vol = ""
        iss = ""
        pages = ""
        m_vip = _re.search(r"(?:19|20)\d{2}\s*(?:[,，]\s*(\d+))?\s*(?:[(（]([0-9A-Za-z]{1,6})[)）])?\s*(?:[:：]\s*([0-9A-Za-z]+(?:[-–—][0-9A-Za-z]+(?:\+\d+)?)?))?", tail)
        if m_vip:
            vol = m_vip.group(1) or ""
            iss = m_vip.group(2) or ""
            pages = (m_vip.group(3) or "").replace("–", "-").replace("—", "-")
        venue = _re.split(r"[,，]\s*(?:19|20)\d{2}|[(（](?:19|20)\d{2}", tail, maxsplit=1)[0].strip().rstrip(".。,，")
        rec: Dict[str, Any] = {
            "title": title,
            "authors": authors,
            "year": year,
            "doc_type": dtype,
            "volume": vol,
            "issue": iss,
            "pages": pages,
            "doi": doi,
            "source": "CNKI" if any("\u4e00" <= c <= "\u9fff" for c in title) else "OpenAlex",
        }
        if dtype == "thesis":
            rec["school"] = venue
        else:
            rec["journal"] = venue
        return rec

    # 2. 中文经管书名号体例：赵涛、张智，2020：《数字经济...》，《管理世界》第10期
    m_econ = _re.match(
        r"^([^，,：:]+?)[，,\s]+((?:19|20)\d{2})[a-z]?\s*[：:]\s*[《“\"](.+?)[》”\"][，,\s]*[《]?([^》，,。]+?)[》]?(?:[，,\s]*第?\s*(\d+)\s*期)?",
        s,
    )
    if m_econ:
        return {
            "title": m_econ.group(3).strip(),
            "authors": _clean_authors_field(m_econ.group(1)),
            "journal": m_econ.group(4).strip(),
            "year": int(m_econ.group(2)),
            "issue": m_econ.group(5) or "",
            "doi": doi,
            "doc_type": "journal",
            "source": "CNKI",
        }

    # 3. APA / 著者(年份) 体例：Author (2021). Title. Journal, 33(2), 402-425
    m_apa = _re.match(r"^(.+?)\s*[(（]((?:19|20)\d{2})[a-z]?[)）]\s*[.．]\s*(.+?)\s*[.．]\s*(.+)$", s)
    if m_apa:
        tail_apa = m_apa.group(4).strip()
        j_parts = [p.strip() for p in tail_apa.split(",")]
        j_name = j_parts[0] if j_parts else tail_apa
        vol_a, iss_a, pg_a = "", "", ""
        if len(j_parts) >= 2:
            mv = _re.match(r"^(\d+)(?:\(([^)]+)\))?$", j_parts[1])
            if mv:
                vol_a = mv.group(1) or ""
                iss_a = mv.group(2) or ""
        if len(j_parts) >= 3 and _re.match(r"^\d+(?:[-–—]\d+)?$", j_parts[2]):
            pg_a = j_parts[2].replace("–", "-")
        title_apa = m_apa.group(3).strip()
        return {
            "title": title_apa,
            "authors": _clean_authors_field(m_apa.group(1)),
            "journal": j_name,
            "year": int(m_apa.group(2)),
            "volume": vol_a,
            "issue": iss_a,
            "pages": pg_a,
            "doi": doi,
            "doc_type": "journal",
            "source": "CNKI" if any("\u4e00" <= c <= "\u9fff" for c in title_apa) else "OpenAlex",
        }

    return None


def _extract_bibliography_from_doc_text(doc_text: str) -> List[Dict[str, Any]]:
    """从文稿全文中定位参考文献节（或纯参考文献列表）并逐行解析为待核验条目。"""
    import re as _re
    lines = doc_text.splitlines()
    ref_start = None
    for idx, ln in enumerate(lines):
        clean_ln = _re.sub(r"^[#\s*]+|[\s*:：]+$", "", ln.strip())
        if clean_ln in ("参考文献", "References", "Bibliography") and len(ln.strip()) <= 24:
            ref_start = idx + 1
            break

    cand_lines = lines[ref_start:] if ref_start is not None else lines
    parsed: List[Dict[str, Any]] = []
    for ln in cand_lines:
        stripped = ln.strip()
        if not stripped:
            continue
        if ref_start is not None and _re.match(r"^#{1,3}\s+\S+", stripped):
            break
        item = _parse_single_reference_line(stripped)
        if item and item.get("title"):
            parsed.append(item)
    return parsed


def cmd_verify_refs(args) -> None:
    """真伪核验与『作者对但题名改名/回译偏差』自动纠偏 + 缺页码补全（支持直接传 --from-file <docx/md/txt>）。"""
    from cnki.search import _extract_first_author_clean, _title_similarity

    project = _session_project(args)
    from_file = getattr(args, "from_file", None)
    fix_session = bool(getattr(args, "fix_session", False))

    if from_file:
        fp = Path(from_file)
        if not fp.exists():
            _output({"status": "error", "code": "FILE_NOT_FOUND", "message": f"未找到文稿文件: {from_file}"})
            return
        doc_txt = _read_any_doc_text(fp)
        all_papers = _extract_bibliography_from_doc_text(doc_txt)
        if not all_papers:
            _output({
                "status": "error",
                "code": "NO_REFERENCES_FOUND",
                "message": f"未能在 {from_file} 中识别到可解析的参考文献条目",
            })
            return
    else:
        all_papers = _load_session(project)
        if not all_papers:
            _output({"status": "error", "code": "NO_SESSION_DATA", "message": "会话中没有待核验文献，请先执行 search、import 或传入 --from-file"})
            return

    indices_raw = getattr(args, "indices", None)
    if indices_raw:
        try:
            pick_idx = parse_indices_spec(indices_raw, len(all_papers), preserve_order=True)
        except ValueError:
            pick_idx = []
        if not pick_idx:
            _output({"status": "error", "code": "INVALID_INDICES", "message": f"无效序号 '{indices_raw}'"})
            return
    else:
        pick_idx = list(range(len(all_papers)))

    fix_session = bool(getattr(args, "fix_session", False))
    report_items: List[Dict[str, Any]] = []
    cnki_papers_to_enrich: List[Dict[str, Any]] = []
    counts = {"verified": 0, "title_reconciled": 0, "not_found": 0}
    dirty = False

    for orig_i in pick_idx:
        p = all_papers[orig_i]
        title = str(p.get("title") or "").strip()
        authors = str(p.get("authors") or "").strip()
        journal = str(p.get("journal") or "").strip()
        year = p.get("year")
        is_cjk_item = _is_cnki_paper(p) or (any("\u4e00" <= ch <= "\u9fff" for ch in title) and not p.get("doi"))

        if is_cjk_item:
            rec = reconcile_cnki_paper(title=title, author=authors, journal=journal, year=year)
            if rec.get("matched") and isinstance(rec.get("paper"), dict):
                matched_p = rec["paper"]
                changed = bool(rec.get("title_changed"))
                st = "title_reconciled" if changed else "verified"
                counts[st] += 1
                item_rep: Dict[str, Any] = {
                    "index": orig_i + 1,
                    "status": st,
                    "method": rec.get("method"),
                    "similarity": rec.get("similarity"),
                    "title": title,
                    "authentic_title": matched_p.get("title", title),
                    "authors": matched_p.get("authors") or authors,
                    "journal": matched_p.get("journal") or journal,
                    "year": matched_p.get("year") or year,
                    "url": matched_p.get("url", ""),
                }
                if fix_session:
                    if changed:
                        p["original_unverified_title"] = title
                        p["title"] = matched_p.get("title", title)
                    for k in ("url", "journal", "year", "authors", "is_core", "core_type", "cited_by", "downloads", "volume", "issue", "pages", "doc_type"):
                        if matched_p.get(k) and (changed or not p.get(k)):
                            p[k] = matched_p[k]
                    p["source"] = p.get("source") or "CNKI"
                    dirty = True
                    if not p.get("pages") and p.get("url") and not p.get("is_online_first"):
                        cnki_papers_to_enrich.append(p)
                report_items.append(item_rep)
            else:
                counts["not_found"] += 1
                report_items.append({
                    "index": orig_i + 1,
                    "status": "not_found",
                    "title": title,
                    "authors": authors,
                    "reason": rec.get("reason", "not_found_on_cnki"),
                })
        else:
            # 英文 / DOI 文献通过 Crossref + OpenAlex 作者锚定核验
            matched_en = None
            sim_en = 0.0
            if p.get("doi"):
                cr = resolve_crossref(str(p["doi"]))
                if cr and cr.get("title"):
                    sim_en = _title_similarity(title, cr["title"]) if title else 1.0
                    matched_en = cr
            if matched_en is None and title:
                import re as _re
                first_au = _extract_first_author_clean(authors)
                # 将 GB/T 7714 / APA 缩写形式（如 'Pietrobruno S'、'Feng D W'、'Pietrobruno, S.'）收敛为纯姓氏以匹配 OpenAlex 全名
                au_query = first_au
                if au_query and not any("\u4e00" <= ch <= "\u9fff" for ch in au_query):
                    if "," in au_query:
                        au_query = au_query.split(",", 1)[0].strip()
                    else:
                        au_query = _re.sub(r"\s+(?:[A-Z]\.?\s*)+$", "", au_query).strip()
                oa_hits = search_openalex(title, limit=5, author=au_query or None)
                if not oa_hits and au_query:
                    # 英文题名若被改写，按作者姓氏 + 题名中最具区分度的 2 个长实词回查
                    sig_words = sorted(
                        {_re.sub(r"[^A-Za-z0-9]", "", w) for w in title.split() if len(w) > 4},
                        key=len,
                        reverse=True,
                    )[:2]
                    yr_int = int(year) if str(year or "").isdigit() else None
                    oa_hits = search_openalex(
                        " ".join(sig_words) if sig_words else au_query,
                        limit=15,
                        author=au_query,
                        year_from=(yr_int - 1) if yr_int else None,
                        year_to=(yr_int + 1) if yr_int else None,
                    )
                for cand in oa_hits:
                    sc = _title_similarity(title, cand.get("title", ""))
                    if sc > sim_en:
                        sim_en = sc
                        matched_en = cand

            if matched_en and sim_en >= 0.45:
                import re as _re
                got_en_title = str(matched_en.get("title") or "").strip()
                changed = bool(got_en_title) and (
                    _re.sub(r"\W+", "", title).lower() != _re.sub(r"\W+", "", got_en_title).lower()
                )
                st = "title_reconciled" if changed else "verified"
                counts[st] += 1
                if fix_session:
                    if changed:
                        p["original_unverified_title"] = title
                        p["title"] = matched_en.get("title", title)
                    for k in ("volume", "issue", "pages", "doi", "journal", "year", "authors"):
                        if matched_en.get(k) and (changed or not p.get(k)):
                            p[k] = matched_en[k]
                    dirty = True
                report_items.append({
                    "index": orig_i + 1,
                    "status": st,
                    "similarity": round(sim_en, 3),
                    "title": title,
                    "authentic_title": matched_en.get("title", title),
                    "pages": matched_en.get("pages") or p.get("pages", ""),
                    "doi": matched_en.get("doi") or p.get("doi", ""),
                })
            else:
                counts["not_found"] += 1
                report_items.append({
                    "index": orig_i + 1,
                    "status": "not_found",
                    "title": title,
                    "authors": authors,
                })

    if fix_session and cnki_papers_to_enrich:
        batch_read_detail(papers=cnki_papers_to_enrich, top_n=len(cnki_papers_to_enrich), fulltext=False)
        for item_rep in report_items:
            orig_p = all_papers[item_rep["index"] - 1]
            if orig_p.get("pages"):
                item_rep["pages"] = orig_p["pages"]
        dirty = True

    if dirty or (from_file and fix_session):
        _save_session(all_papers, project=project)

    overall = "warning" if (counts["not_found"] > 0 or counts["title_reconciled"] > 0) else "success"
    out_payload: Dict[str, Any] = {
        "status": overall,
        "project": project,
        "fixed_session": fix_session and (dirty or bool(from_file)),
        "summary": counts,
        "results": report_items,
    }
    if fix_session:
        subset_for_fmt = [all_papers[i] for i in pick_idx]
        out_payload["formatted_references"] = generate_reference_list(subset_for_fmt, "gbt7714").splitlines()
    _output(out_payload)


def cmd_close_browser(_args) -> None:
    _output(close_shared_browser())


def cmd_data_table(args) -> None:
    _cmd_data_table(args, output_fn=_output)


def cmd_match_journal(args) -> None:
    _cmd_match_journal(args, output_fn=_output)


def cmd_rebuttal(args) -> None:
    _cmd_rebuttal(args, output_fn=_output)


def cmd_trends(args) -> None:
    papers = _load_session(_session_project(args))
    if not papers:
        _output({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可分析的论文，请先执行 search、batch-search 或 import"})
        return
    print(f"[trends] 分析 {len(papers)} 篇论文的研究趋势...", file=sys.stderr)
    result = analyze_trends(papers)
    _output({"status": "success", "project": _session_project(args), **result})


def cmd_workflows(args) -> None:
    from workflows import (
        get_workflow,
        list_workflows,
        render_workflow,
        render_workflow_argv,
        validate_workflow_requirements,
    )

    if args.list:
        wfs = list_workflows()
        _output({"status": "success", "count": len(wfs), "workflows": wfs})
    elif args.execute:
        workflow_id = args.execute
        workflow = get_workflow(workflow_id)
        if not workflow:
            _output({
                "status": "error",
                "code": "WORKFLOW_NOT_FOUND",
                "message": f"未找到工作流: {workflow_id}",
                "available_workflows": [w["id"] for w in list_workflows()],
            })
            return

        variables: Dict[str, str] = {}
        if args.variables:
            try:
                variables = json.loads(args.variables)
            except json.JSONDecodeError as e:
                _output({"status": "error", "code": "INVALID_VARIABLES", "message": f"变量 JSON 格式错误: {e}"})
                return

        missing_vars = [v for v in workflow["variables"] if v not in variables]
        if missing_vars:
            _output({
                "status": "error",
                "code": "MISSING_VARIABLES",
                "message": f"缺少必需变量: {', '.join(missing_vars)}",
                "required_variables": workflow["variables"],
            })
            return

        reqs = workflow.get("requirements") or []
        capabilities: Dict[str, Any] = {}
        if not args.dry_run and reqs:
            if "cnki_feasible" in reqs:
                ok_br, _ = _check_browser()
                ok_net, msg_net = _check_cnki()
                capabilities["cnki_feasible"] = bool(ok_br and (ok_net or "提权" in msg_net))
            if "docx_tools" in reqs:
                try:
                    import docx  # noqa: F401
                    capabilities["docx_tools"] = True
                except ImportError:
                    capabilities["docx_tools"] = False

            validation = validate_workflow_requirements(workflow_id, capabilities)
            if not validation["satisfied"]:
                _output({
                    "status": "error",
                    "code": "REQUIREMENTS_NOT_MET",
                    "message": "工作流前置条件不满足",
                    "missing": validation["missing"],
                    "suggestions": validation["suggestions"],
                })
                return

        commands = render_workflow(workflow_id, variables)
        command_argvs = render_workflow_argv(workflow_id, variables)

        if args.dry_run:
            _output({
                "status": "success",
                "workflow_id": workflow_id,
                "workflow_name": workflow["name"],
                "commands": commands,
                "estimated_time_seconds": workflow.get("estimated_time_seconds", 10),
            })
            return

        results = []
        for i, (cmd, command_argv) in enumerate(zip(commands, command_argvs)):
            print(f"[workflow] 步骤 {i + 1}/{len(commands)}: {cmd}", file=sys.stderr)
            result = subprocess.run(
                [sys.executable, __file__] + command_argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                step_result = json.loads(result.stdout)
            except Exception:
                step_result = {"status": "error", "message": result.stdout or result.stderr}

            results.append({"step": i + 1, "command": cmd, "result": step_result})
            if step_result.get("status") == "error":
                _output({
                    "status": "error",
                    "code": "WORKFLOW_STEP_FAILED",
                    "message": f"工作流在步骤 {i + 1} 失败",
                    "failed_step": i + 1,
                    "failed_command": cmd,
                    "results": results,
                })
                return

        _output({
            "status": "success",
            "workflow_id": workflow_id,
            "workflow_name": workflow["name"],
            "steps_completed": len(results),
            "results": results,
        })
    else:
        _output({"status": "error", "code": "NO_ACTION", "message": "请指定 --list 或 --execute <workflow_id>"})


# ── 子模块命令委托（保留模块级钩子供单元测试 monkeypatch） ───────────────

_check_browser = _doctor.check_browser
_check_driver = _doctor.check_driver
_check_cnki = _doctor.check_cnki
_fix_sandbox_network = _doctor.fix_sandbox_network
_fetch_json = _doctor.fetch_json


def _check_update() -> Optional[Dict[str, Any]]:
    return _doctor.check_update(__version__, fetch_fn=_fetch_json)


def cmd_check(args) -> None:
    _doctor.run_check(
        args,
        version=__version__,
        check_browser_fn=_check_browser,
        check_driver_fn=_check_driver,
        check_cnki_fn=_check_cnki,
        check_update_fn=_check_update,
        output_fn=_output,
    )


def cmd_clean_cache(args) -> None:
    _doctor.cmd_clean_cache(args, output_fn=_output)


def cmd_read_paper(args) -> None:
    _cmd_read_paper(args, output_fn=_output)


def cmd_pdf_meta(args) -> None:
    _cmd_pdf_meta(args, output_fn=_output, crossref_resolver=resolve_crossref)


def cmd_write_docx(args) -> None:
    _cmd_write_docx(args, output_fn=_output)


def cmd_patch_docx(args) -> None:
    _cmd_patch_docx(args, output_fn=_output)


def cmd_format_audit(args) -> None:
    _cmd_format_audit(args, output_fn=_output)


def cmd_format_docx(args) -> None:
    _cmd_format_docx(args, output_fn=_output)


def cmd_review(args) -> None:
    _cmd_review(args, output_fn=_output)


def cmd_write(args) -> None:
    _cmd_write(args, output_fn=_output)


def cmd_validate(args) -> None:
    _cmd_validate(args, output_fn=_output)


def cmd_topics(args) -> None:
    _cmd_topics(args, output_fn=_output)


def cmd_projects(args) -> None:
    _cmd_projects(args, output_fn=_output)


def cmd_library(args) -> None:
    _cmd_library(args, output_fn=_output)


# ── CLI 解析 ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(prog="literature", description="Scholar Kit - 学术文献检索工具")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    # search
    p_search = sub.add_parser("search", help="搜索文献")
    p_search.add_argument("query", nargs="?", default="", help="搜索关键词（指定 --author / --journal / --affiliation / --expr 时可省略）")
    p_search.add_argument("--source", default="cnki",
                          help="数据源: cnki/openalex/semantic/arxiv/pubmed/nssd/dblp/base/all (默认 cnki)")
    p_search.add_argument("--limit", type=int, default=None, help="结果数量限制（默认 20，多页时自动扩展）")
    p_search.add_argument("--core", help="核心/索引筛选（逗号分隔）：知网支持 北大核心,CSSCI,AMI,WJCI,CSCD,EI；英文源支持 SCI,SSCI,AHCI,ESCI,CCF,CCF-A,CCF-B,CCF-C")
    p_search.add_argument("--exclude-warning", action="store_true", help="自动剔除中科院《国际期刊预警名单》在榜风险期刊")
    p_search.add_argument("--doc-type",
                          choices=["journal", "master", "doctor", "thesis", "conference", "newspaper"],
                          help="文献类型筛选: journal/master/doctor/thesis/conference/newspaper")
    p_search.add_argument("--field", default=None,
                          help="搜索字段: 主题(默认)/篇关摘/篇名/关键词/摘要/全文/作者/第一作者/机构/来源/基金/DOI")
    p_search.add_argument("--year-from", type=int, help="起始年份")
    p_search.add_argument("--year-to", type=int, help="截止年份")
    p_search.add_argument("--author", help="作者（知网高级/专业检索 AU=）")
    p_search.add_argument("--first-author", help="第一作者（知网高级/专业检索 FI=）")
    p_search.add_argument("--affiliation", help="作者单位/机构（知网高级/专业检索 AF%%=）")
    p_search.add_argument("--journal", help="期刊/文献来源（知网高级/专业检索 LY%%=，带《》时精确匹配）")
    p_search.add_argument("--fund", help="基金资助（知网高级/专业检索 FU%%=）")
    p_search.add_argument("--expr", help="知网专业检索布尔表达式，如 \"TKA=('大模型' + '生成式AI') AND LY%%='管理世界'\"")
    p_search.add_argument("--professional", metavar="表达式",
                          help="知网官方专业检索表达式（如 \"SU %%= 知识管理 AND AU = 邱均平\"；与 --condition / --expr 互斥）")
    p_search.add_argument("--condition", action="append", metavar="字段|逻辑|匹配|值",
                          help="知网结构化高级检索条件，可重复（最多 10 条），支持 并且/或者/不含 与 精确/模糊（自动加括号编译）")
    p_search.add_argument("--match", default=None,
                          help="知网单字段匹配方式: 精确/模糊（主题仅支持相关度匹配）")
    p_search.add_argument("--cross-language", choices=["on", "off"], default=None,
                          help="知网中英文扩展开关: on 启用（知网默认），off 关闭以仅检索纯中文文献")
    p_search.add_argument("--fuzzy", action="store_true", help="知网检索启用模糊/语义扩展匹配（%%=）")
    p_search.add_argument("--author-filter", help="作者过滤（API 源）")
    p_search.add_argument("--journal-filter", help="期刊过滤（API 源）")
    p_search.add_argument("--field-of-study", help="学科领域过滤（API 源）")
    p_search.add_argument("--page", type=int, default=1, help="页码（API 源分页，默认第 1 页）")
    p_search.add_argument("--sort", choices=["relevance", "date", "citations", "downloads", "quality"],
                          default="relevance", help="排序方式（downloads 仅知网支持）")
    p_search.add_argument("--pages", type=int, default=1, help="知网抓取页数")
    p_search.add_argument("--per-page", type=int, choices=[10, 20, 50], default=None,
                          help="知网单页显示条数（10/20/50；limit>20 时自动切 50）")
    p_search.add_argument("--bulk-abstract", action="store_true",
                          help="强制切换 50 条/页并批量导出题录摘要（未登录时额外详情补全前 5 篇）")
    p_search.add_argument("--no-bulk-abstract", action="store_true",
                          help="关闭知网检索结果的自动无感 Refworks 题录摘要/页码补全")
    p_search.add_argument("--enable-fallback", action="store_true",
                          help="启用 API 降级：主数据源失败时自动切换到备用数据源")
    p_search.add_argument("--async-search", action="store_true",
                          help="启用异步并发搜索（仅 --source all 时有效）")
    p_search.add_argument("--export", help="直接导出: bibtex/ris/markdown/json/excel")
    p_search.add_argument("--output", help="导出文件路径")
    p_search.add_argument("--download", action="store_true", help="搜索后直接下载（仅 --source cnki）")
    p_search.add_argument("--download-dir", default="./papers", help="下载目录（配合 --download，默认 ./papers）")
    p_search.add_argument("--download-top-n", type=int, default=None, help="下载前 N 篇（配合 --download，默认全部）")
    p_search.add_argument("--download-file-format", choices=["pdf", "caj"], default="pdf",
                          help="下载文件格式（配合 --download，默认 pdf）")
    p_search.add_argument("--download-fallback-format", "--fallback-format",
                          dest="download_fallback_format", choices=["caj"], default=None,
                          help="主格式失败时的兜底格式；例如 PDF 按钮不存在时尝试 CAJ")
    p_search.add_argument("--download-citation-style", choices=CITATION_STYLE_CHOICES,
                          default="gbt7714", help="下载清单引用格式（配合 --download）")
    p_search.add_argument("--download-report-output", help="下载清单输出路径（配合 --download，默认写入下载目录）")
    p_search.add_argument("--download-no-report", action="store_true", help="不生成下载清单（配合 --download）")
    p_search.add_argument("--enrich", type=int, default=0, metavar="N",
                          help="自动补全前 N 篇知网论文的卷期页码（需访问详情页）")
    p_search.add_argument("--cite-enrich", type=int, default=0, metavar="N",
                          help="搜索时点击前 N 篇知网引用按钮，快速补全 GB/T 引用和页码")
    p_search.add_argument("--append", action="store_true", help="追加到已有会话结果（而非覆盖）")
    p_search.add_argument("--project", help="课题文献库名称；指定后读写 ./.scholar-kit/projects/<project>/session.json")
    p_search.set_defaults(func=cmd_search)

    # download
    p_download = sub.add_parser("download", help="下载论文")
    p_download.add_argument("target", nargs="?", help="知网论文 URL")
    p_download.add_argument("--doi", help="通过 DOI 查找 OA 链接")
    p_download.add_argument("--dir", default="./papers", help="保存目录")
    p_download.add_argument("--file-format", choices=["pdf", "caj"], default="pdf")
    p_download.set_defaults(func=cmd_download)

    # detail
    p_detail = sub.add_parser("detail", help="获取知网论文详情")
    p_detail.add_argument("url", help="知网论文详情页 URL")
    p_detail.set_defaults(func=cmd_detail)

    # auth-cnki
    p_auth = sub.add_parser("auth-cnki", help="打开知网校外认证入口并保存浏览器会话")
    p_auth.add_argument("--auth-url", default="https://fsso.cnki.net/",
                        help="校外认证入口 URL，默认 CNKI FSSO；也可传学校图书馆/VPN/CARSI 入口")
    p_auth.add_argument("--verify-url", default="https://kns.cnki.net/",
                        help="登录后用于确认访问的知网页面，默认 https://kns.cnki.net/")
    p_auth.add_argument("--institution", help="学校/机构名称；传入后尝试在 FSSO 页面自动选择")
    p_auth.add_argument("--wait-seconds", type=int, default=180, help="等待用户完成认证的秒数，默认 180")
    p_auth.add_argument("--captcha-timeout", type=int, default=180, help="等待安全验证完成的秒数，设为 0 可跳过")
    p_auth.add_argument("--direct-domain", action="append", default=[],
                        help="追加需要直连的学校认证/VPN 域名，可重复传入")
    p_auth.add_argument("--debug-snapshot", nargs="?", const=".scholar-kit/cnki-auth-snapshot.html",
                        help="保存认证后页面 HTML 快照")
    p_auth.add_argument("--keep-browser", action="store_true", help="完成后保留浏览器窗口")
    p_auth.add_argument("--force", action="store_true", help="即使检测到已有机构会话，也重新打开认证入口")
    p_auth.set_defaults(func=cmd_auth_cnki)

    # export
    p_export = sub.add_parser("export", help="导出会话或指定序号文献")
    p_export.add_argument("--format", dest="export_format", required=True,
                          choices=["bibtex", "ris", "markdown", "json", "excel"] + CITATION_STYLE_CHOICES)
    p_export.add_argument("--indices", help="仅导出指定序号文献（按传入顺序，如 '1,3,7' 或 '1-5'）")
    p_export.add_argument("--no-enrich", action="store_true", help="导出引用格式时不自动访问知网/Crossref 补全缺页码")
    p_export.add_argument("--output", help="输出文件路径")
    p_export.add_argument("--raw", action="store_true", help="输出纯文本而非 JSON")
    p_export.add_argument("--project", help="课题文献库名称")
    p_export.set_defaults(func=cmd_export)

    # projects & library
    p_projects = sub.add_parser("projects", help="列出课题文献库")
    p_projects.set_defaults(func=cmd_projects)

    p_library = sub.add_parser("library", help="查看、精选、去重或批注课题文献库")
    p_library.add_argument("--project", help="课题文献库名称")
    p_library.add_argument("--limit", type=int, help="最多显示前 N 篇")
    p_library.add_argument("--keep", help="仅保留指定序号（按传入顺序重排，如 '1,3,5-8'）")
    p_library.add_argument("--remove", help="剔除指定序号（如 '4,9,12-15'）")
    p_library.add_argument("--dedupe", action="store_true", help="合并重复文献（保留更完整字段与标签批注）")
    p_library.add_argument("--indices", help="待标注的目标文献序号（配合 --tag / --note / --clear-tags）")
    p_library.add_argument("--tag", help="为 --indices 文献添加标签（逗号分隔，如 '核心实证,机制检验'）")
    p_library.add_argument("--clear-tags", action="store_true", help="清空 --indices 文献已有标签")
    p_library.add_argument("--note", help="为 --indices 文献写入阅读批注")
    p_library.set_defaults(func=cmd_library)

    # cite & import
    p_cite = sub.add_parser("cite", help="生成引用格式（支持顺序编码制、著者-出版年制、经管顶刊体例与文内引文）")
    p_cite.add_argument("--style", default="gbt7714", choices=CITATION_STYLE_CHOICES,
                        help="引用样式: gbt7714(顺序编码)/gbt7714-author-year(国标著者-出版年)/chinese-econ(经管社科顶刊)/apa/mla/chicago/footnote")
    p_cite.add_argument("--indices", help="仅格式化指定序号文献（顺序编码制按传入顺序；著者-出版年制自动按中前英后拼音排序）")
    p_cite.add_argument("--from-file", help="从 .md/.tex/.docx 正文自动按首次出现顺序提取 [序号] 文献子集（自动跳过文末已有参考文献节）")
    p_cite.add_argument("--rewrite-output", help="配合 --from-file：Markdown 单遍原子重排 [序号] 为 [1]..[N] 并附参考文献；LaTeX (.tex) 自动替换为 \\cite{key}")
    p_cite.add_argument("--bibtex-output", help="同步导出所选文献子集的纯 ASCII Citekey BibTeX 文件（如 refs.bib，完美搭配 .tex 写作）")
    p_cite.add_argument("--keep-numbers", action="store_true", help="配合 --indices 时保留原文献库序号而非从 [1] 重排")
    p_cite.add_argument("--in-text", action="store_true", help="同时生成正文内嵌引文格式（句中叙述式与句末括号式）")
    p_cite.add_argument("--no-enrich", action="store_true", help="跳过自动联网补全缺页码")
    p_cite.add_argument("--raw", action="store_true", help="输出纯文本而非 JSON")
    p_cite.add_argument("--project", help="课题文献库名称")
    p_cite.set_defaults(func=cmd_cite)

    p_import = sub.add_parser("import", help="导入题录文件（NoteExpress/Refworks/BibTeX）或本地 PDF 文件/目录")
    p_import.add_argument("filepath", help="题录文件路径、单篇 PDF 路径或包含多篇 PDF 的文件夹路径")
    p_import.add_argument("--append", action="store_true", help="追加到当前会话或课题文献库（而非覆盖）")
    p_import.add_argument("--project", help="课题文献库名称")
    p_import.set_defaults(func=cmd_import)

    # read-paper & pdf-meta
    p_paper = sub.add_parser("read-paper", help="读取论文文件（.docx/.tex/.md/.txt/.pdf）并支持段落锚点与按章节切片")
    p_paper.add_argument("filepath", help="论文文件路径")
    p_paper.add_argument("--numbered", action="store_true", help="输出带 [P1] / [T1R1] 段落锚点编号的文本，供 patch-docx 精确定位")
    p_paper.add_argument("--toc", action="store_true", help="仅提取文档章节目录、页码与各章字数概览")
    p_paper.add_argument("--section", help="按章节标题关键词或 S序号精准切片读取（逗号分隔，如 '研究设计,实证分析'）")
    p_paper.add_argument("--max-chars", type=int, help="返回文本最大字符数上限")
    p_paper.add_argument("--output", help="输出到文件")
    p_paper.add_argument("--raw", action="store_true", help="输出纯文本而非 JSON")
    p_paper.set_defaults(func=cmd_read_paper)

    p_pdf = sub.add_parser("pdf-meta", help="从 PDF 提取元数据（标题、DOI 等）")
    p_pdf.add_argument("filepath", help="PDF 文件路径")
    p_pdf.set_defaults(func=cmd_pdf_meta)

    # batch-search
    p_batch = sub.add_parser("batch-search", help="批量知网搜索（一次启动浏览器）")
    p_batch.add_argument("queries", nargs="*", help="搜索关键词列表")
    p_batch.add_argument("--query-file", help="关键词文件路径（每行一个关键词）")
    p_batch.add_argument("--core", help="知网侧边栏来源类别，逗号分隔: 北大核心,CSSCI,AMI,WJCI,CSCD,EI")
    p_batch.add_argument("--doc-type",
                         choices=["journal", "master", "doctor", "thesis", "conference", "newspaper"],
                         help="文献类型筛选")
    p_batch.add_argument("--field", default=None, help="搜索字段: 主题(默认)/篇关摘/篇名/关键词/摘要/全文/作者/第一作者/机构/来源/基金")
    p_batch.add_argument("--author", help="作者")
    p_batch.add_argument("--first-author", help="第一作者")
    p_batch.add_argument("--affiliation", help="作者单位/机构")
    p_batch.add_argument("--journal", help="期刊名")
    p_batch.add_argument("--fund", help="基金资助")
    p_batch.add_argument("--match", default=None, help="知网匹配方式: 精确/模糊（主题不支持）")
    p_batch.add_argument("--cross-language", choices=["on", "off"], default=None,
                         help="知网中英文扩展开关: on 启用，off 关闭")
    p_batch.add_argument("--fuzzy", action="store_true", help="启用模糊/语义扩展匹配（%%=）")
    p_batch.add_argument("--year-from", type=int, help="起始年份")
    p_batch.add_argument("--year-to", type=int, help="截止年份")
    p_batch.add_argument("--sort", choices=["relevance", "date", "citations", "downloads", "quality"],
                         default="relevance", help="排序方式")
    p_batch.add_argument("--pages", type=int, default=1, help="每组关键词抓取页数")
    p_batch.add_argument("--no-bulk-abstract", action="store_true",
                         help="关闭知网批量检索的自动无感 Refworks 题录摘要/页码补全")
    p_batch.add_argument("--export", help="直接导出: bibtex/ris/markdown/json/excel")
    p_batch.add_argument("--output", help="导出文件路径")
    p_batch.add_argument("--append", action="store_true", help="追加到已有会话结果（而非覆盖）")
    p_batch.add_argument("--project", help="课题文献库名称")
    p_batch.set_defaults(func=cmd_batch_search)

    # read-detail
    p_read = sub.add_parser("read-detail", help="批量获取论文摘要/全文（支持硕博论文 --toc 目录预览与 --section 章节切片）")
    p_read.add_argument("--top-n", type=int, default=5, help="获取前 N 篇论文的详情（默认 5）")
    p_read.add_argument("--indices", type=str, default=None, help="指定论文序号（从1开始），如 '3' '1,3,9' '2-5'")
    p_read.add_argument("--fulltext", action="store_true", help="抓取全文（指定 --toc 或 --section 时自动启用）")
    p_read.add_argument("--toc", action="store_true", help="仅返回全文目录结构与各章节页码/字数，不输出庞大全文（适合十万字硕博论文）")
    p_read.add_argument("--section", type=str, default=None, help="按章节标题关键词、章号或 S序号精准切片精读（如 '研究设计,实证分析' 或 '第3章,4.2'）")
    p_read.add_argument("--max-chars", type=int, default=None, help="单篇返回正文最大字符数上限")
    p_read.add_argument("--project", help="课题文献库名称")
    p_read.set_defaults(func=cmd_read_detail)

    # batch-download
    p_bdl = sub.add_parser("batch-download", help="批量下载知网论文（一次启动浏览器）")
    p_bdl.add_argument("urls", nargs="*", help="知网论文 URL 列表")
    p_bdl.add_argument("--from-session", action="store_true", help="从上次搜索结果读取 URL")
    p_bdl.add_argument("--top-n", type=int, help="配合 --from-session，只下载前 N 篇")
    p_bdl.add_argument("--dir", default="./papers", help="保存目录")
    p_bdl.add_argument("--file-format", choices=["pdf", "caj"], default="pdf")
    p_bdl.add_argument("--fallback-format", choices=["caj"], default=None, help="主格式失败时的兜底格式")
    p_bdl.add_argument("--citation-style", choices=CITATION_STYLE_CHOICES, default="gbt7714", help="下载清单引用格式")
    p_bdl.add_argument("--report-output", help="下载清单输出路径")
    p_bdl.add_argument("--no-report", action="store_true", help="不生成下载清单")
    p_bdl.add_argument("--project", help="课题文献库名称")
    p_bdl.set_defaults(func=cmd_batch_download)

    # write-docx & patch-docx
    p_wdocx = sub.add_parser("write-docx", help="Markdown → 学术格式 Word 文档（原生三线表、上标角标、摘要楷体与页脚页码）")
    p_wdocx.add_argument("filepath", help="Markdown 文件路径")
    p_wdocx.add_argument("--output", help="输出 .docx 路径")
    p_wdocx.add_argument("--no-superscript", action="store_true", help="关闭正文顺序编码 [1] 自动转 Word 原生上标")
    p_wdocx.set_defaults(func=cmd_write_docx)

    p_pdocx = sub.add_parser("patch-docx", help="在现有 .docx 上打补丁（支持段落锚点、表格替换与 --dry-run 预检）")
    p_pdocx.add_argument("filepath", help="原始 .docx 文件路径")
    p_pdocx.add_argument("--patch", required=True, help="补丁 JSON 文件路径")
    p_pdocx.add_argument("--output", help="输出路径")
    p_pdocx.add_argument("--dry-run", action="store_true", help="仅预检所有替换和脚注定位是否唯一命中，不写入文件")
    p_pdocx.set_defaults(func=cmd_patch_docx)

    p_faudit = sub.add_parser("format-audit", help="审计现有 .docx 学位论文格式（支持学校模板、JSON 或自然语言规范）")
    p_faudit.add_argument("filepath", help="待审计的 .docx 论文文件")
    p_faudit.add_argument("--template", help="用于自动提取格式规范的 .docx 范例模板")
    p_faudit.add_argument("--spec", help="格式规范 JSON 或文本文件路径")
    p_faudit.add_argument("--spec-text", help="自然语言描述的格式规范文本（如 '正文小四宋体1.5倍行距'）")
    p_faudit.add_argument("--format", choices=["json", "markdown"], default="json", help="报告文件输出格式")
    p_faudit.add_argument("-o", "--output", help="报告输出路径")
    p_faudit.add_argument("--raw", action="store_true", help="直接输出 Markdown 审计报告")
    p_faudit.set_defaults(func=cmd_format_audit)

    p_fdocx = sub.add_parser("format-docx", help="按规范套用学位论文格式并生成安全副本（绝不覆盖原稿）")
    p_fdocx.add_argument("filepath", help="待格式化的 .docx 论文文件")
    p_fdocx.add_argument("--template", help="用于自动提取格式规范的 .docx 范例模板")
    p_fdocx.add_argument("--spec", help="格式规范 JSON 或文本文件路径")
    p_fdocx.add_argument("--spec-text", help="自然语言描述的格式规范文本")
    p_fdocx.add_argument("-o", "--output", help="输出 .docx 路径（默认生成 *_formatted.docx）")
    p_fdocx.add_argument("--report-output", help="格式化对比清单输出路径")
    p_fdocx.add_argument("--report-format", choices=["json", "markdown"], default="json", help="格式化清单输出格式")
    p_fdocx.add_argument("--raw", action="store_true", help="直接输出 Markdown 格式化报告")
    p_fdocx.set_defaults(func=cmd_format_docx)

    # clean-cache
    p_clean = sub.add_parser("clean-cache", help="安全清理过期缓存文件")
    p_clean.add_argument("--all", action="store_true", dest="clean_all", help="删除所有缓存（保护登录态与课题库）")
    p_clean.add_argument("--dry-run", action="store_true", help="仅统计，不实际删除")
    p_clean.set_defaults(func=cmd_clean_cache)

    # citations & trends
    p_cite_net = sub.add_parser("citations", help="引文网络分析（支持知网引证/参考/共引/同被引及英文 DOI/arXiv ID，支持滚雪球入库）")
    p_cite_net.add_argument("paper_id", help="论文标识（知网详情 URL、中文论文题名、DOI、Semantic Scholar URL、arXiv ID）")
    p_cite_net.add_argument("--direction", choices=["citing", "cited", "both", "coref", "cocited"], default="both",
                            help="方向: citing(引证)/cited(参考)/both(双向)/coref(知网共引)/cocited(知网同被引)")
    p_cite_net.add_argument("--limit", type=int, default=20)
    p_cite_net.add_argument("--save", choices=["citing", "cited", "both", "coref", "cocited"], default=None,
                            help="将抓取到的引文直接存入会话或课题库")
    p_cite_net.add_argument("--append", action="store_true", help="配合 --save 追加到现有课题文献库")
    p_cite_net.add_argument("--project", help="配合 --save 写入指定课题文献库")
    p_cite_net.set_defaults(func=cmd_citations)

    # zotero & verify-refs
    p_zotero = sub.add_parser("zotero", help="检索或导入本机 Zotero 文献库（零依赖读取 zotero.sqlite 或本地 API）")
    p_zotero.add_argument("query", nargs="?", default="", help="过滤关键词（匹配标题、作者、期刊、摘要、标签）")
    p_zotero.add_argument("--collection", help="仅读取指定名称的 Zotero 分类文件夹")
    p_zotero.add_argument("--data-dir", help="自定义 Zotero 数据目录（默认自动探测 ~/Zotero）")
    p_zotero.add_argument("--limit", type=int, default=30, help="最多读取条数（默认 30）")
    p_zotero.add_argument("--list-collections", action="store_true", help="仅列出本机 Zotero 所有分类文件夹")
    p_zotero.add_argument("--save", action="store_true", help="将结果存入当前会话或指定 --project 课题库")
    p_zotero.add_argument("--append", action="store_true", help="配合 --save 追加到现有课题库")
    p_zotero.add_argument("--project", help="课题文献库名称")
    p_zotero.set_defaults(func=cmd_zotero)

    p_vrefs = sub.add_parser("verify-refs", help="核验会话或文稿参考文献真伪，自动找回『作者正确但题名改名/回译偏差』的真实题名并补全页码")
    p_vrefs.add_argument("--from-file", help="直接从 .docx/.md/.txt 文稿末尾提取参考文献节逐条核验")
    p_vrefs.add_argument("--indices", help="仅核验指定序号（如 '1,3,5-8'，默认全部）")
    p_vrefs.add_argument("--fix-session", action="store_true", help="自动将偏差题名修正为官方真实题名、补齐缺失卷期页码并输出 formatted_references")
    p_vrefs.add_argument("--project", help="课题文献库名称")
    p_vrefs.set_defaults(func=cmd_verify_refs)

    p_trends = sub.add_parser("trends", help="研究趋势分析（基于会话中的搜索结果）")
    p_trends.add_argument("--project", help="课题文献库名称")
    p_trends.set_defaults(func=cmd_trends)

    # review / write / validate / topics
    p_review = sub.add_parser("review", help="基于会话/课题文献库生成可追溯综述证据包")
    p_review.add_argument("--topic", help="综述主题；默认使用 --project 或当前课题")
    p_review.add_argument("--project", help="课题文献库名称")
    p_review.add_argument("--limit", type=int, default=12, help="最多纳入前 N 篇相关文献（默认 12）")
    p_review.add_argument("-o", "--output", help="输出 Markdown 文件路径")
    p_review.add_argument("--auto-detail", action="store_true", help="生成综述前自动补全高相关知网文献摘要")
    p_review.add_argument("--detail-top-n", type=int, default=5, help="配合 --auto-detail，最多补全 N 篇（默认 5）")
    p_review.add_argument("--cluster", action="store_true", help="按语料关键词动态聚类并检测正负向结论分歧")
    p_review.add_argument("--gaps", action="store_true", help="基于当前文献库统计生成覆盖度与空白提示")
    p_review.add_argument("--verify-gaps", action="store_true", help="对候选空白发起外部数据库实时检索计数核验，附带真实命中篇数证明")
    p_review.add_argument("--raw", action="store_true", help="直接输出 Markdown 文本")
    p_review.set_defaults(func=cmd_review)

    p_write = sub.add_parser("write", help="基于课题文献库生成结构化综述证据骨架")
    p_write.add_argument("--project", help="课题文献库名称")
    p_write.add_argument("--topic", help="写作主题；默认使用 --project 或当前课题")
    p_write.add_argument("--limit", type=int, default=12, help="最多纳入前 N 篇相关文献（默认 12）")
    p_write.add_argument("--format", choices=["markdown", "md", "docx"], default="markdown")
    p_write.add_argument("--mode", choices=["outline", "draft", "section"], default="draft")
    p_write.add_argument("--section", help="只生成指定章节")
    p_write.add_argument("-o", "--output", help="输出文件路径")
    p_write.add_argument("--with-citations", action="store_true", help="附加参考文献列表")
    p_write.add_argument("--citation-style", default="gbt7714", choices=CITATION_STYLE_CHOICES)
    p_write.add_argument("--validate", action="store_true", help="同时输出证据质量校验报告")
    p_write.add_argument("--raw", action="store_true", help="直接输出 Markdown 文本")
    p_write.set_defaults(func=cmd_write)

    p_validate = sub.add_parser("validate", help="校验综述/论文正文的证据支撑度、正负向结论极性冲突与统计数值归属")
    p_validate.add_argument("filepath", nargs="?", help="待校验的文稿路径（支持 .md / .docx / .pdf / .txt，也可通过 --file 指定）")
    p_validate.add_argument("--project", help="课题文献库名称")
    p_validate.add_argument("--topic", help="写作主题")
    p_validate.add_argument("--limit", type=int, default=12, help="最多纳入前 N 篇相关文献（默认 12）")
    p_validate.add_argument("--file", help="待校验的文稿文件路径")
    p_validate.add_argument("--allow-primary-data", action="store_true", help="声明正文包含作者真实一手样本/实验统计数据，免报无引文统计告警")
    p_validate.set_defaults(func=cmd_validate)

    p_topics = sub.add_parser("topics", help="基于课题文献库生成带证据与可检验空白的选题建议")
    p_topics.add_argument("--project", help="课题文献库名称")
    p_topics.add_argument("--topic", help="选题方向")
    p_topics.add_argument("--limit", type=int, default=6, help="最多生成 N 个选题建议（默认 6）")
    p_topics.add_argument("--verify-gaps", action="store_true", help="联网探测候选交叉方向的真实外部文献命中篇数")
    p_topics.set_defaults(func=cmd_topics)

    # data-table / match-journal / rebuttal（科研执行中后段闭环）
    p_dtable = sub.add_parser("data-table", help="将实证 CSV/TSV/JSON 数据生成出版级三线统计表、相关系数矩阵、回归表或实验对比表（Markdown/LaTeX/Word）")
    p_dtable.add_argument("filepath", help="输入数据文件路径（.csv / .tsv / .json）")
    p_dtable.add_argument("--type", "--mode", dest="type", choices=["auto", "summary", "corr", "regression", "comparison"], default="auto",
                          help="表格类型：summary(描述统计)/corr(相关矩阵带显著性星号)/regression(双行回归表)/comparison(实验对比加粗最优)")
    p_dtable.add_argument("--columns", help="指定参与计算的列名，逗号分隔（默认自动识别数值列）")
    p_dtable.add_argument("--digits", type=int, default=None, help="保留小数位数（默认描述/相关 3 位，回归 4 位，对比 2 位）")
    p_dtable.add_argument("--lang", choices=["zh", "en"], default="zh", help="表头与表注语言（zh/en）")
    p_dtable.add_argument("--caption", help="自定义表题（如 '表1 主要变量描述性统计'）")
    p_dtable.add_argument("--format", choices=["markdown", "latex", "docx"], default="markdown",
                          help="输出格式：markdown / latex (booktabs) / docx (原生三线表)")
    p_dtable.add_argument("-o", "--output", help="输出文件路径")
    p_dtable.add_argument("--raw", action="store_true", help="直接输出纯文本表格代码")
    p_dtable.set_defaults(func=cmd_data_table)

    p_mjournal = sub.add_parser("match-journal", help="基于课题引文母刊分布、核心收录体系与中科院预警名单推荐冲刺/主投/保底投稿期刊")
    p_mjournal.add_argument("--project", help="课题文献库名称")
    p_mjournal.add_argument("--from-file", help="从现有文稿（.docx/.md）末尾提取参考文献母刊参与匹配")
    p_mjournal.add_argument("--topic", help="研究主题（配合 --live 在线扩展国际核心候选期刊）")
    p_mjournal.add_argument("--live", action="store_true", help="联网检索同主题高被引母刊补充候选池")
    p_mjournal.add_argument("--lang", choices=["all", "zh", "en"], default="all", help="筛选中文刊(zh)/英文刊(en)/全部(all)")
    p_mjournal.add_argument("--limit", type=int, default=12, help="最多推荐期刊数量（默认 12）")
    p_mjournal.add_argument("--raw", action="store_true", help="直接输出 Markdown 推荐表")
    p_mjournal.set_defaults(func=cmd_match_journal)

    p_rebuttal = sub.add_parser("rebuttal", help="拆解审稿专家意见并生成点对点回复函（Response Letter）与原稿段落修订补丁模板")
    p_rebuttal.add_argument("filepath", help="审稿意见文件（.txt / .md / .docx）")
    p_rebuttal.add_argument("--manuscript", help="原稿文件（.docx / .md），传入后自动匹配各条意见对应的段落锚点 [Pn]")
    p_rebuttal.add_argument("--title", help="论文标题（填入回复函抬头）")
    p_rebuttal.add_argument("--lang", choices=["auto", "zh", "en"], default="auto", help="回复函语言（默认按审稿意见自动识别中英文）")
    p_rebuttal.add_argument("--format", choices=["markdown", "docx"], default="markdown", help="输出格式（markdown 或原生排版 docx）")
    p_rebuttal.add_argument("-o", "--output", help="输出 Response Letter 路径")
    p_rebuttal.add_argument("--init-patch", help="输出可供 patch-docx 使用的修订补丁模板 JSON 路径")
    p_rebuttal.add_argument("--raw", action="store_true", help="直接输出 Markdown 回复函")
    p_rebuttal.set_defaults(func=cmd_rebuttal)

    # workflows & check
    p_workflows = sub.add_parser("workflows", help="列出或执行预定义工作流模板")
    p_workflows.add_argument("--list", action="store_true", help="列出所有可用工作流")
    p_workflows.add_argument("--execute", help="执行指定工作流 ID")
    p_workflows.add_argument("--variables", help="工作流变量（JSON 格式）")
    p_workflows.add_argument("--dry-run", action="store_true", help="仅显示将要执行的命令")
    p_workflows.set_defaults(func=cmd_workflows)

    p_check = sub.add_parser("check", help="环境自检（Python / 依赖 / 浏览器 / 知网连通性 / DOM 契约）")
    p_check.add_argument("--fix", action="store_true", help="自动修复可修复项（含 PEP 668 隔离环境与沙箱配置）")
    p_check.add_argument("--cnki-dom", action="store_true", help="启动浏览器巡检知网高级检索 7 项核心 DOM 契约（改版排障用）")
    p_check.set_defaults(func=cmd_check)

    p_close_br = sub.add_parser("close-browser", help="保存 Cookie 并关闭后台常驻的知网共享浏览器实例")
    p_close_br.set_defaults(func=cmd_close_browser)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
