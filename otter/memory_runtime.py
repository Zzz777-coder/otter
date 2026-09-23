"""M3 记忆运行时:工具面 + Run 后反思(loop 集成点)。

工具面(模型可见):memory_read / memory_search / memory_write(create|update,乐观锁)
 / memory_core_update。
一致性:loop 在 Run 结束时记录本 Run 读过的 mid→revision,反思 UPDATE 必须命中该记录。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from otter.memory import CoreMemory, FileMemoryStore
from otter.tools.base import Tool


def build_memory_tools(root: Path | None = None):
    """构造记忆三件套 + 共享运行时上下文(读写记录供反思校验)。"""
    root = root or (Path.cwd() / ".otter" / "memory")
    store = FileMemoryStore(root)
    core = CoreMemory(root)
    ctx = {"reads": {}, "run_id": None}  # mid -> revision(本 Run 读到的)

    class MemoryReadTool(Tool):
        name = "memory_read"
        description = "按 id 读取一条记忆全文。修改前必须先读(本 Run 读过的版本才允许更新)。"
        parameters = {"type": "object", "properties": {"mid": {"type": "string", "description": "如 M001"}},
                      "required": ["mid"]}

        async def run(self, args):
            e = store.read(str(args.get("mid", "")))
            if e is None:
                return f"[otter] 记忆 {args.get('mid')} 不存在;用 memory_search 查"
            ctx["reads"][e.mid] = e.revision  # 记录读取版本(UPDATE 乐观锁依据)
            return f"[{e.mid} rev{e.revision}] {e.title}\n摘要:{e.summary}\n\n{e.content[:4000]}"

    class MemorySearchTool(Tool):
        name = "memory_search"
        description = "按关键词搜索长期记忆(标题/摘要/全文),返回 id 与标题摘要。"
        parameters = {"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"]}

        async def run(self, args):
            hits = store.search(str(args.get("query", "")))
            if not hits:
                return "[otter] 无相关记忆"
            return "\n".join(f"{e.mid} {e.title}:{e.summary}" for e in hits)

    class MemoryWriteTool(Tool):
        name = "memory_write"
        description = (
            "写入长期记忆:新建用 action=create(title/summary/content);"
            "更新用 action=update(mid + 读到的 revision + 修改字段)。"
            "只存跨会话仍有价值的事实/约定/决策,不存一次性任务细节。"
        )
        parameters = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "update"]},
                "mid": {"type": "string", "description": "update 时必填"},
                "revision": {"type": "integer", "description": "update 时必填(memory_read 返回的 rev)"},
                "title": {"type": "string"}, "summary": {"type": "string"}, "content": {"type": "string"},
            },
            "required": ["action"],
        }

        async def run(self, args):
            action = args.get("action")
            if action == "create":
                if not (args.get("title") and args.get("content")):
                    return "[otter] create 需要 title 与 content"
                e = store.create(str(args.get("title", "")), str(args.get("summary", "")),
                                 str(args.get("content", "")))
                return f"[otter] 已新建记忆 {e.mid}(rev{e.revision})"
            # update:乐观锁 + 本 Run 必须读过
            mid = str(args.get("mid", ""))
            if ctx["reads"].get(mid) != args.get("revision"):
                return (f"[otter] 拒绝更新:revision 不匹配或本 Run 未读过 {mid}。"
                        f"请先 memory_read 获取最新 revision。")
            e = store.update(mid, int(args.get("revision", 0)),
                             title=args.get("title"), summary=args.get("summary"),
                             content=args.get("content"))
            if e is None:
                return f"[otter] 更新失败:{mid} 不存在或版本已变化"
            ctx["reads"][mid] = e.revision
            return f"[otter] 已更新 {mid} → rev{e.revision}"

    class MemoryCoreTool(Tool):
        name = "memory_core_update"
        description = ("更新核心记忆(每 Run 常驻注入的用户长期事实,如身份/偏好/项目约束)。"
                       "必须提供 source_quote(用户原话)作为依据。")
        parameters = {"type": "object",
                      "properties": {"key": {"type": "string"}, "value": {"type": "string"},
                                     "reason": {"type": "string"}, "source_quote": {"type": "string"}},
                      "required": ["key", "value", "source_quote"]}

        async def run(self, args):
            core.upsert(str(args.get("key", "")), str(args.get("value", "")),
                        str(args.get("reason", "")), str(args.get("source_quote", "")))
            return f"[otter] 核心记忆已更新:{args.get('key')}"

    return [MemoryReadTool(), MemorySearchTool(), MemoryWriteTool(), MemoryCoreTool()], store, core, ctx


# 2026-09-23 提示词套件重构:REFLECT_PROMPT 移至 otter/prompts.py(内容不变)
from otter.prompts import REFLECT_PROMPT


async def run_reflection(adapter, user_message: str, final_text: str,
                         store: FileMemoryStore, core: CoreMemory, ctx: dict) -> str:
    """Run 后反思(FINAL 时调用):单动作,失败只返回诊断文本,绝不影响主结果(隔离取向)。"""
    from otter.models.types import Message

    convo = f"[用户]{user_message[:1500]}\n[助手结论]{final_text[:1500]}"
    try:
        resp = await adapter.complete_stream(
            [Message(role="user", content=REFLECT_PROMPT + "\n\n" + convo)], tools=None
        )
        text = (resp.content or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        action = json.loads(text)
    except Exception as exc:
        return f"(反思失败被隔离:{type(exc).__name__})"
    if action.get("action") == "create" and action.get("title") and action.get("content"):
        e = store.create(action["title"], action.get("summary", ""), action["content"])
        return f"(反思新建 {e.mid})"
    if action.get("action") == "update":
        mid = action.get("mid", "")
        if ctx["reads"].get(mid) != action.get("revision"):
            return "(反思 update 被拒:本 Run 未读过或 revision 不匹配——防幻觉写)"
        e = store.update(mid, int(action.get("revision", 0)),
                         title=action.get("title"), summary=action.get("summary"),
                         content=action.get("content"))
        return f"(反思更新 {mid})" if e else "(反思 update 失败)"
    return "(反思:none)"
