"""
search.py - 开放学术 API 搜索模块
支持: OpenAlex, Semantic Scholar, Crossref, Unpaywall, arXiv, NSSD
"""

from __future__ import annotations

import json
import time
import hashlib
import re
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote, urlencode
from xml.etree import ElementTree

try:
    import httpx
except ImportError:
    httpx = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

def _cache_dir() -> Path:
    d = Path.cwd() / ".scholar-kit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_cache_ttl_hours() -> int:
    try:
        from config import get as cfg_get
        days = cfg_get("cache_ttl_days", 30)
        return max(int(days) * 24, 1)
    except ImportError:
        return 30 * 24


CACHE_TTL_HOURS = _get_cache_ttl_hours()
USER_AGENT = "ScholarKit/1.0 (academic-research-tool)"


def _get_mailto() -> str:
    try:
        from config import get as cfg_get
        return cfg_get("mailto", "scholarkit@example.com")
    except ImportError:
        return os.environ.get("SCHOLAR_MAILTO", "scholarkit@example.com")


# ── 缓存 ──────────────────────────────────────────────

# 智能缓存 TTL（小时）- 根据数据源特性调整
CACHE_TTL_BY_SOURCE = {
    "openalex": 24 * 7,      # 7 天（更新较慢）
    "semantic_scholar": 24 * 3,  # 3 天
    "arxiv": 24,             # 1 天（更新快）
    "cnki": 24 * 7,          # 7 天
    "nssd": 24 * 7,          # 7 天
    "crossref": 24 * 30,     # 30 天（元数据稳定）
    "unpaywall": 24 * 7,     # 7 天
    "dblp": 24 * 14,         # 14 天（DBLP 更新较慢）
    "base": 24 * 7,          # 7 天
}

# 查询热度追踪（内存中）
_query_heat_tracker: Dict[str, int] = {}


def _get_query_heat(query: str) -> int:
    """获取查询热度（访问次数）"""
    return _query_heat_tracker.get(query, 0)


def _increment_query_heat(query: str):
    """增加查询热度"""
    _query_heat_tracker[query] = _query_heat_tracker.get(query, 0) + 1


def _get_smart_ttl(source: str, query: str) -> int:
    """根据数据源和查询热度返回智能 TTL（小时）"""
    base_ttl = CACHE_TTL_BY_SOURCE.get(source, CACHE_TTL_HOURS)

    # 检查查询热度
    heat = _get_query_heat(query)

    if heat > 10:  # 热门查询
        return base_ttl * 2  # 延长缓存时间
    elif heat < 2:  # 冷门查询
        return max(base_ttl // 2, 24)  # 缩短缓存时间，最少 24 小时
    else:
        return base_ttl


def _cache_key(prefix: str, query: str) -> str:
    h = hashlib.md5(query.encode()).hexdigest()[:12]
    return f"{prefix}_{h}"


def _cache_get(key: str, source: str = "default", query: str = ""):
    """获取缓存，支持智能 TTL"""
    path = _cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data["_cached_at"])

        # 使用智能 TTL
        ttl_hours = _get_smart_ttl(source, query) if query else CACHE_TTL_HOURS

        if datetime.now() - cached_at > timedelta(hours=ttl_hours):
            path.unlink(missing_ok=True)
            return None

        # 增加查询热度
        if query:
            _increment_query_heat(query)

        return data["results"]
    except Exception:
        return None


