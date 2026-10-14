"""GUI 本轮 token 用量显示的接线与数据测试(2026-10-15)。

锁三处接线:
①_run_task 的 onDone payload 携带本轮真实 usage(source-inspection——pywebview
  跨桥无 pytest 收集,GUI 验收纪律与 test_gui_plan_mode 同法:先锁接线,真机
  DOM 断言归 --gui-artifact-probe 一族);
②switch_conversation 的 onHistory payload 挂每轮 Run 的用量(source-inspection);
③_conv_replay_usage 纯函数:真实 Store 库上验证汇总口径与"未知即 None"。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from otter.gui import OtterWebGui, _conv_replay_usage
from otter.models.types import Message
from otter.store import Store


def test_run_task_ondone_carries_usage():
    """① onDone payload 必须带 usage_in/usage_out,且取自 result.usage(未知原样 None)。"""
    src = inspect.getsource(OtterWebGui._run_task)
    assert '"usage_in": result.usage.input_tokens' in src
    assert '"usage_out": result.usage.output_tokens' in src


def test_switch_conversation_history_carries_usage():
    """② 回放 onHistory payload 必须挂每条消息的 usage(无数据的消息为 None)。"""
    api_src = inspect.getsource(OtterWebGui._api)
    assert "_conv_replay_usage" in api_src
    assert '"usage_in": usage_rows[i]["in"] if usage_rows[i] else None' in api_src


def test_conv_replay_usage_sum_and_none_semantics(tmp_path: Path):
    """③ 真库函数:按 Run 汇总(主调用+压缩,反思不计)、挂各轮末条 assistant、
    部分未知保持 None(不伪装 0)、无事件的轮不挂、行数不齐整份放弃。"""

    async def scenario() -> None:
        db = tmp_path / "otter.db"
        store = Store(db_path=db)
        await store.open()
        cid = await store.new_conversation()
        # 第 1 轮(run1):两次主调用 + 一次压缩 → 末条 assistant 挂 (135, 65)
        r1 = await store.new_run()
        await store.append_message(Message(role="user", content="问1"), 0, r1, conversation_id=cid)
        await store.append_message(Message(role="assistant", content="先分析"), 1, r1, conversation_id=cid)
        await store.append_event(r1, "MODEL_COMPLETED", {"usage_in": 100, "usage_out": 50})
        await store.append_event(r1, "CONTEXT_COMPACTED", {"usage_in": 5, "usage_out": 5})
        await store.append_message(Message(role="assistant", content="答1"), 2, r1, conversation_id=cid)
        await store.append_event(r1, "MODEL_COMPLETED", {"usage_in": 30, "usage_out": 10})
        # 第 2 轮(run2):一次主调用,厂商没回 input → (None, 20),不伪装 0
        r2 = await store.new_run()
        await store.append_message(Message(role="user", content="问2"), 3, r2, conversation_id=cid)
        await store.append_message(Message(role="assistant", content="答2"), 4, r2, conversation_id=cid)
        await store.append_event(r2, "MODEL_COMPLETED", {"usage_in": None, "usage_out": 20})
        # 第 3 轮(run3):首次调用前就取消——无任何用量事件 → 不挂(前端不显示)
        r3 = await store.new_run()
        await store.append_message(Message(role="user", content="问3"), 5, r3, conversation_id=cid)
        await store.append_message(Message(role="assistant", content="答3"), 6, r3, conversation_id=cid)
        # 记忆反思的用量记在 Run 名下但不属本轮口径 → 不得混入汇总
        await store.append_event(r1, "MEMORY_REFLECTION", {"usage_in": 999, "usage_out": 999})
        full = await store.load_conversation_messages(cid, include_tools=True)
        await store.close()
        assert [m.content for m in full] == ["问1", "先分析", "答1", "问2", "答2", "问3", "答3"]
        rows = _conv_replay_usage(db, cid, len(full))
        # 挂点只在各轮末条 assistant:答1(下标 2)与 答2(下标 4);答3 无事件不挂
        assert rows[2] == {"in": 135, "out": 65}
        assert rows[4] == {"in": None, "out": 20}
        assert rows[6] is None
        assert all(r is None for i, r in enumerate(rows) if i not in (2, 4))
        # 行数对不齐(写入竞态/旧库):整份放弃,全 None
        assert _conv_replay_usage(db, cid, len(full) + 1) == [None] * (len(full) + 1)
        assert _conv_replay_usage(db, cid, 0) == []
        # 库文件不存在:全 None(回放不因缺库阻断)
        assert _conv_replay_usage(tmp_path / "none.db", cid, 3) == [None, None, None]

    asyncio.run(scenario())
