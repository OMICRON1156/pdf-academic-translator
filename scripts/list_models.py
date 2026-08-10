"""获取 OpenAI 兼容 API 的模型列表，逐行打印模型 ID，供交互时让用户选择模型。
用法: python scripts/list_models.py <api_base_url> [api_key]
兼容 https://host、https://host/v1 两种地址；也支持 Ollama 的 /api/tags。
"""
import json
import sys
import urllib.request


def _get_json(url, api_key, timeout=15):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_models(base_url, api_key="", timeout=15):
    base = base_url.strip().rstrip("/")
    candidates = []
    if base.endswith("/v1"):
        candidates.append(base + "/models")
        candidates.append(base[:-3] + "/models")
    else:
        candidates.append(base + "/models")
        candidates.append(base + "/v1/models")
    for url in candidates:
        try:
            data = _get_json(url, api_key, timeout)
            ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
            if ids:
                return ids
        except Exception:
            continue
    try:
        data = _get_json(base + "/api/tags", api_key, timeout)
        return [m.get("name") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/list_models.py <api_base_url> [api_key]")
        sys.exit(1)
    base = sys.argv[1]
    key = sys.argv[2] if len(sys.argv) > 2 else ""
    ids = fetch_models(base, key)
    if not ids:
        print("(未获取到模型列表，请检查 API 地址与密钥)", file=sys.stderr)
        sys.exit(1)
    for m in ids:
        print(m)
