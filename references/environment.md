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
7. `read-detail --fulltext`：`block_until_ms` = top_n × 40000，最少 120000
8. 超时转后台时，**必须轮询终端文件直到出现 exit_code**

## read-detail 降级

9. 全文获取失败自动降级为摘要。Agent 必须标注"引用依据为摘要"
10. 多数论文全文失败时，建议用户在校园网下重试

## 知网访问

- 需要校园网或 VPN
- Clash 等代理时脚本自动绕过直连知网
- 验证码自动弹出浏览器，用户手动完成后继续

## 浏览器

- 自动检测 Edge / Chrome，优先 Edge
- Selenium 4.10+ 自动管理 WebDriver

## 缓存

所有缓存在项目目录下 `.scholar-kit/`（自动创建），建议加入 `.gitignore`。

```
.scholar-kit/
├── session.json        ← 搜索会话
├── openalex_*.json     ← API 缓存（24h 过期）
└── fulltext/           ← HTML 全文缓存
```

## 故障排查

### 知网搜索失败

1. 确认校园网/VPN 已连接
2. 浏览器手动打开 https://kns.cnki.net 确认能访问
3. 检查 Clash 是否为 TUN 模式（全局接管会绕过 proxy-bypass-list）
4. 尝试关闭 Clash 后重试
5. 如使用 VPN，确认知网域名走 VPN 而非代理

### Semantic Scholar 限流

- 错误码 429 表示超过速率限制
- 等待 30 秒后重试
- 注册 API key 可提高配额: https://www.semanticscholar.org/product/api#api-key

### OpenAlex 响应慢

- 添加 `mailto` 参数: `?mailto=your@email.com`
- 进入 polite pool 后响应速度提升
