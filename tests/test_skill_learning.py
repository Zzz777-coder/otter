"""v0.4 Skill Learning 簇挖掘离线测试(2026-09-24):模型校验/水位批处理/两阶段流水线/Human Gate。

全部离线(FakeAdapter 回放);FileTaskStore/SkillCandidateStore 用 tmp_path。
运行:.venv/bin/python -m pytest tests/test_skill_learning.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from otter.models.types import ModelResponse, ModelUsage
from otter.skill_learning import (
    SkillCandidate,
    SkillCandidateStore,
    TaskCard,
    TaskPatternCluster,
    distill_cluster,
    list_candidates,
    maybe_run_mining,
    mine_patterns,
    review,
)
from otter.task import FileTaskStore, TaskPatch, TaskStep, TaskStepStatus


class ScriptAdapter:
    """按脚本回放 JSON 响应的假 adapter(记录每次调用供断言顺序)。"""

    model = "fake"

    def __init__(self, script: list[str]):
        self.script = list(script)
        self.calls: list[str] = []

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        self.calls.append(messages[0].content[:40])
        return ModelResponse(content=self.script.pop(0), usage=ModelUsage(10, 2))


def _cluster(task_ids, name="pdf-report"):
    return TaskPatternCluster.model_validate({
        "id": name, "task_ids": task_ids, "pattern_name": name,
        "description": "生成中文 PDF 报告的固定流程", "similarity_reason": "三任务同流程",
        "reusable_value": "少走弯路"})


def _mk_completed(task_store: FileTaskStore, title: str, run_id: int):
    """造一个真实形态的 Completed Task(步骤 done 带依据 → 状态 completed)。"""

    async def make():
        t = await task_store.create(title=title, goal=f"完成{title}", owner_conversation_id="cli",
                                    steps=(TaskStep(id="s1", title="执行", status=TaskStepStatus.DONE,
                                                    note="验证通过"),),
                                    run_ids=(str(run_id),))
        return await task_store.apply_patch(t.id, TaskPatch(status="completed"))

    return asyncio.run(make())


# ── 模型校验 ───────────────────────────────────────────────────────

def test_candidate_invariants():
    ok = SkillCandidate(id="a1", action="create", proposed_name="pdf-report-gen",
                        description="d", reason="r", procedure=("s1", "s2"),
                        source_task_ids=("t1", "t2"))
    assert ok.status == "pending"
    with pytest.raises(Exception):  # update 必须带目标
        SkillCandidate(id="a2", action="update", proposed_name="x", description="d",
                       reason="r", procedure=("s",), source_task_ids=("t",))
    with pytest.raises(Exception):  # 名字必须 slug
        SkillCandidate(id="a3", action="create", proposed_name="Bad Name",
                       description="d", reason="r", procedure=("s",), source_task_ids=("t",))
    with pytest.raises(Exception):  # 无步骤拒
        SkillCandidate(id="a4", action="create", proposed_name="x-y", description="d",
                       reason="r", procedure=(), source_task_ids=("t",))


# ── 第一阶段:miner ────────────────────────────────────────────────

def test_mine_patterns_filters_and_errors(tmp_path: Path):
    cards = [TaskCard(task_id=f"t{i}", title=f"任务{i}") for i in range(4)]
    ok_out = json.dumps({"clusters": [
        {"id": "c1", "task_ids": ["t0", "t1", "t2"], "pattern_name": "p", "description": "d",
         "similarity_reason": "s", "reusable_value": "v"},
        # 编造 id(不在批次)与低于 min_cluster 的簇都必须被过滤
        {"id": "c2", "task_ids": ["t0", "zzz"], "pattern_name": "p2", "description": "d",
         "similarity_reason": "s", "reusable_value": "v"},
        {"id": "c3", "task_ids": ["t3"], "pattern_name": "p3", "description": "d",
         "similarity_reason": "s", "reusable_value": "v"},
    ]}, ensure_ascii=False)
    clusters, err = asyncio.run(mine_patterns(ScriptAdapter([ok_out]), cards, min_cluster_size=3))
    assert err is None and len(clusters) == 1 and clusters[0].id == "c1"
    # 非 JSON → 错误隔离,空簇返回
    clusters, err = asyncio.run(mine_patterns(ScriptAdapter(["不是json"]), cards, 3))
    assert clusters == [] and err


# ── 第二阶段:distiller(含仲裁) ──────────────────────────────────

def test_distill_update_target_must_exist(tmp_path: Path):
    store = FileTaskStore(tmp_path / "tasks")
    tasks = {t.id: t for t in [_mk_completed(store, "甲", 1), _mk_completed(store, "乙", 2),
                               _mk_completed(store, "丙", 3)]}
    out = json.dumps({"action": "update", "existing_skill_name": "不存在的技能",
                      "proposed_name": "x", "description": "d", "reason": "r",
                      "procedure": ["a"], "pitfalls": [], "verification": []})
    payload, err = asyncio.run(distill_cluster(
        ScriptAdapter([out]), _cluster(list(tasks)), tasks,
        lambda rid: _no_events(), tmp_path / "skills", []))
    assert err and "not in catalog" in err  # update 目标不在目录 → 拒


def test_distill_overlap_adjudication_downgrades_create(tmp_path: Path):
    """CREATE + 仲裁判 same → 降级为 update(上游 防重复二道闸)。"""
    skills = tmp_path / "skills" / "pdf-report"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: pdf-report\ndescription: 旧技能\n---\n正文",
                                     encoding="utf-8")
    store = FileTaskStore(tmp_path / "tasks2")
    tasks = {t.id: t for t in [_mk_completed(store, "甲", 1), _mk_completed(store, "乙", 2),
                               _mk_completed(store, "丙", 3)]}
    create_out = json.dumps({"action": "create", "proposed_name": "pdf-report-v2",
                             "description": "d", "reason": "r", "procedure": ["a"],
                             "pitfalls": [], "verification": []})
    adj_out = json.dumps({"relationship": "same", "existing_skill_name": "pdf-report",
                          "reason": "同族子类"})
    adapter = ScriptAdapter([create_out, adj_out])  # 依次:distiller → 仲裁
    payload, err = asyncio.run(distill_cluster(
        adapter, _cluster(list(tasks)), tasks, lambda rid: _no_events(), tmp_path / "skills", []))
    assert err is None and payload["action"] == "update"
    assert payload["existing_skill_name"] == "pdf-report"
    assert len(adapter.calls) == 2  # 仲裁确实发生


async def _no_events():
    return [{"type": "TOOL_STARTED", "payload": {"name": "make_pdf", "arguments": {}}}]


# ── 编排:watermark 批处理 + Human Gate 全链 ───────────────────────

def test_maybe_run_mining_batch_and_gate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTTER_SKILL_BATCH", "3")
    monkeypatch.chdir(tmp_path)
    task_store = FileTaskStore(tmp_path / "t")
    for i in range(2):  # 只造 2 个:不满批
        _mk_completed(task_store, f"任务{i}", i + 1)

    class NullEvents:
        async def load_run_events(self, rid):
            return [{"type": "TOOL_STARTED", "payload": {"name": "make_pdf"}}]

    note = asyncio.run(maybe_run_mining(ScriptAdapter([]), task_store, NullEvents()))
    assert note == ""  # 未满批:零模型调用
    _mk_completed(task_store, "任务2", 3)  # 凑满 3 个

    mine_out = json.dumps({"clusters": [
        {"id": "c1", "task_ids": [t.id for t in asyncio.run(task_store.list())],
         "pattern_name": "pdf-report", "description": "d", "similarity_reason": "s",
         "reusable_value": "v"}]}, ensure_ascii=False)
    distill_out = json.dumps({"action": "create", "proposed_name": "pdf-report-gen",
                              "description": "生成中文PDF报告", "reason": "三任务同流程",
                              "procedure": ["收集数据", "make_pdf"], "pitfalls": ["别用 write_file 写 pdf"],
                              "verification": ["打开确认 %PDF 魔数"]})
    adapter = ScriptAdapter([mine_out, distill_out])
    note = asyncio.run(maybe_run_mining(adapter, task_store, NullEvents()))
    assert "pdf-report-gen" in note and "/skill cand" in note
    # 候选落盘 + 水位:processed 三条、inflight 清空
    cs = SkillCandidateStore()
    pending = cs.list(status="pending")
    assert len(pending) == 1 and len(pending[0].source_task_ids) == 3
    wm = cs.load_watermark()
    assert len(wm.processed_task_ids) == 3 and wm.inflight is None
    # 同批不再触发(processed 永不重复计数)
    assert asyncio.run(maybe_run_mining(ScriptAdapter([]), task_store, NullEvents())) == ""

    # Human Gate:accept → SKILL.md 转正;重复审核拒
    cand_id = pending[0].id
    msg = review(cand_id, accept=True)
    assert "已转正" in msg
    skill_md = tmp_path / ".otter" / "skills" / "pdf-report-gen" / "SKILL.md"
    body = skill_md.read_text(encoding="utf-8")
    assert "稳定步骤" in body and "别用 write_file 写 pdf" in body
    assert "已审核过" in review(cand_id, accept=True)  # 重复审核拒
    assert "pdf-report-gen" in list_candidates()


def test_mining_failure_keeps_inflight(tmp_path: Path, monkeypatch):
    """模型失败:inflight 保留(at-least-once),下次触发重试同一批。"""
    monkeypatch.setenv("OTTER_SKILL_BATCH", "2")
    monkeypatch.chdir(tmp_path)
    task_store = FileTaskStore(tmp_path / "t")
    _mk_completed(task_store, "甲", 1)
    _mk_completed(task_store, "乙", 2)

    class NullEvents:
        async def load_run_events(self, rid):
            return []

    bad = ScriptAdapter(["坏输出"])  # miner 非 JSON
    assert asyncio.run(maybe_run_mining(bad, task_store, NullEvents())) == ""
    wm = SkillCandidateStore().load_watermark()
    assert wm.inflight is not None and wm.inflight.attempt == 1
    # 修好模型后同批重试成功
    mine_ok = json.dumps({"clusters": []}, ensure_ascii=False)  # 空簇=无模式,也算成功处理
    assert asyncio.run(maybe_run_mining(ScriptAdapter([mine_ok]), task_store, NullEvents())) == ""
    wm = SkillCandidateStore().load_watermark()
    assert wm.inflight is None and len(wm.processed_task_ids) == 2
