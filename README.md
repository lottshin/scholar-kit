# Scholar Kit

学术文献检索与科研辅助 AI Skill。支持知网（CNKI）、OpenAlex、Semantic Scholar、arXiv、NSSD、DBLP、BASE 七个检索源，通过 Crossref 补全元数据、Unpaywall 解析 OA 下载链接。

## Features

### 文献检索

支持知网、OpenAlex、Semantic Scholar、arXiv、NSSD、DBLP、BASE 七个数据源：
- **知网双引擎高级/专业检索**：优先采用 KNS8S `majorSearch` 专业布尔检索式（`--expr "TKA=('数字经济' + '数据要素') AND ..."`），自动回退 `#gradetxt` 多条件表单；支持 `篇关摘`（`TKA`）、第一作者（`--first-author`）、作者单位（`--affiliation`）、期刊精确/模糊限定（`--journal`）、基金资助（`--fund`）、前置核心期刊勾选（北大核心、CSSCI、CSCD、AMI、WJCI、EI）与出版年度限定。
- **高吞吐与常驻浏览器热复用**：默认开启 `9222` 端口浏览器热复用（`SCHOLAR_REUSE_BROWSER=1`，后续命令 `<0.2s` 秒级附着，支持 `close-browser` 显式关闭）；支持单页 50 条扩容（`--per-page 50`，`--limit > 20` 时自动启用）、按下载量排序（`--sort downloads`）与批量题录摘要导出（`--bulk-abstract`）。
- **国际开放 API 并发与降级**：API 源支持作者、期刊、学科、分页过滤；`--enable-fallback` 在主源无结果时自动切换备用源，`--async-search --source all` 并发检索全部 API 源并完整复用智能 TTL 缓存；中英文双轨 `quality_score` 评分确保核心期刊与国际文献公平排序。

### 论文下载

知网 PDF/CAJ 批量下载，支持搜索+下载一步完成（`search --download`），默认采用防风控并发窗口（每批 4 篇 + 随机抖动冷却）与多标签页目录绑定。断点续传机制确保中途失败后可从上次位置继续。下载文件按格式写入 `pdf/`、`caj/` 子目录；用户明确要 PDF 时可用 `--fallback-format caj` 在 PDF 不可用时显式降级到 CAJ，不会静默混放。批量下载自动生成按 GB/T 7714、APA、MLA 或 Chicago 格式排版的 Markdown 下载清单。英文 OA 论文可通过 DOI 解析获取。

### 全文阅读、本地 PDF 库导入与元数据解析

- **稳定键缓存与本地 PDF 极速提取**：知网详情页直接提取隐藏唯一标识 `cnki_id`（`CJFQ:...`）与未折叠完整摘要，跨会话动态 URL（`v=`）变化仍 100% 命中缓存；若 `./papers/pdf` 已下载对应 PDF，直接本地提取带 `[第 N 页]` 标记的正文，免去网页全文阅读滑块；历史链接 `404` 过期时自动按精确题名重定位。
- **本地 PDF 文件夹一键入库**（`import <PDF目录或文件>`）：自动递归解析英文 DOI/Crossref 元数据及中文知网 PDF 首页摘要、关键词、年卷期，直接复用后续 `read-detail --fulltext`、`review` 与 `cite` 管线。
- **文稿段落锚点读取**（`read-paper --numbered`）：支持 `.docx` / `.txt` / `.md` 编码自动识别，输出 `[P12]`、`[T1R2]` 精确段落与表格行锚点。

### 引用生成、子集重排与导出

支持 GB/T 7714（含知网网络首发文献 `[J/OL]` 规范著录）、APA、MLA、Chicago、BibTeX、RIS、中文脚注等格式。`cite --indices 9,2,5` 与 `export --indices ...` 支持按正文首次引用顺序定向生成子集并自动重排为 `[1][2][3]`（或加 `--keep-numbers` 保留原序号），单会话批量补全卷期页码并持久化写回 `session.json`。支持导入 NoteExpress / Refworks / BibTeX 题录文件。

### 双向引文滚雪球、课题库整理与 Word 安全回填

- **中英文双向引文网络与滚雪球扩检**（`citations`）：中文论文支持直接传入**知网详情 URL 或中文题名**抓取 `#refpartdiv` 原生**引证文献（`citing`）**与**参考文献（`references`）**（无需个人账号登录）；英文/DOI/arXiv 论文基于 Semantic Scholar Graph API；叠加 `--save both --append --project <课题>` 可一键将双向引文并入课题文献库。
- **课题文献库整理与批注**（`library`）：支持 `--remove` 剔除噪音、`--keep` 精选留存、`--dedupe` 跨轮去重，以及 `--indices ... --tag --note` 阅读批注。
- **学科中立证据包与论断核查**（`review` / `validate` / `topics`）：基于检索语料真实关键词动态聚类，输出覆盖度缺口提示、待核对原文标记与正文 `[序号]` 证据支撑度评分。
- **Word 安全打补丁与预检**（`write-docx` / `patch-docx`）：支持按 `"paragraph": "P12"` / `"T1R2"` 精确替换正文或表格单元格、插入原生 OOXML 脚注与追加悬挂缩进参考文献；提供 `patch-docx --dry-run` 零写入预检，100% 确认命中后再落盘。
- **知网改版 5 秒契约自检**（`check --cnki-dom`）：集中选择器注册表（`scripts/cnki/selectors.py`）配合只读 DOM 契约巡检与脱敏诊断快照，秒级定位前端改版。
- **学术表达优化**：按三段式（审计 → 改写 → 报告）逐段诊断论文原创性薄弱段、中文 AI 腔与证据缺口，判据见 [references/style-audit.md](references/style-audit.md)，不改变任何数字、公式与引文。

