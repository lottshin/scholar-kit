"""
literature.py - Scholar Kit 统一 CLI 入口 (v1.5.0)
用法:
  python literature.py search "关键词" [--project 课题名] [--source cnki|openalex|semantic|arxiv|nssd|all] [--doc-type master] [--field 摘要] [--author] [--journal] [--download] ...
  python literature.py batch-search "词1" "词2" ... [--project 课题名] [--query-file kw.txt] [--core CSSCI] [--doc-type master] [--field 摘要] [--author] [--journal] [--append]
  python literature.py read-detail [--project 课题名] [--top-n 5] [--fulltext]
  python literature.py read-paper <论文.docx> [--output paper.txt]
  python literature.py download <url_or_doi> [--dir ./papers] [--doi DOI]
  python literature.py batch-download --from-session [--top-n 20] [--dir ./papers]
  python literature.py batch-download url1 url2 ... [--dir ./papers]
  python literature.py detail <cnki_url>
  python literature.py export --format bibtex|ris|markdown|json|excel|gbt7714|footnote|apa [--output file]
  python literature.py cite --style gbt7714|gb|footnote|apa
  python literature.py import <filepath>
  python literature.py write-docx <draft.md> [--output 论文.docx]
  python literature.py patch-docx <原论文.docx> --patch patch.json [--output 修改后.docx]
  python literature.py citations <DOI|URL> [--direction citing|cited|both] [--limit 20]
  python literature.py trends                  # 研究趋势（基于会话数据）
  python literature.py check                   # 环境自检
  python literature.py clean-cache [--all] [--dry-run]  # 缓存清理
"""

from __future__ import annotations

__version__ = "1.5.0"

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", errors="replace", buffering=1)
    sys.stderr = open(sys.stderr.fileno(), mode="w", encoding="utf-8", errors="replace", buffering=1)

