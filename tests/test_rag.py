"""RAG 向量检索测试(2026-09-29 P5)。

核心验收:中文语义查询(词面与条目无公共词)能经向量路召回纯 FTS5
召不回的条目;RRF 融合排序;未配置 Embedding 时行为与之前一致(纯词法)。
向量用注入的确定性假 embed(同义近、异义远),不依赖真实端点。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from otter.rag import (
    RagIndex,
    chunk_text,
    doc_hybrid_search,
    memory_hybrid_search,
    rrf_fuse,
)
from otter.rag import RagHit


# ── 分片 ────────────────────────────────────────────────────────────

def test_chunk_paragraph_and_sentence(tmp_path: Path):
    paras = "\n\n".join(f"第{i}段。" + "内容" * 10 for i in range(4))
    chunks = chunk_text(paras, size=60, overlap=10)
    assert all(len(c) <= 80 for c in chunks)  # 段落聚合不越界(留余量)
    assert "".join(c.replace("\n", "") for c in chunks).count("第0段") == 1

    one = chunk_text("短文", size=100)
    assert one == ["短文"]
    assert chunk_text("") == []

    long_line = "字" * 500  # 无标点长串:定宽回退
    fixed = chunk_text(long_line, size=100, overlap=20)
    assert all(len(c) <= 100 for c in fixed) and len(fixed) >= 6


# ── RRF 融合 ────────────────────────────────────────────────────────

def test_rrf_fuse_consensus_wins():
    a = [RagHit(key="x", text=""), RagHit(key="y", text=""), RagHit(key="z", text="")]
    b = [RagHit(key="y", text=""), RagHit(key="w", text=""), RagHit(key="x", text="")]
    fused = rrf_fuse(a, b)
    keys = [h.key for h in fused]
    assert keys[0] == "y" and keys[1] == "x"  # 两路共识(累加分数)排最前
    # y(两路均靠前:第2+第1)应压过 x(第1+第3)——RRF 鼓励双路共识而非单路冠军
    assert set(keys) == {"x", "y", "z", "w"}  # 单路命中也保留


# ── 向量索引(sqlite-vec KNN)───────────────────────────────────────

def test_vector_index_knn(tmp_path: Path):
    idx = RagIndex(tmp_path / "rag.db", dim=3)
    idx.upsert([
        ("a", "s1", "苹果", [1.0, 0.0, 0.0]),
        ("b", "s1", "香蕉", [0.6, 0.8, 0.0]),
        ("c", "s1", "樱桃", [0.0, 0.0, 1.0]),
    ])
    # 余弦实际值:q=[0.8,0.6,0] 归一化后 sim(b)=0.96 > sim(a)=0.8 > sim(c)=0
    hits = idx.knn([0.8, 0.6, 0.0], k=3)
    assert hits[0].key == "b" and hits[1].key == "a"
    assert {h.key for h in hits} == {"a", "b"}  # c 正交(相似度≈0)被默认阈值滤掉
    # 2026-09-29 余弦体系:score=相似度;弱命中不进融合
    assert all(h.score >= idx.min_similarity for h in hits)
    assert hits[0].score > hits[1].score
    # 收紧阈值=0.9:a(0.8)也低于线,只剩 b —— 噪声过滤行为可控
    strict = RagIndex(tmp_path / "t2.db", dim=3, min_similarity=0.9)
    strict.upsert([
        ("a", "s1", "苹果", [1.0, 0.0, 0.0]),
        ("b", "s1", "香蕉", [0.6, 0.8, 0.0]),
        ("c", "s1", "樱桃", [0.0, 0.0, 1.0]),
    ])
    assert [h.key for h in strict.knn([0.8, 0.6, 0.0], k=3)] == ["b"]
    # 内容变化 → stale 标记重建;未变化 → 不重建(幂等)
    assert idx.stale("a", "苹果XP") and not idx.stale("a", "苹果")
    idx.close()


# ── 核心验收:中文语义召回(向量路召回纯 FTS5 召不回的条目)────────────

def _semantic_embed(texts: list[str]) -> list[list[float]]:
    """确定性假向量:话题轴(水果/交通)+ 细节轴。同话题近,跨话题远。
    「水果的营养」≈「苹果与维生素」,但两者无公共中文词——纯 trigram FTS5
    必然召不回,向量路可以。"""

    def vec(t: str) -> list[float]:
        fruit = sum(k in t for k in ("水果", "苹果", "香蕉", "维生素", "营养", "维C"))
        traffic = sum(k in t for k in ("地铁", "通勤", "公交", "交通", "早高峰"))
        return [float(fruit), float(traffic), 0.1]

    return [vec(t) for t in texts]


def test_memory_semantic_recall_beyond_fts5(tmp_path: Path, monkeypatch):
    """记忆混合检索:语义查询「哪种水果维生素多」召回「苹果与维生素」条目,
    而纯词法(memory 关键词不含查询词)召不回同一批。"""
    monkeypatch.chdir(tmp_path)
    from otter.memory import FileMemoryStore

    store = FileMemoryStore(tmp_path / "memory")
    store.create("出行习惯", "每天坐地铁通勤,避开早高峰",
                 "用户偏好地铁通勤,工作日 8 点前出门可避开早高峰。")
    target = store.create("苹果与维生素", "苹果富含维C,每天一个",
                          "用户每天吃一个苹果补充维生素,偏爱红富士。")
    store.create("项目约束", "发布会物料必须提前三天冻结",
                 "物料清单在任何变更前需要负责人会签。")

    query = "哪种水果营养好"  # 与目标条目无公共检索词(营养 vs 维生素/维C)
    fts_only = store.search(query, limit=3)
    assert all(e.mid != target.mid for e in fts_only)  # 纯词法召不回(前提成立)

    async def fake_embed(texts):
        return _semantic_embed(texts)

    hybrid = asyncio.run(memory_hybrid_search(store, query, limit=3, embed_fn=fake_embed))
    assert hybrid and hybrid[0].mid == target.mid  # 向量路把它救回来了,且排第一

    # 未注入 embed(向量分支关闭):与纯词法完全一致(行为不变验收)
    plain = asyncio.run(memory_hybrid_search(store, query, limit=3))
    assert [e.mid for e in plain] == [e.mid for e in fts_only]


def test_doc_search_semantic_and_lexical(tmp_path: Path, monkeypatch):
    """文档 QA:分片入库后,语义查询经向量路命中;词面匹配查询走词法路。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.md").write_text(
        "# 保养手册\n\n设备每月需要润滑一次,轴承温度超过八十度要停机检查。\n", encoding="utf-8")
    (tmp_path / "traffic.md").write_text(
        "# 通勤指南\n\n早高峰的地铁非常拥挤,建议错峰出行。\n", encoding="utf-8")

    async def fake_embed(texts):
        return _semantic_embed(texts)

    # 语义查询:词面(水果/营养)与两份文档都不相交——FTS5 空,向量路仍需给出合理结果
    hits = asyncio.run(doc_hybrid_search(tmp_path, "水果的营养价值", limit=2,
                                         embed_fn=fake_embed))
    assert all("plan.md" in h.key or "traffic.md" in h.key for h in hits)  # 不空即证明向量路在工作

    # 词面查询:词法路直接命中
    hits2 = asyncio.run(doc_hybrid_search(tmp_path, "润滑 轴承", limit=2))
    assert hits2 and "plan.md" in hits2[0].key

    # 未配置 Embedding:纯词法仍可用(降级路径)
    hits3 = asyncio.run(doc_hybrid_search(tmp_path, "通勤", limit=2))
    assert hits3 and "traffic.md" in hits3[0].key


