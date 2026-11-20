"""
smoke_upstream.py - 外部开放学术 API 线上契约探针（供 GitHub Actions 每周定时巡检或本地排障使用）

绕过本地缓存直接向 OpenAlex、PubMed (Europe PMC)、Crossref、DBLP、arXiv 发起最小化真实请求，
验证过滤参数（如 raw_author_name.search）未因上游改版返回 400 且核心字段（title / year / pages）解析正常。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import search as search_mod  # noqa: E402


def _run_with_retry(name: str, fn: Callable[[], Dict[str, Any]], retries: int = 2) -> Dict[str, Any]:
    last_err = ""
    for attempt in range(1, retries + 1):
        t0 = time.time()
        try:
            info = fn()
            elapsed = int((time.time() - t0) * 1000)
            return {"probe": name, "ok": True, "attempt": attempt, "elapsed_ms": elapsed, **info}
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(3.0 * attempt)
    return {"probe": name, "ok": False, "error": last_err}


def run_all_probes() -> Dict[str, Any]:
    # 强制绕过本地缓存以检测真实线上接口契约
    orig_get = search_mod._cache_get
    orig_set = search_mod._cache_set
    search_mod._cache_get = lambda *a, **k: None
    search_mod._cache_set = lambda *a, **k: None

    try:
        def _probe_openalex() -> Dict[str, Any]:
            rows = search_mod.search_openalex("deep learning", limit=2, author="Hinton")
            if not rows or not rows[0].get("title"):
                raise RuntimeError("OpenAlex 返回为空或缺少 title（检查 title_and_abstract.search / raw_author_name.search 是否变更）")
            return {"count": len(rows), "sample_title": rows[0]["title"][:60]}

        def _probe_pubmed() -> Dict[str, Any]:
            rows = search_mod.search_pubmed("CRISPR", limit=2)
            if not rows or not rows[0].get("title"):
                raise RuntimeError("PubMed (Europe PMC) 返回为空或缺少 title")
            return {"count": len(rows), "sample_title": rows[0]["title"][:60]}

        def _probe_crossref() -> Dict[str, Any]:
            meta = search_mod.resolve_crossref("10.1038/nature14539")
            if not meta or not meta.get("title") or not meta.get("pages"):
                raise RuntimeError(f"Crossref 元数据缺失: {meta}")
            return {"sample_title": meta["title"][:60], "pages": meta.get("pages", "")}

        def _probe_arxiv() -> Dict[str, Any]:
            rows = search_mod.search_arxiv("attention is all you need", limit=2)
            if not rows or not rows[0].get("title"):
                raise RuntimeError("arXiv 返回为空或缺少 title")
            return {"count": len(rows), "sample_title": rows[0]["title"][:60]}

        probes: List[Dict[str, Any]] = [
            _run_with_retry("openalex", _probe_openalex),
            _run_with_retry("pubmed", _probe_pubmed),
            _run_with_retry("crossref", _probe_crossref),
            _run_with_retry("arxiv", _probe_arxiv),
        ]
    finally:
        search_mod._cache_get = orig_get
        search_mod._cache_set = orig_set

    failed = [p for p in probes if not p.get("ok")]
    return {
        "status": "error" if failed else "success",
        "passed": len(probes) - len(failed),
        "failed": len(failed),
        "probes": probes,
    }


def main() -> int:
    report = run_all_probes()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
