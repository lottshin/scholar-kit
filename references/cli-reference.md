# CLI 命令与参数速查手册

所有子命令默认输出结构化 JSON；支持 `--raw` 的命令可直接输出纯文本/Markdown/LaTeX 代码。执行前按 [SKILL.md](../SKILL.md#python-解释器解析与前置自检) 确定 `<python>`。

## 1. 检索、引文网络与个人文献库

| 命令 | 用途 | 完整常用参数 |
|------|------|--------------|
| `search [词]` | 单次 / 多条件 / 专业检索 | `--source` (`cnki`/`openalex`/`semantic`/`arxiv`/`pubmed`/`nssd`/`dblp`/`base`/`all`) `--condition "字段\|逻辑\|匹配\|值"`（可重复最多 10 条） `--professional` / `--expr`（知网专业检索式） `--match` (`精确`/`模糊`) `--cross-language` (`on`/`off`) `--field` (`主题`/`篇关摘`/`篇名`/`关键词`/`摘要`/`全文`/`小标题`/`参考文献`/`分类号`) `--author` `--first-author` `--affiliation` `--journal` `--fund` `--fuzzy` `--core`（知网 `北大核心,CSSCI,CSCD,AMI,WJCI,EI`；英文源 `SCI,SSCI,AHCI,ESCI,CCF,CCF-A,CCF-B,CCF-C`） `--exclude-warning` `--doc-type` (`journal`/`master`/`doctor`/`thesis`/`conference`/`newspaper`) `--year-from` `--year-to` `--sort` (`relevance`/`date`/`citations`/`downloads`/`quality`) `--pages` `--per-page 50` `--limit` `--bulk-abstract` `--cite-enrich N` `--enrich N` `--author-filter` `--journal-filter` `--field-of-study` `--page` `--enable-fallback` `--async-search` `--download` `--download-fallback-format caj` `--append` `--project` |
| `batch-search` | 多词知网检索 | `词1 词2` 或 `--query-file`（中文独立多主题） `--core` `--doc-type` `--field` `--match` `--cross-language` `--year-from` `--year-to` `--sort` `--pages` `--append` `--project` |
| `citations <id>` | 双向与共引图谱（滚雪球） | `<知网URL/中文题名/DOI/arXiv>` `--direction` (`citing`/`cited`/`both`/`coref`/`cocited`) `--limit 20` `--save` (`citing`/`cited`/`both`) `--append` `--project` |
| `zotero [词]` | 本机 Zotero 文献库直连 | `[query]` `--collection <集合名>` `--list-collections` `--save` `--append` `--project` |
| `import <path>` | 导入题录或本地 PDF 库 | 支持 NoteExpress / Refworks / BibTeX 及本地 PDF 文件/目录；`--append` `--project` |
| `projects` / `library` | 课题库列表与治理批注 | `library --project <名> [--keep 1,3,5-8] [--remove 4,9] [--dedupe] [--indices 1,3 --tag "核心实证" --note "批注"]` |

## 2. 精读切片、知网会话与批量下载

| 命令 | 用途 | 完整常用参数 |
|------|------|--------------|
| `read-detail` | 详情、全文与硕博章节切片 | `--indices 1,3,9` `--top-n` `--fulltext` `--toc`（先看章节目录页码字数） `--section "研究设计,实证"`（按章/节精准切片） `--max-chars` `--project` |
| `detail <url>` | 单篇知网详情页解析 | `<知网URL>` |
| `download` / `batch-download` | 单篇 / 批量全文下载 | `--from-session` `--top-n` `--dir` `--doi` `--file-format pdf/caj` `--fallback-format caj` `--citation-style` `--project` |
| `auth-cnki` | 校外知网 / CARSI / VPN 预热 | `--auth-url` `--institution` `--wait-seconds` `--direct-domain` |
| `close-browser` | 释放后台常驻浏览器 | 立即保存 Cookie 并优雅退出共享浏览器（默认空闲 20 分钟亦自动回收，`SCHOLAR_BROWSER_IDLE_MINUTES` 可配） |

## 3. 综述对比矩阵、选题、实证制表与质量核验

| 命令 | 用途 | 完整常用参数 |
|------|------|--------------|
| `review` | 结构化综述证据包与对比矩阵 | `--project` `--topic` `--limit` `--auto-detail` `--detail-top-n` `--cluster`（动态聚类 + 正负向分歧检测） `--gaps` `--verify-gaps`（OpenAlex 实时计数核验伪空白） `-o/--output` `--raw` |
| `write` | 生成综述证据骨架 | `--project` `--topic` `--mode outline/draft/section` `--section` `--format markdown/docx` `--with-citations` `--citation-style` `--validate` `-o/--output` `--raw` |
| `validate [file]` | 校验证据、极性冲突与统计归属 | `[filepath]` / `--file <md/docx/pdf>` `--project` `--topic` `--limit` `--allow-primary-data`（检测反写正负向结论、年份错位与无引文裸统计数字） |
| `topics` / `trends` | 选题建议与研究趋势统计 | `topics --project <名> --topic <方向> --limit 6 [--verify-gaps]`；`trends --project <名>` |
| `data-table <file>` | 实证统计 / 回归 / 实验对比制表 | `<csv/tsv/json>` `--type/--mode` (`auto`/`summary`/`corr`/`regression`/`comparison`) `--columns "c1,c2"` `--digits` `--lang zh/en` `--caption` `--format` (`markdown`/`latex`/`docx`) `-o/--output` `--raw` |

## 4. 引用排版、文稿修补、投稿选刊与审稿返修

| 命令 | 用途 | 完整常用参数 |
|------|------|--------------|
| `cite` | 标准引用与正文角标原子重排 | `--style` (`gbt7714`/`gbt7714-author-year`/`chinese-econ`/`apa`/`mla`/`chicago`/`footnote`) `--from-file <draft.md/tex>` `--rewrite-output <final.md/tex>` `--bibtex-output <refs.bib>` `--in-text` `--indices 9,2,5` `--keep-numbers` `--project` `--raw` |
| `export` | 多格式题录导出 | `--format` (`bibtex`[纯ASCII键]/`ris`/`markdown`/`json`/`excel`/`csv`/`gbt7714`/`apa`) `--indices` `-o/--output` `--project` `--raw` |
| `verify-refs` | 题名改名纠偏与页码真伪核验 | `--from-file <docx/md/txt>` `--indices 1,3` `--fix-session` `--project` |
| `read-paper` / `pdf-meta` | 读用户文稿 / PDF 元数据 | `read-paper <docx/txt/md/tex/pdf> [--numbered] [--toc] [--section "第3章,4.2"] [--max-chars] [--output] [--raw]`；`pdf-meta <file.pdf>` |
| `write-docx` / `patch-docx` | 交稿级 Word 排版 / 锚点修补 | `write-docx <md> [--output] [--no-superscript]`（原生三线表、正文角标上标、摘要楷体、居中表题与页码）；`patch-docx <docx> --patch patch.json [--dry-run] [--output]`（支持段落/表格锚点、原生脚注、`replace_references` 与 `superscript_citations`） |
| `format-audit` / `format-docx` | 学位论文格式规范审计与副本套用 | `format-audit <论文.docx> [--template 模板.docx] [--spec 规范.json] [--spec-text "描述"] [--format json/markdown] [-o report] [--raw]`；`format-docx <论文.docx> [--template/--spec/--spec-text] [-o *_formatted.docx] [--report-output]` |
| `match-journal` | 投稿目标期刊分层匹配与预警避雷 | `--project` `--from-file <docx/md>` `--topic <方向>` `--live` `--lang all/zh/en` `--limit 12` `--raw` |
| `rebuttal <file>` | 审稿意见拆解与点对点回复函 | `<comments.txt/md/docx>` `--manuscript <原稿.docx/md>` `--title` `--lang auto/zh/en` `--format markdown/docx` `--init-patch <patch.json>` `-o/--output` `--raw` |

## 5. 工作流模板、自检与配置项

| 命令 | 用途 | 常用参数 |
|------|------|----------|
| `workflows` | 列出或运行标准化流水线 | `--list` `--execute <id>` `--variables '<json>'` `--dry-run` |
| `check` | 环境自检与知网 DOM 巡检 | `--fix`（自动补齐依赖/PEP 668 虚拟环境） `--cnki-dom`（巡检知网高级检索 7 项 DOM 契约） |
| `clean-cache` | 清理检索与全文缓存 | `--all` `--dry-run` |

**常用环境变量（优先级：环境变量 > `./.scholar-kit/config.json` > `~/.scholar-kit/config.json` > 默认值）**：
`SCHOLAR_HOME`、`SCHOLAR_REQUEST_INTERVAL=3`、`SCHOLAR_CACHE_TTL_DAYS=30`、`SCHOLAR_MAILTO`、`SCHOLAR_SAVE_DIR=./papers`、`SCHOLAR_BROWSER=auto`、`SCHOLAR_BROWSER_IDLE_MINUTES=20`、`SCHOLAR_DRIVER_PATH`、`SCHOLAR_SKIP_NETWORK_CHECK`。
