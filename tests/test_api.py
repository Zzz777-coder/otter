"""HTTP API 层测试(2026-09-29 新增 R1;R2 扩展 SSE chat)。

TestClient + 临时库注入,完全离线——不碰真实 .otter/otter.db,
不需要模型 key。覆盖:health / 会话 CRUD 全回路 / 404 语义 /
messages 与 include_tools / Run 事件流 / chat 503-404 / SSE 流式全协议。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from otter.api import create_app
from otter.models.types import Message, ModelUsage
from otter.store import Store


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """临时库客户端:每个测试独立 Store,复用 create_app 工厂注入。"""
    store = Store(db_path=tmp_path / "api_test.db")
    with TestClient(create_app(store=store)) as c:
        yield c, store


def test_health_reports_version(client):
    c, _ = client
    resp = c.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]  # 版本号非空(与 otter.__version__ 对齐)


def test_conversation_crud_roundtrip(client):
    c, _ = client
    # 建会话 → 201 + 返回 id
    created = c.post("/api/conversations", json={"title": "API 测试会话"})
    assert created.status_code == 201
    cid = created.json()["id"]

    # 列表能看到它
    listed = c.get("/api/conversations").json()
    assert any(item["id"] == cid and item["title"] == "API 测试会话" for item in listed)

    # PATCH 改名 + 置顶(一次只验一个语义:均生效)
    patched = c.patch(f"/api/conversations/{cid}", json={"title": "改名了", "pinned": True})
    assert patched.status_code == 200
    item = next(x for x in c.get("/api/conversations").json() if x["id"] == cid)
    assert item["title"] == "改名了" and item["pinned"] is True

    # DELETE → 204,再访问 messages → 404(级联删除后资源不存在)
    assert c.delete(f"/api/conversations/{cid}").status_code == 204
    assert c.get(f"/api/conversations/{cid}/messages").status_code == 404


def test_conversation_404_on_missing(client):
    c, _ = client
    assert c.patch("/api/conversations/9999", json={"pinned": True}).status_code == 404
    assert c.delete("/api/conversations/9999").status_code == 404
    assert c.get("/api/conversations/9999/messages").status_code == 404


def test_messages_roundtrip_and_include_tools(client):
    c, store = client
    import asyncio

    async def seed() -> int:
        new_id = await store.new_conversation("消息测试")
        await store.append_message(
            Message(role="user", content="你好"), sequence=1, run_id=0, conversation_id=new_id
        )
        await store.append_message(
            Message(role="assistant", content="在的"), sequence=2, run_id=0, conversation_id=new_id
        )
        return new_id

    cid = asyncio.run(seed())
    # 默认只取 user/assistant
    msgs = c.get(f"/api/conversations/{cid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "你好"
    # include_tools=true 走同一查询的宽口径(无工具消息时结果一致)
    wide = c.get(f"/api/conversations/{cid}/messages", params={"include_tools": True}).json()
    assert len(wide) == 2


def test_run_events_stream_and_empty_legal(client):
    c, store = client
    import asyncio

    async def seed() -> int:
        run_id = await store.new_run()
        await store.append_event(run_id, "tool.start", {"name": "read_file"})
        await store.append_event(run_id, "tool.end", {"name": "read_file", "ok": True})
        return run_id

    run_id = asyncio.run(seed())
    events = c.get(f"/api/runs/{run_id}/events").json()
    assert [e["type"] for e in events] == ["tool.start", "tool.end"]
    # 不存在的 run → 200 空列表(事件流以空为合法态,与 messages 的 404 语义有意区分)
    assert c.get("/api/runs/99999/events").json() == []


# ── R2(2026-09-29):SSE 流式 chat ────────────────────────────────────


@dataclass
class FakeLoop:
    """离线假执行核心:吐 text_delta 与工具事件、按真实语义落库(loop.run
    才是落库责任人——user/assistant 消息与工具事件由它写 Store),返回固定 AgentResult。"""

    store: Store = None  # 落库用(由 FakeEngine 注入同一个临时 Store)
    deltas: list[str] = field(default_factory=lambda: ["你好", ",我是", "结果"])
    events: list[tuple[str, dict]] = field(default_factory=lambda: [
        ("MODEL_STARTED", {"model": "fake"}),
        ("TOOL_STARTED", {"name": "read_file"}),
        ("TOOL_COMPLETED", {"name": "read_file", "ok": True}),
    ])

    async def run(self, history, user_message, run_id, max_steps, on_text_delta=None,
                  on_event=None, conversation_id=None, **kwargs):
        for d in self.deltas:
            if on_text_delta:
                on_text_delta(d)
        for type_, payload in self.events:
            if on_event:
                await on_event(type_, payload)
            await self.store.append_event(run_id, type_, payload)
        from otter.loop import AgentResult

        final_text = "".join(self.deltas)
        # 模拟真实 loop 的落库行为(user/assistant 双消息,sequence 简单递增)
        await self.store.append_message(user_message, sequence=1, run_id=run_id,
                                        conversation_id=conversation_id)
        await self.store.append_message(Message(role="assistant", content=final_text),
                                        sequence=2, run_id=run_id, conversation_id=conversation_id)
        return AgentResult(ok=True, final_text=final_text, stop_reason="final_answer",
                           steps=1, model_calls=1, tool_calls=1,
                           usage=ModelUsage(input_tokens=10, output_tokens=5))


@dataclass
class FakeEngine:
    """chat 端点依赖的最小引擎面:store(真)+ loop(fake)。"""

    store: Store
    loop: FakeLoop = None

    def __post_init__(self):
        if self.loop is None:
            self.loop = FakeLoop(store=self.store)

    async def aclose(self) -> None:  # lifespan 收尾会调;无外部连接,空实现
        pass


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """把原始 SSE 文本解析成 (event, data) 序列(逐帧 split,容忍空行)。"""
    frames = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        if not lines:
            continue
        name = lines[0].removeprefix("event: ").strip()
        data = json.loads(lines[1].removeprefix("data: "))
        frames.append((name, data))
    return frames


def test_chat_503_without_engine(client):
    c, _ = client
    resp = c.post("/api/chat", json={"prompt": "hi"})
    assert resp.status_code == 503  # 引擎未配置(离线测试默认);R1 只读端点不受影响


def test_chat_404_on_missing_conversation(tmp_path: Path):
    store = Store(db_path=tmp_path / "chat404.db")
    engine = FakeEngine(store=store)  # loop 由引擎自建(注入同一临时 Store,落库行为才真)
    with TestClient(create_app(store=store, engine=engine)) as c:
        resp = c.post("/api/chat", json={"prompt": "hi", "conversation_id": 9999})
        assert resp.status_code == 404  # 开流前的校验走标准 JSON 错误,不是流内 error 帧


def test_chat_sse_stream_full_protocol(tmp_path: Path):
    """全协议回路:conversation/run 头帧 → 事件小写直通 → text_delta → final;

    并验证消息确实落库(R1 只读端点可取回)——这是"断连≠取消、落库为准"的基础。
    落库核验必须在 TestClient 上下文内做(lifespan 结束即关库)。
    """
    store = Store(db_path=tmp_path / "chat_sse.db")
    engine = FakeEngine(store=store)  # loop 由引擎自建(注入同一临时 Store,落库行为才真)
    with TestClient(create_app(store=store, engine=engine)) as c:
        resp = c.post("/api/chat", json={"prompt": "介绍一下你自己"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        frames = _parse_sse(resp.text)

        names = [n for n, _ in frames]
        # 头两帧固定:会话与 run 标识(客户端据此定位资源)
        assert names[0] == "conversation" and frames[0][1]["conversation_id"] > 0
        assert names[1] == "run" and frames[1][1]["run_id"] > 0
        # loop 事件小写直通(与 CLI stream-json 事件名对齐)
        assert "model_started" in names and "tool_started" in names and "tool_completed" in names
        # 文本增量逐帧推送,拼接还原全文
        deltas = [d["text"] for n, d in frames if n == "text_delta"]
        assert deltas == ["你好", ",我是", "结果"]
        # 终帧 final:ok/停止原因/统计齐备
        final = frames[-1]
        assert final[0] == "final" and final[1]["ok"] is True
        assert final[1]["final_text"] == "你好,我是结果"
        assert final[1]["usage"] == {"input": 10, "output": 5}

        # 落库核验:用户消息与 assistant 终稿都在会话里(R1 端点取回)
        cid = frames[0][1]["conversation_id"]
        msgs = c.get(f"/api/conversations/{cid}/messages").json()
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["content"] == "介绍一下你自己"
        assert msgs[1]["content"] == "你好,我是结果"


def test_chat_sse_into_existing_conversation(tmp_path: Path):
    """带 conversation_id 续聊:不新建会话,消息追加到既有会话尾部。"""
    store = Store(db_path=tmp_path / "chat_cont.db")
    engine = FakeEngine(store=store)  # loop 由引擎自建(注入同一临时 Store,落库行为才真)

    async def seed() -> int:
        return await store.new_conversation("续聊测试")

    with TestClient(create_app(store=store, engine=engine)) as c:
        # seed 须在库 open 之后(lifespan 已起)——与 R1 既有用例同模式
        cid = asyncio.run(seed())
        resp = c.post("/api/chat", json={"prompt": "第二问", "conversation_id": cid})
        frames = _parse_sse(resp.text)
        assert frames[0] == ("conversation", {"conversation_id": cid})  # 沿用传入会话
        msgs = c.get(f"/api/conversations/{cid}/messages").json()
        assert len(msgs) == 2  # 追加而非新建


def test_chat_sse_engine_error_frame(tmp_path: Path):
    """引擎异常 → 流内 error 帧收尾(run 落 failed),不悬挂连接。"""

    @dataclass
    class BoomLoop:
        async def run(self, *a, **k):
            raise RuntimeError("模型连接失败")

    store = Store(db_path=tmp_path / "chat_err.db")
    engine = FakeEngine(store=store, loop=BoomLoop())
    with TestClient(create_app(store=store, engine=engine)) as c:
        resp = c.post("/api/chat", json={"prompt": "会失败的任务"})
        frames = _parse_sse(resp.text)
    assert frames[-1][0] == "error"
    assert "模型连接失败" in frames[-1][1]["message"]
