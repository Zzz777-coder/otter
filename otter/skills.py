"""Skill Learning 最小版(M4,说明书 5.4;设计参考 vesta app/skill_learning/ 思想,全部重写)。

流水线(最小版):Run 结束后(可关)→ 用 main 模型从"本次任务+最终答案"提炼
候选 Skill(严格 JSON)→ 落候选文件(pending)→ **人工确认才转正**(绝不自动生效)。
watermark:已处理的 run 记录在 .otter/skills/watermark.json,失败下次重试,成功永不重复。

Skill 形态:目录式 `.otter/skills/<name>/SKILL.md`(front matter: name/description/created_from)。
运行时(最小版):skill_list 工具列出已转正技能;注入留 M4.1(先有积累再谈调度)。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from otter.prompts import DISTILL_PROMPT  # 2026-09-23 提示词套件重构:文案单一来源(内容不变)
from otter.tools.base import Tool


def _skills_root() -> Path:
    root = Path.cwd() / ".otter" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _watermark_path() -> Path:
    return _skills_root() / "watermark.json"


def load_watermark() -> dict:
    p = _watermark_path()
    if not p.is_file():
        return {"processed": [], "pending": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed": [], "pending": []}


def save_watermark(wm: dict) -> None:
    _watermark_path().write_text(json.dumps(wm, ensure_ascii=False, indent=1), encoding="utf-8")


async def maybe_distill(adapter, run_id: int, user_message: str, final_text: str) -> str:
    """Run 后提炼(入口在 repl/gui 调用):watermark 去重 → 模型提炼 → 候选落盘 pending。"""
    wm = load_watermark()
    if str(run_id) in wm["processed"] or str(run_id) in wm["pending"]:
        return ""
    wm["pending"].append(str(run_id))
    save_watermark(wm)  # 先占位(at-least-once):模型失败下次重试
    try:
        from otter.models.types import Message

        resp = await adapter.complete_stream(
            [Message(role="user", content=DISTILL_PROMPT + f"\n\n[任务]{user_message[:1200]}\n[结果]{final_text[:1200]}")],
            tools=None,
        )
        text = (resp.content or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(text)
    except Exception:
        wm["pending"].remove(str(run_id))  # 失败释放占位,下次重试
        save_watermark(wm)
        return ""
    if data.get("action") != "create" or not data.get("name") or not data.get("procedure"):
        wm["pending"].remove(str(run_id))
        wm["processed"].append(str(run_id))  # none 也算处理过,永不重复问
        save_watermark(wm)
        return ""
    cand_dir = _skills_root() / "candidates"
    cand_dir.mkdir(exist_ok=True)
    (cand_dir / f"run{run_id}.json").write_text(json.dumps({
        "run_id": run_id, "name": data["name"], "description": data.get("description", ""),
        "procedure": data["procedure"], "created": time.strftime("%Y-%m-%d %H:%M"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    wm["pending"].remove(str(run_id))
    wm["processed"].append(str(run_id))
    save_watermark(wm)
    return f"(Skill 候选已生成:{data['name']} — 用 /skill accept 确认)"


def accept_candidate(run_id: int) -> str:
    """人工确认转正:候选 → .otter/skills/<name>/SKILL.md(没有人工确认,候选永远不生效)。"""
    cand = _skills_root() / "candidates" / f"run{run_id}.json"
    if not cand.is_file():
        return f"[otter] 无 run{run_id} 的候选;用 /skill list 查看"
    data = json.loads(cand.read_text(encoding="utf-8"))
    target = _skills_root() / data["name"]
    if target.exists():
        return f"[otter] 技能 {data['name']} 已存在,拒绝覆盖"
    target.mkdir()
    (target / "SKILL.md").write_text(
        f"---\nname: {data['name']}\ndescription: {data['description']}\n"
        f"created_from: run{run_id}\ncreated: {data['created']}\n---\n\n"
        f"# {data['name']}\n\n{data['procedure']}\n",
        encoding="utf-8",
    )
    cand.unlink()
    return f"[otter] 技能 {data['name']} 已转正(.otter/skills/{data['name']}/SKILL.md)"


def list_skills() -> str:
    confirmed = sorted(p.parent.name for p in _skills_root().glob("*/SKILL.md"))
    cands = sorted((p.stem, json.loads(p.read_text(encoding="utf-8"))["name"])
                   for p in (_skills_root() / "candidates").glob("*.json")) if (_skills_root() / "candidates").is_dir() else []
    lines = []
    if confirmed:
        lines.append("已转正:" + ", ".join(confirmed))
    if cands:
        lines.append("候选(待确认):" + ", ".join(f"{stem}→{name}" for stem, name in cands))
    return "\n".join(lines) or "(无技能)"


class SkillListTool(Tool):
    name = "skill_list"
    description = "列出可复用技能(从历史任务提炼并经人工确认的做法)。"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any]) -> str:
        return list_skills()
