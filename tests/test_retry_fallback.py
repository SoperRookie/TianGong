"""容错与降级测试（F-1-6）。"""

from types import SimpleNamespace

import httpx
import openai
import pytest

from app.llm.client import AllModelsFailedError, LLMClient
from app.llm.registry import ModelRegistry
from app.llm.schemas import ModelConfig


def _registry(max_retries=1):
    return ModelRegistry(
        default_model="primary",
        max_retries=max_retries,
        models=[
            ModelConfig(
                name="primary", provider="deepseek", base_url="http://p", model="m1",
                fallbacks=["backup"],
            ),
            ModelConfig(name="backup", provider="openai", base_url="http://b", model="m2"),
        ],
    )


def _conn_error():
    return openai.APIConnectionError(request=httpx.Request("POST", "http://x"))


def _ok_response(content="ok"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


class FlakyOpenAI:
    """前 fail_times 次抛连接错误，之后成功。"""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **params):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _conn_error()
        return _ok_response()


async def test_同模型重试后成功():
    client = LLMClient(_registry(max_retries=1))
    flaky = FlakyOpenAI(fail_times=1)
    client._clients["primary"] = flaky

    result = await client.chat([{"role": "user", "content": "hi"}])

    assert result.content == "ok"
    assert result.model_name == "primary"
    assert result.attempts == 2


async def test_重试耗尽后降级到备用模型():
    client = LLMClient(_registry(max_retries=1))
    client._clients["primary"] = FlakyOpenAI(fail_times=99)  # 主模型一直失败
    client._clients["backup"] = FlakyOpenAI(fail_times=0)

    result = await client.chat([{"role": "user", "content": "hi"}])

    assert result.model_name == "backup"
    assert result.attempts == 3  # 主模型 2 次（1+重试1）+ 备用 1 次


async def test_全链路失败抛出明确异常():
    client = LLMClient(_registry(max_retries=0))
    client._clients["primary"] = FlakyOpenAI(fail_times=99)
    client._clients["backup"] = FlakyOpenAI(fail_times=99)

    with pytest.raises(AllModelsFailedError, match="primary → backup"):
        await client.chat([{"role": "user", "content": "hi"}])


async def test_鉴权错误不重试():
    client = LLMClient(_registry(max_retries=2))

    class AuthFail:
        def __init__(self):
            self.calls = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **params):
            self.calls += 1
            raise openai.AuthenticationError(
                "bad key",
                response=httpx.Response(401, request=httpx.Request("POST", "http://x")),
                body=None,
            )

    auth_fail = AuthFail()
    client._clients["primary"] = auth_fail
    with pytest.raises(openai.AuthenticationError):
        await client.chat([{"role": "user", "content": "hi"}])
    assert auth_fail.calls == 1  # 非暂时性错误立即抛出，不重试不降级
