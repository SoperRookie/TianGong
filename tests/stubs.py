"""测试公用桩件。"""

import json

from app.llm.client import ChatResult
from app.llm.schemas import UsageInfo


class StubLLM:
    """按调用顺序返回预置内容，兼容 LLMClient.chat 接口。"""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def chat(self, messages, model=None, require_vision=False, **overrides):
        from app.llm.calllog import record_call

        self.calls.append({"messages": messages, "model": model, "require_vision": require_vision})
        result = ChatResult(
            content=self.replies.pop(0),
            model_name=model or "stub",
            provider="stub",
            usage=UsageInfo(),
            elapsed_ms=1,
        )
        record_call(messages, result)
        return result


def make_case(case_id="TC-登录-001", priority="P1", **kw):
    base = {
        "case_id": case_id,
        "module": "登录",
        "title": "验证正确账号密码登录成功",
        "priority": priority,
        "precondition": "已注册账号",
        "steps": [{"action": "输入正确账号密码并提交", "expected": "跳转首页并展示用户昵称"}],
        "remark": "",
    }
    base.update(kw)
    return base


ANALYST_REPLY = json.dumps(
    {"modules": [{"module": "登录", "points": ["正常登录", "密码错误"]}], "blind_spots": ["未说明锁定策略"]},
    ensure_ascii=False,
)


def generator_reply(*cases):
    return json.dumps({"cases": list(cases)}, ensure_ascii=False)


def review_reply(passed, issues=()):
    return json.dumps({"passed": passed, "issues": list(issues), "missing": []}, ensure_ascii=False)
