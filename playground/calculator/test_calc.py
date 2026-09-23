"""calculator.calc 的纯 assert 测试。可直接 python 运行,也兼容 pytest。"""

from calc import CalcError, evaluate, format_result


def test_add():
    assert evaluate("1 + 2") == 3


def test_operator_precedence():
    # 乘法优先于加法
    assert evaluate("1 + 2 * 3") == 7


def test_parentheses():
    assert evaluate("(1 + 2) * 3") == 9


def test_subtract_negative():
    assert evaluate("5 - 8") == -3


def test_divide():
    assert evaluate("10 / 4") == 2.5


def test_floor_divide_and_mod():
    assert evaluate("10 // 3") == 3
    assert evaluate("10 % 3") == 1


def test_power():
    assert evaluate("2 ** 10") == 1024


def test_unary_minus():
    assert evaluate("-5 + 2") == -3


def test_float_input():
    assert evaluate("0.1 + 0.2") == 0.30000000000000004


def test_empty_raises():
    try:
        evaluate("   ")
    except CalcError:
        pass
    else:
        raise AssertionError("空表达式应抛出 CalcError")


def test_bad_syntax_raises():
    try:
        evaluate("1 +")
    except CalcError:
        pass
    else:
        raise AssertionError("语法错误应抛出 CalcError")


def test_name_raises():
    # 变量名 / 函数调用属于不支持的语法
    for expr in ("a + 1", "__import__('os')", "print(1)"):
        try:
            evaluate(expr)
        except CalcError:
            pass
        else:
            raise AssertionError(f"{expr!r} 应抛出 CalcError")


def test_non_string_raises():
    try:
        evaluate(123)
    except TypeError:
        pass
    else:
        raise AssertionError("非字符串应抛出 TypeError")


def test_format_result():
    assert format_result(3) == "3"
    assert format_result(2.0) == "2"
    assert format_result(2.5) == "2.5"
    assert format_result(0.30000000000000004) == "0.3"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n全部通过:{len(tests)} 个用例")
