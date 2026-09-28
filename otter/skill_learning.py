"""Skill Learning 簇挖掘(v0.4,2026-09-24)——从多个 Completed Task 蒸馏可复用技能。

来源:vesta backend/app/skill_learning/ 移植适配(vesta-copy 策略)。两阶段流水线:
1. TaskPatternMiner:只吃轻量 TaskCard 投影(title/goal/key_facts/final_steps),
   判断批内是否存在"本质相似且会复发"的任务簇(允许空);
2. ProcedureDistiller:按簇深挖执行证据(Task.run_ids → 事件流压缩摘要),
   对照现有 Skill Catalog 与 pending 候选判 CREATE/UPDATE/NONE;
   CREATE 且与现有技能疑似同族时二次仲裁防重复。
触发:watermark 批处理——累计 N 个(OTTER_SKILL_BATCH,默认 20)新 Completed Task
才触发一次(at-least-once:inflight batch 持久化,失败下次重试)。
Human Gate:候选永远不自动生效;accept 才写正式 SKILL.md。

与 vesta 的差异(刻意):模型调用直接用传入 adapter(otter 无 registry 工厂,挂点传
reflection_adapter=便宜模型角色);Trace 证据压缩简化——otter 的 Task.run_ids 已把
事件范围限定到相关 Run,无需 vesta 的 TaskTraceSelector 锚点区间切分;catalog 全文
直塞(otter 技能规模小,省掉 RELEVANCE 预筛一次模型调用)。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time as _time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from otter.models.types import Message, ModelUsage

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

# ── 提示词(移植 vesta skill_learning/prompts.py,原文) ─────────────

_PATTERN_MINING_PROMPT = """You are Otter's Completed Task Pattern Miner.

Your only job: decide whether a batch of COMPLETED tasks contains task types that
are fundamentally similar, likely to recur, and worth learning as a reusable
procedure. Do NOT answer the user, do NOT call tools, do NOT modify Task or Skill,
do NOT create any Skill. You only classify.

Rules:
- Output strict JSON only, no markdown fence:
  {"clusters": [{"id": "...", "task_ids": [...], "pattern_name": "...",
    "description": "...", "similarity_reason": "...", "reusable_value": "..."}]}
- A cluster must contain at least the configured minimum number of task_ids
  (see the batch below). Frequency alone is not enough: the tasks must share a
  genuine multi-step workflow with stable verification.
- This first stage intentionally receives TaskCards, not full Trace events. Do
  not reject a plausible cluster merely because commands or raw execution logs
  are absent here. Repeated matching final_steps, key_facts, goals, and run counts
  are enough to nominate a cluster; the Distiller will inspect Trace evidence and
  reject any procedure that is not actually supported.
- Return {"clusters": []} when there is no real reusable pattern. Do NOT force a
  cluster just to produce output.
- Do NOT distill simple mechanical single-step actions into skills, such as:
  renaming a file, reading a file, simple arithmetic, any single-tool action,
  or mechanical actions without an obvious workflow.
- Prefer patterns with: multi-step workflows, repeated similar failures, the user
  correcting the same mistake repeatedly, a stable verification method, clearly
  avoidable redundant steps, and a clear reduction in future cost or error rate.
- Each cluster's task_ids must be a subset of the provided task ids. A task id may
  appear in at most one cluster in this batch.
- id should be a short stable slug for the pattern (e.g. "python-runtime-debug")."""

_DISTILLATION_PROMPT = """You are Otter's Procedure Distiller.

A Pattern Miner found a cluster of similar COMPLETED tasks. Your job: decide
whether these tasks prove a stable, worth-keeping procedure, and if so produce a
Skill Candidate. You do NOT write or modify any Skill; the candidate only enters
human review.

Rules:
- Distinguish a procedure that worked once by luck from a stable flow repeatedly
  validated across the source tasks. Only propose when the latter is credible.
