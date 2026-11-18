"""
search_async.py - 异步并发 API 搜索调度器

通过 asyncio.to_thread 并发调度 search.py 中的同步检索函数：
- 完整复用 search.py 的智能 TTL 缓存、SCHOLAR_MAILTO 礼貌池、429 指数退避与 NSSD 绕过代理逻辑
- 所有诊断日志严格输出到 stderr，绝不污染 CLI 的 JSON stdout
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, Dict, List, Optional

import search as _sync_search


def _invert_abstract(inverted_index: Optional[Dict] = None) -> str:
    """兼容导出：从 OpenAlex 倒排索引重建摘要文本"""
    return _sync_search._invert_abstract(inverted_index or {})


async def search_openalex_async(
    query: str,
    limit: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    sort: str = "relevance",
    field: str = "default",
    journal: Optional[str] = None,
    author: Optional[str] = None,
    field_of_study: Optional[str] = None,
    page: int = 1,
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(
        _sync_search.search_openalex,
        query,
        limit=limit,
        year_from=year_from,
        year_to=year_to,
        sort=sort,
        field=field,
        journal=journal,
        author=author,
        field_of_study=field_of_study,
        page=page,
    )


async def search_semantic_scholar_async(
    query: str,
    limit: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    sort: str = "relevance",
    field: str = "default",
    journal: Optional[str] = None,
    author: Optional[str] = None,
    field_of_study: Optional[str] = None,
    page: int = 1,
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(
        _sync_search.search_semantic_scholar,
        query,
        limit=limit,
        year_from=year_from,
        year_to=year_to,
        sort=sort,
        field=field,
        journal=journal,
        author=author,
        field_of_study=field_of_study,
        page=page,
    )


async def search_arxiv_async(
    query: str,
    limit: int = 10,
    sort_by: str = "relevance",
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    page: int = 1,
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(
        _sync_search.search_arxiv,
        query,
        limit=limit,
        sort_by=sort_by,
        year_from=year_from,
        year_to=year_to,
        page=page,
    )


async def search_nssd_async(
    query: str,
    limit: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(
        _sync_search.search_nssd,
        query,
        limit=limit,
        year_from=year_from,
        year_to=year_to,
    )


async def search_dblp_async(
    query: str,
    limit: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(
        _sync_search.search_dblp,
        query,
        limit=limit,
        year_from=year_from,
        year_to=year_to,
    )


async def search_base_async(
    query: str,
    limit: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(
        _sync_search.search_base,
        query,
        limit=limit,
        year_from=year_from,
        year_to=year_to,
    )


async def search_pubmed_async(
    query: str,
    limit: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    sort: str = "relevance",
    field: str = "default",
    journal: Optional[str] = None,
    author: Optional[str] = None,
    page: int = 1,
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(
        _sync_search.search_pubmed,
        query,
        limit=limit,
        year_from=year_from,
        year_to=year_to,
        sort=sort,
        field=field,
        journal=journal,
        author=author,
        page=page,
    )


async def search_all_async(
    query: str,
    limit: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    sort: str = "relevance",
    sources: Optional[List[str]] = None,
    field: str = "default",
    journal: Optional[str] = None,
    author: Optional[str] = None,
    field_of_study: Optional[str] = None,
    page: int = 1,
) -> Dict[str, Any]:
    """异步并发搜索多个数据源。"""
    if sources is None:
        sources = ["openalex", "semantic_scholar", "arxiv", "nssd", "dblp", "base"]

    start_time = time.time()
    tasks = []
    source_names: List[str] = []

    for src in sources:
        normalized = "semantic_scholar" if src == "semantic" else src
        if normalized == "openalex":
            tasks.append(
                search_openalex_async(
                    query, limit, year_from, year_to, sort,
                    field=field, journal=journal, author=author,
                    field_of_study=field_of_study, page=page,
                )
            )
            source_names.append("openalex")
        elif normalized == "semantic_scholar":
            tasks.append(
                search_semantic_scholar_async(
                    query, limit, year_from, year_to, sort,
                    field=field, journal=journal, author=author,
                    field_of_study=field_of_study, page=page,
                )
            )
            source_names.append("semantic_scholar")
        elif normalized == "arxiv":
            tasks.append(search_arxiv_async(query, limit, sort, year_from, year_to, page=page))
            source_names.append("arxiv")
        elif normalized == "nssd":
            tasks.append(search_nssd_async(query, limit, year_from, year_to))
            source_names.append("nssd")
        elif normalized == "dblp":
            tasks.append(search_dblp_async(query, limit, year_from, year_to))
            source_names.append("dblp")
        elif normalized == "base":
            tasks.append(search_base_async(query, limit, year_from, year_to))
            source_names.append("base")
        elif normalized == "pubmed":
            tasks.append(
                search_pubmed_async(
                    query, limit, year_from, year_to, sort,
                    field=field, journal=journal, author=author, page=page,
                )
            )
            source_names.append("pubmed")

    results_list = await asyncio.gather(*tasks, return_exceptions=True)

    all_results: List[Dict[str, Any]] = []
    sources_used: List[str] = []

    for i, res in enumerate(results_list):
        if isinstance(res, Exception):
            print(f"[async] {source_names[i]} 检索异常: {res}", file=sys.stderr)
        elif isinstance(res, list) and res:
            all_results.extend(res)
            sources_used.append(source_names[i])

    elapsed_ms = int((time.time() - start_time) * 1000)
    return {
        "results": all_results,
        "sources_used": sources_used,
        "elapsed_ms": elapsed_ms,
    }


def search_all_sync(
    query: str,
    limit: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    sort: str = "relevance",
    sources: Optional[List[str]] = None,
    field: str = "default",
    journal: Optional[str] = None,
    author: Optional[str] = None,
    field_of_study: Optional[str] = None,
    page: int = 1,
) -> Dict[str, Any]:
    """同步包装器：在同步 CLI 中并发搜索多个 API 数据源。"""
    return asyncio.run(
        search_all_async(
            query,
            limit=limit,
            year_from=year_from,
            year_to=year_to,
            sort=sort,
            sources=sources,
            field=field,
            journal=journal,
            author=author,
            field_of_study=field_of_study,
            page=page,
        )
    )
