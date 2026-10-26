"""Skill 运行时注入(2026-09-24)的离线测试:brief 读取/渲染、skill_read 工具、
loop 的 system 注入(_build_view 直测 + run() 全链自动加载)。

全离线:FakeAdapter/FakeStore,技能文件写 tmp_path(monkeypatch.chdir 隔离)。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from otter.loop import AgentLoop  # noqa: E402
from otter.models.types import Message, ModelResponse, ModelUsage  # noqa: E402
from otter.skills import (  # noqa: E402
    SkillReadTool, load_skills_brief, read_skill, render_skills_brief,
)
from otter.tools.base import ToolRegistry  # noqa: E402


def _make_skill(root: Path, name: str, desc: str, body: str = "步骤一…") -> None:
    d = root / ".otter" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8")


def test_load_brief_reads_front_matter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_skill(tmp_path, "fault-check", "急单影响面检查")
    _make_skill(tmp_path, "pdf-gen", "生成可打开的报告 PDF")
    brief = load_skills_brief()
    assert brief == [("fault-check", "急单影响面检查"), ("pdf-gen", "生成可打开的报告 PDF")]


def test_load_brief_missing_dir_is_empty_and_no_mkdir(tmp_path, monkeypatch):
    """纯读铁律:目录不存在 → 空列表,且不创建目录。"""
    monkeypatch.chdir(tmp_path)
    assert load_skills_brief() == []
    assert not (tmp_path / ".otter" / "skills").exists()


def test_render_brief_format_and_empty():
    text = render_skills_brief([("a", "描述")])
    assert text.startswith("<skills>") and text.endswith("</skills>")
    assert "- a:描述" in text and "skill_read" in text  # 引导语指向按需读工具
    assert render_skills_brief([]) == ""          # 无技能不注入空段
    assert render_skills_brief([("", "x")]) == ""  # 无名条目跳过


def test_read_skill_hit_and_miss(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_skill(tmp_path, "pdf-gen", "描述", body="1. 用 make_pdf")
    assert "make_pdf" in read_skill("pdf-gen")
    miss = read_skill("nope")
    assert "不存在" in miss and "pdf-gen" in miss  # 报错同时列可用名(引导)


def test_skill_read_tool():
    tool = SkillReadTool()
    assert tool.name == "skill_read"
    assert "name" in tool.parameters["properties"]


def test_build_view_injects_brief(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_skill(tmp_path, "fault-check", "急单影响面检查")
    loop = AgentLoop(_NoopAdapter(), ToolRegistry(), _MemStore())
    loop._skills_brief = render_skills_brief(load_skills_brief())
    view = loop._build_view([], type("S", (), {"summary": None, "covered": 0})())
    assert "<skills>" in view[0].content and "fault-check" in view[0].content


def test_run_loads_brief_into_view(tmp_path, monkeypatch):
    """全链:run() 自动读技能并注入 system(adapter 收到的第一条消息)。"""
    monkeypatch.chdir(tmp_path)
    _make_skill(tmp_path, "fault-check", "急单影响面检查")

    seen = {}

    class ProbeAdapter:
        model = "fake"

        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            seen["system"] = messages[0].content
            return ModelResponse(content="done", usage=ModelUsage(1, 1))

    store = _MemStore()
    loop = AgentLoop(ProbeAdapter(), ToolRegistry(), store)
    result = asyncio.run(loop.run([], Message(role="user", content="查一下"), 1, 3))
    assert result.ok
    assert "<skills>" in seen["system"] and "fault-check" in seen["system"]


class _NoopAdapter:
    model = "fake"

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        return ModelResponse(content="x", usage=ModelUsage())


class _MemStore:
    async def new_run(self):
        return 1

    async def finish_run(self, *a):
        pass

    async def append_message(self, msg, seq, run_id, conversation_id=None):
        pass

    async def save_checkpoint(self, *a, **k):
        pass

    async def append_event(self, run_id, type_, payload):
        pass


def test_skills_payload_two_zones(tmp_path: Path, monkeypatch):
    """2026-10-26 技能页快照:转正/候选两区、正文全量直带、坏文件跳过不阻断。"""
    monkeypatch.chdir(tmp_path)
    from otter.gui import _skills_payload

    # 转正:标准 front matter + markdown 正文
    sdir = tmp_path / ".otter" / "skills" / "demo-skill"
    sdir.mkdir(parents=True)
    (sdir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 演示技能\n"
        "created_from: manual(测试)\ncreated: 2026-10-26\n---\n\n# demo\n\n1. 第一步\n",
        encoding="utf-8")
    # 坏文件:无 front matter,应跳过不炸
    bad = tmp_path / ".otter" / "skills" / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("没有 front matter 的坏文件", encoding="utf-8")
    # 候选:挖掘产物 json
    cdir = tmp_path / ".otter" / "skills" / "candidates"
    cdir.mkdir()
    (cdir / "run9.json").write_text(
        '{"run_id": 9, "name": "cand-x", "description": "候选描述", '
        '"procedure": "步骤…", "created": "2026-10-26"}', encoding="utf-8")

    payload = _skills_payload(root=tmp_path / ".otter" / "skills")
    assert [s["name"] for s in payload["confirmed"]] == ["demo-skill"]  # 坏文件被跳过
    s = payload["confirmed"][0]
    assert s["description"] == "演示技能" and "第一步" in s["body"]  # 正文全量直带
    assert payload["candidates"][0]["name"] == "cand-x"
    assert payload["candidates"][0]["run_id"] == 9
    # 空目录:不炸、两区空
    empty = _skills_payload(root=tmp_path / "nowhere")
    assert empty == {"confirmed": [], "candidates": []}