- Output strict JSON only, no markdown fence:
  {"action":"none|create|update","proposed_name":null,"description":null,
   "reason":"...","procedure":[...],"pitfalls":[...],"verification":[...],
   "existing_skill_name":null}
- action "none" must leave all mutation fields null.
- action "update" requires existing_skill_name (one of the catalog skills below)
  and should NOT propose a duplicate skill name. If the recent tasks simply
  enrich an existing skill, choose update instead of create.
- action "create" requires proposed_name following the existing naming style
  (lowercase, hyphens), plus description/procedure/pitfalls/verification.
- proposed_name must not collide with the catalog below (no debug-python-v2,
  python-debug duplicates).
- BEFORE choosing create/update, also consider the pending_candidates list below:
  these are candidates already proposed and waiting for human review but not yet
  a real Skill. If this pattern is already covered by a pending candidate
  (same meaning, even if the proposed name differs slightly), return action
  "none" so we do not create a duplicate pending candidate. Do not invent a
  merge; just avoid duplicates.
- The "related_skills" field below contains the FULL body of existing skills
  (name + description alone cannot prove coverage). Read their bodies carefully
  before choosing action. The decisive question is whether this new procedure
  belongs to the SAME task family / capability domain as an existing skill, or
  is an INDEPENDENT task family:
  - SAME task family as an existing skill:
      - If the existing skill body ALREADY fully covers this procedure (the
        same stable steps / pitfalls / verification), return action "none".
      - If the existing skill body does NOT fully cover it, but multiple
        completed tasks provide stable NEW steps / pitfalls / verification that
        naturally extend that skill, return action "update" with that
        existing_skill_name set. Do NOT switch to "create" merely because the
        specific steps are missing from the existing body — same family means
        extend the existing skill.
  - DIFFERENT task family from every existing skill:
      - If it has independent, stable reusable value, return action "create".
      - Otherwise return action "none".
  Examples:
      - A "debug Python errors" skill + interpreter / virtualenv mismatch
        troubleshooting (same family: both are Python runtime troubleshooting)
        -> "update".
      - A "debug Python errors" skill + PostgreSQL slow query optimization
        (different family: database tuning) -> "create".
- procedure: the ordered stable steps. pitfalls: repeated mistakes to avoid.
  verification: how to confirm the procedure works.
- Do not invent evidence not present in the provided execution summaries. If the
  evidence is too thin to support a stable procedure, return action "none"."""

_OVERLAP_ADJUDICATION_PROMPT = """You are Otter's Skill Overlap Adjudicator.

The Procedure Distiller proposed CREATE even though one or more existing Skills
were selected as semantically related. Decide only whether the proposed procedure
belongs to the SAME task family as one related Skill, or to a genuinely DIFFERENT
task family. This is a duplicate-prevention review, not a new distillation pass.

Rules:
- Return strict JSON only, no markdown fence:
  {"relationship":"same|different","existing_skill_name":null,"reason":"..."}
- SAME means the goal and reusable capability naturally extend an existing Skill,
  even when the exact new steps are absent from its current body. Set
  existing_skill_name to exactly one supplied related Skill.
- DIFFERENT means an independent goal and procedure that deserves its own Skill.
  Leave existing_skill_name null.
- Prefer SAME when a proposed specialization is a troubleshooting subcase of a
  broader existing troubleshooting Skill.
- Do not judge evidence quality or rewrite the procedure; the first Distiller has
  already done that."""


# ── 领域模型(移植 vesta models.py,校验不变量原样) ─────────────────

def _normalize_text(value: str) -> str:
    return " ".join(value.split()).strip()


class TaskCard(BaseModel):
    """Completed Task 的轻量投影(miner 第一阶段不接触执行证据)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    title: str
    goal: str | None = None
    constraints: tuple[str, ...] = ()
    key_facts: tuple[str, ...] = ()
    final_steps: tuple[str, ...] = ()
    run_count: int = 0


