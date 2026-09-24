"""长期记忆页数据快照(_memory_payload)的离线测试(2026-09-24 新增 GUI 页配套)。

纯读契约:目录缺失/CORE.md 损坏不炸;front matter 解析正确;非 active 不展示;
查看动作零写副作用(不创建 active/、不建 memory.sqlite 投影)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from otter.gui import _memory_payload


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_memory_payload_empty_when_missing(tmp_path):
    """目录整体不存在:空快照,不炸不建。"""
    out = _memory_payload(tmp_path / "nonexistent")
    assert out["core"] == [] and out["entries"] == []


def test_memory_payload_core_and_entries(tmp_path):
    root = tmp_path / "memory"
    _write(root, "CORE.md", json.dumps({"entries": [
        {"key": "偏好", "value": "结论先行", "reason": "用户要求",
         "source_quote": "给我结论先行", "updated_at": "2026-09-24"}]}, ensure_ascii=False))
    _write(root, "active/M001.md",
           "---\ntitle: 排版偏好\nsummary: 用户要简洁\nrevision: 2\naccess_count: 3\n"
           "status: active\nid: M001\n---\n正文内容第一行\n第二行\n")
    _write(root, "active/M002.md",
           "---\ntitle: 已归档\nsummary: x\nrevision: 1\naccess_count: 0\n"
           "status: archived\nid: M002\n---\n不该出现\n")
    out = _memory_payload(root)
    assert [e["key"] for e in out["core"]] == ["偏好"]
    assert out["core"][0]["source_quote"] == "给我结论先行"
    assert len(out["entries"]) == 1  # archived 状态不进快照(与 list_active 同口径)
    e = out["entries"][0]
    assert e["mid"] == "M001" and e["title"] == "排版偏好" and e["summary"] == "用户要简洁"
    assert e["revision"] == 2 and e["access_count"] == 3
    assert "正文内容第一行" in e["content"]
    assert e["mtime"]  # 文件 mtime 已富化为可读时间


def test_memory_payload_broken_core_json(tmp_path):
    """CORE.md 损坏 JSON:core 降级为空列表,查看页不崩(CoreMemory.load 既有容错)。"""
    root = tmp_path / "memory"
    _write(root, "CORE.md", "{broken json")
    out = _memory_payload(root)
    assert out["core"] == []


def test_memory_payload_no_side_effect(tmp_path):
    """查看页纯读铁律:不创建 active 目录、不建 memory.sqlite/INDEX.md 投影
    (刻意不走 FileMemoryStore,其构造带 mkdir+建 FTS 的写副作用)。"""
    root = tmp_path / "memory"
    _write(root, "CORE.md", json.dumps({"entries": []}))
    _memory_payload(root)
    assert not (root / "active").exists()
    assert not (root / "memory.sqlite").exists()
    assert not (root / "INDEX.md").exists()
