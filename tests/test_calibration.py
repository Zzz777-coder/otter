"""估算校准器离线测试(2026-10-13):factor 语义(无样本=1.0/累计均值/夹逼)、
小样本与 None 守卫、loop 接线(observe 进真实 usage、水位判断乘 factor)。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from otter.context.estimator import EstimationCalibrator  # noqa: E402
from otter.loop import AgentLoop, STOP_FINAL  # noqa: E402
from otter.models.types import Message, ModelResponse, ModelUsage  # noqa: E402
from otter.tools.base import Tool, ToolRegistry  # noqa: E402


# ── 校准器单元语义 ───────────────────────────────────────────

def test_factor_is_one_before_any_sample():
    """无样本时 factor=1.0——行为与引入校准前完全一致(渐进增强)。"""
    assert EstimationCalibrator().factor == 1.0


def test_factor_is_cumulative_mean():
    """累计均值口径:两组样本 (1000→1300, 3000→3300) → 4600/4000=1.15。"""
    cal = EstimationCalibrator()
    cal.observe(1000, 1300)
    cal.observe(3000, 3300)
    assert cal.samples == 2
    assert abs(cal.factor - 1.15) < 1e-9


def test_factor_clamped_to_bounds():
    """夹逼护栏:真实/估算 = 10.0 的异常样本,factor 也不超过 3.0(下界同理 0.5)。"""
    cal = EstimationCalibrator()
    cal.observe(1000, 10_000)
    assert cal.factor == 3.0
    cal2 = EstimationCalibrator()
    cal2.observe(10_000, 100)  # 0.01 → 夹回 0.5
    assert cal2.factor == 0.5


def test_small_sample_and_none_skipped():
    """估算 <1000 的小请求噪声大跳过;None(厂商未回 usage)跳过——都不进样本。"""
    cal = EstimationCalibrator()
    cal.observe(999, 5000)      # 小样本:跳过
    cal.observe(None, 2000)     # 估算缺失:跳过
    cal.observe(2000, None)     # 真实缺失:跳过
    assert cal.samples == 0 and cal.factor == 1.0


# ── loop 接线 ────────────────────────────────────────────────

class _FakeStore:
    """最小 Store 桩(与 test_loop.py 同款,仅本文件所需面)。"""

    async def new_run(self):
        return 1

    async def finish_run(self, run_id, status, stop_reason):
        pass

    async def append_message(self, msg, seq, run_id, conversation_id=None):
        pass

    async def save_checkpoint(self, *a, **k):
        pass

    async def append_event(self, run_id, type_, payload):
        pass


class _NoopTool(Tool):
    name = "noop"
    description = "测试占位工具"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args):
        return "ok"


class _ScriptedAdapter:
    """回放单次最终响应,并记录收到的请求供断言。"""

    def __init__(self, resp: ModelResponse) -> None:
        self.model = "fake"
        self._resp = resp
        self.seen_views = 0

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        self.seen_views += 1
        if on_text_delta and self._resp.content:
            on_text_delta(self._resp.content)
        return self._resp


def test_loop_feeds_calibration_from_real_usage():
    """跑一个单步 Run:校准器应记到样本,factor 偏离 1.0(真实 input 人为设 1.3 倍量级)。"""
    adapter = _ScriptedAdapter(ModelResponse(content="done", usage=ModelUsage(10_000, 50)))
    registry = ToolRegistry()
    registry.register(_NoopTool())
    loop = AgentLoop(adapter, registry, _FakeStore())
    # 2026-10-16 修:护栏 MIN_SAMPLE_EST=1000 会跳过小请求(BASE_SYSTEM+工具表仅约 659),
    # 原"测试"二字过不了门槛;垫长文使估算 >1000,对齐"真实 Run 上下文远超门槛"的场景
    payload = "校准样本垫高估算用长文本。" * 120
    result = asyncio.run(loop.run([], Message(role="user", content=payload), run_id=1))
    assert result.ok and result.stop_reason == STOP_FINAL
    assert loop.calibrator.samples == 1  # 单次模型调用 → 恰一组样本
    # 估算侧已垫高 >1000,真实 10000 → factor = 10000/est ∈ (0.5,3)
    assert 0.5 <= loop.calibrator.factor <= 3.0
    assert loop.calibrator.factor != 1.0  # 10000 不会恰好等于估算值


def test_loop_calibration_injectable():
    """校准器可注入(测试/诊断场景);注入对象即 loop 使用的同一对象。"""
    cal = EstimationCalibrator()
    adapter = _ScriptedAdapter(ModelResponse(content="done", usage=ModelUsage(1, 1)))
    registry = ToolRegistry()
    loop = AgentLoop(adapter, registry, _FakeStore(), calibrator=cal)
    asyncio.run(loop.run([], Message(role="user", content="测试"), run_id=1))
    assert loop.calibrator is cal
