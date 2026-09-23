"""Evidence 可逆压缩(M2,说明书 5.2"最值得移植的 vesta 思想")。

机制:工具结果在截断/进模型之前,全文不可变归档(绑定 run/tool_call,sha256 校验);
给模型的压缩文本附 evidence_id;模型需要原文时用 evidence_read 按需取回——
压缩变成可逆的,模型不靠摘要硬编。
"""

from __future__ import annotations

from typing import Any

from otter.store import Store
from otter.tools.base import Tool
from otter.tools.builtin import truncate

ARCHIVE_THRESHOLD = 2000      # 超过即归档全文(截断阈值 8000 之前)
HINT = "\n[otter] 以上为截断结果,完整原文已归档:用 evidence_read(evidence_id={eid}) 取回"


async def maybe_archive(store: Store, run_id: int, tool_call_id: str, tool_name: str,
                        result_text: str) -> str:
    """长结果:归档原文,返回带 evidence 提示的截断文本;短结果原样返回。"""
    if len(result_text) <= ARCHIVE_THRESHOLD or store is None:
        return result_text
    eid = await store.append_evidence(run_id, tool_call_id, tool_name, result_text)
    return truncate(result_text) + HINT.format(eid=eid)


class EvidenceReadTool(Tool):
    """按 id 取回归档原文(可分页,防单次灌爆上下文)。"""

    name = "evidence_read"
    description = (
        "读取之前被截断的工具输出的完整原文。先用 evidence_list 查 id,"
        "再按行分页读取(start_line/end_line,每页默认 300 行)。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "evidence_id": {"type": "integer", "description": "evidence_list 查到的 id"},
            "start_line": {"type": "integer", "description": "起始行(从 1 开始,可选)"},
            "end_line": {"type": "integer", "description": "结束行(可选,默认 start+300)"},
        },
        "required": ["evidence_id"],
    }

    def __init__(self, store: Store) -> None:
        self.store = store

    async def run(self, args: dict[str, Any]) -> str:
        eid = int(args.get("evidence_id", 0))
        row = await self.store.get_evidence(eid)
        if row is None:
            return f"[otter] 错误:evidence {eid} 不存在;先 evidence_list 查看可用归档"
        lines = row["content"].splitlines()
        total = len(lines)
        start = max(1, int(args.get("start_line") or 1))
        end = min(total, int(args.get("end_line") or start + 299))
        picked = "\n".join(f"{n:>6}\t{lines[n - 1]}" for n in range(start, end + 1))
        header = f"[evidence #{eid} · {row['tool_name']} · 共 {total} 行 · sha256 {row['sha256'][:12]}…]\n"
        footer = f"\n[otter] 显示 {start}-{end}/{total} 行" if end < total else ""
        return truncate(header + picked + footer)


class EvidenceListTool(Tool):
    """列出当前 Run 的归档索引。"""

    name = "evidence_list"
    description = "列出最近被截断并归档的工具输出(evidence_id、来源工具、字符数),配合 evidence_read 使用。"

    parameters = {"type": "object", "properties": {}}

    def __init__(self, store: Store) -> None:
        self.store = store

    async def run(self, args: dict[str, Any]) -> str:
        # run_id 从执行上下文注入(见 loop 的 _bind_run);无上下文时列出全部最近归档
        run_id = getattr(self, "current_run_id", None)
        rows = await self.store.list_evidence(run_id) if run_id else await self.store.list_evidence(0)
        if not rows:
            return "[otter] 当前无归档(工具输出未超截断阈值时不会产生 evidence)"
        return "\n".join(f"#{r['id']} · {r['tool_name']} · {r['chars']} 字符" for r in rows)
