# Scholar Kit

学术文献检索与科研辅助 AI Skill。支持知网（CNKI）、OpenAlex、Semantic Scholar、arXiv 等多数据源。

## Installation

### 一句话安装（所有平台通用）

在 Agent 聊天中发送：

```
Fetch and follow instructions from https://raw.githubusercontent.com/lottshin/scholar-kit/main/setup.md
```

Agent 会自动识别平台、clone 到正确位置、安装依赖并验证环境，全程无需手动操作。

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
| 网络 | 知网需校园网或 VPN |
| Selenium | 4.10+（自动管理 WebDriver，无需手动下载） |

## Features

- **文献搜索** — 多数据源检索，支持核心期刊筛选（北大核心、CSSCI、CSCD 等）
- **论文下载** — 知网 PDF 批量并行下载（搜索+下载一步完成），英文 OA 论文通过 DOI 获取
- **全文阅读** — HTML 全文抓取与缓存，支持 .docx/.txt/.md 解析
- **引用生成** — GB/T 7714、BibTeX、RIS、APA、脚注格式
- **Word 文档** — Markdown 转学术格式 .docx（含脚注），.docx 补丁（插引用/脚注/参考文献）
- **文献综述** — 基于检索结果和全文，辅助撰写综述与引用建议
- **学术表达优化** — 逐段提升论文原创表达质量

## Usage

安装完成后，直接用自然语言指示 Agent：

```
"帮我搜索关于乡村振兴的核心期刊论文"
"搜20篇新闻传播的CSSCI论文并下载"
"读取我的论文，帮我写一段文献综述"
"把这些引用插入我的 Word 文档"
"帮我优化这篇论文的学术表达"
```

Agent 会自动识别意图并调用 Scholar Kit。无需记忆任何命令。

## Configuration (Optional)

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

## File Structure

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
│   └── environment.md
└── scripts/              ← Python 脚本
    ├── literature.py     ← 统一 CLI 入口
    ├── requirements.txt
    ├── config.py
    ├── formatter.py
    ├── search.py
    └── cnki/             ← 知网模块
```

## Disclaimer

- 本工具仅供学术研究用途
- 使用知网功能需遵守 CNKI 服务条款，需具备合法访问权限（校园网/机构 VPN）
- 下载的论文版权归原作者和出版方所有
- 本项目不提供任何绕过付费墙的功能

## License

[MIT](LICENSE)
