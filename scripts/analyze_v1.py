"""阶段3：全书分析——合并结构化文件，提炼小节中心意思与核心概念对照翻译表，输出 .md。
用法: python scripts/analyze_v1.py [input_json ...]
若未传输入文件，默认使用 tmp/sample_structured_v3.json。
密钥从环境变量 DEEPSEEK_API_KEY 读取。
"""
import json
import os
import sys
import urllib.request
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_client import chat

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INPUT = os.path.join(BASE, "tmp", "sample_structured_v3.json")
PROMPT_FILE = os.path.join(BASE, "prompts", "analysis_system.md")
OUTPUT = os.path.join(BASE, "tmp", "glossary.md")

with open(PROMPT_FILE, encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read().strip()


def merge_structured(files):
    """合并多个分批 structured JSON 为一个（页数组拼接）。"""
    pages = []
    for f in files:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
        pages.extend(data["pages"])
    return {"total_pages": len(pages), "pages": pages}


def call_deepseek(messages, api_key, model=None):
    return chat(messages, api_key, model=model, temperature=0.2, timeout=300)


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY 环境变量")
        sys.exit(1)
    files = sys.argv[1:] if len(sys.argv) > 1 else [DEFAULT_INPUT]
    structured = merge_structured(files)
    print("合并输入文件 %d 个，共 %d 页" % (len(files), structured["total_pages"]))
    payload = json.dumps(structured, ensure_ascii=False)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "以下是全书结构化文本，请按要求输出全书分析 Markdown：\n" + payload},
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
