"""会话页工作区切换(switch_workspace)的离线测试(2026-09-24 新增,用户要求:
像历史页一样在会话列表正上方放下拉,切过去=在那个工作区干活)。

覆盖:真切换后 Store/根路径/会话级状态全部指向新区并清空串扰、目录不存在拒绝、
运行中任务拒绝(busy)。不碰真实 ~/.otter 清单(monkeypatch 拦截登记与读取)。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from otter import workspaces
from otter.config import Config


@pytest.fixture()
def gui(monkeypatch, tmp_path):
    """真实 OtterWebGui(起后台 loop 线程,不开窗);最近清单读写全部拦截到临时文件,
    避免测试写脏全局 ~/.otter/recent_workspaces.json。"""
    fake = tmp_path / "recent.json"
    # 修正(2026-09-24):lambda 里必须引用"打补丁前"的原函数——直接写
    # workspaces.register_workspace 会拿到补丁自身,无限自递归
    _real_reg = workspaces.register_workspace
    _real_list = workspaces.list_workspaces
    monkeypatch.setattr(workspaces, "register_workspace",
                        lambda path, recent_file=None: _real_reg(path, recent_file=fake))
    monkeypatch.setattr(workspaces, "list_workspaces",
                        lambda limit=10, recent_file=None: _real_list(limit, recent_file=fake))
    from otter.gui import OtterWebGui

    g = OtterWebGui(Config.load())
    yield g
    g.loop.call_soon_threadsafe(g.loop.stop)  # 收尾:停掉后台事件循环线程


def test_switch_reopens_store_and_clears_state(gui, tmp_path):
    """真切换:chdir + Store 重开指向新区;会话级状态(当前会话/写盘放行记忆/记忆包)清空。"""
    area_b = tmp_path / "wsB"
    area_b.mkdir()
    # 预置"切换前的串扰状态":当前会话/放行目录/记忆包,切换后必须全部清掉
    gui.current_cid = 7
    gui.gate_session.ok_dirs = {"/somewhere/old"}
    gui.memory_bundle = object()
    api = gui._api()

    origin = Path.cwd()
    try:
        r = api.switch_workspace(str(area_b))
        assert r["ok"] is True
        assert gui.workspace_root == area_b.resolve()
        assert gui.db_path == area_b.resolve() / ".otter" / "otter.db"
        assert gui.view_root == gui.workspace_root        # 查看根跟随
        assert gui.store is not None and gui.store.db_path == gui.db_path  # Store 已重开
        assert gui.current_cid is None                    # 串扰状态清空
        assert gui.gate_session.ok_dirs == set()
        assert gui.memory_bundle is None
        assert Path.cwd() == area_b.resolve()             # 真切换(chdir)
        # 新区进最近清单(switch 内部登记,经 fixture 拦截写入临时文件)
        names = [Path(w["path"]).name
                 for w in workspaces.list_workspaces(recent_file=tmp_path / "recent.json")]
        assert "wsB" in names
    finally:
        os.chdir(origin)  # 测试不污染进程 cwd(其余用例仍属原目录)


def test_switch_rejects_missing_dir(gui, tmp_path):
    api = gui._api()
    r = api.switch_workspace(str(tmp_path / "nope"))
    assert r["ok"] is False and "不存在" in r["reason"]


def test_switch_rejects_busy(gui, tmp_path):
    """运行中任务(_current_future 未完成)拒绝切换,不 chdir。"""
    class _Pending:  # 模拟运行中的 future(switch 只读 .done();裸 asyncio.Future
        def done(self): return False  # 在主线程无事件循环时行为不稳,故用 stub)

    area_b = tmp_path / "wsB2"
    area_b.mkdir()
    gui._current_future = _Pending()  # 永不完成的运行句柄
    api = gui._api()
    origin = Path.cwd()
    try:
        r = api.switch_workspace(str(area_b))
        assert r["ok"] is False and "运行中" in r["reason"]
        assert Path.cwd() == origin  # 未切换
    finally:
        gui._current_future = None
        os.chdir(origin)


def test_get_workspaces_current_follows_switch(gui, monkeypatch, tmp_path):
    """切换后 get_workspaces 的 current 标记跟到新根(会话页下拉选中态)。"""
    area_b = tmp_path / "wsC"
    area_b.mkdir()
    api = gui._api()
    origin = Path.cwd()
    try:
        assert api.switch_workspace(str(area_b))["ok"] is True
        items = api.get_workspaces()
        cur = [it for it in items if it["current"]]
        assert len(cur) == 1 and Path(cur[0]["path"]).name == "wsC"
    finally:
        os.chdir(origin)


def test_pick_workspace_cancel(gui):
    """新建/打开工作区:目录选择框点取消 → 不切换不动现状(2026-09-24)。
    修正:pywebview 6.x 对话框是窗口实例方法,测试给 gui.window 塞桩。"""

    class _FakeWin:  # 桩窗口:只提供 create_file_dialog
        def create_file_dialog(self, *a, **k):
            return None  # 模拟用户取消

    gui.window = _FakeWin()
    try:
        r = gui._api().pick_workspace()
        assert r["ok"] is False and "取消" in r["reason"]
    finally:
        gui.window = None


def test_pick_workspace_selects_dir(gui, tmp_path):
    """新建/打开工作区:框里选定目录 → 走真切换(与 switch_workspace 同链路)。"""
    area_b = tmp_path / "picked"
    area_b.mkdir()

    class _FakeWin:
        def create_file_dialog(self, *a, **k):
            return [str(area_b)]  # 模拟选定目录

    gui.window = _FakeWin()
    api = gui._api()
    origin = Path.cwd()
    try:
        r = api.pick_workspace()
        assert r["ok"] is True
        assert gui.workspace_root == area_b.resolve()
        assert gui.store is not None and gui.store.db_path == gui.db_path
    finally:
        gui.window = None
        os.chdir(origin)
