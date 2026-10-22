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
#
# 동작 방식 (섹션 4에서 확립):
# - 각 필터는 제너레이터 함수다. 체인 조립(stream = _filter_x(stream, ...)의 연쇄 =
#   함수 합성 f(g(h(x)))의 축차 표기) 시점에는 아무것도 실행·판정되지 않는다.
# - 소비가 시작되면 레코드가 "한 건씩" 체인 전체를 관통한다(단계별 일괄 처리가 아님).
#   체인 내에 동시에 존재하는 거래는 최대 1건.
# - 조건 불만족 시 yield가 실행되지 않으며, 하류로는 아무것도 전달되지 않는다
#   (빈 값이 전달되는 것이 아님). 해당 필터의 for 루프가 다음 반복으로 진행하며
#   상류에 다음 레코드를 요청하고, 이 요청이 연쇄되어 파일의 다음 줄이 읽힌다.
# - 필터의 직렬 연결이 곧 AND 결합이다(결정 4-1): 모든 필터를 통과한 레코드만
#   소비자에 도달한다.

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


@dataclass
class MonthSummary:
    """한 달(YYYY-MM)의 요약 — 저장되지 않는 일회용 보고서 (축 5 기각의 결과).

    숫자 2개 + 카테고리별 지출 dict가 누적의 전부 (결정 5-1).
    budget_amount는 budgets 저장소에서 가져온 그 달의 현재 예산 (없으면 None).
    """

    year_month: str  # 'YYYY-MM' — 연도가 항상 함께 다닌다
    income_total: int = 0
    expense_total: int = 0
    expense_by_category: dict[str, int] = field(default_factory=dict)
    transaction_count: int = 0
    budget_amount: int | None = None

    @property
    def balance(self) -> int:
        return self.income_total - self.expense_total

    @property
    def budget_usage_percent(self) -> float | None:
        """예산 대비 사용률(%). 예산이 없으면 None."""
        if self.budget_amount is None:
            return None
        return self.expense_total / self.budget_amount * 100

    @property
    def budget_exceeded_by(self) -> int:
        """예산 초과액 (초과 아니면 0)."""
        if self.budget_amount is None:
            return 0
        return max(0, self.expense_total - self.budget_amount)


# ----- 예산 (결정 6-1: last-wins append / 6-2: 현재 값 조회) -----

@timed
@logged
def set_budget(store: st.Storage, year_month: str, amount: str | int) -> int:
    """월 예산 설정 — 재설정도 append 한 줄 (last-wins이므로 재작성 불요)."""
    year_month = models.validate_year_month(year_month)
    amount = models.validate_amount(amount)
    store.append_record(st.BUDGETS, {'year_month': year_month, 'amount': amount})
    return amount


def current_budgets(store: st.Storage) -> dict[str, int]:
    """월별 현재 예산 — 스트리밍하며 같은 달은 나중 줄이 덮어씀 (last-wins 그 자체).

    특정 달의 최종값만 필요해도 전수 읽기가 필요하다: 여러 달의 레코드가 섞여
    있어, 어느 줄이 해당 달의 마지막인지는 끝까지 읽어야 확정된다.
    읽기는 전체 줄 수에 비례하고 메모리(dict)는 설정된 달의 개수에 비례한다 —
    재설정 횟수는 읽기량에만 영향을 주고 메모리에는 영향이 없다.
    """
    budgets: dict[str, int] = {}
    for record in store.iter_records(st.BUDGETS):
        if 'year_month' in record and 'amount' in record:
            budgets[record['year_month']] = int(record['amount'])
    return budgets


def get_budget(store: st.Storage, year_month: str) -> int | None:
    """특정 달의 현재 예산 (설정 안 됐으면 None)."""
    return current_budgets(store).get(year_month)


@timed
@logged
def summarize(store: st.Storage, year_month: str) -> MonthSummary:
    """월별 요약 — 스트리밍 1-pass 누적 (결정 5-1).

    거름망(_filter_period)을 재사용하되 소비자는 누적기다. 본문에 yield가 없는 것은
    이 함수가 스트림의 중간 단계가 아니라 종단 소비자이기 때문이다 — 합계는 마지막
    레코드까지 확인해야 확정되므로, 완주 후 완성된 보고서를 return한다.
    누적은 "더하고 잊는" 방식이라 과거 레코드를 보유할 필요가 없고(top-N의 후보
    유지와 대비), 메모리는 카테고리 수에 비례한다.

    두 저장소(거래·예산)는 각각 정확히 1회씩, 상호 의존 없이 독립적으로 읽혀
    MonthSummary 보고서에서 결합된다. 사용률·초과액 등 파생값은 저장하지 않고
    property로 매번 계산한다 (파생 데이터 저장 기각 — 결정 0-2 축 5 논의).
    """
    year_month = models.validate_year_month(year_month)
    # ISO 날짜는 문자열 비교 = 시간 비교라 '-31'이 그 달의 모든 유효 날짜를 덮는다
    stream = _filter_period(_iter_transactions(store), f'{year_month}-01', f'{year_month}-31')
    summary = MonthSummary(year_month, budget_amount=get_budget(store, year_month))
    for tx in stream:
        summary.transaction_count += 1
        if tx.type == 'income':
            summary.income_total += tx.amount
        else:
            summary.expense_total += tx.amount
            summary.expense_by_category[tx.category] = (
                summary.expense_by_category.get(tx.category, 0) + tx.amount
            )
    return summary


def top_expense_categories(
    expense_by_category: dict[str, int], top_n: int,
) -> list[tuple[int, str, int]]:
    """지출 합계 상위 — (순위, 카테고리, 합계) 목록 (결정 5-2).

    공동 순위는 스포츠식(1,1,3), 동점 내부는 가나다순, N위까지는 동점 전부
    포함하므로 줄 수가 N을 넘을 수 있다.

    서명이 store가 아닌 dict를 받는 것은 의도된 제약이다: summarize가 1회 독파로
    만든 집계 결과만 받으므로, 이 함수가 저장소를 재독하는 경로는 구조적으로 없다
    (한 명령 안에서 같은 파일을 두 번 읽지 않는다).
    """
    if top_n <= 0:
        raise ValidationError(f'--top은 양수여야 합니다: {top_n}', hint='예: --top 3')
    items = sorted(expense_by_category.items(), key=lambda kv: (-kv[1], kv[0]))
    ranked: list[tuple[int, str, int]] = []
    rank = 0
    prev_amount: int | None = None
    for position, (name, amount) in enumerate(items, start=1):
        if amount != prev_amount:
            rank = position  # 스포츠식: 순위 = 자기보다 앞선 인원 수 + 1
            prev_amount = amount
        if rank > top_n:
            break
        ranked.append((rank, name, amount))
    return ranked


@timed
@logged
def list_transactions(store: st.Storage, limit: int) -> list[models.Transaction]:
    """날짜 기준 최신순 상위 limit건 (결정 3-1/3-2).

    heapq.nlargest는 스트림을 소비하며 상위 limit건의 후보 목록만 유지한다 —
    읽기는 전체 줄 수(미확인 줄이 남아 있는 한 top-N은 확정 불가),
    메모리는 O(limit)로 제한된다. 조기 종료 가능 여부는 질문의 성질에 달렸다:
    존재 여부(any)는 첫 일치에서 확정되지만, top-N·합계는 전수 확인이 필요하다.
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