def _cache_set(key: str, results, source: str = "default"):
    """设置缓存，记录数据源"""
    path = _cache_dir() / f"{key}.json"
    data = {
        "_cached_at": datetime.now().isoformat(),
        "_source": source,
        "results": results
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── HTTP 工具 ─────────────────────────────────────────

# API 降级链：主数据源失败时的备用数据源
API_FALLBACK_CHAIN = {
    "openalex": ["semantic_scholar", "arxiv"],
    "semantic": ["openalex", "arxiv"],
    "semantic_scholar": ["openalex", "arxiv"],
    "arxiv": ["openalex", "semantic_scholar"],
    "nssd": ["openalex"],
    "dblp": ["openalex", "semantic_scholar"],
    "base": ["openalex"],
}


def _get_client():
    if httpx is not None:
        return httpx.Client(timeout=30, follow_redirects=True)
    return None


def _http_get(url: str, params: Optional[Dict[str, Any]] = None,
              headers: Optional[Dict[str, str]] = None,
              _retries: int = 0) -> Optional[Union[Dict[str, Any], str]]:
    """统一 HTTP GET，支持 httpx 和 urllib 两种后端"""
    MAX_RETRIES = 5
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)

    if httpx is not None:
        try:
            with _get_client() as client:
                resp = client.get(url, params=params, headers=h)
                resp.raise_for_status()
                body = resp.text
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return body
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and _retries < MAX_RETRIES:
                time.sleep(min(30 * (2 ** _retries), 120))
                return _http_get(url, params, headers, _retries=_retries + 1)
            return None
        except Exception:
            return None
    else:
        import urllib.request
        import urllib.error
        if params:
            url = url + "?" + urlencode(params)
        req = urllib.request.Request(url, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return body
        except urllib.error.HTTPError as e:
            if e.code == 429 and _retries < MAX_RETRIES:
                time.sleep(min(30 * (2 ** _retries), 120))
                return _http_get(url, params=None, headers=headers,
                                 _retries=_retries + 1)
            return None
        except Exception:
            return None


def _http_get_no_proxy(url: str, headers: Optional[Dict[str, str]] = None) -> Optional[str]:
    """绕过系统代理直接请求（用于 NSSD 等国内源）"""
    import urllib.request
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(url, headers=h)
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


# ── OpenAlex ──────────────────────────────────────────

def search_openalex(query: str, limit: int = 10, year_from: int = None, year_to: int = None,
                    sort: str = "relevance", field: str = "default", journal: str = None,
                    author: str = None, field_of_study: str = None, page: int = 1) -> list[dict]:
    cache_key = _cache_key("openalex", f"{query}_{limit}_{year_from}_{year_to}_{sort}_{field}_{journal}_{author}_{field_of_study}_{page}")
    cached = _cache_get(cache_key, source="openalex", query=query)
    if cached is not None:
        return cached

    params = {
        "per_page": min(limit, 50),
        "page": page,
        "mailto": _get_mailto(),
    }

    # 根据 field 参数调整搜索方式
    filters = []
    if field == "title":
        # 标题搜索使用 display_name.search filter
        filters.append(f"display_name.search:{query}")
    elif field == "abstract":
        # 摘要搜索使用 abstract.search filter
        filters.append(f"abstract.search:{query}")
    else:
        # 默认全文搜索使用 search 参数
        params["search"] = query

    # 添加排序参数
    if sort == "citations":
        params["sort"] = "cited_by_count:desc"
    elif sort == "date":
        params["sort"] = "publication_date:desc"
    # relevance 是默认排序，不需要传参数

    # 年份过滤
    if year_from is not None:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to is not None:
        filters.append(f"to_publication_date:{year_to}-12-31")

    # 高级过滤：作者（API 支持）
    if author:
        filters.append(f"authorships.author.display_name.search:{author}")

    # 高级过滤：学科领域（API 支持）
    if field_of_study:
        filters.append(f"concepts.display_name.search:{field_of_study}")

    if filters:
        params["filter"] = ",".join(filters)

    data = _http_get("https://api.openalex.org/works", params=params)
    if not data or "results" not in data:
        return []

    results = []
    for w in data["results"]:
        authors = ", ".join(
            a.get("author", {}).get("display_name", "")
            for a in w.get("authorships", [])
        )
        doi = w.get("doi", "")
        if doi and doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]

        loc = w.get("primary_location") or {}
        src = loc.get("source") or {}
        # 提高关键词质量：提升阈值到 0.5，按分数排序，限制数量
        concepts = sorted(
            w.get("concepts", []),
            key=lambda x: x.get("score", 0),
            reverse=True
        )
        keywords = [
            c.get("display_name", "")
            for c in concepts
            if c.get("display_name") and c.get("score", 0) > 0.5
        ][:10]
        # fallback 到 topics
        if not keywords:
            keywords = [
                t.get("display_name", "")
                for t in w.get("topics", [])[:5]
                if t.get("display_name")
            ]
        abstract = _invert_abstract(w.get("abstract_inverted_index"))

        # 摘要质量增强：如果摘要为空且有 DOI，尝试从 Crossref 获取
        if not abstract and doi:
            try:
                crossref_data = resolve_crossref(doi)
                if crossref_data and crossref_data.get("abstract"):
                    abstract = crossref_data["abstract"]
            except Exception:
                pass  # 静默失败，不影响主流程

        # 添加摘要质量标记
        abstract_quality = "full" if len(abstract) > 200 else "partial" if abstract else "none"

        results.append({
            "title": w.get("title", ""),
            "authors": authors,
            "year": w.get("publication_year"),
            "journal": src.get("display_name", ""),
            "doi": doi,
            "cited_by": w.get("cited_by_count", 0),
            "url": w.get("id", ""),
            "abstract": abstract,
            "abstract_quality": abstract_quality,
            "is_oa": w.get("open_access", {}).get("is_oa", False),
            "oa_url": w.get("open_access", {}).get("oa_url", ""),
            "keywords": keywords,
            "source": "OpenAlex",
        })

    # 高级过滤：期刊（客户端过滤，因为 API 过滤不可靠）
    if journal:
        journal_lower = journal.lower()
        results = [r for r in results if journal_lower in r.get("journal", "").lower()]

    _cache_set(cache_key, results, source="openalex")
    return results


def _invert_abstract(inverted_index: dict | None) -> str:
    if not inverted_index:
        return ""
    word_positions = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


def _year_in_range(year, year_from, year_to) -> bool:
    if year is None:
        return True
    try:
        y = int(year)
    except (TypeError, ValueError):
        return True
    if year_from is not None and y < year_from:
        return False
    if year_to is not None and y > year_to:
        return False
    return True


# ── Semantic Scholar ──────────────────────────────────

