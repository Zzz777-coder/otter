"""事件态持久化:SQLite(messages / runs / events 三表)。

持久化取向——"SQLite 管事件与状态,Markdown 管知识事实"(2026-09-24 去溯源字样)。
M0 只写不读(审计与验收用:Run 全程 Trace 落库);会话恢复读取在 M1/M2 实现。
库文件固定在 <cwd>/.otter/otter.db,与工作区绑定。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from otter.models.types import Message


def _now() -> float:
    return time.time()


class Store:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (Path.cwd() / ".otter" / "otter.db")
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,           -- running/completed/failed
                stop_reason TEXT,
                started_at REAL NOT NULL,
                finished_at REAL
            );
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence INTEGER NOT NULL,       -- 稳定序号(会话消息稳定排序)
                run_id INTEGER,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls_json TEXT,           -- assistant 消息的工具调用列表 JSON
                tool_call_id TEXT,
                name TEXT,
                conversation_id INTEGER,        -- 会话归属(v5 新增;旧库经下方 ALTER 迁移)
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                type TEXT NOT NULL,             -- MODEL_STARTED/TOOL_STARTED/TOOL_COMPLETED/...
                payload_json TEXT,
                created_at REAL NOT NULL
            );
            -- v5(2026-09-22):会话表——支撑 GUI 次级栏历史会话与重启恢复(用户五要点之④)
            CREATE TABLE IF NOT EXISTS conversations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            -- M2(2026-09-22):Evidence 不可变归档——工具输出截断前存原文,可回取
            CREATE TABLE IF NOT EXISTS evidence(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                tool_call_id TEXT,
                tool_name TEXT,
                content TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            -- M2:Checkpoint——中断恢复的最小边界
            CREATE TABLE IF NOT EXISTS run_checkpoints(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                user_message TEXT,
                phase TEXT NOT NULL,              -- MODEL_REQUEST / TOOL_EXECUTION / FINISHED
                pending_tools_json TEXT,          -- 不确定是否已执行,恢复时禁止直接重试
                recovered INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            """
        )
        # 幂等迁移:旧库的 messages 没有 conversation_id 列(SQLite 无 IF NOT EXISTS ADD COLUMN)
        cursor = await self._db.execute("PRAGMA table_info(messages)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "conversation_id" not in columns:
            await self._db.execute("ALTER TABLE messages ADD COLUMN conversation_id INTEGER")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def new_run(self) -> int:
        cur = await self._db.execute(
            "INSERT INTO runs(status, started_at) VALUES('running', ?)", (_now(),)
        )
        await self._db.commit()
        return int(cur.lastrowid)

    async def finish_run(self, run_id: int, status: str, stop_reason: str) -> None:
        await self._db.execute(
            "UPDATE runs SET status=?, stop_reason=?, finished_at=? WHERE id=?",
            (status, stop_reason, _now(), run_id),
        )
        await self._db.commit()

    async def append_message(self, msg: Message, sequence: int, run_id: int,
                             conversation_id: int | None = None) -> None:
        tool_calls_json = (
            json.dumps(
                [{"id": t.id, "name": t.name, "arguments": t.arguments} for t in msg.tool_calls],
                ensure_ascii=False,
            )
            if msg.tool_calls
            else None
        )
        await self._db.execute(
            "INSERT INTO messages(sequence, run_id, role, content, tool_calls_json, tool_call_id, name, conversation_id, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (sequence, run_id, msg.role, msg.content, tool_calls_json, msg.tool_call_id, msg.name,
             conversation_id, _now()),
        )
        await self._db.commit()

    # ── v5 会话 API(GUI 次级栏 + 重启恢复)────────────────────────────

    async def new_conversation(self, title: str = "新会话") -> int:
        cur = await self._db.execute(
            "INSERT INTO conversations(title, created_at, updated_at) VALUES(?,?,?)",
            (title, _now(), _now()),
        )
        await self._db.commit()
        return int(cur.lastrowid)

    async def list_conversations(self, limit: int = 50) -> list[dict]:
        # 2026-09-23 历史排版要求:卡片副行需要「N 轮对话」——
        # assistant 消息数作轮数,子查询一次拿齐(避免 N+1)
        cursor = await self._db.execute(
            "SELECT c.id, c.title, c.updated_at,"
            " (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id AND m.role = 'assistant')"
            " FROM conversations c ORDER BY c.updated_at DESC LIMIT ?",
            (limit,),
        )
        return [{"id": r[0], "title": r[1], "updated_at": r[2], "rounds": r[3]}
                for r in await cursor.fetchall()]

    async def touch_conversation(self, cid: int, title: str | None = None) -> None:
        """更新会话活跃时间;给标题则一并更新(首条消息落库时定标题用)。"""
        if title:
            await self._db.execute(
                "UPDATE conversations SET updated_at=?, title=? WHERE id=?", (_now(), title, cid)
            )
        else:
            await self._db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (_now(), cid)
            )
        await self._db.commit()

    async def load_conversation_messages(self, cid: int, include_tools: bool = False) -> list[Message]:
        """按会话加载历史。默认只取 user/assistant(重建对话上下文用);
        include_tools=True 时含工具消息——GUI 历史回看要还原当时的完整过程行(2026-09-22 用户要求)。"""
        where = "conversation_id=?" + ("" if include_tools else " AND role IN ('user','assistant')")
        cursor = await self._db.execute(
            "SELECT role, content, tool_calls_json, name FROM messages"
            f" WHERE {where} ORDER BY id",
            (cid,),
        )
        out: list[Message] = []
        for role, content, tc_json, name in await cursor.fetchall():
            tool_calls = []
            if tc_json:
                from otter.models.types import ToolCall

                tool_calls = [ToolCall(id=t["id"], name=t["name"], arguments=t["arguments"])
                              for t in json.loads(tc_json)]
            out.append(Message(role=role, content=content, tool_calls=tool_calls, name=name))
        return out

    async def append_event(self, run_id: int, type_: str, payload: dict[str, Any]) -> None:
        await self._db.execute(
            "INSERT INTO events(run_id, type, payload_json, created_at) VALUES(?,?,?,?)",
            (run_id, type_, json.dumps(payload, ensure_ascii=False, default=str), _now()),
        )
        await self._db.commit()

    # ── M2 Evidence(可逆压缩的存档侧)──────────────────────────────

    async def append_evidence(self, run_id: int, tool_call_id: str, tool_name: str,
                              content: str) -> int:
        import hashlib

        cur = await self._db.execute(
            "INSERT INTO evidence(run_id, tool_call_id, tool_name, content, sha256, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (run_id, tool_call_id, tool_name, content,
             hashlib.sha256(content.encode()).hexdigest(), _now()),
        )
        await self._db.commit()
        return int(cur.lastrowid)

    async def get_evidence(self, evidence_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT id, tool_name, content, sha256, created_at FROM evidence WHERE id=?",
            (evidence_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {"id": row[0], "tool_name": row[1], "content": row[2], "sha256": row[3], "created_at": row[4]}

    async def list_evidence(self, run_id: int, limit: int = 30) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT id, tool_name, length(content), created_at FROM evidence"
            " WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (run_id, limit),
        )
        return [{"id": r[0], "tool_name": r[1], "chars": r[2], "created_at": r[3]}
                for r in await cursor.fetchall()]

    # ── M2 Checkpoint(中断恢复)────────────────────────────────────

    async def save_checkpoint(self, run_id: int, user_message: str, phase: str,
                              pending_tools: list[dict] | None = None) -> int:
        cur = await self._db.execute(
            "INSERT INTO run_checkpoints(run_id, user_message, phase, pending_tools_json, created_at)"
            " VALUES(?,?,?,?,?)",
            (run_id, user_message, phase,
             json.dumps(pending_tools or [], ensure_ascii=False), _now()),
        )
        await self._db.commit()
        return int(cur.lastrowid)

    async def mark_checkpoint_recovered(self, run_id: int) -> None:
        await self._db.execute(
            "UPDATE run_checkpoints SET recovered=1 WHERE run_id=?", (run_id,)
        )
        await self._db.commit()

    async def latest_unrecovered_checkpoint(self) -> dict | None:
        """reconciliation / --resume 用:最近一条未恢复且非 FINISHED 的 Checkpoint。"""
        cursor = await self._db.execute(
            "SELECT id, run_id, user_message, phase, pending_tools_json, created_at"
            " FROM run_checkpoints WHERE recovered=0 AND phase != 'FINISHED'"
            " ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {"id": row[0], "run_id": row[1], "user_message": row[2], "phase": row[3],
                "pending_tools": json.loads(row[4] or "[]"), "created_at": row[5]}

    async def load_run_messages(self, run_id: int) -> list[Message]:
        """按 run 加载全部消息(Checkpoint 恢复重建对话用,含工具轮)。"""
        cursor = await self._db.execute(
            "SELECT role, content, tool_calls_json, tool_call_id, name FROM messages"
            " WHERE run_id=? ORDER BY id",
            (run_id,),
        )
        from otter.models.types import ToolCall

        out: list[Message] = []
        for role, content, tc_json, tool_call_id, name in await cursor.fetchall():
            tool_calls = []
            if tc_json:
                tool_calls = [ToolCall(id=t["id"], name=t["name"], arguments=t["arguments"])
                              for t in json.loads(tc_json)]
            out.append(Message(role=role, content=content, tool_calls=tool_calls,
                               tool_call_id=tool_call_id, name=name))
        return out

    async def reconcile_interrupted_runs(self) -> int:
        """启动修正:遗留 running 的 Run 改标 interrupted(以 Checkpoint 为事实源,不伪造完成)。"""
        cur = await self._db.execute(
            "UPDATE runs SET status='interrupted', stop_reason='interrupted'"
            " WHERE status IN ('running','pending')"
        )
        await self._db.commit()
        return cur.rowcount or 0