class TaskPatternCluster(BaseModel):
    """一组本质相似的 Completed Task 形成的模式簇(只描述,不生成 SKILL.md)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    task_ids: tuple[str, ...]
    pattern_name: str
    description: str
    similarity_reason: str
    reusable_value: str

    @field_validator("id", "pattern_name", "description", "similarity_reason", "reusable_value")
    @classmethod
    def normalize_required(cls, value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("cluster text fields must be strings")
        normalized = _normalize_text(value)
        if not normalized:
            raise ValueError("cluster text fields cannot be empty")
        return normalized

    @field_validator("task_ids")
    @classmethod
    def normalize_task_ids(cls, value: object) -> tuple[str, ...]:
        seen: list[str] = []
        for item in value if isinstance(value, (list, tuple)) else ():
            if isinstance(item, str) and item.strip() and item.strip() not in seen:
                seen.append(item.strip())
        if not seen:
            raise ValueError("task_ids cannot be empty")
        return tuple(seen)


class PatternMiningResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    clusters: tuple[TaskPatternCluster, ...] = ()


class SkillCandidateStatus:
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SkillCandidateAction:
    CREATE = "create"
    UPDATE = "update"


class SkillCandidate(BaseModel):
    """从多个 Completed Task 提炼、待人工审核的候选过程知识(可溯源)。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    action: str  # create | update
    proposed_name: str
    description: str
    reason: str
    procedure: tuple[str, ...] = ()
    pitfalls: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    source_task_ids: tuple[str, ...] = ()
    source_run_ids: tuple[str, ...] = ()
    existing_skill_name: str | None = None
    status: str = SkillCandidateStatus.PENDING
    created_at: str = ""
    evidence_summary: str = ""

    @model_validator(mode="after")
    def validate_action(self) -> "SkillCandidate":
        if self.action == SkillCandidateAction.UPDATE and not self.existing_skill_name:
            raise ValueError("update candidate requires existing_skill_name")
        if self.action == SkillCandidateAction.CREATE and self.existing_skill_name is not None:
            raise ValueError("create candidate cannot have existing_skill_name")
        if not self.source_task_ids:
            raise ValueError("candidate must reference at least one source task")
        if not self.procedure:
            raise ValueError("candidate must contain at least one procedure step")
        if not _SKILL_NAME_RE.fullmatch(self.proposed_name):
            raise ValueError("proposed_name must be lowercase-hyphen slug")
        return self


class InflightBatch(BaseModel):
    """处理中的 Mining batch(持久化 → at-least-once,失败下次重试)。"""

    model_config = ConfigDict(extra="forbid")
    batch_id: str
    task_ids: tuple[str, ...]
    attempt: int = 0
    last_error: str | None = None


class MiningWatermark(BaseModel):
    """Mining 水位:processed 永不重复计数;pending 凑批;inflight 重试。"""

    model_config = ConfigDict(extra="forbid")
    processed_task_ids: tuple[str, ...] = ()
    pending_task_ids: tuple[str, ...] = ()
    inflight: InflightBatch | None = None
    last_mining_at: str | None = None
    last_error: str | None = None


# ── 持久化(移植 vesta store.py;.otter/skill-learning/) ─────────────

def _learning_root() -> Path:
    root = Path.cwd() / ".otter" / "skill-learning"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    temp.replace(path)  # 原子替换


