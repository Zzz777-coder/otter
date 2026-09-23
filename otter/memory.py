"""双层记忆(M3,说明书 5.3,设计参考 vesta app/memory/ 思想、全部重实现)。

分层:
- Core(.otter/memory/CORE.md):用户长期事实,key 管理条目,≤2000 token,每 Run 常驻注入;
  只经 memory_core_update 工具更新(带出处原话),模型不能整文件覆盖。
- Ordinary(.otter/memory/active/M###.md):单条知识,YAML front matter 为权威元数据
  (title/summary/revision/access_count/status),active 上限 25,超限归档;
  INDEX.md 与 FTS5 索引均为可重建投影(文件是唯一权威——vesta 的"Markdown 管知识"取向)。

一致性铁律(自 vesta 学到的防幻觉写):
- UPDATE 必须本 Run 内 memory_read 成功过,且 revision 乐观锁匹配,否则拒绝;
- 召回不写历史、不加 access_count、不授权反思更新——注入的只是 cue(标题+摘要+片段)。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otter.tools.base import Tool

ACTIVE_LIMIT = 25
CORE_TOKEN_LIMIT = 2000


# ── Core Memory ───────────────────────────────────────────────────

class CoreMemory:
    """CORE.md:key/value/reason/出处原话 的受管条目(非自由文本,防整体覆盖)。"""

    def __init__(self, root: Path) -> None:
        self.path = root / "CORE.md"

    def load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("entries", [])
        except json.JSONDecodeError:
            return []

    def save(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def upsert(self, key: str, value: str, reason: str, source_quote: str) -> None:
        entries = [e for e in self.load() if e["key"] != key]  # 同 key 覆盖
        entries.append({"key": key.strip(), "value": value.strip(),
                        "reason": reason.strip(), "source_quote": source_quote.strip(),
                        "updated_at": time.strftime("%Y-%m-%d")})
        self.save(entries)

    def remove(self, key: str) -> bool:
        entries = self.load()
        rest = [e for e in entries if e["key"] != key]
        if len(rest) != len(entries):
            self.save(rest)
            return True
        return False

    def render(self) -> str:
        entries = self.load()
        if not entries:
            return ""
        body = "\n".join(f"- {e['key']}:{e['value']}(依据:{e['reason']})" for e in entries)
        return f"<core_memory>\n{body}\n</core_memory>"


# ── Ordinary Memory(文件权威 + 投影)──────────────────────────────

@dataclass
class MemoryEntry:
    mid: str                 # M001…
    title: str
    summary: str
    content: str
    revision: int = 1
    access_count: int = 0
    status: str = "active"   # active/archived


def _front_matter(e: MemoryEntry) -> str:
    return (f"---\ntitle: {e.title}\nsummary: {e.summary}\nrevision: {e.revision}\n"
            f"access_count: {e.access_count}\nstatus: {e.status}\nid: {e.mid}\n---\n")


class FileMemoryStore:
    """active/ 目录:一条一文件,front matter 权威;INDEX 与 FTS5 是投影。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.active_dir = root / "active"
        self.active_dir.mkdir(parents=True, exist_ok=True)
        self._fts = sqlite3.connect(root / "memory.sqlite")
        try:
            self._fts.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem USING fts5"
                              "(id UNINDEXED, title, summary, content, tokenize='trigram')")
        except sqlite3.OperationalError:  # 老环境无 trigram:投影整体降级(检索走遍历)
            self._fts = None

    # -- 文件层(权威)--
    def list_active(self) -> list[MemoryEntry]:
        out = []
        for p in sorted(self.active_dir.glob("M*.md")):
            e = self._parse(p)
            if e and e.status == "active":
                out.append(e)
        return out

    def _parse(self, p: Path) -> MemoryEntry | None:
        text = p.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
        if not m:
            return None
        meta = dict(re.findall(r"^(\w+):\s*(.*)$", m.group(1), re.M))
        return MemoryEntry(
            mid=meta.get("id", p.stem), title=meta.get("title", ""),
            summary=meta.get("summary", ""), content=m.group(2).strip(),
            revision=int(meta.get("revision", 1)),
            access_count=int(meta.get("access_count", 0)),
            status=meta.get("status", "active"),
        )

    def read(self, mid: str) -> MemoryEntry | None:
        p = self.active_dir / f"{mid}.md"
        return self._parse(p) if p.is_file() else None

    def create(self, title: str, summary: str, content: str) -> MemoryEntry:
        ids = [int(e.mid[1:]) for e in self.list_active()]
        e = MemoryEntry(mid=f"M{max(ids, default=0) + 1:03d}",
                        title=title[:200], summary=summary[:500], content=content[:12000])
        self._write(e)
        self._evict_if_full()
        return e

    def update(self, mid: str, expected_revision: int, **changes) -> MemoryEntry | None:
        """乐观锁:revision 不匹配即拒绝(vesta 的防幻觉写一致性)。"""
        e = self.read(mid)
        if e is None or e.revision != expected_revision:
            return None
        for k in ("title", "summary", "content"):
            if k in changes and changes[k]:
                setattr(e, k, changes[k])
        e.revision += 1
        self._write(e)
        return e

    def archive(self, mid: str) -> bool:
        e = self.read(mid)
        if e is None:
            return False
        e.status = "archived"
        (self.root / "archive").mkdir(exist_ok=True)
        self._write(e, subdir="archive")
        (self.active_dir / f"{mid}.md").unlink(missing_ok=True)
        return True

    def _evict_if_full(self) -> None:
        actives = self.list_active()
        if len(actives) <= ACTIVE_LIMIT:
            return
        for e in sorted(actives, key=lambda x: x.access_count)[: len(actives) - ACTIVE_LIMIT]:
            self.archive(e.mid)  # 超限按 access_count 淘汰最冷

    def _write(self, e: MemoryEntry, subdir: str = "active") -> None:
        p = self.root / subdir / f"{e.mid}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_front_matter(e) + e.content + "\n", encoding="utf-8")
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        """INDEX.md 与 FTS5:可删重建的投影,失败不影响主流程(vesta 降级取向)。"""
        entries = self.list_active()
        index = "\n".join(f"- {e.mid} {e.title}:{e.summary}" for e in entries)
        (self.root / "INDEX.md").write_text(index or "(空)", encoding="utf-8")
        if self._fts is None:
            return
        try:
            self._fts.execute("DELETE FROM mem")
            for e in entries:
                # 修正(2026-09-22):表共 4 列(id+title+summary+content),此前插 5 值
                # 的 OperationalError 被降级分支吞掉,索引一直是空表(测试暴露)
                self._fts.execute("INSERT INTO mem VALUES(?,?,?,?)",
                                  (e.mid, e.title, e.summary, e.content))
            self._fts.commit()
        except sqlite3.Error:
            pass

    # -- 检索层(投影)--
    def search(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        q = query.strip()
        # 修正(2026-09-22):trigram 查询词需 ≥3 字符才产 token(两字中文词命中不了);
        # 短词直接走遍历分支。FTS 命中按写入顺序读文件重建完整条目。
        if self._fts is not None and len(q) >= 3:
            try:
                cur = self._fts.execute(
                    "SELECT id FROM mem WHERE mem MATCH ? LIMIT ?", (f'"{q}"', limit)
                )
                return [e for row in cur.fetchall() if (e := self.read(row[0]))]
            except sqlite3.OperationalError:
                pass  # 查询语法等问题:降级遍历
        ql = q.lower()
        return [e for e in self.list_active()
                if ql in e.title.lower() or ql in e.summary.lower() or ql in e.content.lower()][:limit]


# ── 确定性召回 + 反思门控(设计参考 vesta recall.py / reflection_gate.py)──

_RECALL_SIGNAL = re.compile(r"记住|以后|下次|偏好|习惯|总是|不要忘|remember|prefer|always")


def recall_query(user_message: str, recent_users: list[str], objective: str) -> str:
    """纯确定性拼装(不调模型):当前+近3条用户消息+会话目标,≤1600 字符。"""
    parts = [user_message] + recent_users[-3:] + ([objective] if objective else [])
    return "".join(parts)[:1600]


def reflection_should_run(user_message: str, recalled: bool) -> bool:
    """保守门控:命中长期信号词或有召回 → 反思;寒暄/能力询问 → 跳;不确定 → 放行。"""
    if len(user_message) < 6:  # 过短(寒暄)跳过
        return False
    if _RECALL_SIGNAL.search(user_message):
        return True
    return recalled
