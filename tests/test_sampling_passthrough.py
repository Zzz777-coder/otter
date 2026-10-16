"""采样参数透传离线测试(2026-10-13):OTTER_TEMPERATURE/OTTER_TOP_P 显式配置时
随请求发送,None 时不携带字段(行为与此前完全一致)。零 mock:HTTP 客户端为脚本化假对象。
"""
from __future__ import annotations

import asyncio
import json
import sys
from json import dumps as _dumps  # 假 client 的 stream(json=...) 参数名会遮蔽 json 模块,预取别名
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from otter.config import Config  # noqa: E402
from otter.models import build_adapter  # noqa: E402
from otter.models.anthropic import AnthropicAdapter  # noqa: E402
from otter.models.openai_compat import OpenAICompatAdapter  # noqa: E402
from otter.models.retry import RetryingAdapter  # noqa: E402
from otter.models.types import Message  # noqa: E402


# ── config 解析 ──────────────────────────────────────────────

def test_config_reads_sampling_env(monkeypatch):
    """显式设置时解析为 float。"""
    monkeypatch.setenv("OTTER_TEMPERATURE", "0.2")
    monkeypatch.setenv("OTTER_TOP_P", "0.9")
    cfg = Config.load()
    assert cfg.temperature == 0.2 and cfg.top_p == 0.9


def test_config_sampling_default_none(monkeypatch):
    """未设置 → None(adapter 层不发送字段)。"""
    monkeypatch.delenv("OTTER_TEMPERATURE", raising=False)
    monkeypatch.delenv("OTTER_TOP_P", raising=False)
    cfg = Config.load()
    assert cfg.temperature is None and cfg.top_p is None


def test_config_sampling_invalid_value_is_none(monkeypatch):
    """非法值静默忽略为 None——采样参数写错不该挡住整个助手启动。"""
    monkeypatch.setenv("OTTER_TEMPERATURE", "abc")
    monkeypatch.setenv("OTTER_TOP_P", "")
    cfg = Config.load()
    assert cfg.temperature is None and cfg.top_p is None


# ── OpenAI 兼容层:kwargs 增删 ────────────────────────────────

class FakeCompletions:
    """脚本化 chat.completions.create:捕获 kwargs,回放一个空流(带 usage 帧)。"""

    def __init__(self) -> None:
        self.captured: list[dict] = []

    async def create(self, **kwargs):
        self.captured.append(kwargs)
        # 流式回放:usage 在最后一帧(chunk.choices 为空)
        usage_frame = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
                                      choices=[])
        return _AsyncIter([usage_frame])


class _AsyncIter:
    def __init__(self, items) -> None:
        self._items = items

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


def _openai_adapter(**kw) -> tuple[OpenAICompatAdapter, FakeCompletions]:
    adapter = OpenAICompatAdapter("https://api.example.com", "sk-test", "test-model", **kw)
    fake = FakeCompletions()
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))  # 替换注入
    return adapter, fake


def test_openai_sends_temperature_when_set():
    adapter, fake = _openai_adapter(temperature=0.2)
    asyncio.run(adapter.complete_stream([Message(role="user", content="hi")]))
    assert fake.captured[0]["temperature"] == 0.2
    assert "top_p" not in fake.captured[0]  # 未配置的字段不发送


def test_openai_omits_sampling_when_none():
    """默认路径:两个字段都不带,与此前行为完全一致。"""
    adapter, fake = _openai_adapter()
    asyncio.run(adapter.complete_stream([Message(role="user", content="hi")]))
    assert "temperature" not in fake.captured[0] and "top_p" not in fake.captured[0]


# ── Anthropic 原生层:body 增删 ───────────────────────────────

class FakeStreamCtx:
    def __init__(self, lines) -> None:
        self._lines = lines

    def raise_for_status(self):
        return None  # 假响应恒为 2xx

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeHttpxClient:
    """捕获 AnthropicAdapter 发出的 JSON body;回放最小合法 SSE 事件序列。"""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def stream(self, method, url, headers=None, json=None):
        """httpx 的 stream() 是同步方法返回异步上下文管理器,签名保持一致。"""
        self.bodies.append(json)
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 5}}},
            {"type": "message_delta", "usage": {"output_tokens": 5}, "delta": {"stop_reason": "end_turn"}},
        ]
        lines = ["data: " + _dumps(ev, ensure_ascii=False) for ev in events]
        return FakeStreamCtx(lines)


def _anthropic_adapter(**kw) -> tuple[AnthropicAdapter, FakeHttpxClient]:
    adapter = AnthropicAdapter("https://api.anthropic.com", "sk-test", "claude-test", **kw)
    fake = FakeHttpxClient()
    adapter._client = fake
    return adapter, fake


def test_anthropic_sends_temperature_when_set():
    from otter.models.types import Message

    adapter, fake = _anthropic_adapter(temperature=0.3, top_p=0.95)
    asyncio.run(adapter.complete_stream([Message(role="user", content="hi")]))
    assert fake.bodies[0]["temperature"] == 0.3
    assert fake.bodies[0]["top_p"] == 0.95


def test_anthropic_omits_sampling_when_none():
    from otter.models.types import Message

    adapter, fake = _anthropic_adapter()
    asyncio.run(adapter.complete_stream([Message(role="user", content="hi")]))
    assert "temperature" not in fake.bodies[0] and "top_p" not in fake.bodies[0]


# ── 工厂透传 ─────────────────────────────────────────────────

def test_build_adapter_factory_passes_sampling():
    wrapped = build_adapter("https://api.example.com", "sk", "m", temperature=0.1)
    assert isinstance(wrapped, RetryingAdapter)
    assert wrapped._inner._temperature == 0.1  # 装配链:工厂 → RetryingAdapter 内层 adapter
