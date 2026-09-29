"""评估 harness 扩展的离线测试(2026-09-29 评估①②③):
事件断言 check(_event_match/event_exists)/ 回归基线(load/apply/write/render)。
不跑模型——真模型跑分验收另走 evals/run_evals.py。

运行:python -m pytest tests/test_evals.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 源码树直跑形态

import evals.run_evals as ev  # noqa: E402


EV = [
    {"type": "TOOL_STARTED", "payload": {"step": 2, "name": "dispatch",
                                         "arguments": {"write": "none", "task": "看看"}}},
    {"type": "TOOL_COMPLETED", "payload": {"step": 2, "name": "dispatch", "ok": True}},
]


# ── 事件断言 check(评估③的机器判据)────────────────────────────────

def test_event_match_by_type_and_payload():
    ok, _ = ev._event_match({"event": "TOOL_STARTED", "payload_in": {"name": "dispatch"}}, EV)
    assert ok
    ok, _ = ev._event_match({"event": "TOOL_STARTED"}, EV)  # 只断类型
    assert ok


def test_event_match_miss_on_payload_or_type():
    assert not ev._event_match({"event": "TOOL_STARTED", "payload_in": {"name": "bash"}}, EV)[0]
    assert not ev._event_match({"event": "RUN_STOPPED"}, EV)[0]


def test_run_check_event_exists_and_not_exists(tmp_path):
    hit = {"type": "event_exists", "event": "TOOL_STARTED", "payload_in": {"name": "dispatch"}}
    ok, why = ev.run_check(hit, tmp_path, "", EV)
    assert ok
    ok, _ = ev.run_check({"type": "event_not_exists", "event": "RUN_STOPPED"}, tmp_path, "", EV)
    assert ok  # 不该发生的没发生=通过
    ok, why = ev.run_check({"type": "event_not_exists", "event": "TOOL_STARTED"}, tmp_path, "", EV)
    assert not ok and "不应发生" in why  # 不该发生的发生了=拒绝


def test_run_check_unknown_type_still_fails(tmp_path):
    ok, why = ev.run_check({"type": "nope"}, tmp_path, "", [])
    assert not ok and "未知" in why


# ── 回归基线(评估①)───────────────────────────────────────────────

def _rec(rid: str, ok: bool) -> dict:
    return {"id": rid, "category": "coding", "ok": ok, "fail_detail": ["x"], "latency_s": 1}


def test_baseline_roundtrip_and_regression_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "BASELINE_PATH", tmp_path / "baseline.json")
    ev.write_baseline([_rec("a", True), _rec("b", False)], "m1")
    loaded = ev.load_baseline()
    assert loaded["model"] == "m1" and loaded["tasks"] == {"a": {"ok": True}, "b": {"ok": False}}

    # 本次:a 过→持平;b 挂→挂(持平);c 过→新;c 挂→新;d 挂→回归
    now = [_rec("a", True), _rec("b", False), _rec("c", True), _rec("d", False)]
    # 注:d 不在基线,属 is_new;回归只对"基线过→本次挂"
    regs = ev.apply_baseline(now, loaded)
    assert regs == []
    by_id = {r["id"]: r for r in now}
    assert by_id["c"].get("is_new") and not by_id["a"].get("regression")

    # 基线全过,本次 a 挂 → 回归
    ev.write_baseline([_rec("a", True)], "m1")
    regs = ev.apply_baseline([_rec("a", False)], ev.load_baseline())
    assert len(regs) == 1 and regs[0]["id"] == "a" and regs[0]["regression"]


def test_load_baseline_missing_or_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "BASELINE_PATH", tmp_path / "none.json")
    assert ev.load_baseline()["tasks"] == {}
    p = tmp_path / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(ev, "BASELINE_PATH", p)
    assert ev.load_baseline()["tasks"] == {}  # 损坏不炸,按无基线处理


def test_render_report_regression_section():
    base = {"model": "m1", "updated": "2026-09-29 10:00", "tasks": {"a": {"ok": True}}}
    rec = _rec("a", False)
    rec["regression"] = True
    rec["baseline_prev"] = True
    txt = ev.render_report([rec], "m2", 3.0, base)
    assert "回归对比" in txt and "回归 1" in txt and "`a`" in txt
    # 无基线时不出现该节
    txt2 = ev.render_report([_rec("a", True)], "m2", 3.0, None)
    assert "回归对比" not in txt2
