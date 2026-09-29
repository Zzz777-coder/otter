"""装饰器示例:无参版本的 simple_timer,以及带参版本的 timer。

要点回顾:
    @simple_timer          -> 纯无参装饰器,写法最简单,不能带参数
    @timer                 -> 无参用法
    @timer(repeat=3)       -> 带参用法,此时 timer 是"装饰器工厂":
                              timer(...) 返回真正的装饰器,再由它装饰函数。
"""

import functools
import time


# --------------------------------------------------------------------------
# 无参版本:只能写成 @simple_timer,不支持 @simple_timer(...)
# --------------------------------------------------------------------------
def simple_timer(func):
    """最简单的计时装饰器:不带任何参数,固定输出毫秒耗时。

    用法:
        @simple_timer
        def work(): ...

    注意:它不能带参调用,写 @simple_timer(repeat=3) 会直接报错,
    因为 simple_timer 的参数 func 收到了 3 这个整数,而不是函数。

    :param func: 被装饰的函数,直接 @simple_timer 时由 Python 自动传入。
    """

    @functools.wraps(func)  # 保住 fn 的 __name__ / __doc__
    def wrapper(*args, **kwargs):  # 透传任意签名
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            # 用 finally:即使原函数抛异常,也把已耗时打出来
            elapsed_ms = (time.perf_counter() - start) * 1e3
            print(f"[simple_timer] {func.__name__} 耗时 {elapsed_ms:.3f}ms")

    return wrapper


# --------------------------------------------------------------------------
# 带参版本:装饰器工厂,支持 @timer 和 @timer(repeat=3, unit="s")
# --------------------------------------------------------------------------
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
@simple_timer
def simple_case(n=150_000):
    """无参版本:@simple_timer,固定按毫秒打印。"""
    return sum(i * i for i in range(n))


@simple_timer
def simple_case_with_params(a, b, *, op="+"):
    """无参版本 + 被装饰函数自带参数,验证签名透传。"""
    return a + b if op == "+" else a * b


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
    print("结果:", simple_case())
    print("结果:", simple_case_with_params(3, 4, op="*"))
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
