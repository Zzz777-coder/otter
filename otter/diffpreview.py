"""diff 预览(M3.5,说明书 3.2/8 章:写操作落盘前先给人看变更)。

设计:unified diff 由标准库 difflib 生成(零依赖);预览不影响写盘路径本身,
由调用方(loop)决定"展示→(GUI 等采纳)→写"还是"展示+直接写"(CLI 信任
git+/undo 兜底,自动写但打印 diff;GUI 弹卡片需点采纳)。
"""

from __future__ import annotations

import difflib
from pathlib import Path


def build_diff(path: str, old_text: str | None, new_text: str) -> str:
    """生成带表头的 unified diff;新文件用 /dev/null 作旧侧。"""
    from_lines = (old_text or "").splitlines(keepends=True)
    to_lines = (new_text or "").splitlines(keepends=True)
    diff = difflib.unified_diff(
        from_lines, to_lines,
        fromfile=f"a/{path}" if old_text is not None else "/dev/null",
        tofile=f"b/{path}",
    )
    return "".join(diff)


def preview_write(path: str, content: str) -> str:
    """write_file 的预览:整文件覆盖,展示旧→新全文 diff。"""
    p = Path(path)
    old = p.read_text(encoding="utf-8") if p.is_file() else None
    return build_diff(path, old, content)


def preview_edit(path: str, old_str: str, new_str: str) -> str:
    """edit_file 的预览:只展示被替换片段的上下文 diff(整文件 diff 太长)。"""
    p = Path(path)
    old_text = p.read_text(encoding="utf-8") if p.is_file() else ""
    # 定位唯一匹配的片段及其上下文(与 EditFileTool 的唯一性判定一致)
    idx = old_text.find(old_str)
    if idx < 0:
        return f"(无法预览:old_str 未在 {path} 中找到)"
    start = max(0, old_text.rfind("\n", 0, max(0, idx - 200)) + 1)
    end_nl = old_text.find("\n", idx + len(old_str) + 200)
    end = len(old_text) if end_nl < 0 else end_nl
    snippet_old = old_text[start:end]
    snippet_new = snippet_old.replace(old_str, new_str, 1)
    return build_diff(path, snippet_old, snippet_new)
