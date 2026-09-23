"""M4 离线测试:Artifact 发布/三通道/Skill watermark+人工确认/子代理只读边界/MCP 配置容错。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from otter.artifact import ArtifactPublishTool, list_artifacts, open_artifact
from otter.mcp_client import load_mcp_config
from otter.skills import accept_candidate, list_skills, maybe_distill, load_watermark
from otter.models.types import Message, ModelResponse, ModelUsage
from otter.tools.builtin import MakePdfTool


# ── Artifact ──────────────────────────────────────────────────────

def _real_pdf(path: Path, text: str = "# 测试报告\n用于夹具的真 PDF。") -> None:
    """2026-09-24 R6 后续教训:测试夹具不得产出打不开的假 PDF——pytest 临时目录
    (/var/folders)能被 Finder/Spotlight 搜到,用户双击到"已损坏"文件(report.pdf
    事故的真正来源)。凡 .pdf 夹具一律用 make_pdf 生成真文件。"""
    r = asyncio.run(MakePdfTool().run({"path": str(path), "content": text}))
    assert "已生成真 PDF" in r


def test_artifact_publish_and_list(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _real_pdf(tmp_path / "report.pdf")  # 真夹具(旧版 b"%PDF-fake-1" 是残留地雷的来源)

    class FakeAdapter:
        async def complete_stream(self, *a, **k): ...

    tool = ArtifactPublishTool()
    r = asyncio.run(tool.run({"path": "report.pdf", "note": "周报"}))
    assert "已发布" in r and "sha" in r
    arts = list_artifacts()
    assert len(arts) == 1 and arts[0]["name"] == "report.pdf" and arts[0]["note"] == "周报"
    assert arts[0]["size"] > 1000  # 真 PDF(嵌入字体有体量;旧假夹具是 11 字节)
    # 不存在的文件 → 错误文本
    r = asyncio.run(tool.run({"path": "nope.pdf"}))
    assert "不存在" in r


def test_artifact_publish_rejects_fake_binary(tmp_path: Path, monkeypatch):
    """R6(2026-09-24):发布层伪二进制拦截——bash echo 伪造的 .pdf(write_file 层拦不到)
    在发布前按魔数再拦,杜绝"发布了但用户打不开"的交付物(report.pdf 事故)。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fake.pdf").write_text("这不是真的 PDF,只是文本", encoding="utf-8")
    tool = ArtifactPublishTool()
    r = asyncio.run(tool.run({"path": "fake.pdf", "note": "周报"}))
    assert "伪造" in r and "make_pdf" in r
    arts = list_artifacts()
    assert len(arts) == 0  # 未入索引,界面不会出现打不开的产物
    # R6 教训:假 PDF 夹具用后即删,不在临时目录留"双击已损坏"的地雷
    (tmp_path / "fake.pdf").unlink()


def test_artifact_open_channels(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _real_pdf(tmp_path / "report.pdf")  # R6 教训:真夹具(旧 b"%PDF-fake" 是残留地雷来源)
    tool = ArtifactPublishTool()
    asyncio.run(tool.run({"path": "report.pdf"}))
    assert "已打开" in open_artifact("report.pdf", "open")       # OS 默认(open 命令)
    assert "已打开" in open_artifact("report.pdf", "finder")     # Finder 定位
    assert "不存在" in open_artifact("ghost", "open")            # 未知产物


# ── Skill Learning ────────────────────────────────────────────────

def test_skill_watermark_distill_and_accept(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class FakeAdapter:
        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            return ModelResponse(content=json.dumps({
                "action": "create", "name": "pytest-first",
                "description": "改代码后先跑 pytest",
                "procedure": "1. 改动 2. python -m pytest 3. 过了再汇报",
            }), usage=ModelUsage(10, 5))

    note = asyncio.run(maybe_distill(FakeAdapter(), 1, "修复登录 bug", "已修复并通过测试"))
    assert "pytest-first" in note and "确认" in note
    # watermark:pending→processed;同 run 再问不再重复提炼
    wm = load_watermark()
    assert wm["processed"] == ["1"] and not wm["pending"]
    note2 = asyncio.run(maybe_distill(FakeAdapter(), 1, "同任务", "同结果"))
    assert note2 == ""  # 已处理,跳过
    # 候选落盘且不自动生效
    assert "候选(待确认)" in list_skills() and "已转正" not in list_skills()
    # 人工确认转正
    msg = accept_candidate(1)
    assert "已转正" in msg and (tmp_path / ".otter/skills/pytest-first/SKILL.md").is_file()
    assert "已转正" in list_skills() and "候选" not in list_skills()
    # 重复 accept 拒绝
    assert "无 run1" in accept_candidate(1)


def test_skill_none_and_failure_paths(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class NoneAdapter:
        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            return ModelResponse(content='{"action":"none"}', usage=ModelUsage(1, 1))

    class BadJsonAdapter:
        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            return ModelResponse(content="not json", usage=ModelUsage(1, 1))

    # none → 不产候选,run 记 processed
    assert asyncio.run(maybe_distill(NoneAdapter(), 5, "闲聊", "ok")) == ""
    wm = load_watermark()
    assert "5" in wm["processed"]
    # 坏 JSON → 失败释放占位(pending 清空,下次可重试)
    assert asyncio.run(maybe_distill(BadJsonAdapter(), 6, "任务", "结果")) == ""
    wm = load_watermark()
    assert "6" not in wm["pending"] and "6" not in wm["processed"]


# ── 子代理只读边界 ────────────────────────────────────────────────

def test_subagent_tool_plan_mode_readonly(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from otter.loop import AgentLoop, MODE_PLAN
    from otter.subagent import SubagentTool
    from otter.tools.base import ToolRegistry
    from otter.tools.builtin import BashTool, ReadFileTool, WriteFileTool

    seen_modes = []

    class FakeAdapter:
        model = "deepseek-v4-flash"
        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            names = sorted(t.name for t in (tools or []))
            seen_modes.append(names)
            return ModelResponse(content="结论:找到了 X", usage=ModelUsage(100, 20))

    class MemStore:
        async def new_run(self): return 9
        async def finish_run(self, *a): pass
        async def append_message(self, *a): pass
        async def save_checkpoint(self, *a, **k): pass
        async def append_event(self, *a): pass

    reg = ToolRegistry()
    reg.register(ReadFileTool()); reg.register(WriteFileTool()); reg.register(BashTool())
    tool = SubagentTool(FakeAdapter(), reg, MemStore())
    r = asyncio.run(tool.run({"task": "找到所有 X"}))
    assert "找到了 X" in r and "子代理结论" in r
    # 子代理运行在 PLAN 模式:请求工具表只含只读(write/bash 不在其中)
    for names in seen_modes:
        assert "write_file" not in names and "bash" not in names


# ── MCP 配置容错 ──────────────────────────────────────────────────

def test_mcp_config_tolerates_missing_and_corrupt(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("otter.mcp_client.CONFIG_PATH", tmp_path / "mcp.json")
    assert load_mcp_config() == {}  # 无配置=空
    (tmp_path / "mcp.json").write_text("{broken", encoding="utf-8")
    assert load_mcp_config() == {}  # 损坏=空,不抛
    (tmp_path / "mcp.json").write_text(json.dumps(
        {"servers": {"demo": {"command": "echo", "args": ["hi"]}}}), encoding="utf-8")
    cfg = load_mcp_config()
    assert cfg["demo"]["command"] == "echo"
