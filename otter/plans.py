"""Plan Mode v2(产品经理模式,2026-09-23;设计参考 vesta PLAN 的产物契约思想,全部重写)。

闭环:调查(只读)→ 结构化计划(固定小节)→ 落盘 .otter/plans/ → 人审 →
采纳后以 <approved_plan> 注入 system 重跑(执行模式)。
otter 不引入 Task 实体(vesta 的 PENDING Task),计划即 markdown 产物——轻量化取舍。
"""

from __future__ import annotations

import time
from pathlib import Path

# 2026-09-23 提示词套件重构:PLAN_DIRECTIVE 移至 otter/prompts.py(单一来源),
# 此处 re-export 保持 loop.py 的 `from otter.plans import PLAN_DIRECTIVE` 兼容
from otter.prompts import PLAN_DIRECTIVE  # noqa: F401

PLAN_SECTION_REQUIRED = "## 步骤"


def plan_is_valid(final_text: str) -> bool:
    """轻校验(对齐 vesta 的 exit validation 思想,但不改写终稿):
    必须含"## 步骤"小节才算一份合格计划。"""
    return PLAN_SECTION_REQUIRED in (final_text or "")


def _plans_root() -> Path:
    root = Path.cwd() / ".otter" / "plans"
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_plan(task: str, plan_text: str) -> Path:
    """计划落盘(学 Claude Code 的 plan file):plan-<N>.md,front matter 记任务原文。"""
    existing = sorted(_plans_root().glob("plan-*.md"))
    n = max((int(p.stem.split("-")[1]) for p in existing), default=0) + 1
    path = _plans_root() / f"plan-{n:03d}.md"
    path.write_text(
        f"---\ntask: {task[:200]}\ncreated: {time.strftime('%Y-%m-%d %H:%M')}\n---\n\n"
        f"{plan_text}\n",
        encoding="utf-8",
    )
    return path


def list_plans(limit: int = 10) -> list[dict]:
    out = []
    for p in sorted(_plans_root().glob("plan-*.md"), reverse=True)[:limit]:
        first = ""
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("task:"):
                first = line[5:].strip()
                break
        out.append({"file": p.name, "task": first, "created": p.stat().st_mtime})
    return out


def load_plan(name: str) -> str | None:
    """按文件名(plan-001.md)或序号读计划正文(去 front matter)。"""
    p = _plans_root() / (name if name.startswith("plan-") else f"plan-{name}.md")
    if not p.is_file():
        p = _plans_root() / (name + ".md" if not name.endswith(".md") else name)
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        return parts[2].strip() if len(parts) == 3 else text
    return text
