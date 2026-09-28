"""Task 系统(v0.3,2026-09-24)——任务事实的权威源,独立于会话消息持久化。

实现要点:
- 数据模型(Task/TaskStep/TaskPatch):pydantic 校验原样(唯一步骤/单 in_progress/
  终态不可回退/done+blocked 必须带 note 等不变量);
- FileTaskStore:每任务一个 JSON(<cwd>/.otter/tasks/<id>.json),临时文件+原子替换,
  乐观锁 revision,plan_accept/plan_reject 仅 PENDING 可转;
- 4 工具:task_create/update/get/list;otter 无独立工具执行上下文,
  以共享 ctx dict(conversation_id/mode/run_id)替代——装配方(GUI/REPL)持有并更新;
- 上下文注入:render_task_context 渲染预算受控快照(done 只留近 3 条、条目 500 字、
  列表 12 条、待办 12 步),由 loop._build_view 注入 <active_task> 段。

与 的差异(刻意):无 legacy 迁移、无 recall_fields_for(otter 无该链)、
工具返回 str(otter 工具协议)、context 并入单 system 视图。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from otter.tools.base import Tool

TASK_ID_LENGTH = 32
_TASK_ID_RE = re.compile(rf"^[0-9a-f]{{{TASK_ID_LENGTH}}}$")
_TASK_PREFIX_RE = re.compile(rf"^[0-9a-f]{{4,{TASK_ID_LENGTH}}}$")
_MAX_TITLE_CHARS = 500
_MAX_TEXT_CHARS = 4_000
_MAX_ENTRY_CHARS = 2_000
_MAX_ENTRIES = 100
_MAX_STEPS = 100
_MAX_LIST_LIMIT = 100
MAX_TASK_FILE_BYTES = 1_000_000

# 会被当作"正在执行的活动任务"注入上下文的非终态状态(标准语义:PENDING
# 是计划已生成未开始,不算活动;ACTIVE/PAUSED 才注入)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_CONTEXT_TASK_STATUSES = frozenset({"active", "paused"})


def _tasks_root() -> Path:
    """任务目录跟 otter 其他 .otter 数据一样按 cwd 绑定(GUI/库同工作区)。"""
    root = Path.cwd() / ".otter" / "tasks"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ── 数据模型(,校验不变量原样) ──────────

class TaskStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TaskStepStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("optional text must be a string or None")
    normalized = _normalize_text(value)
    return normalized or None


def _normalize_entries(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, str) else value
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in values:
        if not isinstance(entry, str):
            raise TypeError("task entries must be strings")
        text = _normalize_text(entry)
        if len(text) > _MAX_ENTRY_CHARS:
            raise ValueError("task entry is too long")
        if text and text not in seen:
            normalized.append(text)
            seen.add(text)
    return tuple(normalized)


class TaskStep(BaseModel):
    """任务中的一个可追踪步骤(done/blocked 必须带 note——防伪造完成)。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    status: TaskStepStatus = TaskStepStatus.TODO
    note: str | None = None

    @field_validator("id", "title", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object, info: ValidationInfo) -> str:
        if not isinstance(value, str):
            raise TypeError("task step id and title must be strings")
        normalized = _normalize_text(value)
        if not normalized:
            raise ValueError("task step id and title cannot be empty")
        maximum = 128 if info.field_name == "id" else _MAX_TITLE_CHARS
        if len(normalized) > maximum:
            raise ValueError("task step id or title is too long")
        return normalized

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: object) -> str | None:
        normalized = _normalize_optional_text(value)
        if normalized is not None and len(normalized) > _MAX_TEXT_CHARS:
            raise ValueError("task step note is too long")
        return normalized

    @model_validator(mode="after")
    def validate_status_note(self) -> "TaskStep":
        if self.status in {TaskStepStatus.DONE, TaskStepStatus.BLOCKED} and not self.note:
            raise ValueError(f"task step with status {self.status.value} requires a note")
        return self


