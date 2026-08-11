"""阶段4：翻译——按逻辑段落组织翻译单元，跨页段落整体翻译并输出 [PB] 换页标签。
输入：整理结果 JSON（sample_structured_v3.json 结构）+ 可选术语表 tmp/glossary.md
输出：翻译结果 JSON（unit_id、blocks、kind、pages、zh_text），带单元级 partial 断点
用法: python scripts/translate_v1.py [input_json] [output_json]
密钥从环境变量 DEEPSEEK_API_KEY 读取。
提示词从 prompts/translate_system.md 读取，术语表从 tmp/glossary.md 读取。
"""
import json
import os
import sys
import time
import html
import urllib.request
import sys as _sys
from collections import deque
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_client import chat, is_retryable_error

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "tmp", "sample_structured_v3.json")
OUTPUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(BASE, "tmp", "sample_translated.json")

PROMPT_FILE = os.path.join(BASE, "prompts", "translate_system.md")
with open(PROMPT_FILE, encoding="utf-8-sig") as _f:
    SYSTEM_PROMPT = _f.read().strip()

GLOSSARY_FILE = os.path.join(BASE, "tmp", "glossary.md")
GLOSSARY = ""
if os.path.exists(GLOSSARY_FILE):
    with open(GLOSSARY_FILE, encoding="utf-8-sig") as _f:
        GLOSSARY = _f.read().strip()

OUTPUT_RATIO_LIMIT = 1.5
OUTPUT_MIN_CHARS = 100


def call_deepseek(messages, api_key, model=None):
    return chat(messages, api_key, model=model, temperature=0.3, timeout=180)


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def output_too_long(zh_text, src_text):
    """译文长度超过 max(1.5×原文, 100) 时视为异常重复输出。"""
    limit = max(OUTPUT_MIN_CHARS, int(OUTPUT_RATIO_LIMIT * len(src_text or "")))
    return len(zh_text or "") > limit


END_PUNCT = set(".?!;:。！？：")


def _normalize_note(note):
    """把 note 规范化为标记列表：空串或缺失为 []，字符串视为单元素，数组原样返回。"""
    if not note:
        return []
    if isinstance(note, str):
        return [note]
    return list(note)


def _is_sentence_end(text):
    t = text.rstrip()
    return bool(t) and t[-1] in END_PUNCT


def _starts_new_sentence(text):
    t = text.lstrip()
    return bool(t) and (t[0].isupper() or t[0].isdigit())


def build_units(structured):
    """把整理结果组织成翻译单元：
    1) 同页内语义连续的碎片块合并（前块不以句末标点结尾、后块不以大写或编号开头）；
    2) 跨页段落用待续队列串联：continuation_from_prev 消费最早的待续单元，continues_to_next 让单元继续挂起；
       组合标记（同一块既是承接又是续页）让同一单元可以连续跨多页。"""
    items = []
    for p in structured["pages"]:
        blocks = [b for b in p["blocks"] if b["kind"] not in ("page_header_footer", "noise")]
        merged = []
        for b in blocks:
            note = _normalize_note(b.get("note", ""))
            if (merged and merged[-1]["kind"] == b["kind"] and merged[-1]["kind"] in ("body", "quote")
                    and not _is_sentence_end(merged[-1]["text"]) and not _starts_new_sentence(b["text"])):
                merged[-1]["text"] = merged[-1]["text"] + " " + b["text"]
                merged[-1]["block_id"] = merged[-1]["block_id"] + "+" + b["block_id"]
                merged[-1]["note"] = sorted(set(merged[-1]["note"] + note))
            else:
                merged.append({
                    "page_no": p["page_no"],
                    "block_id": b["block_id"],
                    "kind": b["kind"],
                    "text": b["text"],
                    "note": note,
                })
        items.extend(merged)
    units = []
    used = set()
    open_units = deque()
    for it in items:
        if it["block_id"] in used:
            continue
        note = it["note"]
        if "continuation_from_prev" in note:
            if not open_units:
                raise ValueError("第 %d 页块 %s 有 continuation_from_prev 但没有待承接的续页段落"
                                 % (it["page_no"], it["block_id"]))
            unit = open_units.popleft()
            unit["blocks"].append(it["block_id"])
            unit["pages"].append(it["page_no"])
            unit["text"] = unit["text"] + "\n[PAGEBREAK]\n" + it["text"]
            used.add(it["block_id"])
            if "continues_to_next" in note:
                open_units.append(unit)
            continue
        unit = {
            "unit_id": "u%03d" % (len(units) + 1),
            "blocks": [it["block_id"]],
            "kind": it["kind"],
            "pages": [it["page_no"]],
            "text": it["text"],
            "note": "cross_page" if "continues_to_next" in note else "",
        }
        used.add(it["block_id"])
        if "continues_to_next" in note:
            open_units.append(unit)
        units.append(unit)
    return units


