"""Codex's local Responses relay, adapted to the workbench's model loop."""
import json
import os
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from audit import emit
from model_usage import observe_request


def _connection():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8-sig"))
    provider = config["model_providers"][config["model_provider"]]
    base = provider["base_url"].rstrip("/")
    if provider.get("wire_api") != "responses" or urlsplit(base).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Codex 当前未配置本机 Responses 中转")
    key = json.loads((home / "auth.json").read_text(encoding="utf-8-sig"))["OPENAI_API_KEY"]
    return base, key


def chat(messages, model, effort, tools, timeout, max_tokens):
    base, key = _connection()
    inputs = []
    for message in messages:
        if message.get("_response_items"):
            inputs.extend(message["_response_items"])
        elif message["role"] == "tool":
            inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                           "output": message["content"]})
        else:
            inputs.append({"role": message["role"], "content": message["content"]})
    payload = {"model": model, "input": inputs, "reasoning": {"effort": effort},
               "store": False, "stream": False, "include": ["reasoning.encrypted_content"]}
    if tools:
        payload["tools"] = [{"type": "function", **tool["function"]} for tool in tools]
    if max_tokens:
        payload["max_output_tokens"] = max_tokens
    request = urllib.request.Request(base + "/responses", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    emit("model.request", payload)
    try:
        with observe_request() as call:
            with urllib.request.urlopen(request, timeout=max(timeout, 150)) as response:
                body = json.load(response)
            usage = body.get("usage") or {}
            hit = (usage.get("input_tokens_details") or {}).get("cached_tokens")
            count = usage.get("input_tokens")
            call.update(prompt_tokens=count, completion_tokens=usage.get("output_tokens"),
                        reasoning_tokens=(usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
                        prompt_cache_hit_tokens=hit,
                        prompt_cache_miss_tokens=count-hit if count is not None and hit is not None else None)
            if body.get("error") or body.get("status") not in {"completed", "incomplete"}:
                raise ValueError(str(body.get("error") or body.get("status") or "中转响应缺少状态"))
        emit("model.response", {"body": body, "usage": call}, "llm")
    except Exception as exc:
        emit("model.error", {"error": str(exc), "usage": call})
        raise
    output = body.get("output") or []
    text = "".join(part["text"] for item in output if item["type"] == "message"
                   for part in item["content"] if part["type"] == "output_text")
    calls = [{"id": item["call_id"], "type": "function",
              "function": {"name": item["name"], "arguments": item["arguments"]}}
             for item in output if item["type"] == "function_call"]
    return {"content": text, "tool_calls": calls, "_response_items": output}, {
        **call, "model": model, "effort": effort, "latency_s": round(call["request_seconds"], 2),
        "finish_reason": "length" if body.get("status") == "incomplete" else "stop"}
