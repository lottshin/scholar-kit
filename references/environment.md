# Windows / 中文环境约束

违反以下规则会导致乱码、命令失败或数据丢失。

## 文件读取

1. **禁止 Read/cat 直接读取 .docx**——docx 是 ZIP 压缩的 XML，直接读取必定乱码。必须用 `read-paper`：
   ```bash
   python scripts/literature.py read-paper "论文.docx" --output paper_text.txt
   ```
2. `.txt` 文件脚本自动尝试 UTF-8 → UTF-8-BOM → GBK → GB2312 → GB18030。

## 终端编码

3. **执行 CLI 前**先 `chcp 65001`，确保 UTF-8。
4. **中文关键词**优先用 `--query-file`：
   ```bash
   echo 文化折扣 > keywords.txt
   echo 跨文化传播 >> keywords.txt
   python scripts/literature.py batch-search --query-file keywords.txt --core 北大核心,CSSCI
   ```

## 文件写入

5. 所有脚本输出 `encoding="utf-8"`。Agent 写中文文件时也必须指定 UTF-8。

## 超时设置

6. `batch-search`：`block_until_ms` = 关键词数 × 60000，最少 120000
7. `batch-download`：`block_until_ms` = 篇数 × 45000（含冷却），最少 120000
8. `read-detail --fulltext`：`block_until_ms` = top_n × 40000，最少 120000
9. 超时转后台时，**必须轮询终端文件直到出现 exit_code**

## read-detail 降级

10. 全文获取失败自动降级为摘要。Agent 必须标注"引用依据为摘要"
11. 多数论文全文失败时，建议用户在校园网下重试

## 知网访问

- 需要校园网、机构 VPN，或学校支持的 CARSI/校外统一认证
- 校外访问可先运行 `python scripts/literature.py auth-cnki` 预热会话；默认入口是 `https://fsso.cnki.net/`，也可用 `--auth-url` 传学校图书馆、VPN 或 CARSI 入口
- `--institution` 是可选项。传入学校/机构名时脚本尝试自动选择；不传时用户在浏览器中手动选择，更适合不同学校和机构复用
- 登录、扫码、短信、滑块等必须由用户手动完成。Agent 在运行时应明确告知用户：浏览器会打开、请不要关闭窗口、脚本会等待并保存 cookies/profile
- 完成一次认证后，后续 CNKI 命令复用 `.scholar-kit/browser-profile` 和 `.scholar-kit/cookies.json`；同一项目/会话内通常无需反复登录，除非机构会话过期或用户使用 `auth-cnki --force`
- 使用 Clash/Mihomo/Surge/Quantumult X/PAC/系统代理等代理时，CNKI、CARSI 和学校认证域名必须直连。脚本会给 Edge/Chrome 注入 `proxy-bypass-list`，但 TUN/全局接管模式需要用户在代理软件中配置 DIRECT 规则
- 验证码会自动弹出浏览器，用户手动完成后继续

### 校外统一认证命令示例

```bash
# 通用 CNKI FSSO：用户在浏览器中手动选择学校
python scripts/literature.py auth-cnki --wait-seconds 240 --keep-browser

# 已知学校/机构名称时尝试自动选择
python scripts/literature.py auth-cnki --institution "示例大学" --wait-seconds 240

# 学校图书馆或 VPN 提供了自己的入口
python scripts/literature.py auth-cnki --auth-url "https://library.example.edu/cnki" --direct-domain idp.example.edu
```

## 浏览器

- 自动检测 Edge / Chrome，优先 Edge
- Selenium 4.10+ 自动管理 WebDriver
- **沙盒环境自动脱困**：当沙盒内启动浏览器崩溃时（`0x80000003` 等），脚本自动在沙盒外启动浏览器（通过 `CREATE_BREAKAWAY_FROM_JOB` 或 `ShellExecuteW`），再用远程调试端口连接，用户无需任何手动操作。可通过 `SCHOLAR_DEBUG_PORT` 环境变量自定义端口（默认 9222）

## 缓存

所有缓存在项目目录下 `.scholar-kit/`（自动创建），建议加入 `.gitignore`。

