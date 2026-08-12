"""OpenAI 兼容 LLM 客户端：支持自定义 base_url / api_key / model。
base_url 与 model 可通过环境变量 DEEPSEEK_BASE_URL / DEEPSEEK_MODEL 覆盖，
便于图形界面在启动流水线前注入配置。
"""
import http.client
import json
import os
import socket
import threading
import urllib.error
import urllib.parse

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def _open_connection(url, timeout):
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname
    port = parts.port or (443 if scheme == "https" else 80)
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _post_and_parse(conn, url, path, body, headers):
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    if resp.status >= 400:
        raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, resp)
    return json.loads(data.decode("utf-8"))


def _request_with_deadline(url, body, headers, timeout):
    """在 timeout 秒内完成请求；超时主动关闭连接，让服务端停止输出。"""
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    holder = {}

    def worker():
        conn = None
        try:
            conn = _open_connection(url, timeout)
            holder["conn"] = conn
            holder["data"] = _post_and_parse(conn, url, path, body, headers)
        except Exception as exc:
            if isinstance(exc, (urllib.error.HTTPError, urllib.error.URLError)):
                holder["exc"] = exc
            else:
                holder["exc"] = urllib.error.URLError(exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    thread = threading.Thread(target=worker, name="llm-http", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        conn = holder.get("conn")
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        raise TimeoutError("请求超过 %d 秒总时长限制" % timeout)
    if "exc" in holder:
        raise holder["exc"]
    return holder["data"]


def is_retryable_error(exc):
    """判断异常是否值得重试：仅超时、429 限流与 5xx 服务端错误。"""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (TimeoutError, socket.timeout))
    return False


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


def chat(messages, api_key, model=None, temperature=0.1, timeout=400, response_format=None):
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
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    }
    data = _request_with_deadline(url, body, headers, timeout)
    return data["choices"][0]["message"]["content"]
