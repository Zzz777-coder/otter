"""v5 会话模型离线测试:conversations 表 / 消息归属 / 重启恢复。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from otter.models.types import Message
from otter.store import Store


def test_conversations_lifecycle_and_restore(tmp_path: Path):
    async def scenario() -> None:
        db = tmp_path / "otter.db"
        store = Store(db_path=db)
        await store.open()
        # 建两个会话,消息分别归属
        c1 = await store.new_conversation("会话一")
        c2 = await store.new_conversation("会话二")
        await store.append_message(Message(role="user", content="你好"), 0, 1, conversation_id=c1)
        await store.append_message(Message(role="assistant", content="你好!"), 1, 1, conversation_id=c1)
        await store.append_message(Message(role="user", content="另一个话题"), 0, 2, conversation_id=c2)
        # 列表按活跃时间倒序(c2 后 touch)
        await store.touch_conversation(c2)
        convs = await store.list_conversations()
        assert [c["id"] for c in convs] == [c2, c1]
        # 标题更新
        await store.touch_conversation(c1, title="改名后")
        convs = {c["id"]: c["title"] for c in await store.list_conversations()}
        assert convs[c1] == "改名后"
        await store.close()

        # 重启"恢复":新 Store 实例(同库)加载 c1 的 user/assistant 历史
        store2 = Store(db_path=db)
        await store2.open()
        msgs = await store2.load_conversation_messages(c1)
        assert [(m.role, m.content) for m in msgs] == [("user", "你好"), ("assistant", "你好!")]
        # 旧库迁移幂等:重复 open 不炸(messages.conversation_id 列已存在)
        await store2.open()
        await store2.close()

    asyncio.run(scenario())


def test_append_message_without_conversation_backcompat(tmp_path: Path):
    """REPL 路径不传 conversation_id——签名向后兼容,消息 conversation_id 为 NULL。"""

    async def scenario() -> None:
        import aiosqlite

        store = Store(db_path=tmp_path / "otter.db")
        await store.open()
        await store.append_message(Message(role="user", content="legacy"), 0, 1)  # 不传 cid
        await store.close()
        db = await aiosqlite.connect(tmp_path / "otter.db")
        cur = await db.execute("SELECT conversation_id FROM messages")
        assert (await cur.fetchone())[0] is None
        await db.close()

    asyncio.run(scenario())
