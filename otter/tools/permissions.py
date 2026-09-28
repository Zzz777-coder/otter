"""权限规则引擎 + 审批门(M2,说明书 5.5 三层 fail-closed 的前两层)。

权限策略思想(未命中=ASK、DENY 优先、
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
        """审批"总是允许/拒绝"时落盘固化(批准可固化为规则)。"""
        self.user_rules.append(rule)
        self._save()  # 2026-09-24 重构:落盘收敛到 _save(与 remove 共用)

    # 2026-09-24 补齐(上游 对齐):规则查看/删除——GUI 与 CLI 此前都只能"写"不能"改",
    # 固化错了的 ALLOW/DENY 只能手编 json;补 /permissions 命令的管理面
    def list_rules(self) -> list[Rule]:
        return list(self.user_rules)

    def remove(self, index: int) -> Rule | None:
        """删除第 index 条用户规则(0 起)并落盘;越界返回 None。"""
        if 0 <= index < len(self.user_rules):
            rule = self.user_rules.pop(index)
            self._save()
            return rule
        return None

    def _save(self) -> None:
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
        answer = await self.ask(tool, args)
        # 2026-09-24 补齐(上游 对齐):ask 可返回丰富判定 str——
        # once=本次 / session=本会话 / always=固化 ALLOW 规则 / never=固化 DENY 规则 / deny=本次拒绝;
        # 兼容旧 bool 子类(True≈once / False≈deny),StubGate 等测试桩不用改
        if isinstance(answer, bool):
            return answer
        if answer == "once":
            return True
        if answer == "session":
            pattern = f"{str(args.get('command', '')).split()[0]} *" if tool == "bash" else None
            self.memory.allow(tool, pattern)
            return True
        if answer == "always":
            pattern = f"{str(args.get('command', '')).split()[0]} *" if tool == "bash" else None
            self.engine.add(Rule(tool, pattern, ALLOW))
            return True
        if answer == "never":
            # 2026-09-24 新增:拒绝方向固化(此前只能固化允许——误放行的工具没法拉黑)
            pattern = f"{str(args.get('command', '')).split()[0]} *" if tool == "bash" else None
            self.engine.add(Rule(tool, pattern, DENY))
            return False
        return False  # deny 及一切未识别值(fail-closed)

    async def ask(self, tool: str, args: dict) -> bool:
        raise NotImplementedError


class ConsoleApprovalGate(ApprovalGate):
    """终端审批门:三选项 + 未识别输入按拒绝(fail-closed)。阻塞 input 经 to_thread 不卡事件循环。"""

    async def ask(self, tool: str, args: dict) -> str | bool:
        preview = tool if tool != "bash" else f"bash: {args.get('command', '')[:120]}"
        # 2026-09-24 补齐:加 [4] 总是拒绝(固化 DENY)——此前拒绝方向只能一次性,
        # 误放行(如某危险 bash 前缀)没法拉黑;/permissions 可删规则
        tip = (
            f"\n⚠️  otter 请求执行 [{preview}]\n"
            f"   [1] 本次允许  [2] 本会话都允许  [3] 总是允许(写规则)\n"
            f"   [4] 总是拒绝(写规则)  [5] 本次拒绝 > "
        )
        try:
            answer = await asyncio.to_thread(input, tip)
        except EOFError:
            # M2 fail-closed:headless 无 stdin(管道/CI)时不可交互 = 拒绝;
            # CI 需要执行 bash 时用 -p --yes 或 ~/.otter/permissions.json 预授权
            return False
        answer = answer.strip()
        # 2026-09-24 重构:判定分流收敛到基类 resolve(ask 只报意图,不再自行固化规则)
        return {"1": "once", "2": "session", "3": "always", "4": "never"}.get(answer, "deny")


class WebApprovalGate(ApprovalGate):
    """GUI 审批门(2026-09-24 升级:confirm() 二态 → 独立四按钮审批窗)。

    此前 GUI 只有"允许/拒绝"二态,GUI 用户永远无法固化规则(只有 CLI 能写
    permissions.json)——权限规则持久化在 GUI 侧断头(上游 对齐轮补齐)。
    实现复刻 gui._diffwin_ask 的成熟模式:临时 html + create_window + js_api
    回调记结果 + 轮询等待 + 代际计数防重入串线。窗创建失败回退 confirm()。
    """

    _gen = 0  # 类级代际计数(防上一轮弹窗未处理时新旧结果串线,diffwin 同款)

    def __init__(self, engine: PermissionEngine, memory: SessionMemory, window) -> None:
        super().__init__(engine, memory)
        self.window = window

    async def ask(self, tool: str, args: dict) -> str | bool:
        preview = tool if tool != "bash" else str(args.get("command", ""))[:200]
        try:
            return await asyncio.to_thread(self._askwin, preview, tool)
        except Exception:
            # 弹窗异常:回退主窗 confirm()(再失败由调用方 fail-closed)
            try:
                return bool(await asyncio.to_thread(
                    self.window.evaluate_js,
                    f'confirm("otter 请求执行:\\n{json.dumps(preview, ensure_ascii=False)}\\n\\n允许?")',
                ))
            except Exception:
                return False

    def _askwin(self, preview: str, tool: str, timeout_s: float = 300.0) -> str:
        """独立四按钮审批窗(工作线程内跑):本次允许/总是允许/总是拒绝/拒绝。"""
        import tempfile
        import time as _time
        import webview

        WebApprovalGate._gen += 1
        gen = WebApprovalGate._gen
        result = {"v": None}

        doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>权限确认</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#f3f5f9;color:#182233;font-family:-apple-system,"PingFang SC",sans-serif;
     display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;padding:16px}}
.icon{{font-size:28px;margin-bottom:10px}}
.q{{font-size:13px;font-weight:600;text-align:center;margin-bottom:16px;line-height:1.5;
    word-break:break-all;max-width:100%}}
.small{{font-size:10.5px;color:#8490a1;font-weight:400;margin-top:6px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;width:100%}}
button{{border:none;border-radius:8px;padding:9px 0;font:600 12px inherit;cursor:pointer}}
.ok{{background:#4f73d9;color:#fff}} .no{{background:#c95555;color:#fff}}
</style></head><body>
<div class="icon">🦦</div>
<div class="q">otter 请求执行<div class="small">{preview}</div></div>
<div class="grid">
<button class="ok" onclick="pywebview.api.verdict('once')">本次允许</button>
<button class="ok" onclick="pywebview.api.verdict('always')">总是允许(写规则)</button>
<button class="no" onclick="pywebview.api.verdict('never')">总是拒绝(写规则)</button>
<button class="no" onclick="pywebview.api.verdict('deny')">本次拒绝</button>
</div>
</body></html>"""
        tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
        tmp.write(doc)
        tmp.close()

        class AskApi:
            def verdict(self, v):
                result["v"] = v  # 只记结果;窗口由 _askwin 收尾统一销毁(diffwin 同款)

        try:
            w = webview.create_window(
                "otter 权限确认", url=f"file://{tmp.name}", width=340, height=240,
                js_api=AskApi())
        except Exception:
            return "deny"  # 窗创建失败:fail-closed

        deadline = _time.monotonic() + timeout_s
        while _time.monotonic() < deadline:
            if gen != WebApprovalGate._gen or result["v"] is not None:
                break
            _time.sleep(0.1)
        try:
            w.destroy()
        except Exception:
            pass
        # 超时/被新窗顶替 = 拒绝(fail-closed);never 由基类 resolve 固化为 DENY 规则
        return result["v"] or "deny"
