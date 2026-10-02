"""模型调用统一重试层(2026-10-02 新增):指数退避 + 流保护 + 耗尽语义。

背景:此前 openai_compat 依赖 SDK 内建 max_retries=2,anthropic 裸 httpx 零重试——
loop/压缩/记忆反思/技能提炼各调用点遇 429/5xx/网络抖动直接异常上抛,长任务几十步
进度因一次瞬态失败全部终止,且两条 adapter 行为不一致。本层把"可重试判定 + 指数
退避 + 重试上限"收敛到一处,经 build_adapter 工厂统一包装,所有调用点自动受益。

设计要点:
- 可重试 = 瞬态:429/408/5xx,或连接/超时类网络错误;4xx(参数错/鉴权错)重试
  无意义,立即上抛;
- 流保护:一旦 on_text_delta 被调过(输出已开始流给用户/界面),本轮不再重试——
  重放会造成 REPL/GUI 重复文本,宁可将错就错上抛;
- 退避:base_delay * 2**attempt + ≤20% 抖动(默认 1s/2s/4s 三档),防雪崩式压端点;
- 耗尽抛 ModelRetryExhausted(带尝试次数与原始异常),loop 据此在 MODEL_ERROR
  事件里给出"端点可能不健康"提示;
- retry_stats 计数器暴露总重试次数/最近错误,诊断与测试可读(不接事件系统,
  保持本层零依赖);
- sleep 可注入:离线测试零等待。
"""

from __future__ import annotations

import asyncio
import random

import httpx2 as httpx  # 与 anthropic adapter 同源:它抛的 TransportError/HTTPStatusError 才能被本层 isinstance 判中(venv 里另有 openai SDK 依赖的独立 httpx,两者异常类不同源)

# 状态码级可重试集合(网络类异常另行按类型判定)
RETRYABLE_STATUS = {408, 429}


class ModelRetryExhausted(RuntimeError):
    """重试预算耗尽(2026-10-02):携带尝试次数与原始异常,提示端点可能不健康。"""

    def __init__(self, attempts: int, last_error: Exception) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"连续 {attempts} 次调用失败(端点可能不健康,检查网络/密钥/服务商状态):{last_error}")


def _status_code(exc: Exception) -> int | None:
    """兼容 httpx.HTTPStatusError 与 openai.APIStatusError 两族(均可读 status_code)。"""
    sc = getattr(exc, "status_code", None)
    if sc is None:
        resp = getattr(exc, "response", None)
        sc = getattr(resp, "status_code", None)
    return sc


def is_retryable(exc: Exception) -> bool:
    """瞬态失败才值得重试:网络/超时类(连接未建立或读断),或 408/429/5xx。

    4xx(参数错/鉴权错/请求格式错)重试也不会好,归为不可重试。"""
    if isinstance(exc, httpx.TransportError):  # 含 TimeoutException/ConnectError/ReadError 等
        return True
    try:
        import openai

        if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
            return True
    except ImportError:  # pragma: no cover - openai 为硬依赖,防御性分支
        pass
    sc = _status_code(exc)
    return sc is not None and (sc in RETRYABLE_STATUS or 500 <= sc < 600)


async def complete_stream_with_retry(
    adapter,
    messages,
    tools=None,
    on_text_delta=None,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    on_retry=None,
    sleep=None,
):
    """complete_stream 的统一重试包装(2026-10-02)。

    on_retry(attempt, delay, error) 每次退避前回调(同步或异步均可),供上层
    记账/发事件;耗尽抛 ModelRetryExhausted,不可重试或已开始流送则原样上抛。"""
    sleep = sleep or asyncio.sleep
    streamed = False

    def _delta(text: str) -> None:
        nonlocal streamed
        streamed = True
        if on_text_delta is not None:
            on_text_delta(text)

    for attempt in range(max_retries + 1):
        try:
            return await adapter.complete_stream(messages, tools, _delta)
        except Exception as exc:
            if streamed:
                raise  # 流保护:输出已开始,重放会造成重复文本
            if not is_retryable(exc):
                raise
            if attempt >= max_retries:
                raise ModelRetryExhausted(max_retries + 1, exc) from exc
            delay = base_delay * (2 ** attempt) * (1 + random.uniform(0, 0.2))
            if on_retry is not None:
                result = on_retry(attempt + 1, delay, exc)
                if asyncio.iscoroutine(result):
                    await result
            await sleep(delay)
    raise AssertionError("unreachable")  # 防御:循环内必经 return/raise


class RetryingAdapter:
    """adapter 重试包装(2026-10-02):build_adapter 工厂统一套上,调用方无感。

    loop/summarizer/memory_runtime/skills 等所有模型调用点此前各自裸调
    complete_stream,重试语义收敛在此;retry_stats 记录总重试次数与最近一次
    错误,诊断时可直接读(不接事件系统,保持零依赖)。"""

    def __init__(self, inner, max_retries: int = 3, base_delay: float = 1.0) -> None:
        self._inner = inner
        self._max_retries = max_retries
        self._base_delay = base_delay
        self.retry_stats = {"total_retries": 0, "last_error": None, "last_delay_s": None}

    @property
    def model(self) -> str:
        return self._inner.model  # 诊断/展示常用字段显式代理

    def __getattr__(self, name):
        return getattr(self._inner, name)  # 其余属性透传(如 Anthropic 的私有配置)

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        def _on_retry(attempt: int, delay: float, error: Exception) -> None:
            self.retry_stats["total_retries"] += 1
            self.retry_stats["last_error"] = str(error)[:200]
            self.retry_stats["last_delay_s"] = round(delay, 2)

        return await complete_stream_with_retry(
            self._inner, messages, tools, on_text_delta,
            max_retries=self._max_retries, base_delay=self._base_delay,
            on_retry=_on_retry,
        )

    async def close(self) -> None:
        await self._inner.close()
