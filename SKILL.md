---
name: scholar-kit
description: >-
  Full-lifecycle academic research skill: search & download papers from CNKI (知网), OpenAlex,
  Semantic Scholar, arXiv, PubMed, NSSD, DBLP, BASE, and local Zotero/PDF libraries; offline-screen
  SCI/SSCI/A&HCI/ESCI, CCF-A/B/C, and CAS Early Warning journals; slice long theses; build structured
  comparison matrices & search-verified gaps; audit claim polarity & unbacked statistics; generate
  empirical/regression/benchmark tables (Markdown, LaTeX, Word three-line docx); format GB/T 7714,
  APA, pure-ASCII BibTeX & reconcile drifted titles; match submission venues and draft peer-review
  rebuttals. Use when asked to 搜索/下载论文, 写文献综述/对比矩阵, 选题/引文追踪, 格式化或核验参考文献,
  生成描述统计/相关矩阵/回归表/实验对比表, 投稿选刊/预警排查, 审稿返修/Rebuttal, or 学术润色/改写Word.
  DO NOT USE for general web search or code documentation lookup.
license: MIT
compatibility: Python >=3.9 on macOS, Windows, or Linux; Chrome/Edge required only for CNKI automation.
metadata:
  version: "1.25.1"
---

# Scholar Kit

面向学术研究全流程的确定性辅助工具：**脚本是"手"（检索、统计、核验、排版），Agent 是"脑"（理解意图、筛选综合与学术写作）**。

## Quick Start

```bash
# 0. 环境自检（同会话首次调用前跑一次；自动适配 PEP 668 虚拟环境）
<python> <skill_path>/scripts/literature.py check --fix

# 1. 检索入库（按需加 --core 北大核心,CSSCI 或 --core SCI,SSCI --exclude-warning）
<python> <skill_path>/scripts/literature.py search "新质生产力" --core 北大核心,CSSCI --project topic1

# 2. 硕博目录预览与精读切片（本地已下载 PDF 离线秒级读取）
<python> <skill_path>/scripts/literature.py read-detail --indices 1,3 --toc --project topic1
<python> <skill_path>/scripts/literature.py read-detail --indices 1 --section "研究设计,实证" --project topic1

# 3. 综述对比矩阵 + 空白检索核验 + 正文极性/统计校验
<python> <skill_path>/scripts/literature.py review --project topic1 --cluster --gaps --verify-gaps
<python> <skill_path>/scripts/literature.py validate draft.docx --project topic1

# 4. 实证制表、标准引用重排、投稿选刊与审稿返修
<python> <skill_path>/scripts/literature.py data-table panel.csv --type corr --format docx -o table.docx
<python> <skill_path>/scripts/literature.py cite --from-file draft.md --rewrite-output final.md --style gbt7714
<python> <skill_path>/scripts/literature.py match-journal --project topic1 --live
<python> <skill_path>/scripts/literature.py rebuttal reviewers.txt --manuscript draft.docx --init-patch patch.json -o reply.docx
```

### Python 解析与能力路由（必读）

