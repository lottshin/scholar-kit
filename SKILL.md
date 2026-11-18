---
name: scholar-kit
description: >-
  Search, download, and manage academic papers from CNKI (知网), OpenAlex,
  Semantic Scholar, arXiv, NSSD, DBLP, and BASE; enriches metadata via Crossref and
  resolves OA links via Unpaywall. Generates citations
  (GB/T 7714, APA, MLA, Chicago, BibTeX, RIS), writes literature reviews, suggests
  inline references, analyzes citation networks, and generates research trend reports.
  Use when the user asks to 搜索/检索/查找 文献/论文, 下载论文/全文, 写文献综述,
  引用建议/插入文献, 选题分析/研究选题/研究问题, 格式化参考文献, 参考文献, 引文追踪/引用网络/谁引用了,
  研究趋势/热点分析, 文献对比/对比矩阵, 阅读笔记,
  学术表达优化/论文改写/提升原创性, 查重/降重/降低重复率/重复率,
  知网/CNKI, 批量导出/计量分析, or 导出BibTeX/RIS.
  DO NOT USE for general web search, non-academic content, or code documentation lookup.
metadata:
  version: 1.17.0
  compatibility:
    platform: any
    python: ">=3.9"
    os: windows, macos, linux
---

# Scholar Kit

学术文献检索与科研辅助工具。**脚本是"手"（确定性检索、抓取、排版与核验），Agent 是"脑"（理解意图、筛选综合与学术写作）**。

## Quick Start

```bash
# 0. 环境自检与自动修复（同一会话首次调用前运行一次；支持 PEP 668 隔离虚拟环境）
<python> <skill_path>/scripts/literature.py check --fix

# 1. 搜索（仅当用户明确要求核心期刊时加 --core）
<python> <skill_path>/scripts/literature.py search "乡村振兴" --core 北大核心,CSSCI

# 2. 获取详情/全文（优先用 --indices 精确指定序号）
<python> <skill_path>/scripts/literature.py read-detail --indices 1,3,9 --fulltext

# 3. 导出标准引用（严禁手拼 GB/T 7714；可按正文实际引用顺序传 --indices 子集）
<python> <skill_path>/scripts/literature.py cite --indices 1,3,9 --style gbt7714
```

### Python 解释器解析与前置自检

首次调用脚本前，按以下优先级确定 `<python>` 并在同一会话复用：
1. 若 `~/.scholar-kit/.venv` 已存在（或 `check` 返回 `capabilities.recommended_python`），优先使用该隔离环境 Python
2. 环境变量 `$PYTHON` → Windows `py -3` → `python3` → `python`（遇 Exit code 127 须按序重新解析）
3. 若依赖缺失，执行 `<python> -m pip install -r <skill_path>/scripts/requirements.txt`；若遇 PEP 668 `externally-managed-environment`，创建 `~/.scholar-kit/.venv`（或运行 `check --fix` 自动引导）并在该环境安装。

### 能力路由与沙盒授权（重要）

