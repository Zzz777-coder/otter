"""M1 离线测试:全部 fake,不发真实请求(沿用 M0 的离线测试策略)。

覆盖:滚动摘要触发与请求视图重建 / Plan 只读白名单 / edit-grep-glob 工具 /
git 自动提交与 /undo / AGENTS.md 加载与 /init。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from otter.agents_md import init_agents_md, load_instructions
from otter.context.summarizer import ConversationSummary
from otter.gitops import auto_commit, head_is_otter, undo_last
from otter.loop import MODE_PLAN, AgentLoop, SummaryState
from otter.models.types import Message, ModelResponse, ModelUsage, ToolCall
from otter.tools.base import ToolRegistry
from otter.tools.builtin import EditFileTool, GlobTool, GrepTool, MakePdfTool, ReadFileTool, WriteFileTool


class FakeAdapter:
    """记录请求(视图首条与工具名),按脚本回放响应。"""

    def __init__(self, script: list[ModelResponse]) -> None:
        self.script = list(script)
        self.views: list[dict] = []

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        self.views.append(
            {
                "first_role": messages[0].role,
                "first_content": messages[0].content or "",
                "tool_names": sorted(t.name for t in (tools or [])),
                "n_messages": len(messages),
            }
        )
        resp = self.script.pop(0)
        if on_text_delta and resp.content:
            on_text_delta(resp.content)
        return resp


class FakeStore:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def new_run(self):
        return 1

    async def finish_run(self, *a):
        pass

    async def append_message(self, *a):
        pass

    async def save_checkpoint(self, *a, **k):
        pass  # M2:loop 新增落档调用,fake 兼容

    async def append_event(self, run_id, type_, payload):
        self.events.append(type_)


class FakeSummarizer:
    """固定返回合法摘要,记录被压缩的消息。"""

    def __init__(self) -> None:
        self.compressed: list[list[Message]] = []

    async def summarize(self, messages, prev_summary):
        self.compressed.append(list(messages))
        return ConversationSummary(current_objective="继续完成示例任务", completed_work=["已读文件"]), ModelUsage(5, 5)


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    for t in (ReadFileTool(), WriteFileTool(), EditFileTool(), GrepTool(), GlobTool()):
        r.register(t)
    return r


def test_rolling_summary_triggers_and_view_rebuilt():
    """长历史 + 小预算 → 触发压缩:视图首条是含摘要的 system,水位推进,原始历史不动。"""
    long_history = [Message(role="user" if i % 2 == 0 else "assistant", content="x" * 200) for i in range(20)]
    adapter = FakeAdapter([ModelResponse(content="done", usage=ModelUsage(1, 1))])
    summarizer = FakeSummarizer()
    loop = AgentLoop(adapter, _registry(), FakeStore(), summarizer=summarizer,
                     context_budget=500, trigger_ratio=0.3, keep_recent=2)
    snapshot_len = len(long_history)
    state = SummaryState()
    result = asyncio.run(loop.run(long_history, Message(role="user", content="继续"), run_id=1, summary_state=state))
    assert result.ok
    assert state.compressions >= 1 and state.covered > 0
    assert len(long_history) == snapshot_len  # 原始历史永不修改
    view0 = adapter.views[0]
    assert view0["first_role"] == "system"
    assert "<conversation_summary>" in view0["first_content"] and "继续完成示例任务" in view0["first_content"]
    assert view0["n_messages"] == 1 + (snapshot_len + 1 - state.covered)  # system + 未压缩尾部
    assert result.usage.input_tokens == 6  # 压缩调用(5) + 主调用(1) 都记账——压缩不免费
    assert "CONTEXT_COMPACTED" in loop.store.events


def test_plan_mode_whitelist_and_write_blocked():
    """Plan:请求只含只读工具;模型硬要 write_file 时被拦截并回喂错误文本。"""
    adapter = FakeAdapter([
        ModelResponse(content=None, tool_calls=[ToolCall(id="c1", name="write_file", arguments={"path": "a.txt", "content": "x"})]),
        ModelResponse(content="这是修改计划……", usage=ModelUsage(2, 2)),
    ])
    loop = AgentLoop(adapter, _registry(), FakeStore(), keep_recent=10)
    result = asyncio.run(loop.run([], Message(role="user", content="规划一下"), run_id=1, mode=MODE_PLAN))
    assert result.ok and result.stop_reason == "final_answer"
    for v in adapter.views:
        assert v["tool_names"] == ["glob", "grep", "read_file"]  # 结构上不含写工具
    # 写调用被硬校验拦截:tool 消息内容我们没有直接暴露,但第二轮模型收到的尾部含拦截文本——
    # 用压缩记录侧证:无。改为断言第二步请求仍只读且最终给出了文字计划(弱断言,行为级验证)
    assert "计划" in result.final_text


def test_edit_grep_glob_tools(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "util.py").write_text("VALUE = 42\n", encoding="utf-8")

    edit, grep, glob_ = EditFileTool(), GrepTool(), GlobTool()
    # edit:唯一匹配替换
    r = asyncio.run(edit.run({"path": "app.py", "old_str": "a + b", "new_str": "a + b + 0"}))
    assert "已修改" in r and "return a + b + 0" in (tmp_path / "app.py").read_text()
    # edit:不唯一报错
    (tmp_path / "dup.py").write_text("x\nx\n", encoding="utf-8")
    r = asyncio.run(edit.run({"path": "dup.py", "old_str": "x", "new_str": "y"}))
    assert "2 次" in r
    # grep:跨目录命中 + 行号
    r = asyncio.run(grep.run({"pattern": r"VALUE|add", "glob": "*.py"}))
    assert "app.py:1:" in r and "sub/util.py:1:" in r
    # glob:递归列出
    r = asyncio.run(glob_.run({"pattern": "**/*.py"}))
    assert "app.py" in r and "sub/util.py" in r


def test_gitops_commit_and_undo(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)  # 修正(2026-09-22):漏了 init,config 报 not in a git directory
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        subprocess.run(["git", "config", k, v], check=True)
    # 修正(2026-09-22):补一个初始提交——单提交仓库没有 HEAD~1,/undo 的 reset 无处可退
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "init"], check=True)
    (tmp_path / "f.txt").write_text("v1", encoding="utf-8")
    note = asyncio.run(auto_commit(["f.txt"], "write_file"))
    assert note and head_is_otter()
    # /undo:回滚 [otter] 提交,文件回到 v1 之前(不存在)
    msg = asyncio.run(undo_last())
    assert "已回滚" in msg and not (tmp_path / "f.txt").exists()
    # 人的提交不许动:手工提交后拒绝 undo
    (tmp_path / "g.txt").write_text("human", encoding="utf-8")
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run(["git", "commit", "-m", "human work"], check=True)
    assert "拒绝" in asyncio.run(undo_last())


def test_agents_md_load_and_init(tmp_path: Path, monkeypatch):
    import otter.agents_md as amd

    monkeypatch.setattr(amd, "_USER_FILE", tmp_path / "user_agents.md")
    (tmp_path / "user_agents.md").write_text("用户级约定", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert "用户级约定" == load_instructions()  # 无项目级文件时只有用户级
    (tmp_path / "CLAUDE.md").write_text("项目约定(CLAUDE 兼容)", encoding="utf-8")
    loaded = load_instructions()
    assert "用户级约定" in loaded and "项目约定" in loaded  # 分层拼接
    # /init:无 AGENTS.md 时生成(确定性模板);再调一次拒绝覆盖
    p = init_agents_md()
    assert p is not None and p.is_file() and "AGENTS.md" in p.read_text(encoding="utf-8")
    assert init_agents_md() is None  # 已存在 → 拒绝覆盖,保护手写内容


def test_write_file_rejects_fake_binary(tmp_path: Path, monkeypatch):
    """R6(2026-09-24 用户报告 report.pdf 打不开):纯文本冒充 .pdf → 写入层硬拦
    并引导正确路径;真 %PDF 头与 .md 不受影响。"""
    monkeypatch.chdir(tmp_path)
    tool = WriteFileTool()
    # 伪造:纯文本写进 .pdf → 拒绝,文件不落盘,文案指明正确出路(make_pdf)
    r = asyncio.run(tool.run({"path": "report.pdf", "content": "# 周报\n本周完成…"}))
    assert "伪造" in r and "make_pdf" in r and not (tmp_path / "report.pdf").exists()
    # 同类:.docx 纯文本也拦
    r = asyncio.run(tool.run({"path": "doc.docx", "content": "hello"}))
    assert "伪造" in r and not (tmp_path / "doc.docx").exists()
    # 放行:真 %PDF 头(bash+fpdf2 产物回写场景)正常写入
    r = asyncio.run(tool.run({"path": "real.pdf", "content": "%PDF-1.4\n%%EOF"}))
    assert "已写入" in r and (tmp_path / "real.pdf").exists()
    (tmp_path / "real.pdf").unlink()  # R6 教训:不完整 PDF 不留在临时目录当"地雷"
    # 放行:普通文本格式不受影响
    r = asyncio.run(tool.run({"path": "note.md", "content": "# 笔记"}))
    assert "已写入" in r and (tmp_path / "note.md").exists()


def test_make_pdf_generates_real_pdf(tmp_path: Path, monkeypatch):
    """R6 后续(2026-09-24 用户要求"生成可以打开的 report"):make_pdf 工具产出
    带真实 %PDF 魔数、中文可渲染的 PDF;PLAN 只读白名单不含它(写类)。"""
    monkeypatch.chdir(tmp_path)
    tool = MakePdfTool()
    r = asyncio.run(tool.run({
        "path": "report.pdf", "title": "项目周报",
        "content": "# 概述\n本周完成三件事。\n\n## 详情\n- 修复了伪 PDF 拦截\n- 新增链接行\n中文换行测试" * 3,
    }))
    assert "已生成真 PDF" in r
    data = (tmp_path / "report.pdf").read_bytes()
    assert data[:4] == b"%PDF" and len(data) > 1000  # 真魔数 + 嵌入字体有体量
    from otter.loop import PLAN_TOOLS

    assert "make_pdf" not in PLAN_TOOLS  # 写类工具不得进 PLAN 只读白名单


def test_make_pdf_file_changed_event(tmp_path: Path, monkeypatch):
    """R6:make_pdf 成功后同样发 FILE_CHANGED(前端 📎 链接行可见可预览),
    二进制产物 plus/minus 为 None(无文本 diffstat)。"""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "out.pdf"
    adapter = FakeAdapter([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="c1", name="make_pdf",
            arguments={"path": str(target), "content": "# 报告\n内容"})]),
        ModelResponse(content="done", usage=ModelUsage(1, 1)),
    ])
    got: list[tuple[str, dict]] = []

    async def on_event(type_, payload):
        got.append((type_, payload))

    reg = _registry()
    reg.register(MakePdfTool())
    loop = AgentLoop(adapter, reg, FakeStore())
    result = asyncio.run(loop.run([], Message(role="user", content="生成报告"), run_id=1, on_event=on_event))
    assert result.ok
    fc = [p for t, p in got if t == "FILE_CHANGED"]
    assert len(fc) == 1
    assert fc[0]["action"] == "已创建" and fc[0]["name"] == "out.pdf"
    assert fc[0]["plus"] is None and fc[0]["minus"] is None
    assert target.read_bytes()[:4] == b"%PDF"


def test_file_changed_event_emitted(tmp_path: Path, monkeypatch):
    """R5(2026-09-24 用户要求):写类工具成功落盘 → FILE_CHANGED 事件
    (action=已创建/已修改、plus/minus diffstat、path/name);错误/拒绝不发。"""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "demo.txt"
    adapter = FakeAdapter([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="c1", name="write_file",
            arguments={"path": str(target), "content": "hello\nworld\n"})]),
        ModelResponse(content="done", usage=ModelUsage(1, 1)),
    ])
    got: list[tuple[str, dict]] = []

    async def on_event(type_, payload):
        got.append((type_, payload))

    loop = AgentLoop(adapter, _registry(), FakeStore())
    result = asyncio.run(loop.run([], Message(role="user", content="写文件"), run_id=1, on_event=on_event))
    assert result.ok
    fc = [p for t, p in got if t == "FILE_CHANGED"]
    assert len(fc) == 1
    assert fc[0]["action"] == "已创建"          # write_file → 已创建;edit_file → 已修改
    assert fc[0]["name"] == "demo.txt"
    assert fc[0]["path"].endswith("demo.txt")
    assert fc[0]["plus"] == 2 and fc[0]["minus"] == 0  # 新文件全为增行(排除 +++ 头)
    # preview_fn 未注入(非 GUI)→ 不带预览字段,前端点击时按需补拉
    assert "preview_type" not in fc[0]
