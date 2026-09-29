"""运行时失败信号巡逻(bug 监测第 1+2 层,2026-09-29)。

第 1 层 scan_run / scan_recent:从 runs 表 + events 表提取失败信号——
    模型错误 / 预算硬停 / 异常中断 / 审批拒绝风暴 / 协议空响应重试 / 工具失败风暴。
    判据全部离线(纯读库),不调模型,不需要 API key。
第 2 层 run_patrol:CLI `otter --patrol [hours]` 入口,汇总终端报告;
    有 high 信号时退出码 1(可直接接 cron/脚本),并弹 macOS 桌面通知
    (一次一条汇总,防逐条刷屏;通知失败静默,绝不影响巡逻本身)。

语义约定(第 1 层的"失败"分级):
    high   引擎层故障——模型错误、Run failed(status/stop_reason)
    medium 行为异常——异常中断(kill)、预算硬停、重复工具截停、审批拒绝风暴、协议重试
    low    观察信号——工具失败/拒绝 ≥3 次(含围栏等设计性拒绝,只统计不归因)、max_steps 截停
    用户主动 cancelled 与干净的 completed/final_answer 不产生任何发现。
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from otter.store import Store


@dataclass
class Finding:
    """一条失败信号:kind 分类 + severity 分级 + 人读描述。"""

    run_id: int
    kind: str          # failed/model_error/interrupted/budget/repeated_tool/denied_storm/protocol_retry/tool_failures/max_steps
    severity: str      # high/medium/low
    detail: str
    is_subagent: bool = False   # 子代理 run(parent_run_id 非空)单独标注,方便上溯父 Run


# ── 第 1 层:单 Run 判据 ───────────────────────────────────────────────

async def scan_run(store: Store, run: dict) -> list[Finding]:
    """对一个 Run(runs 表行)做失败判据扫描,返回发现列表(干净则空)。

    run 字典字段:id/status/stop_reason/started_at/parent_run_id。
    """
    rid = run["id"]
    sub = bool(run.get("parent_run_id"))
    prefix = "[子代理] " if sub else ""
    findings: list[Finding] = []
    events = await store.load_run_events(rid)

    status = run.get("status") or ""
    stop = run.get("stop_reason") or ""

    def add(kind: str, severity: str, detail: str) -> None:
        findings.append(Finding(rid, kind, severity, prefix + detail, sub))

    # ① 引擎层故障(high)
    if status == "failed" or stop in ("model_error", "error"):
        add("failed", "high", f"Run #{rid} 失败(status={status}, stop={stop or '?'})")
    for e in events:
        if e["type"] == "MODEL_ERROR":
            add("model_error", "high",
                f"Run #{rid} 模型错误:{str(e['payload'].get('error', ''))[:160]}")

    # ② 异常中断:启动修正把遗留 running 改标 interrupted(kill -9/崩溃后重启)
    if status == "interrupted":
        add("interrupted", "medium", f"Run #{rid} 异常中断(进程被杀或崩溃,--resume 可恢复)")

    # ③ 预算硬停:事件与 stop_reason 只报一条(同因去重)
    if any(e["type"] == "RUN_BUDGET_EXCEEDED" for e in events) or stop == "run_budget_exceeded":
        add("budget", "medium", f"Run #{rid} 触发费用预算硬停(OTTER_RUN_BUDGET)")

    # ④ 重复工具截停
    for e in events:
        if e["type"] == "RUN_STOPPED":
            add("repeated_tool", "medium",
                f"Run #{rid} 因重复调用同一工具被截停:{e['payload'].get('reason', '?')}")

    # ⑤ 审批拒绝风暴:单次拒绝是正常用户决策,≥3 次仍继续尝试即行为异常
    denied = sum(1 for e in events if e["type"] == "TOOL_DENIED")
    if denied >= 3:
        add("denied_storm", "medium", f"Run #{rid} 审批拒绝 {denied} 次(反复被拒仍在尝试)")

    # ⑥ 协议重试:空响应/协议文本重试累计 ≥2(单次偶发正常,连续即链路不稳)
    retry = sum(1 for e in events
                if e["type"] in ("MODEL_EMPTY_RETRY", "MODEL_TEXTUAL_RETRY"))
    if retry >= 2:
        add("protocol_retry", "medium", f"Run #{rid} 模型协议异常重试 {retry} 次")

    # ⑦ 工具失败风暴(low):ok=False 含围栏/审批/参数校验等设计性拒绝,只观察不归因;
    #    老事件无 ok 字段(payload.get 返回 None)自动跳过
    tool_fail = sum(1 for e in events
                    if e["type"] == "TOOL_COMPLETED" and e["payload"].get("ok") is False)
    if tool_fail >= 3:
        add("tool_failures", "low", f"Run #{rid} 工具失败/拒绝 {tool_fail} 次(含设计性拒绝)")

    # ⑧ max_steps 截停(low):不一定是 bug,常是任务太大,信息性记录
    if stop == "max_steps":
        add("max_steps", "low", f"Run #{rid} 步数用尽截停(max_steps)")

    return findings


async def scan_recent(store: Store, hours: float = 24.0) -> tuple[list[Finding], int]:
    """扫描最近 N 小时的全部 Run(含子代理 run),返回 (发现, 扫描 run 数)。"""
    runs = await store.recent_runs(hours)
    findings: list[Finding] = []
    for run in runs:
        findings.extend(await scan_run(store, run))
    return findings, len(runs)


# ── 第 2 层:巡逻入口(CLI --patrol)───────────────────────────────────

def render_report(findings: list[Finding], scanned: int, hours: float) -> str:
    """发现列表 → 终端报告(按严重度分组;干净时一行带过)。"""
    if not findings:
        return f"✅ 近 {hours:g}h 扫描 {scanned} 个 Run,无失败信号。"
    order = {"high": 0, "medium": 1, "low": 2}
    lines = [f"⚠️  近 {hours:g}h 扫描 {scanned} 个 Run,发现 {len(findings)} 条信号:",
             ""]
    for sev in ("high", "medium", "low"):
        group = [f for f in findings if f.severity == sev]
        if not group:
            continue
        mark = {"high": "🔴", "medium": "🟡", "low": "⚪"}[sev]
        lines.append(f"{mark} {sev}({len(group)})")
        for f in sorted(group, key=lambda x: order[x.severity]):
            lines.append(f"  - [{f.kind}] {f.detail}")
    return "\n".join(lines)


def notify_macos(title: str, body: str) -> bool:
    """macOS 桌面通知(osascript)。非 macOS/无 osascript/超时一律静默 False——
    通知是附加通道,失败绝不影响巡逻结果。"""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             f'display notification {body!r} with title {title!r}'],
            timeout=5, capture_output=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def run_patrol(hours: float = 24.0, workspace: Path | None = None,
               notify: bool = True) -> int:
    """巡逻同步入口(CLI 直调):读当前工作区 .otter/otter.db → 扫描 → 报告。

    返回码:0=干净(或无库);1=存在 high 信号(可接 cron 告警)。
    """
    db_path = (workspace or Path.cwd()) / ".otter" / "otter.db"
    if not db_path.exists():
        print(f"[patrol] 未找到运行库({db_path}),该工作区还没有跑过任务,无需巡逻。")
        return 0

    async def _scan() -> tuple[list[Finding], int]:
        store = Store(db_path)
        await store.open()
        try:
            return await scan_recent(store, hours)
        finally:
            await store.close()

    findings, scanned = asyncio.run(_scan())
    print(render_report(findings, scanned, hours))

    highs = [f for f in findings if f.severity == "high"]
    if notify and findings:
        # 一次一条汇总通知(防逐条刷屏);标题区分有无 high
        title = f"otter 巡逻:{len(highs)} 条高危 / 共 {len(findings)} 条信号" \
            if highs else f"otter 巡逻:{len(findings)} 条信号(无高危)"
        body = f"近 {hours:g}h {scanned} 个 Run;最高级别 {findings[0].severity}"
        notify_macos(title, body[:200])
    return 1 if highs else 0
