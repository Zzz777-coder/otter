"""事前 token 估算(tiktoken,cl100k 近似口径)。

口径声明(说明书 3.3"两套计数器"):这里的数字只用于"要不要压缩"的预防决策,
成本问责一律以厂商返回的真实 usage 为准;tiktoken 对 DeepSeek 等非 OpenAI 模型
是近似值,偏差可接受(触发线上留了 20% 余量)。
"""

from __future__ import annotations

from typing import Any

import tiktoken

_enc: tiktoken.Encoding | None = None
_enc_failed = False  # 修正(2026-09-23):tiktoken 首次用需联网下载编码文件——离线/被墙时
                      # get_encoding 抛异常,降级为字符近似(中文≈1.6字/token),测试必须全离线


def _encoding() -> tiktoken.Encoding | None:
    global _enc, _enc_failed
    if _enc is None and not _enc_failed:
        try:
            _enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _enc_failed = True  # 无网/被墙:降级,不再重试
    return _enc


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _encoding()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    # 字符近似(估算口径,只管"要不要压缩"的预防决策):中文≈1.6 字/token,ASCII≈4 字符/token
    cjk = sum(1 for ch in text if '一' <= ch <= '鿿')
    return round(cjk / 1.6 + (len(text) - cjk) / 4) or 1


def _tool_calls_tokens(tool_calls: list[Any]) -> int:
    """工具调用的 JSON 近似:序列化后估算。"""
    import json

    if not tool_calls:
        return 0
    return estimate_tokens(json.dumps(
        [{"name": t.name, "arguments": t.arguments} for t in tool_calls], ensure_ascii=False
    ))


def estimate_messages_tokens(messages: list[Any], tools_json: str = "") -> int:
    """估算一次请求的输入规模:每条消息 content + 工具调用 JSON + 每条固定开销 4。"""
    total = estimate_tokens(tools_json) if tools_json else 0
    for m in messages:
        total += 4  # role/结构开销的粗略常数
        total += estimate_tokens(m.content or "")
        total += _tool_calls_tokens(getattr(m, "tool_calls", None) or [])
    return total
