# Scholar Kit

学术文献检索与科研辅助 AI Skill。支持知网（CNKI）、OpenAlex、Semantic Scholar、arXiv、NSSD、DBLP、BASE 七个检索源，通过 Crossref 补全元数据、Unpaywall 解析 OA 下载链接。

## Features

### 文献检索

支持知网、OpenAlex、Semantic Scholar、arXiv、NSSD、DBLP、BASE 七个数据源。知网侧支持核心期刊筛选（北大核心、CSSCI、CSCD 等）、文献类型筛选（硕士/博士/期刊/会议/报纸）、搜索字段指定（主题/篇名/关键词/摘要/全文/作者/来源）、按作者/期刊/年份的高级搜索、多关键词批量搜索。API 源支持作者、期刊、学科、分页过滤，`--enable-fallback` 可在单一 API 源无结果时自动切换备用源，`--async-search --source all` 可并发检索多源。`--enrich N` 参数可在搜索后自动补全前 N 篇论文的卷期页码。搜索结果缓存复用，避免重复启动浏览器或重复请求 API。API 数据源基于标准库 urllib 即可运行，不依赖知网环境。

### 论文下载

知网 PDF 批量下载，支持搜索+下载一步完成（`search --download`），自动分批并设置冷却间隔避免触发风控。断点续传机制确保中途失败后可从上次位置继续。英文 OA 论文通过 DOI 获取。

### 全文阅读

知网论文全文抓取与本地缓存。期刊论文使用 HTML 阅读提取，硕博论文通过 FlowPDF 三级加速提取（PDF.js API 直取 → 批量滚动 → 逐页补漏）。支持 .docx / .txt / .md 文件解析，自动处理中文编码（UTF-8 / GBK / GB18030）。`pdf-meta` 命令可从 PDF 文件中提取元数据（标题、作者、DOI），并通过 Crossref 自动补全完整书目信息。

### 引用生成与导出

支持 GB/T 7714、BibTeX、RIS、APA、脚注五种引用格式。文献列表可导出为 BibTeX、RIS、Markdown、JSON、Excel。支持导入知网导出的 NoteExpress / Refworks / BibTeX 题录文件（含卷期页码完整解析）。搜索结果自带引用预览（`citation_preview`），cite 命令对知网论文自动补全卷期页码。

### 引文网络分析

基于 Semantic Scholar API 的前向/后向引用追踪。输入 DOI、arXiv ID 或论文 URL，获取"谁引用了这篇"和"这篇引用了谁"的双向引用链。不依赖知网，任何环境均可使用。

### 研究趋势分析

对搜索结果进行聚合统计：年份分布、高频关键词 Top 30、高被引论文 Top 10、数据源分布。用于选题分析和研究热点判断。

### Word 文档处理

Markdown 转学术格式 .docx（自动生成脚注和参考文献节）。支持在现有 .docx 上打补丁——插入引用、脚注、参考文献，保留原文档格式。

### Agent 驱动的工作流

以下功能由 Agent 结合脚本完成，不需要单独的命令：

- **文献综述** — 读取用户论文，提取关键词搜索，筛选后基于全文/摘要撰写综述
- **引用建议** — 识别论文中需要引用的句子，匹配文献并区分"必须引用"和"建议引用"
- **文献对比矩阵** — 多篇论文按研究问题、方法、发现、局限性等维度结构化对比
- **阅读笔记** — 按模板提取每篇论文的核心信息，多篇时附综合评述
- **学术表达优化** — 逐段诊断论文表达质量，改善措辞、重构句式、增强原创论述

## Usage

安装完成后，直接用自然语言指示 Agent：

```
"帮我搜索关于乡村振兴的核心期刊论文"
"搜20篇新闻传播的CSSCI论文并下载"
"搜几篇关于数字经济的硕士论文，抓取全文"
"读取我的论文，帮我写一段文献综述"
"把这些引用插入我的 Word 文档"
"这篇论文被哪些后续研究引用了"
"分析一下这批搜索结果的研究趋势"
"帮我对比这5篇论文的研究方法和发现"
"帮我优化这篇论文的学术表达"
```

Agent 会自动识别意图并调用 Scholar Kit。

## Installation

### 一句话安装（所有平台通用）

在 Agent 聊天中发送：

```
Fetch and follow instructions from https://raw.githubusercontent.com/lottshin/scholar-kit/main/setup.md
```

Agent 会自动识别平台、clone 到正确位置、安装依赖并验证环境。

