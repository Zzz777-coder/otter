# Python 装饰器三要点

## 要点一:装饰器就是「接收函数、返回函数」的高阶函数

`@decorator` 只是语法糖,等价于 `func = decorator(func)`。所以要写装饰器,记住:参数是函数,返回值必须是一个可调用的新函数。

```python
def shout(func):
    def wrapper():
        return func().upper() + "!"
    return wrapper

@shout
def greet():
    return "hello"

print(greet())        # HELLO!
print(shout(greet))   # 等价写法,结果一样
```

## 要点二:用 functools.wraps 保住原函数的元信息

不加 `wraps` 时,被装饰函数的 `__name__`、`__doc__` 会被替换成 wrapper 的,调试和文档生成都会出错。

```python
import functools

def shout(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs).upper()
    return wrapper

@shout
def greet():
    """打招呼。"""
    return "hello"

print(greet.__name__)   # greet(不加 wraps 时是 wrapper)
print(greet.__doc__)    # 打招呼。
```

## 要点三:用 *args / **kwargs 让装饰器适配任意签名

固定写 `def wrapper():` 只能装饰无参函数。写成 `*args, **kwargs` 并原样透传,才能装饰任意函数,也才能让装饰后的函数继续正常接收参数。

```python
import functools

def log(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        print(f"调用 {func.__name__} args={args} kwargs={kwargs}")
        return func(*args, **kwargs)
    return wrapper

@log
def add(a, b, base=0):
    return a + b + base

print(add(1, 2, base=10))   # 先打印调用信息,再输出 13
```

## 小结

- 装饰器 = 高阶函数,输入函数、输出可调用对象。
- 永远加 `functools.wraps`,保住 `__name__` / `__doc__`。
- wrapper 用 `*args, **kwargs` 透传,兼容任意签名。
- 需要装饰器自己带配置(如超时时间、重试次数)时,再套一层:装饰器工厂返回真正的装饰器——见 `demo.py` 中的 `timer`。

## 常见坑

- 忘了 `return func(*args, **kwargs)`(只调用不返回),原函数的返回值会变成 `None`,而且 `wrapper` 里忘写 `return wrapper` 更直接:被装饰的名字直接成了 `None`。
- 用 `@timer` 这种"能带参"的装饰器,却忘了内部要区分 `func is None` 的两种情况,结果 `@timer(repeat=3)` 报 `'int' object is not callable`——简单起见可以像 `demo.py` 里的 `simple_timer` 一样只做无参版本。
