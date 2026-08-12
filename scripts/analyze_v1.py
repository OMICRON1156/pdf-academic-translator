"""阶段3：全书分析——合并结构化文件，提炼小节中心意思与核心概念对照翻译表，输出 .md。
用法: python scripts/analyze_v1.py [input_json ...]
若未传输入文件，默认使用 tmp/sample_structured_v3.json。
密钥从环境变量 DEEPSEEK_API_KEY 读取。
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
DEFAULT_INPUT = os.path.join(BASE, "tmp", "sample_structured_v3.json")
PROMPT_FILE = os.path.join(BASE, "prompts", "analysis_system.md")
OUTPUT = os.path.join(BASE, "tmp", "glossary.md")

with open(PROMPT_FILE, encoding="utf-8-sig") as f:
    SYSTEM_PROMPT = f.read().strip()


def merge_structured(files):
    """合并多个分批 structured JSON 为一个（页数组拼接）。"""
    pages = []
    for f in files:
        with open(f, encoding="utf-8-sig") as fp:
            data = json.load(fp)
        pages.extend(data["pages"])
    pages.sort(key=lambda p: p["page_no"])
    return {"total_pages": len(pages), "pages": pages}


def _normalize_note(note):
    if not note:
        return []
    if isinstance(note, str):
        return [note]
    return list(note)


def _heading_mark(text):
    if re.match(r"^(chapter|part|第[0-9一二三四五六七八九十百千万]+[章部卷])",
                text.strip(), re.I):
        return "#"
    return "##"


def _join_parts(parts):
    text = " ".join(part.strip() for part in parts if part.strip())
    return re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", text)


def build_markdown(structured):
    """只保留标题与正文，合并跨页段落，输出供全书分析使用的 Markdown。"""
    pages = sorted(structured["pages"], key=lambda p: p["page_no"])
    out = []
    open_parts = None

    def flush():
        nonlocal open_parts
        if not open_parts:
            return
        text = _join_parts(open_parts)
        if text:
            out.append(text)
        open_parts = None

    for page in pages:
        for block in page.get("blocks", []):
            kind = block.get("kind")
            if kind not in ("heading", "body"):
                continue
            text = (block.get("text") or "").strip()
            if not text:
                continue
            if kind == "heading":
                flush()
                out.append("%s %s" % (_heading_mark(text), text))
                continue
            note = _normalize_note(block.get("note"))
            is_prev = "continuation_from_prev" in note
            is_next = "continues_to_next" in note
            if open_parts is not None and not is_prev:
                flush()
            if open_parts is None:
                open_parts = []
            open_parts.append(text)
            if not is_next:
                flush()
    flush()
    return "\n\n".join(out)


def call_deepseek(messages, api_key, model=None):
    return chat(messages, api_key, model=model, temperature=0.2, timeout=400)


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY 环境变量")
        sys.exit(1)
    files = sys.argv[1:] if len(sys.argv) > 1 else [DEFAULT_INPUT]
    structured = merge_structured(files)
    print("合并输入文件 %d 个，共 %d 页" % (len(files), structured["total_pages"]))
    payload = build_markdown(structured)
    print("Markdown 载荷字符数：%d" % len(payload))
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "以下是全书正文 Markdown，请按要求输出全书分析 Markdown：\n" + payload},
    ]
    print("调用 DeepSeek 全书分析 ...")
    content = call_deepseek(messages, api_key).strip()
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(content)
    print("written:", OUTPUT)
    print("--- 输出预览 ---")
    print(content[:1200])


if __name__ == "__main__":
    main()
