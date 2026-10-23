"""공통 관심사 데코레이터 — 실행 일지(@logged) · 시간 측정(@timed) · 예외 번역(@translates_oserrors).

말단 모듈: budget_app 안에서는 errors만 import한다(순환 방지).
공통 규칙: 데코레이터는 기록만 하고 예외를 삼키지 않는다 —
최종 변환·출력은 단일 관문(cli.main) 한 곳에서 한다.

동작 시점:
- `@decorator` 구문은 실행문이다. 모듈이 import되어 상단부터 실행될 때
  `f = timed(logged(f))` 치환이 즉시 수행된다. 이 시점에는 래퍼 조립만 일어나며
  대상 함수의 본문은 실행되지 않는다.
- 데코레이트된 함수를 호출하면 바깥 래퍼부터 안쪽 순서로 실행이 전파되어
  원본 본문에 도달하고, 반환 시 역순으로 각 래퍼의 후처리가 수행된다.
- wrapper는 원본 함수에 대한 참조를 클로저로 유지한다. functools.wraps는
  원본의 __name__·__doc__ 등 메타데이터를 wrapper로 복사한다 — 로그에
  wrapper가 아닌 원본 함수명이 기록되는 근거.
"""
from __future__ import annotations

import functools
import inspect
import logging
import time
from pathlib import Path
from typing import Any, Callable

from budget_app.errors import StorageError

logger = logging.getLogger('budget_app')

LOG_FORMAT = '%(asctime)s %(levelname)-5s %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


def setup_logging(log_path: Path) -> None:
    """실행 일지 설비: data 폴더의 app.log에 레벨 필드와 함께 기록한다."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding='utf-8')
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def logged(func: Callable) -> Callable:
    """호출·완료·실패를 일지에 기록한다. 실패는 기록만 하고 그대로 올려보낸다."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        logger.info('%s 시작', func.__name__)
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            logger.error('%s 실패: %s', func.__name__, type(exc).__name__)
            raise
        logger.info('%s 완료', func.__name__)
        return result

    return wrapper


def timed(func: Callable) -> Callable:
    """소요 시간을 일지에 기록한다.

    @logged 바깥(위)에 두면 일지 기록 시간까지 포함해 잰다 — 중첩 순서가 의미를 가진다.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info('%s: %.1fms', func.__name__, elapsed_ms)
        return result

    return wrapper


def translates_oserrors(func: Callable) -> Callable:
    """OSError 계열을 StorageError(원인+힌트)로 번역한다. storage 계층 전용.

    완화책: OSError만 잡는다(버그 은폐 금지), `from exc`로 원인 사슬 보존.
    제너레이터 함수는 몸체가 첫 next()에야 실행되므로(lazy), 같은 try로 감싸려면
    wrapper 자신도 제너레이터여야 한다 — 두 형태를 구분해 감싼다.
    """

    def translate(exc: OSError) -> StorageError:
        target = exc.filename or '저장 파일'
        return StorageError(
            f'{target} 파일 작업을 수행할 수 없습니다 ({exc.strerror or type(exc).__name__})',
            hint='data 폴더가 존재하는지, 읽기/쓰기 권한이 있는지 확인하세요',
        )

    if inspect.isgeneratorfunction(func):

        @functools.wraps(func)
        def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                yield from func(*args, **kwargs)
            except OSError as exc:
                raise translate(exc) from exc

        return gen_wrapper

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except OSError as exc:
            raise translate(exc) from exc

    return wrapper
