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


# ── 2026-09-28 会话单个删除 ────────────────────────────────────────

def test_delete_conversation_cascades(tmp_path):
    """删会话=行+消息级联删;不存在的 cid 返回 False;events 保留(Run 审计)。"""
    import asyncio

    from otter.models.types import Message
    from otter.store import Store

    async def main():
        s = Store(tmp_path / "t.db")
        await s.open()
        cid = await s.new_conversation("要删的")
        keep = await s.new_conversation("要留的")
        rid = await s.new_run()
        await s.append_message(Message(role="user", content="hi"), 0, rid, cid)
        await s.touch_conversation(cid)
        assert await s.delete_conversation(cid) is True
        assert await s.delete_conversation(cid) is False            # 二次删=False
        assert await s.delete_conversation(99999) is False
        titles = [c["title"] for c in await s.list_conversations()]
        assert titles == ["要留的"]
        # 消息级联删:残留消息不挂在已删会话上
        rows = await s.load_conversation_messages(cid)
        assert rows == []
        assert await s.load_conversation_messages(keep) == []       # 未动其他会话
        await s.close()

    asyncio.run(main())


def test_conversation_file_changes_dedupe_and_order(tmp_path: Path):
    """2026-09-29 界面能打开文件:按会话取 FILE_CHANGED 清单——同 path 留最后一次
    (最新 action/diffstat),顺序按最后变更;回放 payload 不带预览内容(点击现拉)。"""

    async def main() -> None:
        store = Store(db_path=tmp_path / "otter.db")
        await store.open()
        cid = await store.new_conversation("文件会话")
        rid = await store.new_run()
        await store.append_message(Message(role="user", content="建文件"), 0, rid, conversation_id=cid)
        await store.append_event(rid, "FILE_CHANGED",
                                 {"path": "a.txt", "name": "a.txt", "action": "已创建",
                                  "plus": 5, "minus": 0, "preview_type": "text", "content": "旧"})
        await store.append_event(rid, "FILE_CHANGED",
                                 {"path": "a.txt", "name": "a.txt", "action": "已修改",
                                  "plus": 1, "minus": 1, "preview_type": "text", "content": "新"})
        await store.append_event(rid, "FILE_CHANGED",
                                 {"path": "b.md", "name": "b.md", "action": "已创建",
                                  "plus": 2, "minus": 0})
        # 其他会话的 run 不串进来
        rid2 = await store.new_run()
        await store.append_message(Message(role="user", content="别的"), 0, rid2, conversation_id=cid + 99)
        await store.append_event(rid2, "FILE_CHANGED",
                                 {"path": "别的.txt", "name": "别的.txt", "action": "已创建"})
        files = await store.load_conversation_file_changes(cid)
        assert [f["name"] for f in files] == ["a.txt", "b.md"]  # 按最后变更时间升序
        a = next(f for f in files if f["path"] == "a.txt")
        assert a["action"] == "已修改" and a["plus"] == 1       # 同 path 留最后一次
        assert "content" not in a and "preview_type" not in a   # 回放不带快照,点击现拉
        await store.close()

    asyncio.run(main())
