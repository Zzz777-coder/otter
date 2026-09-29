"""带参数的装饰器示例:计时器 timer。

要点回顾:
    @timer                 -> 无参用法
    @timer(repeat=3)       -> 带参用法,此时 timer 是"装饰器工厂":
                              timer(...) 返回真正的装饰器,再由它装饰函数。
"""

import functools
import time


def timer(func=None, *, repeat=1, unit="ms", precision=3):
    """统计函数执行耗时的装饰器,支持带参和不带参两种写法。

    用法:
        @timer                       # 不带参
        @timer(repeat=5)             # 带参:重复执行 5 次取平均
        @timer(repeat=3, unit="s")   # 带参:单位换成秒

    :param func: 被装饰的函数;直接 @timer 时由 Python 自动传入。
    :param repeat: 重复执行次数,取总耗时的平均值,默认 1。
    :param unit: 耗时单位,可选 "s" / "ms" / "us",默认 "ms"。
    :param precision: 小数位数,默认 3。
    """

    # ---- 不带参用法:@timer -> 这里 func 就是被装饰的函数,直接包一层返回 ----
    if func is not None:
        return timer(repeat=repeat, unit=unit, precision=precision)(func)

    # ---- 带参用法:返回真正的装饰器 ----
    def decorator(fn):
        @functools.wraps(fn)  # 保住 fn 的 __name__ / __doc__
        def wrapper(*args, **kwargs):  # 透传任意签名
            if repeat < 1:
                raise ValueError("repeat 必须 >= 1")

            start = time.perf_counter()
            result = None
            for _ in range(repeat):
                result = fn(*args, **kwargs)
            elapsed = (time.perf_counter() - start) / repeat

            scale = {"s": 1.0, "ms": 1e3, "us": 1e6}[unit]
            print(
                f"[timer] {fn.__name__} 调用 {repeat} 次,"
                f"平均耗时 {elapsed * scale:.{precision}f}{unit}"
            )
            return result

        return wrapper

    return decorator


# --------------------------------------------------------------------------
# 使用示例
# --------------------------------------------------------------------------
@timer
def no_args_case(n=200_000):
    """不带参用法:@timer。"""
    return sum(i * i for i in range(n))


@timer(repeat=3)
def with_args_case(n=100_000):
    """带参用法:@timer(repeat=3),重复 3 次取平均。"""
    return sum(i * i for i in range(n))


@timer(repeat=2, unit="s", precision=6)
def unit_case(delay=0.02):
    """带参用法:换单位成秒、保留 6 位小数。"""
    time.sleep(delay)
    return "done"


@timer(repeat=2)
def has_params(a, b, *, op="+"):
    """被装饰函数自带参数,验证 wrapper 的 *args/**kwargs 透传是否正常。"""
    return a + b if op == "+" else a * b


if __name__ == "__main__":
    print("结果:", no_args_case())
    print("结果:", with_args_case())
    print("结果:", unit_case())
    print("结果:", has_params(3, 4, op="*"))

    # 元信息没被吃掉
    print("被装饰后仍保留原函数名:", has_params.__name__, "|", has_params.__doc__)

    # 参数校验
    try:
        timer(repeat=0)(lambda: None)()
    except ValueError as exc:
        print("参数校验生效:", exc)
