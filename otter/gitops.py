"""git 集成(M1):文件修改自动 commit + /undo 回滚。

设计取向(说明书 6 章"可回滚是安全网的地基",aider 的"git 为真相源"思路):
- 仅在 git 仓库内生效,仓库外静默跳过(不替用户 git init);
- 自动提交带 [otter] 前缀,与人的提交区分;
- /undo 只回滚最近的 [otter] 提交(HEAD 不是 [otter] 提交则拒绝——不碰人的提交)。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

_MARKER = "[otter]"


async def _git(cwd: Path, *args: str) -> tuple[int, str]:
    # 修正(2026-09-22):改用 exec 形式——shell 拼接会让含空格/中文的 commit 消息被拆参,提交静默失败
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


def in_repo(cwd: Path | None = None) -> bool:
    return (cwd or Path.cwd()).joinpath(".git").exists()


async def auto_commit(paths: list[str], note: str, cwd: Path | None = None) -> str | None:
    """写入类工具成功后调用:git add 涉事文件并提交。返回提交摘要或 None(未提交)。"""
    cwd = cwd or Path.cwd()
    if not in_repo(cwd) or not paths:
        return None
    real = [str(cwd / p) for p in paths if (cwd / p).exists()]
    if not real:
        return None
    code, _ = await _git(cwd, "add", "--", *real)
    if code != 0:
        return None
    code, out = await _git(cwd, "commit", "-m", f"{_MARKER} {note}({', '.join(paths)})")
    if code != 0 or "nothing to commit" in out:  # 无变更(内容相同)则跳过
        return None
    short = out.splitlines()[-1] if out else "committed"
    return f"git 已自动提交:{short}"


async def head_is_otter(cwd: Path | None = None) -> bool:
    cwd = cwd or Path.cwd()
    if not in_repo(cwd):
        return False
    code, subject = await _git(cwd, "log", "-1", "--format=%s")
    return code == 0 and subject.startswith(_MARKER)


async def undo_last(cwd: Path | None = None) -> str:
    """/undo:回滚最近一次 [otter] 提交(reset --hard 到上一提交,文件级安全)。"""
    cwd = cwd or Path.cwd()
    if not in_repo(cwd):
        return "[otter] 当前目录不是 git 仓库,无可回滚"
    if not await head_is_otter(cwd):
        code, subject = await _git(cwd, "log", "-1", "--format=%s")
        return f"[otter] 拒绝回滚:最近提交不是 otter 自动提交(是:{subject or '未知'})"
    code, out = await _git(cwd, "reset", "--hard", "HEAD~1")
    if code != 0:
        return f"[otter] 回滚失败:{out}"
    return f"[otter] 已回滚上一组 otter 修改({out.splitlines()[-1] if out else 'HEAD~1'})"
