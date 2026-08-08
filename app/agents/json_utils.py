"""LLM 输出的 JSON 提取与解析。"""

import json
import re

from loguru import logger

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMOutputError(ValueError):
    pass


def extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象：优先取代码块，其次取首个花括号平衡段。

    整体解析失败时逐对象抢救（模型长输出偶发 token 跳漏产生局部畸形 JSON）：
    只丢弃损坏的元素，其余保留——缺失用例由评审环节查漏后定点修正补回。
    """
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

    salvaged = _salvage(text)
    if salvaged is not None:
        return salvaged
    raise LLMOutputError(f"无法从模型输出中解析 JSON：{text[:200]}...")


def _salvage(text: str) -> dict | None:
    """局部畸形输出的逐对象抢救：扫描全文提取所有可完整解析的 JSON 对象，
    按特征键识别为用例（case_id+title）或拆解模块（module+points），损坏元素丢弃。"""
    objects = _scan_objects(text)
    cases = [o for o in objects if "case_id" in o and "title" in o]
    if cases:
        logger.warning(
            "模型输出 JSON 局部畸形（{} 字），抢救出 {} 条用例对象，损坏部分交由评审查漏补回",
            len(text), len(cases),
        )
        return {"cases": cases}
    modules = [o for o in objects if "module" in o and "points" in o]
    if modules:
        logger.warning("模型输出 JSON 局部畸形（{} 字），抢救出 {} 个拆解模块", len(text), len(modules))
        return {"modules": modules, "blind_spots": []}
    return None


def _scan_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    i = 0
    while True:
        start = text.find("{", i)
        if start == -1:
            return objects
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            i = start + 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
            i = end
        else:
            i = start + 1
