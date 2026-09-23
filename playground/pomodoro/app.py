"""番茄钟:本地 Tkinter 图形界面。

运行:python app.py
无需第三方依赖,Tkinter 随 Python 自带(Windows/macOS 通常已包含)。
"""

import tkinter as tk
from tkinter import messagebox

from timer import Phase, PomodoroTimer, format_time

# 各阶段倒计时的颜色
PHASE_COLOR = {
    Phase.WORK: "#d94f45",        # 番茄红
    Phase.SHORT_BREAK: "#2f9e6e",  # 绿
    Phase.LONG_BREAK: "#2f6f9e",   # 蓝
}


class PomodoroApp:
    """一个最小可用的番茄钟窗口。"""

    def __init__(self, root):
        self.root = root
        self.root.title("番茄钟")
        self.root.resizable(False, False)

        self.timer = PomodoroTimer()

        self.time_var = tk.StringVar()
        self.phase_var = tk.StringVar()
        self.count_var = tk.StringVar()
        self.toggle_var = tk.StringVar(value="开始")

        # 倒计时大字
        self.time_label = tk.Label(
            root, textvariable=self.time_var, font=("Helvetica", 48, "bold")
        )
        self.time_label.pack(padx=40, pady=(24, 4))

        # 阶段名与完成数
        tk.Label(root, textvariable=self.phase_var, font=("Helvetica", 16)).pack()
        tk.Label(root, textvariable=self.count_var, font=("Helvetica", 12)).pack(
            pady=(0, 12)
        )

        # 按钮排
        btns = tk.Frame(root)
        btns.pack(padx=20, pady=(0, 20))
        tk.Button(btns, textvariable=self.toggle_var, width=8, command=self.on_toggle).grid(
            row=0, column=0, padx=4
        )
        tk.Button(btns, text="重置", width=8, command=self.on_reset).grid(
            row=0, column=1, padx=4
        )
        tk.Button(btns, text="跳过", width=8, command=self.on_skip).grid(
            row=0, column=2, padx=4
        )

        # 快捷键:空格 = 开始/暂停,R = 重置
        root.bind("<space>", lambda e: self.on_toggle())
        root.bind("<r>", lambda e: self.on_reset())
        root.bind("<R>", lambda e: self.on_reset())

        self._refresh()
        self.root.after(1000, self._on_tick)

    # ---- 界面刷新 ----
    def _refresh(self):
        """按当前状态刷新所有显示。"""
        self.time_var.set(format_time(self.timer.remaining))
        self.phase_var.set(self.timer.phase.value)
        self.count_var.set(f"已完成番茄:{self.timer.completed_work_count}")
        self.toggle_var.set("暂停" if self.timer.running else "开始")
        self.time_label.config(fg=PHASE_COLOR[self.timer.phase])

    # ---- 定时驱动 ----
    def _on_tick(self):
        """每秒推进一次逻辑;阶段结束则提示并刷新。"""
        event = self.timer.tick(1)
        if event is not None:
            self._notify(event)
        self._refresh()
        self.root.after(1000, self._on_tick)

    def _notify(self, event):
        """阶段结束时的提示:响铃 + 弹窗。"""
        self.root.bell()
        if event == "work_finished":
            message = "专注结束,休息一下。"
        else:
            message = "休息结束,继续专注。"
        # 弹窗阻塞主循环,关闭后继续下一次 after
        messagebox.showinfo("番茄钟", message)

    # ---- 按钮回调 ----
    def on_toggle(self):
        self.timer.toggle()
        self._refresh()

    def on_reset(self):
        self.timer.reset()
        self._refresh()

    def on_skip(self):
        self._notify(self.timer.skip())
        self._refresh()


def main():
    root = tk.Tk()
    PomodoroApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
