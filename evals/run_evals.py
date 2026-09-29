#!/usr/bin/env python3
"""评估 harness(2026-09-29 P3):跑固定任务集 → 机器验收 → markdown 报告。

用法(一键全跑):
    cd ~/otter && .venv/bin/python evals/run_evals.py
常用参数:
    --limit 3            只跑前 3 个(冒烟)
    --only code-func-fib 只跑指定 id(逗号分隔多个)
    --category coding    只跑某一类(coding/file/command/daily/security/subagent)
    --out evals/report.md  指定报告路径(默认 evals/report-<时间戳>.md)
    --update-baseline    把本次结果写为新基线(evals/baseline.json)

回归基线(2026-09-29 评估①):跑完自动与 baseline.json 对比,只比通过性
(延迟/token 波动大不比,防误报)——基线过而本次挂 = 回归,报告单列并在
退出码 1 体现(可接 CI);--update-baseline 转正新基线。

指标口径:
- 成功率 = check 全过的任务数 / 已跑任务数(不是模型说完成,是文件/命令/输出实测);
- token 成本 = AgentResult.usage(厂商返回的真实用量,非估算);
- 延迟 = run_task 墙钟;
- 工具调用数 = trace 分账(runs/events 表 TOOL_STARTED 事件计数,与结果对象的
  tool_calls 互为印证);
- 失败模式分类:
    check_failed   跑完(final_answer)但验收未过——产出不符合要求
    stopped_early  stop_reason 非 final_answer(步数/预算用尽被截停)
    error          引擎抛异常
依赖 ~/.otter/.env 或 cwd/.env 的模型 key;每任务独立临时工作区,串行执行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS_DIR.parent))  # 源码树直跑(未安装形态)

from otter import __version__, build_engine  # noqa: E402


def load_tasks() -> list[dict]:
    data = json.loads((EVALS_DIR / "tasks.json").read_text(encoding="utf-8"))
    return data["tasks"]


# ── 回归基线(评估①):只比通过性,不比延迟/token(波动大,防误报)─────────

BASELINE_PATH = EVALS_DIR / "baseline.json"


def load_baseline() -> dict:
    """读基线 {"model","updated","tasks":{id:{"ok":bool}}};不存在返回空结构。"""
    if not BASELINE_PATH.exists():
        return {"model": None, "updated": None, "tasks": {}}
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        return {"model": data.get("model"), "updated": data.get("updated"),
                "tasks": data.get("tasks") or {}}
    except (json.JSONDecodeError, OSError):
        return {"model": None, "updated": None, "tasks": {}}


def apply_baseline(records: list[dict], baseline: dict) -> list[dict]:
    """给每条 record 标注基线关系:regression(基线过→本次挂)/improved/
    is_new(基线无此任务)。返回回归任务列表。"""
    regressions = []
    for r in records:
        prev = baseline["tasks"].get(r["id"])
        if prev is None:
            r["is_new"] = True
            continue
        r["baseline_prev"] = bool(prev.get("ok"))
        if r["baseline_prev"] and not r["ok"]:
            r["regression"] = True
            regressions.append(r)
        elif not r["baseline_prev"] and r["ok"]:
            r["improved"] = True
    return regressions


def write_baseline(records: list[dict], model: str) -> None:
    """--update-baseline:本次结果转正为新基线(全量跑后使用才有意义)。"""
    data = {"model": model, "updated": time.strftime("%Y-%m-%d %H:%M"),
            "tasks": {r["id"]: {"ok": bool(r["ok"])} for r in records}}
    BASELINE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                             encoding="utf-8")


# ── 验收器:全部 check 通过才算成功 ────────────────────────────────────

def _event_match(check: dict, events: list[dict]) -> tuple[bool, str]:
    """events 表断言(评估③子代理专项的机器判据):存在指定类型事件,
    且(可选)payload_in 里每个键值与事件 payload 精确相等。
    返回 (是否命中, 失败/命中描述)。"""
    want_type = check.get("event", "")
    want_payload = check.get("payload_in") or {}
    for e in events:
        if e["type"] != want_type:
            continue
        payload = e.get("payload") or {}
        if all(payload.get(k) == v for k, v in want_payload.items()):
            desc = f"{want_type}" + (f" payload~{want_payload}" if want_payload else "")
            return True, desc
    return False, f"events 中未命中 {want_type} payload~{want_payload}"


def run_check(check: dict, ws: Path, final_text: str,
              events: list[dict] | None = None) -> tuple[bool, str]:
    """执行单个 check,返回 (是否通过, 失败描述)。events 供事件断言类 check 用。"""
    t = check["type"]
    if t == "file_exists":
        return (ws / check["path"]).is_file(), f"文件不存在:{check['path']}"
    if t == "not_exists":
        return not (ws / check["path"]).exists(), f"不应存在的文件还在:{check['path']}"
    if t == "file_contains":
        try:
            return check["contains"] in (ws / check["path"]).read_text(encoding="utf-8"), \
                f"{check['path']} 未包含 {check['contains']!r}"
        except OSError:
            return False, f"读不到 {check['path']}"
    if t == "not_file_contains":
        try:
            return check["contains"] not in (ws / check["path"]).read_text(encoding="utf-8"), \
                f"{check['path']} 不应包含 {check['contains']!r}"
        except OSError:
            return True, "文件不存在(视为通过)"
    if t == "cmd":
        r = subprocess.run(check["cmd"], shell=True, cwd=ws, capture_output=True, text=True,
                           timeout=30)
        return r.returncode == 0, f"命令退出码 {r.returncode}:{check['cmd']}"
    if t == "cmd_output_contains":
        r = subprocess.run(check["cmd"], shell=True, cwd=ws, capture_output=True, text=True,
                           timeout=30)
        return check["contains"] in r.stdout, \
            f"输出未包含 {check['contains']!r}(实际:{r.stdout[:120]!r})"
    if t == "final_contains":
        return check["contains"] in (final_text or ""), f"最终回答未包含 {check['contains']!r}"
    if t == "final_contains_any":
        # 2026-09-29:多解判据(翻译类任务"重新排产"=reschedule/rearrange/re-plan 皆合法;
        # 单一词干判据在合法译法间随机挂——评估出题先受审的又一课)
        return any(c in (final_text or "") for c in check["contains"]), \
            f"最终回答未包含任一 {check['contains']!r}"
    if t == "final_contains_cmd":
        # 期望串由命令动态产出(日期类断言,任务集可重复跑)
        r = subprocess.run(check["cmd"], shell=True, capture_output=True, text=True, timeout=10)
        want = r.stdout.strip()
        return bool(want) and want in (final_text or ""), f"最终回答未包含 {want!r}"
    if t == "event_exists":
        # events 表断言(2026-09-29 评估③):如子代理 dispatch 是否真的发生
        hit, desc = _event_match(check, events or [])
        return hit, f"未发生期望事件:{desc}"
    if t == "event_not_exists":
        # 反向:安全断言"某事件不应发生"(如不该出现某工具调用)
        hit, desc = _event_match(check, events or [])
        return not hit, f"不应发生的事件出现了:{desc}"
    return False, f"未知 check 类型:{t}"


async def eval_one(task: dict, orig_cwd: Path) -> dict:
    """跑单个任务:临时工作区 → 预置文件 → 引擎执行 → 机器验收 → 指标归集。"""
    ws = Path(tempfile.mkdtemp(prefix=f"otter-eval-{task['id']}-"))
    for rel, content in (task.get("setup_files") or {}).items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    record: dict = {"id": task["id"], "category": task.get("category", "?")}
    engine = None
    t0 = time.time()
    try:
        engine = await build_engine(workspace=ws, yes=True, connect_mcp=False)
        result = await engine.run_task(task["prompt"], max_steps=task.get("max_steps", 12))
        record["latency_s"] = round(time.time() - t0, 1)
        record["stop_reason"] = result.stop_reason if result else "interrupted"
        record["steps"] = result.steps if result else 0
        record["tool_calls"] = result.tool_calls if result else 0
        usage = result.usage if result else None
        record["tokens_in"] = usage.input_tokens if usage else 0
        record["tokens_out"] = usage.output_tokens if usage else 0
        record["final_text"] = (result.final_text or "") if result else ""

        # trace 分账:events 表的 TOOL_STARTED 计数(与结果对象 tool_calls 互证)。
        # 2026-09-29 修正:run_id 必须取结果对象自带的(子代理 dispatch 会 new_run
        # 挂父 Run,latest_run_id() 会错拿子代理 run → 事件断言全落空)
        run_id = (result.run_id if result else None) or await engine.store.latest_run_id()
        events = await engine.store.load_run_events(run_id) if run_id else []
        record["trace_tool_events"] = sum(1 for e in events if e["type"] == "TOOL_STARTED")

        fails = []
        for check in task.get("checks", []):
            ok, why = run_check(check, ws, record["final_text"], events)
            if not ok:
                fails.append(why)
        record["ok"] = not fails
        record["fail_detail"] = fails
        if not record["ok"]:
            # 失败模式分类:跑完但没达标 vs 中途截停 vs 异常
            if record["stop_reason"] != "final_answer":
                record["failure_mode"] = "stopped_early"
            else:
                record["failure_mode"] = "check_failed"
    except Exception as exc:
        record["latency_s"] = round(time.time() - t0, 1)
        record["ok"] = False
        record["failure_mode"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record.setdefault("stop_reason", "error")
        record.setdefault("steps", 0)
        record.setdefault("tool_calls", 0)
        record.setdefault("tokens_in", 0)
        record.setdefault("tokens_out", 0)
        record.setdefault("trace_tool_events", 0)
        record.setdefault("fail_detail", [])
    finally:
        if engine is not None:
            try:
                await engine.aclose()
            except Exception:
                pass
        os_chdir_back(orig_cwd)
        # 2026-09-29:失败任务保留工作区(取证刚需——sec-env-guard 挂过一次但
        # 工作区已删无从查是沙箱漏了还是判据误伤);成功才删,防积累
        if record.get("ok"):
            shutil.rmtree(ws, ignore_errors=True)
        else:
            record["workspace"] = str(ws)
    return record


def os_chdir_back(orig_cwd: Path) -> None:
    """build_engine 会 chdir 进工作区;任务收尾切回原目录(串行跑,无竞态)。"""
    import os

    try:
        os.chdir(orig_cwd)
    except OSError:
        pass


def render_report(records: list[dict], model: str, elapsed_s: float,
                  baseline: dict | None = None) -> str:
    """跑分明细 → markdown 报告(总表/回归对比/分类表/逐任务表/失败明细)。"""
    n = len(records)
    ok_n = sum(1 for r in records if r["ok"])
    def avg(key: str) -> float:
        vals = [r.get(key, 0) or 0 for r in records]
        return sum(vals) / len(vals) if vals else 0.0

    mode_fail: dict[str, int] = {}
    for r in records:
        if not r["ok"]:
            mode_fail[r.get("failure_mode", "?")] = mode_fail.get(r.get("failure_mode", "?"), 0) + 1

    lines = [
        "# otter 评估报告",
        "",
        f"- 生成时间:{time.strftime('%Y-%m-%d %H:%M')}",
        f"- 引擎版本:v{__version__} · 模型:{model}",
        f"- 任务数:{n} · 总耗时:{elapsed_s:.0f}s(串行)",
        "",
        "## 总览",
        "",
        f"| 指标 | 值 |",
        f"|---|---|",
        f"| 成功率 | **{ok_n}/{n}({ok_n / n * 100:.0f}%)** |",
        f"| 平均延迟 | {avg('latency_s'):.1f}s |",
        f"| 平均 token(in/out) | {avg('tokens_in'):.0f} / {avg('tokens_out'):.0f} |",
        f"| 平均步数 | {avg('steps'):.1f} |",
        f"| 平均工具调用 | {avg('tool_calls'):.1f}(trace 事件口径 {avg('trace_tool_events'):.1f}) |",
    ]

    # 回归对比节(评估①):有基线且本次与基线有交集时才出现
    has_baseline = baseline is not None and baseline["tasks"]
    if has_baseline:
        regs = [r for r in records if r.get("regression")]
        imps = [r for r in records if r.get("improved")]
        news = [r for r in records if r.get("is_new")]
        lines += [
            "",
            f"## 回归对比(基线:{baseline.get('updated') or '?'} · 模型 {baseline.get('model') or '?'})",
            "",
            f"- ⚠️ **回归 {len(regs)}** · ✅ 改善 {len(imps)} · 🆕 新任务 {len(news)}(未入基线)",
        ]
        for r in regs:
            detail = ";".join(r.get("fail_detail") or []) or r.get("error", "")
            lines.append(f"- ⚠️ `{r['id']}`({r['category']})基线过→本次挂:{detail[:150]}")

    cats = sorted({r["category"] for r in records})
    if cats:
        lines += ["", "## 分类", "", "| 类别 | 成功率 | 平均延迟 | 平均工具调用 |", "|---|---|---|---|"]
        for c in cats:
            rs = [r for r in records if r["category"] == c]
            cok = sum(1 for r in rs if r["ok"])
            cl = sum(r.get("latency_s", 0) for r in rs) / len(rs)
            ct = sum(r.get("tool_calls", 0) for r in rs) / len(rs)
            lines.append(f"| {c} | {cok}/{len(rs)}({cok / len(rs) * 100:.0f}%) | {cl:.1f}s | {ct:.1f} |")

    if mode_fail:
        lines += ["", "## 失败模式", ""]
        for m, cnt in sorted(mode_fail.items(), key=lambda x: -x[1]):
            lines.append(f"- **{m}**:{cnt} 个")
        lines += ["", "## 失败明细", ""]
        for r in records:
            if r["ok"]:
                continue
            detail = ";".join(r.get("fail_detail") or []) or r.get("error", "")
            lines.append(f"- `{r['id']}`({r['category']}/{r.get('failure_mode')},"
                         f"stop={r.get('stop_reason')}):{detail[:200]}")

    # 逐任务表:有基线时加"基线"列(基线结果→本次结果,回归标 ⚠️)
    if has_baseline:
        lines += ["", "## 逐任务", "",
                  "| id | 类别 | 结果 | 基线 | 延迟s | 步数 | 工具 | token in/out | stop |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in records:
            if r.get("is_new"):
                base = "🆕"
            else:
                prev = "✅" if r.get("baseline_prev") else "❌"
                base = prev + (" ⚠️" if r.get("regression") else "")
            lines.append(
                f"| {r['id']} | {r['category']} | {'✅' if r['ok'] else '❌'} | {base} "
                f"| {r.get('latency_s', 0)} | {r.get('steps', 0)} | {r.get('tool_calls', 0)} "
                f"| {r.get('tokens_in', 0)}/{r.get('tokens_out', 0)} | {r.get('stop_reason', '?')} |")
    else:
        lines += ["", "## 逐任务", "",
                  "| id | 类别 | 结果 | 延迟s | 步数 | 工具 | token in/out | stop |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in records:
            lines.append(
                f"| {r['id']} | {r['category']} | {'✅' if r['ok'] else '❌'} "
                f"| {r.get('latency_s', 0)} | {r.get('steps', 0)} | {r.get('tool_calls', 0)} "
                f"| {r.get('tokens_in', 0)}/{r.get('tokens_out', 0)} | {r.get('stop_reason', '?')} |")
    lines.append("")
    return "\n".join(lines)


async def main_async(args) -> int:
    tasks = load_tasks()
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        tasks = [t for t in tasks if t["id"] in want]
    if args.category:
        tasks = [t for t in tasks if t.get("category") == args.category]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("没有匹配的任务", file=sys.stderr)
        return 2

    from otter.config import Config

    config = Config.load()
    if not config.api_key:
        print("错误:未配置 OTTER_API_KEY", file=sys.stderr)
        return 2

    orig_cwd = Path.cwd()
    records: list[dict] = []
    t0 = time.time()
    print(f"[evals] {len(tasks)} 个任务 · 模型 {config.model}")
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {task['id']} ...", flush=True)
        r = await eval_one(task, orig_cwd)
        records.append(r)
        mark = "✅" if r["ok"] else f"❌ {r.get('failure_mode')}"
        print(f"    {mark} · {r.get('latency_s', '?')}s · "
              f"{r.get('tokens_in', 0)}in/{r.get('tokens_out', 0)}out", flush=True)

    elapsed = time.time() - t0

    # 回归基线(评估①):对比 → 报告;--update-baseline 时本次转正为新基线
    baseline = load_baseline()
    regressions = apply_baseline(records, baseline) if baseline["tasks"] else []
    if args.update_baseline:
        write_baseline(records, config.model)
        print(f"[evals] 基线已更新:{BASELINE_PATH}(模型 {config.model})")

    report = render_report(records, config.model, elapsed,
                           baseline if baseline["tasks"] else None)
    out = Path(args.out) if args.out else EVALS_DIR / f"report-{time.strftime('%Y%m%d-%H%M')}.md"
    out.write_text(report, encoding="utf-8")
    ok_n = sum(1 for r in records if r["ok"])
    print(f"\n[evals] 成功率 {ok_n}/{len(records)} · 报告:{out}")
    if regressions:
        print(f"[evals] ⚠️ 回归 {len(regressions)} 个:{', '.join(r['id'] for r in regressions)}")
        return 1  # 回归即非零退出(可接 CI 门禁)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_evals", description="otter 评估 harness")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个")
    parser.add_argument("--only", default=None, help="只跑指定 id(逗号分隔)")
    parser.add_argument("--category", default=None, help="只跑某一类")
    parser.add_argument("--out", default=None, help="报告输出路径")
    parser.add_argument("--update-baseline", action="store_true",
                        help="把本次结果写为新基线(evals/baseline.json;全量跑后使用)")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
