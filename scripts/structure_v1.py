"""阶段2：大模型批量整理——判断文本块性质 + 识别页码/空白页 + 轻度语义清理 + 跨页标记。
用法: python scripts/structure_v1.py [input_json] [output_json]
密钥从环境变量 DEEPSEEK_API_KEY 读取。
提示词从 prompts/structure_system.md 读取。
"""
import json
import os
import sys
import urllib.request
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_client import chat

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "tmp", "sample_extracted.json")
OUTPUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(BASE, "tmp", "sample_structured_v3.json")

PROMPT_FILE = os.path.join(BASE, "prompts", "structure_system.md")
with open(PROMPT_FILE, encoding="utf-8") as _f:
    SYSTEM_PROMPT = _f.read().strip()


def call_deepseek(messages, api_key, model=None):
    return chat(messages, api_key, model=model, temperature=0.1, timeout=180,
                response_format={"type": "json_object"})


def parse_json(content):
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return json.loads(content)


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY 环境变量")
        sys.exit(1)
    with open(INPUT, encoding="utf-8") as f:
        raw = json.load(f)
    payload = json.dumps(raw, ensure_ascii=False)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "以下是待整理的渲染数据 JSON，请按要求输出整理结果：\n" + payload},
    ]
    print("调用 DeepSeek ...")
    content = call_deepseek(messages, api_key)
    out = parse_json(content)
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
