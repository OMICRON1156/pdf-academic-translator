"""阶段4：翻译——按逻辑段落组织翻译单元，跨页段落整体翻译并输出 [PB] 换页标签。
输入：整理结果 JSON（sample_structured_v3.json 结构）+ 可选术语表 tmp/glossary.md
输出：翻译结果 JSON（unit_id、blocks、kind、pages、zh_text）
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
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_client import chat

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "tmp", "sample_structured_v3.json")
OUTPUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(BASE, "tmp", "sample_translated.json")

PROMPT_FILE = os.path.join(BASE, "prompts", "translate_system.md")
with open(PROMPT_FILE, encoding="utf-8") as _f:
    SYSTEM_PROMPT = _f.read().strip()

GLOSSARY_FILE = os.path.join(BASE, "tmp", "glossary.md")
GLOSSARY = ""
if os.path.exists(GLOSSARY_FILE):
    with open(GLOSSARY_FILE, encoding="utf-8") as _f:
        GLOSSARY = _f.read().strip()


def call_deepseek(messages, api_key, model=None):
    return chat(messages, api_key, model=model, temperature=0.3, timeout=180)


END_PUNCT = set(".?!;:。！？：")


def _is_sentence_end(text):
    t = text.rstrip()
    return bool(t) and t[-1] in END_PUNCT


def _starts_new_sentence(text):
    t = text.lstrip()
    return bool(t) and (t[0].isupper() or t[0].isdigit())


def build_units(structured):
    """把整理结果组织成翻译单元：
    1) 同页内语义连续的碎片块合并（前块不以句末标点结尾、后块不以大写或编号开头）；
    2) 跨页段落（continues_to_next + 下一页 continuation_from_prev）合并为一个单元。"""
    items = []
    for p in structured["pages"]:
        blocks = [b for b in p["blocks"] if b["kind"] not in ("page_header_footer", "noise")]
        merged = []
        for b in blocks:
            note = b.get("note", "")
            if (merged and merged[-1]["kind"] == b["kind"] and merged[-1]["kind"] in ("body", "quote")
                    and not _is_sentence_end(merged[-1]["text"]) and not _starts_new_sentence(b["text"])):
                merged[-1]["text"] = merged[-1]["text"] + " " + b["text"]
                merged[-1]["block_id"] = merged[-1]["block_id"] + "+" + b["block_id"]
                if note and not merged[-1]["note"]:
                    merged[-1]["note"] = note
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
    for it in items:
        if it["block_id"] in used:
            continue
        unit = {
            "unit_id": "u%03d" % (len(units) + 1),
            "blocks": [it["block_id"]],
            "kind": it["kind"],
            "pages": [it["page_no"]],
            "text": it["text"],
            "note": it["note"],
        }
        used.add(it["block_id"])
        if it["note"] == "continues_to_next":
            for cand in items:
                if (cand["block_id"] not in used
                        and cand["page_no"] == it["page_no"] + 1
                        and cand["note"] == "continuation_from_prev"):
                    unit["blocks"].append(cand["block_id"])
                    unit["pages"].append(cand["page_no"])
                    unit["text"] = it["text"] + "\n[PAGEBREAK]\n" + cand["text"]
                    unit["note"] = "cross_page"
                    used.add(cand["block_id"])
                    break
        units.append(unit)
    return units


def pb_ok(zh, note):
    """跨页单元 [PB] 必须恰好 1 个且位于段落中间；普通单元不得出现 [PB]。"""
    if note != "cross_page":
        return "[PB]" not in zh and "[PAGEBREAK]" not in zh
    if zh.count("[PB]") != 1:
        return False
    s = zh.strip()
    return not s.startswith("[PB]") and not s.endswith("[PB]")


def build_messages(unit, hint=""):
    if unit["note"] == "cross_page":
        pos = "第 %d 页至第 %d 页（跨页段落）" % (unit["pages"][0], unit["pages"][-1])
        tip = "原文中的 [PAGEBREAK] 是源 PDF 的换页位置，请在译文对应自然断点处输出一个 [PB] 标签。"
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


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY 环境变量")
        sys.exit(1)
    with open(INPUT, encoding="utf-8") as f:
        structured = json.load(f)
    units = build_units(structured)
    print("=== 翻译单元清单 ===")
    for u in units:
        print("  %s kind=%-6s pages=%s blocks=%s note=%s" % (
            u["unit_id"], u["kind"], u["pages"], u["blocks"], u["note"]))

    result = {"units": []}
    problems = []
    for u in units:
        content = None
        for attempt in range(3):
            try:
                print("翻译 %s ..." % u["unit_id"])
                content = call_deepseek(build_messages(u), api_key).strip()
                break
            except Exception as exc:
                print("  尝试 %d 失败: %s" % (attempt + 1, exc))
                time.sleep(2)
        if content is None:
            problems.append("%s 翻译失败" % u["unit_id"])
            zh = ""
        else:
            zh = html.unescape(content)
            expect_pb = 1 if u["note"] == "cross_page" else 0
            n_pb = zh.count("[PB]")
            n_src = zh.count("[PAGEBREAK]")
            if n_pb != expect_pb:
                problems.append("%s [PB] 数量=%d 期望=%d" % (u["unit_id"], n_pb, expect_pb))
            if n_src > 0:
                problems.append("%s 残留 [PAGEBREAK]" % u["unit_id"])
        result["units"].append({
            "unit_id": u["unit_id"],
            "blocks": u["blocks"],
            "kind": u["kind"],
            "pages": u["pages"],
            "note": u["note"],
            "zh_text": zh,
        })

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("written:", OUTPUT)
    if problems:
        print("=== 问题 ===")
        for p in problems:
            print(" -", p)
    else:
        print("=== 结构检查通过：所有单元 [PB] 数量正确，无 [PAGEBREAK] 残留 ===")


if __name__ == "__main__":
    main()
