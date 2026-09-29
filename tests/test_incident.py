"""故障现场自动归档(bug 监测第 4 层)测试:事故包内容 / 去重 / 巡逻集成触发。

运行:python -m pytest tests/test_incident.py -v(asyncio.run 手动驱动,同 test_patrol 风格)
2026-09-29 建立:与 otter/incident.py 同批落地。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from otter import patrol
from otter.incident import archive_incident
from otter.models.types import Message
from otter.store import Store


def _mkstore(tmp_path: Path) -> Store:
    """真 Store(临时库):归档测试走真实 events/runs/messages 读写路径。"""

    async def go() -> Store:
        s = Store(tmp_path / ".otter" / "otter.db")
        await s.open()
        return s

    return asyncio.run(go())


def _seed_failed_run(store: Store) -> int:
    """造一个带 MODEL_ERROR 事件的失败 Run(high 信号)+ 两条消息。"""

    async def go() -> int:
        rid = await store.new_run()
        await store.append_message(Message(role="user", content="帮我把报表导出"),
                                   0, rid)
        await store.append_message(
            Message(role="assistant", content="出错了",
                    tool_calls=[]), 1, rid)
        await store.append_event(rid, "MODEL_ERROR", {"error": "401 unauthorized"})
        await store.finish_run(rid, "failed", "model_error")
        return rid

    return asyncio.run(go())


def _run_row(store: Store, rid: int) -> dict:

    async def go() -> dict:
        cur = await store._db.execute(
            "SELECT id, status, stop_reason, started_at, parent_run_id FROM runs WHERE id=?",
            (rid,))
        r = await cur.fetchone()
        return {"id": r[0], "status": r[1], "stop_reason": r[2],
                "started_at": r[3], "parent_run_id": r[4]}

    return asyncio.run(go())


FINDINGS = [{"run_id": None, "kind": "failed", "severity": "high",
             "detail": "Run #1 失败(status=failed, stop=model_error)", "is_subagent": False}]


def _archive(store: Store, run: dict, ws: Path):
    return asyncio.run(archive_incident(store, run, FINDINGS, ws))


def test_archive_writes_json_and_readme(tmp_path):
    store = _mkstore(tmp_path)
    rid = _seed_failed_run(store)
    run = _run_row(store, rid)
    (tmp_path / "report.xlsx").write_bytes(b"fake")  # 现场文件(进清单,不拷内容)

    path = _archive(store, run, tmp_path)
    assert path is not None and path == tmp_path / ".otter" / "incidents" / f"run-{rid}"

    data = json.loads((path / "incident.json").read_text(encoding="utf-8"))
    assert data["run"]["status"] == "failed"
    assert data["events"][0]["type"] == "MODEL_ERROR"
    assert data["messages"][0]["content"] == "帮我把报表导出"
    assert data["findings"][0]["severity"] == "high"
    # 文件清单:现场文件在列,.otter(事故包自身)被排除
    inv_paths = [f["path"] for f in data["file_inventory"]]
    assert "report.xlsx" in inv_paths and not any(p.startswith(".otter") for p in inv_paths)

    readme = (path / "README.md").read_text(encoding="utf-8")
    assert "Run #1" in readme and "MODEL_ERROR" in readme and "report.xlsx" in readme
    asyncio.run(store.close())


def test_archive_dedupes_second_call(tmp_path):
    store = _mkstore(tmp_path)
    rid = _seed_failed_run(store)
    run = _run_row(store, rid)

    assert _archive(store, run, tmp_path) is not None
    # 巡逻可反复跑:同一 Run 只留一次现场(重复归档返回 None)
    assert _archive(store, run, tmp_path) is None
    asyncio.run(store.close())


def test_patrol_archives_high_finding_runs(tmp_path):
    """集成:run_patrol 扫到 high → 自动归档 + 退出码 1(第 4 层挂第 2 层)。"""
    store = _mkstore(tmp_path)
    rid = _seed_failed_run(store)
    asyncio.run(store.close())

    code = patrol.run_patrol(hours=1.0, workspace=tmp_path, notify=False)
    assert code == 1  # high 信号
    base = tmp_path / ".otter" / "incidents" / f"run-{rid}"
    assert (base / "incident.json").is_file() and (base / "README.md").is_file()

    # 再巡逻一次:去重(不新增包),退出码仍 1
    code2 = patrol.run_patrol(hours=1.0, workspace=tmp_path, notify=False)
    assert code2 == 1
    assert (base / "incident.json").is_file()