_script_dir = str(Path(__file__).parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from search import (  # noqa: E402
    search_openalex, search_semantic_scholar, search_arxiv,
    search_nssd, search_all, resolve_crossref, resolve_unpaywall,
    get_citations, analyze_trends,
)
from cnki import (  # noqa: E402
    search_cnki, batch_search_cnki, batch_read_detail,
    get_detail, download_cnki, batch_download_cnki,
    parse_cnki_export, check_cnki_access,
)
from formatter import export_papers, generate_reference_list, citation_preview  # noqa: E402

def _safe_project_name(project: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._- 一-鿿" else "_" for c in project.strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "default"


def _project_dir(project: str) -> Path:
    return Path.cwd() / ".scholar-kit" / "projects" / _safe_project_name(project)


def _session_file(project: Optional[str] = None) -> Path:
    if project:
        return _project_dir(project) / "session.json"
    return Path.cwd() / ".scholar-kit" / "session.json"


def _session_project(args) -> Optional[str]:
    return getattr(args, "project", None) or None


def _save_session(results: List[Dict[str, Any]], append: bool = False, project: Optional[str] = None):
    """保存搜索结果到会话文件。append=True 时追加并按标题去重。"""
    sf = _session_file(project)
    sf.parent.mkdir(parents=True, exist_ok=True)
    if append:
        existing = _load_session(project)
        seen: Dict[str, Dict[str, Any]] = {}
        no_title: List[Dict[str, Any]] = []
        for r in existing:
            key = r.get("title", "").lower().strip()
            if key:
                seen[key] = r
            else:
                no_title.append(r)
        for r in results:
            key = r.get("title", "").lower().strip()
            if key:
                seen[key] = r
            else:
                no_title.append(r)
        results = list(seen.values()) + no_title
    sf.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_session(project: Optional[str] = None) -> List[Dict[str, Any]]:
    sf = _session_file(project)
    if sf.exists():
        try:
            return json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


# ── CNKI 搜索缓存 ─────────────────────────────────────

_CNKI_CACHE_TTL_MINUTES = 30


def _cnki_cache_key(args) -> str:
    import hashlib
    key_parts = f"{args.query}|{args.core}|{args.year_from}|{args.year_to}|{args.author}|{args.journal}|{getattr(args, 'doc_type', '')}|{getattr(args, 'field', '')}|{args.sort}|{args.pages}|{getattr(args, 'cite_enrich', 0)}"
    return hashlib.md5(key_parts.encode()).hexdigest()


def _cnki_cache_get(args) -> Optional[list]:
    cache_dir = Path.cwd() / ".scholar-kit" / "cache"
    cache_file = cache_dir / f"cnki_{_cnki_cache_key(args)}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            from datetime import datetime, timedelta
            cached_at = datetime.fromisoformat(data.get("_cached_at", ""))
            if datetime.now() - cached_at < timedelta(minutes=_CNKI_CACHE_TTL_MINUTES):
                return data.get("results")
        except Exception:
            pass
    return None


def _cnki_cache_set(args, results: list):
    from datetime import datetime
    cache_dir = Path.cwd() / ".scholar-kit" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"cnki_{_cnki_cache_key(args)}.json"
    data = {"_cached_at": datetime.now().isoformat(), "results": results}
    try:
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ── search 命令 ───────────────────────────────────────

def cmd_search(args):
    source = args.source or "cnki"
    if source not in ("cnki", "openalex", "semantic", "arxiv", "nssd", "all"):
        _output({"status": "error", "code": "UNKNOWN_SOURCE",
                 "message": f"未知数据源: {source}"})
        return

    want_download = getattr(args, "download", False)
    if want_download and source != "cnki":
        _output({"status": "error", "code": "DOWNLOAD_SOURCE_MISMATCH",
                 "message": "--download 仅支持 --source cnki"})
        return

    results = []
    reuse_driver = None
    cnki_error = None

    if source in ("cnki", "all"):
        # 检查 CNKI 搜索缓存
        cached_cnki = _cnki_cache_get(args) if not want_download else None
        if cached_cnki is not None:
            print("[cnki] 使用缓存结果", file=__import__('sys').stderr)
            results.extend(cached_cnki)
        else:
            keep = want_download and source == "cnki"
            cnki_ret = search_cnki(
                keyword=args.query,
                core=args.core,
                year_from=args.year_from,
                year_to=args.year_to,
                author=args.author,
                journal=args.journal,
                doc_type=getattr(args, "doc_type", None),
                field=getattr(args, "field", None),
                sort=args.sort or "relevance",
                pages=args.pages or 1,
                cite_enrich=getattr(args, "cite_enrich", 0),
                _keep_driver=keep,
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
                    _output(cnki_results[0])
                    if reuse_driver:
                        try: reuse_driver.quit()
                        except Exception: pass
                    return
                # source == "all" 时知网失败不阻断，记录错误后继续 API 搜索
                cnki_error = cnki_results[0]

    try:
        has_keyword = bool(args.query and args.query.strip())

        api_limit = args.limit if args.limit is not None else 10

        if has_keyword and source in ("openalex", "all"):
            results.extend(search_openalex(
                args.query, limit=api_limit,
                year_from=args.year_from, year_to=args.year_to,
            ))

        if has_keyword and source in ("semantic", "all"):
            results.extend(search_semantic_scholar(
                args.query, limit=api_limit,
                year_from=args.year_from, year_to=args.year_to,
            ))

        if has_keyword and source in ("arxiv", "all"):
            results.extend(search_arxiv(
                args.query, limit=api_limit, sort_by=args.sort or "relevance",
                year_from=args.year_from, year_to=args.year_to,
            ))

        if has_keyword and source in ("nssd", "all"):
            results.extend(search_nssd(
                args.query, limit=api_limit,
                year_from=args.year_from, year_to=args.year_to,
            ))

        seen = set()
        deduped = []
        for r in results:
            # Some sources may return null titles for edge records.
            key = (r.get("title") or "").lower().strip()
            if key and key not in seen:
                seen.add(key)
                deduped.append(r)

        if args.sort == "citations":
            deduped.sort(key=lambda x: x.get("cited_by", 0), reverse=True)
        elif args.sort == "date":
            deduped.sort(key=lambda x: x.get("year") or 0, reverse=True)

        if args.limit is not None:
            effective_limit = args.limit
        elif args.pages and args.pages > 1 and source in ("cnki", "all"):
            effective_limit = args.pages * 20
        else:
            effective_limit = 20
        deduped = deduped[:effective_limit]

        # --enrich: 对知网结果自动补全卷期页码
        enrich_n = getattr(args, "enrich", 0)
        if enrich_n and enrich_n > 0:
            cnki_papers = [(i, p) for i, p in enumerate(deduped)
                           if _is_cnki_paper(p) and p.get("url") and not p.get("pages")]
            to_enrich = cnki_papers[:enrich_n]
            if to_enrich:
                print(f"[enrich] 正在补全 {len(to_enrich)} 篇论文的卷期页码...",
                      file=__import__('sys').stderr)
                from cnki import get_detail
                for idx, p in to_enrich:
                    detail = get_detail(p["url"])
                    if detail and detail.get("status") != "error":
                        for k in ("volume", "issue", "pages", "doi", "year", "journal", "authors"):
                            if detail.get(k) and not p.get(k):
                                p[k] = detail[k]
                    time.sleep(1)

        _save_session(deduped, append=getattr(args, "append", False), project=_session_project(args))

        # 为每条结果添加引用预览
        for p in deduped:
            p["citation_preview"] = citation_preview(p)

        search_output = {"status": "success", "count": len(deduped), "results": deduped}
        if cnki_error and source == "all":
            search_output["cnki_error"] = {"code": cnki_error.get("code"), "message": cnki_error.get("message")}
            search_output["status"] = "partial"
        if args.export:
            content = export_papers(deduped, args.export, args.output)
            if isinstance(content, dict) and content.get("status") == "error":
                search_output["export_error"] = content
            else:
                search_output.update({"format": args.export, "output_file": args.output,
                                      "content": content})

        if want_download and reuse_driver:
            from config import get as cfg_get
            dl_dir = getattr(args, "download_dir", None) or cfg_get("save_dir", "./papers")
            dl_top_n = getattr(args, "download_top_n", None)
            dl_papers = deduped[:dl_top_n] if dl_top_n else deduped
            dl_urls = [p.get("url") for p in dl_papers if isinstance(p, dict) and p.get("url")]
            if dl_urls:
                dl_result = batch_download_cnki(dl_urls, save_dir=dl_dir, _driver=reuse_driver)
                reuse_driver = None
                search_output["download"] = dl_result
            else:
                search_output["download"] = {"status": "warning", "code": "NO_DOWNLOAD_URLS",
                                             "message": "搜索结果中无可下载 URL"}

        _output(search_output)

    finally:
        if reuse_driver is not None:
            try:
                reuse_driver.quit()
            except Exception:
                pass


# ── download 命令 ─────────────────────────────────────

def cmd_download(args):
    from config import get as cfg_get
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
            result = download_cnki(target, save_dir=save_dir, file_format=args.file_format or "pdf")
            _output(result)
            return
        _output({
            "status": "error",
            "code": "OA_NOT_FOUND",
            "message": "Unpaywall 未找到 OA 版本，无知网 URL 可回退",
            "metadata": meta,
        })
        return

    if target and "cnki.net" in target:
        result = download_cnki(target, save_dir=save_dir, file_format=args.file_format or "pdf")
        _output(result)
    elif target:
        _output({
            "status": "error",
            "code": "UNSUPPORTED_URL",
            "message": "目前仅支持知网 URL 直接下载",
        })
    else:
        _output({"status": "error", "code": "NO_DOWNLOAD_TARGET",
                 "message": "请提供下载目标（URL 或 --doi）"})


# ── batch-download 命令 ──────────────────────────────

def cmd_batch_download(args):
    """批量下载：浏览器只启动一次，多标签页并行下载"""
    from config import get as cfg_get
    urls = list(args.urls) if args.urls else []

    if args.from_session:
        session_data = _load_session(_session_project(args))
        if not session_data:
            _output({"status": "error", "code": "NO_SESSION",
                     "message": "没有搜索记录，请先执行 search 或 batch-search"})
            return
        top_n = args.top_n or len(session_data)
        session_urls = [p.get("url") for p in session_data[:top_n]
                        if isinstance(p, dict) and p.get("url")]
        urls.extend(session_urls)

    if not urls:
        _output({"status": "error", "code": "NO_URLS",
                 "message": "未提供下载 URL（可用 --from-session 从上次搜索结果读取）"})
        return

    save_dir = args.dir if args.dir != "./papers" else cfg_get("save_dir", "./papers")
    result = batch_download_cnki(
        urls=urls,
        save_dir=save_dir,
        file_format=args.file_format or "pdf",
    )
    _output(result)


# ── detail 命令 ───────────────────────────────────────

def cmd_detail(args):
    if not args.url:
        _output({"status": "error", "code": "NO_URL", "message": "请提供知网论文详情页 URL"})
        return
    result = get_detail(args.url)
    _output(result)


# ── export 命令 ───────────────────────────────────────

def cmd_export(args):
    papers = _load_session(_session_project(args))
    if not papers:
        _output({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可导出的数据，请先执行 search、batch-search 或 import"})
        return

    result = export_papers(papers, args.export_format, args.output)
    if isinstance(result, dict) and result.get("status") == "error":
        _output(result)
        return
    if args.raw:
        print(result)
    else:
        _output({"status": "success", "project": _session_project(args), "format": args.export_format,
                 "output_file": args.output, "content": result})


# ── project/library 命令 ───────────────────────────────

def _paper_year(paper: Dict[str, Any]) -> Any:
    return paper.get("year") or str(paper.get("date", ""))[:4]


def _paper_summary(paper: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        "index": index,
        "title": paper.get("title", ""),
        "authors": paper.get("authors", ""),
        "journal": paper.get("journal", ""),
        "year": _paper_year(paper),
        "cited_by": paper.get("cited_by", 0),
        "source": paper.get("source", ""),
        "tags": paper.get("tags", []),
        "note": paper.get("note", ""),
    }


def cmd_projects(args):
    base = Path.cwd() / ".scholar-kit" / "projects"
    projects = []
    if base.exists():
        for project_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            papers = _load_session(project_dir.name)
            projects.append({
                "name": project_dir.name,
                "count": len(papers),
                "session_file": str(project_dir / "session.json"),
            })
    _output({"status": "success", "count": len(projects), "projects": projects})


def cmd_library(args):
    papers = _load_session(_session_project(args))
    if not papers:
        _output({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可查看的文献，请先执行 search、batch-search 或 import"})
        return
    limit = args.limit or len(papers)
    rows = [_paper_summary(p, i + 1) for i, p in enumerate(papers[:limit])]
    _output({"status": "success", "project": _session_project(args), "count": len(papers), "results": rows})


# ── cite 命令 ─────────────────────────────────────────

def cmd_cite(args):
    papers = _load_session(_session_project(args))
    if not papers:
        _output({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可格式化的数据，请先执行 search、batch-search 或 import"})
        return

    # 对缺少卷期页码的知网论文，自动走 detail 补全
    cnki_need_enrich = [
        (i, p) for i, p in enumerate(papers)
        if _is_cnki_paper(p) and p.get("url") and not p.get("pages") and "cnki.net" in p.get("url", "")
    ]
    if cnki_need_enrich:
        print(f"[cite] 正在补全 {len(cnki_need_enrich)} 篇知网论文的卷期页码...",
              file=__import__('sys').stderr)
        from cnki import get_detail
        for idx, p in cnki_need_enrich:
            detail = get_detail(p["url"])
            if detail and detail.get("status") != "error":
                for k in ("volume", "issue", "pages", "doi", "year", "journal"):
                    if detail.get(k) and not p.get(k):
                        p[k] = detail[k]
            time.sleep(1)

    enriched = []
    for i, p in enumerate(papers):
        if p.get("doi") and not p.get("volume"):
            if i > 0:
                time.sleep(1)
            crossref_data = resolve_crossref(p["doi"])
            if crossref_data:
                p.update({k: v for k, v in crossref_data.items() if v and not p.get(k)})
        enriched.append(p)

    ref_list = generate_reference_list(enriched, args.style or "gbt7714")
    if args.raw:
        print(ref_list)
    else:
        _output({"status": "success", "project": _session_project(args), "style": args.style or "gbt7714",
                 "count": len(enriched), "references": ref_list})


# ── import 命令 ───────────────────────────────────────

def cmd_import(args):
    results = parse_cnki_export(args.filepath)
    if results and not (len(results) == 1 and results[0].get("status") == "error"):
        _save_session(results, project=_session_project(args))
        _output({"status": "success", "count": len(results), "results": results})
    else:
        _output(results[0] if results else {"status": "error", "code": "IMPORT_PARSE_FAILED", "message": "解析失败"})


# ── read-paper 命令 ───────────────────────────────────

def cmd_read_paper(args):
    """读取用户论文文件（.docx / .txt / .md），输出 UTF-8 纯文本"""
    filepath = Path(args.filepath)
    if not filepath.exists():
        _output({"status": "error", "code": "FILE_NOT_FOUND", "message": f"文件不存在: {args.filepath}"})
        return

    suffix = filepath.suffix.lower()
    text = ""

    if suffix == ".docx":
        try:
            from docx import Document
        except ImportError:
            _output({"status": "error", "code": "MISSING_DEPENDENCY",
                     "message": "缺少 python-docx 依赖"})
            return
        try:
            doc = Document(str(filepath))
            parts: List[str] = []
            for p in doc.paragraphs:
                if p.text.strip():
                    parts.append(p.text)
            for table in doc.tables:
                for row in table.rows:
                    row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if row_texts:
                        parts.append(" | ".join(row_texts))
            text = "\n\n".join(parts)
        except Exception as e:
            _output({"status": "error", "code": "DOCX_PARSE_FAILED", "message": f"docx 解析失败: {e}"})
            return

    elif suffix in (".txt", ".md", ".markdown"):
        decoded = False
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb2312", "gb18030"):
            try:
                text = filepath.read_text(encoding=enc)
                decoded = True
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if not decoded:
            _output({"status": "error", "code": "ENCODING_ERROR", "message": "无法识别文件编码"})
            return

    elif suffix == ".pdf":
        _output({"status": "error", "code": "UNSUPPORTED_FORMAT",
                 "message": "PDF 请使用 Agent 内置的文件读取工具直接读取，无需 read-paper"})
        return
    else:
        _output({"status": "error", "code": "UNSUPPORTED_FORMAT",
                 "message": f"不支持的文件格式: {suffix}"})
        return

    char_count = len(text)
    para_count = text.count("\n\n") + 1 if text else 0

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        _output({"status": "success",
                 "message": f"已提取到: {args.output}",
                 "chars": char_count,
                 "paragraphs": para_count})
    else:
        if args.raw:
            print(text)
        else:
            _output({"status": "success",
                     "chars": char_count,
                     "paragraphs": para_count,
                     "text": text})


# ── pdf-meta 命令 ──────────────────────────────────────

def cmd_pdf_meta(args):
    """从 PDF 文件中提取元数据（标题、作者、DOI 等）"""
    filepath = Path(args.filepath)
    if not filepath.exists():
        _output({"status": "error", "code": "FILE_NOT_FOUND", "message": f"文件不存在: {args.filepath}"})
        return

    if filepath.suffix.lower() != ".pdf":
        _output({"status": "error", "code": "NOT_PDF", "message": "仅支持 PDF 文件"})
        return

    try:
        from pypdf import PdfReader
    except ImportError:
        _output({"status": "error", "code": "MISSING_DEPENDENCY", "message": "缺少 pypdf 依赖"})
        return

    try:
        reader = PdfReader(str(filepath))
        meta = reader.metadata or {}

        result = {"status": "success", "file": str(filepath)}

        if meta.title:
            result["title"] = meta.title
        if meta.author:
            result["authors"] = meta.author
        if meta.subject:
            result["subject"] = meta.subject

        # 从 XMP 元数据中提取 DOI
        doi = None
        if hasattr(reader, 'xmp_metadata') and reader.xmp_metadata:
            xmp = reader.xmp_metadata
            # DOI 可能在 dc:identifier 或自定义属性中
            if hasattr(xmp, 'dc_identifier') and xmp.dc_identifier:
                for ident in (xmp.dc_identifier if isinstance(xmp.dc_identifier, list) else [xmp.dc_identifier]):
                    if ident and '10.' in str(ident):
                        import re as _re
                        doi_m = _re.search(r'(10\.\d{4,}/[^\s]+)', str(ident))
                        if doi_m:
                            doi = doi_m.group(1)
                            break

        # 从前几页文本中查找 DOI
        if not doi:
            import re as _re
            for page_num in range(min(3, len(reader.pages))):
                page_text = reader.pages[page_num].extract_text() or ""
                doi_m = _re.search(r'(?:DOI|doi)[：:\s]*\s*(10\.\d{4,}/[^\s]+)', page_text)
                if doi_m:
                    doi = doi_m.group(1).rstrip(".")
                    break

        if doi:
            result["doi"] = doi
            # 用 DOI 从 Crossref 补全完整元数据
            crossref_data = resolve_crossref(doi)
            if crossref_data:
                result["crossref"] = crossref_data

        _output(result)

    except Exception as e:
        _output({"status": "error", "code": "PDF_READ_FAILED", "message": str(e)})


# ── batch-search 命令 ─────────────────────────────────

def cmd_batch_search(args):
    """批量搜索：浏览器只启动一次，循环搜索多个关键词"""
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
            _output({"status": "error", "code": "ENCODING_ERROR",
                     "message": f"关键词文件编码无法识别: {args.query_file}"})
            return
        for line in qf_text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                keywords.append(line)

    if not keywords:
        _output({"status": "error", "code": "NO_KEYWORDS",
                 "message": "未提供关键词"})
        return

    result = batch_search_cnki(
        keywords=keywords,
        core=args.core,
        author=getattr(args, "author", None),
        journal=getattr(args, "journal", None),
        doc_type=getattr(args, "doc_type", None),
        field=getattr(args, "field", None),
        year_from=args.year_from,
        year_to=args.year_to,
        sort=args.sort or "relevance",
        pages=args.pages or 1,
    )

    if result.get("status") in ("success", "partial") and result.get("results"):
        _save_session(result.get("results") or [], append=args.append, project=_session_project(args))

    if args.export and result.get("results"):
        content = export_papers(result["results"], args.export, args.output)
        if isinstance(content, dict) and content.get("status") == "error":
            _output(content)
            return
        export_output = {"status": result.get("status", "success"),
                         "count": len(result["results"]),
                         "format": args.export, "output_file": args.output,
                         "content": content}
        if result.get("errors"):
            export_output["errors"] = result["errors"]
        _output(export_output)
    else:
        _output(result)


# ── read-detail 命令 ──────────────────────────────────

def _is_cnki_paper(paper: dict) -> bool:
    url = paper.get("url", "")
    source = paper.get("source", "")
    return "cnki" in url.lower() or source == "CNKI" or source == "CNKI-export"


def _parse_indices(raw: str, total: int) -> List[int]:
    """解析用户传入的序号字符串，返回 0-based 索引列表。

    支持格式: "3" "1,3,9" "2-5" "1,3-5,8" （序号从 1 开始）
    """
    indices: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo_i, hi_i = int(lo.strip()), int(hi.strip())
            indices.extend(range(lo_i - 1, min(hi_i, total)))
        else:
            i = int(part.strip()) - 1
            if 0 <= i < total:
                indices.append(i)
    return sorted(set(indices))


def cmd_read_detail(args):
    """对会话中的论文批量获取摘要/全文"""
    papers = _load_session(_session_project(args))
    if not papers:
        _output({"status": "error", "code": "NO_SESSION_DATA",
                 "message": "没有可读取的论文，请先执行 search、batch-search 或 import"})
        return

    do_fulltext = args.fulltext
    indices_raw = getattr(args, "indices", None)

    if indices_raw:
        pick_idx = _parse_indices(indices_raw, len(papers))
        if not pick_idx:
            _output({"status": "error", "code": "INVALID_INDICES",
                     "message": f"无效序号 '{indices_raw}'，会话共 {len(papers)} 篇（序号 1-{len(papers)}）"})
            return
        selected = [papers[i] for i in pick_idx]
        label = f"第 {indices_raw} 篇"
    else:
        top_n = args.top_n or 5
        selected = papers[:top_n]
        label = f"前 {len(selected)} 篇"

    print(f"[read-detail] 会话共 {len(papers)} 篇，将获取{label}的{'全文' if do_fulltext else '摘要'}",
          file=sys.stderr)

    cnki_selected = [p for p in selected if _is_cnki_paper(p)]
    non_cnki_selected = [p for p in selected if not _is_cnki_paper(p)]
    if not cnki_selected:
        _output({"status": "warning", "code": "NO_SESSION_DATA",
                 "message": "所选论文中无知网论文，read-detail 仅支持知网论文。API 源论文请直接使用搜索时返回的摘要",
                 "count": len(non_cnki_selected), "results": non_cnki_selected})
        return
    enriched = batch_read_detail(
        papers=cnki_selected,
        top_n=len(cnki_selected),
        fulltext=do_fulltext,
    )
    enriched.extend(non_cnki_selected)

    if indices_raw:
        updated = list(papers)
        enriched_map = {p.get("url", ""): p for p in enriched if p.get("url")}
        for i in pick_idx:
            url = updated[i].get("url", "")
            if url in enriched_map:
                merged = {k: v for k, v in enriched_map[url].items() if k != "fulltext"}
                merged.update({k: updated[i][k] for k in updated[i] if k not in merged})
                updated[i] = merged
        session_papers = updated
    else:
        session_papers = []
        for p in enriched:
            sp = {k: v for k, v in p.items() if k != "fulltext"}
            session_papers.append(sp)
    _save_session(session_papers, project=_session_project(args))

    output_papers = enriched
    results = []
    for p in output_papers:
        entry: Dict[str, Any] = {
            "title": p.get("title", ""),
            "authors": p.get("authors", ""),
            "journal": p.get("journal", ""),
            "date": p.get("date", ""),
            "abstract": p.get("abstract", ""),
            "keywords": p.get("keywords", []),
            "has_fulltext": p.get("has_fulltext", False),
            "fulltext_length": p.get("fulltext_length", 0),
        }
        if p.get("fulltext_cache"):
            entry["fulltext_cache"] = p["fulltext_cache"]

        if do_fulltext and p.get("fulltext"):
            entry["fulltext"] = p["fulltext"]
        elif do_fulltext and p.get("fulltext_cache"):
            try:
                cache_path = Path(p["fulltext_cache"]).resolve()
                allowed_dir = (Path.cwd() / ".scholar-kit" / "fulltext").resolve()
                try:
                    cache_path.relative_to(allowed_dir)
                except ValueError:
                    entry["fulltext"] = ""
                else:
                    cache_data = json.loads(
                        cache_path.read_text(encoding="utf-8")
                    )
                    entry["fulltext"] = cache_data.get("fulltext", "")
            except Exception:
                entry["fulltext"] = ""

        results.append(entry)

    _output({"status": "success", "count": len(results), "results": results})


# ── docx 辅助函数 ────────────────────────────────────

def _get_or_create_footnotes_part(doc):
    """获取或创建符合 OOXML 标准的 footnotes XmlPart。

    使用 XmlPart 而非 Part：修改 element 后 doc.save() 自动序列化，无需手动同步。
    返回 (footnotes_element, max_id)。
    """
    from docx.oxml.ns import qn
    from docx.opc.part import XmlPart
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
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
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

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
    """段落级文本替换：先尝试单 run 内替换；跨 run 时精确切割，只改被命中区间，保留其余 run 格式。"""
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

    fs, fe, first_run = boundaries[first_i]
    ls, le, last_run = boundaries[last_i]

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
    """在段落中找到包含指定文本的 run（取全文第一次匹配），返回 run._element 或 None。
    跨 run 时精确定位到匹配文本末尾字符所在的 run（脚注应插在该 run 之后）。
    """
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


# ── write-docx 命令 ───────────────────────────────────

def cmd_write_docx(args):
    """Markdown 文件 → 学术格式 .docx"""
    import re
    try:
        from docx import Document
        from docx.shared import Pt, Cm
        from docx.oxml.ns import qn
    except ImportError:
        _output({"status": "error", "code": "MISSING_DEPENDENCY",
                 "message": "缺少 python-docx 依赖"})
        return

    md_path = Path(args.filepath)
    if not md_path.exists():
        _output({"status": "error", "code": "FILE_NOT_FOUND",
                 "message": f"文件不存在: {args.filepath}"})
        return

    md_text = None
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb2312", "gb18030"):
        try:
            md_text = md_path.read_text(encoding=enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if md_text is None:
        _output({"status": "error", "code": "ENCODING_ERROR",
                 "message": "文件编码无法识别"})
        return

    output_path = Path(args.output) if args.output else md_path.with_suffix(".docx")
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
        _output({"status": "error", "code": "IO_ERROR",
                 "message": f"保存失败: {e}"})
        return

    result: Dict[str, Any] = {
        "status": "success" if not warnings else "warning",
        "message": f"已生成: {output_path}",
        "output": str(output_path),
        "footnotes": fn_counter[0] - max_id,
        "references": len(ref_section_lines),
    }
    if warnings:
        result["warnings"] = warnings
    _output(result)


# ── patch-docx 命令 ───────────────────────────────────

def cmd_patch_docx(args):
    """在现有 .docx 上打补丁：文本替换 + 脚注插入 + 追加参考文献"""
    try:
        from docx import Document
        from docx.shared import Cm
    except ImportError:
        _output({"status": "error", "code": "MISSING_DEPENDENCY",
                 "message": "缺少 python-docx 依赖"})
        return

    docx_path = Path(args.filepath)
    if not docx_path.exists():
        _output({"status": "error", "code": "FILE_NOT_FOUND",
                 "message": f"文件不存在: {args.filepath}"})
        return

    patch_path = Path(args.patch)
    if not patch_path.exists():
        _output({"status": "error", "code": "FILE_NOT_FOUND",
                 "message": f"补丁文件不存在: {args.patch}"})
        return

    try:
        patch_data = json.loads(patch_path.read_text(encoding="utf-8"))
    except Exception as e:
        _output({"status": "error", "code": "PATCH_PARSE_FAILED",
                 "message": f"补丁 JSON 解析失败: {e}"})
        return

    if not isinstance(patch_data, dict):
        _output({"status": "error", "code": "PATCH_PARSE_FAILED",
                 "message": "补丁文件顶层必须是 JSON 对象"})
        return

    patches = patch_data.get("patches", [])
    footnotes_list = patch_data.get("footnotes", [])
    append_refs = patch_data.get("append_references", [])

    if not isinstance(patches, list) or not isinstance(footnotes_list, list) or not isinstance(append_refs, list):
        _output({"status": "error", "code": "PATCH_PARSE_FAILED",
                 "message": "patches / footnotes / append_references 必须是数组"})
        return

    output_path = Path(args.output) if args.output else docx_path.with_name(
        docx_path.stem + "_patched" + docx_path.suffix
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = Document(str(docx_path))
    stats = {"replaced": 0, "not_found": 0, "footnotes_added": 0, "references_appended": 0}
    warnings: List[str] = []

    ftn_element, max_id = _get_or_create_footnotes_part(doc)
    fn_counter = [max_id]

    for patch in patches:
        find_text = patch.get("find", "")
        replace_text = patch.get("replace", "")
        if not find_text:
            continue
        found = False
        for para in doc.paragraphs:
            if find_text in para.text:
                if _para_replace_text(para, find_text, replace_text):
                    stats["replaced"] += 1
                    found = True
                    break
        if not found:
            stats["not_found"] += 1
            warnings.append(f"未找到替换目标: \"{find_text[:30]}...\"" if len(find_text) > 30 else f"未找到替换目标: \"{find_text}\"")

    for fn_entry in footnotes_list:
        after_text = fn_entry.get("after", "")
        fn_text = fn_entry.get("text", "")
        if not after_text or not fn_text:
            continue
        found = False
        for para in doc.paragraphs:
            if after_text not in para.text:
                continue
            run_el = _find_run_containing(para, after_text)
            if run_el is not None:
                _add_footnote_to_element(ftn_element, fn_counter, run_el, fn_text)
                stats["footnotes_added"] += 1
                found = True
                break
        if not found:
            warnings.append(f"脚注定位失败: \"{after_text[:30]}\"" if len(after_text) > 30 else f"脚注定位失败: \"{after_text}\"")

    if append_refs:
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

    try:
        doc.save(str(output_path))
    except Exception as e:
        _output({"status": "error", "code": "IO_ERROR",
                 "message": f"保存失败: {e}"})
        return

    has_issues = stats["not_found"] > 0 or len(warnings) > 0
    result: Dict[str, Any] = {
        "status": "partial" if has_issues else "success",
        "message": f"已保存: {output_path}",
        "output": str(output_path),
        **stats,
    }
    if warnings:
        result["warnings"] = warnings
    _output(result)


# ── clean-cache 命令 ──────────────────────────────────

def cmd_clean_cache(args):
    """清理 .scholar-kit/ 缓存目录"""
    from datetime import datetime, timedelta
    from config import get as cfg_get

    cache_dir = Path.cwd() / ".scholar-kit"
    if not cache_dir.exists():
        _output({"status": "success", "message": "无缓存目录", "deleted": 0, "freed_bytes": 0})
        return

    ttl_days = cfg_get("cache_ttl_days", 30)
    now = datetime.now()
    stats = {"total": 0, "expired": 0, "deleted": 0, "freed_bytes": 0, "kept": 0}

    for root, _dirs, files in os.walk(str(cache_dir)):
        for fname in files:
            fpath = Path(root) / fname
            stats["total"] += 1
            fsize = fpath.stat().st_size

            _protected = {"session.json", "config.json", "cookies.json"}
            should_delete = args.clean_all and fname not in _protected
            if not should_delete and fname.endswith(".json") and ttl_days > 0 and fname not in _protected:
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    ts = data.get("_cached_at", "")
                    if ts:
                        cached_at = datetime.fromisoformat(ts)
                        if now - cached_at > timedelta(days=ttl_days):
                            should_delete = True
                            stats["expired"] += 1
                except Exception:
                    pass

            if should_delete:
                if not args.dry_run:
                    try:
                        fpath.unlink()
                        stats["deleted"] += 1
                        stats["freed_bytes"] += fsize
                    except Exception:
                        pass
                else:
                    stats["deleted"] += 1
                    stats["freed_bytes"] += fsize
            else:
                stats["kept"] += 1

    stats["freed_mb"] = round(stats["freed_bytes"] / 1024 / 1024, 2)
    mode = "dry-run" if args.dry_run else ("全部清理" if args.clean_all else f"TTL>{ttl_days}天")
    _output({"status": "success", "mode": mode, **stats})


# ── citations 命令 ────────────────────────────────────

def cmd_citations(args):
    """引文网络分析：获取论文的前向/后向引用"""
    paper_id = args.paper_id
    if not paper_id:
        _output({"status": "error", "code": "NO_PAPER_ID",
                 "message": "请提供论文标识（DOI、URL 或 arXiv ID）"})
        return

    direction = args.direction or "both"
    limit = args.limit or 20

    print(f"[citations] 查询 {paper_id} 的引文网络（方向: {direction}）...",
          file=sys.stderr)

    result = get_citations(paper_id, direction=direction, limit=limit)

    if "error" in result:
        _output({"status": "error", "code": "RESOLVE_FAILED",
                 "message": result["error"]})
        return

    paper = result.get("paper", {})
    citing = result.get("citing", [])
    references = result.get("references", [])
    status = "success" if paper else "partial"

    _output({
        "status": status,
        "paper": paper,
        "citing_count": len(citing),
        "references_count": len(references),
        "citing": citing,
        "references": references,
    })


# ── trends 命令 ───────────────────────────────────────

def cmd_trends(args):
    """研究趋势分析：基于当前会话数据进行聚合统计"""
    papers = _load_session(_session_project(args))
    if not papers:
        _output({"status": "error", "code": "NO_SESSION_DATA",
                 "message": "没有可分析的论文，请先执行 search、batch-search 或 import"})
        return

    print(f"[trends] 分析 {len(papers)} 篇论文的研究趋势...", file=sys.stderr)

    result = analyze_trends(papers)
    _output({"status": "success", "project": _session_project(args), **result})


# ── check 命令 ────────────────────────────────────────

def _check_browser(subprocess_mod) -> tuple:
    """检测可用浏览器，返回 (ok: bool, detail: str)"""
    import shutil as _shutil
    if sys.platform == "win32":
        import os as _os
        for p in [
            _os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            _os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            _os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        ]:
            if _os.path.exists(p):
                try:
                    r = subprocess_mod.run([p, "--version"], capture_output=True, text=True, timeout=5)
                    return True, r.stdout.strip() if r.returncode == 0 else f"Edge ({p})"
                except Exception:
                    return True, f"Edge ({p})"
        _chrome = _shutil.which("chrome") or _shutil.which("google-chrome")
        if _chrome:
            return True, f"Chrome ({_chrome})"
    else:
        for cmd in ["microsoft-edge", "microsoft-edge-stable", "google-chrome", "chromium", "chromium-browser"]:
            found = _shutil.which(cmd)
            if found:
                try:
                    r = subprocess_mod.run([found, "--version"], capture_output=True, text=True, timeout=5)
                    return True, r.stdout.strip() if r.returncode == 0 else cmd
                except Exception:
                    return True, cmd
    return False, "未检测到 Edge/Chrome"


def _check_driver() -> tuple:
    """检测浏览器驱动是否可用，返回 (ok: bool, detail: str)。"""
    try:
        from cnki.driver import _detect_browser, _find_local_driver
        browser = _detect_browser()
    except Exception as e:
        return False, f"无法检测浏览器类型: {e}"

    try:
        from selenium.webdriver.common.selenium_manager import SeleniumManager
        sm_bin = SeleniumManager._get_binary()
        if sm_bin and os.path.isfile(str(sm_bin)):
            import subprocess as _sp
            browser_arg = "MicrosoftEdge" if browser == "edge" else "chrome"
            result = _sp.run(
                [str(sm_bin), "--browser", browser_arg, "--offline", "--output", "json"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                try:
                    info = json.loads(result.stdout)
                    driver_path = info.get("result", {}).get("driver_path", "")
                    if driver_path and os.path.isfile(driver_path):
                        return True, f"selenium-manager 找到: {driver_path}"
                except (ValueError, KeyError):
                    pass
    except Exception:
        pass

    local = _find_local_driver(browser)
    if local:
        return True, f"本地驱动: {local}"

    driver_name = "msedgedriver" if browser == "edge" else "chromedriver"
    return False, (
        f"未找到 {driver_name}（Selenium Manager 需联网下载）。"
        f"解决：1) 提权获取网络权限 2) 设置 SCHOLAR_DRIVER_PATH 环境变量"
    )


def _check_cnki() -> tuple:
    """检测知网连通性，返回 (ok: bool, detail: str)。
    SANDBOX_BLOCKED 单独标记，让 check 输出能指导 Agent 提权重试。
    """
    try:
        accessible, msg = check_cnki_access()
        if accessible:
            return True, "可访问"
        if msg.startswith("SANDBOX_BLOCKED"):
            return False, f"沙盒权限阻止（WinError 10013 等），提权后可能正常"
        return False, msg
    except Exception as e:
        return False, str(e)


def _fix_sandbox_network() -> List[str]:
    """检测沙箱环境并写入网络配置。返回已修复项列表。

    注意：Codex 的网络权限在任务创建时锁定，运行中写入 config.toml
    只对下次任务生效，当前任务仍然无网络。
    """
    fixes = []

    codex_dir = Path.cwd() / ".codex"
    codex_home = Path.home() / ".codex"
    is_codex_env = (codex_dir.exists() or codex_home.exists()
                    or os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED"))
    if is_codex_env:
        config_targets = []
        if codex_dir.exists():
            config_targets.append(codex_dir / "config.toml")
        if codex_home.exists() and codex_home != codex_dir:
            config_targets.append(codex_home / "config.toml")
        if not config_targets:
            codex_dir.mkdir(parents=True, exist_ok=True)
            config_targets.append(codex_dir / "config.toml")

        codex_net_config = (
            '\n# Scholar Kit - 知网检索需要网络权限\n'
            'approval_policy = "on-request"\n'
            'sandbox_mode = "workspace-write"\n\n'
            '[sandbox_workspace_write]\n'
            'network_access = true\n'
        )
        for config_path in config_targets:
            try:
                existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
                if "network_access" in existing:
                    continue
                with open(config_path, "a", encoding="utf-8") as f:
                    if existing and not existing.endswith("\n"):
                        f.write("\n")
                    f.write(codex_net_config)
                fixes.append(f"codex: 已写入 {config_path}（network_access = true）")
            except (PermissionError, OSError):
                continue

    claude_dir = Path.cwd() / ".claude"
    if claude_dir.exists():
        settings_path = claude_dir / "settings.json"
        try:
            settings = {}
            if settings_path.exists():
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            domains = settings.setdefault("sandbox", {}).setdefault("network", {}).setdefault("allowedDomains", [])
            if "*.cnki.net" not in domains:
                domains.append("*.cnki.net")
                settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
                fixes.append("claude-code: 已在 .claude/settings.json 添加 *.cnki.net")
        except Exception:
            pass

    return fixes


def cmd_check(args):
    """环境自检：逐项检查运行条件，输出能力位供 Agent 决策。--fix 时自动修复可修复项。"""
    import subprocess
    fix_mode = getattr(args, "fix", False)

    checks = []
    fixes_applied = []

    v = sys.version_info
    checks.append({
        "item": "Python",
        "status": "ok" if v >= (3, 9) else "warn" if v >= (3, 8) else "fail",
        "detail": f"{v.major}.{v.minor}.{v.micro}",
    })

    for pkg, import_name in [
        ("selenium", "selenium"), ("httpx", "httpx"),
        ("beautifulsoup4", "bs4"), ("openpyxl", "openpyxl"),
        ("python-docx", "docx"),
    ]:
        try:
            mod = __import__(import_name)
            ver = getattr(mod, "__version__", "?")
            status = "ok"
            detail = ver
            if pkg == "selenium" and ver != "?":
                try:
                    parts = [int(x) for x in ver.split(".")[:2]]
                    if parts < [4, 10]:
                        status = "warn"
                        detail = f"{ver}（需要 >=4.10）"
                except (ValueError, IndexError):
                    pass
            checks.append({"item": pkg, "status": status, "detail": detail})
        except ImportError:
            if pkg == "httpx":
                checks.append({"item": pkg, "status": "warn", "detail": "未安装（urllib 兜底可用）"})
            elif fix_mode and pkg == "selenium":
                try:
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install", "selenium>=4.10"],
                        capture_output=True, timeout=120,
                    )
                    mod = __import__(import_name)
                    ver = getattr(mod, "__version__", "?")
                    checks.append({"item": pkg, "status": "ok", "detail": ver})
                    fixes_applied.append(f"selenium: 已自动安装 ({ver})")
                except Exception as e:
                    checks.append({"item": pkg, "status": "fail", "detail": f"自动安装失败: {e}"})
            else:
                checks.append({"item": pkg, "status": "fail", "detail": "未安装"})

    encoding = sys.stdout.encoding or "unknown"
    checks.append({
        "item": "终端编码",
        "status": "ok" if "utf" in encoding.lower() else "warn",
        "detail": encoding,
    })

    browser_ok, browser_detail = _check_browser(subprocess)
    checks.append({
        "item": "浏览器",
        "status": "ok" if browser_ok else "warn",
        "detail": browser_detail,
    })

    driver_ok, driver_detail = _check_driver()
    checks.append({
        "item": "浏览器驱动",
        "status": "ok" if driver_ok else "warn",
        "detail": driver_detail,
    })

    cnki_ok, cnki_detail = _check_cnki()
    checks.append({
        "item": "知网连通性",
        "status": "ok" if cnki_ok else "fail",
        "detail": cnki_detail,
    })

    if fix_mode and not cnki_ok:
        sandbox_fixes = _fix_sandbox_network()
        if sandbox_fixes:
            fixes_applied.extend(sandbox_fixes)
            cnki_ok, cnki_detail = _check_cnki()
            for c in checks:
                if c["item"] == "知网连通性":
                    c["status"] = "ok" if cnki_ok else "fail"
                    c["detail"] = cnki_detail
                    break

    cache_dir = Path.cwd() / ".scholar-kit"
    if cache_dir.exists():
        total = sum(f.stat().st_size for f in cache_dir.rglob("*") if f.is_file())
        checks.append({
            "item": "缓存目录", "status": "ok",
            "detail": f"{cache_dir} ({round(total/1024/1024, 2)} MB)",
        })
    else:
        checks.append({"item": "缓存目录", "status": "ok", "detail": "尚未创建"})

    selenium_item = next((c for c in checks if c["item"] == "selenium"), None)
    selenium_ok = selenium_item is not None and selenium_item["status"] != "fail"
    sandbox_blocked = cnki_detail and "沙盒权限阻止" in cnki_detail
    cnki_feasible = browser_ok and cnki_ok and selenium_ok and driver_ok
    cnki_reasons = []
    if not selenium_ok:
        cnki_reasons.append("selenium 未安装")
    if not browser_ok:
        cnki_reasons.append("未检测到浏览器")
    if not driver_ok:
        cnki_reasons.append("浏览器驱动缺失（需联网下载）")
    if not cnki_ok:
        cnki_reasons.append("知网不可达（沙盒权限阻止）" if sandbox_blocked else "知网不可达")

    is_codex = (Path.cwd() / ".codex").exists() or os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED")

    capabilities: Dict[str, Any] = {
        "cnki_feasible": cnki_feasible,
        "sandbox_blocked": sandbox_blocked,
        "driver_ok": driver_ok,
        "api_sources": True,
        "docx_tools": any(c["item"] == "python-docx" and c["status"] == "ok" for c in checks),
    }
    if not cnki_feasible:
        capabilities["cnki_blocked_reasons"] = cnki_reasons
        capabilities["suggested_sources"] = ["openalex", "semantic", "arxiv", "nssd"]

        if sandbox_blocked or (is_codex and not driver_ok):
            capabilities["fix_note"] = (
                "沙盒限制导致预检/驱动下载失败。"
                "解决：设置 SCHOLAR_SKIP_NETWORK_CHECK=1，"
                "然后以提权方式运行知网命令（平台会弹窗让用户授权网络访问）。"
                "提权后 Selenium Manager 自动下载驱动，所有卡点一步清零"
            )
        elif not driver_ok:
            capabilities["fix_note"] = (
                "浏览器驱动缺失。确保有网络连接后运行任意知网命令，"
                "Selenium Manager 会自动下载匹配的驱动。"
                "或手动设置 SCHOLAR_DRIVER_PATH 环境变量指向已有驱动"
            )
        elif fix_mode and not cnki_ok and fixes_applied:
            capabilities["fix_note"] = "已写入沙箱网络配置但知网仍不可达。可能原因：未连接校园网/VPN"

    update_info = _check_update()

    all_ok = all(c["status"] != "fail" for c in checks)
    output: Dict[str, Any] = {
        "status": "success" if all_ok else "warning",
        "version": __version__,
        "capabilities": capabilities,
        "checks": checks,
    }
    if fixes_applied:
        output["fixes_applied"] = fixes_applied
    if update_info:
        output["update"] = update_info
    _output(output)


def _check_update():
    """只读版本对比：本地版本 vs GitHub 最新 Release/Tag（超时不阻塞）"""
    import re
    _SEMVER_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")

    repo = "lottshin/scholar-kit"
    urls = [
        f"https://api.github.com/repos/{repo}/releases/latest",
        f"https://api.github.com/repos/{repo}/tags?per_page=1",
    ]

    try:
        latest = None
        for url in urls:
            if latest:
                break
            try:
                data = _fetch_json(url, timeout=5)
                if data is None:
                    continue
                if isinstance(data, list):
                    latest = data[0].get("name", "") if data else ""
                else:
                    latest = data.get("tag_name", "")
            except Exception:
                continue

        if not latest:
            return None

        latest = latest.removeprefix("v")
        if not _SEMVER_RE.match(latest):
            return None

        if latest != __version__:
            return {
                "update_available": True,
                "current": __version__,
                "latest": latest,
                "message": f"新版本 {latest} 可用，在 skill 目录执行 git pull 更新",
            }
        return {"update_available": False, "current": __version__, "latest": latest}
    except Exception:
        return None


def _fetch_json(url: str, timeout: int = 10):
    """HTTP GET 返回 JSON，httpx 优先，urllib 兜底。失败统一返回 None。"""
    try:
        import httpx
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        return resp.json() if resp.status_code == 200 else None
    except ImportError:
        pass
    except Exception:
        return None
    try:
        from urllib.request import urlopen, Request
        req = Request(url, headers={"User-Agent": "scholar-kit",
                                    "Accept": "application/json"})
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            import json as _json
            return _json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


# ── 输出工具 ──────────────────────────────────────────

def _output(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ── CLI 解析 ──────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="literature",
        description="Scholar Kit - 学术文献检索工具",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    # search
    p_search = sub.add_parser("search", help="搜索文献")
    p_search.add_argument("query", help="搜索关键词")
    p_search.add_argument("--source", default="cnki",
                          help="数据源: cnki/openalex/semantic/arxiv/nssd/all (默认 cnki)")
    p_search.add_argument("--limit", type=int, default=None, help="结果数量限制（默认 20，多页时自动扩展）")
    p_search.add_argument("--core",
                          help="知网侧边栏来源类别，逗号分隔: 北大核心,CSSCI,AMI,WJCI,CSCD,EI")
    p_search.add_argument("--doc-type",
                          choices=["journal", "master", "doctor", "thesis",
                                   "conference", "newspaper"],
                          help="文献类型筛选: journal/master/doctor/thesis/conference/newspaper")
    p_search.add_argument("--field", default=None,
                          help="搜索字段: 主题(默认)/篇名/关键词/摘要/全文/作者/来源")
    p_search.add_argument("--year-from", type=int, help="起始年份")
    p_search.add_argument("--year-to", type=int, help="截止年份")
    p_search.add_argument("--author", help="作者")
    p_search.add_argument("--journal", help="期刊名")
    p_search.add_argument("--sort", choices=["relevance", "date", "citations"],
                          default="relevance", help="排序方式")
    p_search.add_argument("--pages", type=int, default=1, help="知网抓取页数")
    p_search.add_argument("--export", help="直接导出: bibtex/ris/markdown/json/excel")
    p_search.add_argument("--output", help="导出文件路径")
    p_search.add_argument("--download", action="store_true",
                          help="搜索后直接下载（仅 --source cnki）")
    p_search.add_argument("--download-dir", default="./papers",
                          help="下载目录（配合 --download，默认 ./papers）")
    p_search.add_argument("--download-top-n", type=int, default=None,
                          help="下载前 N 篇（配合 --download，默认全部）")
    p_search.add_argument("--enrich", type=int, default=0, metavar="N",
                          help="自动补全前 N 篇知网论文的卷期页码（需访问详情页）")
    p_search.add_argument("--cite-enrich", type=int, default=0, metavar="N",
                          help="搜索时点击前 N 篇知网引用按钮，快速补全 GB/T 引用和页码")
    p_search.add_argument("--append", action="store_true",
                          help="追加到已有会话结果（而非覆盖）")
    p_search.add_argument("--project", help="课题文献库名称；指定后读写 .scholar-kit/projects/<project>/session.json")
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

    # export
    p_export = sub.add_parser("export", help="导出上次搜索结果")
    p_export.add_argument("--format", dest="export_format", required=True,
                          choices=["bibtex", "ris", "markdown", "json", "excel", "gbt7714", "footnote", "apa"])
    p_export.add_argument("--output", help="输出文件路径")
    p_export.add_argument("--raw", action="store_true", help="输出纯文本而非 JSON")
    p_export.add_argument("--project", help="课题文献库名称")
    p_export.set_defaults(func=cmd_export)

    # projects
    p_projects = sub.add_parser("projects", help="列出课题文献库")
    p_projects.set_defaults(func=cmd_projects)

    # library
    p_library = sub.add_parser("library", help="查看当前或指定课题文献库")
    p_library.add_argument("--project", help="课题文献库名称")
    p_library.add_argument("--limit", type=int, help="最多显示前 N 篇")
    p_library.set_defaults(func=cmd_library)

    # cite
    p_cite = sub.add_parser("cite", help="生成引用格式")
    p_cite.add_argument("--style", default="gbt7714",
                        choices=["gbt7714", "gb", "footnote", "apa"])
    p_cite.add_argument("--raw", action="store_true", help="输出纯文本而非 JSON")
    p_cite.add_argument("--project", help="课题文献库名称")
    p_cite.set_defaults(func=cmd_cite)

    # import
    p_import = sub.add_parser("import", help="导入知网导出的题录文件")
    p_import.add_argument("filepath", help="题录文件路径")
    p_import.add_argument("--project", help="课题文献库名称")
    p_import.set_defaults(func=cmd_import)

    # read-paper
    p_paper = sub.add_parser("read-paper", help="读取论文文件（.docx/.txt/.md）并输出 UTF-8 文本")
    p_paper.add_argument("filepath", help="论文文件路径")
    p_paper.add_argument("--output", help="输出到文件（默认直接打印）")
    p_paper.add_argument("--raw", action="store_true", help="输出纯文本而非 JSON")
    p_paper.set_defaults(func=cmd_read_paper)

    # pdf-meta
    p_pdf = sub.add_parser("pdf-meta", help="从 PDF 提取元数据（标题、DOI 等）")
    p_pdf.add_argument("filepath", help="PDF 文件路径")
    p_pdf.set_defaults(func=cmd_pdf_meta)

    # batch-search
    p_batch = sub.add_parser("batch-search", help="批量知网搜索（一次启动浏览器）")
    p_batch.add_argument("queries", nargs="*", help="搜索关键词列表")
    p_batch.add_argument("--query-file", help="关键词文件路径（每行一个关键词）")
    p_batch.add_argument("--core",
                         help="知网侧边栏来源类别，逗号分隔: 北大核心,CSSCI,AMI,WJCI,CSCD,EI")
    p_batch.add_argument("--doc-type",
                         choices=["journal", "master", "doctor", "thesis",
                                  "conference", "newspaper"],
                         help="文献类型筛选: journal/master/doctor/thesis/conference/newspaper")
    p_batch.add_argument("--field", default=None,
                         help="搜索字段: 主题(默认)/篇名/关键词/摘要/全文/作者/来源")
    p_batch.add_argument("--author", help="作者（对每组关键词生效）")
    p_batch.add_argument("--journal", help="期刊名（对每组关键词生效）")
    p_batch.add_argument("--year-from", type=int, help="起始年份")
    p_batch.add_argument("--year-to", type=int, help="截止年份")
    p_batch.add_argument("--sort", choices=["relevance", "date", "citations"],
                         default="relevance", help="排序方式")
    p_batch.add_argument("--pages", type=int, default=1, help="每组关键词抓取页数")
    p_batch.add_argument("--export", help="直接导出: bibtex/ris/markdown/json/excel")
    p_batch.add_argument("--output", help="导出文件路径")
    p_batch.add_argument("--append", action="store_true",
                         help="追加到已有会话结果（而非覆盖）")
    p_batch.add_argument("--project", help="课题文献库名称")
    p_batch.set_defaults(func=cmd_batch_search)

    # read-detail
    p_read = sub.add_parser("read-detail", help="批量获取论文摘要/全文（需先搜索）")
    p_read.add_argument("--top-n", type=int, default=5,
                        help="获取前 N 篇论文的详情（默认 5）")
    p_read.add_argument("--indices", type=str, default=None,
                        help="指定论文序号（从1开始），如 '3' '1,3,9' '2-5'。指定后忽略 --top-n")
    p_read.add_argument("--fulltext", action="store_true",
                        help="抓取 HTML 全文（默认只抓摘要）")
    p_read.add_argument("--project", help="课题文献库名称")
    p_read.set_defaults(func=cmd_read_detail)

    # batch-download
    p_bdl = sub.add_parser("batch-download", help="批量下载知网论文（一次启动浏览器）")
    p_bdl.add_argument("urls", nargs="*", help="知网论文 URL 列表（可选，也可用 --from-session）")
    p_bdl.add_argument("--from-session", action="store_true",
                       help="从上次搜索结果（session.json）读取 URL")
    p_bdl.add_argument("--top-n", type=int, help="配合 --from-session，只下载前 N 篇")
    p_bdl.add_argument("--dir", default="./papers", help="保存目录")
    p_bdl.add_argument("--file-format", choices=["pdf", "caj"], default="pdf")
    p_bdl.add_argument("--project", help="课题文献库名称；配合 --from-session 使用")
    p_bdl.set_defaults(func=cmd_batch_download)

    # write-docx
    p_wdocx = sub.add_parser("write-docx", help="Markdown → 学术格式 Word 文档")
    p_wdocx.add_argument("filepath", help="Markdown 文件路径")
    p_wdocx.add_argument("--output", help="输出 .docx 路径（默认同名 .docx）")
    p_wdocx.set_defaults(func=cmd_write_docx)

    # patch-docx
    p_pdocx = sub.add_parser("patch-docx", help="在现有 .docx 上打补丁（插入引用/脚注）")
    p_pdocx.add_argument("filepath", help="原始 .docx 文件路径")
    p_pdocx.add_argument("--patch", required=True, help="补丁 JSON 文件路径")
    p_pdocx.add_argument("--output", help="输出路径（默认 原名_patched.docx）")
    p_pdocx.set_defaults(func=cmd_patch_docx)

    # clean-cache
    p_clean = sub.add_parser("clean-cache", help="清理过期缓存文件")
    p_clean.add_argument("--all", action="store_true", dest="clean_all",
                         help="删除所有缓存（不仅是过期的）")
    p_clean.add_argument("--dry-run", action="store_true",
                         help="仅统计，不实际删除")
    p_clean.set_defaults(func=cmd_clean_cache)

    # citations
    p_cite_net = sub.add_parser("citations", help="引文网络分析（前向/后向引用）")
    p_cite_net.add_argument("paper_id", help="论文标识（DOI、Semantic Scholar URL、arXiv ID 等）")
    p_cite_net.add_argument("--direction", choices=["citing", "cited", "both"], default="both",
                            help="引用方向：citing=谁引了它，cited=它引了谁，both=双向（默认）")
    p_cite_net.add_argument("--limit", type=int, default=20,
                            help="每个方向最多返回条数（默认 20）")
    p_cite_net.set_defaults(func=cmd_citations)

    # trends
    p_trends = sub.add_parser("trends", help="研究趋势分析（基于会话中的搜索结果）")
    p_trends.add_argument("--project", help="课题文献库名称")
    p_trends.set_defaults(func=cmd_trends)

    # check
    p_check = sub.add_parser("check", help="环境自检（Python / 依赖 / 浏览器 / 知网连通性）")
    p_check.add_argument("--fix", action="store_true",
                         help="自动修复可修复项（安装 selenium、写入沙箱网络配置）")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
