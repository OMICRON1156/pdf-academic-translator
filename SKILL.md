---
name: pdf-academic-translator
description: 将英文或其他语种 PDF（学术专著、书籍）完整翻译为简体中文 PDF，页数与原书一致、页面内容对应、跨页段落上下文连贯、术语全书统一，并输出可按页码与文本块定位问题的错误报告。使用前先依次向用户收集 API 地址、API 密钥、模型名、本地 PDF 路径、分批大小（默认 10 页/批）和并发数（默认 4 线程），然后按提取、结构化整理、全书分析、翻译、排版合成的顺序逐步执行，每步把该阶段产出的 JSON/Markdown 文件展示给用户并请求确认后再继续，最后输出成品 PDF 与错误报告。当用户要求翻译英文或其他语种 PDF 书籍、输出中文版 PDF 或处理跨页段落/术语一致性时使用本 skill。
---

# PDF Academic Translator

把 PDF 学术专著（英文或其他语种）翻译成简体中文 PDF：模型自动识别原文语种，页数与原书一致、页面内容对应、跨页段落上下文连贯、术语全书统一，并输出可按页码和文本块定位问题的错误报告。

## 交互流程

按顺序向用户收集六样信息，再开始运行：

1. 询问 API 地址（base URL，例如 https://api.deepseek.com；兼容带 /v1 或 /chat/completions 的写法）。
2. 询问 API 密钥（OpenAI 兼容 Bearer 密钥）。
3. 获取模型列表并请用户选择：运行
   `python scripts/list_models.py "<API 地址>" "<API 密钥>"`
   展示返回的模型 ID 供用户选择；若列表为空，请用户直接输入模型名。
4. 询问本地 PDF 路径，确认文件存在。
5. 向用户确认两个运行参数（先简要解释含义，再给出默认值，用户可直接接受默认）：
   - 分批大小（默认 10 页/批）：每批交给模型整理的页数；越大上下文越连贯、模型单次负载越大，建议不超过 20。
   - 并发数（默认 4 线程）：整理与翻译阶段同时调用模型的批次数量；越高处理越快，但过高可能触发限流或模型故障，建议不超过 8。
6. 运行完整流水线，交付成品 PDF 与错误报告。

## 分步执行（默认模式，每步展示产物并请求确认）

按顺序逐阶段推进，每完成一个阶段就把该阶段产物展示给用户，请求确认后再进入下一阶段。所有命令使用同一 `--work-dir`、`--batch-size <分批大小>` 与 `--max-workers <并发数>`（取用户确认值，默认 10 与 4）；断点续传会自动跳过已完成阶段，重跑同一命令只补做未完成部分。

1. 提取：`python scripts/pipeline_v1.py "<PDF 路径>" --batch-size <分批大小> --max-workers <并发数> --work-dir "<输出根目录>" --until extract`
   运行后展示提取摘要与文件路径：
   `python scripts/summarize.py "<输出根目录>/work_<书名>" --stage extract`
2. 结构化整理：同一命令把 `--until` 改为 `structure` 重跑。
   展示 `... --stage structure`：每页文本块性质统计（页眉页脚/标题/正文/脚注/题注图注/噪声）、印刷页码识别、空白页、跨页段落标记。
3. 全书分析：`--until analyze`。
   展示 `... --stage analyze`：小节中心意思与原文-中文对照术语表全文。
4. 翻译：`--until translate`。
   展示 `... --stage translate`：翻译单元数、跨页单元数、[PB] 换页标记情况。
5. 排版合成：`--until render`。
   展示 `... --stage render`：成品 PDF 路径，请用户打开核对版式。
6. 错误报告：完整运行（去掉 `--until`）生成报告，展示 `06_report/report.html` 路径与问题数。

展示方式：把 `summarize.py` 的输出整理进对话（JSON 只展示统计摘要，术语表展示全文），同时给出每个产物文件的绝对路径供用户自行打开。用户确认后继续下一步；用户对产物提出修改意见时，先按意见调整提示词或参数，再重跑当前阶段。

## 一键执行（可选）

用户希望一次跑完所有阶段时使用：

```
python scripts/pipeline_v1.py "<PDF 路径>" --batch-size <分批大小> --max-workers <并发数> --work-dir "<输出根目录>"
```

## 环境与依赖

新电脑首次使用前先安装依赖（需要已装 Python 3.11+，并允许联网）：

```
python install_deps.py
```

该脚本检查 Python 版本，按 `requirements.txt` 安装 PyMuPDF 1.25.1 与 reportlab 5.0.0，并检查中文字体；只想确认环境是否就绪时加 `--check-only` 只检查不安装。

- Python：3.11 及以上（开发验证环境为 3.13.1）。
- 中文字体（渲染必需）：正文与页脚页码使用思源宋体 Regular（Noto Serif SC 分发版，`assets/NotoSerifSC-Regular.ttf`），标题使用思源宋体 Heavy（即 Noto Serif SC Black，`assets/NotoSerifSC-Black.ttf`）。两款字体均为 SIL OFL 开源许可，可自由分发；渲染时优先使用系统字体，系统缺失时自动回退到 `assets/` 内的打包字体，无需手动安装。
- 大模型：任意 OpenAI 兼容接口，默认 DeepSeek。

## 交付物

- 成品 PDF：`<work>/05_rendered/<书名>_zh.pdf`，页数与源一致、白底黑字、图像与表格为黑色占位框、图注表题照常翻译。
- 错误报告：`<work>/06_report/report.html`，按页号加块 ID 定位问题。
- 中间产物全部落盘，供审计与断点续传。

## 注意事项

- 源 PDF 只读，绝不覆盖；译文按页回填，源第 n 页内容只出现在译文第 n 页。
- 跨页段落绝不按页切开翻译；译文 [PB] 位置不合格自动重试，仍失败则整段回退并记入错误报告。
- 翻译风格与术语一致性由 `prompts/` 下三个提示词约束；保持模型输出的块性质标签与页码标签不变。
- 页码统一放页脚居中；`page_no`（物理页序）与 `page_number`（印刷页码）绝不混用。
