"""故障现场自动归档(bug 监测第 4 层,2026-09-29)。

巡逻(第 2 层)发现高危信号时,把该 Run 的完整现场留存一份"事故包"——
库内数据(run 行/事件流/消息)+ 工作区文件清单,落 <workspace>/.otter/incidents/run-<id>/:

    incident.json  机读全量(run/events/messages/findings 原始数据)
    README.md      人读摘要(发现列表/元数据/事件时间线/消息尾/文件清单)

设计口径:
- 只归档 high(引擎层故障);medium 是行为异常,量大利薄,不自动留存;
- 一个 Run 只归档一次(目录存在即跳过)——巡逻可反复跑,不重复占盘;
- 归档失败静默返回 None(归档是巡逻的附属动作,绝不反噬巡逻本身);
- 文件清单只记 相对路径/大小/mtime,不拷内容——工作区就是现场本身,
  拷贝既慢又可能把"现场"和"快照"混淆;清单足够回答"当时有哪些文件"。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from otter.store import Store

INCIDENTS_DIRNAME = "incidents"

# 文件清单上限:防大仓库把事故包撑爆(超出的截断并标注)
MAX_FILES = 300
# README 消息尾条数:足够看到"最后在干什么",又不至于把摘要淹没
TAIL_MESSAGES = 8


def _file_inventory(workspace: Path) -> tuple[list[dict], bool]:
    """工作区文件清单(相对路径/字节/iso 时间;排除 .otter 与 .git)。"""
    out: list[dict] = []
    truncated = False
    for p in sorted(workspace.rglob("*")):
        rel = p.relative_to(workspace)
        if rel.parts[0] in (".otter", ".git"):
            continue
        if len(out) >= MAX_FILES:
            truncated = True
            break
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_dir():
            out.append({"path": str(rel), "dir": True})
        else:
            out.append({"path": str(rel), "bytes": st.st_size,
                        "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out, truncated


def _fmt_time(val) -> str:
    """started_at 等时间字段(库内是 unix 秒)转人读格式;转不动就原样。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(val)))
    except (TypeError, ValueError):
        return str(val or "?")


def _timeline(events: list[dict]) -> list[str]:
    """事件流 → 人读时间线(每行 type + 关键 payload 字段)。"""
    KEY_FIELDS = ("step", "name", "ok", "error", "reason", "attempt", "tool")
    lines = []
    for e in events:
        payload = e.get("payload") or {}
        parts = [f"{e['type']}"]
        for k in KEY_FIELDS:
            if k in payload:
                parts.append(f"{k}={str(payload[k])[:80]}")
        lines.append(" · ".join(parts))
    return lines


async def archive_incident(store: Store, run: dict, findings: list[dict],
                           workspace: Path) -> Path | None:
    """把高危 Run 的现场打包落盘;已归档过(目录存在)返回 None。

    findings 传巡逻 Finding 的 asdict 形式或兼容 dict(有 severity/kind/detail 即可)。
    """
    rid = run["id"]
    base = workspace / ".otter" / INCIDENTS_DIRNAME / f"run-{rid}"
    if base.exists():
        return None  # 去重:同一 Run 只留一次现场

    try:
        events = await store.load_run_events(rid)
        messages = await store.load_run_messages(rid)
        inventory, truncated = _file_inventory(workspace)

        base.mkdir(parents=True, exist_ok=True)
        # 机读全量:复盘/写 bug 档案时按 run 行逐字段可查
        (base / "incident.json").write_text(json.dumps({
            "archived_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run": run,
            "findings": findings,
            "events": events,
            "messages": [{"role": m.role, "content": m.content,
                          "tool_calls": [tc.name for tc in (m.tool_calls or [])],
                          "name": m.name}
                         for m in messages],
            "file_inventory": inventory,
            "inventory_truncated": truncated,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

        # 人读摘要:一眼看懂"发生了什么/当时在干什么/现场有哪些文件"
        lines = [
            f"# 事故包 · Run #{rid}",
            "",
            f"- 归档时间:{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- Run 状态:{run.get('status')} · stop_reason:{run.get('stop_reason') or '?'}"
            f" · 开始:{_fmt_time(run.get('started_at'))}",
            f"- 子代理:{'是(父 Run #{})'.format(run['parent_run_id']) if run.get('parent_run_id') else '否'}",
            "",
            "## 巡逻发现",
            "",
        ]
        if findings:
            lines += [f"- [{f.get('severity')}/{f.get('kind')}] {f.get('detail')}"
                      for f in findings]
        else:
            lines.append("- (无)")
        lines += ["", "## 事件时间线", ""]
        timeline = _timeline(events)
        lines += timeline if timeline else ["- (无事件)"]
        lines += ["", f"## 消息尾(最后 {TAIL_MESSAGES} 条)", ""]
        for m in messages[-TAIL_MESSAGES:]:
            text = (m.content or "").strip().replace("\n", " ")[:160]
            tools = f" [调用 {'/'.join(tc.name for tc in m.tool_calls)}]" if m.tool_calls else ""
            lines.append(f"- **{m.role}**{tools}:{text or '(空)'}")
        lines += ["", "## 工作区文件清单(不拷内容,现场保留在原工作区)", ""]
        if inventory:
            lines += [f"- {f['path']}" + (f"({f['bytes']}B,{f['mtime']})" if not f.get("dir") else "/")
                      for f in inventory]
        else:
            lines.append("- (空)")
        if truncated:
            lines.append(f"- ⚠️ 清单截断(超过 {MAX_FILES} 个文件)")
        (base / "README.md").write_text("\n".join(lines), encoding="utf-8")
        return base
    except Exception:
        # 归档失败:返回 None 不抛(巡逻附属动作,失败只损失现场包,不能损失巡逻)
        try:
            import shutil

            shutil.rmtree(base, ignore_errors=True)  # 半成品不留(下次巡逻可重试)
        except Exception:
            pass
        return None