```
.scholar-kit/
├── session.json        ← 搜索会话
├── openalex_*.json     ← API 缓存（默认 30 天过期，可通过 cache_ttl_days 配置）
└── fulltext/           ← HTML 全文缓存
```

## 平台兼容性

### 功能 × 环境对照表

| 功能 | 桌面环境 (Cursor / 本地终端) | 沙箱环境 (Codex 等) | 纯云端 Agent |
|------|---------------------------|-------------------|------------|
| OpenAlex / Semantic Scholar / arXiv 搜索 | 可用 | 可用 | 可用 |
| 引用生成 / 导出 / cite / export | 可用 | 可用 | 可用 |
| read-paper / write-docx / patch-docx | 可用 | 可用 | 可用 |
| 知网搜索 (search --source cnki) | 可用 | 需校园网/VPN/CARSI + 浏览器 | 不可用 |
| 知网批量搜索 (batch-search) | 可用 | 需校园网/VPN/CARSI + 浏览器 | 不可用 |
| 知网下载 (batch-download) | 可用 | 需校园网/VPN/CARSI + 浏览器 | 不可用 |
| 知网全文 (read-detail / detail) | 可用 | 需校园网/VPN/CARSI + 浏览器 | 不可用 |

Agent 判断依据：优先读取 `check` 命令返回的 `capabilities.needs_escalation`，其次读取 `capabilities.cnki_feasible`。

### 沙箱环境与知网

知网功能需要桌面浏览器（Edge/Chrome）和合法机构访问权限（校园网、机构 VPN，或 CARSI/校外统一认证）。**沙箱环境通常不具备这些条件**，`check` 会报告 `cnki_feasible: false`。

知网需要有头浏览器（headless 会被反爬检测拦截），因此**区分两类沙箱**：

#### 本地沙箱（Cursor / Claude Code / Codex CLI 等）

这些平台运行在用户本地机器，浏览器已安装，但沙箱可能通过网络代理拦截出站请求。

`check --fix` 会自动检测沙箱环境（Codex: `.codex/` 目录或 `CODEX_*` 环境变量；Claude Code: `.claude/` 目录）并写入对应的网络配置，然后重试知网连通性。Agent 只需运行 `check --fix` 并读取最终的 `capabilities`，无需手动处理沙箱配置。

若返回 `needs_escalation: true`，Agent 必须按 `agent_action` 和 `retry_command` 请求平台提权重试，并携带 `recommended_env`。只有完成提权重试后仍然 `cnki_feasible: false`，才按 `fix_note` 判断是否是校园网/VPN/CARSI 校外认证问题；非 CNKI 专属任务可切换 API 源继续工作。

#### 云端/容器沙箱

纯容器、无桌面环境没有浏览器，Agent 应直接切换到 API 数据源。Codex Desktop 等具备 computer use 能力的环境可以调用本地浏览器，不要轻易判定为"不可用"——先按上方流程尝试。

### 版本更新

skill 安装后不会自动更新。`check` 命令会对比本地版本与 GitHub 最新 Release（无 Release 时回退到最新 Tag）：

- `update.update_available: true` → 提示用户在 skill 目录执行 `git pull`
- 用户决定是否更新，Agent 不自动执行 `git pull`

非 git 安装（ZIP 下载等）的用户需重新下载最新版本。

## 故障排查

### 知网搜索失败

1. 确认校园网/VPN 已连接；校外访问时确认已运行 `auth-cnki` 并完成统一身份认证
2. 浏览器手动打开 https://kns.cnki.net 确认能访问
3. 检查代理软件是否为 TUN/全局接管模式（会绕过浏览器 `proxy-bypass-list`）
4. 检查代理日志，确认 `fsso.cnki.net`、`kns.cnki.net`、CARSI 域名和学校统一认证域名均命中 DIRECT
5. 尝试关闭代理软件后重试
6. 如使用 VPN，确认知网域名走 VPN 而非代理

### Semantic Scholar 限流

- 错误码 429 表示超过速率限制
- 等待 30 秒后重试
- 注册 API key 可提高配额: https://www.semanticscholar.org/product/api#api-key

### OpenAlex 响应慢

- 添加 `mailto` 参数: `?mailto=your@email.com`
- 进入 polite pool 后响应速度提升
