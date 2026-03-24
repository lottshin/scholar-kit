# 错误码对照表

脚本返回 `{"status": "error", "code": "...", "message": "..."}`，不含建议。
Agent 根据此表决定如何回应用户。

| 错误码 | 含义 | Agent 应对 |
|--------|------|-----------|
| `CNKI_UNREACHABLE` | 无法连接知网 | 提示检查校园网/VPN |
| `CNKI_SEARCH_FAILED` | 搜索过程异常 | 提示网络问题，建议稍后重试 |
| `CNKI_BATCH_FAILED` | 批量搜索异常 | 同上 |
| `CNKI_DETAIL_FAILED` | 获取论文详情失败 | 提示网络问题或该论文页面结构异常 |
| `CNKI_DOWNLOAD_FAILED` | 下载失败 | 提示手动下载或检查网络 |
| `CNKI_BATCH_DOWNLOAD_FAILED` | 批量下载异常 | 同上 |
| `DOWNLOAD_BTN_NOT_FOUND` | 未找到下载按钮 | 该论文可能不支持在线下载（博硕论文等） |
| `DOWNLOAD_TIMEOUT` | 下载等待超时（warning） | 文件可能仍在下载，让用户检查目录 |
| `NO_URLS` | 未提供下载 URL | Agent 应从搜索结果中选取 URL 后传入 |
| `NO_KEYWORDS` | 未提供搜索关键词 | Agent 应从用户请求中提取关键词 |
| `ADV_SEARCH_FALLBACK` | 高级搜索失败且无关键词可回退 | 提示用户补充关键词，或建议稍后重试；Agent 也可尝试改为传 keyword 重新搜索 |
| `UNKNOWN_SOURCE` | 未知数据源 | 可用：cnki, openalex, semantic, arxiv, nssd, all |
| `UNSUPPORTED_URL` | 非知网 URL | 仅支持知网直接下载，可改用 `--doi` 查 OA |
| `OA_NOT_FOUND` | DOI 查询无 OA 且无知网 URL 可回退 | 返回 metadata 供 Agent 展示；建议用户手动获取或用知网 URL |
| `NO_DOWNLOAD_TARGET` | 未提供 URL 也未提供 DOI | Agent 应从搜索结果选取 URL 或提取 DOI |
| `UNSUPPORTED_FORMAT` | 不支持的文件格式 | 支持 .docx/.txt/.md；PDF 用 Agent 内置读取工具 |
| `FILE_NOT_FOUND` | 指定文件不存在 | 检查路径是否正确 |
| `DOCX_PARSE_FAILED` | docx 文件解析异常 | 提示用户确认文件完整性 |
| `ENCODING_ERROR` | 文本文件编码无法识别 | 建议用户转为 UTF-8 编码 |
| `IMPORT_PARSE_FAILED` | 题录文件解析失败 | 检查文件格式是否为 NoteExpress/Refworks/BibTeX |
| `NO_SESSION_DATA` | 无会话数据 | 提示先执行 search 或 batch-search |
| `NO_URL` | 未提供 URL 参数 | Agent 应从搜索结果中获取 URL |
| `UNSUPPORTED_EXPORT_FORMAT` | 不支持的导出格式 | 支持 bibtex/ris/markdown/json/excel/gbt7714/footnote/apa |
| `MISSING_DEPENDENCY` | 缺少依赖包 | 提示用户 `pip install` |
| `IO_ERROR` | 文件保存失败（磁盘满、只读等） | 检查磁盘空间和写入权限 |
| `PATCH_PARSE_FAILED` | 补丁 JSON 解析失败 | 检查 JSON 格式是否正确 |
