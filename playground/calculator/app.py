"""最基础计算器:本地 Tkinter 图形界面。

运行:python app.py
无需第三方依赖,Tkinter 随 Python 自带(Windows/macOS 通常已包含)。
"""

import tkinter as tk

from calc import CalcError, evaluate, format_result

# 按键布局:每行一个列表
BUTTONS = [
    ["C", "(", ")", "/"],
    ["7", "8", "9", "*"],
    ["4", "5", "6", "-"],
    ["1", "2", "3", "+"],
    ["0", ".", "<-", "="],
]


class CalculatorApp:
    """一个最小可用的计算器窗口。"""

    def __init__(self, root):
        self.root = root
        self.root.title("最基础计算器")
        self.root.resizable(False, False)

        # 显示区:表达式 + 结果
        self.expr_var = tk.StringVar()
        self.result_var = tk.StringVar(value="0")

        display = tk.Frame(root)
        display.grid(row=0, column=0, columnspan=4, sticky="nsew", padx=8, pady=8)

        tk.Entry(
            display,
            textvariable=self.expr_var,
            font=("Helvetica", 16),
            justify="right",
            width=18,
        ).pack(fill="x")

        tk.Label(
            display,
            textvariable=self.result_var,
            font=("Helvetica", 20, "bold"),
            anchor="e",
        ).pack(fill="x")

        # 按键区
        for r, row in enumerate(BUTTONS):
            for c, label in enumerate(row):
                tk.Button(
                    root,
                    text=label,
                    width=5,
                    height=2,
                    font=("Helvetica", 14),
                    command=lambda lb=label: self.on_press(lb),
                ).grid(row=r + 1, column=c, padx=2, pady=2, sticky="nsew")

        # 键盘支持:回车求值,Backspace 退格,Esc 清空
        root.bind("<Return>", lambda e: self.on_press("="))
        root.bind("<KP_Enter>", lambda e: self.on_press("="))
        root.bind("<BackSpace>", lambda e: self.on_press("<-"))
        root.bind("<Escape>", lambda e: self.on_press("C"))

    def on_press(self, label):
        """处理一次按键。"""
        if label == "C":
            self.expr_var.set("")
            self.result_var.set("0")
            return

        if label == "<-":
            self.expr_var.set(self.expr_var.get()[:-1])
            return

        if label == "=":
            self.calculate()
            return

        # 数字与运算符:追加到表达式
        self.expr_var.set(self.expr_var.get() + label)

    def calculate(self):
        """求值并把结果显示到下方;出错时显示错误信息。"""
        expr = self.expr_var.get().strip()
        if not expr:
            return
        try:
            result = format_result(evaluate(expr))
        except CalcError as err:
            self.result_var.set(f"错误: {err}")
        else:
            self.result_var.set(result)
            # 把表达式替换为结果,方便继续计算
            self.expr_var.set(result)


def main():
    root = tk.Tk()
    CalculatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
