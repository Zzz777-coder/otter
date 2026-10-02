"""模型调用统一重试层离线测试(2026-10-02):可重试分类、退避节奏、流保护、
耗尽语义、工厂包装。零 mock:adapter 为脚本化假对象,sleep 注入零等待。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx2 as httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from otter.models import build_adapter  # noqa: E402
from otter.models.retry import (  # noqa: E402
    ModelRetryExhausted, RetryingAdapter, complete_stream_with_retry, is_retryable,
)
from otter.models.types import Message, ModelResponse, ModelUsage  # noqa: E402


def _status_exc(code: int) -> httpx.HTTPStatusError:
    """构造指定状态码的 HTTPStatusError(httpx 两族异常与 openai 同构可类比)。"""
    req = httpx.Request("POST", "https://api.example.com/v1/messages")
    return httpx.HTTPStatusError(f"{code}", request=req, response=httpx.Response(code, request=req))


class ScriptedAdapter:
    """脚本化假 adapter:actions 每项为异常(抛出)或 ModelResponse(返回),逐次消费。"""

    def __init__(self, actions: list) -> None:
        self.model = "fake-model"
        self.actions = list(actions)
        self.calls = 0

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        self.calls += 1
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


def _ok(content: str = "done") -> ModelResponse:
    return ModelResponse(content=content, usage=ModelUsage())


class FlakyStreamAdapter:
    """先吐一个 delta 再抛错——验证流保护(已开始流送则不重放)。"""

    def __init__(self, exc: Exception) -> None:
        self.model = "fake-model"
        self._exc = exc
        self.calls = 0

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        self.calls += 1
        if on_text_delta:
            on_text_delta("半截输出")
        raise self._exc


class FakeSleep:
    """注入用零等待 sleep,顺带收集延迟序列供断言。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def test_is_retryable_classification():
    """瞬态(429/408/5xx/网络类)可重试;4xx 参数/鉴权错与普通异常不可重试。"""
    assert is_retryable(_status_exc(429)) is True
    assert is_retryable(_status_exc(408)) is True
    assert is_retryable(_status_exc(500)) is True
    assert is_retryable(_status_exc(503)) is True
    assert is_retryable(httpx.ConnectError("connection refused")) is True
    assert is_retryable(httpx.ReadTimeout("read timed out")) is True
    assert is_retryable(_status_exc(400)) is False  # 参数错,重试无意义
    assert is_retryable(_status_exc(401)) is False  # 鉴权错
    assert is_retryable(ValueError("普通异常")) is False


def test_retry_then_success_with_backoff():
    """429 两次后第三次成功:共 3 次调用,退避 2 次且指数递增,on_retry 逐次回调。"""
    adapter = ScriptedAdapter([_status_exc(429), _status_exc(429), _ok()])
    sleeps = FakeSleep()
    retries: list[tuple[int, Exception]] = []

    def on_retry(attempt, delay, error):
        retries.append((attempt, error))

    resp = asyncio.run(complete_stream_with_retry(
        adapter, [Message(role="user", content="hi")], on_retry=on_retry, sleep=sleeps))

    assert resp.content == "done" and adapter.calls == 3
    assert len(sleeps.delays) == 2
    assert 1.0 <= sleeps.delays[0] < 1.3   # 1s 档(含 ≤20% 抖动)
    assert 2.0 <= sleeps.delays[1] < 2.5   # 2s 档,指数递增
    assert [a for a, _ in retries] == [1, 2]


def test_non_retryable_raises_immediately():
    """401 鉴权错:立即上抛,不退避不重试。"""
    adapter = ScriptedAdapter([_status_exc(401), _ok("不该到达")])
    sleeps = FakeSleep()

    try:
        asyncio.run(complete_stream_with_retry(
            adapter, [Message(role="user", content="hi")], sleep=sleeps))
        raise AssertionError("应当上抛 HTTPStatusError")
    except httpx.HTTPStatusError:
        pass
    assert adapter.calls == 1 and sleeps.delays == []


def test_exhausted_raises_with_attempts():
    """持续 5xx:重试 3 次耗尽后抛 ModelRetryExhausted(attempts=4,携带原始异常)。"""
    adapter = ScriptedAdapter([_status_exc(500)] * 4)
    sleeps = FakeSleep()

    try:
        asyncio.run(complete_stream_with_retry(
            adapter, [Message(role="user", content="hi")], sleep=sleeps))
        raise AssertionError("应当抛 ModelRetryExhausted")
    except ModelRetryExhausted as exc:
        assert exc.attempts == 4
        assert isinstance(exc.last_error, httpx.HTTPStatusError)
    assert adapter.calls == 4 and len(sleeps.delays) == 3


def test_streamed_output_never_replayed():
    """流保护:已回调过 on_text_delta(输出开始流送)后出错,不重试直接上抛。"""
    adapter = FlakyStreamAdapter(_status_exc(500))
    sleeps = FakeSleep()
    seen: list[str] = []

    try:
        asyncio.run(complete_stream_with_retry(
            adapter, [Message(role="user", content="hi")],
            on_text_delta=seen.append, sleep=sleeps))
        raise AssertionError("应当上抛 HTTPStatusError")
    except httpx.HTTPStatusError:
        pass
    assert adapter.calls == 1          # 未重放
    assert sleeps.delays == []         # 未退避
    assert seen == ["半截输出"]        # 原始 delta 只送达一次


def test_retrying_adapter_records_stats():
    """工厂包装层:重试计数与最近错误记入 retry_stats,诊断可读。"""
    inner = ScriptedAdapter([httpx.ConnectError("网络抖动"), _ok("恢复")])
    wrapped = RetryingAdapter(inner)

    resp = asyncio.run(wrapped.complete_stream([Message(role="user", content="hi")]))

    assert resp.content == "恢复" and inner.calls == 2
    assert wrapped.retry_stats["total_retries"] == 1
    assert "网络抖动" in wrapped.retry_stats["last_error"]
    assert wrapped.model == "fake-model"  # model 字段透传


def test_build_adapter_wraps_retrying():
    """工厂出口统一为 RetryingAdapter:所有调用点(loop/压缩/反思/提炼)自动受益。"""
    adapter = build_adapter("https://api.deepseek.example", "sk-test", "deepseek-chat")
    assert isinstance(adapter, RetryingAdapter)
    assert adapter.model == "deepseek-chat"

    anthropic_style = build_adapter("https://api.anthropic.com", "sk-test", "claude-x")
    assert isinstance(anthropic_style, RetryingAdapter)