def search_semantic_scholar(query: str, limit: int = 10, year_from: int = None, year_to: int = None,
                           sort: str = "relevance", field: str = "default", journal: str = None,
                           author: str = None, field_of_study: str = None, page: int = 1) -> list[dict]:
    cache_key = _cache_key("s2", f"{query}_{limit}_{year_from}_{year_to}_{sort}_{field}_{journal}_{author}_{field_of_study}_{page}")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Semantic Scholar 使用 offset 而不是 page
    offset = (page - 1) * limit

    params = {
        "query": query,
        "limit": min(limit, 100),
        "offset": offset,
        "fields": "title,authors,year,abstract,url,citationCount,externalIds,isOpenAccess,openAccessPdf,fieldsOfStudy,venue",
    }
    # 添加排序参数
    if sort == "citations":
        params["sort"] = "citationCount:desc"
    elif sort == "date":
        params["sort"] = "publicationDate:desc"
    # relevance 是默认排序，不需要传参数

    if year_from is not None or year_to is not None:
        if year_from is not None and year_to is not None:
            params["year"] = f"{year_from}-{year_to}"
        elif year_from is not None:
            params["year"] = f"{year_from}-"
        else:
            params["year"] = f"-{year_to}"
    data = _http_get("https://api.semanticscholar.org/graph/v1/paper/search", params=params)
    if not data or "data" not in data:
        return []

    results = []
    for p in data["data"]:
        ext_ids = p.get("externalIds") or {}
        oa_pdf = p.get("openAccessPdf") or {}
        fos = p.get("fieldsOfStudy") or []
        # 改进关键词质量：添加 fallback
        if not fos and p.get("title"):
            # 从标题提取关键词作为 fallback
            title_words = set(p.get("title", "").lower().split())
            stopwords = {"the", "a", "an", "of", "in", "on", "for", "with", "to", "and", "or", "from", "by", "at", "as"}
            fos = [w for w in title_words if len(w) > 3 and w not in stopwords][:5]

        venue = p.get("venue", "") or ""

        results.append({
            "title": p.get("title", ""),
            "authors": ", ".join(a.get("name", "") for a in p.get("authors", [])),
            "year": p.get("year"),
            "journal": venue,
            "doi": ext_ids.get("DOI", ""),
            "arxiv_id": ext_ids.get("ArXiv", ""),
            "cited_by": p.get("citationCount", 0),
            "url": p.get("url", ""),
            "abstract": p.get("abstract", "") or "",
            "keywords": fos,
            "is_oa": p.get("isOpenAccess", False),
            "oa_url": oa_pdf.get("url", ""),
            "source": "Semantic Scholar",
        })

    # Semantic Scholar API 不支持字段搜索和高级过滤，客户端过滤
    if field == "title":
        query_lower = query.lower()
        results = [r for r in results if query_lower in r.get("title", "").lower()]
    elif field == "abstract":
        query_lower = query.lower()
        results = [r for r in results if query_lower in r.get("abstract", "").lower()]

    # 高级过滤：期刊（客户端过滤）
    if journal:
        journal_lower = journal.lower()
        results = [r for r in results if journal_lower in r.get("journal", "").lower()]

    # 高级过滤：作者（客户端过滤）
    if author:
        author_lower = author.lower()
        results = [r for r in results if author_lower in r.get("authors", "").lower()]

    # 高级过滤：学科领域（客户端过滤）
    if field_of_study:
        fos_lower = field_of_study.lower()
        results = [r for r in results
                  if any(fos_lower in kw.lower() for kw in r.get("keywords", []))]

    _cache_set(cache_key, results, source="semantic_scholar")
    return results


# ── Crossref ──────────────────────────────────────────

def resolve_crossref(doi: str) -> dict | None:
    """用 DOI 获取完整元数据（卷期页码等）"""
    cache_key = _cache_key("crossref", doi)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _http_get(f"https://api.crossref.org/works/{quote(doi, safe='')}")
    if not data or "message" not in data:
        return None

    msg = data["message"]
    authors_raw = msg.get("author", [])
    authors = ", ".join(
        f"{a.get('family', '')} {a.get('given', '')}".strip()
        for a in authors_raw
    )

    published = msg.get("published-print") or msg.get("published-online") or {}
    parts = published.get("date-parts") or [[None]]
    date_parts = parts[0] if parts else [None]
    year = date_parts[0] if date_parts else None

    result = {
        "title": (msg.get("title") or [""])[0],
        "authors": authors,
        "year": year,
        "journal": (msg.get("container-title") or [""])[0],
        "volume": msg.get("volume", ""),
        "issue": msg.get("issue", ""),
        "pages": msg.get("page", ""),
        "doi": msg.get("DOI", ""),
        "issn": (msg.get("ISSN") or [""])[0],
        "publisher": msg.get("publisher", ""),
        "type": msg.get("type", ""),
        "abstract": msg.get("abstract", ""),
        "source": "Crossref",
    }

    _cache_set(cache_key, result, source="crossref")
    return result


# ── Unpaywall ─────────────────────────────────────────

