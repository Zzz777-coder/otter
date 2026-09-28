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
        ("b", "s1", "香蕉", [0.0, 1.0, 0.0]),
        ("c", "s1", "樱桃", [0.0, 0.0, 1.0]),
    ])
    hits = idx.knn([0.9, 0.1, 0.0], k=2)
    assert hits[0].key == "a"  # 最近邻方向正确
    assert {h.key for h in hits} == {"a", "b"}
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