适用于 Cursor、Claude Code 及任何支持 fetch 指令的 AI Agent。

### 手动安装

<details>
<summary>展开手动安装步骤</summary>

```bash
# Cursor — Windows (PowerShell)
git clone https://github.com/lottshin/scholar-kit "$env:USERPROFILE\.cursor\skills\scholar-kit"

# Cursor — macOS / Linux
git clone https://github.com/lottshin/scholar-kit ~/.cursor/skills/scholar-kit

# Claude Code (Codex)
git clone https://github.com/lottshin/scholar-kit ~/.codex/skills/scholar-kit

# Gemini CLI
gemini skills install https://github.com/lottshin/scholar-kit.git
# 或手动 clone:
# git clone https://github.com/lottshin/scholar-kit ~/.gemini/skills/scholar-kit

# 安装依赖
pip install -r <安装路径>/scripts/requirements.txt
```

</details>

### 验证

开启新会话，对 Agent 说：

> "帮我搜索关于乡村振兴的核心期刊论文"

Agent 应自动识别 Scholar Kit 并执行。

## Requirements

| 要求 | 说明 |
|------|------|
| Python | 3.9+ |
| 浏览器 | Edge 或 Chrome（知网功能需要） |
| 网络 | 知网需校园网、机构 VPN，或学校支持的 CARSI/校外统一认证 |
| Selenium | 4.10+（自动管理 WebDriver，无需手动下载） |
| httpx | 可选（未安装时 HTTP 请求走标准库 urllib 兜底） |

## Configuration

<details>
<summary>可选配置</summary>

在项目目录下创建 `.scholar-kit/config.json`：

```json
{
  "request_interval": 3,
  "cache_ttl_days": 30,
  "save_dir": "./papers",
  "browser": "auto",
  "batch_window_size": 10
}
```

也可通过环境变量覆盖，详见 [SKILL.md](SKILL.md#配置)。

</details>

## Platform Notes

- **知网功能**需要本地桌面浏览器（Edge/Chrome）+ 合法机构访问权限（校园网、机构 VPN，或学校支持的 CARSI/校外统一认证），沙箱环境中需具备这些条件才可用
- **校外访问知网**可运行 `auth-cnki` 预热会话：默认打开 CNKI FSSO，也可用 `--auth-url` 传学校图书馆、VPN 或 CARSI 入口；`--institution` 可选，不传时由用户在浏览器中手动选择机构。登录成功后会复用 `.scholar-kit/browser-profile` 和 cookies，通常同一项目/会话无需反复登录。
- **代理/梯子软件**（Clash、Mihomo、Surge、Quantumult X、系统 PAC 等）需要让 CNKI、CARSI 和学校认证域名直连。脚本会给浏览器注入 `proxy-bypass-list`，但 TUN/全局接管模式仍需在代理软件中配置 DIRECT 规则；可用 `--direct-domain` 或 `SCHOLAR_CNKI_DIRECT_DOMAINS` 追加学校认证域名。
- **其他功能**（API 搜索、引用生成、Word 文档处理、引文网络、趋势分析）在所有平台均可用
- `check` 命令返回 `capabilities` 字段，Agent 据此自动选择可用的数据源

## File Structure

<details>
<summary>展开目录结构</summary>

```
scholar-kit/
├── SKILL.md              ← Agent 指令（核心）
├── setup.md              ← 一句话自动安装脚本
├── README.md
├── LICENSE
├── .gitignore
├── references/           ← Agent 按需读取的参考文档
│   ├── core-journals.md
│   ├── error-codes.md
│   ├── environment.md
│   ├── api-search-best-practices.md
│   └── workflows.md
└── scripts/              ← Python 脚本
    ├── literature.py     ← 统一 CLI 入口
    ├── requirements.txt
    ├── config.py
    ├── formatter.py
    ├── search.py
    ├── search_async.py
    ├── workflows.py
    └── cnki/             ← 知网模块
```

</details>

## Update

在 skill 安装目录执行：

```bash
git pull
pip install -r scripts/requirements.txt
```

`check` 命令会自动检测是否有新版本可用。

## Disclaimer

- 本工具仅供学术研究用途
- 使用知网功能需遵守 CNKI 服务条款，需具备合法访问权限（校园网/机构 VPN）
- 下载的论文版权归原作者和出版方所有
- 本项目不提供任何绕过付费墙的功能

## License

[MIT](LICENSE)
