"""API 应用工厂(2026-09-29 新增:本地 HTTP 开发者入口)。

设计要点:
- create_app() 工厂 + lifespan 只管资源开关——Store 的 open/close,engine
  (R2 起,可选)的 aclose;进程退出时正常收尾 SQLite 连接,不截断 WAL;
- 全部端点挂 /api 前缀,/docs 与 /openapi.json 由框架自动生成;
- 仅绑 loopback 使用(启动参数默认 127.0.0.1),不加鉴权——
  开放局域网前必须补 token,注释留档;
- 会话/消息/事件均为对既有 Store 的薄封装,不引入第二份业务逻辑;
- R2(2026-09-29):POST /api/chat SSE 流式端点,断连≠取消
  (实现与连接语义见 otter/api/chat.py 模块 docstring)。

结构注意(2026-09-29 踩坑记录):本模块启用 `from __future__ import
annotations`,端点参数注解会被字符串化再由框架求值——求值用的是
模块全局命名空间,看不到工厂函数体内的局部类。因此 Pydantic 模型
与端点必须放模块级,create_app 只负责装配 state 与 lifespan。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from otter import __version__
from otter.api.chat import ChatRequest, drive_chat, prepare_chat, sse_frame
from otter.store import Store


def _get_store(request: Request) -> Store:
    """依赖注入入口:端点统一从这里拿 Store 实例(挂在 app.state,随应用生命周期开关)。"""
    store: Store = request.app.state.store
    return store


def _message_to_dict(msg: Any) -> dict[str, Any]:
    """Message dataclass → 纯 JSON 结构(tool_calls 一并带出)。"""
    return {
        "role": msg.role,
        "content": msg.content,
        "tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in msg.tool_calls
        ],
        "tool_call_id": msg.tool_call_id,
        "name": msg.name,
    }


# ── Pydantic 请求模型(2026-09-29):请求体校验与 /docs 表单由它们驱动 ──

class ConversationCreate(BaseModel):
    title: str = "新会话"


class ConversationUpdate(BaseModel):
    """改名/置顶可单传其一(对应 GUI 会话⋯菜单同名能力)。"""

    title: str | None = None
    pinned: bool | None = None


# ── 端点(模块级注册,依赖注入拿 Store)──


async def health() -> dict[str, Any]:
    """探活 + 版本(供脚本判断服务可用、版本对齐)。"""
    return {"status": "ok", "version": __version__}


async def list_conversations(
    limit: int = 50, store: Store = Depends(_get_store)
) -> list[dict[str, Any]]:
    """会话列表:置顶优先、组内按活跃时间倒序(与 GUI 侧栏同序)。"""
    return await store.list_conversations(limit=limit)


async def create_conversation(
    body: ConversationCreate, store: Store = Depends(_get_store)
) -> dict[str, Any]:
    """新建空会话(脚本批量建会话/预置模板时用)。"""
    cid = await store.new_conversation(title=body.title)
    return {"id": cid, "title": body.title}


async def update_conversation(
    cid: int, body: ConversationUpdate, store: Store = Depends(_get_store)
) -> dict[str, Any]:
    """改名/置顶。不存在的会话返回 404;两个字段均可选,只传一个只改一个。"""
    if not await store.exists_conversation(cid):
        raise HTTPException(status_code=404, detail="会话不存在")
    if body.pinned is not None:
        await store.pin_conversation(cid, body.pinned)
    if body.title is not None:
        await store.touch_conversation(cid, title=body.title)
    return {"id": cid, "title": body.title}


async def delete_conversation(
    cid: int, store: Store = Depends(_get_store)
) -> Response:
    """删除会话(messages 级联删,events/runs 审计数据保留,与 GUI 行为一致)。"""
    if not await store.delete_conversation(cid):
        raise HTTPException(status_code=404, detail="会话不存在")
    return Response(status_code=204)


async def conversation_messages(
    cid: int,
    include_tools: bool = False,
    store: Store = Depends(_get_store),
) -> list[dict[str, Any]]:
    """会话消息历史。include_tools=true 时含工具消息(GUI 过程行还原同款开关)。"""
    if not await store.exists_conversation(cid):
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = await store.load_conversation_messages(cid, include_tools=include_tools)
    return [_message_to_dict(m) for m in messages]


async def run_events(
    run_id: int, store: Store = Depends(_get_store)
) -> list[dict[str, Any]]:
    """Run 事件流(Trace 分账原始数据)。

    不存在的 run 返回空列表而非 404:事件流以空为合法状态
    (run 刚创建尚未产生事件),与 messages 的严格语义有意区分。
    """
    return await store.load_run_events(run_id)


async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    """SSE 流式 chat(R2,2026-09-29):断连≠取消,事件照常落库。

    引擎(app.state.engine)由 create_app 的 engine 参数注入;未注入时
    本端点返回 503(R1 只读端点不受影响,测试可完全离线)。
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="chat 引擎未配置(启动时需提供模型 key)")

    # 开流前完成 404 校验 + 建会话/run(失败走标准 JSON 错误,而非流内 error 帧)
    cid, run_id, handle = await prepare_chat(engine, body)

    async def stream():
        # 2026-09-29 R2:后台 task 跑 agent(断连后仍跑完落库);本生成器只负责
        # 把 queue 里的事件转成 SSE 帧。客户端断开 → 生成器被取消 → 置
        # client_gone 停止转发,run 不受影响。
        task = asyncio.create_task(drive_chat(engine, body, cid, run_id, handle))
        _BACKGROUND.add(task)  # 持引用防 GC;断连后 run 仍在跑
        task.add_done_callback(_BACKGROUND.discard)
        try:
            while True:
                item = await handle.queue.get()
                if item is None:
                    break
                yield sse_frame(*item)
        except asyncio.CancelledError:
            handle.client_gone = True  # 断连≠取消:run 继续,只是不再转发
            raise

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


_BACKGROUND: set = set()  # 后台 run 的 task 引用(防 GC;完成自动移除)


def create_app(store: Store | None = None, engine=None) -> FastAPI:
    """构造 API 应用。

    store 为 None 时用默认路径(cwd/.otter/otter.db,与 GUI/CLI 同库);
    engine(R2 起)供 /api/chat 流式端点使用(build_engine 产物或测试注入
    fake);测试传 store 指向临时库即可完全离线。
    """
    if store is None:
        store = Store()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.open()
        try:
            yield
        finally:
            if engine is not None:
                await engine.aclose()
            await store.close()

    app = FastAPI(
        title="otter API",
        version=__version__,
        description="otter 本地助手开发者入口——会话/消息/Run 事件 + SSE 流式 chat",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.engine = engine

    app.get("/api/health")(health)
    app.get("/api/conversations")(list_conversations)
    app.post("/api/conversations", status_code=201)(create_conversation)
    app.patch("/api/conversations/{cid}")(update_conversation)
    app.delete("/api/conversations/{cid}", status_code=204)(delete_conversation)
    app.get("/api/conversations/{cid}/messages")(conversation_messages)
    app.get("/api/runs/{run_id}/events")(run_events)
    app.post("/api/chat")(chat)

    return app


__all__ = ["create_app"]
