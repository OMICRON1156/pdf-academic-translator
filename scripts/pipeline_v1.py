"""流水线主控 v1——提取、分批整理、全书分析、分批翻译、排版、错误报告。
用法: python scripts/pipeline_v1.py <input.pdf> [--batch-size 10] [--max-workers 4] [--work-dir DIR]
密钥从环境变量 DEEPSEEK_API_KEY 读取。
工作目录: tmp/work_<源文件名>/（每阶段落盘，支持断点续传）
整理与翻译阶段按批次并发调用大模型，每批一个线程。
"""
import html
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)

import extract_v1
import structure_v1
import analyze_v1
import translate_v1
import render_v1

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(BASE, "tmp")
BATCH_SIZE = 10


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def load_state(work):
    p = os.path.join(work, "state.json")
    if os.path.exists(p):
        return load_json(p)
    return {}


def save_state(work, state):
    save_json(os.path.join(work, "state.json"), state)


def split_batches(extracted, size):
    """按页切批；每批 pages 保留全局 page_no。"""
    pages = extracted["pages"]
    batches = []
    for i in range(0, len(pages), size):
        batch = {
            "source_file": extracted.get("source_file", ""),
            "total_pages": len(pages[i:i+size]),
            "pages": pages[i:i+size],
        }
        batches.append(batch)
    return batches


def run_parallel(task, items, max_workers):
    """用线程池并发执行任务，每个任务各占一个线程；返回错误信息列表。"""
    errors = []
    if not items:
        return errors
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(task, item): item for item in items}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                errors.append(str(exc))
    return errors


def call_structure(batch, api_key):
    payload = json.dumps(batch, ensure_ascii=False)
    messages = [
        {"role": "system", "content": structure_v1.SYSTEM_PROMPT},
        {"role": "user", "content": "以下是待整理的渲染数据 JSON，请按要求输出整理结果：\n" + payload},
    ]
    content = structure_v1.call_deepseek(messages, api_key)
    return structure_v1.parse_json(content)


def call_analyze(merged, api_key):
    payload = json.dumps(merged, ensure_ascii=False)
    messages = [
        {"role": "system", "content": analyze_v1.SYSTEM_PROMPT},
        {"role": "user", "content": "以下是全书结构化文本，请按要求输出全书分析 Markdown：\n" + payload},
    ]
    return analyze_v1.call_deepseek(messages, api_key).strip()


def call_translate(units, api_key):
    result_units = []
    problems = []
    for u in units:
        content = None
        raw = None
        for attempt in range(3):
            try:
                hint = ""
                if attempt > 0 and u["note"] == "cross_page":
                    hint = "[PB] 必须位于段落中间对应原文 [PAGEBREAK] 的位置，绝不能放在段落开头或末尾。"
                raw = translate_v1.call_deepseek(translate_v1.build_messages(u, hint), api_key).strip()
                zh = html.unescape(raw)
                if translate_v1.pb_ok(zh, u["note"]):
                    content = zh
                    break
                print("  %s [PB] 校验不通过，重试 %d" % (u["unit_id"], attempt + 1))
            except Exception as exc:
                print("  尝试 %d 失败: %s" % (attempt + 1, exc))
                time.sleep(2)
        if content is None and raw is not None:
            # 保留最后一次译文（即使 [PB] 不合格），回退整段到第一块
            content = html.unescape(raw)
            problems.append("%s [PB] 数量或位置不合格，回退整段到第一块" % u["unit_id"])
        elif content is None:
            problems.append("%s 翻译失败" % u["unit_id"])
        zh = content if content is not None else ""
        result_units.append({
            "unit_id": u["unit_id"],
            "blocks": u["blocks"],
            "kind": u["kind"],
            "pages": u["pages"],
            "note": u["note"],
            "zh_text": zh,
        })
    return result_units, problems


def merge_structured(batch_files):
    pages = []
    for f in batch_files:
        data = load_json(f)
        pages.extend(data["pages"])
    pages.sort(key=lambda p: p["page_no"])
    return {"total_pages": len(pages), "pages": pages}


def merge_translated(batch_files):
    units = []
    for f in batch_files:
        data = load_json(f)
        units.extend(data["units"])
    return {"units": units}


