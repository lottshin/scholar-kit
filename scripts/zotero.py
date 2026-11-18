"""
zotero.py - 本地 Zotero 文献库零依赖直连模块

支持双通道自动探测（无需安装 pyzotero 等第三方库）：
1. Zotero Desktop 本地 HTTP 接口 (`http://127.0.0.1:23119/api/users/0/...`)
2. 本地 `zotero.sqlite` 只读快照读取（兼容 Zotero 开启或关闭状态，通过临时拷贝避免 WAL 锁冲突，
   自动关联 `~/Zotero/storage/<key>/*.pdf` 本地附件供 `read-detail --fulltext` 提取带页码全文）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def default_zotero_data_dir() -> Optional[Path]:
    """定位本机 Zotero 数据根目录（支持 `ZOTERO_DATA_DIR` 覆盖）。"""
    env_dir = os.environ.get("ZOTERO_DATA_DIR", "").strip()
    if env_dir:
        p = Path(env_dir).expanduser()
        if (p / "zotero.sqlite").is_file():
            return p

    candidates = [
        Path.home() / "Zotero",
        Path.home() / "Documents" / "Zotero",
    ]
    for c in candidates:
        if (c / "zotero.sqlite").is_file():
            return c
    return None


def _probe_local_http_api(timeout: float = 0.6) -> bool:
    try:
        req = urllib.request.Request("http://127.0.0.1:23119/api/users/0/items?limit=1")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return getattr(resp, "status", 200) == 200
    except Exception:
        return False


def _zotero_item_type_to_doc_type(z_type: str) -> str:
    mapping = {
        "journalArticle": "journal",
        "book": "book",
        "bookSection": "chapter",
        "thesis": "thesis",
        "conferencePaper": "conference",
        "report": "report",
        "newspaperArticle": "newspaper",
        "patent": "patent",
        "webpage": "webpage",
        "preprint": "preprint",
    }
    return mapping.get(z_type or "", "journal")


def _extract_year(date_val: str) -> Optional[int]:
    m = re.search(r"((?:19|20)\d{2})", str(date_val or ""))
    return int(m.group(1)) if m else None


def read_zotero_sqlite(
    data_dir: Path,
    *,
    query: str = "",
    collection: str = "",
    limit: int = 30,
) -> Dict[str, Any]:
    """从 `zotero.sqlite` 安全只读提取题录、标签、集合与本地 PDF 附件路径。"""
    db_file = data_dir / "zotero.sqlite"
    if not db_file.is_file():
        return {"status": "error", "code": "ZOTERO_DB_NOT_FOUND", "message": f"未找到 zotero.sqlite: {db_file}"}

    storage_root = data_dir / "storage"
    tmp_dir = Path(tempfile.mkdtemp(prefix="scholarkit_zotero_"))
    snap_db = tmp_dir / "zotero.sqlite"
    try:
        shutil.copy2(str(db_file), str(snap_db))
        for wal_ext in ("-wal", "-shm"):
            side = Path(str(db_file) + wal_ext)
            if side.is_file():
                shutil.copy2(str(side), str(snap_db) + wal_ext)

        conn = sqlite3.connect(str(snap_db))
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()

            # 获取所有分类集合
            cur.execute("SELECT collectionID, collectionName FROM collections ORDER BY collectionName")
            all_collections = [dict(r) for r in cur.fetchall()]

            col_filter_ids: set[int] = set()
            if collection.strip():
                q_col = collection.strip().lower()
                for c in all_collections:
                    if q_col in str(c["collectionName"] or "").lower():
                        col_filter_ids.add(int(c["collectionID"]))
                if not col_filter_ids:
                    return {
                        "status": "warning",
                        "code": "COLLECTION_NOT_FOUND",
                        "message": f"未找到匹配的 Zotero 分类文件夹: {collection}",
                        "available_collections": [c["collectionName"] for c in all_collections],
                        "count": 0,
                        "results": [],
                    }

            # 查询非附件、非笔记、未进回收站的正式文献条目
            cur.execute("""
                SELECT i.itemID, i.key, it.typeName
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                LEFT JOIN deletedItems di ON i.itemID = di.itemID
                WHERE di.itemID IS NULL
                  AND it.typeName NOT IN ('attachment', 'note', 'annotation')
                ORDER BY i.dateModified DESC
            """)
            base_items = cur.fetchall()

            if col_filter_ids:
                placeholders = ",".join("?" for _ in col_filter_ids)
                cur.execute(
                    f"SELECT itemID FROM collectionItems WHERE collectionID IN ({placeholders})",
                    tuple(col_filter_ids),
                )
                allowed_ids = {int(r["itemID"]) for r in cur.fetchall()}
                base_items = [r for r in base_items if int(r["itemID"]) in allowed_ids]

            q_lower = query.strip().lower()
            papers: List[Dict[str, Any]] = []

            for row in base_items:
                item_id = int(row["itemID"])
                item_type = str(row["typeName"])

                cur.execute("""
                    SELECT f.fieldName, idv.value
                    FROM itemData id
                    JOIN fields f ON id.fieldID = f.fieldID
                    JOIN itemDataValues idv ON id.valueID = idv.valueID
                    WHERE id.itemID = ?
                """, (item_id,))
                fields_map = {str(r["fieldName"]): str(r["value"]) for r in cur.fetchall()}

                title = fields_map.get("title", "").strip()
                if not title:
                    continue

                cur.execute("""
                    SELECT c.firstName, c.lastName, c.fieldMode
                    FROM itemCreators ic
                    JOIN creators c ON ic.creatorID = c.creatorID
                    WHERE ic.itemID = ?
                    ORDER BY ic.orderIndex ASC
                """, (item_id,))
                author_list: List[str] = []
                for cr in cur.fetchall():
                    fn = str(cr["firstName"] or "").strip()
                    ln = str(cr["lastName"] or "").strip()
                    if int(cr["fieldMode"] or 0) == 1 or not fn:
                        full = ln or fn
                    elif any("\u4e00" <= ch <= "\u9fff" for ch in (ln + fn)):
                        full = f"{ln}{fn}"
                    else:
                        full = f"{fn} {ln}".strip()
                    if full:
                        author_list.append(full)
                authors_str = "; ".join(author_list)

                cur.execute("""
                    SELECT t.name
                    FROM itemTags itg
                    JOIN tags t ON itg.tagID = t.tagID
                    WHERE itg.itemID = ?
                """, (item_id,))
                tags = [str(r["name"]).strip() for r in cur.fetchall() if r["name"]]

                journal = (
                    fields_map.get("publicationTitle")
                    or fields_map.get("proceedingsTitle")
                    or fields_map.get("bookTitle")
                    or fields_map.get("university")
                    or ""
                )
                abstract = fields_map.get("abstractNote", "")

                if q_lower:
                    haystack = " ".join([title, authors_str, journal, abstract, " ".join(tags)]).lower()
                    if q_lower not in haystack:
                        continue

                # 查找关联的本地 PDF 附件
                local_pdf = ""
                cur.execute("""
                    SELECT att_i.key, ia.path, ia.contentType
                    FROM itemAttachments ia
                    JOIN items att_i ON ia.itemID = att_i.itemID
                    LEFT JOIN deletedItems di ON ia.itemID = di.itemID
                    WHERE ia.parentItemID = ? AND di.itemID IS NULL
                """, (item_id,))
                for att in cur.fetchall():
                    ctype = str(att["contentType"] or "").lower()
                    raw_path = str(att["path"] or "")
                    att_key = str(att["key"] or "")
                    if "pdf" not in ctype and not raw_path.lower().endswith(".pdf"):
                        continue
                    if raw_path.startswith("storage:"):
                        rel_name = raw_path[len("storage:"):]
                        cand = storage_root / att_key / rel_name
                        if cand.is_file():
                            local_pdf = str(cand.resolve())
                            break
                    elif raw_path and Path(raw_path).is_file():
                        local_pdf = str(Path(raw_path).resolve())
                        break
                    att_dir = storage_root / att_key
                    if att_dir.is_dir():
                        pdfs = list(att_dir.glob("*.pdf"))
                        if pdfs:
                            local_pdf = str(pdfs[0].resolve())
                            break

                extra = fields_map.get("extra", "")
                citekey_m = re.search(r"Citation Key:\s*(\S+)", extra, re.IGNORECASE)

                entry: Dict[str, Any] = {
                    "title": title,
                    "authors": authors_str,
                    "journal": journal,
                    "year": _extract_year(fields_map.get("date", "")),
                    "date": fields_map.get("date", ""),
                    "volume": fields_map.get("volume", ""),
                    "issue": fields_map.get("issue", ""),
                    "pages": fields_map.get("pages", ""),
                    "doi": fields_map.get("DOI", ""),
                    "url": fields_map.get("url", ""),
                    "abstract": abstract,
                    "tags": tags,
                    "doc_type": _zotero_item_type_to_doc_type(item_type),
                    "zotero_key": str(row["key"]),
                    "source": "Zotero",
                }
                if citekey_m:
                    entry["citekey"] = citekey_m.group(1)
                if local_pdf:
                    entry["local_pdf"] = local_pdf
                    entry["has_fulltext"] = True

                papers.append(entry)
                if len(papers) >= limit:
                    break

            return {
                "status": "success",
                "backend": "sqlite",
                "data_dir": str(data_dir),
                "collections": [c["collectionName"] for c in all_collections],
                "count": len(papers),
                "results": papers,
            }
        finally:
            conn.close()
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


def query_local_zotero(
    query: str = "",
    *,
    collection: str = "",
    data_dir: Optional[str] = None,
    limit: int = 30,
) -> Dict[str, Any]:
    """统一读取本机 Zotero 文献库（优先快照读取 `zotero.sqlite` 以提取完整本地 PDF 路径，回退本地 HTTP API）。"""
    z_dir = Path(data_dir).expanduser() if data_dir else default_zotero_data_dir()
    if z_dir and (z_dir / "zotero.sqlite").is_file():
        return read_zotero_sqlite(z_dir, query=query, collection=collection, limit=limit)

    if _probe_local_http_api():
        params = {"limit": min(max(limit, 1), 100), "format": "json"}
        if query:
            params["q"] = query
        url = "http://127.0.0.1:23119/api/users/0/items/top?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                raw_items = json.loads(resp.read().decode("utf-8", errors="replace"))
            papers = []
            for item in raw_items:
                d = item.get("data") or {}
                if d.get("itemType") in ("attachment", "note", "annotation") or not d.get("title"):
                    continue
                creators = d.get("creators") or []
                au = "; ".join(
                    (c.get("name") or f"{c.get('lastName', '')}{c.get('firstName', '')}".strip())
                    for c in creators if c.get("name") or c.get("lastName")
                )
                papers.append({
                    "title": d.get("title", ""),
                    "authors": au,
                    "journal": d.get("publicationTitle") or d.get("proceedingsTitle") or "",
                    "year": _extract_year(d.get("date", "")),
                    "volume": d.get("volume", ""),
                    "issue": d.get("issue", ""),
                    "pages": d.get("pages", ""),
                    "doi": d.get("DOI", ""),
                    "url": d.get("url", ""),
                    "abstract": d.get("abstractNote", ""),
                    "tags": [t.get("tag", "") for t in (d.get("tags") or []) if t.get("tag")],
                    "doc_type": _zotero_item_type_to_doc_type(d.get("itemType", "")),
                    "zotero_key": d.get("key", ""),
                    "source": "Zotero",
                })
            return {"status": "success", "backend": "local_http", "count": len(papers), "results": papers}
        except Exception as e:
            return {"status": "error", "code": "ZOTERO_API_FAILED", "message": f"Zotero 本地接口访问失败: {e}"}

    return {
        "status": "error",
        "code": "ZOTERO_NOT_FOUND",
        "message": "未在本机检测到 ~/Zotero/zotero.sqlite（可通过 --data-dir 或环境变量 ZOTERO_DATA_DIR 指定路径，或在 Zotero 设置中开启本地 API）",
    }
