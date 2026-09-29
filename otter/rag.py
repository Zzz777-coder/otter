"""RAG 向量检索(2026-09-29 P5):分片 + Embedding + sqlite-vec 向量列 + 混合检索。

三层结构:
- chunk_text:中文友好的文本分片(段落→句子→定宽回退,相邻片留重叠);
- VectorIndex:sqlite-vec 向量列(FLOAT BLOB),KNN 近邻查询;
  sqlite-vec 扩展加载失败时自动降级为"仅 FTS5 词法"(功能可用性优先);
- rrf_fuse:混合检索融合——词法(FTS5 bm25)与向量(KNN)两路召回按
  Reciprocal Rank Fusion 加权合并,单路失明时另一路兜底。

Embedding 接入:任意 OpenAI 兼容 /embeddings 端点(OTTER_EMBED_BASE_URL +
OTTER_EMBED_MODEL,如 Qwen text-embedding-v3 / Ollama bge-m3 / OpenAI
text-embedding-3-small);未配置时向量分支整体关闭(纯词法检索,行为与
之前完全一致)。embed_fn 可注入,测试与生产同一代码路径。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from otter.tools.base import Tool

# sqlite-vec 可选:加载失败只丢向量分支,词法检索不受影响
try:
    import sqlite_vec as _sqlite_vec  # 2026-09-29 P5:向量列扩展(可降级依赖)

    _VEC_OK = True
except ImportError:  # pragma: no cover - 环境相关
    _sqlite_vec = None
    _VEC_OK = False


# ── 分片(Embedding 前置:长文档切成可检索的小段)──────────────────────

_SENT_SPLIT = re.compile(r"(?<=[。!?;;\n])")  # 中文句号/问叹号/分号/换行后切


def chunk_text(text: str, size: int = 400, overlap: int = 60) -> list[str]:
    """文本分片:段落聚合到 ~size 字;超长段按句子切;句子仍超长按定宽回退。

    overlap 是相邻片的重叠字数——切断处的语义在下一片仍可见,减少边界漏召回。
    """
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras:
        if len(buf) + len(para) + 1 <= size:
            buf = f"{buf}\n{para}".strip()
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(para) <= size:
            buf = para
            continue
        # 超长段落:按句子聚合;单句超长再定宽切
        for sent in _SENT_SPLIT.split(para):
            sent = sent.strip()
            if not sent:
                continue
            while len(sent) > size:  # 定宽回退(无标点的长串,如 base64/长 URL)
                chunks.append(sent[:size])
                sent = sent[size - overlap:]
            if len(buf) + len(sent) + 1 <= size:
                buf = f"{buf}{sent}".strip()
            else:
                if buf:
                    chunks.append(buf)
                buf = sent
    if buf:
        chunks.append(buf)
    # 重叠是聚合策略的近似:上面的句子级拼接天然带上下文,这里不再补切
    return chunks


# ── Embedding 客户端(OpenAI 兼容 /embeddings)────────────────────────

class EmbeddingClient:
    """OpenAI 兼容 embeddings 端点的最小封装(batch 一次请求)。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=self._base, api_key=self._key, timeout=60)
        try:
            resp = await client.embeddings.create(model=self.model, input=texts)
            return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
        finally:
            await client.close()


def build_embed_client_from_env() -> EmbeddingClient | None:
    """按环境配置构造 Embedding 客户端;未配置模型名 → None(向量分支关闭)。

    2026-09-29 补 OTTER_EMBED_API_KEY:embedding 与 chat 厂商可不同
    (如 DeepSeek 聊天 + 硅基流动 embedding),此时各用各的 key;
    缺省回退主 key(同厂商或 Ollama 本地不校验 key 的场景无感)。
    """
    import os

    model = os.environ.get("OTTER_EMBED_MODEL", "")
    if not model:
        return None
    base = os.environ.get("OTTER_EMBED_BASE_URL") or os.environ.get("OTTER_BASE_URL", "")
    key = (os.environ.get("OTTER_EMBED_API_KEY")
           or os.environ.get("OTTER_API_KEY", ""))
    if not (base and key):
        return None
    return EmbeddingClient(base, key, model)


# ── 向量索引(sqlite-vec)────────────────────────────────────────────

@dataclass
class RagHit:
    """一路检索的命中:键(条目/分片 id)与文本,供融合与展示。"""

    key: str
    text: str = ""
    score: float = 0.0
    source: str = ""