def generate_report(work, issues, extracted):
    rows = []
    for it in issues:
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            html.escape(str(it.get("page", ""))),
            html.escape(str(it.get("block", ""))),
            html.escape(str(it.get("kind", ""))),
            html.escape(str(it.get("detail", ""))),
        ))
    blank_pages = [p["page_no"] for p in extracted["pages"] if not p.get("blocks") and not p.get("image_regions")]
    for pno in blank_pages:
        rows.append("<tr><td>%d</td><td>-</td><td>blank</td><td>空白页</td></tr>" % pno)
    body = "".join(rows) if rows else "<tr><td colspan=4>无问题</td></tr>"
    doc = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>错误报告</title></head>
<body><h1>翻译错误报告</h1>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>页</th><th>块</th><th>类型</th><th>说明</th></tr>
%s
</table></body></html>""" % body
    report_path = os.path.join(work, "06_report", "report.html")
    ensure_dir(os.path.dirname(report_path))
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return report_path


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/pipeline_v1.py <input.pdf> [--batch-size 10] [--max-workers 4] [--work-dir DIR]")
        sys.exit(1)
    pdf = sys.argv[1]
    global BATCH_SIZE
    if "--batch-size" in sys.argv:
        BATCH_SIZE = int(sys.argv[sys.argv.index("--batch-size") + 1])

    max_workers = 4
    if "--max-workers" in sys.argv:
        max_workers = max(1, int(sys.argv[sys.argv.index("--max-workers") + 1]))

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    until = None
    if "--until" in sys.argv:
        until = sys.argv[sys.argv.index("--until") + 1]
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY 环境变量")
        sys.exit(1)

    name = os.path.splitext(os.path.basename(pdf))[0]
    work_root = TMP
    if "--work-dir" in sys.argv:
        work_root = sys.argv[sys.argv.index("--work-dir") + 1]
    work = os.path.join(work_root, "work_" + name)
    ensure_dir(work)
    state = load_state(work)
    issues = []
    state_lock = threading.Lock()

    # 阶段1 提取
    if state.get("extract"):
        print("[1/6] 提取：已存在，跳过")
        extracted = load_json(os.path.join(work, "01_extracted", "extracted.json"))
    else:
        print("[1/6] 提取 ...")
        extracted = extract_v1.extract(pdf)
        save_json(os.path.join(work, "01_extracted", "extracted.json"), extracted)
        state["extract"] = True
        save_state(work, state)
    print("  共 %d 页" % len(extracted["pages"]))
    if until in ("extract", "1"):
        print("已运行至「提取」阶段，停止。下一步：结构化整理。\n"
              "查看产物：%s" % os.path.join(work, "01_extracted", "extracted.json"))
        sys.exit(0)

    # 阶段2 分批整理（每批一个线程并发调用，避免排队等待）
    batches = split_batches(extracted, BATCH_SIZE)
    structured_files = []
    pending = []
    for idx, batch in enumerate(batches):
        out = os.path.join(work, "02_structured", "batch_%02d.json" % (idx + 1))
        structured_files.append(out)
        if state.get("structure_%d" % idx):
            print("[2/6] 整理批次 %d：已存在，跳过" % (idx + 1))
            continue
        pending.append((idx, batch, out))
    if pending:
        workers = min(max_workers, len(pending))

        def structure_task(item):
            idx, batch, out = item
            first, last = batch["pages"][0]["page_no"], batch["pages"][-1]["page_no"]
            print("[2/6] 整理批次 %d（页 %d-%d）开始..." % (idx + 1, first, last))
            try:
                structured = call_structure(batch, api_key)
            except Exception as exc:
                raise RuntimeError("整理批次 %d 调用失败：%s" % (idx + 1, exc))
            save_json(out, structured)
            with state_lock:
                state["structure_%d" % idx] = True
                save_state(work, state)
            print("[2/6] 整理批次 %d（页 %d-%d）完成。" % (idx + 1, first, last))

        print("[2/6] 并发整理 %d 个批次，并发数 %d ..." % (len(pending), workers))
        errors = run_parallel(structure_task, pending, workers)
        if errors:
            for msg in errors:
                print("  " + msg)
            sys.exit(1)
    merged_structured = merge_structured(structured_files)
    if until in ("structure", "2"):
        print("已运行至「结构化整理」阶段，停止。下一步：全书分析。\n"
              "查看产物：%s" % os.path.join(work, "02_structured", "batch_*.json"))
        sys.exit(0)

    # 阶段3 全书分析
    glossary_path = os.path.join(work, "03_analysis", "glossary.md")
    if state.get("analyze"):
        print("[3/6] 全书分析：已存在，跳过")
        with open(glossary_path, encoding="utf-8") as f:
            glossary = f.read().strip()
    else:
        print("[3/6] 全书分析（%d 页）..." % len(merged_structured["pages"]))
        glossary = call_analyze(merged_structured, api_key)
        ensure_dir(os.path.dirname(glossary_path))
        with open(glossary_path, "w", encoding="utf-8") as f:
            f.write(glossary)
        state["analyze"] = True
        save_state(work, state)
    translate_v1.GLOSSARY = glossary
    if until in ("analyze", "3"):
        print("已运行至「全书分析」阶段，停止。下一步：翻译。\n"
              "查看产物：%s" % os.path.join(work, "03_analysis", "glossary.md"))
        sys.exit(0)
    print("  术语表条目行数：", len([l for l in glossary.splitlines() if l.startswith("|")]) - 2)

    # 阶段4 分批翻译（全书组织单元，按首块所在页分配到批次）
    units = translate_v1.build_units(merged_structured)
    batch_units = [[] for _ in batches]
    for u in units:
        first_page = u["pages"][0]
        bidx = (first_page - 1) // BATCH_SIZE
        if bidx >= len(batch_units):
            bidx = len(batch_units) - 1
        batch_units[bidx].append(u)
    # 阶段4 分批翻译（每批一个线程并发调用，避免排队等待）
    translated_files = []
    pending = []
    for idx, bu in enumerate(batch_units):
        out = os.path.join(work, "04_translated", "batch_%02d.json" % (idx + 1))
        translated_files.append(out)
        if state.get("translate_%d" % idx):
            print("[4/6] 翻译批次 %d：已存在，跳过" % (idx + 1))
            continue
        pending.append((idx, bu, out))
    if pending:
        workers = min(max_workers, len(pending))

        def translate_task(item):
            idx, bu, out = item
            print("[4/6] 翻译批次 %d（%d 个单元）开始..." % (idx + 1, len(bu)))
            try:
                result_units, _ = call_translate(bu, api_key)
            except Exception as exc:
                raise RuntimeError("翻译批次 %d 调用失败：%s" % (idx + 1, exc))
            save_json(out, {"units": result_units})
            with state_lock:
                state["translate_%d" % idx] = True
                save_state(work, state)
            print("[4/6] 翻译批次 %d（%d 个单元）完成。" % (idx + 1, len(bu)))

        print("[4/6] 并发翻译 %d 个批次，并发数 %d ..." % (len(pending), workers))
        errors = run_parallel(translate_task, pending, workers)
        if errors:
            for msg in errors:
                print("  " + msg)
            sys.exit(1)
    merged_translated = merge_translated(translated_files)
    print("  翻译单元总数：", len(merged_translated["units"]))
    if until in ("translate", "4"):
        print("已运行至「翻译」阶段，停止。下一步：排版合成。\n"
              "查看产物：%s" % os.path.join(work, "04_translated", "batch_*.json"))
        sys.exit(0)
    for u in merged_translated["units"]:
        if not translate_v1.pb_ok(u["zh_text"], u["note"]):
            issues.append({"page": "", "block": u["unit_id"], "kind": "translate",
                           "detail": "%s [PB] 数量或位置不合格" % u["unit_id"]})

    # 阶段5 排版
    out_pdf = os.path.join(work, "05_rendered", name + "_zh.pdf")
    if state.get("render"):
        print("[5/6] 排版：已存在，跳过")
    else:
        print("[5/6] 排版 ...")
        size_by_page = {p["page_no"]: (p["page_size"][0], p["page_size"][1]) for p in extracted["pages"]}
        block_text = render_v1.split_translated_units(merged_translated)
        pages = render_v1.build_pages(merged_structured, block_text)
        from reportlab.pdfgen import canvas
        ensure_dir(os.path.dirname(out_pdf))
        out = canvas.Canvas(out_pdf)
        for page in pages:
            w, h = size_by_page.get(page["page_no"], (441.0, 666.0))
            out.setPageSize((w, h))
            warns = render_v1.draw_page(out, page, (w, h))
            for wmsg in warns:
                issues.append({"page": page["page_no"], "block": "", "kind": "render", "detail": wmsg})
            out.showPage()
        out.save()
        state["render"] = True
        save_state(work, state)
    print("  输出：", out_pdf)
    if until in ("render", "5"):
        print("已运行至「排版合成」阶段，停止。下一步：生成错误报告。\n"
              "查看产物：%s" % out_pdf)
        sys.exit(0)

    # 阶段6 错误报告
    print("[6/6] 错误报告 ...")
    report_path = generate_report(work, issues, extracted)
    print("  报告：", report_path)
    print("  问题总数：", len(issues))
    for it in issues[:20]:
        print("    -", it)


if __name__ == "__main__":
    main()
