"""공용 예외 — 모든 계층이 바라보는 말단(leaf) 모듈.

이 모듈은 다른 budget_app 모듈을 import하지 않는다(순환 import 방지).
오류는 스택트레이스 대신 '원인 + 해결 힌트'로 사용자에게 전달된다.
"""
from __future__ import annotations


class BudgetError(Exception):
    """사용자에게 보여줄 수 있는 오류의 공통 조상."""

    def __init__(self, cause: str, hint: str | None = None) -> None:
        super().__init__(cause)
        self.cause = cause
        self.hint = hint


class ValidationError(BudgetError):
    """입력값이 규칙에 어긋남 (날짜 형식, 음수 금액 등)."""


class StorageError(BudgetError):
    """저장 파일을 읽거나 쓸 수 없음 (손상된 줄 등)."""


class NotFoundError(BudgetError):
    """요청한 데이터가 존재하지 않음 (없는 id 등)."""
