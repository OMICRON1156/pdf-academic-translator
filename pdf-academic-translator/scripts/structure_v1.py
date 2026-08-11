"""阶段2：大模型批量整理——判断文本块性质 + 识别页码/空白页 + 轻度语义清理 + 跨页标记。
用法: python scripts/structure_v1.py [input_json] [output_json]
密钥从环境变量 DEEPSEEK_API_KEY 读取。
提示词从 prompts/structure_system.md 读取。
"""
import json
import os
import re
import sys
import urllib.request
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_client import chat

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "tmp", "sample_extracted.json")
OUTPUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(BASE, "tmp", "sample_structured_v3.json")

PROMPT_FILE = os.path.join(BASE, "prompts", "structure_system.md")
with open(PROMPT_FILE, encoding="utf-8-sig") as _f:
    SYSTEM_PROMPT = _f.read().strip()


def call_deepseek(messages, api_key, model=None):
    return chat(messages, api_key, model=model, temperature=0.1, timeout=400,
                response_format={"type": "json_object"})


def _matching_brace(text, start):
    """返回与 start 处 { 配对的 } 下标；无配对返回 -1。字符串内容与转义不计入。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _strip_trailing_commas(text):
    """删除字符串外、且后跟 } 或 ] 的逗号，避免污染字符串内容。"""
    out = []
    in_str = False
    esc = False
    n = len(text)
    for i, ch in enumerate(text):
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\n\r":
                j += 1
            if j < n and text[j] in "}]":
                continue
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def parse_json(content):
    if not isinstance(content, str):
        raise ValueError("模型返回内容不是字符串: %r" % (content,))
    text = content.strip()
    if text.startswith("\ufeff"):
        text = text[1:].strip()
    fallback = None
    best = None
    for m in re.finditer(r"\{", text):
        end = _matching_brace(text, m.start())
        if end == -1:
            continue
        candidate = text[m.start():end + 1]
        for raw in (candidate, _strip_trailing_commas(candidate)):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("pages"), list):
                if best is None or len(raw) > len(best[0]):
                    best = (raw, obj)
            if fallback is None:
                fallback = obj
            break
    if best is not None:
        return best[1]
    if fallback is not None:
        return fallback
    raise ValueError("模型输出中未找到完整的 JSON 对象")


ALLOWED_KINDS = {"page_header_footer", "heading", "body", "footnote", "caption", "noise"}
ALLOWED_NOTES = {"continues_to_next", "continuation_from_prev"}


def _normalize_note(note):
    """把 note 规范化为标记列表：空串或缺失为 []，字符串视为单元素，数组原样返回。"""
    if not note:
        return []
    if isinstance(note, str):
        return [note]
    return list(note)


def validate_structured(result, batch):
    """校验模型返回的结构化结果与输入批次一致；不满足时抛出 ValueError。"""
    if not isinstance(result, dict) or not isinstance(result.get("pages"), list):
        raise ValueError("结构化结果缺少 pages 数组")
    expected_by_page = {}
    expected_images = {}
    for p in batch["pages"]:
        expected_by_page[p["page_no"]] = {b["block_id"] for b in p.get("blocks", [])}
        expected_images[p["page_no"]] = bool(p.get("image_regions"))
    for p in result["pages"]:
        if not isinstance(p, dict):
            raise ValueError("结构化结果中存在非对象的页面项")
        pno = p.get("page_no")
        if not isinstance(pno, int) or isinstance(pno, bool):
            raise ValueError("page_no 必须为整数: %r" % (pno,))
        if "page_number" not in p:
            raise ValueError("第 %s 页缺少 page_number 字段" % pno)
        pn = p.get("page_number")
        if pn is not None and (not isinstance(pn, int) or isinstance(pn, bool)):
            raise ValueError("第 %s 页 page_number 必须为整数或 null" % pno)
        if not isinstance(p.get("is_blank"), bool):
            raise ValueError("第 %s 页 is_blank 必须为布尔值" % pno)
        blocks = p.get("blocks")
        if not isinstance(blocks, list):
            raise ValueError("第 %s 页 blocks 必须为数组" % pno)
        if p.get("is_blank") and blocks:
            raise ValueError("第 %s 页 is_blank 为 true 但包含文本块" % pno)
        if not p.get("is_blank") and not blocks and not expected_images.get(pno):
            raise ValueError("第 %s 页 is_blank 为 false 但没有任何文本块且无图像" % pno)
        ids = []
        for b in blocks:
            if not isinstance(b, dict):
                raise ValueError("第 %s 页 blocks 中存在非对象项" % pno)
            bid = b.get("block_id")
            if not isinstance(bid, str) or not bid:
                raise ValueError("第 %s 页存在缺失或非字符串的 block_id" % pno)
            ids.append(bid)
            if b.get("kind") not in ALLOWED_KINDS:
                raise ValueError("block %s 的 kind 不合法: %r" % (bid, b.get("kind")))
            if not isinstance(b.get("text"), str):
                raise ValueError("block %s 的 text 必须为字符串" % bid)
            if "note" not in b:
                raise ValueError("block %s 缺少 note 字段" % bid)
            note = _normalize_note(b.get("note"))
            if any(v not in ALLOWED_NOTES for v in note):
                raise ValueError("block %s 的 note 不合法: %r" % (bid, b.get("note")))
            if len(note) != len(set(note)):
                raise ValueError("block %s 的 note 存在重复取值: %r" % (bid, b.get("note")))
            if len(note) > 2:
                raise ValueError("block %s 的 note 取值超过两个: %r" % (bid, b.get("note")))
        expected = expected_by_page.get(pno)
        if expected is None:
            raise ValueError("输出包含输入中不存在的页号: %s" % pno)
        if set(ids) != expected:
            missing = expected - set(ids)
            extra = set(ids) - expected
            raise ValueError("第 %s 页 block_id 与输入不一致：缺失 %s，多余 %s" % (
                pno, sorted(missing) or "-", sorted(extra) or "-"))
        if len(ids) != len(set(ids)):
            raise ValueError("第 %s 页存在重复的 block_id" % pno)
        body_idx = [i for i, b in enumerate(blocks) if b.get("kind") == "body"]
        cont_prev = [i for i, b in enumerate(blocks) if "continuation_from_prev" in _normalize_note(b.get("note"))]
        cont_next = [i for i, b in enumerate(blocks) if "continues_to_next" in _normalize_note(b.get("note"))]
        if len(cont_prev) > 1:
            raise ValueError("第 %s 页出现多个 continuation_from_prev" % pno)
        if len(cont_next) > 1:
            raise ValueError("第 %s 页出现多个 continues_to_next" % pno)
        if cont_prev and (not body_idx or cont_prev[0] != body_idx[0]):
            raise ValueError("第 %s 页 continuation_from_prev 不是页首正文块" % pno)
        if cont_next and (not body_idx or cont_next[0] != body_idx[-1]):
            raise ValueError("第 %s 页 continues_to_next 不是页尾正文块" % pno)
        if len(body_idx) != 1:
            for i, b in enumerate(blocks):
                if len(_normalize_note(b.get("note"))) == 2:
                    raise ValueError("第 %s 页块 %s 使用组合跨页标记，但该页不止一个正文块" % (pno, b.get("block_id")))
    pnos = [p.get("page_no") for p in result["pages"]]
    if len(pnos) != len(set(pnos)):
        dup = sorted({p for p in pnos if pnos.count(p) > 1})
        raise ValueError("输出存在重复的 page_no: %s" % dup)
    expected_pnos = set(expected_by_page)
    if set(pnos) != expected_pnos:
        missing = sorted(expected_pnos - set(pnos))
        extra = sorted(set(pnos) - expected_pnos)
        raise ValueError("输出页号与输入不一致：缺失 %s，多余 %s" % (missing or "-", extra or "-"))


def validate_cross_page(merged):
    """全局校验跨页标记：待续状态机，遇到 continues_to_next 挂起，直到后续某页出现 continuation_from_prev 才消费。
    配对可跨页，但中间的隔页不得包含正文，防止误配；组合标记让同一单元可以连续跨多页。"""
    by_page = {p["page_no"]: p for p in merged["pages"]}
    open_pages = []
    for pno in sorted(by_page):
        p = by_page[pno]
        blocks = p.get("blocks", [])
        cont_prev = [b for b in blocks if "continuation_from_prev" in _normalize_note(b.get("note"))]
        cont_next = [b for b in blocks if "continues_to_next" in _normalize_note(b.get("note"))]
        if len(cont_prev) > 1:
            raise ValueError("第 %d 页出现多个 continuation_from_prev" % pno)
        if len(cont_next) > 1:
            raise ValueError("第 %d 页出现多个 continues_to_next" % pno)
        if cont_prev:
            if not open_pages:
                raise ValueError("第 %d 页有 continuation_from_prev，但没有待承接的续页段落" % pno)
            start = open_pages.pop(0)
            for mid in range(start + 1, pno):
                if any(b.get("kind") == "body" for b in by_page[mid].get("blocks", [])):
                    raise ValueError("第 %d 页与第 %d 页之间的第 %d 页包含正文，无法配对" % (start, pno, mid))
        if cont_next:
            open_pages.append(pno)
    if open_pages:
        raise ValueError("以下页面有 continues_to_next 但没有后续承接：%s" % sorted(open_pages))


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY 环境变量")
        sys.exit(1)
    with open(INPUT, encoding="utf-8-sig") as f:
        raw = json.load(f)
    payload = json.dumps(raw, ensure_ascii=False)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "以下是待整理的渲染数据 JSON，请按要求输出整理结果：\n" + payload},
    ]
    print("调用 DeepSeek ...")
    content = call_deepseek(messages, api_key)
    out = parse_json(content)
    validate_structured(out, raw)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("written:", OUTPUT)
    for p in out["pages"]:
        print("页%d 页码=%s 空白=%s" % (p.get("page_no"), p.get("page_number"), p.get("is_blank")))
        for b in p.get("blocks", []):
            note = (" note=" + b["note"]) if b.get("note") else ""
            print("   %s kind=%s%s" % (b["block_id"], b["kind"], note))


if __name__ == "__main__":
    main()
