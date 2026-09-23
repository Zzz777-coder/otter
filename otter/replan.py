"""Replan 领域工具包(M4 示范,说明书 P2/8 章;绑 KAIST 申请主线的核心演示)。

场景:生产重计划——急单插入/设备故障/物料延误下,对一组工单做人工重计划。
数据:模拟车间(5 台机器 × 12 张工单),确定性生成,可复现。
设计要点:otter 的"领域扩展底座"叙事——同一个 CLI,装入 replan 工具包
即变身为领域 Agent;工具面=get_schedule(读)/simulate_change(沙盘)/
commit_reschedule(提交),与编码场景的 read/edit/bash 同构(读-试-写三分)。
Skill Learning 从重计划 Trace 提炼启发式规则 = KAIST 叙事的技术证明。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from otter.tools.base import Tool

STATE_PATH = Path.cwd() / ".otter" / "replan_state.json"

MACHINES = ["CNC-01", "CNC-02", "SMT-A", "SMT-B", "ASSY-1"]
PRODUCTS = {"P1": 40, "P2": 65, "P3": 90}  # 单件工时(分钟)


@dataclass
class Order:
    id: str
    product: str
    qty: int
    due: str          # 交期 HH:MM
    prio: int         # 1=急单 2=普通 3=可延
    machine: str
    start: str        # 计划开始 HH:MM
    status: str = "scheduled"   # scheduled / moved / dropped


def _seed_orders() -> list[Order]:
    base = [
        ("WO-101", "P1", 30, "14:00", 2, "CNC-01", "08:00"),
        ("WO-102", "P2", 20, "15:00", 3, "CNC-02", "08:00"),
        ("WO-103", "P3", 10, "12:00", 2, "SMT-A", "08:00"),
        ("WO-104", "P1", 25, "17:00", 3, "CNC-01", "11:00"),
        ("WO-105", "P2", 15, "13:00", 2, "SMT-B", "08:00"),
        ("WO-106", "P3", 12, "18:00", 3, "SMT-A", "10:00"),
        ("WO-107", "P1", 20, "16:00", 2, "CNC-02", "10:00"),
        ("WO-108", "P2", 10, "11:00", 3, "SMT-B", "10:30"),
        ("WO-109", "P3", 8,  "14:30", 2, "ASSY-1", "08:00"),
        ("WO-110", "P1", 15, "19:00", 3, "ASSY-1", "09:00"),
        ("WO-111", "P2", 18, "12:30", 2, "CNC-01", "14:00"),
        ("WO-112", "P3", 6,  "16:30", 3, "CNC-02", "13:00"),
    ]
    return [Order(*row) for row in base]


def _load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    state = {"orders": [asdict(o) for o in _seed_orders()], "machines": MACHINES,
             "events": [], "committed": 0}
    _save_state(state)
    return state


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def reset_replan() -> str:
    STATE_PATH.unlink(missing_ok=True)
    _load_state()
    return "[otter] 重计划现场已重置为初始排程(5 机器 × 12 工单)"


def _fmt_minutes(m: int) -> str:
    return f"{8 + m // 60:02d}:{m % 60:02d}"


def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return (int(h) - 8) * 60 + int(m)


def _load_minutes(o: dict) -> int:
    return round(o["qty"] * PRODUCTS[o["product"]] / 10)  # 压缩时间轴:分钟/10


def _schedule_table(state: dict) -> str:
    lines = ["机器     工单     产品 数量  开始   交期   优先级 状态"]
    for o in sorted(state["orders"], key=lambda x: (x["machine"], x["start"])):
        lines.append(f'{o["machine"]:<8} {o["id"]:<7} {o["product"]:<3} {o["qty"]:>3}  '
                     f'{o["start"]:<5} {o["due"]:<5} P{o["prio"]}    {o["status"]}')
    if state.get("events"):
        lines.append("\n扰动事件:" + ";".join(state["events"]))
    return "\n".join(lines)


class GetScheduleTool(Tool):
    name = "get_schedule"
    description = "读取当前车间排程(5 机器 × 12 工单:工单/产品/数量/开始/交期/优先级),以及已发生的扰动事件。"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any]) -> str:
        return _schedule_table(_load_state())


class SimulateChangeTool(Tool):
    """沙盘:注入扰动或试排,返回后果评估(延误加权/违反交期数),不落盘。"""

    name = "simulate_change"
    description = (
        "重计划沙盘:注入扰动或调整工单,评估后果但不落盘。"
        "action=disrupt 急单插入(product/qty/due);action=move 移动工单(order/machine/new_start);"
        "action=fault 机器故障(machine/duration_min)。返回延误加权分与违反交期数。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["disrupt", "move", "fault"]},
            "order": {"type": "string", "description": "move 时:工单号"},
            "machine": {"type": "string", "description": "move 目标机器 / fault 故障机器"},
            "new_start": {"type": "string", "description": "move 时:新开始 HH:MM"},
            "duration_min": {"type": "integer", "description": "fault 时:停机分钟(压缩轴)"},
            "product": {"type": "string", "description": "disrupt 时:P1/P2/P3"},
            "qty": {"type": "integer", "description": "disrupt 时:数量"},
            "due": {"type": "string", "description": "disrupt 时:交期 HH:MM"},
        },
        "required": ["action"],
    }

    async def run(self, args: dict[str, Any]) -> str:
        state = _load_state()
        orders = [dict(o) for o in state["orders"]]  # 深拷贝=沙盘
        action = args.get("action")
        if action == "disrupt":
            new_id = f"WO-{200 + len(state.get('events', []))}"
            orders.append({"id": new_id, "product": args.get("product", "P1"),
                          "qty": int(args.get("qty", 10)), "due": args.get("due", "13:00"),
                          "prio": 1, "machine": "待排", "start": "?", "status": "urgent"})
            note = f"急单 {new_id}({args.get('product')}×{args.get('qty')},交期 {args.get('due')})——请在 commit 中为它排产"
        elif action == "move":
            for o in orders:
                if o["id"] == args.get("order"):
                    o["machine"], o["start"] = args.get("machine", o["machine"]), args.get("new_start", o["start"])
                    note = f'{o["id"]} → {o["machine"]}@{o["start"]}'
                    break
            else:
                return f'[otter] 工单 {args.get("order")} 不存在'
        elif action == "fault":
            m, dur = args.get("machine", ""), int(args.get("duration_min", 60))
            for o in orders:
                if o["machine"] == m:
                    o["start"] = _fmt_minutes(_to_min(o["start"]) + dur)
            note = f"{m} 故障停机 {dur} 分钟(压缩轴),其上工单顺延"
        else:
            return "[otter] 未知 action"
        score, violations = _evaluate(orders)
        return (f"[沙盘 · 不落盘] {note}\n延误加权分:{score}(越低越好)· 违反交期:{violations} 单\n"
                f"提示:用 commit_reschedule 提交同样的变更序列才会生效")


class CommitRescheduleTool(Tool):
    name = "commit_reschedule"
    description = (
        "提交重计划决策使之生效(与 simulate 同参数,但落盘)。"
        "每次提交会记录事件;提交后用 get_schedule 查看新排程。"
    )
    parameters = SimulateChangeTool.parameters

    async def run(self, args: dict[str, Any]) -> str:
        state = _load_state()
        sim = SimulateChangeTool()
        preview = await sim.run(args)  # 复用沙盘逻辑做前置校验
        if "[otter]" in preview.splitlines()[0]:
            return preview  # 参数错误直接透传
        action = args.get("action")
        if action == "disrupt":
            new_id = f"WO-{200 + len(state.get('events', []))}"
            machine = _pick_machine(state, args)
            start = _earliest_slot(state, machine)
            state["orders"].append({"id": new_id, "product": args.get("product", "P1"),
                                    "qty": int(args.get("qty", 10)), "due": args.get("due", "13:00"),
                                    "prio": 1, "machine": machine, "start": start, "status": "urgent"})
            state.setdefault("events", []).append(f"急单{new_id}→{machine}@{start}")
        elif action == "move":
            for o in state["orders"]:
                if o["id"] == args.get("order"):
                    o["machine"], o["start"], o["status"] = args.get("machine", o["machine"]), args.get("new_start", o["start"]), "moved"
                    state.setdefault("events", []).append(f'{o["id"]}→{o["machine"]}@{o["start"]}')
        elif action == "fault":
            m, dur = args.get("machine", ""), int(args.get("duration_min", 60))
            for o in state["orders"]:
                if o["machine"] == m:
                    o["start"] = _fmt_minutes(_to_min(o["start"]) + dur)
            state.setdefault("events", []).append(f"{m}故障{dur}min")
        state["committed"] = state.get("committed", 0) + 1
        _save_state(state)
        score, violations = _evaluate(state["orders"])
        return f"[已提交 · 第 {state['committed']} 次] 延误加权 {score} · 违反交期 {violations} 单\n" + _schedule_table(state)


def _pick_machine(state: dict, args: dict) -> str:
    """确定性选机:同类产品最少负载的机器(启发式,可被 Skill 学习改进的点)。"""
    product = args.get("product", "P1")
    load = {m: 0 for m in MACHINES}
    for o in state["orders"]:
        if o["machine"] in load:
            load[o["machine"]] += _load_minutes(o)
    return min(load, key=load.get)


def _earliest_slot(state: dict, machine: str) -> str:
    starts = [_to_min(o["start"]) for o in state["orders"] if o["machine"] == machine and o["start"] != "?"]
    return _fmt_minutes(max(starts) + 10 if starts else 0)


def _evaluate(orders: list[dict]) -> tuple[int, int]:
    """延误加权分 = Σ 超交期分钟 × 优先级权重(1急=3/2普=2/3可延=1);违反交期数。"""
    weight = {1: 3, 2: 2, 3: 1}
    score = violations = 0
    for o in orders:
        if o["status"] == "dropped" or o["start"] in ("?",):
            continue
        end = _to_min(o["start"]) + _load_minutes(o)
        late = max(0, end - _to_min(o["due"]))
        if late > 0:
            violations += 1
            score += late * weight.get(o["prio"], 1)
    return score, violations


def build_replan_tools() -> list[Tool]:
    return [GetScheduleTool(), SimulateChangeTool(), CommitRescheduleTool()]