Agent 根据 `check` 返回的 `capabilities` 决策，**不得因 `status == "warning"` 直接退出**：
- **`cnki_feasible: true`**：全部知网与 API 命令可用。
- **`needs_escalation: true` 或沙盒拦截网络/浏览器**：
  - 知网自动化需联网与本机浏览器。请通过宿主平台的**标准授权/提权机制**（弹窗审批、unsandboxed retry 等，详见 [沙盒与权限配置](references/environment.md#沙箱环境与知网)）携带 `recommended_env` 重跑 `retry_command`。
  - 未尝试宿主授权重试前，不要把知网直接判定为不可用。
  - 若提权后仍不可达（或处于无桌面浏览器的纯云端容器）：硕博论文/知网全文等**知网专属任务**须告知用户连接校园网/VPN 或运行 `auth-cnki`；**通用文献检索**可切换 `--source openalex`（或 `--source all --async-search`）并告知用户。
- **仅使用 API 源**（OpenAlex / Semantic Scholar / arXiv / NSSD / DBLP / BASE）无需浏览器，仅需基础 HTTP 网络访问。
- **`docx_tools: false`** 时降级输出 Markdown；`update.update_available: true` 时顺带提示用户可 `git pull` 更新。

## 状态目录与配置分层

| 目录层级 | 默认路径 | 存放内容 |
|----------|----------|----------|
| **全局共享目录** | `~/.scholar-kit/`（支持 `SCHOLAR_HOME` 覆盖） | `browser-profile/`、`cookies.json`、`cache/`、`fulltext/`、`selenium-cache/`、`.venv/`、全局 `config.json`（一次登录知网，跨所有工作目录共享） |
| **工作区会话目录** | `./.scholar-kit/`（自动生成 `.gitignore` 防误提交） | 当前会话 `session.json`、课题库 `projects/<课题>/session.json`、工作区 `config.json` |

配置优先级：**环境变量 > `./.scholar-kit/config.json` > `~/.scholar-kit/config.json` > 默认值**（`SCHOLAR_REQUEST_INTERVAL=3`、`SCHOLAR_CACHE_TTL_DAYS=30`、`SCHOLAR_MAILTO`、`SCHOLAR_SAVE_DIR=./papers`、`SCHOLAR_BROWSER=auto`、`SCHOLAR_DRIVER_PATH`、`SCHOLAR_SKIP_NETWORK_CHECK`）。

## 决策指南

| 用户意图 | `cnki_feasible: true` | `cnki_feasible: false`（确认无法用知网后） |
|----------|----------------------|--------------------------------------------|
| 单词/高级搜索 | `search "词"`（支持 `--field 篇关摘`、`--author`、`--first-author`、`--affiliation`、`--journal`、`--fund`、`--fuzzy`） | `search "词" --source openalex`（多源用 `--source all --async-search`） |
| 复杂同义词/布尔搜索 | **优先**单次专业检索式 `search --expr "TKA=('词1' + '词2') AND ..."`（快且不易弹滑块）；确实独立的多主题再用 `batch-search` | 逐组 `search --source openalex --append` |
| 核心期刊筛选 | 加 `--core`（先读 [core-journals.md](references/core-journals.md)） | API 源不支持中国核心期刊体系筛选，须如实说明 |
| 写文献综述 | 检索入库 (`--project`) → `library --remove/--keep/--dedupe` 去噪 → `review --auto-detail --cluster --gaps` 取证据包 → Agent 按 [workflows.md](references/workflows.md#写文献综述) 撰写 → `validate --file` 核验 → `cite --indices ...` | 同左，检索改用 API 源或 `import <PDF目录>` |
| 选题分析 | `topics --project <课题> --topic <方向>` | 同左 |
| 论文下载 | 先确认格式后 `search --download` 或 `batch-download` | 仅 `download --doi <DOI>`（开放获取 OA） |
| 学术表达优化 / Word 改写 | 先读 [style-audit.md](references/style-audit.md)：`read-paper --numbered` 读入段落锚点 → 审计 → 改写 → `patch-docx --dry-run` 预检 → 正式写回与变更报告 | 同左（不依赖知网） |
| 引文追踪（滚雪球） / 趋势 | `citations <知网URL/中文题名/DOI> [--save both --append --project <课题>]` / `trends` | `citations <DOI/arXiv> [--save both --append]` / `trends` |
| 本地 PDF 文献库导入 | `import <PDF文件夹或文件> --project <课题> [--append]` → 直接复用 `read-detail --fulltext`、`review`、`cite` | 同左 |
| 对比矩阵 / 笔记 | 读 [workflows.md](references/workflows.md#文献对比矩阵) | 同左 |

> 搜索结果为 `0` 时：主动尝试同义词、中英文切换、放宽年份或 `--source all --async-search` / `--enable-fallback`，不要第一次无结果就直接放弃。

## CLI 命令速查（默认输出 JSON；`--raw` 输出纯文本）

| 命令 | 用途 | 常用参数 |
|------|------|----------|
| `search [词]` | 单次/专业检索 | `--source` (cnki/openalex/semantic/arxiv/nssd/dblp/base/all) `--expr`（知网专业检索布尔式） `--field` (主题/篇关摘/篇名/关键词/摘要/全文/...) `--author` `--first-author` `--affiliation` `--journal` `--fund` `--fuzzy` `--core` `--doc-type` `--year-from` `--year-to` `--sort` (relevance/date/citations/downloads/quality) `--pages` `--per-page 50` `--limit` `--bulk-abstract` `--cite-enrich N` `--enrich N` `--author-filter` `--journal-filter` `--field-of-study` `--page` `--enable-fallback` `--async-search` `--download` `--download-fallback-format caj` `--append` `--project` |
| `batch-search` | 多词知网检索 | `词1 词2` 或 `--query-file`（中文多词推荐） `--core` `--doc-type` `--field` `--year-from` `--year-to` `--sort` `--pages` `--append` `--project` |
| `read-detail` | 知网摘要/全文 | `--indices 1,3,9`（优先，已下载本地 PDF 时秒级提取免滑块） `--top-n` `--fulltext` `--project` |
| `detail "url"` | 单篇知网详情 | |
| `auth-cnki` / `close-browser` | 认证预热 / 关闭常驻浏览器 | `auth-cnki --auth-url --institution --wait-seconds --direct-domain`；`close-browser` 释放后台热复用浏览器 |
| `download` / `batch-download` | 单篇 / 批量下载 | `--from-session` `--top-n` `--dir` `--doi` `--file-format pdf/caj` `--fallback-format caj` `--citation-style` `--project` |
| `cite` / `export` | 引用 / 导出 | `cite --style` (gbt7714/footnote/apa/mla/chicago) `--indices 9,2,5`（按正文引用顺序生成 `[1][2][3]`，仅补全所选文献页码） `--keep-numbers`；`export --format` (bibtex/ris/markdown/json/excel/...) `--indices` `--output` `--raw` `--project` |
| `import "path"` | 导入题录或 PDF 库 | 支持 NoteExpress / Refworks / BibTeX 及**本地 PDF 文件或文件夹**，`--append` `--project` |
| `projects` / `library` | 课题库整理与批注 | `library --project <名> [--keep 1,3,5-8] [--remove 4,9] [--dedupe] [--indices 1,3 --tag "核心实证" --note "批注"]` |
| `review` | 生成综述证据包 | `--project` `--topic` `--limit` `--auto-detail` `--detail-top-n` `--cluster` `--gaps` `--output` `--raw` |
| `write` | 生成综述证据骨架 | `--project` `--topic` `--mode outline/draft/section` `--section` `--format markdown/docx` `--with-citations` `--validate` |
| `validate` | 校验正文证据支撑 | `--project` `--topic` `--file draft.md` |
| `topics` / `trends` / `citations` | 选题 / 趋势 / 滚雪球引文 | `citations <知网URL/中文题名/DOI> --direction citing/cited/both --limit 20 [--save both --append --project <名>]` |
| `read-paper` / `pdf-meta` | 读用户文稿 / PDF 元数据 | `read-paper <docx/txt/md> [--numbered] [--output] [--raw]`；`pdf-meta <file.pdf>` |
| `write-docx` / `patch-docx` | 生成 / 修补 Word | `write-docx <md> [--output]`；`patch-docx <docx> --patch patch.json [--dry-run] [--output]` |
| `workflows` / `check` / `clean-cache` | 模板 / 自检 / 清缓存 | `check [--fix] [--cnki-dom]`（`--cnki-dom` 巡检知网页面改版契约）；`clean-cache [--all] [--dry-run]` |

## 交互规范

1. **结果展示**：搜索默认展示前 **10 条**表格（序号、标题、作者、来源/期刊、年份、被引）；`read-detail --fulltext` 过长时先给每篇 200 字核心摘要，按需展开；`cite` / `export` 完整展示不截断。
2. **搜索与下载联动**：
   - 用户未指明偏好时先追问：**下载文件格式**（`pdf` / `caj`，默认推荐 `pdf`）与**下载清单引用格式**（`gbt7714` / `apa` / `mla` / `chicago`）。
   - 明确"搜并下"用 `search ... --download` 一步完成；需先看列表挑选再用 `search` → `batch-download --from-session`。
   - 仅当用户同意兜底时加 `--fallback-format caj`（分目录存 `pdf/` 与 `caj/`，严禁把 CAJ 静默当作 PDF），完成后必须展示 `download_report.path` 及未下载原因。
3. **校外知网认证**：校外/CARSI/VPN 场景优先运行 `auth-cnki` 预热（提前告知用户会打开浏览器手动登录/过验证、请勿关窗），成功后自动保存在 `~/.scholar-kit/browser-profile` 跨项目复用。
4. **综述写作闭环**：优先用 `review --cluster --gaps`（缺摘要叠加 `--auto-detail`）获取结构化证据包 → 由 Agent 结合学科语境撰写正文（`write` 命令仅提供证据骨架草稿） → 用 `validate --file` 校验论断与 `[序号]` 支撑度。
5. **长文档与歧义**：请求模糊（如"帮我找论文/改论文"）先简短追问方向与操作类型；超过 **15,000 字**文稿按 1–2 章分段汇报改写，最后一次 `patch-docx` 提交；**严禁直接 cat/Read `.docx`**，必须用 `read-paper`。

## 硬性规则

1. **禁止编造文献与元数据**：API/检索失败如实报错；题名、作者、年份、卷期页码、DOI 只来自工具返回，缺失标"未获取"或用 `--cite-enrich` 补全。
2. **"研究空白"须附检索证据**：必须标明数据库、关键词、时间范围与命中篇数，不得把单次漏检断言为学界空白。
3. **核心期刊标明体系与来源**：`--core` 仅在用户明确要求核心期刊时添加；展示时标明体系名称，未匹配答"未匹配"。
4. **概括观点须可追溯**：严格基于摘要或 `read-detail` 全文，仅题录条目必须标注"待核对原文"；预印本（arXiv 等）须标注版本号与"非同行评议终稿"。
5. **引用格式统一由脚本生成**：Agent 不手拼 GB/T 7714，统一调用 `cite` 命令。
6. **合规执行学术表达优化**：用户提出"降重/降低重复率/去 AI 腔/润色"时，按"学术表达优化"正常执行（严格遵循 [style-audit.md](references/style-audit.md) 的审计→改写→报告三步法，不改数字、公式与引文，并说明不提供查重比数值检测）。

## 按需参考文档（不要一次性全部预加载）

- [工作流详解](references/workflows.md) — 综述、引用建议、Word 改写与补丁、对比矩阵、阅读笔记步骤
- [表达审计规则](references/style-audit.md) — 学术表达优化、去 AI 腔清单、保留清单与变更日志模板
- [API 源检索最佳实践](references/api-search-best-practices.md) — 高级过滤参数、`quality_score` 评分细则、分页与多源组合策略
- [核心期刊知识](references/core-journals.md) — 学科与 `--core`（北大核心/CSSCI/CSCD/AMI/WJCI/EI）映射规则
- [环境约束与沙盒故障排查](references/environment.md) — 各平台沙盒授权表、Windows 编码、超时计算与代理排查
- [错误码对照表](references/error-codes.md) — 脚本 `status: error/warning` 时的标准处置指引
