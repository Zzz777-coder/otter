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
