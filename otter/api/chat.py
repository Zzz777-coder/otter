"""SSE 流式 chat 端点(R2,2026-09-29):POST /api/chat。

连接语义(有意取舍,与本地桌面工具的可靠性模型一致):
- **断连 ≠ 取消**——客户端中途断开(刷新/关标签页/网络抖动),服务端的
  agent run 照常跑完:消息与事件全程落库,事后可经 R1 只读端点
  (/api/conversations/{cid}/messages、/api/runs/{rid}/events)取回全量。
- 事件协议与 CLI stream-json 事件名对齐(小写直通 loop 事件流 +
  text_delta/final/error),同一套消费端可复用。

SSE 帧格式:`event: <name>` + `data: <json>`,UTF-8,`text/event-stream`。

两步式结构(开流前/后台):
- prepare_chat:校验(404)+建会话+建 run——在 StreamingResponse 开流
  **之前**完成,失败走标准 JSON 错误而非流内 error 帧;
- drive_chat:跑 loop.run 的后台 task——SSE 消费端断开后由 _RunHandle
  停止转发,run 继续执行到自然终态。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from otter.loop import MODE_NORMAL, MODE_PLAN, SummaryState
from otter.models.types import Message


class ChatRequest(BaseModel):
    """chat 请求体:prompt 必填;conversation_id 缺省=新建会话。"""

    prompt: str
    conversation_id: int | None = None
    max_steps: int = 30
    mode: str = "normal"  # normal / plan(PLAN=只读调查模式,与 CLI --plan 同义)


@dataclass
class _RunHandle:
    """一次 chat run 的转发状态(queue + 客户端在线标记)。"""

    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    client_gone: bool = False  # SSE 消费端断开后置 True:后续事件不再入队(只落库)

    async def put(self, name: str, data: dict[str, Any]) -> None:
        if not self.client_gone:
            await self.queue.put((name, data))

    def put_nowait(self, name: str, data: dict[str, Any]) -> None:
        """同步上下文入队(text_delta 回调是同步签名,借用 queue 非阻塞接口)。"""
        if not self.client_gone:
            self.queue.put_nowait((name, data))


async def prepare_chat(engine, body: ChatRequest) -> tuple[int, int, _RunHandle]:
    """开流前置:404 校验 + 建会话(缺省时)+ 建 run + 首两帧入队。

    返回 (conversation_id, run_id, handle);调用方在 StreamingResponse
    之前调用,保证校验失败能走标准 JSON 错误通道。
    """
    store = engine.store
    if body.conversation_id is not None:
        if not await store.exists_conversation(body.conversation_id):
            raise HTTPException(status_code=404, detail="会话不存在")
        cid = body.conversation_id
    else:
        cid = await store.new_conversation()
        await store.touch_conversation(cid, title=body.prompt[:20])
    run_id = await store.new_run()
    mode = MODE_PLAN if body.mode == "plan" else MODE_NORMAL
    handle = _RunHandle()
    await handle.put("conversation", {"conversation_id": cid})
    await handle.put("run", {"run_id": run_id, "mode": mode})
    return cid, run_id, handle


async def drive_chat(engine, body: ChatRequest, cid: int, run_id: int,
                     handle: _RunHandle) -> None:
    """后台驱动:loop.run 全程(断连后仍在跑,事件只落库不转发)。"""
    store = engine.store
    mode = MODE_PLAN if body.mode == "plan" else MODE_NORMAL

    async def on_event(type_: str, payload: dict) -> None:
        await handle.put(type_.lower(), payload)

    def on_text_delta(s: str) -> None:
        if s:
            handle.put_nowait("text_delta", {"text": s})

    try:
        result = await engine.loop.run(
            [], Message(role="user", content=body.prompt), run_id, body.max_steps,
            on_text_delta=on_text_delta,
            on_event=on_event, summary_state=SummaryState(),
            conversation_id=cid, mode=mode,
        )
        await store.touch_conversation(cid)
        await handle.put("final", {
            "ok": bool(result and result.ok),
            "stop_reason": result.stop_reason if result else "interrupted",
            "final_text": result.final_text if result else "",
            "steps": result.steps if result else 0,
            "tool_calls": result.tool_calls if result else 0,
            "usage": {"input": result.usage.input_tokens, "output": result.usage.output_tokens}
            if result else None,
        })
    except asyncio.CancelledError:
        # 服务关停等场景的硬取消:诚实落终态后向上传播(与 GUI 停止键同语义)
        await store.finish_run(run_id, "cancelled", "stopped")
        raise
    except Exception as exc:  # 引擎异常:SSE 推 error 帧(若客户端还在),run 落 failed
        await store.finish_run(run_id, "failed", "error")
        await handle.put("error", {"message": f"{type(exc).__name__}: {exc}"})
    finally:
        await handle.queue.put(None)  # 结束哨兵:消费端据此收尾断流


def sse_frame(name: str, data: dict[str, Any]) -> str:
    """一帧 SSE:event 行 + data 行(JSON 直列;字段名不含换行,无需转义)。"""
    import json

    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
