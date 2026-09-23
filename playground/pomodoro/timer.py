"""番茄钟核心逻辑:阶段状态机 + 倒计时,不依赖任何界面与真实时钟。

设计要点:
- tick(dt) 只按传入的秒数递减,不读系统时间,因此完全可单测。
- 倒计时归零时自动切换到下一阶段,并返回一个事件字符串,
  由界面层决定怎么提示(响铃 / 弹窗)。
"""

from enum import Enum

# 默认时长(秒)
WORK_SECONDS = 25 * 60
SHORT_BREAK_SECONDS = 5 * 60
LONG_BREAK_SECONDS = 15 * 60

# 每完成多少个专注后进入一次长休
ROUNDS_BEFORE_LONG = 4


class Phase(Enum):
    """番茄钟的三个阶段。"""

    WORK = "专注"
    SHORT_BREAK = "短休"
    LONG_BREAK = "长休"


# 每个阶段的默认时长(秒)
_DURATION = {
    Phase.WORK: WORK_SECONDS,
    Phase.SHORT_BREAK: SHORT_BREAK_SECONDS,
    Phase.LONG_BREAK: LONG_BREAK_SECONDS,
}


def format_time(seconds):
    """把剩余秒数格式化成 "MM:SS"。负数按 0 处理。"""
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class PomodoroTimer:
    """一个番茄钟状态机。

    对外状态:
    - phase:当前阶段(Phase)
    - remaining:当前阶段剩余秒数
    - running:是否正在倒计时
    - completed_work_count:已完成的专注个数
    """

    def __init__(self):
        self.reset()

    # ---- 内部辅助 ----
    def _phase_duration(self, phase):
        return _DURATION[phase]

    def _next_phase(self):
        """根据当前阶段与已完成专注数,算出下一个阶段。"""
        if self.phase is Phase.WORK:
            # 刚完成第 N 个专注:N 能被 4 整除则长休,否则短休
            if self.completed_work_count % ROUNDS_BEFORE_LONG == 0:
                return Phase.LONG_BREAK
            return Phase.SHORT_BREAK
        # 休息结束一律回到专注
        return Phase.WORK

    # ---- 对外操作 ----
    def reset(self):
        """回到初始状态:专注阶段、未运行、完成数清零。"""
        self.phase = Phase.WORK
        self.remaining = self._phase_duration(self.phase)
        self.running = False
        self.completed_work_count = 0

    def start(self):
        """开始(或继续)倒计时。"""
        self.running = True

    def pause(self):
        """暂停倒计时。"""
        self.running = False

    def toggle(self):
        """在运行 / 暂停之间切换。"""
        self.running = not self.running

    def skip(self):
        """跳过当前阶段,直接进入下一个阶段(返回触发的事件名)。

        与自然结束一致:跳过专注也会把完成数 +1。
        """
        return self._advance()

    def _advance(self):
        """结束当前阶段并切到下一阶段,返回事件名。"""
        finished = self.phase
        if finished is Phase.WORK:
            self.completed_work_count += 1
            event = "work_finished"
        else:
            event = "break_finished"

        self.phase = self._next_phase()
        self.remaining = self._phase_duration(self.phase)
        return event

    def tick(self, dt=1):
        """推进 dt 秒。

        - 未运行时什么都不做,返回 None。
        - 归零时切换阶段并返回事件名("work_finished" / "break_finished")。
        - 一次 tick 最多只跨越一个阶段边界。
        """
        if not self.running:
            return None

        self.remaining -= dt
        if self.remaining > 0:
            return None

        return self._advance()
