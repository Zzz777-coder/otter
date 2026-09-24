"""通用小工具包(2026-09-24 身份通用化配套):current_time / web_fetch / calculate。

otter 定位为通用 AI 助手后补的日常能力面:
- current_time:当前日期时间(本地时区,含星期)——模型训练记忆里没有"现在",
  高频刚需,常驻 schema(描述极短);
- web_fetch:抓网页转正文(复用 httpx2,无新依赖)——deferred(低频+网络),
  HTML→文本为纯函数可离线测;
- calculate:四则/幂/取余的安全算式求值——AST 白名单解释执行,**绝不 eval**,
  deferred(低频)。
"""

from __future__ import annotations

import ast
import datetime
import html as _html
import operator
import re
from typing import Any

from otter.tools.base import Tool


class CurrentTimeTool(Tool):
    name = "current_time"
    description = "获取当前日期与时间(本地时区,含星期)。凡涉及'今天/现在/几点'必须调用,不要凭记忆回答。"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any]) -> str:
        now = datetime.datetime.now()
        week = "一二三四五六日"[now.weekday()]
        return f"现在是 {now.strftime('%Y-%m-%d %H:%M')} 星期{week}"


# ── web_fetch:HTML→正文纯函数 + 抓取工具 ──

def html_to_text(html: str) -> str:
    """HTML → 可读正文(纯函数,离线可测):去 script/style/head/nav/footer,
    块级标签转换行,剥其余标签,解实体,压空白。"""
    html = re.sub(r"(?is)<(script|style|head|nav|footer)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|article|blockquote)>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "抓取一个网页并转为正文文本(url 以 http/https 开头)。网络失败/非文本如实返回错误。"
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "完整 URL"}},
        "required": ["url"],
    }

    async def run(self, args: dict[str, Any]) -> str:
        url = str(args.get("url", "")).strip()
        if not re.match(r"^https?://", url):
            return "[otter] URL 需以 http:// 或 https:// 开头"
        import httpx2 as httpx

        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(url)
        except Exception as exc:
            return f"[otter] 抓取失败:{type(exc).__name__}: {exc}"
        ctype = resp.headers.get("content-type", "")
        body = resp.text or ""
        if "html" in ctype.lower() or body.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            body = html_to_text(body)
        return (f"[otter] {url}\n(状态 {resp.status_code},{len(body)} 字符)\n"
                + body[:8000] + ("\n…(截断)" if len(body) > 8000 else ""))


# ── calculate:AST 白名单安全求值(绝不 eval)──

_BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
            ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
            ast.Mod: operator.mod, ast.Pow: operator.pow}


def safe_eval(expr: str) -> float:
    """四则/幂/取余白名单求值。非法元素/语法错误抛 ValueError/SyntaxError(由工具层接住)。"""
    tree = ast.parse(expr, mode="eval")

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 400:
                raise ValueError("指数过大(>400)拒绝计算")  # 防 10**10**10 炸内存
            return _BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            v = ev(node.operand)
            return v if isinstance(node.op, ast.UAdd) else -v
        raise ValueError(f"不支持的表达式元素:{type(node).__name__}")

    return ev(tree)


class CalculateTool(Tool):
    name = "calculate"
    description = ("计算算式:支持 + - * / // % ** 与括号(如 '(120*1.13+450)/3')。"
                   "只接受数字与运算符,不接受函数或变量名。")

    parameters = {
        "type": "object",
        "properties": {"expression": {"type": "string", "description": "算式"}},
        "required": ["expression"],
    }

    async def run(self, args: dict[str, Any]) -> str:
        expr = str(args.get("expression", "")).strip()
        try:
            result = safe_eval(expr)
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
            return f"[otter] 计算失败:{type(exc).__name__}: {exc}"
        # 浮点整数值收敛显示(2.0 → 2)
        shown = int(result) if isinstance(result, float) and result.is_integer() else result
        return f"{expr} = {shown}"