def pb_ok(zh, note, pages=None):
    """跨页单元 [PB] 数量必须等于跨页数减一且位于段落中间；普通单元不得出现 [PB]。"""
    if note != "cross_page":
        return "[PB]" not in zh and "[PAGEBREAK]" not in zh
    expected = (len(pages) - 1) if pages else 1
    if zh.count("[PB]") != expected:
        return False
    s = zh.strip()
    if s.startswith("[PB]") or s.endswith("[PB]"):
        return False
    if "[PB][PB]" in zh:
        return False
    return True


def build_messages(unit, hint=""):
    if unit["note"] == "cross_page":
        pos = "第 %d 页至第 %d 页（跨页段落）" % (unit["pages"][0], unit["pages"][-1])
        tip = "原文中的 [PAGEBREAK] 表示换页位置，译文在对应自然断点处各输出一个 [PB]；[PAGEBREAK] 有多少个，[PB] 就输出多少个，顺序对应各页，不要多标或漏标。"
    else:
        pos = "第 %d 页" % unit["pages"][0]
        tip = "该段落不跨页，不要输出 [PB] 标签。"
    glossary_block = ""
    if GLOSSARY:
        glossary_block = "\n【全书术语表，以下译法必须遵循】\n" + GLOSSARY + "\n"
    extra = ""
    if hint:
        extra = "\n注意：" + hint + "\n"
    user = (
        "待翻译段落（%s）：\n"
        "类型：%s\n\n"
        "%s\n\n"
        "%s\n\n"
        "%s%s请输出中文定稿译文。" % (pos, unit["kind"], unit["text"], tip, glossary_block, extra)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def translate_units(units, api_key, progress_path=None, existing_units=None):
    """翻译一组单元；每成功一个写入 partial，硬失败时停止本批。"""
    result_by_id = {}
    if existing_units:
        for item in existing_units:
            if isinstance(item, dict) and item.get("unit_id"):
                result_by_id[item["unit_id"]] = item
    problems = []
    hard_failures = []

    def persist_progress():
        if not progress_path:
            return
        ordered = [result_by_id[x["unit_id"]] for x in units if x["unit_id"] in result_by_id]
        _save_json(progress_path, {"units": ordered})

    for u in units:
        if u["unit_id"] in result_by_id:
            continue
        content = None
        raw = None
        hard_fail = False
        for attempt in range(2):
            try:
                hint = ""
                if attempt > 0 and u["note"] == "cross_page":
                    hint = ("每个 [PB] 必须位于段落中间对应原文 [PAGEBREAK] 的位置，"
                            "绝不能放在段落开头或末尾；[PB] 数量必须与 [PAGEBREAK] 数量一致。")
                raw = call_deepseek(build_messages(u, hint), api_key).strip()
                zh = html.unescape(raw)
                if output_too_long(zh, u["text"]):
                    problems.append("%s 输出长度异常（%d 字符），本批停止"
                                    % (u["unit_id"], len(zh)))
                    hard_failures.append({"unit_id": u["unit_id"],
                                          "error": "输出长度异常：%d 字符" % len(zh)})
                    print("  %s 输出长度异常（%d 字符），本批停止"
                          % (u["unit_id"], len(zh)))
                    persist_progress()
                    result_units = [result_by_id[x["unit_id"]] for x in units
                                    if x["unit_id"] in result_by_id]
                    return result_units, problems, hard_failures
                if pb_ok(zh, u["note"], u.get("pages")):
                    content = zh
                    break
                print("  %s [PB] 校验不通过，重试 %d" % (u["unit_id"], attempt + 1))
            except Exception as exc:
                if is_retryable_error(exc) and attempt == 0:
                    print("  %s 第 %d 次调用失败（%s），自动重试..."
                          % (u["unit_id"], attempt + 1, exc))
                    time.sleep(2)
                    continue
                hard_fail = True
                if is_retryable_error(exc):
                    problems.append("%s 调用失败（重试次数用尽）：%s"
                                    % (u["unit_id"], exc))
                    print("  %s 重试次数用尽：%s" % (u["unit_id"], exc))
                else:
                    problems.append("%s 调用失败（确定性错误，不重试）：%s"
                                    % (u["unit_id"], exc))
                    print("  %s 调用失败，不重试：%s" % (u["unit_id"], exc))
                hard_failures.append({"unit_id": u["unit_id"], "error": str(exc)})
                break
        if content is None and raw is not None:
            content = html.unescape(raw)
            problems.append("%s [PB] 数量或位置不合格，回退整段到第一块" % u["unit_id"])
        elif content is None and not hard_fail:
            problems.append("%s 翻译失败（重试次数用尽）" % u["unit_id"])
            hard_failures.append({"unit_id": u["unit_id"], "error": "翻译失败"})
            hard_fail = True
        if hard_fail:
            persist_progress()
            result_units = [result_by_id[x["unit_id"]] for x in units
                            if x["unit_id"] in result_by_id]
            return result_units, problems, hard_failures
        zh = content if content is not None else ""
        result_by_id[u["unit_id"]] = {
            "unit_id": u["unit_id"],
            "blocks": u["blocks"],
            "kind": u["kind"],
            "pages": u["pages"],
            "note": u["note"],
            "zh_text": zh,
        }
        persist_progress()
    result_units = [result_by_id[x["unit_id"]] for x in units
                    if x["unit_id"] in result_by_id]
    return result_units, problems, hard_failures


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY 环境变量")
        sys.exit(1)
    with open(INPUT, encoding="utf-8-sig") as f:
        structured = json.load(f)
    units = build_units(structured)
    print("=== 翻译单元清单 ===")
    for u in units:
        print("  %s kind=%-6s pages=%s blocks=%s note=%s" % (
            u["unit_id"], u["kind"], u["pages"], u["blocks"], u["note"]))

    partial_path = OUTPUT[:-5] + ".partial.json" if OUTPUT.endswith(".json") else OUTPUT + ".partial.json"
    failed_path = OUTPUT[:-5] + ".failed.json" if OUTPUT.endswith(".json") else OUTPUT + ".failed.json"
    if os.path.exists(failed_path):
        os.remove(failed_path)
    existing_units = []
    if os.path.exists(partial_path):
        try:
            existing_units = json.load(open(partial_path, encoding="utf-8-sig")).get("units", [])
        except (json.JSONDecodeError, ValueError):
            existing_units = []
    if existing_units:
        print("从断点恢复 %d 个单元" % len(existing_units))
    result_units, problems, hard_failures = translate_units(
        units, api_key, progress_path=partial_path, existing_units=existing_units)
    if hard_failures:
        _save_json(failed_path, hard_failures)
        print("partial:", partial_path)
        print("failed:", failed_path)
        for hf in hard_failures:
            print("  - %s: %s" % (hf["unit_id"], hf["error"]))
        if problems:
            print("=== 问题 ===")
            for p in problems:
                print(" -", p)
        sys.exit(1)
    _save_json(OUTPUT, {"units": result_units})
    if os.path.exists(partial_path):
        try:
            os.remove(partial_path)
        except OSError:
            pass
    print("written:", OUTPUT)
    if problems:
        print("=== 问题 ===")
        for p in problems:
            print(" -", p)
    else:
        print("=== 结构检查通过：所有单元 [PB] 数量正确，无 [PAGEBREAK] 残留 ===")


if __name__ == "__main__":
    main()
