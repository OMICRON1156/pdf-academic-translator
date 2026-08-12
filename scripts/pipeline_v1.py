"""流水线主控 v1——提取、分批整理、全书分析、分批翻译、排版、EPUB、错误报告。
用法: python scripts/pipeline_v1.py <input.pdf> [--batch-size 10] [--max-workers 4] [--work-dir DIR] [--with-epub]
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
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)

import extract_v1
import structure_v1
import analyze_v1
import translate_v1
import render_v1
import epub_v1
import llm_client

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(BASE, "tmp")
BATCH_SIZE = 10
LOCK_DIR = None


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self):
        return getattr(self.streams[0], "isatty", lambda: False)()


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def load_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path, data):
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_state(work):
    p = os.path.join(work, "state.json")
    if os.path.exists(p):
        try:
            return load_json(p)
        except (json.JSONDecodeError, ValueError) as exc:
            print("state.json 已损坏（%s），请检查该文件或改用新工作目录" % exc)
            release_runner_lock()
            sys.exit(1)
    return {}


def save_state(work, state):
    """写 state 前先合并磁盘上的最新状态，避免旧进程覆盖新进程的完成标记。"""
    path = os.path.join(work, "state.json")
    latest = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                latest = json.load(f)
        except (json.JSONDecodeError, ValueError):
            latest = {}
    latest.update(state)
    state.update(latest)
    save_json(path, latest)


def acquire_runner_lock(work):
    """用目录锁保证同一工作目录同时只有一个流水线进程。"""
    global LOCK_DIR
    lock_dir = os.path.join(work, "runner.lock")
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        print("发现 runner.lock：工作目录可能已有流水线进程在运行。")
        print("请先确认并终止该进程，再由用户确认后删除 runner.lock 目录或重跑。")
        sys.exit(1)
    with open(os.path.join(lock_dir, "owner.json"), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "started_at": time.time()}, f, ensure_ascii=False)
    LOCK_DIR = lock_dir


def release_runner_lock():
    global LOCK_DIR
    if not LOCK_DIR:
        return
    try:
        owner = os.path.join(LOCK_DIR, "owner.json")
        if os.path.exists(owner):
            os.remove(owner)
        os.rmdir(LOCK_DIR)
    except OSError:
        pass
    LOCK_DIR = None


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
    result = structure_v1.parse_json(content)
    structure_v1.validate_structured(result, batch)
    return result


def call_analyze(merged, api_key):
    payload = analyze_v1.build_markdown(merged)
    messages = [
        {"role": "system", "content": analyze_v1.SYSTEM_PROMPT},
        {"role": "user", "content": "以下是全书正文 Markdown，请按要求输出全书分析 Markdown：\n" + payload},
    ]
    return analyze_v1.call_deepseek(messages, api_key).strip()


def call_translate(units, api_key, progress_path=None, existing_units=None):
    """流水线翻译入口：复用 translate_v1.translate_units，保持 partial 断点与失败逻辑一致。"""
    return translate_v1.translate_units(units, api_key, progress_path=progress_path,
                                        existing_units=existing_units)


output_too_long = translate_v1.output_too_long


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

    with_epub = "--with-epub" in sys.argv

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
    acquire_runner_lock(work)
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
    batches = extract_v1.split_batches(extracted, BATCH_SIZE)
    for idx, batch in enumerate(batches):
        save_json(os.path.join(work, "01_extracted", "extracted_batch_%02d.json" % (idx + 1)), batch)
    print("  共 %d 页，按分批大小 %d 切分为 %d 批" % (len(extracted["pages"]), BATCH_SIZE, len(batches)))
    if until in ("extract", "1"):
        print("已运行至「提取」阶段，停止。下一步：结构化整理。\n"
              "查看产物：%s（整本 extracted.json 与分批 extracted_batch_*.json）"
              % os.path.join(work, "01_extracted"))
        release_runner_lock()
        sys.exit(0)

    # 阶段2 分批整理（每个 extracted_batch_XX.json 一对一整理，每批一个线程并发调用）
    structured_files = []
    pending = []
    failed = []
    failed_lock = threading.Lock()
    failed_path = os.path.join(work, "02_structured", "failed_batches.json")
    if os.path.exists(failed_path):
        os.remove(failed_path)
    for idx in range(len(batches)):
        in_path = os.path.join(work, "01_extracted", "extracted_batch_%02d.json" % (idx + 1))
        out = os.path.join(work, "02_structured", "structured_batch_%02d.json" % (idx + 1))
        structured_files.append(out)
        if state.get("structure_s%d_%d" % (BATCH_SIZE, idx)):
            print("[2/6] 整理批次 %d：已存在，跳过" % (idx + 1))
            continue
        pending.append((idx, in_path, out))
    if pending:
        workers = min(max_workers, len(pending))

        def structure_task(item):
            idx, in_path, out = item
            batch = load_json(in_path)
            first, last = batch["pages"][0]["page_no"], batch["pages"][-1]["page_no"]
            print("[2/6] 整理批次 %d（页 %d-%d）开始..." % (idx + 1, first, last))
            last_error = None
            for attempt in range(2):
                try:
                    structured = call_structure(batch, api_key)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt == 0 and llm_client.is_retryable_error(exc):
                        print("  批次 %d 第 1 次调用失败（%s），自动重试..." % (idx + 1, exc))
                        time.sleep(2)
                        continue
                    print("  批次 %d 调用失败（%s），记入失败日志。" % (idx + 1, exc))
                    break
            if last_error is not None:
                with failed_lock:
                    failed.append({"batch": idx + 1, "page_from": first, "page_to": last,
                                   "error": str(last_error)})
                print("[2/6] 整理批次 %d（页 %d-%d）失败，已记入日志。" % (idx + 1, first, last))
                return
            save_json(out, structured)
            with state_lock:
                state["structure_s%d_%d" % (BATCH_SIZE, idx)] = True
                save_state(work, state)
            print("[2/6] 整理批次 %d（页 %d-%d）完成。" % (idx + 1, first, last))

        print("[2/6] 并发整理 %d 个批次，并发数 %d ..." % (len(pending), workers))
        errors = run_parallel(structure_task, pending, workers)
        if errors:
            for msg in errors:
                print("  " + msg)
            release_runner_lock()
            sys.exit(1)
    if failed:
        save_json(failed_path, failed)
        print("整理阶段有 %d 个批次失败，详情见：%s" % (len(failed), failed_path))
        for f in failed:
            print("  - 批次 %d（页 %d-%d）：%s" % (f["batch"], f["page_from"], f["page_to"], f["error"]))
        print("请向用户确认后重跑同一命令，重跑只会补做失败批次。")
        release_runner_lock()
        sys.exit(1)
    for idx in range(len(batches)):
        f = os.path.join(work, "02_structured", "structured_batch_%02d.json" % (idx + 1))
        if not os.path.exists(f):
            continue
        try:
            file_pnos = [p["page_no"] for p in load_json(f)["pages"]]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            print("结构化批次文件 %s 无法读取（%s），请检查该文件或改用新工作目录" % (f, exc))
            release_runner_lock()
            sys.exit(1)
        exp_pnos = [p["page_no"] for p in batches[idx]["pages"]]
        if file_pnos != exp_pnos:
            print("结构化批次文件 %s 的页序与实际分批不一致：文件 %s，应为 %s" % (f, file_pnos, exp_pnos))
            print("可能原因：分批大小在断点续传之间被修改，或旧工作目录残留。请使用原分批大小重跑，或改用新工作目录。")
            release_runner_lock()
            sys.exit(1)
    missing = [f for f in structured_files if not os.path.exists(f)]
    if missing:
        print("缺少结构化批次文件（可能被删除或状态损坏）：%s" % ", ".join(missing))
        print("请删除 state.json 后重跑，或改用新工作目录。")
        release_runner_lock()
        sys.exit(1)
    merged_structured = merge_structured(structured_files)
    expected_pnos = [p["page_no"] for p in extracted["pages"]]
    got_pnos = [p["page_no"] for p in merged_structured["pages"]]
    if got_pnos != expected_pnos:
        print("结构化合并页序与提取结果不一致：实际页号 %s，应为 %s" % (got_pnos, expected_pnos))
        print("可能原因：分批大小在断点续传之间被修改，或旧工作目录残留。请使用原分批大小重跑，或改用新工作目录。")
        release_runner_lock()
        sys.exit(1)
    try:
        structure_v1.validate_cross_page(merged_structured)
    except ValueError as exc:
        print("跨页标记一致性校验失败：%s" % exc)
        release_runner_lock()
        sys.exit(1)
    if until in ("structure", "2"):
        print("已运行至「结构化整理」阶段，停止。下一步：全书分析。\n"
              "查看产物：%s" % os.path.join(work, "02_structured", "structured_batch_*.json"))
        release_runner_lock()
        sys.exit(0)

    # 阶段3 全书分析
    glossary_path = os.path.join(work, "03_analysis", "glossary.md")
    if state.get("analyze"):
        print("[3/6] 全书分析：已存在，跳过")
        with open(glossary_path, encoding="utf-8-sig") as f:
            glossary = f.read().strip()
    else:
        print("[3/6] 全书分析（%d 页）..." % len(merged_structured["pages"]))
        try:
            glossary = call_analyze(merged_structured, api_key)
        except Exception as exc:
            error_path = os.path.join(work, "03_analysis", "analysis_error.log")
            ensure_dir(os.path.dirname(error_path))
            with open(error_path, "w", encoding="utf-8") as f:
                f.write(str(exc))
            print("全书分析调用失败：%s" % exc)
            print("流水线终止。请检查 API 地址、密钥与模型配置后重跑。错误详情：%s" % error_path)
            release_runner_lock()
            sys.exit(1)
        ensure_dir(os.path.dirname(glossary_path))
        with open(glossary_path, "w", encoding="utf-8") as f:
            f.write(glossary)
        state["analyze"] = True
        save_state(work, state)
    translate_v1.GLOSSARY = glossary
    if until in ("analyze", "3"):
        print("已运行至「全书分析」阶段，停止。下一步：翻译。\n"
              "查看产物：%s" % os.path.join(work, "03_analysis", "glossary.md"))
        release_runner_lock()
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
    failed = []
    failed_lock = threading.Lock()
    failed_path = os.path.join(work, "04_translated", "failed_batches.json")
    if os.path.exists(failed_path):
        os.remove(failed_path)
    for idx, bu in enumerate(batch_units):
        out = os.path.join(work, "04_translated", "translated_batch_%02d.json" % (idx + 1))
        translated_files.append(out)
        if state.get("translate_s%d_%d" % (BATCH_SIZE, idx)):
            print("[4/6] 翻译批次 %d：已存在，跳过" % (idx + 1))
            continue
        pending.append((idx, bu, out))
    if pending:
        workers = min(max_workers, len(pending))

        def translate_task(item):
            idx, bu, out = item
            partial_path = out[:-5] + ".partial.json"
            existing_units = []
            if os.path.exists(partial_path):
                try:
                    existing_units = load_json(partial_path).get("units", [])
                except (json.JSONDecodeError, ValueError):
                    existing_units = []
            existing_ids = {e.get("unit_id") for e in existing_units if isinstance(e, dict)}
            remaining = sum(1 for u in bu if u["unit_id"] not in existing_ids)
            if existing_units:
                print("[4/6] 翻译批次 %d：从断点恢复 %d 个单元，本次还需翻译 %d 个。"
                      % (idx + 1, len(existing_units), remaining))
            else:
                print("[4/6] 翻译批次 %d（%d 个单元）开始..." % (idx + 1, len(bu)))
            try:
                result_units, problems, hard_failures = call_translate(
                    bu, api_key, progress_path=partial_path, existing_units=existing_units)
            except Exception as exc:
                raise RuntimeError("翻译批次 %d 调用失败：%s" % (idx + 1, exc))
            if hard_failures:
                with failed_lock:
                    for hf in hard_failures:
                        failed.append({"batch": idx + 1, "unit_id": hf["unit_id"], "error": hf["error"]})
                print("[4/6] 翻译批次 %d 有 %d 个单元调用失败，本批不落盘；"
                      "已完成的单元保留在 %s，重跑只补失败单元。"
                      % (idx + 1, len(hard_failures), partial_path))
                return
            with state_lock:
                for p in problems:
                    issues.append({"page": "", "block": "", "kind": "translate", "detail": p})
            save_json(out, {"units": result_units})
            with state_lock:
                state["translate_s%d_%d" % (BATCH_SIZE, idx)] = True
                save_state(work, state)
            if os.path.exists(partial_path):
                try:
                    os.remove(partial_path)
                except OSError:
                    pass
            print("[4/6] 翻译批次 %d（%d 个单元）完成。" % (idx + 1, len(bu)))

        print("[4/6] 并发翻译 %d 个批次，并发数 %d ..." % (len(pending), workers))
        errors = run_parallel(translate_task, pending, workers)
        if errors:
            for msg in errors:
                print("  " + msg)
            release_runner_lock()
            sys.exit(1)
    if failed:
        save_json(failed_path, failed)
        print("翻译阶段有 %d 个单元调用失败，详情见：%s" % (len(failed), failed_path))
        for f in failed:
            print("  - 批次 %d 单元 %s：%s" % (f["batch"], f["unit_id"], f["error"]))
        print("请向用户确认后重跑同一命令，重跑只会补做失败单元，同批已完成的单元由 partial 文件保留。")
        release_runner_lock()
        sys.exit(1)
    missing = [f for f in translated_files if not os.path.exists(f)]
    if missing:
        print("缺少翻译批次文件（可能被删除或状态损坏）：%s" % ", ".join(missing))
        print("请删除 state.json 后重跑，或改用新工作目录。")
        release_runner_lock()
        sys.exit(1)
    merged_translated = merge_translated(translated_files)
    expected_units = [(u["blocks"], u["pages"], u["note"], u["kind"]) for u in units]
    got_units = [(u["blocks"], u["pages"], u["note"], u["kind"]) for u in merged_translated["units"]]
    if got_units != expected_units:
        print("翻译单元与结构化结果不一致（预期 %d 个单元，实际 %d 个单元），可能分批大小在续传之间被修改。请使用原分批大小重跑，或改用新工作目录。" % (
            len(expected_units), len(got_units)))
        release_runner_lock()
        sys.exit(1)
    print("  翻译单元总数：", len(merged_translated["units"]))
    if until in ("translate", "4"):
        print("已运行至「翻译」阶段，停止。下一步：排版合成。\n"
        "查看产物：%s" % os.path.join(work, "04_translated", "translated_batch_*.json"))
        release_runner_lock()
        sys.exit(0)
    for u in merged_translated["units"]:
        if not translate_v1.pb_ok(u["zh_text"], u["note"], u.get("pages")):
            issues.append({"page": "", "block": u["unit_id"], "kind": "translate",
                           "detail": "%s [PB] 数量或位置不合格" % u["unit_id"]})

    # 阶段5 排版
    block_text = render_v1.split_translated_units(merged_translated)
    epub_block_text = epub_v1.build_block_text(merged_translated)
    no_indent_ids = render_v1.continuation_block_ids(merged_translated)
    out_pdf = os.path.join(work, "05_rendered", name + "_zh.pdf")
    if state.get("render"):
        print("[5/6] 排版：已存在，跳过")
    else:
        print("[5/6] 排版 ...")
        size_by_page = {p["page_no"]: (p["page_size"][0], p["page_size"][1]) for p in extracted["pages"]}
        pages = render_v1.build_pages(merged_structured, block_text, no_indent_ids)
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
    out_epub = os.path.join(work, "05_rendered", name + "_zh.epub")
    if with_epub:
        if state.get("epub"):
            print("[5/6] EPUB：已存在，跳过")
        else:
            print("[5/6] EPUB ...")
            epub_v1.build_epub(merged_structured, epub_block_text, name, out_epub)
            state["epub"] = True
            save_state(work, state)
    print("  输出：", out_pdf)
    if with_epub:
        print("  输出：", out_epub)
    if until in ("render", "5"):
        print("已运行至「排版合成」阶段，停止。下一步：生成错误报告。\n"
              "查看产物：%s%s" % (out_pdf, "、" + out_epub if with_epub else ""))
        release_runner_lock()
        sys.exit(0)

    # 阶段6 错误报告
    print("[6/6] 错误报告 ...")
    report_path = generate_report(work, issues, extracted)
    print("  报告：", report_path)
    print("  问题总数：", len(issues))
    for it in issues[:20]:
        print("    -", it)
    release_runner_lock()


def _resolve_work_from_argv():
    pdf = sys.argv[1]
    name = os.path.splitext(os.path.basename(pdf))[0]
    work_root = TMP
    if "--work-dir" in sys.argv:
        idx = sys.argv.index("--work-dir") + 1
        if idx < len(sys.argv):
            work_root = sys.argv[idx]
    return os.path.join(work_root, "work_" + name)


def _run_cli():
    if len(sys.argv) < 2:
        main()
        return
    work = _resolve_work_from_argv()
    ensure_dir(work)
    log_path = os.path.join(work, "pipeline.log")
    with open(log_path, "a", encoding="utf-8") as logf:
        logf.write("\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        logf.flush()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _Tee(old_out, logf)
        sys.stderr = _Tee(old_err, logf)
        try:
            main()
        except SystemExit:
            raise
        except Exception:
            traceback.print_exc()
            sys.exit(1)
        finally:
            sys.stdout = old_out
            sys.stderr = old_err


if __name__ == "__main__":
    _run_cli()
