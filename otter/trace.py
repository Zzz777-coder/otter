"""Trace 分账(v0.3,2026-09-24)——从 durable 事件流构建 Run 级 Usage 账本。

:
- 用独立 agent_events 表 + 富事件模型;otter 已有通用 events 表
  (loop.append_event 落 JSON payload),故不另建表,聚合读 load_run_events;
- 三行账本口径同 :main_agent(MODEL_COMPLETED,含收尾步)/
  context_summary(CONTEXT_COMPACTED,压缩调用"不免费")/
  memory_reflection(MEMORY_REFLECTION)+ provider_total 合计;
- 事件里的 usage 字段自 v0.3 起写入(旧库事件无这些键 → 记 0,历史可分析)。
"""

from __future__ import annotations

from typing import Any

from otter.store import Store


def _u(payload: dict[str, Any], key_in: str, key_out: str) -> tuple[int, int]:
    return (int(payload.get(key_in) or 0), int(payload.get(key_out) or 0))


def summarize_run_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    """纯函数聚合(同名函数角色;离线可测)。

    events=[{type, payload, ...] 来自 store.load_run_events。
    返回账本 dict:main/summary/reflection 三行 + provider_total + 各次数。
    """
    main_in = main_out = 0
    main_calls = 0
    summary_in = summary_out = 0
    summary_calls = 0
    refl_in = refl_out = 0
    refl_calls = 0
    for ev in events:
        etype, payload = ev.get("type", ""), ev.get("payload", {})
        if etype == "MODEL_COMPLETED":
            i, o = _u(payload, "usage_in", "usage_out")
            main_in += i
            main_out += o
            main_calls += 1
        elif etype == "CONTEXT_COMPACTED":
            i, o = _u(payload, "usage_in", "usage_out")
            summary_in += i
            summary_out += o
            summary_calls += 1
        elif etype == "MEMORY_REFLECTION":
            i, o = _u(payload, "usage_in", "usage_out")
            refl_in += i
            refl_out += o
            refl_calls += 1
    return {
        "main_agent": {"input": main_in, "output": main_out, "calls": main_calls},
        "context_summary": {"input": summary_in, "output": summary_out, "calls": summary_calls},
        "memory_reflection": {"input": refl_in, "output": refl_out, "calls": refl_calls},
        "provider_total": {
            "input": main_in + summary_in + refl_in,
            "output": main_out + summary_out + refl_out,
            "calls": main_calls + summary_calls + refl_calls,
        },
    }


def render_ledger(ledger: dict[str, Any]) -> str:
    """账本 → 人读文本(REPL /usage 与 GUI 复用;对齐 Run Detail 呈现口径)。"""
    def row(name: str, key: str) -> str:
        d = ledger[key]
        return f"{name}:{d['input']}in/{d['output']}out({d['calls']} 次)"

    t = ledger["provider_total"]
    return (
        "📊 Run 账本(v0.3 分账)\n"
        f"  主模型     {row('', 'main_agent')}\n"
        f"  上下文压缩 {row('', 'context_summary')}\n"
        f"  记忆反思   {row('', 'memory_reflection')}\n"
        f"  合计       {t['input']}in/{t['output']}out({t['calls']} 次调用)"
    )


async def run_usage_ledger(store: Store, run_id: int) -> dict[str, Any]:
    """读事件流并聚合(异步入口;GUI/REPL 共用)。"""
    events = await store.load_run_events(run_id)
    return summarize_run_usage(events)


__all__ = ["summarize_run_usage", "render_ledger", "run_usage_ledger"]
