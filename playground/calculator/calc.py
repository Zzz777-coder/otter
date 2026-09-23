"""最基础计算器:安全地解析并计算四则运算表达式。

不直接使用 eval,而是把表达式解析成 AST 后逐节点计算,
只允许数字与受支持的运算符,避免执行任意代码。
"""

import ast
import operator

# 支持的二元运算符
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# 支持的一元运算符(正负号)
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class CalcError(ValueError):
    """表达式非法或无法计算时抛出。"""


def _eval_node(node):
    """递归计算单个 AST 节点。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant):
        # 排除 True/False 这类 bool(它是 int 的子类)
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError("只支持数字")
        return node.value

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise CalcError("不支持的运算符")
        return op(_eval_node(node.left), _eval_node(node.right))

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise CalcError("不支持的一元运算符")
        return op(_eval_node(node.operand))

    raise CalcError("表达式包含不支持的语法")


def evaluate(expr):
    """计算字符串表达式并返回数值结果。

    支持:+ - * / // % ** 与括号、正负号。
    例如:evaluate("1 + 2 * 3") -> 7
    """
    if not isinstance(expr, str):
        raise TypeError("expr 必须是字符串")

    expr = expr.strip()
    if not expr:
        raise CalcError("表达式为空")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as err:
        raise CalcError(f"表达式无法解析: {err.msg}") from err

    return _eval_node(tree)


def format_result(value):
    """把计算结果格式化成便于显示的字符串。

    整数不带小数点;浮点数去掉多余的尾随零。
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        # 去掉浮点误差造成的长尾,例如 0.30000000000000004
        return f"{value:.10g}"
    return str(value)


if __name__ == "__main__":
    # 简单命令行模式:输入表达式回车即算,输入 q 退出
    print("最基础计算器(输入 q 退出)")
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if line in ("q", "quit", "exit"):
            break
        if not line:
            continue
        try:
            print(format_result(evaluate(line)))
        except CalcError as err:
            print(f"错误: {err}")
