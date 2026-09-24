"""headless stream-json(2026-09-24)的离线测试。

覆盖两层:StreamJsonEmitter 行格式(NDJSON 契约)+ run_single_stream 全链
(FakeAdapter/FakeStore 仿 test_loop.py,不发真实请求,stdout 经 capsys 抓取)。
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from otter.loop import AgentLoop  # noqa: E402
from otter.models.types import ModelResponse, ModelUsage  # noqa: E402
from otter.repl import StreamJsonEmitter, run_single_stream  # noqa: E402
from otter.tools.base import ToolRegistry  # noqa: E402


class FakeAdapter:
    """按脚本回放的假 adapter(最小面:stream-json 测试不需要工具)。"""

    def __init__(self, script: list[ModelResponse]) -> None:
        self.script = list(script)
        self.model = "fake-model"

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        resp = self.script.pop(0)
        if on_text_delta and resp.content:
            on_text_delta(resp.content)
        return resp


class FakeStore:
    def __init__(self) -> None:
        self.messages = []

    async def new_run(self):
        return 1

    async def finish_run(self, *a):
        pass

    async def append_message(self, msg, seq, run_id, conversation_id=None):
        self.messages.append(msg)

    async def save_checkpoint(self, *a, **k):
        pass

    async def append_event(self, run_id, type_, payload):
        pass


def test_emitter_formats():
    """行格式契约:每行合法 JSON;事件名小写直通;空增量不发;error 行。"""
    buf = io.StringIO()
    e = StreamJsonEmitter(file=buf)
    e.init(model="m", mode="normal", max_steps=3)
    e.event("MODEL_STARTED", {"step": 1, "mode": "normal"})
    e.delta("")  # 空增量不产生行
    e.delta("你好")
    e.error("boom")
    lines = buf.getvalue().splitlines()
    objs = [json.loads(x) for x in lines]  # NDJSON 契约:行行可解析
    assert objs[0] == {"type": "init", "model": "m", "mode": "normal", "max_steps": 3}
    assert objs[1] == {"type": "model_started", "step": 1, "mode": "normal"}
    assert objs[2] == {"type": "text_delta", "text": "你好"}
    assert objs[3] == {"type": "error", "message": "boom"}


def test_emitter_final_interrupted():
    """result=None(中断)时 final 行 ok=false、stop_reason=interrupted。"""
    buf = io.StringIO()
    e = StreamJsonEmitter(file=buf)
    e.final(None)
    obj = json.loads(buf.getvalue())
    assert obj["type"] == "final" and obj["ok"] is False
    assert obj["stop_reason"] == "interrupted" and obj["steps"] == 0


def test_run_single_stream_end_to_end(capsys):
    """全链:init → model_started → text_delta → final(ok,字段与 -p json 同构),退出码 0。"""
    adapter = FakeAdapter([
        ModelResponse(content="任务完成:hello", usage=ModelUsage(12, 4)),
    ])
    store = FakeStore()
    loop = AgentLoop(adapter, ToolRegistry(), store)
    rc = asyncio.run(run_single_stream(loop, store, "说 hello", 5))
    out = capsys.readouterr().out
    lines = [x for x in out.splitlines() if x.strip()]
    objs = [json.loads(x) for x in lines]  # stdout 全体必须是合法 NDJSON(混入即坏流)
    assert rc == 0
    assert objs[0]["type"] == "init" and objs[0]["mode"] == "normal" and objs[0]["model"] == "fake-model"
    types = [o["type"] for o in objs]
    assert "model_started" in types          # 事件小写直通
    assert "text_delta" in types             # 流式增量
    final = objs[-1]
    assert final["type"] == "final" and final["ok"] is True
    assert final["stop_reason"] == "final_answer"
    assert final["final_text"] == "任务完成:hello"
    assert final["steps"] == 1
    assert final["usage"] == {"input": 12, "output": 4}
