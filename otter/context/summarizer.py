"""滚动摘要(M1 核心件)。

核心思想(全部自行实现):
- 把可压缩的最旧前缀与上一版摘要合并,输出**严格 JSON 的结构化摘要**;
- 结构化 schema 用 pydantic 做代码级校验(不靠 prompt 自觉);
- 校验失败或"摘要不比原文短"则放弃本次压缩(宁可不压,不可压错);
- current_objective 是必填首字段:长任务压缩后绝不丢当前目标。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from otter.models.types import Message, ModelUsage
from otter.prompts import SUMMARY_PROMPT  # 2026-09-23 套件化:文案单一来源(原 _SUMMARY_PROMPT)


class ConversationSummary(BaseModel):
    """结构化会话摘要 schema(说明书 5.2:各限条数与字数)。"""

    current_objective: str = Field(..., min_length=1, max_length=200)  # 当前目标,绝不丢
    user_constraints: list[str] = Field(default_factory=list, max_length=8)
    key_decisions: list[str] = Field(default_factory=list, max_length=8)
    completed_work: list[str] = Field(default_factory=list, max_length=8)
    pending_work: list[str] = Field(default_factory=list, max_length=8)
    important_facts: list[str] = Field(default_factory=list, max_length=8)

    def render(self) -> str:
        """渲染为注入请求视图的 <conversation_summary> 系统文本。"""
        lines = ["<conversation_summary>", f"当前目标:{self.current_objective}"]
        for label, items in (
            ("用户约束", self.user_constraints), ("关键决策", self.key_decisions),
            ("已完成", self.completed_work), ("待办", self.pending_work),
            ("重要事实", self.important_facts),
        ):
            if items:
                lines.append(f"{label}:" + ";".join(f"- {i}" for i in items[:8]))
        lines.append("</conversation_summary>")
        return "\n".join(lines)


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


class RollingSummarizer:
    """用(可独立配置的)摘要模型执行一次滚动压缩。"""

    def __init__(self, adapter) -> None:  # adapter: 复用 OpenAICompatAdapter 接口
        self.adapter = adapter

    async def summarize(
        self, messages: list[Message], prev_summary: ConversationSummary | None
    ) -> tuple[ConversationSummary | None, ModelUsage]:
        """返回 (新摘要 | None(放弃压缩), 摘要调用的真实用量——压缩不免费,要记账)。"""
        if not messages:
            return None, ModelUsage()

        def render_msg(m: Message) -> str:
            role = {"user": "用户", "assistant": "助手", "tool": "工具结果", "system": "系统"}.get(m.role, m.role)
            preview = (m.content or "")[:800]  # 单条入摘要上限,防超长工具输出撑爆摘要请求
            if m.tool_calls:
                preview += f" [调用工具:{','.join(t.name for t in m.tool_calls)}]"
            return f"[{role}] {preview}"

        parts = [SUMMARY_PROMPT]  # 2026-09-23 套件化:改用 prompts.py 导入(内容不变)
        if prev_summary:
            parts.append("【旧摘要】\n" + prev_summary.model_dump_json(ensure_ascii=False))
        parts.append("【待压缩对话】\n" + "\n".join(render_msg(m) for m in messages))

        resp = await self.adapter.complete_stream(
            [Message(role="user", content="\n\n".join(parts))], tools=None
        )
        try:
            summary = ConversationSummary.model_validate(json.loads(_strip_fence(resp.content or "")))
        except (json.JSONDecodeError, ValidationError):
            # 校验失败放弃本次压缩(下一次触发线还会再试)
            return None, resp.usage

        # "摘要必须比原文短"硬校验:比待压缩文本还长就放弃
        raw_len = sum(len(m.content or "") for m in messages)
        if len(summary.render()) >= raw_len:
            return None, resp.usage
        return summary, resp.usage
