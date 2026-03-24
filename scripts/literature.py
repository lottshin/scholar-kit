"""
literature.py - Scholar Kit 统一 CLI 入口 (v1.2.0)
用法:
  python literature.py search "关键词" [--source cnki|openalex|semantic|arxiv|nssd|all] [--author] [--journal] [--download] ...
  python literature.py batch-search "词1" "词2" ... [--query-file kw.txt] [--core CSSCI] [--author] [--journal] [--append]
  python literature.py read-detail [--top-n 5] [--fulltext]
  python literature.py read-paper <论文.docx> [--output paper.txt]
  python literature.py download <url_or_doi> [--dir ./papers] [--doi DOI]
  python literature.py batch-download --from-session [--top-n 20] [--dir ./papers]
  python literature.py batch-download url1 url2 ... [--dir ./papers]
  python literature.py detail <cnki_url>
  python literature.py export --format bibtex|ris|markdown|json|excel|gbt7714|footnote|apa [--output file]
  python literature.py cite --style gbt7714|footnote|apa
  python literature.py import <filepath>
  python literature.py write-docx <draft.md> [--output 论文.docx]
  python literature.py patch-docx <原论文.docx> --patch patch.json [--output 修改后.docx]
  python literature.py check                    # 环境自检
  python literature.py clean-cache [--all] [--dry-run]  # 缓存清理
"""

from __future__ import annotations

__version__ = "1.2.0"

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", errors="replace", buffering=1)
    sys.stderr = open(sys.stderr.fileno(), mode="w", encoding="utf-8", errors="replace", buffering=1)