class RagIndex:
    """持久化混合索引:向量列(sqlite-vec vec0 虚拟表)+ 词法列(FTS5)。

    同一 db 文件承载两路;dim 记在 meta(换 Embedding 模型维度变化 → 自动清表重建)。
    """

    def __init__(self, db_path: Path, dim: int) -> None:
        self.db_path = db_path
        self.dim = dim
        self._db: sqlite3.Connection | None = None
        self._vec_ok = _VEC_OK  # 实例级:连接失败也会翻回 False

    def _connect(self) -> sqlite3.Connection:
        if self._db is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.db_path)
            try:
                if _VEC_OK:
                    db.enable_load_extension(True)
                    _sqlite_vec.load(db)
                    db.enable_load_extension(False)
            except sqlite3.Error:  # 扩展不可用:向量分支降级关闭
                self._vec_ok = False
            self._db = db
            self._ensure_schema(db)
        return self._db

    def _ensure_schema(self, db: sqlite3.Connection) -> None:
        row = db.execute("SELECT v FROM meta WHERE k='dim'").fetchone() \
            if self._has_table(db, "meta") else None
        if row is not None and int(row[0]) != self.dim:
            # 维度变化(换了 Embedding 模型):旧向量不可比,清空重建
            for t in ("rag_vec", "rag_fts", "chunks", "meta"):
                try:
                    db.execute(f"DROP TABLE IF EXISTS {t}")
                except sqlite3.Error:
                    pass
        db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS chunks("
                   "chunk_id TEXT PRIMARY KEY, source TEXT, text TEXT, hash TEXT)")
        if self._vec_ok and self.dim > 0:  # dim=0(纯词法降级)不建向量列
            db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS rag_vec USING vec0("
                       f"chunk_id TEXT PRIMARY KEY, embedding float[{self.dim}])")
        # 2026-09-29 trigram tokenizer:中文连续串在默认 unicode61 下整串成一个
        # token(「紧急插单」查不中「紧急插单流程」),trigram 才有子串语义
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5"
                   "(chunk_id UNINDEXED, text, tokenize='trigram')")
        db.execute("INSERT OR REPLACE INTO meta VALUES('dim', ?)", (str(self.dim),))
        db.commit()

    @staticmethod
    def _has_table(db: sqlite3.Connection, name: str) -> bool:
        return bool(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())

    def upsert(self, items: list[tuple[str, str, str, list[float] | None]]) -> None:
        """写入/更新分片:(chunk_id, source, text, embedding)。

        embedding 为 None(向量分支关闭)时只进词法索引;chunk_id 以内容 hash
        为准的调用方自带幂等(内容未变不重复 embed)。
        """
        db = self._connect()
        for chunk_id, source, text, vec in items:
            h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            db.execute("INSERT OR REPLACE INTO chunks VALUES(?,?,?,?)",
                       (chunk_id, source, text, h))
            db.execute("DELETE FROM rag_fts WHERE chunk_id=?", (chunk_id,))
            db.execute("INSERT INTO rag_fts(chunk_id, text) VALUES(?,?)", (chunk_id, text))
            if self._vec_ok and vec is not None:
                import struct

                blob = struct.pack(f"{len(vec)}f", *vec)
                db.execute("INSERT OR REPLACE INTO rag_vec(chunk_id, embedding) VALUES(?,?)",
                           (chunk_id, blob))
        db.commit()

    def stale(self, chunk_id: str, text: str) -> bool:
        """该分片是否需要重建(不存在或内容 hash 变化)。"""
        db = self._connect()
        row = db.execute("SELECT hash FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        want = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        return row is None or row[0] != want

    def knn(self, vec: list[float], k: int = 5) -> list[RagHit]:
        """向量 KNN 近邻(sqlite-vec;扩展不可用时返回空)。"""
        if not self._vec_ok:
            return []
        db = self._connect()
        import struct

        blob = struct.pack(f"{len(vec)}f", *vec)
        rows = db.execute(
            "SELECT chunk_id, distance FROM rag_vec WHERE embedding MATCH ? "
            "AND k = ? ORDER BY distance", (blob, k)).fetchall()
        out: list[RagHit] = []
        for chunk_id, dist in rows:
            r = db.execute("SELECT source, text FROM chunks WHERE chunk_id=?",
                           (chunk_id,)).fetchone()
            if r:
                out.append(RagHit(key=chunk_id, text=r[1], score=-float(dist), source=r[0]))
        return out

    def fts(self, query: str, limit: int = 5) -> list[RagHit]:
        """词法检索:≥3 字词走 FTS5 trigram(bm25);2 字短词 trigram 产不了
        token,退 LIKE 子串扫描(分片量小,代价可接受)。"""
        db = self._connect()
        q = query.strip()
        if len(q) < 2:
            return []
        tokens = [t for t in re.split(r"[\s,，。;；、]+", q) if len(t) >= 2]
        tri = [t for t in tokens if len(t) >= 3]
        short = [t for t in tokens if len(t) == 2]
        hits: list[RagHit] = []
        seen: set[str] = set()
        if tri:
            match = " OR ".join(f'"{t}"' for t in tri)  # trigram 子串语义,不加前缀 *
            try:
                rows = db.execute(
                    "SELECT chunk_id, text FROM rag_fts WHERE rag_fts MATCH ? "
                    "ORDER BY bm25(rag_fts) LIMIT ?", (match, limit)).fetchall()
                for c, t in rows:
                    hits.append(RagHit(key=c, text=t))
                    seen.add(c)
            except sqlite3.OperationalError:
                pass
        if short:  # 2 字短词兜底:子串扫描
            conds = " OR ".join("text LIKE ?" for _ in short)
            params = [f"%{t}%" for t in short] + [limit]
            try:
                for c, t in db.execute(
                        f"SELECT chunk_id, text FROM chunks WHERE {conds} "
                        "LIMIT ?", params).fetchall():
                    if c not in seen:
                        hits.append(RagHit(key=c, text=t))
                        seen.add(c)
            except sqlite3.OperationalError:
                pass
        return hits[:limit]

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None


# ── 混合检索融合(Reciprocal Rank Fusion)────────────────────────────

def rrf_fuse(*rankings: list[RagHit], k: int = 60) -> list[RagHit]:
    """多路召回按 RRF 融合:score = Σ 1/(k + rank)。

    k=60 是常用默认(平滑头部,单路第一名不能碾压两路共识);
    同一 key 在多路出现则分数累加——词法与语义都认为相关的条目排最前。
    """
    scores: dict[str, float] = {}
    best: dict[str, RagHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, 1):
            scores[hit.key] = scores.get(hit.key, 0.0) + 1.0 / (k + rank)
            cur = best.get(hit.key)
            if cur is None or hit.score > cur.score:
                best[hit.key] = hit
    fused = [RagHit(key=key, text=best[key].text, score=s, source=best[key].source)
             for key, s in scores.items()]
    fused.sort(key=lambda h: -h.score)
    return fused


# ── 场景一:Ordinary 记忆混合检索(memory_search 的向量增强)──────────

_MEMORY_INDEX: dict[Path, RagIndex] = {}  # 每记忆目录一个索引(进程内复用连接)


async def memory_hybrid_search(store, query: str, limit: int = 5,
                               embed_client: EmbeddingClient | None = None,
                               embed_fn: Callable[[list[str]], Any] | None = None
                               ) -> list:
    """记忆混合检索:FTS5(现有 store.search)∪ 向量(KNN),RRF 融合。

    embed_fn 可注入(测试假向量);生产路径 embed_client.embed。
    向量索引惰性构建:active 条目中内容变化的才(重)embed,Embedding 失败静默
    降级为纯词法结果(检索可用性优先,不为向量分支付中断代价)。
    返回 MemoryEntry 列表(融合排序),兼容 FileMemoryStore.search 调用方。
    """
    entries = store.list_active()
    by_mid = {e.mid: e for e in entries}
    fts_hits = store.search(query, limit=limit)  # 现有词法召回(已按 bm25 排序)

    if embed_client is None and embed_fn is None:
        return fts_hits  # 未配置 Embedding:行为与之前完全一致(纯词法)

    async def embed(texts: list[str]) -> list[list[float]]:
        return await (embed_fn(texts) if embed_fn is not None
                      else embed_client.embed(texts))  # type: ignore[union-attr]

    vec_as_mids: list[str] = []
    try:
        texts = [f"{e.title}\n{e.summary}\n{e.content[:2000]}" for e in entries]
        vecs = await embed(texts) if texts else []
        dim = len(vecs[0]) if vecs else 0
        index = _MEMORY_INDEX.get(store.root)
        if index is None or index.dim != dim:
            index = RagIndex(store.root / "rag.sqlite", dim)
            _MEMORY_INDEX[store.root] = index
        items = [(f"mem:{e.mid}", str(e.mid), text, vec)
                 for e, text, vec in zip(entries, texts, vecs)
                 if index.stale(f"mem:{e.mid}", text)]
        if items:
            index.upsert(items)
        qvec = (await embed([query]))[0]
        vec_as_mids = [h.source for h in index.knn(qvec, k=limit) if h.source in by_mid]
    except Exception:
        return fts_hits  # Embedding/向量分支任何失败:静默回纯词法(降级取向)

    fused = rrf_fuse([RagHit(key=e.mid) for e in fts_hits],
                     [RagHit(key=m) for m in vec_as_mids])
    return [by_mid[h.key] for h in fused if h.key in by_mid][:limit]


# ── 场景二:文档 QA(doc_search 工具)────────────────────────────────

_DOC_EXTS = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".csv", ".html", ".log"}
_DOC_INDEX: dict[Path, RagIndex] = {}  # 每文档根一个索引(进程内复用连接)


