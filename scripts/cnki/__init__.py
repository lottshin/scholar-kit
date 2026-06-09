"""
cnki - 知网 (CNKI) 搜索与下载模块
通过 Selenium 自动化操作知网进行文献检索、详情获取和下载。
"""
from __future__ import annotations

from .constants import (
    CNKI_SEARCH_URL, REQUEST_INTERVAL, CNKI_SIDEBAR_CORES,
    HAS_SELENIUM, _log,
)
from .driver import (
    _detect_browser, _create_driver, _load_cookies, _save_cookies,
    _handle_captcha, check_cnki_access, _cookie_path,
    _is_cnki_security_gate, _show_browser_for_captcha,
    authenticate_cnki,
)
from .search import search_cnki, batch_search_cnki
from .detail import get_detail, batch_read_detail
from .download import download_cnki, batch_download_cnki, parse_cnki_export
