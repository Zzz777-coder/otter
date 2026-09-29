"""巡逻(bug 监测第 1+2 层)测试:失败判据 / 同因去重 / 时间窗 / CLI 入口 / 通知容错。

运行:python -m pytest tests/test_patrol.py -v(asyncio.run 手动驱动,不依赖插件)
2026-09-29 建立:TOOL_COMPLETED.ok 字段与巡逻判据同批落地。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from otter import patrol
from otter.store import Store


def _mkstore(tmp_path: Path) -> Store:
    """真 Store(临时库):判据测试走真实 events/runs 读写路径。"""

    async def go() -> Store:
        # 库路径必须与 run_patrol 的取数口径一致(<workspace>/.otter/otter.db)
        s = Store(tmp_path / ".otter" / "otter.db")
        await s.open()
        return s

    return asyncio.run(go())


def _seed_run(store: Store, *, status: str = "completed", stop: str = "final_answer",
              parent: int | None = None, events: list[tuple[str, dict]] | None = None) -> int:
    """造一个 Run + 事件,返回 run_id。"""

    async def go() -> int:
        rid = await store.new_run(parent_run_id=parent)
        for t, p in events or []:
            await store.append_event(rid, t, p)
        await store.finish_run(rid, status, stop)
        return rid

    return asyncio.run(go())


def _run_row(store: Store, rid: int) -> dict:
    """scan_run 需要 runs 行;从库取(scan_recent 同款取数)。"""

    async def go() -> dict:
        cur = await store._db.execute(
            "SELECT id, status, stop_reason, started_at, parent_run_id FROM runs WHERE id=?",
            (rid,))
        r = await cur.fetchone()
        return {"id": r[0], "status": r[1], "stop_reason": r[2],
                "started_at": r[3], "parent_run_id": r[4]}

    return asyncio.run(go())


def _scan(store: Store, rid: int) -> list[patrol.Finding]:
    return asyncio.run(patrol.scan_run(store, _run_row(store, rid)))


# ── 判据逐条 ──────────────────────────────────────────────────────────

def test_clean_run_no_findings(tmp_path):
    s = _mkstore(tmp_path)
    rid = _seed_run(s)  # completed/final_answer,零事件
    assert _scan(s, rid) == []


def test_cancelled_user_intent_not_reported(tmp_path):
    s = _mkstore(tmp_path)
    rid = _seed_run(s, status="cancelled", stop="cancelled")
    assert _scan(s, rid) == []  # 用户主动取消不是失败信号


def test_failed_and_model_error_are_high(tmp_path):
    s = _mkstore(tmp_path)
    rid = _seed_run(s, status="failed", stop="model_error",
                    events=[("MODEL_ERROR", {"error": "连接超时boom"})])
    kinds = {(f.kind, f.severity) for f in _scan(s, rid)}
    assert ("failed", "high") in kinds and ("model_error", "high") in kinds
    assert any("连接超时" in f.detail for f in _scan(s, rid))


def test_interrupted_medium(tmp_path):
    s = _mkstore(tmp_path)
    rid = _seed_run(s, status="interrupted", stop="interrupted")
    f = _scan(s, rid)
    assert len(f) == 1 and f[0].severity == "medium" and f[0].kind == "interrupted"


def test_budget_event_and_stop_reason_dedupe(tmp_path):
    s = _mkstore(tmp_path)
    # 同因两条来源(事件 + stop_reason)只报一条,避免一条故障刷两行
    rid = _seed_run(s, stop="run_budget_exceeded",
                    events=[("RUN_BUDGET_EXCEEDED", {"used": 300000, "budget": 300000})])
    f = [x for x in _scan(s, rid) if x.kind == "budget"]
    assert len(f) == 1 and f[0].severity == "medium"


def test_denied_storm_threshold(tmp_path):
    s = _mkstore(tmp_path)
    two = _seed_run(s, events=[("TOOL_DENIED", {"name": "bash"})] * 2)
    three = _seed_run(s, events=[("TOOL_DENIED", {"name": "bash"})] * 3)
    assert not [x for x in _scan(s, two) if x.kind == "denied_storm"]
    assert [x for x in _scan(s, three) if x.kind == "denied_storm"]  # ≥3 才算风暴


def test_protocol_retry_threshold(tmp_path):
    s = _mkstore(tmp_path)
    rid = _seed_run(s, events=[("MODEL_EMPTY_RETRY", {}), ("MODEL_TEXTUAL_RETRY", {})])
    assert any(f.kind == "protocol_retry" for f in _scan(s, rid))


def test_tool_failures_low_and_legacy_ok_absent(tmp_path):
    s = _mkstore(tmp_path)
    # 3 次 ok=False → low;老事件(无 ok 字段)不计入(兼容历史库)
    rid = _seed_run(s, events=[
        ("TOOL_COMPLETED", {"step": 1, "name": "write_file", "ok": False}),
        ("TOOL_COMPLETED", {"step": 2, "name": "edit_file", "ok": False}),
        ("TOOL_COMPLETED", {"step": 3, "name": "bash", "ok": False}),
        ("TOOL_COMPLETED", {"step": 4, "name": "bash", "result_chars": 10}),  # 老格式
    ])
    f = [x for x in _scan(s, rid) if x.kind == "tool_failures"]
    assert len(f) == 1 and f[0].severity == "low"


def test_max_steps_informational_low(tmp_path):
    s = _mkstore(tmp_path)
    rid = _seed_run(s, stop="max_steps")
    f = _scan(s, rid)
    assert len(f) == 1 and f[0].kind == "max_steps" and f[0].severity == "low"


def test_subagent_run_prefixed(tmp_path):
    s = _mkstore(tmp_path)
    parent = _seed_run(s)
    rid = _seed_run(s, status="interrupted", stop="interrupted", parent=parent)
    f = _scan(s, rid)
    assert f and f[0].is_subagent and f[0].detail.startswith("[子代理]")


# ── 时间窗 / 批量扫描 ────────────────────────────────────────────────

def test_recent_runs_window(tmp_path):
    s = _mkstore(tmp_path)
    rid = _seed_run(s)

    async def age_it() -> None:  # 把 started_at 拨回 48h 前,滑出 24h 窗口
        await s._db.execute("UPDATE runs SET started_at = started_at - 48*3600 WHERE id=?", (rid,))
        await s._db.commit()

    asyncio.run(age_it())
    runs = asyncio.run(s.recent_runs(24.0))
    assert runs == []
    assert asyncio.run(s.recent_runs(72.0))  # 拉大窗口又能看到


def test_scan_recent_aggregates(tmp_path):
    s = _mkstore(tmp_path)
    _seed_run(s)  # 干净
    bad = _seed_run(s, status="failed", stop="model_error",
                    events=[("MODEL_ERROR", {"error": "x"})])
    findings, scanned = asyncio.run(patrol.scan_recent(s, 24.0))
    assert scanned == 2 and {f.run_id for f in findings} == {bad}


# ── 第 2 层:报告渲染 / 通知容错 / CLI 入口 ──────────────────────────

def test_render_report_clean_and_grouped():
    clean = patrol.render_report([], 5, 24.0)
    assert "无失败信号" in clean and "5" in clean
    f = patrol.render_report(
        [patrol.Finding(1, "failed", "high", "Run #1 失败"),
         patrol.Finding(2, "max_steps", "low", "Run #2 截停")], 2, 24.0)
    assert "high" in f and "low" in f and "2 条信号" in f


def test_notify_macos_silent_on_failure(monkeypatch):
    # osascript 不存在/报错时静默返回 False,绝不抛出(通知不炸巡逻)
    monkeypatch.setattr(patrol.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no osascript")))
    assert patrol.notify_macos("t", "b") is False


def test_run_patrol_no_db_returns_zero(tmp_path):
    assert patrol.run_patrol(hours=24.0, workspace=tmp_path, notify=False) == 0


def test_run_patrol_high_returns_one(tmp_path, capsys, monkeypatch):
    s = _mkstore(tmp_path)
    _seed_run(s, status="failed", stop="model_error")
    asyncio.run(s.close())
    # 桌面通知走 mock(测试环境不真弹)
    monkeypatch.setattr(patrol, "notify_macos", lambda *a, **k: True)
    code = patrol.run_patrol(hours=24.0, workspace=tmp_path, notify=True)
    out = capsys.readouterr().out
    assert code == 1 and "high" in out and "model_error" in out


def test_run_patrol_clean_returns_zero(tmp_path, capsys):
    s = _mkstore(tmp_path)
    _seed_run(s)
    asyncio.run(s.close())
    assert patrol.run_patrol(hours=24.0, workspace=tmp_path, notify=False) == 0
    assert "无失败信号" in capsys.readouterr().out
