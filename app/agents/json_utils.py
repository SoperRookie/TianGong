"""LLM 输出的 JSON 提取与解析。"""

import json
import re

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMOutputError(ValueError):
    pass


def extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象：优先取代码块，其次取首个花括号平衡段。"""
    candidates = [m.strip() for m in _FENCE_RE.findall(text)]
    candidates.append(text.strip())

    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise LLMOutputError(f"无法从模型输出中解析 JSON：{text[:200]}...")
