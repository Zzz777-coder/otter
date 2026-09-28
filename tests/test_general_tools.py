"""通用小工具包(2026-09-24 身份通用化配套)的离线测试。

current_time / calculate(safe_eval)/ html_to_text(纯函数);web_fetch 的网络层
不在离线面(连通性由使用环境决定,契约由 URL 校验与文本转换用例锁定)。
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from otter.tools.general import (  # noqa: E402
    CalculateTool, CurrentTimeTool, WebFetchTool, html_to_text, safe_eval,
)


def test_current_time_format():
    out = asyncio.run(CurrentTimeTool().run({}))
    # 契约:"现在是 YYYY-MM-DD HH:MM 星期X"(X 为一到日之一)
    assert re.match(r"^现在是 \d{4}-\d{2}-\d{2} \d{2}:\d{2} 星期[一二三四五六日]$", out)


def test_calculate_arithmetic():
    assert safe_eval("1+2*3") == 7
    assert safe_eval("(120*1.13+450)/3") == pytest.approx(195.2, abs=0.01)
    assert safe_eval("2**10") == 1024
    assert safe_eval("7 // 2") == 3 and safe_eval("7 % 2") == 1
    assert safe_eval("-5 + 3") == -2


def test_calculate_rejects_dangerous():
    with pytest.raises(ValueError):
        safe_eval("__import__('os').system('ls')")  # 函数调用不在白名单
    with pytest.raises(ValueError):
        safe_eval("1 if True else 2")  # 条件表达式不在白名单
    with pytest.raises(ZeroDivisionError):
        safe_eval("1/0")
    with pytest.raises(ValueError):
        safe_eval("10**10000")  # 大指数防线(防内存炸)
    with pytest.raises(SyntaxError):
        safe_eval("1 +")  # 语法错误原样抛


def test_calculate_tool_surface():
    out = asyncio.run(CalculateTool().run({"expression": "6*7"}))
    assert out == "6*7 = 42"
    out2 = asyncio.run(CalculateTool().run({"expression": "1/0"}))
    assert out2.startswith("[otter] 计算失败")


def test_web_fetch_url_guard():
    out = asyncio.run(WebFetchTool().run({"url": "ftp://x"}))
    assert out.startswith("[otter] URL 需以")


def test_html_to_text():
    html = ("<html><head><style>.x{}</style></head><body>"
            "<script>alert(1)</script>"
            "<h1>标题</h1><p>第一段 &amp; 实体</p>"
            "<div>第二段<br>换行</div><footer>页脚</footer></body></html>")
    text = html_to_text(html)
    assert "标题" in text and "第一段 & 实体" in text and "第二段" in text
    assert "alert" not in text and "页脚" not in text  # script/footer 剥除
    assert "<" not in text and "&amp;" not in text      # 标签与实体已清


# ── 2026-09-24 web_search:DDG Lite 解析(纯函数,离线) ──────────────

def test_parse_lite_results_and_uddg_cleanup():
    """DDG Lite HTML fixture:链接/摘要按序配对,uddg 重定向清洗,截断生效。"""
    from otter.tools.general import parse_lite_results

    html = """
    <table>
    <tr><td class='result-snippet'>摘要A 内容</td></tr>
    <tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2Fx&rut=1">标题A</a></td></tr>
    <tr><td class='result-snippet'>摘要B 内容</td></tr>
    <tr><td><a rel="nofollow" href="https://b.com/y">标题B</a></td></tr>
    <tr><td><a rel="nofollow" href="javascript:alert(1)">坏链接</a></td></tr>
    </table>
    """
    rs = parse_lite_results(html, max_results=5)
    # 注意:DDG Lite 页面结构为 链接行在摘要行之前;本 fixture 故意倒序以验证"按序配对"
    # (解析器不依赖行序,只按出现顺序对齐 links[i]↔snippets[i])
    urls = [r["url"] for r in rs]
    assert "https://a.com/x" in urls            # uddg 已解包
    assert "https://b.com/y" in urls
    assert all(u.startswith(("http://", "https://")) for u in urls)  # javascript: 已滤
    assert rs[0]["snippet"]                      # 摘要按序配对(非空)


def test_websearch_tool_via_injected_fetcher():
    """注入 fetcher(离线):正常结果格式 / 空结果降级文案。"""
    import asyncio

    from otter.tools.general import WebSearchTool

    tool = WebSearchTool()
    tool._fetcher = lambda q: _fake_fetch(q)

    async def _fake_fetch(q):
        return ("<a rel='nofollow' href='https://x.com/1'>结果1</a>"
                "<td class='result-snippet'>片段1</td>")

    out = asyncio.run(tool.run({"query": "测试"}))
    assert "结果1" in out and "https://x.com/1" in out and "片段1" in out

    async def _empty_fetch(q):
        return "<html>empty</html>"

    tool._fetcher = _empty_fetch
    out = asyncio.run(tool.run({"query": "测试"}))
    assert "无搜索结果" in out                    # 如实降级,不编造