def resolve_unpaywall(doi: str, email: str = None) -> dict | None:
    """查找论文的开放获取 PDF 链接"""
    if email is None:
        email = _get_mailto()
    cache_key = _cache_key("unpaywall", doi)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _http_get(f"https://api.unpaywall.org/v2/{quote(doi, safe='')}", params={"email": email})
    if not data:
        return None

    best_loc = data.get("best_oa_location") or {}
    result = {
        "is_oa": data.get("is_oa", False),
        "oa_url": best_loc.get("url_for_pdf") or best_loc.get("url", ""),
        "oa_status": data.get("oa_status", ""),
        "journal": data.get("journal_name", ""),
        "title": data.get("title", ""),
        "doi": doi,
        "source": "Unpaywall",
    }

    _cache_set(cache_key, result, source="unpaywall")
    return result


# ── arXiv ─────────────────────────────────────────────

def search_arxiv(query: str, limit: int = 10, sort_by: str = "relevance", year_from: int = None, year_to: int = None, page: int = 1) -> list[dict]:
    cache_key = _cache_key("arxiv", f"{query}_{limit}_{sort_by}_{year_from}_{year_to}_{page}")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # 修正排序映射：arXiv 不支持按引用数排序
    sort_map = {"relevance": "relevance", "date": "lastUpdatedDate"}
    # citations 排序不支持，使用 relevance 作为 fallback
    sort_param = sort_map.get(sort_by, "relevance")

    # arXiv 使用 start 参数进行分页
    start = (page - 1) * limit

    params = {
        "search_query": f"all:{query}",
        "start": start,
        "max_results": min(limit, 50),
        "sortBy": sort_param,
        "sortOrder": "descending",
    }
    url = "http://export.arxiv.org/api/query?" + urlencode(params)
    text = _http_get(url)
    if not text or not isinstance(text, str):
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []

    results = []
    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
        summary = (entry.findtext("atom:summary", "", ns) or "").strip().replace("\n", " ")
        authors = ", ".join(
            (a.findtext("atom:name", "", ns) or "")
            for a in entry.findall("atom:author", ns)
        )
        published = entry.findtext("atom:published", "", ns)
        year = int(published[:4]) if published and len(published) >= 4 else None

        arxiv_id = ""
        entry_id = entry.findtext("atom:id", "", ns) or ""
        if "arxiv.org/abs/" in entry_id:
            arxiv_id = entry_id.split("arxiv.org/abs/")[-1]

        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            if link.get("title") == "pdf":
                pdf_url = link.get("href", "")
                break

        categories = []
        arxiv_ns = {"arxiv": "http://arxiv.org/schemas/atom"}
        for cat in entry.findall("atom:category", ns):
            term = cat.get("term", "")
            if term:
                categories.append(term)
        if not categories:
            for cat in entry.findall("arxiv:primary_category", arxiv_ns):
                term = cat.get("term", "")
                if term:
                    categories.append(term)

        results.append({
            "title": title,
            "authors": authors,
            "year": year,
            "journal": "arXiv preprint",
            "doi": "",
            "arxiv_id": arxiv_id,
            "cited_by": 0,
            "url": entry_id,
            "abstract": summary,
            "keywords": categories,
            "is_oa": True,
            "oa_url": pdf_url,
            "source": "arXiv",
        })

    if year_from is not None or year_to is not None:
        results = [r for r in results if _year_in_range(r.get("year"), year_from, year_to)]

    _cache_set(cache_key, results, source="arxiv")
    return results


# ── NSSD ──────────────────────────────────────────────

def search_nssd(query: str, limit: int = 10, year_from: int = None, year_to: int = None) -> list[dict]:
    if not BeautifulSoup:
        return []

    cache_key = _cache_key("nssd", f"{query}_{limit}_{year_from}_{year_to}")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"https://www.nssd.cn/literature/search?query={quote(query)}&dataType=journal&pageSize={min(limit, 20)}"
    html = _http_get_no_proxy(url, headers={"Accept": "text/html"})
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []

    items = soup.select(".article-list .article-item, .search-result-list li, .result-item")
    for item in items[:limit]:
        title_el = item.select_one("a.title, h3 a, .article-title a")
        if not title_el:
            continue

        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = "https://www.nssd.cn" + href

        author_el = item.select_one(".author, .article-author")
        authors = author_el.get_text(strip=True) if author_el else ""

        journal_el = item.select_one(".journal, .article-source")
        journal = journal_el.get_text(strip=True) if journal_el else ""

        year_el = item.select_one(".year, .article-date")
        year_text = year_el.get_text(strip=True) if year_el else ""
        year_match = re.search(r"(\d{4})", year_text)
        year = int(year_match.group(1)) if year_match else None

        results.append({
            "title": title,
            "authors": authors,
            "year": year,
            "journal": journal,
            "doi": "",
            "cited_by": 0,
            "url": href,
            "abstract": "",
            "is_oa": True,
            "oa_url": "",
            "keywords": [],
            "source": "NSSD",
        })

    if year_from is not None or year_to is not None:
        results = [r for r in results if _year_in_range(r.get("year"), year_from, year_to)]

    _cache_set(cache_key, results, source="nssd")
    return results


