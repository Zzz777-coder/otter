"""M2 离线测试:权限引擎/审批/沙箱 fail-closed/Evidence 归档回读/Checkpoint 恢复。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from otter.evidence import ARCHIVE_THRESHOLD, maybe_archive
from otter.loop import AgentLoop
from otter.models.types import Message, ModelResponse, ModelUsage, ToolCall
from otter.sandbox import NullSandbox, SeatbeltSandbox, make_sandbox
from otter.store import Store
from otter.tools.base import Tool, ToolRegistry
from otter.tools.builtin import BashTool, ReadFileTool, WriteFileTool
from otter.tools.permissions import (
    ALLOW, ASK, DENY, ApprovalGate, PermissionEngine, Rule, SessionMemory,
)


# ── 权限引擎 ──────────────────────────────────────────────────────

def test_engine_default_and_precedence(tmp_path: Path):
    engine = PermissionEngine(rules_file=tmp_path / "nope.json")
    assert engine.decide("bash", {"command": "ls"}) == ALLOW      # 2026-09-23 用户要求:bash 全放行
    assert engine.decide("read_file", {"path": "x"}) == ALLOW       # 读类直放
    engine.user_rules.append(Rule("bash", "git *", ALLOW))
    assert engine.decide("bash", {"command": "git status"}) == ALLOW  # 前缀命中放行
    assert engine.decide("bash", {"command": "rm -rf /"}) == ALLOW    # 2026-09-23 bash 全放行(无前缀规则时走默认)
    engine.user_rules.append(Rule("bash", None, DENY))              # 整工具 DENY 冲突优先
    assert engine.decide("bash", {"command": "git status"}) == DENY


def test_engine_rules_file_roundtrip(tmp_path: Path):
    f = tmp_path / "permissions.json"
    engine = PermissionEngine(rules_file=f)
    engine.add(Rule("bash", "pip *", ALLOW))
    engine2 = PermissionEngine(rules_file=f)
    assert engine2.decide("bash", {"command": "pip install x"}) == ALLOW


def test_gate_deny_via_rule_and_session_memory(tmp_path: Path):
    """gate.resolve:规则 DENY 直接拒;会话记忆命中直接放;ASK 走交互(此处桩掉)。"""
    engine = PermissionEngine(rules_file=tmp_path / "nope.json")
    memory = SessionMemory()
    calls = {"asked": 0}

    class StubGate(ApprovalGate):
        async def ask(self, tool, args):
            calls["asked"] += 1
            return False  # 模拟用户拒绝

    gate = StubGate(engine, memory)
    # 2026-09-23:bash 已改为 ALLOW(用户要求全放行)——此测试改用 DENY 规则验证拒绝链路
    engine.user_rules.append(Rule("bash", "rm *", DENY))
    assert asyncio.run(gate.resolve("bash", {"command": "rm x"})) is False  # DENY→直接拒
    assert calls["asked"] == 0  # DENY 短路,不进交互
    memory.allow("bash", "ls *")  # 会话记忆:ls 放行
    assert asyncio.run(gate.resolve("bash", {"command": "ls -la"})) is True
    assert calls["asked"] == 0  # bash 本身 ALLOW + 记忆命中,也不进交互


# ── 沙箱 ──────────────────────────────────────────────────────────

def test_make_sandbox_config():
    assert isinstance(make_sandbox("off"), NullSandbox)
    assert isinstance(make_sandbox("on"), SeatbeltSandbox)


def test_seatbelt_available_and_wrap(tmp_path: Path):
    sb = SeatbeltSandbox(root=tmp_path)
    ok, msg = sb.available()
    assert ok, f"macOS 上沙箱应可用:{msg}"
    wrapped = sb.wrap("echo hi")
    assert wrapped.startswith("sandbox-exec -p ") and "echo hi" in wrapped


def test_seatbelt_blocks_write_outside_root(tmp_path: Path):
    """验收'危险命令被拦':沙箱内写 cwd 之外的 home 路径应失败。"""
    import subprocess

    sb = SeatbeltSandbox(root=tmp_path)
    ok, _ = sb.available()
    if not ok:
        pytest.skip("sandbox-exec 不可用")
    outside = Path.home() / ".otter_sandbox_probe.txt"
    proc = subprocess.run(["zsh", "-c", sb.wrap(f"echo x > {outside}")],
                          capture_output=True, timeout=15)
    assert proc.returncode != 0 or not outside.exists()  # 写失败或文件未产生
    inside = tmp_path / "ok.txt"
    proc = subprocess.run(["zsh", "-c", sb.wrap(f"echo x > {inside}")],
                          capture_output=True, timeout=15)
    assert proc.returncode == 0 and inside.exists()  # cwd 内可写


# ── Evidence ──────────────────────────────────────────────────────

def test_evidence_archive_and_read(tmp_path: Path):
    async def scenario() -> None:
        store = Store(db_path=tmp_path / "otter.db")
        await store.open()
        long_text = "\n".join(f"line{i:04d} " + "x" * 20 for i in range(300))  # >阈值
        shortened = await maybe_archive(store, 1, "call1", "bash", long_text)
        assert len(shortened) < len(long_text) and "evidence_read(evidence_id=1)" in shortened
        row = await store.get_evidence(1)
        assert row and row["content"] == long_text  # 原文完好(可逆)
        short = await maybe_archive(store, 1, "call2", "bash", "short")
        assert short == "short"  # 短结果不归档
        await store.close()

    asyncio.run(scenario())


# ── Checkpoint 恢复 ───────────────────────────────────────────────

def test_checkpoint_save_recover_and_reconcile(tmp_path: Path):
    async def scenario() -> None:
        store = Store(db_path=tmp_path / "otter.db")
        await store.open()
        run_id = await store.new_run()
        await store.save_checkpoint(run_id, "原始任务X", "TOOL_EXECUTION",
                                    pending_tools=[{"id": "c1", "name": "bash", "arguments": {"command": "ls"}}])
        cp = await store.latest_unrecovered_checkpoint()
        assert cp and cp["run_id"] == run_id and cp["user_message"] == "原始任务X"
        assert cp["pending_tools"][0]["name"] == "bash"
        msgs = await store.load_run_messages(run_id)
        # reconcile:遗留 running → interrupted;恢复后不再出现在未恢复列表
        assert await store.reconcile_interrupted_runs() == 1
        await store.mark_checkpoint_recovered(run_id)
        assert await store.latest_unrecovered_checkpoint() is None
        # FINISHED 的 Checkpoint 不算可恢复
        run2 = await store.new_run()
        await store.save_checkpoint(run2, "done", "FINISHED")
        assert await store.latest_unrecovered_checkpoint() is None
        await store.close()

    asyncio.run(scenario())


def test_loop_denied_tool_feeds_back(tmp_path: Path, monkeypatch):
    """loop 集成:审批拒绝时工具结果为拒绝文本,模型收到后正常收尾(fail-closed 闭环)。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")

    class FakeAdapter:
        def __init__(self):
            self.script = [
                ModelResponse(content=None, tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "rm a.txt"})]),
                ModelResponse(content="好的,不删了。", usage=ModelUsage(1, 1)),
            ]

        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            return self.script.pop(0)

    class DenyAllGate(ApprovalGate):
        def __init__(self):
            super().__init__(PermissionEngine(rules_file=tmp_path / "no.json"), SessionMemory())
            # 2026-09-23:bash 已改为 ALLOW,此测试要验证拒绝链路,需显式加 DENY 规则
            self.engine.user_rules.append(Rule("bash", None, DENY))

        async def ask(self, tool, args):
            return False

    class MemStore(Store):
        async def append_event(self, *a):
            pass

    async def scenario() -> None:
        store = MemStore(db_path=tmp_path / "otter.db")
        await store.open()
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        registry.register(BashTool())  # 修正(2026-09-22):漏注册 bash 走了"工具不存在"分支,审批分支没被测到
        adapter = FakeAdapter()
        loop = AgentLoop(adapter, registry, store, approval_gate=DenyAllGate())
        result = await loop.run([], Message(role="user", content="删文件"), run_id=1)
        msgs = await store.load_run_messages(1)
        assert result.ok and "拒绝" in msgs[2].content  # tool 消息=拒绝文本
        assert (tmp_path / "a.txt").exists()  # 文件未被删
        await store.close()

    asyncio.run(scenario())