async def doc_hybrid_search(root: Path, query: str, limit: int = 5,
                            embed_client: EmbeddingClient | None = None,
                            embed_fn: Callable[[list[str]], Any] | None = None
                            ) -> list[RagHit]:
    """文档混合检索:分片 → 惰性 Embedding → FTS5 ∪ KNN → RRF。

    增量索引:分片内容 hash 未变不重新 embed;文件删除后条目自然失配(不主动清)。
    索引落 <根>/.otter/rag/docs.sqlite;chunk_id 用相对路径(#序号),可跨次复用。
    """
    base = root if root.is_dir() else root.parent
    files = [root] if root.is_file() else sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _DOC_EXTS
        and ".otter" not in p.parts)[:60]
    chunks: list[tuple[str, str]] = []  # (chunk_id, text)
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, piece in enumerate(chunk_text(text)):
            chunks.append((str(f.relative_to(base)) + f"#{i}", piece))

    async def embed(texts: list[str]) -> list[list[float]]:
        return await (embed_fn(texts) if embed_fn is not None
                      else embed_client.embed(texts))  # type: ignore[union-attr]

    # 维度探测:embed 首个分片一次;无 Embedding 配置 → dim=0(纯词法索引)
    if (embed_client is not None or embed_fn is not None) and chunks:
        try:
            probe = await embed([chunks[0][1]])
            dim = len(probe[0]) if probe else 0
        except Exception:
            dim = -1  # Embedding 暂时失败:本轮退纯词法
    else:
        dim = 0
    index = _DOC_INDEX.get(base)
    if index is None or index.dim != max(dim, 0):
        index = RagIndex(base / ".otter" / "rag" / "docs.sqlite", max(dim, 0))
        _DOC_INDEX[base] = index

    knn_hits: list[RagHit] = []
    if dim > 0:
        try:
            todo = [(cid, text) for cid, text in chunks if index.stale(cid, text)]
            if todo:
                vecs = await embed([t for _, t in todo])
                index.upsert([(cid, str(base), text, vec)
                              for (cid, text), vec in zip(todo, vecs)])
            qv = (await embed([query]))[0]
            knn_hits = index.knn(qv, k=limit)
        except Exception:
            knn_hits = []  # 向量分支失败:退纯词法

    # 词法路:补齐未入库分片后查 FTS5(vec=None 只进词法列)
    lexical_todo = [(cid, text) for cid, text in chunks if index.stale(cid, text)]
    if lexical_todo:
        index.upsert([(cid, str(base), text, None) for cid, text in lexical_todo])
    fts_hits = index.fts(query, limit=limit)
    return rrf_fuse(knn_hits, fts_hits)[:limit]



