"""交付物页数据快照(_artifacts_payload)的离线测试(2026-09-24 新增 GUI 页配套)。

纯读契约:无索引空列表;坏行跳过;倒序(最新在前);查看动作不创建 artifacts 目录。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from otter.gui import _artifacts_payload


def _write_index(root: Path, records: list[dict]) -> None:
    d = root / ".otter" / "artifacts"
    d.mkdir(parents=True)
    (d / "index.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")


def test_artifacts_payload_empty_when_missing(tmp_path, monkeypatch):
    """索引不存在:空列表,且不创建 artifacts 目录(纯读,无 mkdir 副作用)。"""
    monkeypatch.chdir(tmp_path)  # _artifacts_payload 读 Path.cwd()
    out = _artifacts_payload()
    assert out == []
    assert not (tmp_path / ".otter" / "artifacts").exists()


def test_artifacts_payload_order_and_bad_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_index(tmp_path, [
        {"id": "20260924-1", "name": "old.txt", "size": 5},
        {"id": "20260924-2", "name": "new.pdf", "size": 100, "note": "报告", "run_id": 7},
    ])
    # 在合法行之间夹一行坏 JSON(不完整行)
    idx = tmp_path / ".otter" / "artifacts" / "index.jsonl"
    lines = idx.read_text(encoding="utf-8").splitlines()
    idx.write_text("\n".join([lines[0], "{broken", lines[1]]) + "\n", encoding="utf-8")
    out = _artifacts_payload()
    assert [a["name"] for a in out] == ["new.pdf", "old.txt"]  # 倒序+坏行跳过
    assert out[0]["note"] == "报告" and out[0]["run_id"] == 7


def test_artifacts_payload_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_index(tmp_path, [{"id": f"i{n}", "name": f"a{n}.txt", "size": 1} for n in range(60)])
    assert len(_artifacts_payload()) == 50  # 上限 50,取最新