1. **解释器优先级**：`~/.scholar-kit/.venv`（或 `check` 返回的 `capabilities.recommended_python`）→ `$PYTHON` → Windows `py -3` → `python3` → `python`。依赖缺失时运行 `check --fix` 或安装 `scripts/requirements.txt`。
2. **沙盒授权与降级**：不要因 `check` 返回 `warning` 直接退出：
   - `needs_escalation: true` 或沙盒拦截网络/浏览器时，先通过宿主平台**标准授权/提权机制**携带 `recommended_env` 重跑 `retry_command`（见 [environment.md](references/environment.md#沙箱环境与知网)），切勿跳过授权直接判知网不可用。
   - 提权后仍无桌面浏览器/校园网：知网专属任务提示连接校园网/VPN 或运行 `auth-cnki`；通用文献检索切换 `--source openalex`（或 `--source all --async-search`）并如实告知。
   - 仅用 API 源、Zotero/PDF 本地库、`data-table`、`rebuttal`、`validate` 均**无需浏览器**；`docx_tools: false` 时降级输出 Markdown。

## 状态目录分层

| 层级 | 默认路径 | 存放内容 |
|------|----------|----------|
| **全局共享** | `~/.scholar-kit/`（`SCHOLAR_HOME` 可覆盖） | `browser-profile/`、`cookies.json`、`cache/`、`fulltext/`、`.venv/`、全局 `config.json`（知网一次登录跨项目共享） |
| **工作区会话** | `./.scholar-kit/`（自动生成 `.gitignore`） | 当前 `session.json`、课题库 `projects/<课题>/session.json`、工作区 `config.json` |

## 决策指南（全流程路由）

| 用户意图 | 首选命令组合（完整参数见 [cli-reference.md](references/cli-reference.md)） |
|----------|---------------------------------------------------------------------------|
| 中文/多源文献检索 | `search "词"`（复杂同义词优先 `search --expr "TKA=('A'+'B') AND ..."` 降风控；无知网改 `--source openalex` 或 `--source all --async-search`；独立多词用 `batch-search`） |
| 核心期刊与预警把关 | 叠加 `--core`（中文 `北大核心,CSSCI,CSCD,AMI,EI`；英文 `SCI,SSCI,AHCI,ESCI,CCF-A/B`）与 `--exclude-warning`（规则见 [core-journals.md](references/core-journals.md)） |
| 本机 Zotero / 本地 PDF | `zotero [词] --save --project <课题>` 或 `import <PDF目录> --project <课题>` → 直接复用 `read-detail`、`review`、`cite` |
| 论文下载与引文滚雪球 | 先确认 `pdf`/`caj` 与引文格式后 `search --download` 或 `batch-download`（无知网用 `download --doi`）；追踪引文用 `citations <URL/题名/DOI> --save both --append` |
| 硕博精读 / 文献综述 / 长文 | `library --dedupe/--remove` 去噪 → `read-detail --toc` + `--section` 切片 → `review --auto-detail --cluster --gaps --verify-gaps` → 撰写（见 [workflows.md](references/workflows.md#撰写完整学术论文)）→ `validate` → `cite --from-file` → `write-docx` |
| 参考文献核验与格式导出 | 核对错名/改名/漏页码用 `verify-refs [--from-file <文稿>] --fix-session`；导出/重排统一用 `cite`（支持顺序编码、著者-出版年、经管顶刊、LaTeX `\cite` + 纯 ASCII `.bib`）或 `export` |
| 实证统计 / 回归 / 实验制表 | `data-table <csv/tsv/json> --type summary/corr/regression/comparison --format markdown/latex/docx`（纯本地精确计算，零手算） |
| 学术表达优化 / Word 修补 | 先读 [style-audit.md](references/style-audit.md)：`read-paper --numbered` → 审计 → 改写 → `patch-docx --dry-run` → 正式写回并附变更日志 |
| 投稿选刊与审稿返修 | 选刊：`match-journal [--project] [--from-file] [--topic --live]`（隔离在榜预警刊）；返修：`rebuttal <意见文件> --manuscript <原稿.docx> --init-patch patch.json -o reply.docx` → `patch-docx --dry-run` |

> 搜索返回 `0` 结果时：主动切换同义词、中英文、`%` 模糊检索或 `--source all --async-search` / `--enable-fallback`，不要首次空结果就放弃。

## 交互规范

1. **展示与长文切片**：搜索默认展示前 10 条表格；阅读硕博论文或长文先用 `read-detail --toc` 看目录再用 `--section` 切片；`cite` / `export` 完整输出不截断；超过 **15,000 字**文稿分段汇报改写再一次性 `patch-docx`；**严禁直接读取 `.docx` 二进制**，须用 `read-paper`。
2. **下载确认**：未指定格式时先确认 `pdf`/`caj` 与清单引文格式；仅用户同意兜底时加 `--fallback-format caj`（分目录存放，严禁静默把 CAJ 当 PDF），完成后汇报 `download_report.path` 与失败原因。
3. **知网认证与滑块协同**：校外/CARSI/VPN 优先用 `auth-cnki` 预热（提前提醒用户浏览器会打开并勿关窗）；未登录执行全文/下载会秒级返回 `CNKI_AUTH_REQUIRED`；当日志提示安全验证页已置顶弹窗等待手动滑块（最长 180 秒）时，**严禁自行点击/绕过验证码，也勿强杀任务或调用 `close-browser`**，简短提醒用户在置顶窗口完成滑动即可。

## 硬性规则（不可违背）

1. **禁止编造文献、意译中文题名与漏填页码**：检索失败如实报错；严禁把英文库标题自行翻成中文冒充知网题名，不确定真实题名须运行 `verify-refs --fix-session`；引用统一由 `cite` / `export` 生成（勿手拼 GB/T 7714 或复制缺页码预览）。
2. **"研究空白"须附检索证据**：标明数据库、关键词、时间区间与命中篇数（优先用 `review --verify-gaps` / `topics --verify-gaps` 核验），不得把单次漏检断言为学界空白。
3. **写综述与论文须把关选刊质量**：中文优先加 `--core 北大核心,CSSCI`（理工医 `CSCD`）辅以高校博硕论文，确无覆盖再补普刊并说明；英文核查 `venue_tier`、核心索引与预警标签，剔除中科院在榜预警刊与零被引水刊。
4. **概括观点可追溯**：严格基于摘要或 `read-detail` 全文，仅题录条目须标"待核对原文"；预印本标明版本号与"非同行评议终稿"。
5. **严禁捏造一手实证数据与统计数值**：用户未提供真实数据时，禁止编造 `N=...`、百分比、回归系数或 $p$ 值；有真实数据文件时统一用 `data-table` 确定性制表，正文校验可显式加 `--allow-primary-data`。
6. **合规执行学术表达优化**：用户提"降重/去 AI 腔/润色"时严格按 [style-audit.md](references/style-audit.md) 三步法执行，**绝不改动数字、公式与引文**，并说明不提供查重比数值检测。

## 按需参考文档（不要一次性全部预加载）

- [CLI 命令与参数速查](references/cli-reference.md) — 全部子命令完整参数、短旗标与环境变量说明
- [工作流详解](references/workflows.md) — 综述、万字长文、实证制表、投稿选刊、审稿返修、Word 补丁与对比矩阵步骤
- [表达审计规则](references/style-audit.md) — 学术表达优化、去 AI 腔清单、保留清单与变更日志模板
- [API 源检索最佳实践](references/api-search-best-practices.md) — 高级过滤、`quality_score` 评分细则、分页与多源组合
- [核心期刊知识](references/core-journals.md) — 学科与 `--core`（北大核心/CSSCI/CSCD/AMI/WJCI/EI）映射规则
- [环境与沙盒排查](references/environment.md) — 各平台沙盒授权表、Windows 编码、浏览器热复用与网络代理排查
- [错误码对照表](references/error-codes.md) — 脚本 `status: error/warning` 标准处置指引
