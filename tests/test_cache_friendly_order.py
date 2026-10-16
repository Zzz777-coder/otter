# -*- coding: utf-8 -*-
"""2026-10-15 缓存友好组装的顺序不变式测试。

背景:DeepSeek/OpenAI 的自动缓存与 Anthropic 的显式 cache_control 命中前提都是
"请求前缀逐字节一致"。_build_view 各注入段按变化频率排序(会话稳定段前置、
Step 级易变段后置)。顺序回归不会有功能测试失败——只会缓存命中率静默下跌、
成本静默上涨,所以用本测试锁死相对顺序。
"""
from types import SimpleNamespace

from otter.loop import AgentLoop
from otter.tools.base import ToolRegistry


def _make_loop(**attrs) -> AgentLoop:
    """最小可构造 AgentLoop(不发请求,只调 _build_view;adapter/store 用占位对象)。"""
    loop = AgentLoop(adapter=object(), registry=ToolRegistry(), store=object(),
                     instructions="AGENTS_MARKER")
    for k, v in attrs.items():
        setattr(loop, k, v)
    return loop


def test_build_view_cache_friendly_order():
    """不变式:BASE < AGENTS.md < Core记忆 < 技能 < 采纳计划 < 滚动摘要 < 任务快照 < 召回cue < 预算话术。
    变化频率:进程级/会话级段在前,Run 级段居中,Step 级段(任务快照/预算)垫底。"""
    loop = _make_loop(
        memory_bundle=(object(), SimpleNamespace(render=lambda: "COREMEMO_MARKER"), object()),
        _skills_brief="SKILLS_MARKER",
        _plan_context="PLANCTX_MARKER",
        _task_context_text="TASKCTX_MARKER",
        _recall_text="RECALL_MARKER",
        _budget_hint="BUDGET_MARKER",
    )
    state = SimpleNamespace(summary=SimpleNamespace(render=lambda: "SUMMARY_MARKER"), covered=0)
    system_text = loop._build_view([], state)[0].content

    order = ["AGENTS_MARKER", "COREMEMO_MARKER", "SKILLS_MARKER", "PLANCTX_MARKER",
             "SUMMARY_MARKER", "TASKCTX_MARKER", "RECALL_MARKER", "BUDGET_MARKER"]
    positions = [system_text.index(m) for m in order]
    assert positions == sorted(positions), f"注入段顺序破坏缓存前缀: {positions}"
    # BASE_SYSTEM 永远在最前(身份段是前缀的起点)
    assert system_text.index("COREMEMO_MARKER") > 0


def test_build_view_stable_prefix_without_volatile_segments():
    """无易变段时(Run 开始/无任务无预算),system 只含稳定段——前缀跨 Step 逐字节一致。"""
    loop = _make_loop(instructions="AGENTS_MARKER")
    state = SimpleNamespace(summary=None, covered=0)
    text_a = loop._build_view([], state)[0].content
    text_b = loop._build_view([], state)[0].content
    assert text_a == text_b and "TASKCTX" not in text_a and "BUDGET" not in text_a
