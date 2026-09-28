"""v0.5 离线测试(2026-09-24):MCP 域模型/状态机/失败回滚 + Extensions 两阶段导入。

运行:.venv/bin/python -m pytest tests/test_v05.py -v
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest

from otter.extensions import (
    ExtensionImportError,
    apply_import_plan,
    parse_import_plan,
)
from otter.mcp_client import (
    MCPConfigurationStore,
    MCPClientManager,
    MCPServerConfig,
    mcp_tool_name,
)
from otter.tools.base import ToolRegistry
from otter.store import Store


def _run(coro):
    return asyncio.run(coro)


# ── MCP:域模型与配置存储 ───────────────────────────────────────────

def test_mcp_config_domain_and_store(tmp_path: Path):
    with pytest.raises(Exception):  # name 约束
        MCPServerConfig(name="bad-name!", command="x")
    store = MCPConfigurationStore(tmp_path / "mcp.json")
    store.add(MCPServerConfig(name="weather", command="uvx", args=["mcp-weather"]))
    store.add(MCPServerConfig(name="fs", command="npx"))
    with pytest.raises(ValueError):  # 重名拒
        store.add(MCPServerConfig(name="weather", command="y"))
    cfg = store.load()
    assert [s.name for s in cfg.servers] == ["weather", "fs"]
    store.set_enabled("fs", enabled=False)
    assert store.load().servers[1].enabled is False
    store.delete("fs")
    assert [s.name for s in store.load().servers] == ["weather"]
    # 旧 dict 形态兼容(M4 配置无缝)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"servers": {"old": {"command": "x"}}}), encoding="utf-8")
    assert [s.name for s in MCPConfigurationStore(legacy).load().servers] == ["old"]


def test_mcp_tool_namespace():
    assert mcp_tool_name("fs", "read-file/x") == "mcp__fs__read_file_x"
    with pytest.raises(ValueError):
        mcp_tool_name("fs", "///")


def test_mcp_manager_failed_server_rolls_back(tmp_path: Path):
    """坏 server(command 不存在)→ failed 状态 + 零工具注册,进程不炸(隔离)。"""

    class Probe:
        pass

    registry = ToolRegistry()
    from otter.tools.builtin import BashTool

    registry.register(BashTool())  # 垫一个本地工具,验证不被误删
    manager = MCPClientManager([MCPServerConfig(name="ghost", command="/nonexistent/xyz",
                                                startup_timeout_seconds=2)])
    report = _run(manager.start(registry))
    (status,) = manager.statuses()
    assert status.state == "failed" and status.error
    assert "失败" in report[0]
    assert registry.get("mcp__ghost__anything") is None   # 零 MCP 工具残留
    assert registry.get("bash") is not None               # 本地工具不受牵连


# ── Extensions:预览阶段(纯解析) ───────────────────────────────────

def test_parse_mcp_json_both_shapes():
    # 社区形态 mcpServers + 名称规范化 + env 引用警告
    plan = parse_import_plan(json.dumps({
        "mcpServers": {"my-fs": {"command": "npx", "args": ["-y", "fs-mcp"],
                                 "env": {"KEY": "secret-value"}}}}))
    assert len(plan.mcp_servers) == 1
    assert plan.mcp_servers[0].name == "my_fs"
    assert any("环境变量" in w for w in plan.warnings)
    # otter 形态 servers 列表
    plan2 = parse_import_plan(json.dumps({
        "servers": [{"name": "ok_name", "command": "uvx", "args": ["a"]}]}))
    assert plan2.mcp_servers[0].name == "ok_name"
    # npx skills add <repo> 命令形态 → GitHub 技能源
    plan3 = parse_import_plan("npx skills add anthropics/skills")
    assert plan3.skill_sources and plan3.skill_sources[0].slug == "anthropics/skills"


def test_parse_rejects_garbage():
    with pytest.raises(ExtensionImportError):
        parse_import_plan("")
    with pytest.raises(ExtensionImportError):
        parse_import_plan("not a github repo !!")
    with pytest.raises(ExtensionImportError):
        parse_import_plan(json.dumps({"foo": 1}))


def test_parse_local_source(tmp_path: Path):
    d = tmp_path / "mypack"
    d.mkdir()
    plan = parse_import_plan(str(d))
    assert plan.skill_sources[0].kind == "local"


# ── Extensions:确认阶段(本地源全链) ──────────────────────────────

def _make_skill_dir(root: Path, name="demo-skill", with_resources=True):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 演示技能\n---\n\n# {name}\n正文",
        encoding="utf-8")
    if with_resources:
        (d / "scripts" / "run.sh").parent.mkdir(parents=True)
        (d / "scripts" / "run.sh").write_text("#!/bin/sh\necho ok", encoding="utf-8")
        (d / "evil.txt").write_text("不在白名单的资源", encoding="utf-8")
    return d


def test_apply_local_source_installs_whitelist_only(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src_root = tmp_path / "src"
    _make_skill_dir(src_root)
    plan = parse_import_plan(str(src_root / "demo-skill"))
    result = _run(apply_import_plan(plan, skills_root=tmp_path / ".otter/skills"))
    assert result["skills"] and result["skills"][0]["name"] == "demo-skill"
    installed = tmp_path / ".otter" / "skills" / "demo-skill"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "scripts" / "run.sh").is_file()
    assert not (installed / "evil.txt").exists()  # 白名单外资源不装
    # 重复导入:重名拒
    with pytest.raises(ExtensionImportError):
        _run(apply_import_plan(parse_import_plan(str(src_root / "demo-skill")),
                               skills_root=tmp_path / ".otter/skills"))


def test_apply_writes_mcp_config(tmp_path: Path):
    store = MCPConfigurationStore(tmp_path / "mcp.json")
    plan = parse_import_plan(json.dumps(
        {"mcpServers": {"probe": {"command": "uvx", "args": ["probe-mcp"]}}}))
    result = _run(apply_import_plan(plan, skills_root=tmp_path / "sk",
                                    mcp_store=store))
    assert result["mcp_servers"] == ["probe"] and result["restart_required"]
    assert [s.name for s in store.load().servers] == ["probe"]


def test_zip_security_path_traversal_rejected(tmp_path: Path):
    """越界路径的 zip 直接拒(不写盘任何内容)。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ok/SKILL.md", "---\nname: ok\ndescription: d\n---\n# ok")
        z.writestr("../escape.txt", "越界")
    from otter.extensions import _skill_packages_from_zip

    with pytest.raises(ExtensionImportError):
        _skill_packages_from_zip(buf.getvalue())


def test_registry_unregister():
    from otter.tools.builtin import ReadFileTool

    reg = ToolRegistry()
    reg.register(ReadFileTool())
    reg.unregister("read_file")
    assert reg.get("read_file") is None
    reg.unregister("read_file")  # 幂等