## Usage

安装完成后，直接用自然语言指示 Agent：

```text
"帮我搜索关于乡村振兴的核心期刊论文"
"搜20篇新闻传播的CSSCI论文并下载"
"搜几篇关于数字经济的硕士论文，抓取全文"
"读取我的论文，帮我写一段文献综述并校验引用支撑"
"把这些引用插入我的 Word 文档"
"这篇论文被哪些后续研究引用了"
"分析一下这批搜索结果的研究趋势"
"帮我对比这5篇论文的研究方法和发现"
"帮我优化这篇论文的学术表达"
```

## Installation

### 一句话安装（所有平台通用）

在 Agent 聊天中发送：

```text
Fetch and follow instructions from https://raw.githubusercontent.com/lottshin/scholar-kit/main/setup.md
```

### 手动安装

<details>
<summary>展开各平台手动安装步骤</summary>

```bash
# Claude Code
git clone https://github.com/lottshin/scholar-kit ~/.claude/skills/scholar-kit

# Codex
git clone https://github.com/lottshin/scholar-kit ~/.codex/skills/scholar-kit

# Antigravity（全局或工作区）
git clone https://github.com/lottshin/scholar-kit ~/.gemini/config/skills/scholar-kit
# 或工作区内: git clone https://github.com/lottshin/scholar-kit .agents/skills/scholar-kit

# Cursor — macOS / Linux
git clone https://github.com/lottshin/scholar-kit ~/.cursor/skills/scholar-kit

# Cursor — Windows (PowerShell)
git clone https://github.com/lottshin/scholar-kit "$env:USERPROFILE\.cursor\skills\scholar-kit"

# Gemini CLI
gemini skills install https://github.com/lottshin/scholar-kit.git

# 多 Agent 共用建议：保留一份主仓库（如 ~/.agents/skills/scholar-kit），其余目录用 ln -s 软链接指向它

# 安装依赖（若系统 Python 受 PEP 668 保护，可装入 ~/.scholar-kit/.venv 或运行 check --fix 自动引导）
python3 -m pip install -r <安装路径>/scripts/requirements.txt
python3 <安装路径>/scripts/literature.py check --fix
```

</details>

## State & Configuration

Scholar Kit 默认将状态拆分为两层，避免切换目录重复登录或污染业务 Git 仓库：
- **全局共享目录 `~/.scholar-kit/`**（可通过 `SCHOLAR_HOME` 覆盖）：保存知网 `browser-profile/`、`cookies.json`、API/全文 `cache/`、可选 `.venv/` 与全局 `config.json`，一次完成校内/CARSI 认证即可在所有项目目录复用。
- **工作区会话目录 `./.scholar-kit/`**（自动生成 `.gitignore`）：仅保存当前课题的 `session.json`、`projects/<课题>/` 与项目级 `config.json` 覆盖项。

## File Structure

<details>
<summary>展开目录结构</summary>

```text
scholar-kit/
├── SKILL.md              ← Agent 核心指令（渐进式披露）
├── setup.md              ← 一句话自动安装指引
├── README.md
├── LICENSE
├── .gitignore
├── references/           ← Agent 按需读取的参考文档
│   ├── core-journals.md
│   ├── error-codes.md
│   ├── environment.md
│   ├── api-search-best-practices.md
│   ├── style-audit.md    ← 学术表达审计规则
│   └── workflows.md
├── scripts/              ← Python 模块
│   ├── literature.py     ← 统一 CLI 路由入口
│   ├── config.py         ← 全局/工作区路径与配置管理
│   ├── evidence.py       ← 会话课题库、动态聚类、证据包与论断校验
│   ├── docx_tools.py     ← 论文读取、PDF 元数据提取与 Word 排版/补丁
│   ├── doctor.py         ← 环境自检、PEP 668 虚拟环境与安全缓存清理
│   ├── formatter.py      ← GB/T 7714 / APA / MLA / Chicago / BibTeX / RIS
│   ├── search.py         ← 开放学术 API 检索与智能缓存
│   ├── search_async.py   ← 多源并发调度器
│   ├── workflows.py      ← 预定义工作流模板
│   ├── requirements.txt
│   └── cnki/             ← 知网浏览器自动化与解析模块
└── tests/                ← pytest 单元测试套件
```

</details>

## Update

```bash
git pull
python3 -m pip install -r scripts/requirements.txt
```

`check` 命令会自动只读检测是否有新版本可用。

## Disclaimer

- 本工具仅供学术研究用途
- 使用知网功能需遵守 CNKI 服务条款，需具备合法访问权限（校园网/机构 VPN/CARSI）
- 下载的论文版权归原作者和出版方所有
- 本项目不提供任何绕过付费墙的功能

## Acknowledgments

学术表达优化（`references/style-audit.md`）的方法框架改编自 [AIScientists-Dev/academic-humanizer](https://github.com/AIScientists-Dev/academic-humanizer)（MIT）；其通用 AI 痕迹目录源自 [blader/humanizer](https://github.com/blader/humanizer)（MIT）。中文语料、学科语域与查重边界按本项目的写作场景重写。

本项目仅做表达与论证质量的改进，不针对任何查重或 AI 检测系统做规避；使用 AI 辅助写作时请遵守投稿或学位授予单位的相关规定。

## License

[MIT](LICENSE)
