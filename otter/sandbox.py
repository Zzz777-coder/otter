"""bash 沙箱层(M2,说明书 5.5 第三层)。

沙箱取向(平台 Backend + fail-closed:无法强制即拒绝,
绝不静默降级),全部重新实现:
- SeatbeltBackend:macOS sandbox-exec,最小 profile = 写权限限本目录(先禁全 home 再放开 cwd),
  拒写 .git、拒读 .env;网络 M2 暂放行(取舍注释:全断会让 pip/curl 类任务全部失败,
  收紧留 M2.1 按域名白名单做);
- NullBackend:显式关闭(OTTER_SANDBOX=off),工具结果会标注"未沙箱";
- available() 检查失败 → 上层直接拒绝执行(fail-closed)。
"""

from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path


class SandboxBackend(ABC):
    name = "abstract"

    @abstractmethod
    def available(self) -> tuple[bool, str]: ...

    @abstractmethod
    def wrap(self, command: str) -> str:
        """把用户命令包装为实际执行的命令串。"""


class NullSandbox(SandboxBackend):
    """显式关闭(仅当用户配置 OTTER_SANDBOX=off)。"""

    name = "off"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def wrap(self, command: str) -> str:
        return command


class SeatbeltSandbox(SandboxBackend):
    """macOS sandbox-exec 最小 profile:写限于 cwd,拒写 .git、拒读 .env。"""

    name = "seatbelt"

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path.cwd()).resolve()
        self._profile: str | None = None

    def available(self) -> tuple[bool, str]:
        if shutil.which("sandbox-exec") is None:
            return False, "未找到 /usr/bin/sandbox-exec"
        try:
            probe = self._build_profile()
        except Exception as exc:  # profile 生成失败 = 平台无法强制 → 拒绝(fail-closed)
            return False, f"profile 生成失败:{exc}"
        # 干跑校验:sandbox-exec -p <profile> true(允许 true 执行且立即退出)
        proc = subprocess.run(
            ["sandbox-exec", "-p", probe, "/usr/bin/true"],
            capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            return False, f"profile 校验失败:{proc.stderr.decode()[:200]}"
        return True, ""

    def _build_profile(self) -> str:
        if self._profile is None:
            home = str(Path.home())
            cwd = str(self.root)
            # 修正(2026-09-22):SBPL 是 first-match 语义——(allow default) 必须放最后,
            # 特定规则按"最特定优先"排序,否则兜底短路一切(deny 永远不生效)
            self._profile = f"""(version 1)
(deny file-write-data (subpath "{cwd}/.git"))
(deny file-read-data (subpath "{cwd}/.env"))
(allow file-write-data (subpath "{cwd}"))
(deny file-write-data (subpath "{home}"))
(allow default)
"""
        return self._profile

    def wrap(self, command: str) -> str:
        import shlex

        return f"sandbox-exec -p {shlex.quote(self._build_profile())} /bin/zsh -c {shlex.quote(command)}"


def make_sandbox(setting: str, root: Path | None = None) -> SandboxBackend:
    """按配置装配:off → Null;on/auto → Seatbelt(macOS 之外 available=False 由上层拒)。"""
    setting = (setting or "on").lower()
    if setting in ("off", "no", "0", "false"):
        return NullSandbox()
    return SeatbeltSandbox(root)
