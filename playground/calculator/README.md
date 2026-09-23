# 最基础计算器

一个本地小计算器,零第三方依赖。分三层:

- `calc.py`:纯计算逻辑,安全解析表达式(用 `ast`,不用 `eval`)。
- `test_calc.py`:测试用例。
- `app.py`:Tkinter 图形界面。

## 图形界面

```bash
cd calculator
python app.py
```

弹出窗口后,点按钮或直接敲键盘:

| 按键 | 作用 |
|---|---|
| `Enter` | 计算 |
| `Backspace` | 退格 |
| `Esc` | 清空 |

支持的运算:`+ - * / // % **` 以及括号。

## 命令行模式

不想开窗口的话,直接跑逻辑文件,进入交互式:

```bash
cd calculator
python calc.py
```

```text
最基础计算器(输入 q 退出)
> 1 + 2 * 3
7
> (1 + 2) * 3
9
> q
```

## 跑测试

```bash
cd calculator
python test_calc.py       # 直接运行
# 或
pytest test_calc.py       # 用 pytest
```

## 说明

- 计算逻辑不执行任意代码:`calc.evaluate` 只认数字和受支持的运算符,像 `__import__('os')` 这种会被拒绝并抛出 `CalcError`。
- 界面代码依赖 Tkinter。个别 Linux 发行版需先 `apt install python3-tk`;Windows/macOS 的官方 Python 一般自带。
