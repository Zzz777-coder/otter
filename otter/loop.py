"""Agent Loop——otter 的心脏(M1 版)。

设计参考 vesta app/agent/loop.py 的循环组织思想,全部重新实现:
每个 Step 五拍:① 停止/防御评估 → ② 请求组装(system+AGENTS.md+摘要+尾部历史) →
③ 压缩(估算超线 → 滚动摘要,作用于请求视图,原始历史不动) → ④ 流式调模型 →
⑤ 回复分岔(工具轮 / 最终答案)。

M1 新增(相对 M0):
- 滚动摘要压缩(tiktoken 事前估算 + 结构化摘要 + 水位跨 Run);
- Plan/Act 双模式:Plan=只读白名单(结构上不含写工具,说明书 5.5);
- write/edit 成功后 git 自动提交([otter] 前缀),支撑 /undo;
- system prompt:身份 + AGENTS.md 分层指令 + <conversation_summary> 注入。
max_steps 用尽时进入"收尾专用步":零工具表发最后一次请求,从结构上杜绝收尾时再调工具。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from otter.context.estimator import estimate_messages_tokens, estimate_tokens
from otter.context.summarizer import ConversationSummary, RollingSummarizer
from otter.models.types import Message, ModelResponse, ModelUsage
from otter.models.openai_compat import TextDeltaCallback
from otter.prompts import (  # 2026-09-23 提示词套件重构:全部话术单一来源 otter/prompts.py
    BASE_SYSTEM,
    EDIT_HINT_DIFF,
    EDIT_HINT_WHOLE,
    MAX_STEPS_FINAL_MESSAGE,
    approved_plan_note,
    budget_finalizing,
    budget_hard_report,
    budget_warning,
)
from otter.tools.base import ToolRegistry
from otter.store import Store

# 停止原因三族(设计参考 vesta 的停止出口分类:正常 / 预算 / 兜底;预算三段收口是 M2)
STOP_FINAL = "final_answer"        # 正常:模型给出纯文本最终答案
STOP_MAX_STEPS = "max_steps"       # 正常收尾:步数用尽,经收尾专用步总结
STOP_REPEATED = "repeated_tool"    # 兜底:同签名工具调用累计 3 次,防弱模型原地打转
STOP_ERROR = "model_error"         # 兜底:模型调用异常
STOP_BUDGET = "run_budget_exceeded"  # 预算:Run 级费用硬停(2026-09-22 补装,防 45 万 token 级事故)

MODE_NORMAL = "normal"
MODE_PLAN = "plan"
# 修正(2026-09-22):Plan 白名单去掉 bash——bash 可执行写操作,保留它违反
# "结构上只读"原则(说明书 5.5);检索能力由 read_file/grep/glob 完整覆盖
# 修正(2026-09-22 M3):repo_map 与 tool_search 是只读/元工具,应进 Plan 白名单
PLAN_TOOLS = {"read_file", "grep", "glob", "repo_map", "tool_search"}
WRITE_TOOLS = {"write_file", "edit_file"}            # 成功后触发 git 自动提交

# 2026-09-23 提示词套件重构:BASE_SYSTEM 移至 otter/prompts.py(vesta 五段式重写,
# 融合 Fable 5 决策示例/Scaling/正反例/反幻觉技术);此处仅引用,单一来源调词

_REPEAT_LIMIT = 3

# 2026-09-23 用户要求:bash 中检测到文件修改操作也弹确认窗(与 write_file 同一闸门)
_BASH_WRITE_CMDS = re.compile(
    r"(?:^|[\s;])(?:touch|rm|mv|cp|mkdir|rmdir|tee|truncate|dd|chmod|chown|ln|shred|install|patch|split)\s"
    r"|sed\s+\S*\s*-i"
    r"|pip\s+install|npm\s+install"
    r"|>(?!&)\s*\S",   # > 文件重定向(排除 >& 合并输出;2026-09-23 用户反馈弹两次窗)
    re.MULTILINE
)


def _bash_writes_files(command: str) -> bool:
    """检测 bash 命令是否包含文件修改操作(宁可误报弹窗也不漏报)。"""
    if not command:
        return False
    return bool(_BASH_WRITE_CMDS.search(command))


@dataclass
class SummaryState:
    """滚动摘要的跨 Run 状态(由 REPL/-p 持有并反复传入同一引用)。"""

    summary: ConversationSummary | None = None
    covered: int = 0        # messages 前多少条已被摘要覆盖(水位)
    compressions: int = 0   # 本会话累计压缩次数


@dataclass
class AgentResult:
    ok: bool
    final_text: str
    stop_reason: str
    steps: int
    model_calls: int
    tool_calls: int
    usage: ModelUsage = field(default_factory=ModelUsage)

    def summary(self) -> str:
        tokens = "?"
        if self.usage.input_tokens is not None:
            tokens = f"{self.usage.input_tokens}in/{self.usage.output_tokens}out"
        return (
            f"停止原因={self.stop_reason} 步数={self.steps} 模型调用={self.model_calls} "
            f"工具调用={self.tool_calls} tokens={tokens}"
        )


class AgentLoop:
    def __init__(
        self,
        adapter,
        registry: ToolRegistry,
        store: Store,
        on_event: Callable[[str, dict], Awaitable[None]] | None = None,
        *,
        summarizer: RollingSummarizer | None = None,
        context_budget: int = 50_000,
        trigger_ratio: float = 0.8,
        keep_recent: int = 10,
        git_auto: bool = True,
        instructions: str = "",
        approval_gate=None,   # M2:权限审批门(ConsoleApprovalGate/WebApprovalGate;None=不拦)
        preview_gate=None,    # M3.5:diff 预览闸(None=自动通过,CLI 打印即写;GUI 弹卡片)
        sandbox=None,         # M2:沙箱后端(SeatbeltSandbox/NullSandbox;None=不包装)
        memory_bundle=None,   # M3:(store, core, ctx) 三元组;None=记忆关闭
        edit_format: str = "auto",  # M3:whole/diff/auto(按模型自适应弱模型整文件替换)
        activated_tools: set | None = None,  # M3:与 ToolSearchTool 共享的激活名单(同一对象!)
        run_budget: int = 300_000,  # Run 级费用预算(计费 token;0=禁用)——设计参考 vesta 三段,2026-09-22 补装
        base_system: str | None = None,  # 2026-09-23 套件化:子代理等可覆盖系统提示(修 SUBAGENT_SYSTEM 死代码)
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.store = store
        self.on_event = on_event
        self.summarizer = summarizer
        self.context_budget = context_budget
        self.trigger_ratio = trigger_ratio
        self.keep_recent = keep_recent
        self.git_auto = git_auto
        self.instructions = instructions
        self.approval_gate = approval_gate
        self.sandbox = sandbox
        self.memory_bundle = memory_bundle
        # 修正(2026-09-22 M3):激活名单必须与装配层 ToolSearchTool 共享同一对象——
        # 此前 loop 自建空 set,tool_search 激活写到另一个集合,永不到位(真实验收暴露,
        # 模型自己诊断出了"两把名单")
        self._activated_tools: set[str] = activated_tools if activated_tools is not None else set()
        self._recall_text = ""                    # M3:本 Run 的确定性召回快照
        self._sandbox_checked = False
        self._sandbox_ok, self._sandbox_msg = True, ""
        self.preview_gate = preview_gate
        self.run_budget = run_budget
        self.base_system = base_system  # 2026-09-23 套件化:None=用 BASE_SYSTEM(主循环默认)
        self._budget_hint = ""  # 每 Run 重置;注入请求视图 system 尾部(不污染消息序列)
        # M3 分层 edit format:auto=按模型启发(说明书:弱模型整文件替换更稳)
        model_name = (getattr(adapter, "model", "") or "").lower()
        if edit_format == "auto":
            edit_format = "whole" if any(k in model_name for k in ("deepseek", "qwen", "glm")) else "diff"
        self.edit_format = edit_format

    @property
    def _edit_hint(self) -> str:
        """edit format 分层提示:弱模型引导走 write_file(整文件),强模型走 edit_file(局部)。
        2026-09-23 套件化:文案移至 prompts.py(EDIT_HINT_*)。"""
        return EDIT_HINT_WHOLE if self.edit_format == "whole" else EDIT_HINT_DIFF

    async def _emit(self, run_id: int, type_: str, payload: dict) -> None:
        await self.store.append_event(run_id, type_, payload)
        # 修正(2026-09-22):run 级 on_event 优先——此前 REPL/GUI 定义了渲染回调却从未接入
        # (构造与 run 都没传),导致事件只落库不上屏,M0 起就存在的装配盲点
        callback = getattr(self, "_run_on_event", None) or self.on_event
        if callback:
            await callback(type_, payload)

    def _definitions_for(self, mode: str):
        """Plan 模式 = 结构上只暴露只读工具;deferred 工具仅激活后可见(M3)。"""
        defs = self.registry.definitions(active_extra=self._activated_tools)
        if mode == MODE_PLAN:
            defs = [d for d in defs if d.name in PLAN_TOOLS]
        return defs

    async def _maybe_compress(self, messages: list[Message], state: SummaryState) -> None:
        """第③拍:估算超线则压缩最旧前缀进摘要(原始历史不动,只更新水位)。"""
        if self.summarizer is None:
            return
        tail = messages[state.covered:]
        estimated = estimate_messages_tokens(tail) + estimate_tokens(self.instructions)
        trigger = int(self.context_budget * self.trigger_ratio)
        if estimated < trigger or len(tail) <= self.keep_recent + 2:
            return
        # 修正(2026-09-22):水位落点必须避开 assistant(tool_calls)→tool 消息对——
        # 视图若以孤立 tool 消息开头,API 直接 400(实测 DeepSeek:
        # "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'")
        cut = len(messages) - self.keep_recent
        while cut < len(messages) and (
            messages[cut].role == "tool" or getattr(messages[cut], "tool_calls", None)
        ):
            cut += 1  # 向前推进到 user 或纯文本 assistant 为止
        if cut <= state.covered or cut >= len(messages) - 1:
            return
        compressible = messages[state.covered:cut]
        summary, usage = await self.summarizer.summarize(compressible, state.summary)
        # 压缩调用本身记账(设计参考 vesta"压缩不免费"的结论):由调用方统一累加
        self._last_compress_usage = usage
        if summary is None:
            return  # 校验失败/摘要不够短:放弃本次,下次触发线再试
        state.summary = summary
        state.covered = cut
        state.compressions += 1

    def _build_view(self, messages: list[Message], state: SummaryState) -> list[Message]:
        """请求视图 = system(身份+编辑策略+AGENTS.md+Core记忆+召回+摘要+计划上下文) + 未压缩尾部。
        2026-09-23 套件化:系统提示可被 base_system 覆盖(子代理),采纳计划文案移至 prompts.py。"""
        parts = [(self.base_system or BASE_SYSTEM) + self._edit_hint]
        # Plan Mode v2(2026-09-23):产品经理指令(结构化计划契约)+ 采纳计划注入
        if getattr(self, "_plan_mode_directive", False):
            from otter.plans import PLAN_DIRECTIVE

            parts[0] += PLAN_DIRECTIVE
        if getattr(self, "_plan_context", ""):
            parts.append(approved_plan_note(self._plan_context))
        if self.instructions:
            parts.append(f"<project_instructions>\n{self.instructions}\n</project_instructions>")
        if self.memory_bundle:
            _, core, _ = self.memory_bundle
            core_text = core.render()
            if core_text:
                parts.append(core_text)
            if self._recall_text:
                parts.append(self._recall_text)
        if state.summary:
            parts.append(state.summary.render())
        if self._budget_hint:
            parts.append(self._budget_hint)  # 预算话术随视图注入(不污染消息序列)
        view = [Message(role="system", content="\n\n".join(parts))]
        view.extend(messages[state.covered:])
        return view

    async def run(
        self,
        history: list[Message],
        user_message: Message,
        run_id: int,
        max_steps: int = 30,
        on_text_delta: TextDeltaCallback | None = None,
        mode: str = MODE_NORMAL,
        summary_state: SummaryState | None = None,
        on_event: Callable[[str, dict], Awaitable[None]] | None = None,
        conversation_id: int | None = None,  # v5(2026-09-22):GUI 多会话落库用;REPL 不传=行为不变
        plan_context: str = "",  # Plan Mode v2(2026-09-23):采纳的计划注入 system(执行模式跑)
    ) -> AgentResult:
        messages = list(history) + [user_message]
        self._run_on_event = on_event  # run 级渲染回调(见 _emit 修正说明)
        # Plan Mode v2:PLAN 模式注入结构化计划指令;plan_context 供采纳后执行跑
        self._plan_mode_directive = mode == MODE_PLAN
        self._plan_context = plan_context
        seq = len(history)
        await self.store.append_message(user_message, seq, run_id, conversation_id)
        seq += 1
        state = summary_state or SummaryState()

        # M3 确定性召回:纯规则拼查询(当前消息+近期用户消息+摘要目标),每 Run 一次快照;
        # 注入的只是 cue,不写历史、不加 access_count(vesta 的召回纪律)
        self._recall_text = ""
        recalled = False
        if self.memory_bundle:
            from otter.memory import recall_query

            mem_store, _, _ = self.memory_bundle
            recent_users = [m.content or "" for m in history if m.role == "user"][-3:]
            objective = state.summary.current_objective if state.summary else ""
            q = recall_query(user_message.content or "", recent_users, objective)
            hits = mem_store.search(q) if q else []
            if hits:
                recalled = True
                cues = "\n".join(f"- {e.mid} {e.title}:{e.summary}" for e in hits[:5])
                self._recall_text = f"<memory_recall>\n可能有用的长期记忆(用 memory 系工具查全文):\n{cues}\n</memory_recall>"

        # 用量累加(含压缩调用):初始 None=尚未记账,与"厂商未返回"区分(M0 修正沿用)
        usage: ModelUsage | None = None

        def _acc(u: ModelUsage | None) -> None:
            nonlocal usage
            if u is None:
                return
            if usage is None:
                usage = ModelUsage(u.input_tokens, u.output_tokens)
            else:
                usage.add(u)

        model_calls = 0
        total_tool_calls = 0
        signature_counts: Counter[tuple[str, str]] = Counter()

        # ── Run 级费用预算三段(2026-09-22 补装,设计参考 vesta app/agent/budget.py)──
        # 口径:真实 usage 的 input+output 合计(事后问责口径);三段一次性熔断 flag。
        # 修正(2026-09-23 套件化):local 标志改名 warned/finalizing——原变量名
        # budget_finalizing 与 prompts.py 导入的同名函数冲突(bool 不可调用,
        # 与 2026-09-22 state 解包覆盖同类阴影 bug,此处在脑内静态检查时抓住)
        self._budget_hint = ""
        warned = finalizing = False

        def _used() -> int:
            return ((usage.input_tokens or 0) + (usage.output_tokens or 0)) if usage else 0

        def _check_budget(step_now: int) -> tuple[str, bool] | None:
            """返回 (段位, 是否补发warning);'hard' 表示应立即终止。
            跨段(如一次调用从 60% 以下直接跳到 85%+)时先补发 warning——
            对齐 vesta 的"预警补发"先例(2026-09-22,离线测试暴露 elif 跳段)。"""
            nonlocal warned, finalizing
            if not self.run_budget:
                return None
            u, hard = _used(), self.run_budget
            also_warning = False
            if u >= hard:
                if not warned:
                    warned = True
                    also_warning = True
                return ("hard", also_warning)
            if u >= int(hard * 0.85) and not finalizing:
                finalizing = True
                if not warned:
                    warned = True
                    also_warning = True
                # 2026-09-23 套件化:话术移至 prompts.py(文案不变)
                self._budget_hint = budget_finalizing(u, hard)
                return ("finalizing", also_warning)
            if u >= int(hard * 0.6) and not warned:
                warned = True
                self._budget_hint = budget_warning(u, hard)
                return ("warning", False)
            return None


        for step in range(1, max_steps + 1):
            # M2 Checkpoint:每 Step 调模型前落档(MODEL_REQUEST 阶段)——中断恢复的边界
            await self.store.save_checkpoint(
                run_id, user_message.content or "", "MODEL_REQUEST"
            )
            # ③ 压缩:估算 → 超线滚动摘要(第③拍;M0 只有工具侧截断)
            # 修正(2026-09-22):只在压缩真正成功(次数增加)时才发事件——
            # 此前失败尝试也会 emit,界面出现"第 0 次"的误导
            self._last_compress_usage = None
            _compressions_before = state.compressions
            await self._maybe_compress(messages, state)
            _acc(getattr(self, "_last_compress_usage", None))
            if state.compressions > _compressions_before:
                await self._emit(
                    run_id, "CONTEXT_COMPACTED",
                    {"covered": state.covered, "compressions": state.compressions},
                )

            # ② 请求组装(system+摘要+尾部) + ④ 流式调模型
            await self._emit(run_id, "MODEL_STARTED", {"step": step, "mode": mode})
            try:
                resp: ModelResponse = await self.adapter.complete_stream(
                    self._build_view(messages, state), self._definitions_for(mode), on_text_delta
                )
            except Exception as exc:
                await self._emit(run_id, "MODEL_ERROR", {"error": str(exc)[:500]})
                await self.store.finish_run(run_id, "failed", STOP_ERROR)
                return AgentResult(False, f"模型调用失败:{exc}", STOP_ERROR, step, model_calls, total_tool_calls, usage or ModelUsage())
            model_calls += 1
            _acc(resp.usage)

            assistant_msg = Message(role="assistant", content=resp.content, tool_calls=resp.tool_calls)
            messages.append(assistant_msg)
            await self.store.append_message(assistant_msg, seq, run_id, conversation_id)
            seq += 1
            await self._emit(
                run_id, "MODEL_COMPLETED",
                {
                    "step": step,
                    "content_chars": len(resp.content or ""),
                    "tool_calls": [t.name for t in resp.tool_calls],
                    "usage_in": resp.usage.input_tokens,
                    "usage_out": resp.usage.output_tokens,
                },
            )

            # 预算三段检查(每次真实记账后):warning 提醒 → finalization 逼收口 → 硬停
            # 修正(2026-09-22):硬停的 report/return 必须只在 hard 段内——此前缩进
            # 错位挂在 if 外层,finalizing 段也会直接终止(离线测试暴露)
            budget_state = _check_budget(step)
            if budget_state is not None:
                # 修正(2026-09-22):解包曾用变量名 state,覆盖了外层摘要状态对象,
                # 下一圈 state.compressions 直接 AttributeError(最小重现定位)
                b_stage, b_also_warning = budget_state
                if b_also_warning:
                    await self._emit(run_id, "RUN_BUDGET_WARNING", {"used": _used(), "budget": self.run_budget})
                if b_stage == "finalizing":
                    await self._emit(run_id, "RUN_BUDGET_FINALIZING", {"used": _used(), "budget": self.run_budget})
                elif b_stage == "hard":
                    await self._emit(run_id, "RUN_BUDGET_EXCEEDED", {"used": _used(), "budget": self.run_budget})
                    # 2026-09-23 套件化:话术移至 prompts.py(文案不变)
                    report = budget_hard_report(_used(), self.run_budget, total_tool_calls)
                    await self.store.finish_run(run_id, "failed", STOP_BUDGET)
                    return AgentResult(False, report, STOP_BUDGET, step, model_calls,
                                       total_tool_calls, usage or ModelUsage())

            # ⑤ 回复分岔
            if not resp.tool_calls:
                # Plan Mode v2:计划产物轻校验 + 落盘(不改写终稿——与 vesta 的差异点)
                if mode == MODE_PLAN:
                    from otter.plans import plan_is_valid, save_plan

                    valid = plan_is_valid(resp.content or "")
                    plan_path = save_plan(user_message.content or "", resp.content or "") if valid else None
                    await self._emit(run_id, "PLAN_RESULT", {
                        "valid": valid,
                        "plan_file": str(plan_path) if plan_path else "",
                    })
                # M3 Run 后反思:确定性门控先过滤,单动作+乐观锁,失败完全隔离
                reflect_note = ""
                if self.memory_bundle:
                    from otter.memory import reflection_should_run
                    from otter.memory_runtime import run_reflection

                    mem_store, core, mem_ctx = self.memory_bundle
                    mem_ctx["reads"] = {}  # 每 Run 重置读取记录(乐观锁的"本 Run"语义)
                    mem_ctx["run_id"] = run_id
                    if reflection_should_run(user_message.content or "", recalled):
                        reflect_note = await run_reflection(
                            self.adapter, user_message.content or "", resp.content or "",
                            mem_store, core, mem_ctx,
                        )
                        await self._emit(run_id, "MEMORY_REFLECTION", {"result": reflect_note})
                await self.store.save_checkpoint(run_id, user_message.content or "", "FINISHED")
                await self.store.finish_run(run_id, "completed", STOP_FINAL)
                final = resp.content or ""
                if reflect_note:
                    final += f"\n{reflect_note}"
                return AgentResult(True, final, STOP_FINAL, step, model_calls, total_tool_calls, usage or ModelUsage())

            # M2 Checkpoint:工具轮开始前落档(TOOL_EXECUTION 阶段,pending=本批调用)
            await self.store.save_checkpoint(
                run_id, user_message.content or "", "TOOL_EXECUTION",
                pending_tools=[
                    {"id": t.id, "name": t.name, "arguments": t.arguments} for t in resp.tool_calls
                ],
            )
            for tc in resp.tool_calls:
                total_tool_calls += 1
                await self._emit(run_id, "TOOL_STARTED", {"step": step, "name": tc.name, "arguments": tc.arguments})
                tool = self.registry.get(tc.name)
                # 2026-09-24 R5:写盘成功标志——工具结果文本都以 "[otter]" 开头(成功也是
                # "[otter] 已写入…"),前缀区分不了成败,改在真实执行点显式置位
                write_ok = False
                cur_diff = None  # R6:本迭代的文本 diff(make_pdf 无 diff,保持 None)
                if mode == MODE_PLAN and tc.name not in PLAN_TOOLS:
                    # Plan 只读硬校验(设计参考 vesta:白名单外调用在执行层拦截)
                    result_text = f"[otter] PLAN 模式为只读,禁止调用 {tc.name};请仅检索与分析,输出计划。"
                elif tool is not None and tool.deferred and tc.name not in self._activated_tools:
                    # M3 deferred 硬校验:未激活即调用 → 拒绝并引导(vesta 的执行层拦截语义)
                    result_text = (f"[otter] 工具 {tc.name} 未激活。请先调用 tool_search 搜索并激活它,"
                                   f"下一步即可使用。")
                elif tool is None:
                    result_text = f"[otter] 错误:不存在名为 {tc.name} 的工具,可用工具:{[t.name for t in self._definitions_for(mode)]}"
                elif self.approval_gate is not None and not await self.approval_gate.resolve(
                    tc.name, tc.arguments
                ):
                    # M2 权限第一层:审批拒绝(含规则 DENY 与未识别输入,fail-closed)
                    await self._emit(run_id, "TOOL_DENIED", {"step": step, "name": tc.name})
                    result_text = f"[otter] 用户未授权执行 {tc.name},本次调用已拒绝。请换一种不越权的方式,或向用户说明为何需要它。"
                else:
                    args = dict(tc.arguments)
                    # M3.5 diff 预览:写类工具落盘前生成 unified diff 并广播——
                    # REPL 打印、GUI 弹卡片采纳后写(说明书 M3.5;写盘由 preview_gate 决定)
                    if tc.name in WRITE_TOOLS:
                        from otter.diffpreview import preview_edit, preview_write

                        if tc.name == "write_file":
                            diff_text = preview_write(str(args.get("path", "")), str(args.get("content", "")))
                        else:
                            diff_text = preview_edit(str(args.get("path", "")),
                                                     str(args.get("old_str", "")), str(args.get("new_str", "")))
                        cur_diff = diff_text  # R6:记录本迭代 diff(FILE_CHANGED 的 diffstat 用)
                        await self._emit(run_id, "DIFF_PREVIEW", {
                            "step": step, "name": tc.name, "path": str(args.get("path", "")),
                            "diff": diff_text[:8000],
                        })
                        if self.preview_gate is not None and not await self.preview_gate(diff_text):
                            await self._emit(run_id, "TOOL_DENIED", {"step": step, "name": tc.name})
                            result_text = "[otter] 用户查看了 diff 并拒绝本次写入,文件未改动。"
                            tool = None  # 跳过执行
                    # 2026-09-23 用户要求:bash 里检测到文件修改操作也要弹确认窗
                    if tc.name == "bash" and tool is not None and self.preview_gate is not None:
                        cmd = str(args.get("command", ""))
                        if _bash_writes_files(cmd):
                            await self._emit(run_id, "DIFF_PREVIEW", {
                                "step": step, "name": "bash", "path": cmd[:100],
                                "diff": f"bash: {cmd[:500]}",
                            })
                            if not await self.preview_gate(f"bash: {cmd[:500]}"):
                                await self._emit(run_id, "TOOL_DENIED", {"step": step, "name": tc.name})
                                result_text = "[otter] 用户拒绝了此 bash 命令(涉及文件修改),未执行。"
                                tool = None
                    # M2 沙箱第三层:bash 经沙箱包装;平台无法强制即拒绝(fail-closed)
                    if tc.name == "bash" and self.sandbox is not None and tool is not None:
                        if not self._sandbox_checked:
                            self._sandbox_ok, self._sandbox_msg = self.sandbox.available()
                            self._sandbox_checked = True
                        if not self._sandbox_ok:
                            result_text = f"[otter] 沙箱不可用,按 fail-closed 拒绝执行:{self._sandbox_msg}"
                            tool = None
                        else:
                            args["command"] = self.sandbox.wrap(str(args.get("command", "")))
                    # 修正(2026-09-23):预览拒绝/沙箱不可用会把 tool 置 None——此前 try 块
                    # 仍然执行 tool.run(None.run)→ AttributeError 覆盖掉拒绝文案(真机暴露);
                    # 改为 tool 非 None 才执行,Evidence/git 只包真实执行
                    if tool is not None:
                        try:
                            result_text = await tool.run(args)
                            write_ok = True  # 2026-09-24 R5:真实执行且无异常(FILE_CHANGED 用)
                        except Exception as exc:
                            result_text = f"[otter] 错误:工具执行异常 {type(exc).__name__}: {exc}"
                        # M2 Evidence:长结果归档原文,截断文本附取回提示(压缩可逆)
                        from otter.evidence import maybe_archive

                        result_text = await maybe_archive(
                            self.store, run_id, tc.id, tc.name, result_text
                        )
                        # git 自动提交:写类工具成功后(仓库内)——支撑 /undo
                        if (
                            self.git_auto and tc.name in WRITE_TOOLS
                            and not result_text.startswith("[otter] 错误")
                        ):
                            from otter.gitops import auto_commit

                            commit_note = await auto_commit(
                                [str(tc.arguments.get("path", ""))], f"{tc.name}"
                            )
                            if commit_note:
                                result_text += f"\n{commit_note}"
                tool_msg = Message(role="tool", content=result_text, tool_call_id=tc.id, name=tc.name)
                messages.append(tool_msg)
                await self.store.append_message(tool_msg, seq, run_id, conversation_id)
                seq += 1
                await self._emit(
                    run_id, "TOOL_COMPLETED",
                    {"step": step, "name": tc.name, "result_chars": len(result_text)},
                )
                # 2026-09-23 artifact_publish → 广播含预览数据的产物事件
                if tc.name == "artifact_publish":
                    from pathlib import Path as _P

                    file_path = str(tc.arguments.get("path", ""))
                    name = _P(file_path).name
                    note = str(tc.arguments.get("note", ""))[:100]
                    # 尝试从注册的工具获取 preview 生成器(loop 无 gui 引用,
                    # 通过 artifact 工具实例上的回调注入;失败仅发基本字段)
                    preview = {}
                    art_tool = self.registry.get("artifact_publish")
                    if hasattr(art_tool, "preview_fn") and art_tool.preview_fn:
                        try:
                            preview = art_tool.preview_fn(file_path)
                        except Exception:
                            pass
                    await self._emit(run_id, "ARTIFACT", {
                        "path": file_path, "name": name, "note": note, **preview,
                    })
                # 2026-09-24 R5(用户要求):写类工具成功落盘 → 广播文件变更事件。前端渲染
                # 「📎 已修改 product.py (+2/-1)」链接行(文件名超链接,点击在 chat 内预览)。
                # 成功判定=write_ok(真实执行且无异常;预览拒绝/沙箱拒绝时 tool 为 None 不会置位);
                # diffstat 用本迭代 diff;R6 起 make_pdf 也纳入(二进制无文本 diff → plus/minus 置 None)
                if (tc.name in WRITE_TOOLS or tc.name == "make_pdf") and write_ok:
                    from pathlib import Path as _P

                    file_path = str(tc.arguments.get("path", ""))
                    if cur_diff is not None:
                        plus = sum(1 for ln in cur_diff.splitlines()
                                   if ln.startswith("+") and not ln.startswith("+++"))
                        minus = sum(1 for ln in cur_diff.splitlines()
                                    if ln.startswith("-") and not ln.startswith("---"))
                    else:
                        plus = minus = None  # make_pdf 等二进制产物:不带 diffstat
                    file_preview = {}
                    _art = self.registry.get("artifact_publish")
                    if hasattr(_art, "preview_fn") and _art.preview_fn:
                        try:
                            file_preview = _art.preview_fn(file_path)
                        except Exception:
                            pass  # 预览生成失败仅发基本字段,前端点击时可按需补拉
                    await self._emit(run_id, "FILE_CHANGED", {
                        "path": file_path, "name": _P(file_path).name,
                        "action": "已创建" if tc.name in ("write_file", "make_pdf") else "已修改",
                        "plus": plus, "minus": minus, **file_preview,
                    })

                signature = (tc.name, json.dumps(tc.arguments, ensure_ascii=False, sort_keys=True))
                signature_counts[signature] += 1
                if signature_counts[signature] >= _REPEAT_LIMIT:
                    await self._emit(run_id, "RUN_STOPPED", {"reason": STOP_REPEATED, "signature": signature[0]})
                    await self.store.save_checkpoint(run_id, user_message.content or "", "FINISHED")
                    await self.store.finish_run(run_id, "completed", STOP_REPEATED)
                    return AgentResult(
                        False,
                        f"检测到同一工具调用({tc.name})以相同参数连续执行 {_REPEAT_LIMIT} 次,已终止以防止循环。",
                        STOP_REPEATED, step, model_calls, total_tool_calls, usage or ModelUsage(),
                    )

        # 收尾专用步:零工具表,从结构上杜绝再调工具(设计参考 vesta 的 +1 收尾步)
        # 2026-09-23 套件化:文案移至 prompts.py(文案不变)
        await self._emit(run_id, "RUN_FINALIZING", {"step": max_steps})
        messages.append(Message(role="user", content=MAX_STEPS_FINAL_MESSAGE))
        try:
            resp = await self.adapter.complete_stream(self._build_view(messages, state), tools=None, on_text_delta=on_text_delta)
        except Exception as exc:
            await self.store.finish_run(run_id, "failed", STOP_ERROR)
            return AgentResult(False, f"收尾调用失败:{exc}", STOP_ERROR, max_steps, model_calls, total_tool_calls, usage or ModelUsage())
        model_calls += 1
        _acc(resp.usage)
        final_msg = Message(role="assistant", content=resp.content or "")
        messages.append(final_msg)
        await self.store.append_message(final_msg, seq, run_id, conversation_id)
        await self.store.save_checkpoint(run_id, user_message.content or "", "FINISHED")
        await self.store.finish_run(run_id, "completed", STOP_MAX_STEPS)
        return AgentResult(True, resp.content or "(收尾步无输出)", STOP_MAX_STEPS, max_steps, model_calls, total_tool_calls, usage or ModelUsage())