_script_dir = str(Path(__file__).parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from search import (  # noqa: E402
    search_openalex, search_semantic_scholar, search_arxiv,
    search_nssd, search_all, resolve_crossref, resolve_unpaywall,
)
from cnki import (  # noqa: E402
    search_cnki, batch_search_cnki, batch_read_detail,
    get_detail, download_cnki, batch_download_cnki,
    parse_cnki_export, check_cnki_access,
)
from formatter import export_papers, generate_reference_list  # noqa: E402

def _session_file() -> Path:
    return Path.cwd() / ".scholar-kit" / "session.json"


def _save_session(results: List[Dict[str, Any]], append: bool = False):
    """保存搜索结果到会话文件。append=True 时追加并按标题去重。"""
    sf = _session_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    if append:
        existing = _load_session()
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


def _load_session() -> List[Dict[str, Any]]:
    sf = _session_file()
    if sf.exists():
        try:
            return json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


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

    if source in ("cnki", "all"):
        keep = want_download and source == "cnki"
        cnki_ret = search_cnki(
            keyword=args.query,
            core=args.core,
            year_from=args.year_from,
            year_to=args.year_to,
            author=args.author,
            journal=args.journal,
            sort=args.sort or "relevance",
            pages=args.pages or 1,
            _keep_driver=keep,
        )
        if keep and isinstance(cnki_ret, tuple):
            cnki_results, reuse_driver = cnki_ret
        else:
            cnki_results = cnki_ret

        if cnki_results and not (len(cnki_results) == 1 and cnki_results[0].get("status") == "error"):
            results.extend(cnki_results)
        elif cnki_results and cnki_results[0].get("status") == "error":
            _output(cnki_results[0])
            if source == "cnki":
                if reuse_driver:
                    try: reuse_driver.quit()
                    except Exception: pass
                return

    try:
        has_keyword = bool(args.query and args.query.strip())

        api_limit = args.limit if args.limit is not None else 10

        if has_keyword and source in ("openalex", "all"):
            results.extend(search_openalex(
                args.query, limit=api_limit,
                year_from=args.year_from, year_to=args.year_to,
            ))

        if has_keyword and source in ("semantic", "all"):
            results.extend(search_semantic_scholar(args.query, limit=api_limit))

        if has_keyword and source in ("arxiv", "all"):
            results.extend(search_arxiv(args.query, limit=api_limit, sort_by=args.sort or "relevance"))

        if has_keyword and source in ("nssd", "all"):
            results.extend(search_nssd(args.query, limit=api_limit))

        seen = set()
        deduped = []
        for r in results:
            key = r.get("title", "").lower().strip()
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

        _save_session(deduped)

        search_output = {"status": "success", "count": len(deduped), "results": deduped}
        if args.export:
            content = export_papers(deduped, args.export, args.output)
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
        session_data = _load_session()
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
    papers = _load_session()
    if not papers:
        _output({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可导出的数据，请先执行 search 命令"})
        return

    result = export_papers(papers, args.export_format, args.output)
    if args.raw:
        print(result)
    else:
        _output({"status": "success", "format": args.export_format,
                 "output_file": args.output, "content": result})


# ── cite 命令 ─────────────────────────────────────────

def cmd_cite(args):
    papers = _load_session()
    if not papers:
        _output({"status": "error", "code": "NO_SESSION_DATA", "message": "没有可格式化的数据，请先执行 search 命令"})
        return

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
        _output({"status": "success", "style": args.style or "gbt7714",
                 "count": len(enriched), "references": ref_list})


# ── import 命令 ───────────────────────────────────────

def cmd_import(args):
    results = parse_cnki_export(args.filepath)
    if results and not (len(results) == 1 and results[0].get("status") == "error"):
        _save_session(results)
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
        year_from=args.year_from,
        year_to=args.year_to,
        sort=args.sort or "relevance",
        pages=args.pages or 1,
    )

    if result.get("status") == "success":
        _save_session(result.get("results") or [], append=args.append)

    if args.export and result.get("results"):
        content = export_papers(result["results"], args.export, args.output)
        _output({"status": "success", "count": len(result["results"]),
                 "format": args.export, "output_file": args.output,
                 "content": content})
    else:
        _output(result)


# ── read-detail 命令 ──────────────────────────────────

def cmd_read_detail(args):
    """对会话中的论文批量获取摘要/全文"""
    papers = _load_session()
    if not papers:
        _output({"status": "error", "code": "NO_SESSION_DATA",
                 "message": "没有可读取的论文，请先执行 search 或 batch-search"})
        return

    top_n = args.top_n or 5
    do_fulltext = args.fulltext

    print(f"[read-detail] 从会话中读取 {len(papers)} 篇论文，"
          f"将获取前 {top_n} 篇的{'全文' if do_fulltext else '摘要'}",
          file=sys.stderr)

    enriched = batch_read_detail(
        papers=papers,
        top_n=top_n,
        fulltext=do_fulltext,
    )

    session_papers = []
    for p in enriched:
        sp = {k: v for k, v in p.items() if k != "fulltext"}
        session_papers.append(sp)
    _save_session(session_papers)

    output_papers = enriched[:top_n]
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
                cache_data = json.loads(
                    Path(p["fulltext_cache"]).read_text(encoding="utf-8")
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

    try:
        md_text = md_path.read_text(encoding="utf-8")
    except Exception as e:
        _output({"status": "error", "code": "ENCODING_ERROR",
                 "message": f"文件读取失败: {e}"})
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
    import os
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

            should_delete = args.clean_all
            if not should_delete and fname.endswith(".json") and ttl_days > 0:
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


# ── check 命令 ────────────────────────────────────────

def cmd_check(_args):
    """环境自检：逐项检查运行条件"""
    import os
    import subprocess

    checks = []

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
            checks.append({"item": pkg, "status": "ok", "detail": ver})
        except ImportError:
            checks.append({"item": pkg, "status": "fail", "detail": "未安装"})

    encoding = sys.stdout.encoding or "unknown"
    checks.append({
        "item": "终端编码",
        "status": "ok" if "utf" in encoding.lower() else "warn",
        "detail": encoding,
    })

    browser = "unknown"
    for name, cmd in [("Edge", "msedge"), ("Chrome", "chrome"), ("Chrome", "google-chrome")]:
        try:
            result = subprocess.run(
                [cmd, "--version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                browser = result.stdout.strip()
                break
        except Exception:
            continue
    checks.append({
        "item": "浏览器",
        "status": "ok" if browser != "unknown" else "warn",
        "detail": browser if browser != "unknown" else "未检测到 Edge/Chrome",
    })

    try:
        accessible, msg = check_cnki_access()
        checks.append({
            "item": "知网连通性",
            "status": "ok" if accessible else "fail",
            "detail": "可访问" if accessible else msg,
        })
    except Exception as e:
        checks.append({"item": "知网连通性", "status": "fail", "detail": str(e)})

    cache_dir = Path.cwd() / ".scholar-kit"
    if cache_dir.exists():
        import os as _os
        total = sum(
            f.stat().st_size for f in cache_dir.rglob("*") if f.is_file()
        )
        checks.append({
            "item": "缓存目录",
            "status": "ok",
            "detail": f"{cache_dir} ({round(total/1024/1024, 2)} MB)",
        })
    else:
        checks.append({"item": "缓存目录", "status": "ok", "detail": "尚未创建"})

    all_ok = all(c["status"] != "fail" for c in checks)
    _output({
        "status": "success" if all_ok else "warning",
        "version": __version__,
        "checks": checks,
    })


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
    p_export.set_defaults(func=cmd_export)

    # cite
    p_cite = sub.add_parser("cite", help="生成引用格式")
    p_cite.add_argument("--style", default="gbt7714",
                        choices=["gbt7714", "gb", "footnote", "apa"])
    p_cite.add_argument("--raw", action="store_true", help="输出纯文本而非 JSON")
    p_cite.set_defaults(func=cmd_cite)

    # import
    p_import = sub.add_parser("import", help="导入知网导出的题录文件")
    p_import.add_argument("filepath", help="题录文件路径")
    p_import.set_defaults(func=cmd_import)

    # read-paper
    p_paper = sub.add_parser("read-paper", help="读取论文文件（.docx/.txt/.md）并输出 UTF-8 文本")
    p_paper.add_argument("filepath", help="论文文件路径")
    p_paper.add_argument("--output", help="输出到文件（默认直接打印）")
    p_paper.add_argument("--raw", action="store_true", help="输出纯文本而非 JSON")
    p_paper.set_defaults(func=cmd_read_paper)

    # batch-search
    p_batch = sub.add_parser("batch-search", help="批量知网搜索（一次启动浏览器）")
    p_batch.add_argument("queries", nargs="*", help="搜索关键词列表")
    p_batch.add_argument("--query-file", help="关键词文件路径（每行一个关键词）")
    p_batch.add_argument("--core",
                         help="知网侧边栏来源类别，逗号分隔: 北大核心,CSSCI,AMI,WJCI,CSCD,EI")
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
    p_batch.set_defaults(func=cmd_batch_search)

    # read-detail
    p_read = sub.add_parser("read-detail", help="批量获取论文摘要/全文（需先搜索）")
    p_read.add_argument("--top-n", type=int, default=5,
                        help="获取前 N 篇论文的详情（默认 5）")
    p_read.add_argument("--fulltext", action="store_true",
                        help="抓取 HTML 全文（默认只抓摘要）")
    p_read.set_defaults(func=cmd_read_detail)

    # batch-download
    p_bdl = sub.add_parser("batch-download", help="批量下载知网论文（一次启动浏览器）")
    p_bdl.add_argument("urls", nargs="*", help="知网论文 URL 列表（可选，也可用 --from-session）")
    p_bdl.add_argument("--from-session", action="store_true",
                       help="从上次搜索结果（session.json）读取 URL")
    p_bdl.add_argument("--top-n", type=int, help="配合 --from-session，只下载前 N 篇")
    p_bdl.add_argument("--dir", default="./papers", help="保存目录")
    p_bdl.add_argument("--file-format", choices=["pdf", "caj"], default="pdf")
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

    # check
    p_check = sub.add_parser("check", help="环境自检（Python / 依赖 / 浏览器 / 知网连通性）")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