# ── DBLP ──────────────────────────────────────────────

def search_dblp(query: str, limit: int = 10, year_from: int = None, year_to: int = None) -> list[dict]:
    """
    搜索 DBLP（计算机科学文献数据库）

    DBLP 是计算机科学领域最权威的文献索引，覆盖所有主要会议和期刊。
    API 文档: https://dblp.org/faq/How+to+use+the+dblp+search+API.html
    """
    cache_key = _cache_key("dblp", f"{query}_{limit}_{year_from}_{year_to}")
    cached = _cache_get(cache_key, source="dblp", query=query)
    if cached is not None:
        return cached

    params = {
        "q": query,
        "h": min(limit, 100),  # DBLP 最多返回 100 条
        "format": "json",
    }

    data = _http_get("https://dblp.org/search/publ/api", params=params)
    if not data or "result" not in data:
        return []

    hits = data.get("result", {}).get("hits", {}).get("hit", [])
    if not hits:
        return []

    results = []
    for hit in hits:
        info = hit.get("info", {})

        # 解析作者
        authors_data = info.get("authors", {}).get("author", [])
        if isinstance(authors_data, dict):
            authors_data = [authors_data]
        authors = ", ".join(a.get("text", "") if isinstance(a, dict) else str(a) for a in authors_data)

        # 解析年份
        year = info.get("year")
        if year:
            try:
                year = int(year)
            except (ValueError, TypeError):
                year = None

        # 年份过滤
        if year_from and year and year < year_from:
            continue
        if year_to and year and year > year_to:
            continue

        # 解析 venue（会议或期刊）
        venue = info.get("venue", "")

        # 解析 DOI
        doi = info.get("doi", "")

        # 解析 URL
        url = info.get("url", "")
        ee = info.get("ee", "")  # 电子版链接

        # 解析类型
        doc_type = info.get("type", "")

        results.append({
            "title": info.get("title", ""),
            "authors": authors,
            "year": year,
            "journal": venue,
            "doi": doi,
            "cited_by": 0,  # DBLP 不提供引用数
            "url": url,
            "abstract": "",  # DBLP 不提供摘要
            "keywords": [doc_type] if doc_type else [],
            "is_oa": bool(ee),  # 有电子版链接视为开放获取
            "oa_url": ee if isinstance(ee, str) else "",
            "source": "DBLP",
            "doc_type": doc_type,  # Conference Paper, Journal Article, etc.
        })

    _cache_set(cache_key, results, source="dblp")
    return results


# ── BASE ──────────────────────────────────────────────

def search_base(query: str, limit: int = 10, year_from: int = None, year_to: int = None) -> list[dict]:
    """
    搜索 BASE（Bielefeld Academic Search Engine）

    BASE 是欧洲最大的开放获取学术搜索引擎，索引超过 3.5 亿份文档。
    API 文档: https://www.base-search.net/about/en/about_develop.php

    注意：BASE API 可能会限制某些 IP 地址或 User-Agent，如遇到访问限制，建议使用其他数据源。
    """
    cache_key = _cache_key("base", f"{query}_{limit}_{year_from}_{year_to}")
    cached = _cache_get(cache_key, source="base", query=query)
    if cached is not None:
        return cached

    params = {
        "func": "PerformSearch",
        "query": query,
        "hits": min(limit, 50),  # BASE 建议最多 50 条
        "format": "json",
    }

    # BASE API 对 User-Agent 有限制，使用标准浏览器 UA
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    data = _http_get("https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi",
                     params=params, headers=headers)

    # 检查是否被拒绝访问
    if isinstance(data, dict) and "error" in data:
        import sys
        print(f"[BASE] API 访问受限: {data.get('error')}", file=sys.stderr)
        return []

    if not data or "response" not in data:
        return []

    docs = data.get("response", {}).get("docs", [])
    if not docs:
        return []

    results = []
    for doc in docs:
        # 解析作者
        authors_data = doc.get("dcauthor", [])
        if isinstance(authors_data, str):
            authors_data = [authors_data]
        authors = ", ".join(authors_data)

        # 解析年份
        year = None
        year_data = doc.get("dcyear")
        if year_data:
            try:
                year = int(year_data[0]) if isinstance(year_data, list) else int(year_data)
            except (ValueError, TypeError, IndexError):
                pass

        # 年份过滤
        if year_from and year and year < year_from:
            continue
        if year_to and year and year > year_to:
            continue

        # 解析标题
        title_data = doc.get("dctitle", [])
        title = title_data[0] if isinstance(title_data, list) and title_data else str(title_data) if title_data else ""

        # 解析摘要
        abstract_data = doc.get("dcdescription", [])
        abstract = abstract_data[0] if isinstance(abstract_data, list) and abstract_data else str(abstract_data) if abstract_data else ""

        # 解析来源
        source_data = doc.get("dcsource", [])
        journal = source_data[0] if isinstance(source_data, list) and source_data else str(source_data) if source_data else ""

        # 解析 DOI
        doi_data = doc.get("dcdoi", [])
        doi = doi_data[0] if isinstance(doi_data, list) and doi_data else str(doi_data) if doi_data else ""

        # 解析链接
        link_data = doc.get("dclink", [])
        url = link_data[0] if isinstance(link_data, list) and link_data else str(link_data) if link_data else ""

        # 解析主题
        subject_data = doc.get("dcsubject", [])
        if isinstance(subject_data, str):
            subject_data = [subject_data]
        keywords = subject_data[:10] if subject_data else []

        results.append({
            "title": title,
            "authors": authors,
            "year": year,
            "journal": journal,
            "doi": doi,
            "cited_by": 0,  # BASE 不提供引用数
            "url": url,
            "abstract": abstract,
            "keywords": keywords,
            "is_oa": True,  # BASE 主要索引开放获取资源
            "oa_url": url,
            "source": "BASE",
        })

    _cache_set(cache_key, results, source="base")
    return results