class Task(BaseModel):
    """一个可恢复、可查询的长任务(目标/约束/进度/关键事实,抗上下文压缩)。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str | None = None
    goal: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.NORMAL
    constraints: tuple[str, ...] = ()
    state: tuple[str, ...] = ()
    key_facts: tuple[str, ...] = ()
    steps: tuple[TaskStep, ...] = ()
    owner_conversation_id: str = Field(min_length=1, frozen=True)
    run_ids: tuple[str, ...] = ()
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    revision: int = Field(default=1, ge=1)

    @field_validator("title", "description", "goal", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("text fields must be strings or None")
        normalized = _normalize_text(value)
        maximum = _MAX_TITLE_CHARS if info.field_name == "title" else _MAX_TEXT_CHARS
        if len(normalized) > maximum:
            raise ValueError(f"task {info.field_name} is too long")
        return normalized or None

    @field_validator("constraints", "state", "key_facts", mode="before")
    @classmethod
    def normalize_entries(cls, value: object) -> tuple[str, ...]:
        return _normalize_entries(value)

    @field_validator("title")
    @classmethod
    def title_required(cls, value: str | None) -> str:
        if not value:
            raise ValueError("task title cannot be empty")
        return value

    @field_validator("id", mode="before")
    @classmethod
    def id_required(cls, value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("task id must be a string")
        normalized = value.strip().lower()
        if not _TASK_ID_RE.fullmatch(normalized):
            raise ValueError("task id must be a 32-character hexadecimal string")
        return normalized

    @field_validator("owner_conversation_id", mode="before")
    @classmethod
    def normalize_owner(cls, value: object) -> str:
        normalized = _normalize_optional_text(value)
        if normalized is None:
            raise ValueError("task owner_conversation_id cannot be empty")
        return normalized

    @field_validator("created_at", "updated_at", "completed_at")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("task datetimes must include timezone information")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_invariants(self) -> "Task":
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("task step ids must be unique")
        if len(self.steps) > _MAX_STEPS:
            raise ValueError(f"task cannot contain more than {_MAX_STEPS} steps")
        in_progress = sum(step.status is TaskStepStatus.IN_PROGRESS for step in self.steps)
        if in_progress > 1:
            raise ValueError("task can contain at most one in_progress step")
        if self.status is TaskStatus.PAUSED and in_progress:
            raise ValueError("paused task cannot contain an in_progress step")
        if self.status is TaskStatus.COMPLETED and self.steps:
            if any(step.status is not TaskStepStatus.DONE for step in self.steps):
                raise ValueError("completed task requires all steps to be done")
        for field_name in ("constraints", "state", "key_facts"):
            if len(getattr(self, field_name)) > _MAX_ENTRIES:
                raise ValueError(f"task {field_name} cannot contain more than {_MAX_ENTRIES} entries")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at cannot be earlier than created_at")
        return self

    @property
    def progress_summary(self) -> str:
        total = len(self.steps)
        done = sum(1 for step in self.steps if step.status is TaskStepStatus.DONE)
        if total:
            return f"[{self.status.value}] {self.title} ({done}/{total} 步骤完成)"
        return f"[{self.status.value}] {self.title}"


class TaskPatch(BaseModel):
    """一次任务更新的完整变更集(原子校验+写入)。"""

    model_config = ConfigDict(extra="forbid")

    goal: str | None = None
    status: TaskStatus | None = None
    state: tuple[str, ...] | None = None
    add_constraints: tuple[str, ...] = ()
    add_key_facts: tuple[str, ...] = ()
    replace_steps: tuple[TaskStep, ...] | None = None
    step_id: str | None = None
    step_status: TaskStepStatus | None = None
    step_note: str | None = None
    run_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator("goal", "step_note", mode="before")
    @classmethod
    def normalize_optional(cls, value: object) -> str | None:
        return _normalize_optional_text(value)

    @field_validator("state", "add_constraints", "add_key_facts", mode="before")
    @classmethod
    def normalize_patch_entries(cls, value: object) -> tuple[str, ...] | None:
        if value is None:
            return None
        return _normalize_entries(value)

    @model_validator(mode="after")
    def validate_step_update(self) -> "TaskPatch":
        if (self.step_id is None) != (self.step_status is None):
            raise ValueError("step_id and step_status must be provided together")
        if self.step_note is not None and self.step_id is None:
            raise ValueError("step_note requires step_id and step_status")
        if "replace_steps" in self.model_fields_set and self.step_id is not None:
            raise ValueError("replace_steps cannot be combined with a single step update")
        return self

    @property
    def has_changes(self) -> bool:
        explicit_nullable = bool({"goal", "state", "replace_steps"} & self.model_fields_set)
        return explicit_nullable or any((
            self.status is not None,
            bool(self.add_constraints),
            bool(self.add_key_facts),
            self.step_id is not None,
            self.run_id is not None,
        ))


# ── FileTaskStore() ───────

def _validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str):
        raise TypeError("task_id must be a string")
    normalized = task_id.strip().lower()
    if not _TASK_ID_RE.fullmatch(normalized):
        raise ValueError("task_id must be a 32-character hexadecimal string")
    return normalized


def _normalize_required_entry(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = " ".join(value.split()).strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    return normalized


def _merge_entries(existing, new) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for entry in (*existing, *new):
        text = " ".join(entry.split()).strip()
        if text and text not in seen:
            merged.append(text)
            seen.add(text)
    return tuple(merged)


def _read_task(path: Path) -> Task:
    if path.stat().st_size > MAX_TASK_FILE_BYTES:
        raise ValueError(f"task file exceeds {MAX_TASK_FILE_BYTES} byte safety limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Task.model_validate(data)


def _write_task(path: Path, task: Task) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(task.model_dump(mode="json"), ensure_ascii=False, indent=2)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)  # 原子替换:中断不留半文件
    finally:
        if temporary.exists():
            temporary.unlink()


def _scan_tasks(tasks_dir: Path) -> list[Task]:
    if not tasks_dir.is_dir():
        return []
    tasks: list[Task] = []
    for path in sorted(tasks_dir.glob("*.json")):
        if path.is_symlink() or not _TASK_ID_RE.fullmatch(path.stem):
            continue
        try:
            task = _read_task(path)
            if task.id == path.stem:
                tasks.append(task)
        except (OSError, ValueError, TypeError):
            continue  # 损坏文件跳过(与 同:单文件坏不拖累整体)
    return tasks


def _apply_patch(task: Task, patch: TaskPatch, now: datetime) -> Task:
    """在内存中完成整组变更,最后统一经 Task 重校验(原子性)。"""
    data = task.model_dump(mode="python")
    if task.status in _TERMINAL_STATUSES and patch.status is not None and patch.status is not task.status:
        raise ValueError("terminal task cannot be reopened by ordinary update")
    if "goal" in patch.model_fields_set:
        data["goal"] = patch.goal
    if patch.status is not None:
        data["status"] = patch.status
        if patch.status in _TERMINAL_STATUSES:
            if task.status not in _TERMINAL_STATUSES:
                data["completed_at"] = now
        elif task.status in _TERMINAL_STATUSES:
            data["completed_at"] = None
    if "state" in patch.model_fields_set:
        data["state"] = patch.state or ()
    if patch.add_constraints:
        data["constraints"] = _merge_entries(task.constraints, patch.add_constraints)
    if patch.add_key_facts:
        data["key_facts"] = _merge_entries(task.key_facts, patch.add_key_facts)
    if "replace_steps" in patch.model_fields_set:
        replacement = patch.replace_steps or ()
        _validate_step_replacement(task.steps, replacement)
        data["steps"] = replacement
    if patch.step_id is not None and patch.step_status is not None:
        updated: list[TaskStep] = []
        found = False
        for step in task.steps:
            if step.id == patch.step_id:
                found = True
                if step.status is TaskStepStatus.DONE and patch.step_status is not TaskStepStatus.DONE:
                    raise ValueError("done task step cannot be rolled back")
                step_data = step.model_dump(mode="python")
                step_data["status"] = patch.step_status
                if "step_note" in patch.model_fields_set:
                    step_data["note"] = patch.step_note
                updated.append(TaskStep.model_validate(step_data))
            else:
                updated.append(step)
        if not found:
            raise KeyError(f"任务步骤不存在:{patch.step_id}")
        data["steps"] = tuple(updated)
    if patch.run_id is not None:
        data["run_ids"] = _merge_entries(task.run_ids, (patch.run_id,))
    data["revision"] = task.revision + 1
    data["updated_at"] = now
    return Task.model_validate(data)


def _validate_step_replacement(existing, replacement) -> None:
    """重排计划不能删除或回退已开始执行的步骤(防伪造保护)。"""
    replacement_by_id = {step.id: step for step in replacement}
    for step in existing:
        if step.status not in {TaskStepStatus.DONE, TaskStepStatus.IN_PROGRESS}:
            continue
        candidate = replacement_by_id.get(step.id)
        if candidate is None:
            raise ValueError(f"replace_steps cannot delete protected step: {step.id}")
        if step.status is TaskStepStatus.DONE:
            if candidate.status is not TaskStepStatus.DONE:
                raise ValueError(f"replace_steps cannot roll back done step: {step.id}")
        elif candidate.status is TaskStepStatus.TODO:
            raise ValueError(f"replace_steps cannot roll back in_progress step: {step.id}")


class FileTaskStore:
    """任务 CRUD/状态推进/关联管理(本地 JSON 文件,每任务一文件)。"""

    def __init__(self, tasks_dir: str | Path | None = None) -> None:
        self.tasks_dir = Path(tasks_dir).expanduser().resolve() if tasks_dir else _tasks_root()
        self._locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        await asyncio.to_thread(self.tasks_dir.mkdir, parents=True, exist_ok=True)

    async def create(self, *, title: str, description: str | None = None, goal: str | None = None,
                     priority: TaskPriority = TaskPriority.NORMAL, steps=(),
                     owner_conversation_id: str, run_ids=()) -> Task:
        now = datetime.now(UTC)
        task = Task(
            id=uuid4().hex, title=title, description=description, goal=goal,
            status=TaskStatus.PENDING, priority=priority, steps=tuple(steps),
            owner_conversation_id=_normalize_required_entry(
                owner_conversation_id, field_name="owner_conversation_id"),
            run_ids=_merge_entries((), run_ids), created_at=now, updated_at=now,
        )
        await self._write(task)
        return task

    async def get(self, task_id: str) -> Task | None:
        normalized = _validate_task_id(task_id)
        path = self.tasks_dir / f"{normalized}.json"
        if not await asyncio.to_thread(path.is_file) or await asyncio.to_thread(path.is_symlink):
            return None
        try:
            return await asyncio.to_thread(_read_task, path)
        except (OSError, ValueError, TypeError):
            return None

    async def resolve(self, identifier: str, *, owner_conversation_id: str | None = None) -> Task | None:
        """完整 ID 或唯一前缀查找,可先按 owner 过滤(标准语义)。"""
        normalized = identifier.strip().lower()
        if not normalized:
            return None
        if not _TASK_PREFIX_RE.fullmatch(normalized):
            raise ValueError("task identifier must be a 4-32 character hexadecimal prefix")
        owner = (_normalize_required_entry(owner_conversation_id, field_name="owner_conversation_id")
                 if owner_conversation_id is not None else None)
        if len(normalized) == TASK_ID_LENGTH:
            exact = await self.get(normalized)
            if exact is not None and (owner is None or exact.owner_conversation_id == owner):
                return exact
            return None
        matches = [t for t in await self._all_tasks()
                   if t.id.startswith(normalized) and (owner is None or t.owner_conversation_id == owner)]
        if len(matches) > 1:
            raise ValueError(f"任务 ID 前缀不唯一:{identifier}")
        return matches[0] if matches else None

    async def list(self, *, limit: int = 50, status: TaskStatus | None = None,
                   owner_conversation_id: str | None = None) -> tuple[Task, ...]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        tasks = await self._all_tasks()
        if status is not None:
            tasks = [t for t in tasks if t.status is status]
        if owner_conversation_id is not None:
            normalized = _normalize_required_entry(owner_conversation_id, field_name="owner_conversation_id")
            tasks = [t for t in tasks if t.owner_conversation_id == normalized]
        tasks.sort(key=lambda t: t.updated_at, reverse=True)
        return tuple(tasks[:limit])

    async def delete(self, task_id: str) -> bool:
        normalized = _validate_task_id(task_id)
        path = self.tasks_dir / f"{normalized}.json"
        if not await asyncio.to_thread(path.is_file) or await asyncio.to_thread(path.is_symlink):
            return False
        await asyncio.to_thread(path.unlink)
        return True

    async def apply_patch(self, task_id: str, patch: TaskPatch, *,
                          owner_conversation_id: str | None = None) -> Task:
        """原子应用整组变更;校验失败不写任何部分结果(乐观锁在锁内校验)。"""
        normalized = _validate_task_id(task_id)
        if not patch.has_changes:
            raise ValueError("task patch must contain at least one change")
        async with self._lock_for(normalized):
            task = await self._require(normalized)
            if owner_conversation_id is not None:
                owner = _normalize_required_entry(owner_conversation_id, field_name="owner_conversation_id")
                if task.owner_conversation_id != owner:
                    raise KeyError(f"任务不存在:{task_id}")
            if patch.expected_revision is not None and patch.expected_revision != task.revision:
                raise ValueError(f"task revision conflict: expected {patch.expected_revision}, current {task.revision}")
            updated = _apply_patch(task, patch, datetime.now(UTC))
            await self._write(updated)
            return updated

    async def plan_accept(self, task_id: str) -> Task:
        """接受 PENDING 计划:PENDING → ACTIVE(仅 PENDING 可转)。"""
        return await self._transition_from_pending(task_id, TaskStatus.ACTIVE)

    async def plan_reject(self, task_id: str) -> Task:
        """拒绝 PENDING 计划:PENDING → CANCELLED。"""
        return await self._transition_from_pending(task_id, TaskStatus.CANCELLED)

    async def _transition_from_pending(self, task_id: str, status: TaskStatus) -> Task:
        normalized = _validate_task_id(task_id)
        async with self._lock_for(normalized):
            task = await self._require(normalized)
            if task.status is not TaskStatus.PENDING:
                raise ValueError(f"only pending task can be transitioned: {task_id} ({task.status.value})")
            now = datetime.now(UTC)
            data = task.model_dump(mode="python")
            data["status"] = status
            if status in _TERMINAL_STATUSES:
                data["completed_at"] = now
            data["revision"] = task.revision + 1
            data["updated_at"] = now
            updated = Task.model_validate(data)
            await self._write(updated)
            return updated

    async def active_for_conversation(self, conversation_id: str) -> Task | None:
        """会话最近更新的活动任务(ACTIVE/PAUSED;PENDING 不注入)。"""
        normalized = _normalize_required_entry(conversation_id, field_name="conversation_id")
        tasks = [t for t in await self._all_tasks()
                 if t.owner_conversation_id == normalized and t.status.value in _CONTEXT_TASK_STATUSES]
        tasks.sort(key=lambda t: t.updated_at, reverse=True)
        return tasks[0] if tasks else None

    async def latest_pending_for_conversation(self, conversation_id: str) -> Task | None:
        """会话最近的 PENDING 任务(otter 增补:Plan 采纳流程找计划用)。"""
        normalized = _normalize_required_entry(conversation_id, field_name="conversation_id")
        tasks = [t for t in await self._all_tasks()
                 if t.owner_conversation_id == normalized and t.status is TaskStatus.PENDING]
        tasks.sort(key=lambda t: t.updated_at, reverse=True)
        return tasks[0] if tasks else None

    async def _require(self, task_id: str) -> Task:
        task = await self.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在:{task_id}")
        return task

    async def _all_tasks(self) -> list[Task]:
        return await asyncio.to_thread(_scan_tasks, self.tasks_dir)

    async def _write(self, task: Task) -> None:
        path = self.tasks_dir / f"{task.id}.json"
        if await asyncio.to_thread(path.is_symlink):
            raise ValueError("task path cannot be a symbolic link")
        await asyncio.to_thread(_write_task, path, task)

    def _lock_for(self, task_id: str) -> asyncio.Lock:
        lock = self._locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[task_id] = lock
        return lock


# ── 上下文注入() ──

TASK_CONTEXT_HEADER = (
    "以下是当前会话绑定的活动任务状态。目标和用户约束应继续遵守;"
    "开始执行具体步骤前应将它更新为 in_progress;获得充分完成证据、发生真实阻塞、"
    "计划变化或任务状态变化后,立即调用 task_update 写回。不要因为单个工具成功就自动"
    "认定整个步骤完成;最终回答前核对本轮实际进展是否已写回。若快照折叠了旧完成步骤,"
    "可调用 task_get 查看。操作本快照对应的活动任务时,task_get/task_update 的 task_id "
    "优先使用 current。更新时优先携带 revision 作为 expected_revision。"
)


def _compact(value: str | None, max_chars: int) -> str | None:
    if value is None or len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}…"


def _compact_entries(values: tuple[str, ...], *, max_entries: int, max_chars: int) -> list[str | None]:
    return [_compact(v, max_chars) for v in values[-max_entries:]]


def _visible_steps(steps: tuple[TaskStep, ...], *, recent_done_steps: int, max_pending_steps: int):
    done = [s for s in steps if s.status is TaskStepStatus.DONE]
    retained_done_ids = {s.id for s in (done[-recent_done_steps:] if recent_done_steps else ())}
    pending = [s for s in steps if s.status is not TaskStepStatus.DONE]
    retained_pending = pending[:max_pending_steps]
    retained_ids = retained_done_ids | {s.id for s in retained_pending}
    visible = tuple(s for s in steps if s.id in retained_ids)
    return visible, len(done) - len(retained_done_ids), len(pending) - len(retained_pending)


def render_task_context(task: Task, *, recent_done_steps: int = 3, max_entry_chars: int = 500,
                        max_list_entries: int = 12, max_pending_steps: int = 12) -> str:
    """渲染预算受控的任务快照;Store 数据始终完整(预算口径)。"""
    visible, omitted_done, omitted_pending = _visible_steps(
        task.steps, recent_done_steps=recent_done_steps, max_pending_steps=max_pending_steps)
    payload = {
        "id": task.id,
        "revision": task.revision,
        "title": task.title,
        "goal": _compact(task.goal, max_entry_chars),
        "status": task.status.value,
        "priority": task.priority.value,
        "constraints": _compact_entries(task.constraints, max_entries=max_list_entries, max_chars=max_entry_chars),
        "state": _compact_entries(task.state, max_entries=max_list_entries, max_chars=max_entry_chars),
        "key_facts": _compact_entries(task.key_facts, max_entries=max_list_entries, max_chars=max_entry_chars),
        "omitted_entries": {
            "constraints": max(0, len(task.constraints) - max_list_entries),
            "state": max(0, len(task.state) - max_list_entries),
            "key_facts": max(0, len(task.key_facts) - max_list_entries),
        },
        "step_counts": {s.value: sum(st.status is s for st in task.steps) for s in TaskStepStatus},
        "omitted_done_steps": omitted_done,
        "omitted_pending_steps": omitted_pending,
        "steps": [
            {"id": s.id, "title": s.title, "status": s.status.value, "note": _compact(s.note, max_entry_chars)}
            for s in visible
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"{TASK_CONTEXT_HEADER}\n<active_task>{serialized}</active_task>"


async def pending_plan_is_valid(store: FileTaskStore, conversation_id: str | None, task_id: str) -> bool:
    """PENDING 计划有效性(Plan Mode 完成条件;标准语义:防伪造进度)。"""
    if not conversation_id or not task_id:
        return False
    task = await store.resolve(task_id, owner_conversation_id=conversation_id)
    if task is None or task.status is not TaskStatus.PENDING:
        return False
    if not task.goal or not task.steps:
        return False
    if any(s.status in {TaskStepStatus.DONE, TaskStepStatus.IN_PROGRESS} for s in task.steps):
        return False
    return True


# ── 4 工具() ──

def _task_full_text(task: Task) -> str:
    return json.dumps(task.model_dump(mode="json"), ensure_ascii=False)


def _task_brief(task: Task) -> dict[str, Any]:
    return {"id": task.id, "title": task.title, "status": task.status.value,
            "priority": task.priority.value, "goal": task.goal,
            "progress": task.progress_summary, "updated_at": task.updated_at.isoformat()}


def _require_conversation_id(ctx: dict | None) -> str:
    if not ctx or not ctx.get("conversation_id"):
        raise ValueError("task tool requires conversation context")
    return str(ctx["conversation_id"])


async def _resolve_owned(store: FileTaskStore, task_id: str, conversation_id: str) -> Task:
    """先按会话归属过滤再解析 ID/前缀;不存在统一报错(防跨会话探测)。"""
    normalized = task_id.strip()
    if normalized.lower() == "current":
        task = await store.active_for_conversation(conversation_id)
    else:
        task = await store.resolve(normalized, owner_conversation_id=conversation_id)
    if task is None:
        raise KeyError(f"任务不存在:{task_id}")
    return task


def _build_steps(raw_steps: object) -> tuple[TaskStep, ...]:
    if raw_steps is None:
        return ()
    if not isinstance(raw_steps, list):
        raise ValueError("'steps' must be a list")
    steps: list[TaskStep] = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise ValueError(f"steps[{index}] must be an object")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"steps[{index}].title must be a non-empty string")
        note = item.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError(f"steps[{index}].note must be a string")
        steps.append(TaskStep(id=uuid4().hex, title=title, note=note))
    return tuple(steps)


def _build_update_steps(raw_steps: object) -> tuple[TaskStep, ...]:
    """解析重排后的完整计划:保留已有 ID,新增步骤生成 ID(标准语义)。"""
    if not isinstance(raw_steps, list):
        raise ValueError("'steps' must be a list")
    steps: list[TaskStep] = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise ValueError(f"steps[{index}] must be an object")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"steps[{index}].title must be a non-empty string")
        step_id = item.get("id") or uuid4().hex
        note = item.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError(f"steps[{index}].note must be a string")
        raw_status = item.get("status", TaskStepStatus.TODO.value)
        steps.append(TaskStep(id=step_id, title=title,
                              status=TaskStepStatus(raw_status), note=note))
    return tuple(steps)


_TASK_CREATE_PARAMS = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "任务标题,简洁描述要完成的工作。"},
        "description": {"type": "string", "description": "可选的任务详细描述。"},
        "goal": {"type": "string", "description": "可选的当前目标,说明任务要达成的结果。"},
        "priority": {"type": "string", "enum": [p.value for p in TaskPriority],
                     "description": "可选优先级,默认 normal。"},
        "steps": {
            "type": "array",
            "description": "可选的任务步骤拆解。",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string", "description": "步骤标题。"},
                               "note": {"type": "string", "description": "可选的步骤说明。"}},
                "required": ["title"],
            },
        },
    },
    "required": ["title"],
}


class TaskCreateTool(Tool):
    """创建用于长期跟踪进度的任务(文案 )。"""

    name = "task_create"
    description = (
        "创建一个任务用于长期跟踪一个整体目标。当工作复杂需要拆解、跨多轮跟踪,或用户"
        "明确要求记录任务时调用。同一整体目标中的阶段、模块和动作应放入本任务的 steps,"
        "不要分别创建多个任务;只有彼此独立、可分别完成和关闭的目标才创建多个任务。"
        "任务状态独立于对话保存,不会因上下文压缩而丢失。"
    )
    parameters = _TASK_CREATE_PARAMS

    def __init__(self, store: FileTaskStore, ctx: dict) -> None:
        self._store = store
        self.ctx = ctx  # 装配方持有的共享上下文(conversation_id/mode/run_id)

    async def run(self, args: dict[str, Any]) -> str:
        try:
            conversation_id = _require_conversation_id(self.ctx)
        except ValueError as exc:
            return f"[otter] 错误:{exc}"  # otter 工具协议:错误以文本返回(2026-09-24)
        title = args.get("title")
        if not isinstance(title, str) or not title.strip():
            return "[otter] 错误:'title' 必须为非空字符串"
        priority = TaskPriority(args.get("priority", TaskPriority.NORMAL.value))
        steps = _build_steps(args.get("steps"))
        task = await self._store.create(
            title=title, description=args.get("description"), goal=args.get("goal"),
            priority=priority, steps=steps, owner_conversation_id=conversation_id,
            run_ids=((str(self.ctx.get("run_id", "")),) if self.ctx.get("run_id") else ()),
        )
        return _task_full_text(task)


class TaskUpdateTool(Tool):
    """更新任务:推进步骤、状态、补充约束/事实(含 Plan 模式限制)。"""

    name = "task_update"
    description = (
        "更新一个已有任务。当某个步骤完成、任务状态变化、需要补充用户约束或关键事实时"
        "调用。当前会话和运行由系统自动关联;至少提供 task_id 与一个更新字段。更新系统"
        "注入的当前活动任务时,优先把 task_id 设为 current,避免转录长 ID。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID、唯一前缀,或当前活动任务句柄 current。"},
            "status": {"type": "string", "enum": [s.value for s in TaskStatus],
                       "description": "新的任务状态;步骤 blocked 等待用户/外部条件时可置 paused。"},
            "goal": {"type": "string", "description": "替换任务的当前目标。"},
            "state": {"type": "array", "items": {"type": "string"}, "description": "替换为最新的当前状态事实。"},
            "constraints": {"type": "array", "items": {"type": "string"}, "description": "追加的用户约束(去重)。"},
            "facts": {"type": "array", "items": {"type": "string"}, "description": "追加的关键事实或决策(去重)。"},
            "steps": {
                "type": "array",
                "description": "可选的新步骤计划;提供时整体替换当前步骤。已有步骤应保留原 id。",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                                   "status": {"type": "string", "enum": [s.value for s in TaskStepStatus]},
                                   "note": {"type": "string"}},
                    "required": ["title"],
                },
            },
            "step_id": {"type": "string", "description": "要推进的步骤 ID;与 step_status 配合。"},
            "step_status": {"type": "string", "enum": [s.value for s in TaskStepStatus],
                            "description": "步骤新状态;done/blocked 时必须提供 step_note。"},
            "step_note": {"type": "string", "description": "步骤备注;done 记录完成依据,blocked 说明阻塞原因。"},
            "expected_revision": {"type": "integer", "description": "可选期望版本;不一致时拒绝覆盖(乐观锁)。"},
        },
        "required": ["task_id"],
    }

    def __init__(self, store: FileTaskStore, ctx: dict) -> None:
        self._store = store
        self.ctx = ctx

    async def run(self, args: dict[str, Any]) -> str:
        task_id = args.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return "[otter] 错误:'task_id' 必须为非空字符串"
        update_keys = ("status", "goal", "state", "constraints", "facts", "steps", "step_id", "step_status")
        if not any(k in args for k in update_keys):
            return "[otter] 错误:task_update 需要至少一个更新字段(除 task_id 外)"

        # Plan 模式限制(标准语义:计划期不改状态/不推进步骤——执行是采纳后的事)
        if str(self.ctx.get("mode", "")) == "plan":
            if "status" in args:
                return "[otter] 错误:PLAN 模式下不能改变任务状态;任务由用户接受后才开始"
            if args.get("step_id") is not None or args.get("step_status") is not None:
                return "[otter] 错误:PLAN 模式下不能推进步骤状态;只允许更新计划内容"

        step_id = args.get("step_id")
        step_status = args.get("step_status")
        if (step_id is None) != (step_status is None):
            return "[otter] 错误:'step_id' 与 'step_status' 必须成对提供"
        if "steps" in args and (step_id is not None or "step_note" in args):
            return "[otter] 错误:'steps' 不能与 step_id/step_status/step_note 组合"
        step_note = args.get("step_note")
        if step_status in ("done", "blocked") and (not isinstance(step_note, str) or not step_note.strip()):
            return (f"[otter] 错误:步骤标记为 {step_status} 时必须提供 step_note"
                    + ("说明完成依据" if step_status == "done" else "说明阻塞原因"))
        expected_revision = args.get("expected_revision")
        if expected_revision is not None and (not isinstance(expected_revision, int)
                                              or isinstance(expected_revision, bool) or expected_revision < 1):
            return "[otter] 错误:'expected_revision' 必须为正整数"

        try:
            conversation_id = _require_conversation_id(self.ctx)
        except ValueError as exc:
            return f"[otter] 错误:{exc}"  # otter 工具协议:错误以文本返回(2026-09-24)
        try:
            task = await _resolve_owned(self._store, task_id, conversation_id)
        except KeyError as exc:
            return f"[otter] 错误:{exc}"

        patch_data: dict[str, Any] = {
            "status": TaskStatus(args["status"]) if "status" in args else None,
            "add_constraints": tuple(args.get("constraints") or ()),
            "add_key_facts": tuple(args.get("facts") or ()),
            "step_id": step_id,
            "step_status": TaskStepStatus(step_status) if step_status is not None else None,
            "expected_revision": expected_revision,
            "run_id": str(self.ctx.get("run_id", "")) or None,
        }
        if "goal" in args:
            patch_data["goal"] = args.get("goal")
        if "state" in args:
            patch_data["state"] = tuple(args.get("state") or ())
        if "steps" in args:
            patch_data["replace_steps"] = _build_update_steps(args["steps"])
        if "step_note" in args:
            patch_data["step_note"] = step_note

        try:
            updated = await self._store.apply_patch(
                task.id, TaskPatch.model_validate(patch_data), owner_conversation_id=conversation_id)
        except (ValueError, KeyError) as exc:
            return f"[otter] 错误:更新被拒:{exc}"
        return _task_full_text(updated)


class TaskGetTool(Tool):
    """获取单个任务完整详情(task_id 支持 current)。"""

    name = "task_get"
    description = ("获取一个任务的完整详情(目标、状态、步骤、约束、关键事实与关联记录)。"
                   "需要重新确认当前活动任务时优先用 current;其他任务用精确 ID 或唯一前缀。")
    parameters = {
        "type": "object",
        "properties": {"task_id": {"type": "string",
                                   "description": "任务 ID、唯一前缀,或当前活动任务句柄 current。"}},
        "required": ["task_id"],
    }

    def __init__(self, store: FileTaskStore, ctx: dict) -> None:
        self._store = store
        self.ctx = ctx

    async def run(self, args: dict[str, Any]) -> str:
        task_id = args.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return "[otter] 错误:'task_id' 必须为非空字符串"
        try:
            conversation_id = _require_conversation_id(self.ctx)
        except ValueError as exc:
            return f"[otter] 错误:{exc}"  # otter 工具协议:错误以文本返回(2026-09-24)
        try:
            task = await _resolve_owned(self._store, task_id, conversation_id)
        except KeyError as exc:
            return f"[otter] 错误:{exc}"
        return _task_full_text(task)


class TaskListTool(Tool):
    """列出任务,可按状态过滤。"""

    name = "task_list"
    description = "列出任务(可按状态过滤)。需要总览当前有哪些任务、查看进度,或用户明确要求列出时调用。"
    parameters = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": [s.value for s in TaskStatus], "description": "可选的状态过滤。"},
            "limit": {"type": "integer", "description": f"返回上限,默认 50,最大 {_MAX_LIST_LIMIT}。"},
        },
    }

    def __init__(self, store: FileTaskStore, ctx: dict) -> None:
        self._store = store
        self.ctx = ctx

    async def run(self, args: dict[str, Any]) -> str:
        limit = args.get("limit", 50)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIST_LIMIT:
            return f"[otter] 错误:'limit' 必须为 1~{_MAX_LIST_LIMIT} 的整数"
        status = TaskStatus(args["status"]) if args.get("status") else None
        try:
            conversation_id = _require_conversation_id(self.ctx)
        except ValueError as exc:
            return f"[otter] 错误:{exc}"  # otter 工具协议:错误以文本返回(2026-09-24)
        tasks = await self._store.list(limit=limit, status=status, owner_conversation_id=conversation_id)
        return json.dumps({"count": len(tasks), "tasks": [_task_brief(t) for t in tasks]},
                          ensure_ascii=False)


def build_task_tools(store: FileTaskStore | None = None, ctx: dict | None = None) -> list[Tool]:
    """装配 4 个任务工具(与 register_task_tools 同角色;otter 返回列表由装配方注册)。"""
    store = store or FileTaskStore()
    ctx = ctx if ctx is not None else {}
    return [TaskCreateTool(store, ctx), TaskUpdateTool(store, ctx),
            TaskGetTool(store, ctx), TaskListTool(store, ctx)]


__all__ = [
    "FileTaskStore", "Task", "TaskPatch", "TaskStep", "TaskStatus", "TaskStepStatus",
    "TaskPriority", "render_task_context", "pending_plan_is_valid", "build_task_tools",
]
