"""서비스 계층 — 업무 절차(매뉴얼)를 아는 곳.

"무엇을 어떤 순서로 하는가"만 안다. 값의 옳고 그름은 스스로 판정하지 않고
모델의 검사기(validate_*)를 빌려 쓴다 — 품질 기준은 모델 소관, 절차는 여기 소관.
파일 경로(storage 소관)와 사용자 대화(cli 소관)는 모른다. print 대신 보고서
(InitReport 등)를 돌려주는 이유가 그것이다.
입구(대화형 add, CSV import, …)가 어디든 업무 절차는 여기 한 곳에만 존재한다.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from budget_app import models
from budget_app import storage as st
from budget_app.decorators import logged, timed
from budget_app.errors import ValidationError

# 결정 0-5 부속: 토스 '내 소비' 대분류를 한 단어로 축약한 기본 카테고리.
# 계층 없는 1단 평면 구조 — 세분화는 사용자가 category add로 같은 평면에 추가한다.
DEFAULT_CATEGORIES = (
    '식비', '카페', '유흥', '쇼핑', '교통', '주거', '통신', '의료', '여가', '생활',  # 지출
    '급여', '용돈',  # 수입
    '기타',  # 공용 탈출구
)


@dataclass
class InitReport:
    """첫 실행 준비에서 무슨 일이 있었는지 — 사용자 안내는 cli가 이걸 보고 한다."""

    created_files: list[str] = field(default_factory=list)
    seeded_categories: list[str] = field(default_factory=list)


@timed   # 바깥: 일지 기록 시간까지 포함해 잰다 (중첩 순서 = 결정 1-1 워크스루 참고)
@logged  # 안쪽: 시작/완료/실패 기록
def initialize(store: st.Storage) -> InitReport:
    """저장 파일이 없으면 만들고, 카테고리가 비어 있으면 기본 목록을 심는다."""
    report = InitReport(created_files=store.ensure_files())
    categories_empty = not any(True for _ in store.iter_records(st.CATEGORIES))
    if categories_empty:
        for name in DEFAULT_CATEGORIES:
            store.append_record(st.CATEGORIES, {'name': name})
        report.seeded_categories = list(DEFAULT_CATEGORIES)
    return report


def _recency_key(tx: models.Transaction) -> tuple[str, str]:
    """최신순 정렬 키 (결정 3-2): 날짜 우선, 같으면 id(생성 순) — 둘 다 고정폭 문자열이라 비교 가능."""
    return (tx.date, tx.id)


def _iter_transactions(store: st.Storage):
    """저장 레코드를 Transaction 스트림으로 — 모든 조회의 공통 수원(水源)."""
    return (models.Transaction.from_record(r) for r in store.iter_records(st.TRANSACTIONS))


# ----- 검색 거름망 (결정 4-2: 조건 하나 = 거름망 하나, None이면 통과) -----

def _filter_period(stream, date_from: str | None, date_to: str | None):
    for tx in stream:
        if date_from is not None and tx.date < date_from:
            continue
        if date_to is not None and tx.date > date_to:
            continue
        yield tx


def _filter_type(stream, tx_type: str | None):
    for tx in stream:
        if tx_type is None or tx.type == tx_type:
            yield tx


def _filter_category(stream, category: str | None):
    for tx in stream:
        if category is None or tx.category == category:
            yield tx


def _filter_query(stream, query: str | None):
    """메모 글자 부분일치, 대소문자 무시 (결정 4-3)."""
    needle = query.casefold() if query is not None else None
    for tx in stream:
        if needle is None or needle in tx.memo.casefold():
            yield tx


def _filter_tag(stream, tag: str | None):
    """태그도 부분일치 + 대소문자 무시 (결정 4-3: 메모와 같은 철학으로 통일)."""
    needle = tag.casefold() if tag is not None else None
    for tx in stream:
        if needle is None or any(needle in t.casefold() for t in tx.tags):
            yield tx


@timed
@logged
def search_transactions(
    store: st.Storage,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    tx_type: str | None = None,
    category: str | None = None,
    query: str | None = None,
    tag: str | None = None,
    limit: int | None = None,
) -> list[models.Transaction]:
    """조건 검색 (모든 조건 AND, 결정 4-1). 결과는 최신순.

    거름망 5장을 쌓은 파이프라인 — 각 거래는 한 건씩 흘러가며 거름망마다
    "그 줄만 보면 확정되는" 판정을 받는다. limit 없으면 매칭 전부(메모리 O(결과 수)),
    있으면 선발 명단(O(limit)) (결정 4-4).
    """
    if date_from is not None:
        date_from = models.validate_date(date_from)
    if date_to is not None:
        date_to = models.validate_date(date_to)
    if tx_type is not None:
        tx_type = models.validate_type(tx_type)

    stream = _iter_transactions(store)
    stream = _filter_period(stream, date_from, date_to)
    stream = _filter_type(stream, tx_type)
    stream = _filter_category(stream, category)
    stream = _filter_query(stream, query)
    stream = _filter_tag(stream, tag)

    if limit is not None:
        if limit <= 0:
            raise ValidationError(f'--limit는 양수여야 합니다: {limit}', hint='예: --limit 10')
        return heapq.nlargest(limit, stream, key=_recency_key)
    return sorted(stream, key=_recency_key, reverse=True)


@timed
@logged
def list_transactions(store: st.Storage, limit: int) -> list[models.Transaction]:
    """날짜 기준 최신순 상위 limit건 (결정 3-1/3-2).

    heapq.nlargest는 스트림을 흘려보내며 "지금까지의 상위 limit명 명단"만 유지한다 —
    전체를 다 읽지만(top-N은 끝까지 봐야 확정) 메모리는 O(limit)로 묶인다.
    key=(date, id): 날짜 같으면 id가 큰 쪽(나중 입력)이 위.
    """
    if limit <= 0:
        raise ValidationError(f'--limit는 양수여야 합니다: {limit}', hint='예: --limit 10')
    return heapq.nlargest(limit, _iter_transactions(store), key=_recency_key)


def category_registered(store: st.Storage, name: str) -> bool:
    """카테고리 등록 여부. any()라 일치하는 줄을 만나면 더 읽지 않는다."""
    return any(record.get('name') == name for record in store.iter_records(st.CATEGORIES))


def list_categories(store: st.Storage) -> list[str]:
    return [record['name'] for record in store.iter_records(st.CATEGORIES) if 'name' in record]


@timed
@logged
def add_category(store: st.Storage, name: str) -> str:
    """카테고리를 등록한다. add의 등록 유도(결정 2-3)와 category add가 같이 쓰는 입구."""
    name = name.strip()
    if not name:
        raise ValidationError('카테고리 이름이 비어 있습니다', hint='한 단어 이름을 입력하세요 (예: 식비)')
    if category_registered(store, name):
        raise ValidationError(f'이미 존재하는 카테고리입니다: {name}',
                              hint='category list로 현재 목록을 확인하세요')
    store.append_record(st.CATEGORIES, {'name': name})
    return name


@timed
@logged
def add_transaction(
    store: st.Storage,
    *,
    date: str,
    tx_type: str,
    category: str,
    amount: str | int,
    memo: str = '',
    tags: list[str] | None = None,
) -> models.Transaction:
    """거래 추가의 단일 입구 — 대화형 add든 CSV import든 전부 여기를 지난다.

    검증은 모델의 검사기를 빌려 다시 거친다(CLI가 이미 검증했어도).
    저장 순서(결정 0-4): 카운터 선점 → 거래 append — 중간에 죽어도 번호 구멍만 남는다.
    """
    # 1) 전부 검증 — 하나라도 실패하면 카운터를 건드리기 전에 중단 (번호 낭비 없음)
    valid_type = models.validate_type(tx_type)
    valid_date = models.validate_date(date)
    valid_amount = models.validate_amount(amount)
    if not category_registered(store, category):
        raise ValidationError(f'등록되지 않은 카테고리입니다: {category}',
                              hint='category add로 먼저 등록하세요')
    # 2) 카운터 선점 → 3) append (결정 0-4: 이 사이에 죽으면 중복 대신 무해한 구멍)
    tx = models.Transaction(
        id=models.format_tx_id(store.claim_tx_number()),
        type=valid_type,
        date=valid_date,
        amount=valid_amount,
        category=category,
        memo=memo.strip(),
        tags=list(tags or []),
    )
    store.append_record(st.TRANSACTIONS, tx.to_record())
    return tx