class SkillCandidateStore:
    """候选 + 水位的本地 JSON 持久化(每候选一文件;watermark 单文件)。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or _learning_root()
        self.candidates_dir = self.data_dir / "candidates"
        self.watermark_path = self.data_dir / "watermark.json"

    def load_watermark(self) -> MiningWatermark:
        if not self.watermark_path.is_file():
            return MiningWatermark()
        try:
            return MiningWatermark.model_validate_json(self.watermark_path.read_text(encoding="utf-8"))
        except Exception:
            return MiningWatermark()  # 损坏重置(vesta 同:单文件坏不拖累)

    def save_watermark(self, wm: MiningWatermark) -> None:
        _write_json(self.watermark_path, wm.model_dump(mode="json"))

    def create(self, cand: SkillCandidate) -> None:
        self.candidates_dir.mkdir(parents=True, exist_ok=True)
        path = self.candidates_dir / f"{cand.id}.json"
        if path.is_file():
            raise ValueError(f"candidate already exists: {cand.id}")
        _write_json(path, cand.model_dump(mode="json"))

    def get(self, cand_id: str) -> SkillCandidate | None:
        path = self.candidates_dir / f"{cand_id.strip().lower()}.json"
        if not path.is_file():
            return None
        try:
            return SkillCandidate.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list(self, status: str | None = None) -> list[SkillCandidate]:
        if not self.candidates_dir.is_dir():
            return []
        out = []
        for path in sorted(self.candidates_dir.glob("*.json")):
            c = self.get(path.stem)
            if c is not None and (status is None or c.status == status):
                out.append(c)
        out.sort(key=lambda c: c.created_at, reverse=True)
        return out

    def update(self, cand: SkillCandidate) -> None:
        _write_json(self.candidates_dir / f"{cand.id}.json", cand.model_dump(mode="json"))

    def find_duplicate_source(self, source_task_ids: tuple[str, ...]) -> SkillCandidate | None:
        source_set = set(source_task_ids)
        for c in self.list():
            if set(c.source_task_ids) == source_set:
                return c
        return None


# ── 模型调用小件(vesta _call.py 角色,otter 简版) ──────────────────

async def _call_json(adapter, system_prompt: str, user_content: str) -> tuple[dict | None, str | None]:
    """一次结构化 JSON 调用;返回 (payload, error)。"""
    try:
        resp = await adapter.complete_stream(
            [Message(role="system", content=system_prompt),
             Message(role="user", content=user_content)], tools=None)
        text = (resp.content or "").strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


# ── 第一阶段:Pattern Miner(vesta miner.py 移植) ──────────────────

async def mine_patterns(adapter, cards: list[TaskCard], min_cluster_size: int) -> tuple[list[TaskPatternCluster], str | None]:
    user_content = json.dumps([c.model_dump(mode="json") for c in cards], ensure_ascii=False, separators=(",", ":"))
    payload, err = await _call_json(adapter, _PATTERN_MINING_PROMPT, user_content)
    if err or payload is None:
        return [], err or "miner returned non-JSON"
    try:
        parsed = PatternMiningResult.model_validate(payload)
    except Exception as exc:
        return [], f"invalid pattern mining schema: {exc}"
    valid_ids = {c.task_id for c in cards}
    # 簇过滤:规模达标且 task_ids ⊆ 批次(vesta 同款防线:模型编造 id 直接弃)
    clusters = [cl for cl in parsed.clusters
                if len(cl.task_ids) >= min_cluster_size and set(cl.task_ids).issubset(valid_ids)]
    return clusters, None


# ── 第二阶段:Procedure Distiller(vesta distiller.py 简化移植) ─────

async def _compress_trace(task, load_events) -> str:
    """压缩执行证据:Task.run_ids 限定的事件流 → 摘要文本(otter 简化:
    run_ids 已圈定范围,无需 vesta TaskTraceSelector 的锚点区间切分)。"""
    lines = [f"任务[{task.title}] 各 Run 执行证据:"]
    done_notes = [f"已完成步骤:{s.title}(依据:{s.note})" for s in task.steps if s.status.value == "done"]
    lines += done_notes[:10]
    for rid in task.run_ids[:5]:
        try:
            events = await load_events(int(rid))
        except Exception:
            continue
        seq = []
        for ev in events[:200]:
            et, p = ev.get("type", ""), ev.get("payload", {})
            if et == "TOOL_STARTED":
                args_preview = str(p.get("arguments", {}))[:80]
                seq.append(f"{p.get('name')}({args_preview})")
            elif et == "MODEL_ERROR":
                seq.append(f"[错误]{str(p.get('error', ''))[:60]}")
            elif et == "FILE_CHANGED":
                seq.append(f"[产出]{p.get('name')}({p.get('action')})")
        if seq:
            lines.append(f"Run {rid}: " + " → ".join(seq[:40]))
    return "\n".join(lines)


def _catalog_brief(skills_root: Path) -> tuple[str, list[str]]:
    """现有技能目录:全文(otter 技能少,省 relevance 预筛;>5 个才截断)。"""
    bodies, names = [], []
    for md in sorted(skills_root.glob("*/SKILL.md")):
        names.append(md.parent.name)
        if len(bodies) < 5:
            bodies.append(f"### {md.parent.name}\n" + md.read_text(encoding="utf-8")[:1500])
    return ("\n\n".join(bodies) or "(无现有技能)"), names


async def distill_cluster(adapter, cluster: TaskPatternCluster, tasks_by_id: dict,
                          load_events, skills_root: Path, pending_names: list[str]) -> tuple[dict | None, str | None]:
    # load_events 为 async 回调(rid -> list[event]);逐任务压缩证据
    parts = []
    for tid in cluster.task_ids:
        if tid in tasks_by_id:
            parts.append(await _compress_trace(tasks_by_id[tid], load_events))
    evidence = "\n\n".join(parts)
    catalog, catalog_names = _catalog_brief(skills_root)
    user_content = (
        f"## 簇:{cluster.pattern_name}\n{cluster.description}\n\n"
        f"## 现有技能目录(全文)\n{catalog}\n\n"
        f"## 待审候选(防重复)\n{json.dumps(pending_names, ensure_ascii=False)}\n\n"
        f"## 执行证据\n{evidence[:6000]}"
    )
    payload, err = await _call_json(adapter, _DISTILLATION_PROMPT, user_content)
    if err or payload is None:
        return None, err or "distiller returned non-JSON"
    if payload.get("action") not in ("create", "update", "none"):
        return None, f"invalid action: {payload.get('action')}"
    if payload["action"] == "none":
        return payload, None

    # CREATE 且目录非空 → 重叠仲裁(vesta 防重复二道闸)
    if payload["action"] == "create" and catalog_names:
        adj_payload, adj_err = await _call_json(
            adapter, _OVERLAP_ADJUDICATION_PROMPT,
            f"## 提议\n{payload.get('proposed_name')}: {payload.get('description')}\n\n"
            f"## 相关技能(全文见上)\n{json.dumps(catalog_names, ensure_ascii=False)}")
        if adj_payload is not None and adj_payload.get("relationship") == "same":
            name = adj_payload.get("existing_skill_name")
            if name in catalog_names:  # 仲裁指向目录内技能才信(防编造)
                payload = {**payload, "action": "update", "existing_skill_name": name,
                           "proposed_name": name}
    if payload["action"] == "update" and payload.get("existing_skill_name") not in catalog_names:
        return None, f"update target not in catalog: {payload.get('existing_skill_name')}"
    return payload, None


# ── 编排:watermark 批处理(vesta service.py 的 maybe_run_mining 移植) ──

def _batch_size() -> int:
    try:
        return max(2, int(os.environ.get("OTTER_SKILL_BATCH", "20")))
    except ValueError:
        return 20


async def maybe_run_mining(adapter, task_store, events_store, skills_root: Path | None = None) -> str:
    """挂点(repl/_dispatch 成功后):新 Completed Task 凑满一批才挖掘。

    at-least-once:inflight 落 watermark,失败下次触发点重试;成功移入 processed。
    返回给人看的提示文本(空=无动作)。"""
    from otter.task import TaskStatus

    root = skills_root or (Path.cwd() / ".otter" / "skills")
    store = SkillCandidateStore()
    wm = store.load_watermark()

    # inflight 优先重试(at-least-once)
    completed = await task_store.list(status=TaskStatus.COMPLETED, limit=200)
    completed_ids = [t.id for t in completed]
    fresh = [tid for tid in completed_ids
             if tid not in wm.processed_task_ids and tid not in wm.pending_task_ids]
    if wm.inflight is None:
        if len(fresh) + len(wm.pending_task_ids) < _batch_size():
            wm.pending_task_ids = tuple([*wm.pending_task_ids, *fresh])
            store.save_watermark(wm)  # 攒批
            return ""
        batch_ids = tuple([*wm.pending_task_ids, *fresh][:_batch_size()])
        wm.pending_task_ids = tuple([*wm.pending_task_ids, *fresh][_batch_size():])
        wm.inflight = InflightBatch(batch_id=uuid4().hex[:12], task_ids=batch_ids)
        store.save_watermark(wm)
    batch_ids = list(wm.inflight.task_ids)
    wm.inflight.attempt += 1

    tasks_by_id = {t.id: t for t in completed}
    cards = []
    for tid in batch_ids:
        t = tasks_by_id.get(tid)
        if t is None:  # 任务文件被删:跳过并记 processed(不阻塞批次)
            continue
        cards.append(TaskCard(
            task_id=t.id, title=t.title, goal=t.goal, constraints=t.constraints,
            key_facts=t.key_facts,
            final_steps=tuple(f"{s.title}[{s.status.value}]" for s in t.steps[-12:]),
            run_count=len(t.run_ids)))
    if not cards:
        wm.processed_task_ids = tuple([*wm.processed_task_ids, *batch_ids])
        wm.inflight = None
        store.save_watermark(wm)
        return ""

    min_cluster = max(2, int(os.environ.get("OTTER_SKILL_MIN_CLUSTER", "3")))
    clusters, err = await mine_patterns(adapter, cards, min_cluster)
    if err:
        wm.inflight.last_error = err
        store.save_watermark(wm)  # 保留 inflight,下次重试
        return ""

    notes = []

    async def load_events(rid: int) -> list[dict]:
        return await events_store.load_run_events(rid)

    pending_names = [c.proposed_name for c in store.list(status=SkillCandidateStatus.PENDING)]
    for cluster in clusters:
        payload, derr = await distill_cluster(
            adapter, cluster, tasks_by_id, load_events, root, pending_names)
        if derr or payload is None or payload.get("action") == "none":
            continue
        cand = SkillCandidate(
            id=uuid4().hex[:12], action=payload["action"],
            proposed_name=payload.get("proposed_name") or payload.get("existing_skill_name") or "",
            description=payload.get("description") or "", reason=payload.get("reason") or "",
            procedure=tuple(payload.get("procedure") or ()),
            pitfalls=tuple(payload.get("pitfalls") or ()),
            verification=tuple(payload.get("verification") or ()),
            source_task_ids=tuple(tid for tid in cluster.task_ids if tid in tasks_by_id),
            source_run_ids=tuple(rid for tid in cluster.task_ids
                                 if (task := tasks_by_id.get(tid)) is not None
                                 for rid in task.run_ids),
            existing_skill_name=payload.get("existing_skill_name"),
            created_at=_time.strftime("%Y-%m-%d %H:%M:%S"),
            evidence_summary=f"簇[{cluster.pattern_name}]:{cluster.similarity_reason[:200]}")
        try:
            if store.find_duplicate_source(cand.source_task_ids) is not None:
                continue  # 同源候选已存在(vesta 防重复)
            store.create(cand)
            pending_names.append(cand.proposed_name)
            notes.append(f"{cand.action}:{cand.proposed_name}")
        except ValueError:
            continue

    wm.processed_task_ids = tuple([*wm.processed_task_ids, *batch_ids])
    wm.inflight = None
    wm.last_mining_at = _time.strftime("%Y-%m-%d %H:%M:%S")
    store.save_watermark(wm)
    if notes:
        return f"(Skill Learning:新候选 {'; '.join(notes)} — /skill cand 查看,人工确认才生效)"
    return ""


# ── Human Gate(accept 生成/扩展 SKILL.md;reject 落状态) ────────────

def review(candidate_id: str, accept: bool) -> str:
    """人工审核:accept=create 写新 SKILL.md / update 追加既有技能;reject=拒绝。"""
    store = SkillCandidateStore()
    cand = store.get(candidate_id)
    if cand is None:
        return f"[otter] 无候选 {candidate_id};用 /skill cand 查看"
    if cand.status != SkillCandidateStatus.PENDING:
        return f"[otter] 候选 {candidate_id} 已审核过({cand.status})"
    root = Path.cwd() / ".otter" / "skills"
    if accept:
        if cand.action == SkillCandidateAction.CREATE:
            target = root / cand.proposed_name
            if target.exists():
                return f"[otter] 技能 {cand.proposed_name} 已存在,拒绝覆盖"
            target.mkdir(parents=True)
            body = _render_skill_md(cand, created_from=cand.id)
            (target / "SKILL.md").write_text(body, encoding="utf-8")
            msg = f"[otter] 技能 {cand.proposed_name} 已转正"
        else:  # update:追加到既有 SKILL.md 末尾(vesta 同:扩展现有技能体)
            path = root / cand.existing_skill_name / "SKILL.md"
            if not path.is_file():
                return f"[otter] 目标技能 {cand.existing_skill_name} 不存在"
            extra = _render_skill_update_md(cand)
            path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
            msg = f"[otter] 技能 {cand.existing_skill_name} 已扩展(候选 {candidate_id})"
        cand.status = SkillCandidateStatus.ACCEPTED
    else:
        cand.status = SkillCandidateStatus.REJECTED
        msg = f"[otter] 候选 {candidate_id} 已拒绝"
    store.update(cand)
    return msg


def _render_skill_md(cand: SkillCandidate, created_from: str) -> str:
    def bullets(items):
        return "\n".join(f"- {i}" for i in items) or "-(无)"
    return (f"---\nname: {cand.proposed_name}\ndescription: {cand.description}\n"
            f"created_from: skill-learning:{created_from}\ncreated: {cand.created_at}\n---\n\n"
            f"# {cand.proposed_name}\n\n## 稳定步骤\n{bullets(cand.procedure)}\n\n"
            f"## 常见坑\n{bullets(cand.pitfalls)}\n\n## 验证方法\n{bullets(cand.verification)}\n\n"
            f"> 来源:任务 {', '.join(cand.source_task_ids[:6])};{cand.evidence_summary}\n")


def _render_skill_update_md(cand: SkillCandidate) -> str:
    def bullets(items):
        return "\n".join(f"- {i}" for i in items) or "-(无)"
    return (f"\n\n---\n\n## 扩展({cand.created_at},候选 {cand.id})\n\n"
            f"### 新增稳定步骤\n{bullets(cand.procedure)}\n\n"
            f"### 新增常见坑\n{bullets(cand.pitfalls)}\n\n"
            f"### 新增验证\n{bullets(cand.verification)}\n")


def list_candidates() -> str:
    """待审候选一览(含已审尾部)。"""
    store = SkillCandidateStore()
    pending = store.list(status=SkillCandidateStatus.PENDING)
    reviewed = [c for c in store.list() if c.status != SkillCandidateStatus.PENDING][:5]
    lines = []
    for c in pending:
        lines.append(f"{c.id} [{c.action}] {c.proposed_name} — {c.description[:60]}"
                     f"(源任务×{len(c.source_task_ids)})")
    if reviewed:
        lines.append("已审:" + ", ".join(f"{c.proposed_name}({c.status})" for c in reviewed))
    return "\n".join(lines) or "(无候选;Completed Task 凑满一批后自动挖掘)"


__all__ = ["maybe_run_mining", "mine_patterns", "distill_cluster", "review",
           "list_candidates", "SkillCandidateStore", "SkillCandidate", "TaskCard"]
