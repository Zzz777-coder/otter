"""otter — 轻量级本地通用 AI 助手(能写代码、读写文件、跑命令、查资料、做文档;2026-09-24 身份通用化)。

v0.6(2026-09-29)起提供库形态:外部项目 import otter 直接把引擎当库用。
公共 API 面(2026-09-29 P1 定稿):
    build_engine / Engine   库形态装配入口(otter.engine)
    AgentLoop               执行核心(otter.loop)
    Store                   SQLite 持久层(otter.store)
    Config                  配置加载(otter.config)
    build_full / build_gate / make_sandbox   细粒度装配(otter.assembly)
    build_adapter           模型接入工厂(otter.models)
HTTP API(FastAPI,需安装 server extras)与 GUI(pywebview)为可选层,惰性导入:
    from otter.api import create_app, serve
    from otter.gui import launch_gui

产品说明书:~/Desktop/otter-产品说明书.md(本项目的唯一准绳)。
"""

from otter.assembly import build_full, build_gate, make_sandbox
from otter.config import Config
from otter.engine import Engine, build_engine
from otter.loop import AgentLoop
from otter.models import build_adapter
from otter.store import Store

__version__ = "0.6.0"  # 2026-09-29 P1 库化 bump(单源:pyproject 经 dynamic 引用此值)

__all__ = [
    "AgentLoop", "Config", "Engine", "Store",
    "build_adapter", "build_engine", "build_full", "build_gate", "make_sandbox",
    "__version__",
]
