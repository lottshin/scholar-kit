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

def _cache_key(prefix: str, query: str) -> str:
    h = hashlib.md5(query.encode()).hexdigest()[:12]
    return f"{prefix}_{h}"


def _cache_get(key: str):
    path = _cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data["_cached_at"])
        if datetime.now() - cached_at > timedelta(hours=CACHE_TTL_HOURS):
            path.unlink(missing_ok=True)
            return None
        return data["results"]
    except Exception:
        return None


def _cache_set(key: str, results):
    path = _cache_dir() / f"{key}.json"
    data = {"_cached_at": datetime.now().isoformat(), "results": results}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── HTTP 工具 ─────────────────────────────────────────

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

def search_openalex(query: str, limit: int = 10, year_from: int = None, year_to: int = None) -> list[dict]:
    cache_key = _cache_key("openalex", f"{query}_{limit}_{year_from}_{year_to}")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = {
        "search": query,
        "per_page": min(limit, 50),
        "mailto": _get_mailto(),
    }

    filters = []
    if year_from is not None:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to is not None:
        filters.append(f"to_publication_date:{year_to}-12-31")
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
        keywords = [
            c.get("display_name", "")
            for c in w.get("concepts", [])
            if c.get("display_name") and c.get("score", 0) > 0.3  # score 为 0-1 相关度，0.3 过滤低相关概念
        ]
        if not keywords:
            keywords = [
                t.get("display_name", "")
                for t in w.get("topics", [])
                if t.get("display_name")
            ]
        results.append({
            "title": w.get("title", ""),
            "authors": authors,
            "year": w.get("publication_year"),
            "journal": src.get("display_name", ""),
            "doi": doi,
            "cited_by": w.get("cited_by_count", 0),
            "url": w.get("id", ""),
            "abstract": _invert_abstract(w.get("abstract_inverted_index")),
            "is_oa": w.get("open_access", {}).get("is_oa", False),
            "oa_url": w.get("open_access", {}).get("oa_url", ""),
            "keywords": keywords,
            "source": "OpenAlex",
        })

    _cache_set(cache_key, results)
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

def search_semantic_scholar(query: str, limit: int = 10, year_from: int = None, year_to: int = None) -> list[dict]:
    cache_key = _cache_key("s2", f"{query}_{limit}_{year_from}_{year_to}")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = {
        "query": query,
        "limit": min(limit, 100),
        "fields": "title,authors,year,abstract,url,citationCount,externalIds,isOpenAccess,openAccessPdf,fieldsOfStudy",
    }
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
        results.append({
            "title": p.get("title", ""),
            "authors": ", ".join(a.get("name", "") for a in p.get("authors", [])),
            "year": p.get("year"),
            "journal": "",
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

    _cache_set(cache_key, results)
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
        "source": "Crossref",
    }

    _cache_set(cache_key, result)
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

    _cache_set(cache_key, result)
    return result


# ── arXiv ─────────────────────────────────────────────

def search_arxiv(query: str, limit: int = 10, sort_by: str = "relevance", year_from: int = None, year_to: int = None) -> list[dict]:
    cache_key = _cache_key("arxiv", f"{query}_{limit}_{sort_by}_{year_from}_{year_to}")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    sort_map = {"relevance": "relevance", "date": "lastUpdatedDate", "citations": "relevance"}
    params = {
        "search_query": f"all:{query}",
        "max_results": min(limit, 50),
        "sortBy": sort_map.get(sort_by, "relevance"),
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

    _cache_set(cache_key, results)
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

    _cache_set(cache_key, results)
    return results


# ── 聚合搜索 ─────────────────────────────────────────

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
        _cache_set(cache_key, result)
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
