"""``mcdreforged.api.decorator`` 的桩：``new_thread`` / ``event_listener``。

桩的 ``new_thread`` 直接在**当前线程**同步调用（不真的起线程），
这样测试里断言输出顺序是确定的；真实 MCDR 才会真的丢到后台线程。
"""

from __future__ import annotations

import functools
from typing import Any, Callable


def new_thread(name: str = "") -> Callable:
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper.__thread_name__ = name  # type: ignore[attr-defined]
        return wrapper

    return decorator


def event_listener(event: Any = None, *args: Any, **kwargs: Any) -> Callable:
    def decorator(func: Callable) -> Callable:
        return func

    return decorator
