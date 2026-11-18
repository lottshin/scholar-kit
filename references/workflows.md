# 工作流详解

Agent 执行具体任务时按需读取本文档。决策入口见 [SKILL.md 决策指南](../SKILL.md#决策指南)。

## 文献检索

1. 提取关键词（中文 + 英文）
   - 中文关键词适用于 CNKI、NSSD
   - 英文关键词适用于 OpenAlex、Semantic Scholar、arXiv、DBLP、BASE
   - `cnki_feasible: false` 时若用户给中文主题，Agent 应翻译为英文再查 API 源
2. 用户要求核心期刊时 → 判断学科 → 决定 `--core`（读 [核心期刊知识](core-journals.md)）
3. `cnki_feasible: true` → 单词用 `search`，多词用 `batch-search`；`cnki_feasible: false` → 用 `search --source openalex` 等 API 源
4. 展示结果，注明筛选了哪些来源类别

### 知网（CNKI）高级检索与专业检索技巧

新版知网检索底层采用**双引擎机制**（优先 `majorSearch` 专业检索布尔式，自动回退 `#gradetxt` 多条件表单，并在提交前一次性预设文献库、来源类别与发表年份区间）：

- **高频复合字段 `--field 篇关摘`（`TKA`）**：同时匹配篇名 + 关键词 + 摘要，比默认 `主题` 边界更清晰、比单搜 `篇名` 检全率更高，综述检索首选。
- **结构化多维限定**（位置参数 `query` 在只按作者/机构/期刊搜时可省略）：
  - `--author "张三"`（`AU='张三'`） / `--first-author "张三"`（`FI='张三'`）
  - `--affiliation "清华大学"`（`AF % '清华大学'`，默认模糊包含以覆盖院系后缀）
  - `--journal "《中国社会科学》"`（带书名号精确匹配 `LY='中国社会科学'`；不带书名号默认 `LY %` 兼容学报分版）
  - `--fund "国家社会科学基金"`（`FU % '国家社会科学基金'`）
  - `--fuzzy`：对主题/篇关摘/篇名启用 `%` 模糊包含匹配
- **单次布尔专业检索 `--expr`（强烈推荐替代同义词多轮 `batch-search`）**：
  - 语法：字段代码（`SU` / `TKA` / `TI` / `KY` / `AB` / `AU` / `FI` / `AF` / `LY` / `FU`）+ `=`（精确）或 `%`（模糊）+ **英文单引号**包裹词项；同字段内并列用 `+`、交集用 `*`、排除用 `-`；跨字段用 `AND` / `OR` / `NOT`（注意 `AND` 后必须带字段代码，如 `AND TKA=(...)`）。
  - 注意：年份直接搭配 `--year-from` / `--year-to` 即可（知网 KNS8S 专业检索式不支持 `YE` 字段）。

```bash
# 1. 一条专业检索式同时覆盖多组同义词 + 核心期刊 + 年份（1 次请求完成，大幅降低滑块触发概率）
search --expr "TKA=('数字经济' + '数据要素') AND TKA=('乡村振兴' + '县域治理')" --core 北大核心,CSSCI --year-from 2021

# 2. 限定高校/机构 + 第一作者或期刊（无需传空字符串 query）
search --affiliation "北京大学" --journal "《管理世界》" --year-from 2020
search --first-author "张三" --doc-type journal

# 3. 篇关摘检索博硕士学位论文
search "大模型微调" --field 篇关摘 --doc-type doctor --year-from 2023
```

### API 源高级检索技巧

**排序策略**：
- 按被引数排序：`--sort citations` - 找经典高引论文
- 按时间排序：`--sort date` - 找最新研究进展
- 按质量排序：`--sort quality` - 综合评分筛选高质量论文

**精确过滤**：
- 期刊过滤：`--journal-filter "Nature"` - 限定特定期刊
- 作者过滤：`--author-filter "Hinton"` - 查找特定作者的论文
- 学科过滤：`--field-of-study "Computer Vision"` - 限定学科领域
- 组合过滤：可同时使用多个过滤条件，例如 `--year-from 2020 --journal-filter "Nature" --author-filter "LeCun"`

**字段搜索**：
- 标题搜索：`--field 篇名` - 只在标题中搜索，减少噪音
- 摘要搜索：`--field 摘要` - 只在摘要中搜索

**分页浏览**：
- 大量结果时使用 `--page 2` 浏览后续页面
- 每页结果单独缓存，重复访问速度快

**降级与并发**：
- 单一 API 源偶发无结果或请求失败时，加 `--enable-fallback` 自动尝试备用源
- 多源检索需要更快返回时，用 `--source all --async-search` 并发检索 OpenAlex、Semantic Scholar、arXiv、NSSD、DBLP、BASE

**示例**：
```bash
# 查找 2020 年后 Nature 上关于深度学习的高引论文
search "deep learning" --source openalex --year-from 2020 --journal-filter "Nature" --sort citations --limit 20

# 查找 Hinton 关于神经网络的最新论文
search "neural networks" --source openalex --author-filter "Hinton" --sort date --limit 10

# 按质量筛选计算机视觉领域的论文
search "object detection" --source openalex --field-of-study "Computer Vision" --sort quality --limit 15

# 使用 DBLP 搜索计算机科学领域的论文（适合查找会议论文和技术报告）
search "transformer architecture" --source dblp --year-from 2020 --limit 15

# 使用 BASE 搜索欧洲开放获取资源
search "climate change" --source base --year-from 2021 --limit 20

# 分页浏览大量结果
search "machine learning" --source openalex --limit 20 --page 1
search "machine learning" --source openalex --limit 20 --page 2

# 主源无结果时自动降级
search "graph neural networks" --source semantic --enable-fallback --limit 20

# 并发多源检索
search "AI in education" --source all --async-search --limit 30
```

更多 API 源过滤、排序和分页策略见 [API 源检索最佳实践](api-search-best-practices.md)。

**数据源选择建议**：
- **计算机科学**：优先使用 DBLP（会议论文和技术报告覆盖全面）+ OpenAlex
- **欧洲研究/开放获取**：优先使用 BASE（欧洲学术资源，350M+ 文档）
- **综合学科**：OpenAlex（覆盖最广）或 Semantic Scholar（引文网络强）
- **预印本/最新研究**：arXiv（物理、数学、计算机）
- **中文文献**：CNKI（核心期刊）或 NSSD（哲学社会科学）

**数据源特点**：
- **DBLP**：计算机科学专业数据库，会议论文质量高，但无摘要和被引数据
- **BASE**：欧洲开放获取资源，文档量大，但可能有 IP 访问限制
- **OpenAlex**：综合性最强，摘要和引用数据完整
- **Semantic Scholar**：AI 驱动，引文网络分析能力强
- **arXiv**：预印本为主，更新快但未经同行评议

### 预定义工作流

可用 `workflows --list` 查看模板，用 `workflows --execute <id> --variables <json>` 预览或执行。Windows PowerShell 调用原生命令时，JSON 内部双引号需要写成 `""`：

```powershell
python scripts\literature.py workflows --execute literature_review_classic --variables '{""topic"":""deep learning""}' --dry-run
```

## 写文献综述（建库 → 去噪扩检 → 证据包 → 撰写 → 校验 → 定向国标引用）

1. **检索建库（推荐指定 `--project <课题名>` 持久化积累）**
   - `cnki_feasible: true`：优先用 `--expr` 布尔专业检索一次性覆盖同义词与核心期刊（例如 `search --expr "TKA=('数字经济' + '数据要素') AND TKA=('新质生产力' + '高质量发展')" --core 北大核心,CSSCI --sort citations --limit 25 --project 数字经济综述`），再视需要加跑一轮 `--sort date --year-from 2024 --append --project 数字经济综述` 补充前沿文献。
   - 滚雪球溯源与前沿扩检（可选）：挑 1–2 篇高被引基石论文运行 `citations "<知网URL或题名>" --save both --append --project 数字经济综述`。
   - `cnki_feasible: false`：用 `search "<英文词>" --source all --async-search --project <课题名>`，多组检索叠加 `--append`。
2. **筛选去噪与阅读批注（`library`）**
   - 运行 `library --project <课题名> --dedupe` 自动跨轮去重；
   - 用 `library --project <课题名> --remove 4,9`（或 `--keep 1,2,3,5-12`）剔除偏题文献，必要时用 `--indices 1,3 --tag "核心机制" --note "基准实证模型"` 标记重点文献。
3. **生成可追溯综述证据包与全文精读**
   - 运行 `review --project <课题名> --topic "<综述主题>" --auto-detail --detail-top-n 6 --cluster --gaps`（自动单会话补全知网摘要、输出动态聚类与覆盖度空白提示）；
   - 对需要深挖论证细节的 3–5 篇代表作运行 `read-detail --project <课题名> --indices 1,2,5 --fulltext`（本地已下载 PDF 时秒级提取免滑块）。
4. **撰写综述与证据支撑核验（`validate`）**
   - Agent 基于证据包撰写综述正文（先按正文引用先后保留课题库证据序号用于核查，运行 `validate --project <课题名> --file draft.md` 检查是否存在无支撑断言或仅题录强断言）。
5. **按正文引用顺序生成标准参考文献（`cite --indices`）**
   - 按正文最终首次出现顺序传入 `--indices`（如正文依次引了课题库第 5、1、8 篇，则运行 `cite --project <课题名> --indices 5,1,8 --style gbt7714`），脚本仅对被选文献补全卷期页码并自动重编为 `[1][2][3]`，严禁手拼参考文献。

## 引用建议

1. `read-paper --numbered` 读取用户论文（获取 `[P12]` 等段落锚点）
2. 识别需要引用的句子 → 提取关键词检索入库 → `read-detail --indices ...` 核对摘要/全文
3. 精准匹配：哪一段（`[P12]`）哪句话引哪篇文献的哪个论据，区分"必须引用"和"建议引用"
4. 调用 `cite --indices <选用序号> --style gbt7714` 生成标准引文

## 改写论文并生成 Word（内容大改）

1. `read-paper` 读取用户论文
2. 提取关键词 → 搜索并筛选入库 → 获取参考文献摘要/全文
3. Agent 改写论文，输出 Markdown 文件，用 `[^1]` 标记脚注
4. 在 Markdown 末尾用 `[^1]: 引用文本` 定义脚注内容（或用 `## 参考文献` 节附上 `cite` 生成的国标引文）
5. `write-docx draft.md --output 论文.docx` → 生成标准学术格式 Word

## 基于用户提供的 PDF 文献库

用户提供一个文件夹（含多篇中英文 PDF），要求筛选可用文献、写综述或插入引用：

1. **一键导入课题库**：运行 `import "<PDF目录路径>" --project <课题名>`（自动递归扫描全部 `.pdf`，解析英文 DOI/Crossref 元数据及中文知网 PDF 首页摘要、关键词、年卷期，并记录 `local_pdf` 路径）。
2. **初筛与整理**：运行 `library --project <课题名>` 查看题录，用 `library --remove ...` 剔除无关条目，或运行 `review --project <课题名> --cluster --gaps` 获取主题聚类。
3. **按需提取带页码全文**：对核心篇目运行 `read-detail --project <课题名> --indices 1,3,5 --fulltext`（直接提取本地 PDF `[第 N 页]` 全文）或用 Agent 内置文件读取工具精读图表。
4. **闭环输出**：写综述用 `validate` + `write-docx`；生成参考文献统一调用 `cite --project <课题名> --indices ... --style gbt7714`；在原稿插引用用 `patch-docx --dry-run` 预检后写回。

## 在原论文中插入引用（保留格式 + 锚点预检）

1. `read-paper 原论文.docx --numbered` 读取用户论文，记录目标段落锚点（如 `P12`、表格行 `T1R2`）。
2. 搜索匹配引用并用 `cite --indices ...` 生成标准引用文本。
3. Agent 生成补丁 JSON 文件（推荐带上 `"paragraph": "P12"` 消除跨章节同句歧义）：
   ```json
   {
     "patches": [{"paragraph": "P12", "find": "原文片段", "replace": "替换后片段[1]"}],
     "footnotes": [{"paragraph": "P12", "after": "定位文本", "text": "脚注引用内容"}],
     "append_references": ["[1] 参考文献1", "[2] 参考文献2"]
   }
   ```
4. **先预检再写盘**：先运行 `patch-docx 原论文.docx --patch patch.json --dry-run` 确认 `not_found == 0` 且无意外重复匹配，再去掉 `--dry-run` 输出正式 `.docx`（支持正文段落与表格单元格）。

## 学术表达优化

当用户请求"降重""改重""降低重复率"或"改写表达""去 AI 腔"时，按以下步骤执行。每一步都是独立的学术写作辅助任务。

**改写前先读 [表达审计规则](style-audit.md)**。其中的问题清单、保留清单和证据纪律是后续步骤的判据，不要凭感觉润色。

**第一步：阅读与理解论文**

`read-paper 原论文.docx --numbered` 读取带 `[P1]...[Pn]` 段落锚点的全文，理解结构、核心论点与论证逻辑。用户提供了本人既往论文时，一并读取 1–2 篇作为风格样本。

**第二步：审计（只诊断，不改写）**

按 [style-audit](style-audit.md) 逐段检查三个维度，产出问题清单（段落锚点 `P...` / 命中类型 / 初步改法），先交给用户确认再动手：
- **原创性**：概念定义段、文献引述段、论证过渡段是否只做了搬运（style-audit 第一节）
- **文体**：中文 AI 腔清单 2.1–2.10 命中哪些（写英文时另查 2.11）
- **证据**：强断言无支撑、动词强于证据、空洞量级（style-audit 第四节）

如用户提供了标注报告（PDF），用 Agent 内置工具读取，与审计结果合并，以报告标记为主要依据。

**第三步：逐段改写**

对每个标记段落，按审计结论逐条改写：
- **措辞与句式**：按 style-audit 第二章的对应条目替换，不做清单外的自由发挥
- **原创论述增强**：在引用他人观点后补充作者的分析、比较或评价
- **论证逻辑**：补出前提→推断，而不是只加连接词
- **证据对齐**：无支撑的强断言补指标、表号或引文；补不上就降级动词

约束：**不得改变原文学术含义，不得删除关键论据，不得引入原文未涉及的观点，不得改动任何数字、公式、引文**（见 style-audit 第三节保留清单）

**第四步：预检与写回文档**

生成带 `"paragraph": "P..."` 锚点的 patch JSON → 先运行 `patch-docx 原论文.docx --patch patch.json --dry-run` 确保 100% 命中无漏改 → 再执行 `patch-docx` 保留原格式写回（改写幅度极大时可用 `write-docx` 全文输出）。

**第五步：报告改动**

按 style-audit 第七节的模板输出变更日志：段落锚点、命中类型、改动要点、证据核查结果，并声明数字/引文未改动的处数。

**限制**：本工具不提供文本相似度检测，无法给出重复率数值，也不针对查重系统做规避；优化效果需用户自行验证。详见 [style-audit 第六节](style-audit.md#六与查重的边界)。

## 引文网络分析（中英文双向引文 + 滚雪球入库）

用户提供一篇论文（知网详情页 URL、中文论文题名、英文 DOI 或 arXiv ID），要求追踪引用关系：

1. `citations "<知网URL / 中文题名 / DOI / arXiv>"` 获取双向引文网络（默认 `--direction both`，支持加 `--save both --append --project <课题>` 直接把引文并入课题文献库）：
   - **中文论文（`cnki_feasible: true`）**：自动走知网详情页 `#refpartdiv` 原生引文网络（无需登录个人账号），同时抓取**引证文献（`citing`）**与**参考文献（`references`）**。
   - **英文 / DOI / arXiv 论文**：自动走 Semantic Scholar Graph API，`cnki_feasible: false` 时仍可正常使用。
2. Agent 展示结果：
   - 前向引用（`--direction citing`，输出字段 `citing`）：后续研究如何发展了这篇论文的观点
   - 后向引用（`--direction cited`，输出字段 `references`）：这篇论文引用了哪些前人工作
3. 滚雪球后续动作：入库后可直接衔接 `library --dedupe`、`read-detail --indices ...`、`trends` 或 `review --cluster --gaps`。

## 研究趋势分析

用户搜索了一批论文后，要求分析研究趋势：

1. 确保会话中有搜索数据（先执行 search 或 batch-search）
2. `trends` 输出聚合统计：年份分布、高频关键词 Top 30、高被引论文 Top 10、数据源分布
3. Agent 据此向用户描述：
   - 发文量趋势（哪些年份发文多，是否在增长）
   - 热点关键词（当前研究聚焦什么主题）
   - 关键文献（高被引论文有哪些，值得优先阅读）

## 文献对比矩阵

用户要求对比多篇论文时，Agent 按以下模板整理（不需要新脚本命令）：

1. 读取论文内容：知网论文用 `read-detail --fulltext`；API 源论文直接使用搜索返回的摘要，或由用户提供 PDF
2. 对每篇论文提取以下维度：

| 维度 | 说明 |
|------|------|
| 研究问题 | 论文试图回答什么问题 |
| 研究方法 | 定性/定量/混合，具体方法 |
| 数据来源 | 样本量、数据收集方式 |
| 核心发现 | 主要结论（1-2 句） |
| 理论贡献 | 对已有理论的推进 |
| 局限性 | 作者承认的局限 |
| 与用户研究的关联 | 如何为用户研究提供支持 |

3. 输出为 Markdown 对比表，可用 `write-docx` 生成 Word

## 阅读笔记生成

用户要求对论文做阅读笔记时，Agent 按以下结构输出（不需要新脚本命令）：

对每篇论文生成：

```
## [论文标题]（[年份]）
**作者**：[作者列表]
**期刊**：[期刊名]
**核心论点**：[1-2 句概括]
**研究方法**：[方法描述]
**关键发现**：
- [发现 1]
- [发现 2]
**与我的研究的关联**：[如何为当前研究提供支持或启发]
**值得引用的观点**：[具体可引用的论述，标注页码/章节]
**局限与未来方向**：[作者提出的局限和研究展望]
```

多篇论文时，最后附一段"综合评述"，指出论文间的共识、分歧和研究空白。
