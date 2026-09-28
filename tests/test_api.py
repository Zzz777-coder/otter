"""HTTP API 层测试(2026-09-29 新增 R1)。

TestClient + 临时库注入,完全离线——不碰真实 .otter/otter.db,
不需要模型 key。覆盖:health / 会话 CRUD 全回路 / 404 语义 /
messages 与 include_tools / Run 事件流。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from otter.api import create_app
from otter.models.types import Message
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
