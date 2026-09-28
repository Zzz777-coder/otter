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
from html.parser import HTMLParser
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


# ── web_search:DuckDuckGo Lite 免 key 搜索(2026-09-24 补,vesta 对齐) ──
# 方案照抄 vesta 的 DuckDuckGoSearchProvider:抓 lite.duckduckgo.com/lite 的静态 HTML,
# HTMLParser(标准库)解析 a[rel=nofollow] 链接 + td.result-snippet 摘要,uddg 重定向清洗。
# 刻意不走搜索 API(Tavily 要 key)也不引第三方搜索库——零新依赖、零 key。

DUCKDUCKGO_LITE_URL = "https://lite.duckduckgo.com/lite/?q={query}"


class _LiteParser(HTMLParser):
    """DDG Lite 结果页解析器:链接(rel=nofollow)与摘要(td.result-snippet)按序配对。"""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.snippets: list[str] = []
        self._in_link = False
        self._href: str | None = None
        self._link_text: list[str] = []
        self._in_snippet = False
        self._snippet: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr_map = {k: v for k, v in attrs}
        href = attr_map.get("href") or ""
        if tag == "a" and attr_map.get("rel") == "nofollow" and href:
            self._in_link, self._href, self._link_text = True, href, []
        elif tag == "td" and "result-snippet" in (attr_map.get("class") or ""):
            self._in_snippet, self._snippet = True, []

    def handle_data(self, data):
        if self._in_link:
            self._link_text.append(data)
        elif self._in_snippet:
            self._snippet.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._in_link:
            title = " ".join("".join(self._link_text).split())
            url = _clean_ddg_url(self._href or "")
            if title and url:
                self.links.append((title, url))
            self._in_link, self._href, self._link_text = False, None, []
        elif tag == "td" and self._in_snippet:
            self.snippets.append(" ".join("".join(self._snippet).split()))
            self._in_snippet = False


def _clean_ddg_url(href: str) -> str:
    """清洗 DDG 跳转链接://duckduckgo.com/l/?uddg=<encoded> → 真实 URL;非法 scheme 置空。"""
    from urllib.parse import parse_qs, unquote, urlsplit

    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlsplit(href)
    if "duckduckgo.com" in (parsed.hostname or ""):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            href = unquote(target)
            parsed = urlsplit(href)
    return href if parsed.scheme in {"http", "https"} else ""


def parse_lite_results(html: str, max_results: int = 5) -> list[dict]:
    """DDG Lite HTML → [{title,url,snippet}](纯函数,离线可测)。"""
    parser = _LiteParser()
    parser.feed(html)
    out = []
    for i, (title, url) in enumerate(parser.links[:max_results]):
        snippet = parser.snippets[i] if i < len(parser.snippets) else ""
        out.append({"title": title[:300], "url": url,
                    "snippet": snippet[:500]})  # 截断口径同 vesta(title 300/snippet 500)
    return out


class WebSearchTool(Tool):
    name = "web_search"
    description = ("网页搜索(DuckDuckGo):查实时信息/资料检索用这个,再配合 web_fetch 读原文。"
                   "免 key,尽力而为;无结果/被限流时如实报错,不要编造结果。")
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "搜索词"}},
        "required": ["query"],
    }

    def __init__(self) -> None:
        self._fetcher = None  # 测试注入:异步 fetch(query)->html;None=真实抓取

    async def run(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "[otter] 搜索词为空"
        html = ""
        if self._fetcher is not None:
            html = await self._fetcher(query)
        else:
            from urllib.parse import quote_plus
            import httpx2 as httpx

            # 真机暴露(2026-09-24):裸 httpx 默认 UA 被 DDG 反爬挡(HTTP 202 挑战页,
            # <400 不报错但解析出 0 条)——带浏览器 UA 后正常返回 200 结果页
            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                                     "Chrome/129.0.0.0 Safari/537.36"}
            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                             headers=headers) as client:
                    resp = await client.get(DUCKDUCKGO_LITE_URL.format(query=quote_plus(query)))
                if resp.status_code >= 400:
                    return f"[otter] 搜索服务异常(HTTP {resp.status_code})"
                html = resp.text
            except Exception as exc:
                return f"[otter] 搜索失败:{type(exc).__name__}: {exc}"
        results = parse_lite_results(html)
        if not results:
            return "[otter] 无搜索结果(可能被限流或词太偏);请换关键词或改用 web_fetch 直接访问已知站点"
        lines = [f"[otter] 搜索“{query}”共 {len(results)} 条:"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
        return "\n".join(lines)
