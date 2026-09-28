"""v0.3 Task 系统离线测试(2026-09-24):模型校验/Store 状态机/Plan 契约/工具层/上下文渲染。

全部离线(无网络/无模型);FileTaskStore 用 tmp_path 目录。
运行:.venv/bin/python -m pytest tests/test_task.py -v
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from otter.task import (
    FileTaskStore,
    Task,
    TaskPatch,
    TaskPriority,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    TaskCreateTool,
    TaskUpdateTool,
    TaskListTool,
    pending_plan_is_valid,
    render_task_context,
)


def _run(coro):
    return asyncio.run(coro)


# ── 模型不变量(移植 vesta 校验语义) ────────────────────────────────

def _mk_task(**overrides) -> Task:
    now_base = overrides.pop("now", None)
    from datetime import UTC, datetime

    now = now_base or datetime.now(UTC)
    data = dict(
        id="a" * 32, title="测试任务", goal="完成某事",
        owner_conversation_id="cli", created_at=now, updated_at=now,
    )
    data.update(overrides)
    return Task.model_validate(data)


def test_step_done_requires_note():
    with pytest.raises(ValidationError):
        TaskStep(id="s1", title="步骤", status=TaskStepStatus.DONE)  # 无 note 必拒
    ok = TaskStep(id="s1", title="步骤", status=TaskStepStatus.DONE, note="测试通过 3/3")
    assert ok.status is TaskStepStatus.DONE


def test_task_single_in_progress_and_completed_requires_all_done():
    s1 = TaskStep(id="s1", title="a", status=TaskStepStatus.IN_PROGRESS)
    s2 = TaskStep(id="s2", title="b", status=TaskStepStatus.IN_PROGRESS)
    with pytest.raises(ValidationError):
        _mk_task(steps=(s1, s2))  # 两个 in_progress 拒
    with pytest.raises(ValidationError):
        _mk_task(status=TaskStatus.COMPLETED, steps=(s1,))  # 未全 done 不能 completed


# ── Store:CRUD / 前缀解析 / 乐观锁 / PENDING 转换 ────────────────────

def test_store_create_resolve_and_prefix(tmp_path: Path):
    store = FileTaskStore(tmp_path)
    task = _run(store.create(title="拆解任务", goal="g", owner_conversation_id="cli",
                             steps=(TaskStep(id="x1", title="一"),)))
    assert task.status is TaskStatus.PENDING
    # 唯一前缀解析
    got = _run(store.resolve(task.id[:8]))
    assert got is not None and got.id == task.id
    # owner 过滤:别的会话看不到
    assert _run(store.resolve(task.id[:8], owner_conversation_id="other")) is None


def test_store_patch_step_flow_and_revision_lock(tmp_path: Path):
    store = FileTaskStore(tmp_path)
    task = _run(store.create(title="t", goal="g", owner_conversation_id="cli",
                             steps=(TaskStep(id="s1", title="一"), TaskStep(id="s2", title="二"))))
    # 推进 s1:in_progress(note 可选)
    t1 = _run(store.apply_patch(task.id, TaskPatch(step_id="s1", step_status=TaskStepStatus.IN_PROGRESS)))
    assert t1.revision == 2
    # done 必须 note → 不带 note 走校验直接抛
    with pytest.raises(ValidationError):
        _run(store.apply_patch(task.id, TaskPatch(step_id="s1", step_status=TaskStepStatus.DONE)))
    t2 = _run(store.apply_patch(task.id, TaskPatch(step_id="s1", step_status=TaskStepStatus.DONE,
                                                   step_note="产物已生成并验证")))
    assert t2.steps[0].status is TaskStepStatus.DONE
    # done 步骤不可回退
    with pytest.raises(ValueError):
        _run(store.apply_patch(task.id, TaskPatch(step_id="s1", step_status=TaskStepStatus.TODO)))
    # 乐观锁:期望旧 revision 拒
    with pytest.raises(ValueError):
        _run(store.apply_patch(task.id, TaskPatch(goal="新目标", expected_revision=1)))
    # 冲突时原子性:goal 未被部分写入
    t3 = _run(store.get(task.id))
    assert t3.goal == "g" and t3.revision == 3


def test_store_plan_accept_reject_only_pending(tmp_path: Path):
    store = FileTaskStore(tmp_path)
    task = _run(store.create(title="计划", goal="g", owner_conversation_id="cli"))
    acc = _run(store.plan_accept(task.id))
    assert acc.status is TaskStatus.ACTIVE
    with pytest.raises(ValueError):  # ACTIVE 不能再 accept/reject(仅 PENDING 可转)
        _run(store.plan_reject(task.id))
    rej = _run(store.create(title="计划2", goal="g2", owner_conversation_id="cli"))
    r = _run(store.plan_reject(rej.id))
    assert r.status is TaskStatus.CANCELLED and r.completed_at is not None


def test_store_persistence_across_instances(tmp_path: Path):
    """跨会话不丢:新 store 实例(新进程语义)重读同一目录。"""
    s1 = FileTaskStore(tmp_path)
    task = _run(s1.create(title="跨会话", goal="g", owner_conversation_id="cli"))
    _run(s1.plan_accept(task.id))
    s2 = FileTaskStore(tmp_path)
    loaded = _run(s2.get(task.id))
    assert loaded is not None and loaded.status is TaskStatus.ACTIVE
    # 活动任务按会话可查(上下文注入的取数路径)
    active = _run(s2.active_for_conversation("cli"))
    assert active is not None and active.id == task.id


# ── Plan 契约:pending_plan_is_valid(防伪造进度) ─────────────────────

def test_pending_plan_validity(tmp_path: Path):
    store = FileTaskStore(tmp_path)
    bad = _run(store.create(title="无步骤", goal="g", owner_conversation_id="cli"))
    assert not _run(pending_plan_is_valid(store, "cli", bad.id))  # steps 空 → 无效
    good = _run(store.create(title="合格计划", goal="g", owner_conversation_id="cli",
                             steps=(TaskStep(id="a", title="一"), TaskStep(id="b", title="二"))))
    assert _run(pending_plan_is_valid(store, "cli", good.id))
    _run(store.plan_accept(good.id))
    assert not _run(pending_plan_is_valid(store, "cli", good.id))  # ACTIVE 不再是 PENDING 计划
    forged = _run(store.create(title="伪造进度", goal="g", owner_conversation_id="cli",
                               steps=(TaskStep(id="a", title="x", status=TaskStepStatus.DONE,
                                               note="假装完成"),)))
    assert not _run(pending_plan_is_valid(store, "cli", forged.id))  # PENDING 但含 DONE → 拒


# ── 工具层:ctx 注入 / current 句柄 / PLAN 模式限制 ──────────────────

def _tools(tmp_path: Path):
    store = FileTaskStore(tmp_path)
    ctx: dict = {}
    return store, ctx, TaskCreateTool(store, ctx), TaskUpdateTool(store, ctx)


def test_tool_create_and_current_handle(tmp_path: Path):
    store, ctx, create, update = _tools(tmp_path)
    ctx["conversation_id"] = "cli"
    out = json.loads(_run(create.run({"title": "工具任务", "goal": "g",
                                      "steps": [{"title": "一"}, {"title": "二"}]})))
    assert out["status"] == "pending" and len(out["steps"]) == 2
    # 无会话上下文 → 拒
    ctx2: dict = {}
    c2 = TaskCreateTool(store, ctx2)
    assert "[otter] 错误" in _run(c2.run({"title": "x"}))


def test_tool_plan_mode_blocks_status_and_step(tmp_path: Path):
    store, ctx, create, update = _tools(tmp_path)
    ctx.update(conversation_id="cli", mode="plan")
    out = json.loads(_run(create.run({"title": "p", "goal": "g"})))
    tid = out["id"]
    # PLAN 下改状态拒
    assert "PLAN" in _run(update.run({"task_id": tid, "status": "active"}))
    # PLAN 下推进步骤拒
    assert "PLAN" in _run(update.run({"task_id": tid, "step_id": out["steps"][0]["id"] if out["steps"] else "s",
                                      "step_status": "done", "step_note": "x"}))
    # ACT 模式下正常推进
    ctx["mode"] = "normal"
    ok = json.loads(_run(update.run({"task_id": tid, "status": "active"})))
    assert ok["status"] == "active"


def test_tool_cross_conversation_isolation(tmp_path: Path):
    store, ctx, create, update = _tools(tmp_path)
    ctx.update(conversation_id="cli")
    out = json.loads(_run(create.run({"title": "私有", "goal": "g"})))
    ctx["conversation_id"] = "other"  # 换会话:看不到 cli 的任务
    assert "任务不存在" in _run(update.run({"task_id": out["id"], "goal": "劫持"}))


# ── 上下文渲染:预算受控快照 ────────────────────────────────────────

def test_render_task_context_budget():
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    done_steps = tuple(TaskStep(id=f"d{i}", title=f"已完成{i}", status=TaskStepStatus.DONE,
                                note="依据") for i in range(6))
    pending_steps = tuple(TaskStep(id=f"p{i}", title=f"待办{i}") for i in range(15))
    task = _mk_task(now=now, status=TaskStatus.ACTIVE, steps=done_steps + pending_steps,
                    key_facts=(f"事实{i}" for i in range(20)),
                    constraints=(f"约束{i}" for i in range(20)))
    text = render_task_context(task)
    payload = json.loads(text.split("<active_task>")[1].rstrip("</active_task>"))
    # done 只留近 3、待办截 12、列表截 12(vesta 预算口径)
    assert payload["omitted_done_steps"] == 3
    assert payload["omitted_pending_steps"] == 3
    assert payload["omitted_entries"]["key_facts"] == 8
    assert len(payload["steps"]) == 15  # 3 done + 12 pending
    assert len(payload["key_facts"]) == 12
