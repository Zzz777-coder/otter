"""模型层厂商无关类型。

核心思想:内部统一 Message/ToolDefinition 等类型,
厂商差异收敛在 adapter 一层,loop 与工具子系统不感知任何厂商 API 形状。
(M0 用 dataclass,M1 引入摘要 schema
校验时再按说明书迁 pydantic v2。)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    """模型发出的一次工具调用。arguments 为已解析的 dict(adapter 负责聚合流式片段并 json.loads)。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """统一消息类型。role=tool 时必须带 tool_call_id(对应 ToolCall.id)与 name(工具名)。"""

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)  # 仅 assistant 消息使用
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class ModelUsage:
    """事后记账用的真实用量(来自厂商响应)。

    "两套计数器"结论:事前估算(tiktoken,M1 引入)管预防,
    事后真实 usage 管问责;未知字段保持 None 而非伪装成 0。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None

    def add(self, other: "ModelUsage") -> None:
        # 累加时保持"未知即 None"语义:任一侧未知,和也未知
        if self.input_tokens is None or other.input_tokens is None:
            self.input_tokens = None
        else:
            self.input_tokens += other.input_tokens
        if self.output_tokens is None or other.output_tokens is None:
            self.output_tokens = None
        else:
            self.output_tokens += other.output_tokens


@dataclass
class ModelResponse:
    """一次模型补全的结果。content 与 tool_calls 二者至少其一。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: ModelUsage = field(default_factory=ModelUsage)
    finish_reason: str | None = None


@dataclass
class ToolDefinition:
    """给模型看的工具定义(名称/描述/JSON Schema 参数)。"""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
