"""OpenAI 兼容 LLM 客户端：支持自定义 base_url / api_key / model。
base_url 与 model 可通过环境变量 DEEPSEEK_BASE_URL / DEEPSEEK_MODEL 覆盖，
便于图形界面在启动流水线前注入配置。
"""
import json
import os
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def resolve_chat_url(base_url=None):
    """把用户填写的 API 地址规范化为 /chat/completions 端点。
    兼容三种写法：https://host、https://host/v1、https://host/chat/completions。
    """
    base = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/chat/completions"


def chat(messages, api_key, model=None, temperature=0.1, timeout=180, response_format=None):
    """调用聊天补全接口，返回 assistant 消息文本。"""
    model = model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
    url = resolve_chat_url()
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]
