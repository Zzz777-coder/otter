"""机制开关离线测试(2026-10-02):OTTER_COMPRESS / OTTER_MEMORY 的布尔解析与默认值。

开关用途:ablation 对照实验——装配层据此不建 summarizer / 不传 memory_bundle,
loop 代码零改动。此处锁定配置面;装配分支的正确性由 ablation 实跑冒烟覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from otter.config import Config  # noqa: E402


def _load_with_env(monkeypatch, env: dict) -> Config:
    for k in ("OTTER_COMPRESS", "OTTER_MEMORY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Config.load()


def test_switches_default_on(monkeypatch):
    """未设置时两开关默认开(行为与历史版本完全一致)。"""
    cfg = _load_with_env(monkeypatch, {})
    assert cfg.compress is True and cfg.memory is True


def test_switches_off_forms(monkeypatch):
    """0 / false / no 三种关法均可解析(与 OTTER_GIT_AUTO 同一约定)。"""
    for off in ("0", "false", "no"):
        cfg = _load_with_env(monkeypatch, {"OTTER_COMPRESS": off, "OTTER_MEMORY": off})
        assert cfg.compress is False and cfg.memory is False


def test_switches_independent(monkeypatch):
    """两开关互不影响:只关压缩时记忆仍开。"""
    cfg = _load_with_env(monkeypatch, {"OTTER_COMPRESS": "0"})
    assert cfg.compress is False and cfg.memory is True
