# 环境约束、沙盒授权与故障排查

违反以下规则可能导致乱码、命令超时、JSON 解析失败或登录态丢失。

## 文件读取与编码

1. **禁止 Read/cat 直接读取 `.docx`**——`.docx` 是 ZIP 压缩的 OOXML，直接读取必定乱码。必须使用 `read-paper`：
   ```bash
   python3 scripts/literature.py read-paper "论文.docx" --output paper_text.txt
   ```
2. `.txt` / `.md` 文件脚本自动按 UTF-8 → UTF-8-BOM → GBK → GB2312 → GB18030 探测解码。
3. **Windows 终端编码**：执行 CLI 前确保 UTF-8（如 `chcp 65001` 或 `py -3 -X utf8`）。
4. **中文多关键词**优先通过 UTF-8 编码的 `--query-file` 传入，避免 Shell 引号或代码页转义问题：
   ```bash
   python3 scripts/literature.py batch-search --query-file keywords.txt --core 北大核心,CSSCI
   ```
5. **标准输出契约**：CLI 默认向 `stdout` 输出纯 JSON，诊断日志均写入 `stderr`。

## PEP 668 与 Python 虚拟环境

在 macOS Homebrew Python 或 Debian/Ubuntu 新版系统 Python 中，直接运行 `pip install` 可能触发 **PEP 668 (`externally-managed-environment`)** 限制：
- 优先使用 `uv` 或隔离虚拟环境 `~/.scholar-kit/.venv`：
  ```bash
  python3 -m venv ~/.scholar-kit/.venv
  ~/.scholar-kit/.venv/bin/python -m pip install -r scripts/requirements.txt
  ```
- 运行 `literature.py check --fix` 时，若检测到 PEP 668 拦截，脚本会自动在 `~/.scholar-kit/.venv` 创建隔离环境并把解释器路径写入返回 JSON 的 `capabilities.recommended_python`，后续命令直接复用该路径即可。

## 超时设置建议

- `batch-search`：关键词数 × 60s（最少 120s）
- `batch-download`：篇数 × 45s（含风控冷却，最少 120s）
- `read-detail --fulltext`：篇数 × 40s（最少 120s）
- 命令转入后台运行时，等待任务完成通知或轮询至进程退出后再解析输出。

## 状态目录分层（全局 vs 工作区）

Scholar Kit 将可复用的登录态/缓存与当前课题会话分离存储：

```text
~/.scholar-kit/                 ← 全局共享目录（可通过 SCHOLAR_HOME 覆盖）
├── browser-profile/            ← 知网/CARSI/机构认证 Chromium Profile（跨项目复用）
├── cookies.json                ← 知网持久化 Cookies
├── cache/                      ← API 源与知网搜索结果缓存
├── fulltext/                   ← 知网全文缓存
├── selenium-cache/             ← 沙盒回退 WebDriver 缓存
├── .venv/                      ← 可选隔离 Python 环境（PEP 668 自动引导）
└── config.json                 ← 用户全局默认配置

<workspace>/.scholar-kit/       ← 工作区会话目录（创建时自动生成 .gitignore 忽略全部内容）
├── .gitignore                  ← 内容为 `*`，防止误提交到业务 Git 仓库
├── session.json                ← 当前目录默认检索会话
├── projects/<课题名>/           ← 课题级文献库 session.json
└── config.json                 ← 当前项目局部配置覆盖
```

> `clean-cache --all` 仅清理过期或全部检索/全文缓存，**自动保护** `browser-profile/`、`projects/`、`.venv/`、`session.json`、`cookies.json` 与 `config.json`。

## 知网访问与校外认证 (`auth-cnki`)

- 知网搜索、详情、全文和下载需要合法机构权限（校园网、机构 VPN 或 CARSI/FSSO 校外统一身份认证）。
- 校外访问优先运行 `auth-cnki` 预热会话：
  ```bash
  # 通用 CNKI FSSO（用户在打开的浏览器中手动选择高校并登录）
  python3 scripts/literature.py auth-cnki --wait-seconds 240 --keep-browser

  # 指定高校名称尝试自动匹配入口
  python3 scripts/literature.py auth-cnki --institution "示例大学" --wait-seconds 240

  # 使用学校图书馆/WebVPN 专属入口并追加直连域名
  python3 scripts/literature.py auth-cnki --auth-url "https://library.example.edu/cnki" --direct-domain idp.example.edu
  ```
- 登录、扫码、短信或滑块验证均须由用户在弹出的浏览器窗口中手动完成。认证成功后保存在 `~/.scholar-kit/browser-profile` 与 `cookies.json`，切换论文目录无需重新登录。
- **浏览器指纹一致性**：有头模式下脚本直接复用本机 Edge/Chrome 原生 User-Agent，避免跨平台 UA 与 `navigator.platform` 冲突触发额外安全验证。

## 沙箱环境与知网

知网自动化依赖本地有头浏览器（Edge/Chrome）与外部网络。不同 Agent 宿主的沙盒授权方式如下：

| 平台 | 标准授权 / 提权方式 |
|------|---------------------|
| **Antigravity** | 先在默认沙盒内运行；若因网络或路径受限报错，使用工具的标准提权审批（用户批准后在宿主机执行需要网络/浏览器的命令） |
| **Codex** | 在 `~/.codex/config.toml` 或项目本地未跟踪的 `.codex/config.toml` 中启用 `network_access = true`（`check --fix` 可辅助写入），按平台弹窗授权联网 |
| **Claude Code** | 通过 `/permissions` 或 `/sandbox` 放行网络与命令；子 Agent 遇权限拒绝（如 Exit code 49）按宿主审批流使用非沙盒执行 |
| **Cursor** | Shell 工具调用请求完整网络/宿主执行权限（如 `required_permissions: ["all"]`） |
| **Gemini CLI** | 批准 Sandbox Expansion Request，或在具备桌面 GUI 与网络权限的本机会话中运行知网命令 |
| **纯云端/无 GUI 容器** | 无桌面浏览器，无法运行知网 Selenium 自动化；请明确告知用户并切换 OpenAlex / Semantic Scholar / arXiv / NSSD 等 API 源 |

### 沙盒常见卡点与内置自愈

1. **预检 socket 受限（如 WinError 10013）**：设置 `SCHOLAR_SKIP_NETWORK_CHECK=1` 跳过预检并在授权后直接运行。
2. **默认 `~/.cache/selenium` 不可写**：自动降级到 `SE_CACHE_PATH`（`~/.scholar-kit/selenium-cache` 或沙盒内 `.scholar-kit/selenium-cache`）。
3. **Windows 沙盒内浏览器进程崩溃（`0x80000003`）**：`driver.py` 自动尝试在沙盒外启动浏览器并通过远程调试端口（默认 `9222`，可通过 `SCHOLAR_DEBUG_PORT` 调整）连接。

## 故障排查

### 知网搜索或下载失败
1. 确认校园网/VPN 已连接，或校外已运行 `auth-cnki` 且返回 `access_confirmed: true`。
2. 手动打开 `https://kns.cnki.net` 确认机构名称显示在页头。
3. 若开启了 Clash / Mihomo / Surge 等代理软件的 TUN / 全局模式，需将 `*.cnki.net`、`*.carsi.edu.cn` 及学校认证域名设为 `DIRECT` 直连。

### API 源限流或响应慢
- **Semantic Scholar 429**：脚本内置指数退避重试；也可加 `--enable-fallback` 自动切换 OpenAlex。
- **OpenAlex 响应慢**：配置环境变量 `SCHOLAR_MAILTO=your@email.com` 进入官方 Polite Pool 提速。