def test_embed_client_env_config(monkeypatch, tmp_path: Path):
    """Embedding 客户端环境配置:未配模型→None;独立 key 优先;缺省回退主 key。"""
    from otter.rag import build_embed_client_from_env

    monkeypatch.delenv("OTTER_EMBED_MODEL", raising=False)
    assert build_embed_client_from_env() is None  # 未配置=向量分支关闭

    env = {"OTTER_EMBED_MODEL": "bge-m3", "OTTER_EMBED_BASE_URL": "http://localhost:11434/v1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("OTTER_API_KEY", "sk-main")
    monkeypatch.delenv("OTTER_EMBED_API_KEY", raising=False)
    c = build_embed_client_from_env()
    assert c is not None and c.model == "bge-m3"
    # 缺省回退主 key(同厂商或 Ollama 不校验场景)
    assert c._key == "sk-main"
    # 2026-09-29 补:embedding 厂商与 chat 不同时,独立 key 优先
    monkeypatch.setenv("OTTER_EMBED_API_KEY", "sk-embed")
    assert build_embed_client_from_env()._key == "sk-embed"


def test_backfill_then_search_no_remote_calls(tmp_path: Path, monkeypatch):
    """后台补全:backfill 后再次检索,查询路径零 embed 调用(零等待验收)。"""
    monkeypatch.chdir(tmp_path)
    from otter.memory import FileMemoryStore
    from otter.rag import backfill_memory_embeddings

    store = FileMemoryStore(tmp_path / "memory")
    store.create("苹果与维生素", "苹果富含维C", "用户每天吃一个苹果补充维生素。")
    store.create("出行", "地铁通勤", "用户偏好地铁通勤。")

    calls = {"n": 0}

    async def counting_embed(texts):
        calls["n"] += len(texts)
        return _semantic_embed(texts)

    asyncio.run(backfill_memory_embeddings(store, embed_fn=counting_embed))
    assert calls["n"] > 0  # 首轮:条目 + 查询预对账发生了远程调用
    calls["n"] = 0
    hits = asyncio.run(memory_hybrid_search(store, "哪种水果营养好",
                                            embed_fn=counting_embed))
    assert calls["n"] == 1  # 查询侧只剩 query 自身 1 次(条目零重算)
    assert hits  # 检索仍正常返回


def test_doc_search_dedup_per_document(tmp_path: Path, monkeypatch):
    """按文档去重:长文档多片命中只计一次 RRF 分,不凭块数碾压目标文档。"""
    monkeypatch.chdir(tmp_path)
    # target.md:单文档单段(命中一次);noise.md:同主题切成多段(片片沾边)
    (tmp_path / "target.md").write_text("紧急插单必须先核对在制品再压缩换型。\n", encoding="utf-8")
    (tmp_path / "noise.md").write_text(
        "插单流程第一歩。\n\n插单流程第二歩。\n\n插单流程第三歩。\n\n插单流程第四歩。\n\n插单流程第五歩。\n\n插单流程第六歩。\n\n插单流程第七歩。\n\n插单流程第八歩。\n",
        encoding="utf-8")

    hits = asyncio.run(doc_hybrid_search(tmp_path, "紧急插单", limit=5))
    docs = [h.key.split("#")[0] for h in hits]
    assert len(docs) == len(set(docs))  # 结果里同一文档最多一片
    assert "target.md" in docs  # 目标文档在场(不被多片噪声挤出前 5)
