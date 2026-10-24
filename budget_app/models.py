"""모델 계층 — 데이터의 스키마(구조 + 값 제약)를 정의한다.

'거래란 무엇인가'에 대한 지식은 전부 여기 있다: 필드 구성(Transaction)과
유효한 값의 범위(validate_*)는 같은 스키마의 두 얼굴이다.
저장 매체(파일·JSON)와 사용자 대화(input/print)는 모른다 — import 목록이 증거다.
이 모듈은 상태를 갖지 않는다. 파일은 storage, 업무 절차는 services, 대화는 cli 소관.
"""
from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Literal

from budget_app.errors import ValidationError

TransactionType = Literal['income', 'expense']

TX_ID_PREFIX = 'TX-'
TX_ID_WIDTH = 6


def format_tx_id(number: int) -> str:
    """일련번호를 거래 id 문자열로 만든다 (예: 12 → 'TX-000012')."""
    return f'{TX_ID_PREFIX}{number:0{TX_ID_WIDTH}d}'


def validate_date(value: str) -> str:
    """YYYY-MM-DD 형식인지 검사하고 그대로 돌려준다."""
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValidationError(
            f'날짜 형식이 올바르지 않습니다: {value!r} (YYYY-MM-DD)',
            hint='예: 2026-10-07',
        ) from None
    return value


def validate_year_month(value: str) -> str:
    """'연-월'(YYYY-MM) 형식인지 검사하고 그대로 돌려준다 — 단순 월(MM)이 아니다."""
    try:
        date.fromisoformat(f'{value}-01')
    except ValueError:
        raise ValidationError(
            f'월 형식이 올바르지 않습니다: {value!r} (YYYY-MM)',
            hint='예: 2026-10',
        ) from None
    return value


def month_date_range(year_month: str) -> tuple[str, str]:
    """'YYYY-MM'을 (월초, 월말) 실제 날짜 쌍으로 — 월말은 달마다 다르다(28~31일)."""
    year_month = validate_year_month(year_month)
    year, month = int(year_month[:4]), int(year_month[5:7])
    last_day = calendar.monthrange(year, month)[1]
    return f'{year_month}-01', f'{year_month}-{last_day:02d}'


def validate_type(value: str) -> TransactionType:
    if value not in ('income', 'expense'):
        raise ValidationError(
            f'허용되지 않은 타입입니다: {value!r}',
            hint='income 또는 expense 중 하나를 입력하세요',
        )
    return value  # type: ignore[return-value]


def validate_amount(value: str | int) -> int:
    try:
        amount = int(value)
    except (TypeError, ValueError):
        raise ValidationError(
            f'금액은 정수여야 합니다: {value!r}',
            hint='예: 15000',
        ) from None
    if amount <= 0:
        raise ValidationError(
            f'금액은 양수여야 합니다: {amount}',
            hint='0보다 큰 정수를 입력하세요',
        )
    return amount


# import/export 교환 포맷의 고정 스키마 (명세 지정: UTF-8, 헤더 포함)
CSV_COLUMNS = ('Date', 'Type', 'Category', 'Amount', 'Memo', 'Tags')
CSV_REQUIRED = ('Date', 'Type', 'Category', 'Amount')


def transaction_to_csv_row(tx: 'Transaction') -> list[str]:
    """Transaction을 교환 CSV의 한 행으로 — id는 스키마에 없어 내보내지 않는다."""
    return [tx.date, tx.type, tx.category, str(tx.amount), tx.memo, ','.join(tx.tags)]


def parse_tags(raw: str) -> list[str]:
    """쉼표 구분 태그 입력을 다듬는다: trim · 빈 조각 제거 · 중복 제거.

    dict.fromkeys로 중복을 걸러 입력 순서를 보존한다 (set은 순서를 섞는다).
    """
    parts = (part.strip() for part in raw.split(','))
    return list(dict.fromkeys(part for part in parts if part))


@dataclass
class Transaction:
    """거래 한 건. 필드 구성은 명세의 최소 필드를 따른다."""

    id: str
    type: TransactionType
    date: str  # YYYY-MM-DD
    amount: int  # 양수
    category: str
    memo: str = ''
    tags: list[str] = field(default_factory=list)

    def to_record(self) -> dict:
        """JSONL 한 줄로 저장될 dict 형태로 변환한다."""
        return asdict(self)

    @classmethod
    def from_record(cls, record: dict) -> 'Transaction':
        """저장 파일에서 읽은 dict를 Transaction으로 복원한다."""
        return cls(
            id=record['id'],
            type=record['type'],
            date=record['date'],
            amount=record['amount'],
            category=record['category'],
            memo=record.get('memo', ''),
            tags=list(record.get('tags', [])),
        )
