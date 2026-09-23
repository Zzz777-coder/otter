"""提示词套件防呆测试(2026-09-23):断言单一来源、关键纪律词、子代理接线。

背景:提示词曾散落 6 个文件且 SUBAGENT_SYSTEM 是死代码(定义了从未注入)。
本测试防止退化:改词时误删纪律段、或 future 重构再次断开 wiring。
"""

from __future__ import annotations

import inspect

from otter import loop, plans, prompts
from otter.subagent import SubagentTool


# ── A. BASE_SYSTEM 五段纪律齐全(vesta 式结构防退化)──────────────

def test_base_system_has_five_disciplines():
    s = prompts.BASE_SYSTEM
    # 五段关键词:身份/工具纪律/上下文意识/产物发布/排版
    assert "你是 otter" in s
    assert "工具纪律" in s and "grep/glob/read_file" in s
    assert "不要凭记忆猜测" in s                    # 反幻觉(vesta 段2 精神)
    assert "上下文意识" in s and "不要凭摘要补造" in s  # 压缩反幻觉(vesta 段4)
    assert "产物发布" in s and "artifact_publish" in s
    assert "中间文件、临时文件不要发布" in s           # Fable 正反例清单(负面清单)
    assert "排版" in s and "本质结论" in s             # 用户 2026-09-22 定版保留


def test_base_system_decision_examples_and_scaling():
    """Fable 5 技术:决策示例 + 工具量化 Scaling 必须在工具纪律段。"""
    s = prompts.BASE_SYSTEM
    assert "判断示例" in s and "→直接回答" in s       # 决策示例(examples)
    assert "次数与任务复杂度匹配" in s                 # Scaling(core_search_behaviors #2)


# ── B. 场景注入齐全且格式正确 ─────────────────────────────────────

def test_situational_prompts_nonempty():
    for name in ("PLAN_DIRECTIVE", "SUBAGENT_SYSTEM", "SUMMARY_PROMPT",
                 "REFLECT_PROMPT", "DISTILL_PROMPT", "MAX_STEPS_FINAL_MESSAGE",
                 "EDIT_HINT_WHOLE", "EDIT_HINT_DIFF"):
        assert getattr(prompts, name, "").strip(), f"prompts.{name} 缺失或为空"


def test_plan_directive_keeps_contract():
    """计划结构契约(## 步骤等五小节)是 plan_is_valid 的依据,不可误删。"""
    d = prompts.PLAN_DIRECTIVE
    for section in ("## 目标", "## 步骤", "## 验收标准", "## 风险"):
        assert section in d, f"PLAN_DIRECTIVE 丢了契约小节 {section}"


def test_budget_and_plan_note_formatting():
    assert "100" in prompts.budget_warning(100, 200) and "200" in prompts.budget_warning(100, 200)
    assert "收口" in prompts.budget_finalizing(100, 200)
    assert "3" in prompts.budget_hard_report(100, 200, 3) and "OTTER_RUN_BUDGET" in prompts.budget_hard_report(1, 2, 3)
    note = prompts.approved_plan_note("计划A")
    assert "<approved_plan>" in note and "计划A" in note and "不要擅自偏离" in note


# ── C. 单一来源与接线(防"死提示词"复发)─────────────────────────

def test_loop_uses_prompts_single_source():
    """loop.py 不再自带 BASE_SYSTEM 文案,引用必须来自 prompts(同一对象)。"""
    assert loop.BASE_SYSTEM is prompts.BASE_SYSTEM


def test_plans_reexports_plan_directive():
    assert plans.PLAN_DIRECTIVE is prompts.PLAN_DIRECTIVE


def test_agentloop_supports_base_system_override():
    """子代理覆盖入口:AgentLoop 必须接受 base_system 并在 _build_view 生效。"""
    sig = inspect.signature(loop.AgentLoop.__init__)
    assert "base_system" in sig.parameters
    # 生效性:_build_view 首段应是覆盖文本(adapter=None 时 getattr 安全,不触库)
    from otter.models.types import Message

    lp = loop.AgentLoop(None, None, None, base_system="XX-子代理提示-XX")
    view = lp._build_view([Message(role="user", content="hi")], loop.SummaryState())
    assert view[0].content.startswith("XX-子代理提示-XX")


def test_subagent_system_actually_wired():
    """2026-09-23 修复的 wiring:SubagentTool.run 必须把 SUBAGENT_SYSTEM 传给 AgentLoop。
    防复发断言:源码里找得到接线(此前定义了但从未注入,子代理纪律失效)。"""
    src = inspect.getsource(SubagentTool.run)
    assert "base_system=SUBAGENT_SYSTEM" in src


# ── D. 内部提示保持结构化 JSON 设计(搬家不改内容的守卫)───────────

def test_internal_prompts_keep_json_contracts():
    assert '"current_objective"' in prompts.SUMMARY_PROMPT          # 摘要必填首字段
    assert '{"action":"none"}' in prompts.REFLECT_PROMPT            # 反思三动作
    assert "memory_write" in prompts.REFLECT_PROMPT                 # 已写过则 none(防重复)
    assert "宁缺毋滥" in prompts.DISTILL_PROMPT                     # 提炼保守性
