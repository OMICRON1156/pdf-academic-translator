---
name: pdf-academic-translator
description: 将英文或其他语种 PDF（学术专著、书籍）完整翻译为简体中文 PDF，页数与原书一致、页面内容对应、跨页段落上下文连贯、术语全书统一，并输出可按页码与文本块定位问题的错误报告。使用前按固定顺序逐项收集 API 地址、API 密钥、模型名、本地 PDF 路径、分批页数（默认 10 页/批）和并发进程数（默认 4），每条消息只问一项，用户已直接提供的参数不重复询问；启动与各阶段结果使用固定模板报告并请求确认。然后按提取、结构化整理、全书分析、翻译、排版合成的顺序逐步执行，翻译完成后请用户选择只输出 PDF 或同时输出 PDF 与 EPUB，最后输出成品文件与错误报告。断点续传前先终止残留进程、检查目录并向用户提供决策参考，确认后再续跑。当用户要求翻译英文或其他语种 PDF 书籍、输出中文版 PDF 或 EPUB、处理跨页段落/术语一致性时使用本 skill。
---

# PDF Academic Translator

把 PDF 学术专著（英文或其他语种）翻译成简体中文 PDF：模型自动识别原文语种，页数与原书一致、页面内容对应、跨页段落上下文连贯、术语全书统一，并输出可按页码和文本块定位问题的错误报告。

## 交互流程

按固定顺序逐项收集，每条消息只问一项，得到回答后再问下一项。询问措辞使用 `references/report_templates.md` 中的【配置 · ...】模板，不把 API 与分批参数合并提问；用户已直接提供的参数直接采用，不重复询问。

1. API 地址（兼容带 /v1 或 /chat/completions 的写法）。
2. API 密钥（OpenAI 兼容 Bearer 密钥）。
3. 模型名：运行 `python scripts/list_models.py "<API 地址>" "<API 密钥>"` 获取候选并请用户选择；列表为空时请用户直接输入模型名。
4. 本地 PDF 路径，确认文件存在。
5. 分批页数（默认 10，建议不超过 20）。
6. 并发进程数（默认 4，建议不超过 8）。

收集完毕后先展示【配置汇总】，请用户确认，再开始运行。

## 分步执行（默认模式，每步展示产物并请求确认）

按顺序逐阶段推进，每完成一个阶段就把该阶段产物展示给用户，请求确认后再进入下一阶段。所有命令使用同一 `--work-dir`、`--batch-size <分批大小>` 与 `--max-workers <并发数>`（取用户确认值，默认 10 与 4）；断点续传会自动跳过已完成阶段，重跑同一命令只补做未完成部分。

1. 提取：`python scripts/pipeline_v1.py "<PDF 路径>" --batch-size <分批大小> --max-workers <并发数> --work-dir "<输出根目录>" --until extract`
   提取会同时落盘整本 `01_extracted/extracted.json` 与按分批大小切分的 `01_extracted/extracted_batch_XX.json`（每批一个文件）。运行后展示提取摘要与文件路径：
   `python scripts/summarize.py "<输出根目录>/work_<书名>" --stage extract`
2. 结构化整理：同一命令把 `--until` 改为 `structure` 重跑。整理阶段逐个读取 `extracted_batch_XX.json`，一对一输出 `02_structured/structured_batch_XX.json`。每个批次模型调用超时 400 秒，超时主动关闭连接，仅对超时、限流（429）与 5xx 自动重试一次；4xx 等确定性错误与解析/校验失败不重试；失败批次停止并记入 `02_structured/failed_batches.json`，其他批次继续处理，不受失败批次影响。展示给用户确认后重跑同一命令，只补做失败批次。
   展示 `... --stage structure`：每页文本块性质统计（页眉页脚/标题/正文/脚注/题注图注/噪声）、印刷页码识别、空白页、跨页段落标记。
3. 全书分析：`--until analyze`。全书分析只发送结构化结果中标题与正文合并后的 Markdown，跨页段落先合并为完整段落，不发送脚注、图注或 JSON 标签。调用超时 400 秒，单次调用，失败即终止流水线并提示用户。
   展示 `... --stage analyze`：小节中心意思与原文-中文对照术语表全文。
4. 翻译：`--until translate`。翻译结果按批落盘为 `04_translated/translated_batch_XX.json`。翻译调用超时 180 秒（总时长硬超时），超时主动关闭连接，最多尝试两次，仅对超时、限流（429）与 5xx 自动重试；单个单元译文长度超过 `max(1.5×原文长度, 100 字符)` 时视为异常重复输出，本批停止并记入 `04_translated/failed_batches.json`；失败单元所属批次停止，不再处理该批后续单元，已完成的单元由 partial 文件保留，其他批次继续处理，不受失败批次影响。尝试失败后展示给用户确认，重跑同一命令只补做失败单元。
   展示 `... --stage translate`：翻译单元数、跨页单元数、[PB] 换页标记情况。
   翻译完成后使用【输出选择】模板询问用户：只输出 PDF，还是同时输出 PDF 与 EPUB。
5. 排版合成：用户选择只输出 PDF 时运行 `--until render`；选择同时输出 PDF 与 EPUB 时运行 `--until render --with-epub`。
   展示 `... --stage render`：成品 PDF 路径（以及 EPUB 路径），请用户打开核对版式。
