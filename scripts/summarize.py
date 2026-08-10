"""输出流水线中间产物的可读摘要，供逐步执行时展示给用户。
用法: python scripts/summarize.py <work_dir> [--stage extract|structure|analyze|translate|render]
"""
import json
import os
import sys

KINDS_ZH = {
    "page_header_footer": "页眉页脚",
    "heading": "标题",
    "body": "正文",
    "footnote": "脚注",
    "caption": "题注图注",
    "noise": "噪声",
}


def summarize_extracted(work):
    p = os.path.join(work, "01_extracted", "extracted.json")
    if not os.path.exists(p):
        return ["（提取产物不存在）"]
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    lines = ["【提取】源文件：%s" % data.get("source_file", "")]
    lines.append("总页数：%d" % len(data["pages"]))
    for pg in data["pages"]:
        n_blocks = len(pg["blocks"])
        n_lines = sum(len(b["lines"]) for b in pg["blocks"])
        n_imgs = len(pg.get("image_regions", []))
        lines.append("  第%d页：文本块%d 行%d 图像区域%d" % (pg["page_no"], n_blocks, n_lines, n_imgs))
    return lines


def summarize_structured(work):
    d = os.path.join(work, "02_structured")
    if not os.path.isdir(d):
        return ["（结构化产物不存在）"]
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    if not files:
        return ["（结构化产物不存在）"]
    lines = ["【结构化整理】批次文件：%s" % ", ".join(files)]
    for fn in files:
        with open(os.path.join(d, fn), encoding="utf-8") as f:
            data = json.load(f)
        kind_count = {}
        notes = []
        blanks = []
        pagenos = []
        for pg in data["pages"]:
            if pg.get("is_blank"):
                blanks.append(pg["page_no"])
            pagenos.append(pg.get("page_number"))
            for b in pg.get("blocks", []):
                k = b.get("kind", "?")
                kind_count[k] = kind_count.get(k, 0) + 1
                n = b.get("note", "")
                if n:
                    notes.append("%s:%s" % (b.get("block_id", ""), n))
        cnt = "、".join("%s %d" % (KINDS_ZH.get(k, k), v) for k, v in sorted(kind_count.items()))
        lines.append("  文件 %s：页 %d-%d，印刷页码 %s，空白页 %s" % (
            fn, data["pages"][0]["page_no"], data["pages"][-1]["page_no"],
            ",".join(str(x) for x in pagenos) or "-", ",".join(map(str, blanks)) or "无"))
        lines.append("    块性质：%s" % cnt)
        if notes:
            lines.append("    跨页标记：%s" % "; ".join(notes))
    return lines


def summarize_glossary(work):
    p = os.path.join(work, "03_analysis", "glossary.md")
    if not os.path.exists(p):
        return ["（全书分析产物不存在）"]
    with open(p, encoding="utf-8") as f:
        content = f.read().strip()
    lines = ["【全书分析】文件：%s" % p]
    lines.append(content)
    return lines


def summarize_translated(work):
    d = os.path.join(work, "04_translated")
    if not os.path.isdir(d):
        return ["（翻译产物不存在）"]
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    if not files:
        return ["（翻译产物不存在）"]
    lines = ["【翻译】批次文件：%s" % ", ".join(files)]
    total = 0
    cross = 0
    pb_bad = 0
    for fn in files:
        with open(os.path.join(d, fn), encoding="utf-8") as f:
            data = json.load(f)
        units = data.get("units", [])
        n_cross = sum(1 for u in units if u.get("note") == "cross_page")
        n_bad = sum(1 for u in units if u.get("note") == "cross_page"
                    and "[PB]" not in u.get("zh_text", ""))
        total += len(units)
        cross += n_cross
        pb_bad += n_bad
        lines.append("  文件 %s：单元 %d，跨页单元 %d，缺 [PB] %d" % (fn, len(units), n_cross, n_bad))
    lines.append("  合计：单元 %d，跨页单元 %d，缺 [PB] %d" % (total, cross, pb_bad))
    return lines


def summarize_render(work, name):
    lines = []
    pdf = os.path.join(work, "05_rendered", name + "_zh.pdf")
    if os.path.exists(pdf):
        lines.append("【排版合成】成品 PDF：%s" % pdf)
    else:
        lines.append("【排版合成】成品尚未生成")
    report = os.path.join(work, "06_report", "report.html")
    if os.path.exists(report):
        lines.append("【错误报告】%s" % report)
    else:
        lines.append("【错误报告】尚未生成")
    return lines


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/summarize.py <work_dir> [--stage extract|structure|analyze|translate|render]")
        sys.exit(1)
    work = sys.argv[1]
    stage = None
    if "--stage" in sys.argv:
        stage = sys.argv[sys.argv.index("--stage") + 1]
    name = os.path.basename(work)
    if name.startswith("work_"):
        name = name[len("work_"):]
    out = []
    if stage in (None, "extract"):
        out.extend(summarize_extracted(work))
    if stage in (None, "structure"):
        out.extend(summarize_structured(work))
    if stage in (None, "analyze"):
        out.extend(summarize_glossary(work))
    if stage in (None, "translate"):
        out.extend(summarize_translated(work))
    if stage in (None, "render"):
        out.extend(summarize_render(work, name))
    print("\n".join(out))


if __name__ == "__main__":
    main()
