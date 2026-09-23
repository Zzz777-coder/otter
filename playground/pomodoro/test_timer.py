"""pomodoro.timer 的纯 assert 测试。可直接 python 运行,也兼容 pytest。"""

from timer import (
    LONG_BREAK_SECONDS,
    ROUNDS_BEFORE_LONG,
    SHORT_BREAK_SECONDS,
    WORK_SECONDS,
    Phase,
    PomodoroTimer,
    format_time,
)


def test_initial_state():
    t = PomodoroTimer()
    assert t.phase is Phase.WORK
    assert t.remaining == WORK_SECONDS
    assert t.running is False
    assert t.completed_work_count == 0


def test_tick_only_when_running():
    t = PomodoroTimer()
    # 未开始:tick 不生效
    assert t.tick(5) is None
    assert t.remaining == WORK_SECONDS


def test_tick_decrements():
    t = PomodoroTimer()
    t.start()
    t.tick(1)
    assert t.remaining == WORK_SECONDS - 1


def test_pause_stops_countdown():
    t = PomodoroTimer()
    t.start()
    t.tick(10)
    t.pause()
    t.tick(10)
    assert t.remaining == WORK_SECONDS - 10


def test_work_finished_goes_to_short_break():
    t = PomodoroTimer()
    t.start()
    event = t.tick(WORK_SECONDS)
    assert event == "work_finished"
    assert t.phase is Phase.SHORT_BREAK
    assert t.remaining == SHORT_BREAK_SECONDS
    assert t.completed_work_count == 1


def test_break_finished_goes_back_to_work():
    t = PomodoroTimer()
    t.start()
    t.tick(WORK_SECONDS)  # 完成第 1 个专注 -> 短休
    event = t.tick(SHORT_BREAK_SECONDS)
    assert event == "break_finished"
    assert t.phase is Phase.WORK
    assert t.remaining == WORK_SECONDS


def test_long_break_after_fourth_work():
    t = PomodoroTimer()
    t.start()
    for i in range(ROUNDS_BEFORE_LONG):
        t.tick(WORK_SECONDS)  # 完成专注
        if i < ROUNDS_BEFORE_LONG - 1:
            t.tick(SHORT_BREAK_SECONDS)  # 前三次走短休
    assert t.completed_work_count == ROUNDS_BEFORE_LONG
    assert t.phase is Phase.LONG_BREAK
    assert t.remaining == LONG_BREAK_SECONDS


def test_skip_work_counts_as_completed():
    t = PomodoroTimer()
    event = t.skip()
    assert event == "work_finished"
    assert t.completed_work_count == 1
    assert t.phase is Phase.SHORT_BREAK


def test_tick_crosses_only_one_boundary():
    t = PomodoroTimer()
    t.start()
    # 一次给足两个阶段的量,也只切换一个阶段
    event = t.tick(WORK_SECONDS + SHORT_BREAK_SECONDS)
    assert event == "work_finished"
    assert t.phase is Phase.SHORT_BREAK


def test_reset_restores_initial():
    t = PomodoroTimer()
    t.start()
    t.tick(WORK_SECONDS)
    t.reset()
    assert t.phase is Phase.WORK
    assert t.remaining == WORK_SECONDS
    assert t.running is False
    assert t.completed_work_count == 0


def test_toggle():
    t = PomodoroTimer()
    t.toggle()
    assert t.running is True
    t.toggle()
    assert t.running is False


def test_format_time():
    assert format_time(0) == "00:00"
    assert format_time(59) == "00:59"
    assert format_time(60) == "01:00"
    assert format_time(WORK_SECONDS) == "25:00"
    assert format_time(-5) == "00:00"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n全部通过:{len(tests)} 个用例")