6. 错误报告：完整运行（去掉 `--until`）生成报告，展示 `06_report/report.html` 路径与问题数。

展示方式：所有对话报告必须使用 `references/report_templates.md` 中的固定模板。任务开始前使用【启动报告】或【续跑报告】，阶段完成后使用【阶段报告】，失败时使用【阶段报告 · 失败】。把 `summarize.py` 的输出整理进对话（JSON 只展示统计摘要，术语表展示全文），同时给出每个产物文件的绝对路径供用户自行打开。用户确认后继续下一步；用户对产物提出修改意见时，先按意见调整提示词或参数，再重跑当前阶段。

## 一键执行（可选）

用户希望一次跑完所有阶段时使用：

```
python scripts/pipeline_v1.py "<PDF 路径>" --batch-size <分批大小> --max-workers <并发数> --work-dir "<输出根目录>"
```

若用户选择同时输出 PDF 与 EPUB，在命令末尾加 `--with-epub`。

## 断点续传流程

中断后续跑必须按以下顺序执行，不能直接重跑：

1. 终止残留进程：检查系统进程，确认没有正在运行的 `pipeline_v1.py`、`translate_v1.py` 等翻译脚本；如有，先终止并向用户说明。
2. 检查目录：读取 `work` 目录下的 `state.json`、`runner.lock`、`pipeline.log`、`04_translated/*.partial.json`、`failed_batches.json`，以及各阶段产物，统计已完成与缺失批次。
3. 给出决策参考：把待跑批次、页范围、单元数、类型构成和预计工作量展示给用户。
4. 等待确认：用户确认后，使用相同的 `--work-dir`、`--batch-size`、`--max-workers` 重跑；确认无进程后如残留 `runner.lock` 可删除。
5. 展示续跑结果：重跑后展示新落盘的 `translated_batch_*.json`、partial 文件与失败日志。

## 断点与并发机制

- 翻译为单元级断点：每完成一个单元即更新 `04_translated/translated_batch_XX.partial.json`；整批完成后生成正式 `translated_batch_XX.json` 并删除 partial。
- 同一工作目录同时只允许一个流水线进程：脚本用 `runner.lock` 目录加锁；发现锁时脚本会退出，由用户确认进程状态后再续跑。
- `state.json` 写盘前会合并磁盘上的最新状态，避免旧进程覆盖新进程的完成标记。
- 每次 API 调用有总时长硬超时（默认 400 秒，翻译阶段为 180 秒），超时、429 与 5xx 才会重试；确定性错误写入 `failed_batches.json`。
- `render` 与 `epub` 状态分开记录；已生成 PDF 后补跑 `--with-epub` 只会生成 EPUB，不会重复渲染 PDF。

## 环境与依赖

新电脑首次使用前先安装依赖（需要已装 Python 3.11+，并允许联网）：

```
python install_deps.py
```

该脚本检查 Python 版本，按 `requirements.txt` 安装 PyMuPDF 1.25.1 与 reportlab 5.0.0，并检查中文字体；只想确认环境是否就绪时加 `--check-only` 只检查不安装。

- Python：3.11 及以上（开发验证环境为 3.13.1）。
- 中文字体（渲染必需）：正文与页脚页码使用思源宋体 Regular（Noto Serif SC 分发版，`assets/NotoSerifSC-Regular.ttf`），标题使用思源宋体 Heavy（即 Noto Serif SC Black，`assets/NotoSerifSC-Black.ttf`）。两款字体均为 SIL OFL 开源许可，可自由分发；渲染时直接使用 `assets/` 内打包的字体文件，不读取系统字体目录，无需手动安装。
- 大模型：任意 OpenAI 兼容接口，默认 DeepSeek。

## 交付物

- 成品 PDF：`<work>/05_rendered/<书名>_zh.pdf`，页数与源一致、白底黑字、图像与表格为黑色占位框、图注表题照常翻译。
- 成品 EPUB（可选）：用户选择 PDF+EPUB 时生成 `<work>/05_rendered/<书名>_zh.epub`，按结构化页序与标题分章，跨页段落合并为完整段落，不保留 [PB] 或页码标记。
- 错误报告：`<work>/06_report/report.html`，按页号加块 ID 定位问题。
- 运行日志：`<work>/pipeline.log`，记录每次运行的控制台输出与未捕获异常。
- 中间产物全部落盘，供审计与断点续传。
- 外层命令可能被超时中断；重跑同一命令只会补做未完成单元，不会重译已落盘单元。

## 注意事项

- 源 PDF 只读，绝不覆盖；译文按页回填，源第 n 页内容只出现在译文第 n 页。
- 文件编码：所有中间产物与报告均以 UTF-8 写入；读取时兼容带 BOM 的 UTF-8 文件，控制台输出按 UTF-8 处理，不会因系统代码页引发编码崩溃。
- 跨页段落绝不按页切开翻译；跨页段落中间可隔整页插图或空白页，note 可用组合标记表达连续跨多页；译文 [PB] 数量必须与跨页数减一相等，位置不合格自动重试，仍失败则整段回退并记入错误报告。
- 翻译风格与术语一致性由 `prompts/` 下三个提示词约束；保持模型输出的块性质标签与页码标签不变。
- 页码统一放页脚居中；`page_no`（物理页序）与 `page_number`（印刷页码）绝不混用。
