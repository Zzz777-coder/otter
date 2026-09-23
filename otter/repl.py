"""REPL 与渲染(M1 版):流式打屏 + 工具调用实时展示 + 斜杠命令。

设计取向(说明书第 6 章):CLI 不需要进程间 RPC(刻意排除 vesta 的 Desktop/WS 层),
事件流直接经回调进终端渲染。
M1 新增命令:/init 生成 AGENTS.md · /undo 回滚 git · /plan /act 只读/执行模式 ·
/context 查看压缩与预算状态。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Console

from otter.agents_md import init_agents_md, load_instructions
from otter.context.estimator import estimate_messages_tokens
from otter.loop import MODE_NORMAL, MODE_PLAN, AgentLoop, AgentResult, SummaryState
from otter.models.types import Message

console = Console()

BANNER = """[bold cyan]otter[/] — 轻量 CLI 编码 Agent (M1)
命令:/init 生成 AGENTS.md · /undo 回滚上组修改 · /plan /act 切只读/执行 · /context 看压缩状态 · /reset 清空 · exit 退出"""


def _render_event(type_: str, payload: dict) -> None:
    """工具/模型事件的轻量渲染:让用户看得见 agent 在干什么。"""
    if type_ == "TOOL_STARTED":
        args_preview = str(payload.get("arguments", {}))
        if len(args_preview) > 160:
            args_preview = args_preview[:160] + "…"
        mode = " [magenta](PLAN)[/]" if payload.get("mode") == MODE_PLAN else ""
        console.print(f"\n[dim]⚡ step{payload.get('step')}{mode} [/][bold yellow]{payload.get('name')}[/][dim] {args_preview}[/]")
    elif type_ == "TOOL_COMPLETED":
        console.print(f"[dim]   ↳ 结果 {payload.get('result_chars', 0)} 字符[/]")
    elif type_ == "MODEL_STARTED":
        console.print(f"[dim]—— step {payload.get('step')} 思考中 ——[/]")
    elif type_ == "RUN_FINALIZING":
        console.print("[bold red]! 达到最大步数,进入收尾总结[/]")
    elif type_ == "PLAN_RESULT":
        # Plan Mode v2:计划校验结果提示
        if payload.get("valid"):
            console.print(f"[bold cyan]📋 计划已存盘:{payload.get('plan_file','')}[/]")
        else:
            console.print("[yellow]⚠ 计划缺少'## 步骤'小节(格式不完整)[/]")
    elif type_ == "DIFF_PREVIEW":
        # M3.5:写操作落盘前的变更预览(CLI 信任 git+/undo,自动写;先给人看)
        console.print(f"\n[bold blue]📝 变更预览 {payload.get('name')} → {payload.get('path')}[/]")
        for dl in str(payload.get("diff", "")).splitlines()[:60]:
            if dl.startswith("+") and not dl.startswith("+++"):
                console.print(f"[green]{dl}[/]")
            elif dl.startswith("-") and not dl.startswith("---"):
                console.print(f"[red]{dl}[/]")
            elif dl.startswith("@@"):
                console.print(f"[cyan]{dl}[/]")
            else:
                console.print(f"[dim]{dl}[/]")
    elif type_ == "RUN_BUDGET_WARNING":
        console.print(f"[yellow]💰 预算提醒:{payload.get('used')}/{payload.get('budget')} 计费 token[/]")
    elif type_ == "RUN_BUDGET_FINALIZING":
        console.print("[bold yellow]💰 预算收口:即将耗尽,已注入收口指令[/]")
    elif type_ == "RUN_BUDGET_EXCEEDED":
        console.print(f"[bold red]💰 预算硬停:{payload.get('used')}/{payload.get('budget')}[/]")
    elif type_ == "CONTEXT_COMPACTED":
        console.print(
            f"[bold blue]⇩ 上下文已压缩(第 {payload.get('compressions')} 次,已覆盖 {payload.get('covered')} 条历史进摘要)[/]"
        )


async def _dispatch(loop: AgentLoop, store, history: list[Message], state: SummaryState,
                    prompt: str, max_steps: int, mode: str, plan_context: str = "") -> AgentResult | None:
    """一次用户任务的公共执行路径(单发/REPL 共用)。"""
    run_id = await store.new_run()

    async def on_event(type_: str, payload: dict) -> None:
        _render_event(type_, payload)

    try:
        result = await loop.run(
            history, Message(role="user", content=prompt), run_id, max_steps,
            on_text_delta=lambda s: print(s, end="", flush=True),
            mode=mode, summary_state=state,
            on_event=on_event,  # 修正(2026-09-22):接入渲染回调(M0 起漏接)
            plan_context=plan_context,  # Plan Mode v2:采纳计划注入
        )
        # M4(2026-09-22):Run 成功后过 Skill 提炼(watermark 去重+候选落盘;
        # 失败静默不影响主结果——vesta 的隔离取向)
        if result is not None and result.ok and loop.adapter is not None:
            try:
                from otter.skills import maybe_distill

                note = await maybe_distill(loop.adapter, run_id, prompt, result.final_text)
                if note:
                    console.print(f"[magenta]{note}[/]")
            except Exception:
                pass
        return result
    except KeyboardInterrupt:
        console.print("\n[bold red]已中断(M2 起有 Checkpoint 恢复,见说明书路线图)[/]")
        await store.finish_run(run_id, "failed", "interrupted")
        return None
    finally:
        print()


async def run_single(loop: AgentLoop, store, prompt: str, max_steps: int, mode: str = MODE_NORMAL,
                     json_output: bool = False) -> int:
    """单任务模式(-p):跑完一个任务,打印结果与统计,返回退出码。json_output=M2 headless。"""
    console.print(f"[dim]workspace={Path.cwd()} · mode={mode}[/]\n")
    result = await _dispatch(loop, store, [], SummaryState(), prompt, max_steps, mode)
    if json_output:
        import json as _json

        # M2 headless:结构化输出供 CI 消费(退出码即成败)
        print(_json.dumps({
            "ok": bool(result and result.ok),
            "stop_reason": result.stop_reason if result else "interrupted",
            "final_text": result.final_text if result else "",
            "steps": result.steps if result else 0,
            "tool_calls": result.tool_calls if result else 0,
            "usage": {"input": result.usage.input_tokens, "output": result.usage.output_tokens}
            if result else None,
        }, ensure_ascii=False))
    else:
        console.print(f"[bold]── 结果[/] {result.summary() if result else '未完成'}")
    return 0 if (result and result.ok) else 1


async def run_resume(loop: AgentLoop, store, max_steps: int) -> int:
    """M2 --resume:以 Checkpoint 为边界恢复最近一次中断的 Run(新 Run 承接,不复活旧 Run)。"""
    cp = await store.latest_unrecovered_checkpoint()
    if not cp:
        console.print("[yellow]没有可恢复的中断 Run(全部已完成或已恢复)[/]")
        return 1
    old_run = cp["run_id"]
    messages = await store.load_run_messages(old_run)
    # M2 修正(2026-09-22):中断点多落在 TOOL_EXECUTION——载入历史的尾部常是
    # assistant(tool_calls) 而其 tool 结果缺失;直接追加恢复通知(user)违反
    # OpenAI 协议"tool_calls 后必须紧跟 tool 结果"→ 400(实测暴露)。
    # 为每个未应答的 tool_call 合成"状态未知"结果:把 Checkpoint 的
    # "pending 禁止盲目重试"语义翻译成协议合法形态。
    if messages and messages[-1].role == "assistant" and messages[-1].tool_calls:
        for tc in messages[-1].tool_calls:
            messages.append(Message(
                role="tool",
                content="[otter 恢复] 此调用在进程中断时执行状态未知;禁止盲目重试,请先用读类工具核实现状再决定继续或跳过。",
                tool_call_id=tc.id, name=tc.name,
            ))
    await store.mark_checkpoint_recovered(old_run)
    console.print(
        f"[bold blue]恢复 Run #{old_run}(中断于 {cp['phase']},原始任务:{(cp['user_message'] or '')[:40]}…)[/]\n"
        f"[dim]已载入 {len(messages)} 条历史;pending 工具按'不确定是否已执行'处理,模型将先核实现状[/]\n"
    )
    if cp["phase"] == "TOOL_EXECUTION" and cp.get("pending_tools"):
        import json as _json

        notice = (
            "[otter 恢复] 上一 Run 在工具执行中中断。以下调用不确定是否已执行,"
            "禁止盲目重试——先用读类工具核实现状,再决定继续或跳过:\n"
            + _json.dumps(cp["pending_tools"], ensure_ascii=False)[:1500]
        )
    else:
        notice = f"[otter 恢复] 上一 Run 中断。原始任务:{cp['user_message'] or '(无记录)'}。请核实现状后继续完成它。"
    result = await _dispatch(loop, store, messages, SummaryState(), notice, max_steps, MODE_NORMAL)
    console.print(f"[bold]── 恢复结果[/] {result.summary() if result else '再次中断(可再次 --resume)'}")
    return 0 if result else 1


async def run_repl(loop: AgentLoop, store, max_steps: int) -> None:
    """交互 REPL:多轮对话;history 全量在内存,请求视图由 loop 按水位重建(M1)。"""
    console.print(BANNER)
    history: list[Message] = []
    state = SummaryState()
    mode = MODE_NORMAL
    while True:
        try:
            prompt = console.input(f"\n[bold cyan]otter{'(plan)' if mode == MODE_PLAN else ''}>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见 👋")
            return
        if not prompt:
            continue
        if prompt in ("exit", "quit", "/quit"):
            console.print("再见 👋")
            return

        # ── 斜杠命令(M1) ──
        if prompt == "/help":
            console.print(BANNER.split("\n", 1)[1])
            continue
        if prompt == "/reset":
            history.clear()
            state = SummaryState()
            console.print("[dim]会话与压缩状态已清空[/]")
            continue
        if prompt == "/init":
            from otter.agents_md import init_agents_md

            path = init_agents_md()
            console.print(f"[green]已生成 {path}[/]" if path else "[yellow]AGENTS.md 已存在,拒绝覆盖(保护手写内容)[/]")
            loop.instructions = load_instructions()  # 立即生效
            continue
        if prompt == "/undo":
            from otter.gitops import undo_last

            console.print(await undo_last())
            continue
        if prompt == "/resume":
            result = await run_resume(loop, store, max_steps)
            history.clear()  # 恢复语义:历史已被恢复流程管理,重置现场避免双份
            state = SummaryState()
            continue
        if prompt.startswith("/skill"):
            # M4:skill list / skill accept runN(人工确认才转正)
            from otter.skills import accept_candidate, list_skills

            parts = prompt.split()
            if len(parts) >= 3 and parts[1] == "accept" and parts[2].startswith("run"):
                console.print(accept_candidate(int(parts[2][3:])))
            else:
                console.print(list_skills())
            continue
        if prompt.startswith("/artifact"):
            from otter.artifact import list_artifacts, open_artifact

            parts = prompt.split()
            arts = list_artifacts()
            if not arts:
                console.print("[dim](无产物)[/]")
            elif len(parts) >= 3:  # /artifact open|finder|vscode <id|name>
                console.print(open_artifact(parts[2], parts[1]))
            else:
                for a in arts:
                    console.print(f"[dim]#{a['id']} · {a['name']} · {a['size']}B · {a['note'] or ''}[/]")
            continue
        if prompt in ("/plan", "/act"):
            mode = MODE_PLAN if prompt == "/plan" else MODE_NORMAL
            console.print(f"[magenta]PLAN 只读模式(检索与分析,不写文件)[/]" if mode == MODE_PLAN else "[green]ACT 执行模式(可写文件)[/]")
            continue
        if prompt.startswith("/plan "):
            # Plan Mode v2(2026-09-23):产品经理模式——调查→结构化计划→采纳即执行
            task = prompt[6:].strip()
            if not task:
                console.print("[yellow]用法:/plan <任务描述>[/]")
                continue
            result = await _dispatch(loop, store, history, state, task, max_steps, MODE_PLAN)
            if result is None:
                continue
            console.print(f"\n[dim]{result.summary()}[/]")
            if result.ok:
                from otter.plans import load_plan, plan_is_valid

                if not plan_is_valid(result.final_text):
                    console.print("[yellow]⚠ 计划未包含'## 步骤'小节,格式不完整;仍可执行[/]")
                console.print("[bold cyan]采纳此计划并执行? [Y/n][/] ", end="")
                try:
                    answer = (await asyncio.to_thread(input, "")).strip().lower()
                except EOFError:
                    # 修正(2026-09-23):管道输入下 stdin 被 REPL 主循环读尽,内层 input 抛
                    # EOF——按 [Y/n] 的回车默认语义处理为采纳(真终端不会走到这)
                    answer = ""
                if answer in ("", "y", "yes"):
                    console.print("[green]→ 按计划执行[/]\n")
                    exec_result = await _dispatch(loop, store, history, state, task, max_steps,
                                                  MODE_NORMAL, plan_context=result.final_text)
                    if exec_result:
                        console.print(f"\n[dim]{exec_result.summary()}[/]")
                        history.append(Message(role="user", content=task))
                        history.append(Message(role="assistant", content=exec_result.final_text))
                else:
                    console.print("[dim]计划已存盘(.otter/plans/),仅保留不执行;可用 /plans 查看[/]")
            continue
        if prompt == "/plans":
            from otter.plans import list_plans

            rows = list_plans()
            if not rows:
                console.print("[dim](无计划)[/]")
            for r in rows:
                console.print(f"[dim]{r['file']} · {r['task'][:50]}[/]")
            continue
        if prompt == "/context":
            tail_est = estimate_messages_tokens(history[state.covered:]) if history else 0
            console.print(
                f"[dim]历史 {len(history)} 条(未压缩尾部估算 ~{tail_est} tokens)· "
                f"预算 {loop.context_budget}(触发线 {int(loop.context_budget * loop.trigger_ratio)}) · "
                f"已压缩 {state.compressions} 次 · 摘要:{'有' if state.summary else '无'}[/]"
            )
            continue

        result = await _dispatch(loop, store, history, state, prompt, max_steps, mode)
        if result:
            console.print(f"\n[dim]{result.summary()}[/]")
            history.append(Message(role="user", content=prompt))
            history.append(Message(role="assistant", content=result.final_text))
        else:
            console.print("[bold red]本轮未完成(中断/异常),历史未追加[/]")
