"""最近工作区登记(otter.workspaces)的离线测试(2026-09-24 新增,GUI 跨工作区查看历史配套)。

覆盖:登记去重置顶 / 上限裁剪 / 不存在目录过滤 / 坏 JSON 容错 / 写失败静默。
全部走 recent_file 参数注入临时文件,不碰真实 ~/.otter/。
"""

from __future__ import annotations

import json
from pathlib import Path

from otter.workspaces import RECENT_LIMIT, list_workspaces, register_workspace


def test_register_dedup_and_move_to_front(tmp_path: Path) -> None:
    """重复登记同一工作区:去重并置顶(最近使用优先),不产生重复条目。"""
    f = tmp_path / "recent.json"
    # 修正(2026-09-24):list 会过滤不存在的目录——登记的工作区必须是真目录
    for name in ("projA", "projB"):
        (tmp_path / name).mkdir()
    register_workspace(tmp_path / "projA", recent_file=f)
    register_workspace(tmp_path / "projB", recent_file=f)
    register_workspace(tmp_path / "projA", recent_file=f)  # 再次使用 A → 置顶
    out = list_workspaces(recent_file=f)
    assert [Path(w["path"]).name for w in out] == ["projA", "projB"]


def test_register_limit(tmp_path: Path) -> None:
    """超过上限:只保留最近 RECENT_LIMIT 个(最旧的被裁掉)。"""
    f = tmp_path / "recent.json"
    for i in range(RECENT_LIMIT + 3):
        (tmp_path / f"p{i}").mkdir()  # 真目录(list 过滤不存在的路径)
        register_workspace(tmp_path / f"p{i}", recent_file=f)
    out = list_workspaces(recent_file=f)
    assert len(out) == RECENT_LIMIT
    # 最新登记的在最前,最旧的 p0/p1/p2 已被裁掉
    assert Path(out[0]["path"]).name == f"p{RECENT_LIMIT + 2}"
    assert "p0" not in [Path(w["path"]).name for w in out]


def test_list_filters_missing_dirs(tmp_path: Path) -> None:
    """已删除的目录不再出现在清单里(过滤 is_dir)。"""
    f = tmp_path / "recent.json"
    gone = tmp_path / "gone"
    alive = tmp_path / "alive"
    gone.mkdir()
    alive.mkdir()  # 修正(2026-09-24):存活的工作区也得是真目录
    register_workspace(gone, recent_file=f)
    register_workspace(alive, recent_file=f)
    gone.rmdir()  # 模拟项目被删
    out = list_workspaces(recent_file=f)
    assert [Path(w["path"]).name for w in out] == ["alive"]


def test_list_tolerates_bad_json(tmp_path: Path) -> None:
    """清单文件损坏(坏 JSON / 非列表)一律回退空清单,不抛异常。"""
    f = tmp_path / "recent.json"
    f.write_text("{broken", encoding="utf-8")
    assert list_workspaces(recent_file=f) == []
    f.write_text(json.dumps({"path": "x"}), encoding="utf-8")  # 顶层非列表
    assert list_workspaces(recent_file=f) == []


def test_register_silent_on_write_failure(tmp_path: Path) -> None:
    """清单路径不可写(此处给一个目录当文件用)→ 静默不抛,不影响启动。"""
    register_workspace(tmp_path / "proj", recent_file=tmp_path)  # tmp_path 是目录,write_text 必失败
    # 能走到这里即未抛异常;清单也未产生
    assert not (tmp_path / "recent.json").exists()