# ── 聚合搜索 ─────────────────────────────────────────

def search_with_fallback(query: str, primary_source: str, limit: int = 10,
                         year_from: int = None, year_to: int = None,
                         sort: str = "relevance", **kwargs) -> Dict[str, Any]:
    """
    带降级的搜索：主数据源失败时自动切换到备用数据源

    Args:
        query: 搜索关键词
        primary_source: 主数据源（openalex/semantic_scholar/arxiv/nssd）
        limit: 结果数量
        year_from: 起始年份
        year_to: 截止年份
        sort: 排序方式
        **kwargs: 其他参数（field、journal、author 等）

    Returns:
        {
            "source": 实际使用的数据源,
            "results": 搜索结果列表,
            "fallback": 是否使用了降级,
            "original_source": 原始请求的数据源（仅在降级时存在）,
            "error": 错误信息（仅在所有数据源都失败时存在）
        }
    """
    # 数据源映射
    source_functions = {
        "openalex": search_openalex,
        "semantic_scholar": search_semantic_scholar,
        "semantic": search_semantic_scholar,
        "arxiv": search_arxiv,
        "nssd": search_nssd,
        "dblp": search_dblp,
        "base": search_base,
    }

    def call_source(source_name: str, source_func):
        common = {
            "limit": limit,
            "year_from": year_from,
            "year_to": year_to,
        }
        if source_name == "arxiv":
            return source_func(
                query,
                sort_by=sort,
                page=kwargs.get("page", 1),
                **common,
            )
        if source_name in ("openalex", "semantic", "semantic_scholar"):
            api_kwargs = {
                key: value
                for key, value in kwargs.items()
                if key in ("field", "journal", "author", "field_of_study", "page")
            }
            return source_func(query, sort=sort, **common, **api_kwargs)
        return source_func(query, **common)

    # 尝试主数据源
    primary_func = source_functions.get(primary_source)
    if not primary_func:
        return {
            "source": None,
            "results": [],
            "error": f"未知数据源: {primary_source}"
        }

    try:
        results = call_source(primary_source, primary_func)
        if results and len(results) > 0:
            return {
                "source": primary_source,
                "results": results,
                "fallback": False
            }
    except Exception as e:
        import sys
        print(f"[fallback] {primary_source} 失败: {e}", file=sys.stderr)

    # 尝试备用数据源
    fallback_sources = API_FALLBACK_CHAIN.get(primary_source, [])
    for fallback_source in fallback_sources:
        try:
            fallback_func = source_functions.get(fallback_source)
            if not fallback_func:
                continue

            print(f"[fallback] 尝试备用数据源: {fallback_source}", file=__import__('sys').stderr)

            results = call_source(fallback_source, fallback_func)
            if results and len(results) > 0:
                return {
                    "source": fallback_source,
                    "results": results,
                    "fallback": True,
                    "original_source": primary_source
                }
        except Exception as e:
            print(f"[fallback] {fallback_source} 也失败: {e}", file=__import__('sys').stderr)
            continue

    # 所有数据源都失败
    return {
        "source": None,
        "results": [],
        "fallback": True,
        "original_source": primary_source,
        "error": f"所有数据源均失败（尝试了 {primary_source} 和 {', '.join(fallback_sources)}）"
    }


def search_all(query: str, limit: int = 10, year_from: int = None, year_to: int = None) -> list[dict]:
    """多源聚合搜索（不含知网，知网由 cnki 包单独处理）"""
    all_results = []

    all_results.extend(search_openalex(query, limit=limit, year_from=year_from, year_to=year_to))
    time.sleep(0.5)
    all_results.extend(search_semantic_scholar(query, limit=limit, year_from=year_from, year_to=year_to))
    time.sleep(0.5)
    all_results.extend(search_arxiv(query, limit=min(limit, 10), year_from=year_from, year_to=year_to))
    time.sleep(0.5)
    all_results.extend(search_nssd(query, limit=min(limit, 10), year_from=year_from, year_to=year_to))
    time.sleep(0.5)
    all_results.extend(search_dblp(query, limit=limit, year_from=year_from, year_to=year_to))
    time.sleep(0.5)
    all_results.extend(search_base(query, limit=limit, year_from=year_from, year_to=year_to))

    seen_titles = set()
    deduped = []
    for r in all_results:
        normalized = (r.get("title") or "").lower().strip()
        if normalized and normalized not in seen_titles:
            seen_titles.add(normalized)
            deduped.append(r)

    deduped.sort(key=lambda x: x.get("cited_by", 0), reverse=True)
    return deduped[:limit * 3]


