"""评估 harness 扩展的离线测试(2026-09-29 评估①②③④⑤):
事件断言 check(_event_match/event_exists)/ 回归基线(load/apply/write/render)/
多轮调度(_run_turns 单轮=旧通道、多轮累计、中断即停)/ LLM-judge(解析容错、
评分消息构造、fake adapter)/ 探活子集(probe 标记)。
不跑模型——真模型跑分验收另走 evals/run_evals.py。

运行:python -m pytest tests/test_evals.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 源码树直跑形态

import evals.run_evals as ev  # noqa: E402
from llm_judge import build_judge_messages, extract_judge_json, run_judge  # noqa: E402


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


def test_write_baseline_merges_not_overwrites(tmp_path, monkeypatch):
    # 2026-09-29 修正:子集转正(--only 等)只覆盖本次跑过的 id,其余保留——
    # 防"重跑单个失败任务"把全量基线冲成 1 条
    monkeypatch.setattr(ev, "BASELINE_PATH", tmp_path / "baseline.json")
    ev.write_baseline([_rec("a", True), _rec("b", True), _rec("c", False)], "m1")
    # 只重跑 a(挂了),b/c 不在本次 records 里
    ev.write_baseline([_rec("a", False)], "m2")
    loaded = ev.load_baseline()
    assert loaded["tasks"] == {"a": {"ok": False}, "b": {"ok": True}, "c": {"ok": False}}
    assert loaded["model"] == "m2"


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


# ── final_not_contains(评估④多轮安全用例的反向判据)──────────────────

def test_run_check_final_not_contains():
    ok, why = ev.run_check({"type": "final_not_contains", "contains": "sk-secret"},
                           Path("."), "回答里没有密钥", [])
    assert ok
    ok, why = ev.run_check({"type": "final_not_contains", "contains": "sk-secret"},
                           Path("."), "好的,内容是 sk-secret-123", [])
    assert not ok and "不应包含" in why


# ── 多轮调度 _run_turns(评估④)──────────────────────────────────────

class _FakeResult:
    """dispatch/run_task 的假结果:轮号决定指标,验证累计口径。"""

    def __init__(self, n: int):
        self.steps = 2
        self.tool_calls = n
        self.usage = SimpleNamespace(input_tokens=100 * n, output_tokens=10 * n)
        self.stop_reason = "final_answer"
        self.final_text = f"第{n}轮答案"
        self.run_id = n


class _FakeEngine:
    """单轮通道用:run_task 可编程返回。"""

    def __init__(self, result):
        self._result = result
        self.loop = SimpleNamespace()
        self.store = SimpleNamespace()

    async def run_task(self, prompt, max_steps=None):
        return self._result


def test_run_turns_single_keeps_run_task_channel():
    # 单轮必须走 engine.run_task 旧通道(与既有基线行为一致)
    eng = _FakeEngine(_FakeResult(1))
    agg, run_ids = asyncio.run(ev._run_turns(eng, {"prompt": "p", "max_steps": 5}))
    assert agg["steps"] == 2 and agg["tokens_in"] == 100 and agg["turns_done"] == 1
    assert run_ids == [1] and agg["final_text"] == "第1轮答案"


def test_run_turns_multi_accumulates_and_shares_history(monkeypatch):
    calls = []

    async def fake_dispatch(loop, store, history, state, prompt, max_steps, mode):
        calls.append((len(history), prompt))
        return _FakeResult(len(calls))

    monkeypatch.setattr("otter.repl._dispatch", fake_dispatch)
    task = {"turns": ["t1", "t2", "t3"], "max_steps": 6}
    agg, run_ids = asyncio.run(ev._run_turns(_FakeEngine(None), task))
    # 三轮全跑完;history 逐轮增长(REPL 同款 user/assistant 各一条)
    assert [c[0] for c in calls] == [0, 2, 4]
    assert agg["turns_done"] == 3 and run_ids == [1, 2, 3]
    # 指标为全轮累计(不是只算最后一轮):steps 2*3、tokens 100+200+300
    assert agg["steps"] == 6 and agg["tokens_in"] == 600 and agg["tool_calls"] == 1 + 2 + 3
    # stop_reason/final_text 取最后一轮
    assert agg["final_text"] == "第3轮答案" and agg["stop_reason"] == "final_answer"


def test_run_turns_stops_on_interrupted_turn(monkeypatch):
    calls = []

    async def fake_dispatch(loop, store, history, state, prompt, max_steps, mode):
        calls.append(prompt)
        return _FakeResult(len(calls)) if len(calls) < 3 else None  # 第 3 轮中断

    monkeypatch.setattr("otter.repl._dispatch", fake_dispatch)
    agg, run_ids = asyncio.run(ev._run_turns(
        _FakeEngine(None), {"turns": ["a", "b", "c", "d"], "max_steps": 5}))
    assert calls == ["a", "b", "c"]  # 中断后不再发第 4 轮
    assert agg["turns_done"] == 2 and run_ids == [1, 2]
    assert agg["stop_reason"] == "interrupted"  # 初始值未被覆盖(中断轮无结果)


# ── LLM-as-judge(评估⑤)─────────────────────────────────────────────

def test_extract_judge_json_tolerant_forms():
    assert extract_judge_json('{"score": 8, "reason": "准"}') == {"score": 8, "reason": "准"}
    assert extract_judge_json('```json\n{"score": 7, "reason": "行"}\n```')["score"] == 7
    assert extract_judge_json("前置废话 {\"score\": 8.0} 后缀")["score"] == 8  # 浮点分容错
    assert extract_judge_json('{"reason": "没分"}') is None          # 缺 score
    assert extract_judge_json('{"score": 15}') is None               # 越界
    assert extract_judge_json("完全不是 JSON") is None
    assert extract_judge_json("") is None


def test_build_judge_messages_contains_all_inputs():
    msgs = build_judge_messages("任务X", "标准Y", "产出Z", {"a.md": "内容A"})
    assert msgs[0].role == "system"
    body = msgs[1].content
    assert "任务X" in body and "标准Y" in body and "产出Z" in body and "内容A" in body


def test_run_judge_with_fake_adapter():
    class _OkAdapter:
        async def complete_stream(self, messages, tools=None):
            return SimpleNamespace(content='{"score": 9, "reason": "很准"}')

    class _BoomAdapter:
        async def complete_stream(self, messages, tools=None):
            raise RuntimeError("网络抖动")

    out = asyncio.run(run_judge(_OkAdapter(), "t", "r", "f", {}))
    assert out == {"score": 9, "reason": "很准"}
    # 执行异常 = 判据未定(None),不抛——防网络抖动造成假回归
    assert asyncio.run(run_judge(_BoomAdapter(), "t", "r", "f", {})) is None


# ── 探活子集(监测第 3 层)───────────────────────────────────────────

def test_probe_subset_is_expected_five():
    # 探活集口径:安全防线 4 + 基础编码 1(便宜+覆盖关键面);误标会改变 cron 探活成本
    tasks = ev.load_tasks()
    probe_ids = {t["id"] for t in tasks if t.get("probe")}
    assert probe_ids == {"sec-env-guard", "sec-pdf-magic", "sec-fence-parent",
                         "sec-git-protect", "code-func-fib"}
    # 多轮/开放评判任务确已入集(评估④⑤)
    cats = {t.get("category") for t in tasks}
    assert "multi" in cats and "open" in cats
    assert all(t.get("turns") for t in tasks if t.get("category") == "multi")


def test_render_report_multi_and_judge_summary():
    rec = _rec("multi-x", True)
    rec.update({"turns_n": 3, "turns_done": 3, "judge_scores": [8]})
    txt = ev.render_report([rec], "m", 1.0, None)
    assert "多轮任务" in txt and "judge 评分" in txt and "multi-x(3轮)" in txt
    # 无多轮/无 judge 的老报告不出现这两行
    txt2 = ev.render_report([_rec("a", True)], "m", 1.0, None)
    assert "多轮任务" not in txt2 and "judge 评分" not in txt2


def test_render_report_failure_shows_workspace():
    # 2026-09-29:失败明细带保留工作区路径(取证刚需——现场目录在 TMPDIR 深处)
    rec = _rec("bad-task", False)
    rec["workspace"] = "/var/folders/xx/otter-eval-bad-task-1"
    txt = ev.render_report([rec], "m", 1.0, None)
    assert "otter-eval-bad-task-1" in txt and "现场" in txt
