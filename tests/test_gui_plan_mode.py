"""GUI 常驻 Plan/Act 开关防呆测试(2026-09-23):source-inspection 防接线断裂。

背景:GUI 此前无模式概念(_run_task 永远 MODE_NORMAL)。本测试锁住三处接线:
①会话级 mode 状态 ②toggle_mode JsApi ③loop.run 传 mode——任何一处被
refactor 断掉,GUI 的 PLAN 只读保证即静默失效(跑的还是可写模式)。
"""

from __future__ import annotations

import inspect

from otter import gui as gui_mod
from otter.gui import OtterWebGui
from otter.loop import MODE_NORMAL, MODE_PLAN


def test_gui_has_session_mode_state():
    """① OtterWebGui 必须有会话级 mode 属性,默认执行模式。"""
    src = inspect.getsource(OtterWebGui.__init__)
    assert "self.mode = MODE_NORMAL" in src


def test_gui_toggle_mode_api_exists():
    """② JsApi 必须暴露 toggle_mode(前端 #modeBtn 的唯一入口)。"""
    api_src = inspect.getsource(OtterWebGui._api)
    assert "def toggle_mode" in api_src
    # 翻转逻辑必须双向覆盖(plan→normal / normal→plan)
    assert "MODE_PLAN" in api_src and "MODE_NORMAL" in api_src


def test_gui_run_task_threads_mode():
    """③ _run_task 必须把会话级 mode 传给 loop.run——断掉即 PLAN 开关静默失效。"""
    src = inspect.getsource(OtterWebGui._run_task)
    assert "mode=self.mode" in src


def test_mode_constants_unchanged():
    """模式常量契约:值不可变(loop/事件/前端三方字符串比对依赖)。"""
    assert MODE_NORMAL == "normal" and MODE_PLAN == "plan"
    # gui 模块应引用 loop 的常量(单一来源)
    assert gui_mod.MODE_PLAN is MODE_PLAN


def test_diff_gate_session_dir_memory():
    """R7(2026-09-24 用户要求:不逐次弹窗):同目录(同层级)允许一次,本会话放行;
    bash 写文件会话级一次允许;不同目录仍要问;拒绝不记忆。"""
    from otter.gui import DiffGateSession

    g = DiffGateSession()
    g.record("edit_file", "playground/src/a.py")
    assert not g.allowed()                    # 首次必须弹窗询问
    g.mark_allowed()
    g.record("write_file", "playground/src/b.py")
    assert g.allowed()                        # 同目录(src)后续直接放行
    g.record("write_file", "playground/docs/b.md")
    assert not g.allowed()                    # 不同目录(docs)须再问
    g.mark_allowed()
    g.record("bash", "rm -rf x")
    assert not g.allowed()                    # bash 首次要问(会话级)
    g.mark_allowed()
    g.record("bash", "touch y")
    assert g.allowed()                        # bash 本会话后续放行
