"""配置加载:环境变量 > ./.env > ~/.otter/.env。

M0 有意不引入 pydantic-settings(少依赖);配置面只有四项。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """极简 .env 解析(KEY=VALUE,# 开头为注释),不覆盖已存在的环境变量。"""
    candidates = [Path.cwd() / ".env", Path.home() / ".otter" / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:  # 环境变量优先,不覆盖
                os.environ[key] = value
        break  # 只加载第一个命中的文件,避免多份配置叠加


@dataclass
class Config:
    base_url: str
    api_key: str
    model: str
    max_steps: int
    # ── M1 新增(2026-09-22):上下文压缩 / git / 摘要模型配置 ──
    context_budget: int = 50_000   # 日常输入预算(token,估算口径)
    trigger_ratio: float = 0.8     # 达到预算的该比例触发滚动摘要
    keep_recent: int = 10          # 压缩时保留的最近消息条数
    summary_model: str = ""        # 摘要模型(空=复用 main 模型)
    git_auto: bool = True          # 写类工具成功后自动 git commit
    sandbox: str = "off"           # 2026-09-23 用户要求:agent 可访问任意路径,沙箱默认关闭
    run_budget: int = 300_000      # Run 级费用预算(计费 token:input+output;0=禁用)——2026-09-22 补装

    @classmethod
    def load(cls) -> "Config":
        _load_dotenv()
        return cls(
            base_url=os.environ.get("OTTER_BASE_URL", "https://api.deepseek.com"),
            api_key=os.environ.get("OTTER_API_KEY", ""),
            model=os.environ.get("OTTER_MODEL", "deepseek-v4-flash"),
            max_steps=int(os.environ.get("OTTER_MAX_STEPS", "30")),
            context_budget=int(os.environ.get("OTTER_CONTEXT_BUDGET", "50000")),
            trigger_ratio=float(os.environ.get("OTTER_TRIGGER_RATIO", "0.8")),
            keep_recent=int(os.environ.get("OTTER_KEEP_RECENT", "10")),
            summary_model=os.environ.get("OTTER_SUMMARY_MODEL", ""),
            git_auto=os.environ.get("OTTER_GIT_AUTO", "1") not in ("0", "false", "no"),
            sandbox=os.environ.get("OTTER_SANDBOX", "on"),
            run_budget=int(os.environ.get("OTTER_RUN_BUDGET", "300000")),
        )