class DocSearchTool(Tool):
    """doc_search(2026-09-29 P5):文档 QA——分片 + Embedding + 混合检索。

    语义查文档("讲排产的那段在哪个文件")比逐词 grep 更宽容;
    未配置 OTTER_EMBED_MODEL 时自动退纯词法(FTS5),工具仍可用。
    """

    name = "doc_search"
    description = (
        "语义检索文档(向量检索+分片+混合检索):给定文件或目录与自然语言问题,"
        "返回最相关的原文片段与出处。适合「哪份文档讲过 X」「在哪定义的」这类"
        "词面不完全匹配的查找;配置 OTTER_EMBED_MODEL 后支持中文语义召回。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件或目录(目录递归取常见文档类型)"},
            "query": {"type": "string", "description": "自然语言问题/关键词"},
            "limit": {"type": "integer", "description": "返回片段数(默认 5)"},
        },
        "required": ["path", "query"],
    }

    async def run(self, args) -> str:
        raw = str(args.get("path", "")).strip()
        if not raw:
            return "[otter] 错误:path 为空"
        root = Path(raw).expanduser()
        if not root.exists():
            return f"[otter] 路径不存在:{raw}"
        limit = int(args.get("limit", 5) or 5)
        hits = await doc_hybrid_search(root, str(args.get("query", "")), limit=limit,
                                       embed_client=build_embed_client_from_env())
        if not hits:
            return "[otter] 没有检索到相关片段"
        return "\n\n".join(f"◆ {h.key}\n{h.text[:400]}" for h in hits)
