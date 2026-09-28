"""v0.3 装配集成冒烟(2026-09-24):task 工具注册/ctx 挂载/Plan 白名单/Closing 收窄。"""

from __future__ import annotations

from otter.loop import CLOSING_TOOLS, PLAN_TOOLS, AgentLoop, MODE_PLAN
from otter.store import Store
from otter.tools.base import ToolRegistry


def _registry():
    # 直接调 full_registry(与 CLI/GUI 同路径;Store 仅占位,Evidence 工具持引用不执行)
    from otter.assembly import full_registry

    return full_registry(Store(":memory:"))


def test_task_tools_registered_with_ctx():
    reg = _registry()
    for name in ("task_create", "task_update", "task_get", "task_list"):
        assert reg.get(name) is not None, f"{name} 未注册"
    # ctx/store 挂载在 registry 上(loop 统一写入的契约)
    assert isinstance(getattr(reg, "task_ctx", None), dict)
    assert getattr(reg, "task_store", None) is not None
    # 常驻(非 deferred):默认 schema 可见
    names = [d.name for d in reg.definitions()]
    assert "task_create" in names


def test_plan_whitelist_includes_task_tools():
    for name in ("task_create", "task_update", "task_get", "task_list"):
        assert name in PLAN_TOOLS


def test_closing_narrows_toolface(monkeypatch=None):
    """Closing 段:调查类工具从 schema 消失,交付类保留(结构性限制)。"""
    reg = ToolRegistry()

    class Fake(ToolRegistry):  # 复用 definitions 过滤逻辑,不打依赖
        pass

    # 用真实 registry 更贴近实况
    reg = _registry()
    loop = AgentLoop.__new__(AgentLoop)  # 仅测 _definitions_for,绕开构造依赖
    loop.registry = reg
    loop._activated_tools = set()
    loop._budget_closing = False
    full = [d.name for d in loop._definitions_for("normal")]
    assert "bash" in full and "grep" in full
    loop._budget_closing = True
    narrowed = [d.name for d in loop._definitions_for("normal")]
    assert "bash" not in narrowed and "grep" not in narrowed      # 调查类消失
    assert "write_file" in narrowed and "make_pdf" in narrowed     # 交付类保留
    # 注:artifact_publish 自身为 deferred(经 tool_search 激活后可见),
    # 激活名单共享同一收窄过滤,激活态下同样在 Closing 面内(CLOSING_TOOLS 含它)
    assert "task_update" in narrowed                               # task_* 前缀保留
    assert set(narrowed) <= ({n for n in full if n in CLOSING_TOOLS}
                             | {n for n in full if n.startswith("task_")})
