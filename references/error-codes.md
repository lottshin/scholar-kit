# 错误码对照表

脚本在 `status` 为 `error`、`warning` 或 `partial` 时可能附带 `code` 字段，格式为 `{"status": "error|warning|partial", "code": "...", "message": "..."}`。
注意：部分 `warning`/`partial` 响应无 `code` 字段（如 `check`、`write-docx`、`patch-docx`），需读 `message` 或 `warnings` 数组。
`search --source all` 时知网失败不阻断，错误信息在 `cnki_error` 子字段中，Agent 应检查并提示用户。
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
| `NO_RESULTS` | 搜索无匹配结果 | 尝试同义词/英文关键词、放宽年份或改用 `--source all --enable-fallback` |
| `ADV_SEARCH_FALLBACK` | 高级搜索失败且无关键词可回退 | 提示用户补充关键词，或建议稍后重试；Agent 也可尝试改为传 keyword 重新搜索 |
| `UNKNOWN_SOURCE` | 未知数据源 | 可用：cnki, openalex, semantic, arxiv, nssd, dblp, base, all |
| `DRIVER_MISSING` | 浏览器驱动缺失 | 运行 `check --fix`，必要时以平台提权方式允许联网下载驱动 |
| `SANDBOX_BLOCKED` | 沙箱阻止网络或浏览器能力 | 按 `check.capabilities.retry_command` 提权重试，不要直接判定 CNKI 不可用 |
| `API_RATE_LIMIT` | API 源触发速率限制 | 等待后重试，或切换到其他 API 源 / 使用 `--enable-fallback` |
| `UNSUPPORTED_URL` | 非知网 URL | 仅支持知网直接下载，可改用 `--doi` 查 OA |
| `OA_NOT_FOUND` | DOI 查询无 OA 且无知网 URL 可回退 | 返回 metadata 供 Agent 展示；建议用户手动获取或用知网 URL |
| `NO_DOWNLOAD_TARGET` | 未提供 URL 也未提供 DOI | Agent 应从搜索结果选取 URL 或提取 DOI |
| `UNSUPPORTED_FORMAT` | 不支持的文件格式 | 支持 .docx/.txt/.md；PDF 用 Agent 内置读取工具 |
| `NOT_PDF` | `pdf-meta` 输入不是 PDF 文件 | 检查文件路径和后缀 |
| `PDF_READ_FAILED` | PDF 元数据读取失败 | 换用 Agent 内置 PDF 读取工具或检查文件是否损坏 |
| `FILE_NOT_FOUND` | 指定文件不存在 | 检查路径是否正确 |
| `DOCX_PARSE_FAILED` | docx 文件解析异常 | 提示用户确认文件完整性 |
| `ENCODING_ERROR` | 文本文件编码无法识别 | 建议用户转为 UTF-8 编码 |
| `IMPORT_PARSE_FAILED` | 题录文件解析失败 | 检查文件格式是否为 NoteExpress/Refworks/BibTeX |
| `NO_SESSION_DATA` | 无会话数据 | 提示先执行 search、batch-search 或 import。注意：`read-detail` 在会话仅含 API 源论文（无知网论文）时也返回此码（status=warning），此时应提示用户 read-detail 仅支持知网论文 |
| `NO_URL` | 未提供 URL 参数 | Agent 应从搜索结果中获取 URL |
| `UNSUPPORTED_EXPORT_FORMAT` | 不支持的导出格式 | 支持 bibtex/ris/markdown/json/excel/gbt7714/footnote/apa |
| `MISSING_DEPENDENCY` | 缺少依赖包 | 提示用户 `pip install` |
| `IO_ERROR` | 文件保存失败（磁盘满、只读等） | 检查磁盘空间和写入权限 |
| `PATCH_PARSE_FAILED` | 补丁 JSON 解析失败 | 检查 JSON 格式是否正确 |
| `BROWSER_CRASH` | 浏览器进程崩溃（如 0x80000003） | 脚本已内置 `--disable-gpu` + `--disable-features=RendererCodeIntegrity` + 独立 `--user-data-dir` 防护。若仍崩溃：提权运行（`required_permissions: ["all"]`） |
| `DOWNLOAD_SOURCE_MISMATCH` | --download 仅支持 --source cnki | 提示用户搜索+下载一步到位仅限知网源 |
| `NO_SESSION` | 无搜索会话记录 | 提示先执行 search 或 batch-search |
| `INVALID_INDICES` | `read-detail --indices` 序号无效 | 根据当前会话论文数量重新选择 1 开始的序号或范围 |
| `NO_PAPER_ID` | 未提供论文标识 | Agent 应从搜索结果获取 DOI 或 URL |
| `RESOLVE_FAILED` | 无法识别论文标识或 API 查询失败 | 检查 DOI/URL 格式；可能 S2 API 暂时不可用 |
| `NO_DOWNLOAD_URLS` | search --download 但结果中无可用下载链接（warning） | 展示搜索结果，提示用户手动选取 URL 再 download |
| `WORKFLOW_NOT_FOUND` | 未找到预定义工作流 | 先运行 `workflows --list` 查看可用 ID |
| `INVALID_VARIABLES` | `workflows --variables` 不是合法 JSON | 修正 JSON 字符串，确保键和值使用双引号 |
| `MISSING_VARIABLES` | 工作流缺少必需变量 | 按响应中的 `required_variables` 补齐 |
| `REQUIREMENTS_NOT_MET` | 工作流前置条件不满足 | 读取 `missing` 和 `suggestions`，先完成环境检查或换用不依赖该能力的流程 |
| `WORKFLOW_STEP_FAILED` | 工作流某一步失败 | 查看 `failed_step`、`failed_command` 和前序 `results`，修复该步骤后重跑 |
| `NO_ACTION` | `workflows` 未指定操作 | 使用 `workflows --list` 或 `workflows --execute <workflow_id>` |