# ── 引文网络分析 ──────────────────────────────────────

def get_citations(paper_id: str, direction: str = "both",
                  limit: int = 20) -> Dict[str, Any]:
    """获取论文的引文网络（前向引用 / 后向引用）。

    Args:
        paper_id: DOI、OpenAlex ID、Semantic Scholar ID 或论文 URL
        direction: "citing"（谁引了它）、"cited"（它引了谁）、"both"
        limit: 每个方向最多返回条数

    Returns:
        {"paper": {...}, "citing": [...], "references": [...]}
    """
    cache_key = _cache_key("citations", f"{paper_id}_{direction}_{limit}")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result: Dict[str, Any] = {"paper": {}, "citing": [], "references": []}

    s2_id = _resolve_s2_id(paper_id)
    if not s2_id:
        return {"error": f"无法识别论文 ID: {paper_id}",
                "paper": {}, "citing": [], "references": []}

    fields = "title,authors,year,citationCount,externalIds,url"
    paper_data = _http_get(
        f"https://api.semanticscholar.org/graph/v1/paper/{s2_id}",
        params={"fields": fields},
    )
    if paper_data and isinstance(paper_data, dict) and "title" in paper_data:
        ext = paper_data.get("externalIds") or {}
        result["paper"] = {
            "title": paper_data.get("title", ""),
            "authors": ", ".join(a.get("name", "") for a in paper_data.get("authors", [])),
            "year": paper_data.get("year"),
            "cited_by_count": paper_data.get("citationCount", 0),
            "doi": ext.get("DOI", ""),
            "s2_id": s2_id,
        }

    if direction in ("citing", "both"):
        time.sleep(1)
        citing = _http_get(
            f"https://api.semanticscholar.org/graph/v1/paper/{s2_id}/citations",
            params={"fields": fields, "limit": min(limit, 100)},
        )
        if citing and isinstance(citing, dict):
            for item in (citing.get("data") or []):
                cp = item.get("citingPaper", {})
                if not cp.get("title"):
                    continue
                ext = cp.get("externalIds") or {}
                result["citing"].append({
                    "title": cp.get("title", ""),
                    "authors": ", ".join(a.get("name", "") for a in cp.get("authors", [])),
                    "year": cp.get("year"),
                    "citation_count": cp.get("citationCount", 0),
                    "doi": ext.get("DOI", ""),
                    "url": cp.get("url", ""),
                    "source": "Semantic Scholar",
                })

    if direction in ("cited", "both"):
        time.sleep(1)
        refs = _http_get(
            f"https://api.semanticscholar.org/graph/v1/paper/{s2_id}/references",
            params={"fields": fields, "limit": min(limit, 100)},
        )
        if refs and isinstance(refs, dict):
            for item in (refs.get("data") or []):
                rp = item.get("citedPaper", {})
                if not rp.get("title"):
                    continue
                ext = rp.get("externalIds") or {}
                result["references"].append({
                    "title": rp.get("title", ""),
                    "authors": ", ".join(a.get("name", "") for a in rp.get("authors", [])),
                    "year": rp.get("year"),
                    "citation_count": rp.get("citationCount", 0),
                    "doi": ext.get("DOI", ""),
                    "url": rp.get("url", ""),
                    "source": "Semantic Scholar",
                })

    if result["paper"]:
        _cache_set(cache_key, result, source="semantic_scholar")
    return result


def _resolve_s2_id(paper_id: str) -> Optional[str]:
    """将各种论文标识符解析为 Semantic Scholar Paper ID"""
    pid = paper_id.strip()
    if pid.lower().startswith("doi:"):
        return f"DOI:{pid[4:]}"
    if pid.startswith("10."):
        return f"DOI:{pid}"
    if pid.startswith("https://doi.org/"):
        return f"DOI:{pid[16:]}"
    if pid.startswith("http://doi.org/"):
        return f"DOI:{pid[15:]}"
    if "semanticscholar.org/paper/" in pid:
        return pid.rsplit("/", 1)[-1]
    if pid.startswith("https://openalex.org/W"):
        doi = _openalex_to_doi(pid)
        return f"DOI:{doi}" if doi else None
    if pid.startswith("W") and len(pid) > 5:
        doi = _openalex_to_doi(f"https://openalex.org/{pid}")
        return f"DOI:{doi}" if doi else None
    if "arxiv.org/abs/" in pid:
        arxiv_id = pid.split("arxiv.org/abs/")[-1].split("?")[0]
        return f"ArXiv:{arxiv_id}"
    if pid.startswith("arXiv:") or pid.startswith("arxiv:"):
        return f"ArXiv:{pid.split(':',1)[1]}"
    if re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", pid):
        return f"ArXiv:{pid}"
    return None


