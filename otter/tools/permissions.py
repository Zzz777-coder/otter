"""权限规则引擎 + 审批门(M2,说明书 5.5 三层 fail-closed 的前两层)。

设计参考 vesta app/tools/permissions/policy.py 的思想(未命中=ASK、DENY 优先、
规则可固化),全部重新实现:
- 规则来源:内置默认表 → 用户规则文件(~/.otter/permissions.json,审批"总是允许"落盘)
  → 会话记忆(审批"本次会话都允许",进程内);
- 判定顺序:显式规则(含命令前缀匹配)优先于默认表;同工具多条冲突时 DENY > ALLOW > ASK;
- 未命中任何规则 = ASK(fail-closed 的第一道);
- 审批交互:ConsoleApprovalGate(终端三选项,未识别输入按拒绝 = fail-closed 第二道)。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

ALLOW, ASK, DENY = "allow", "ask", "deny"
_VERDICT_RANK = {DENY: 3, ALLOW: 2, ASK: 1}

# 内置默认:检索/读类/元工具直接放行;bash 首次必问(写文件由 git+/undo + diff 预览兜底)
# 修正(2026-09-23):tool_search/repo_map 等元工具此前未列 → ASK → GUI 弹确认框刷屏
# (真机暴露:用户被 tool_search 的审批框烦到拒绝,误以为"没有权限")
DEFAULT_RULES: list[tuple[str, str | None, str]] = [
    ("read_file", None, ALLOW),
    ("grep", None, ALLOW),
    ("glob", None, ALLOW),
    ("evidence_read", None, ALLOW),
    ("evidence_list", None, ALLOW),
    ("tool_search", None, ALLOW),
    ("repo_map", None, ALLOW),
    ("memory_read", None, ALLOW),
    ("memory_search", None, ALLOW),
    ("memory_list", None, ALLOW),
    ("skill_list", None, ALLOW),
    ("explore", None, ALLOW),
    ("get_schedule", None, ALLOW),
    ("simulate_change", None, ALLOW),
    ("commit_reschedule", None, ALLOW),
    ("artifact_publish", None, ALLOW),
    ("write_file", None, ALLOW),
    ("edit_file", None, ALLOW),
    ("make_pdf", None, ALLOW),  # 2026-09-24 R6:原生 PDF 生成(输出必带 %PDF 魔数,只写工作区)
    ("bash", None, ALLOW),  # 2026-09-23 用户要求:agent 可访问任意路径,不再逐条审批
]

RULES_FILE = Path.home() / ".otter" / "permissions.json"


@dataclass
class Rule:
    tool: str            # 工具名;"*" 匹配所有
    pattern: str | None  # bash 命令前缀(glob 风格,"git *" );None=整工具
    verdict: str
    source: str = "user"


def _match(rule: Rule, tool: str, args: dict) -> bool:
    if rule.tool not in ("*", tool):
        return False
    if rule.pattern is None:
        return True
    if tool != "bash":  # 目前只有 bash 命令细分
        return True
    command = str(args.get("command", ""))
    return re.fullmatch(rule.pattern.replace("*", ".*"), command) is not None


class PermissionEngine:
    def __init__(self, rules_file: Path | None = None) -> None:
        self.rules_file = rules_file or RULES_FILE
        self.user_rules: list[Rule] = self._load()

    def _load(self) -> list[Rule]:
        if not self.rules_file.is_file():
            return []
        try:
            data = json.loads(self.rules_file.read_text(encoding="utf-8"))
            return [Rule(r["tool"], r.get("pattern"), r["verdict"]) for r in data.get("rules", [])]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []  # 规则文件损坏:退回内置默认(fail-closed 方向)

    def add(self, rule: Rule) -> None:
        """审批"总是允许/拒绝"时落盘固化(设计参考 vesta:批准可固化为规则)。"""
        self.user_rules.append(rule)
        self.rules_file.parent.mkdir(parents=True, exist_ok=True)
        data = {"rules": [
            {"tool": r.tool, "pattern": r.pattern, "verdict": r.verdict} for r in self.user_rules
        ]}
        self.rules_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def decide(self, tool: str, args: dict) -> str:
        """显式规则(含会话记忆)优先;同工具多条冲突 DENY > ALLOW > ASK;未命中默认表;再未命中=ASK。"""
        verdicts = [r.verdict for r in self.user_rules if _match(r, tool, args)]
        for t, pattern, verdict in DEFAULT_RULES:
            if _match(Rule(t, pattern, verdict), tool, args):
                verdicts.append(verdict)
        if not verdicts:
            return ASK
        return max(verdicts, key=lambda v: _VERDICT_RANK[v])


class SessionMemory:
    """会话级审批记忆("本次会话都允许"),进程内、不落盘。"""

    def __init__(self) -> None:
        self._allowed: list[Rule] = []

    def allow(self, tool: str, pattern: str | None) -> None:
        self._allowed.append(Rule(tool, pattern, ALLOW))

    def matches(self, tool: str, args: dict) -> bool:
        return any(_match(r, tool, args) for r in self._allowed)


class ApprovalGate:
    """审批门基类:resolve = 完整判定入口(会话记忆 → 规则 → 交互)。loop 只调这个。"""

    def __init__(self, engine: PermissionEngine, memory: SessionMemory) -> None:
        self.engine = engine
        self.memory = memory

    async def resolve(self, tool: str, args: dict) -> bool:
        if self.memory.matches(tool, args):
            return True
        verdict = self.engine.decide(tool, args)
        if verdict == DENY:
            return False
        if verdict == ALLOW:
            return True
        return await self.ask(tool, args)

    async def ask(self, tool: str, args: dict) -> bool:
        raise NotImplementedError


class ConsoleApprovalGate(ApprovalGate):
    """终端审批门:三选项 + 未识别输入按拒绝(fail-closed)。阻塞 input 经 to_thread 不卡事件循环。"""

    async def ask(self, tool: str, args: dict) -> bool:
        preview = tool if tool != "bash" else f"bash: {args.get('command', '')[:120]}"
        tip = (
            f"\n⚠️  otter 请求执行 [{preview}]\n"
            f"   [1] 本次允许  [2] 本会话都允许  [3] 总是允许(写入规则)  [4] 拒绝 > "
        )
        try:
            answer = await asyncio.to_thread(input, tip)
        except EOFError:
            # M2 fail-closed:headless 无 stdin(管道/CI)时不可交互 = 拒绝;
            # CI 需要执行 bash 时用 -p --yes 或 ~/.otter/permissions.json 预授权
            return False
        answer = answer.strip()
        if answer == "1":
            return True
        if answer == "2":
            pattern = f"{str(args.get('command', '')).split()[0]} *" if tool == "bash" else None
            self.memory.allow(tool, pattern)
            return True
        if answer == "3":
            pattern = f"{str(args.get('command', '')).split()[0]} *" if tool == "bash" else None
            self.engine.add(Rule(tool, pattern, ALLOW))
            return True
        return False  # 含未识别输入:一律拒绝


class WebApprovalGate(ApprovalGate):
    """GUI 审批门(M2 简版):evaluate_js 同步等 confirm() 返回值。"""

    def __init__(self, engine: PermissionEngine, memory: SessionMemory, window) -> None:
        super().__init__(engine, memory)
        self.window = window

    async def ask(self, tool: str, args: dict) -> bool:
        preview = tool if tool != "bash" else str(args.get("command", ""))[:120]
        try:
            return bool(await asyncio.to_thread(
                self.window.evaluate_js,
                f'confirm("otter 请求执行:\\n{json.dumps(preview, ensure_ascii=False)}\\n\\n允许?")',
            ))
        except Exception:
            return False  # 窗口异常:拒绝(fail-closed)