def _openalex_to_doi(openalex_url: str) -> Optional[str]:
    """从 OpenAlex ID 获取 DOI"""
    data = _http_get(openalex_url, params={"mailto": _get_mailto()})
    if data and isinstance(data, dict):
        doi = data.get("doi", "")
        if doi and doi.startswith("https://doi.org/"):
            return doi[16:]
        return doi or None
    return None


# ── 结果去重 ──────────────────────────────────────────

def deduplicate_results(results: list[dict]) -> list[dict]:
    """基于 DOI 和标题去重，保留第一个出现的版本。

    优先级：DOI 精确匹配 > 标题相似度 > 保留

    Args:
        results: 搜索结果列表

    Returns:
        去重后的结果列表
    """
    seen_dois = set()
    seen_titles = set()
    unique = []

    for r in results:
        # DOI 去重（精确匹配）
        doi = (r.get("doi") or "").lower().strip()
        if doi and doi in seen_dois:
            continue
        if doi:
            seen_dois.add(doi)

        # 标题去重（标准化后精确匹配）
        title = (r.get("title") or "").lower().strip()
        # 移除多余空格和标点
        title = re.sub(r'\s+', ' ', title)
        title = re.sub(r'[^\w\s]', '', title)

        if title and title in seen_titles:
            continue
        if title:
            seen_titles.add(title)

        unique.append(r)

    return unique


# ── 质量评分 ──────────────────────────────────────────

def calculate_quality_score(paper: dict) -> float:
    """计算论文质量分数（0-100）。

    评分维度：
    - 摘要完整性（0-30）
    - DOI 存在（20）
    - 被引次数（0-20，对数归一化）
    - 关键词存在（10）
    - 开放获取（10）
    - 数据源可靠性（5）
    - 年份新近性（0-10）

    Args:
        paper: 论文字典

    Returns:
        质量分数（0-100）
    """
    import math
    from datetime import datetime

    score = 0.0

    # 摘要完整性
    abstract = paper.get("abstract", "")
    if len(abstract) > 500:
        score += 30
    elif len(abstract) > 200:
        score += 20
    elif abstract:
        score += 10

    # DOI 存在
    if paper.get("doi"):
        score += 20

    # 被引次数（对数归一化）
    cited_by = paper.get("cited_by", 0)
    if cited_by > 0:
        score += min(20, math.log10(cited_by + 1) * 5)

    # 关键词存在
    keywords = paper.get("keywords", [])
    if keywords and len(keywords) > 0:
        score += 10

    # 开放获取
    if paper.get("is_oa"):
        score += 10

    # 数据源可靠性
    source = paper.get("source", "")
    if source in ("OpenAlex", "Semantic Scholar"):
        score += 5
    elif source == "arXiv":
        score += 3

    # 年份新近性（最近 5 年）
    year = paper.get("year")
    if year:
        current_year = datetime.now().year
        age = current_year - year
        if age <= 5:
            score += max(0, 10 - (age * 2))  # 每年递减 2 分

    return min(100, score)


# ── 研究趋势分析 ──────────────────────────────────────

def analyze_trends(papers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对论文列表进行趋势聚合分析。

    Args:
        papers: 搜索结果列表（需含 year、cited_by、keywords 等字段）

    Returns:
        {
            "total": int,
            "year_distribution": {year: count, ...},
            "top_cited": [...],
            "keyword_frequency": {keyword: count, ...},
            "source_distribution": {source: count, ...},
        }
    """
    if not papers:
        return {"total": 0, "year_distribution": {}, "top_cited": [],
                "keyword_frequency": {}, "source_distribution": {}}

    year_dist: Dict[int, int] = {}
    kw_freq: Dict[str, int] = {}
    source_dist: Dict[str, int] = {}

    for p in papers:
        y = p.get("year")
        if y:
            year_dist[y] = year_dist.get(y, 0) + 1

        kws = p.get("keywords", [])
        if isinstance(kws, str):
            kws = [k.strip() for k in kws.split(";") if k.strip()]
        if not isinstance(kws, list):
            kws = []
        for kw in kws:
            kw_clean = str(kw).strip().lower()
            if kw_clean:
                kw_freq[kw_clean] = kw_freq.get(kw_clean, 0) + 1

        src = p.get("source", "unknown")
        source_dist[src] = source_dist.get(src, 0) + 1

    sorted_years = dict(sorted(year_dist.items()))

    sorted_kw = sorted(kw_freq.items(), key=lambda x: x[1], reverse=True)[:30]

    top_cited = sorted(papers, key=lambda x: x.get("cited_by", 0), reverse=True)[:10]
    top_cited_brief = [
        {
            "title": p.get("title", ""),
            "authors": p.get("authors", ""),
            "year": p.get("year"),
            "cited_by": p.get("cited_by", 0),
            "journal": p.get("journal", ""),
            "source": p.get("source", ""),
        }
        for p in top_cited
    ]

    return {
        "total": len(papers),
        "year_distribution": sorted_years,
        "top_cited": top_cited_brief,
        "keyword_frequency": dict(sorted_kw),
        "source_distribution": source_dist,
    }
