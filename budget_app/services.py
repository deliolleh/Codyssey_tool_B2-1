"""서비스 계층 — 업무 절차(매뉴얼)를 아는 곳.

"무엇을 어떤 순서로 하는가"만 안다. 값의 옳고 그름은 스스로 판정하지 않고
모델의 검사기(validate_*)를 빌려 쓴다 — 품질 기준은 모델 소관, 절차는 여기 소관.
파일 경로(storage 소관)와 사용자 대화(cli 소관)는 모른다. print 대신 보고서
(InitReport 등)를 돌려주는 이유가 그것이다.
입구(대화형 add, CSV import, …)가 어디든 업무 절차는 여기 한 곳에만 존재한다.
"""
from __future__ import annotations

import csv
import heapq
from dataclasses import dataclass, field
from pathlib import Path

from budget_app import models
from budget_app import storage as st
from budget_app.decorators import logged, timed, translates_oserrors
from budget_app.errors import BudgetError, NotFoundError, ValidationError

# 기본 카테고리: 토스 '내 소비' 대분류를 한 단어로 축약한 기본 카테고리.
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


@timed   # 바깥: 일지 기록 시간까지 포함해 잰다 (중첩 순서가 의미를 가진다)
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
    """최신순 정렬 키: 날짜 우선, 같으면 id(생성 순) — 둘 다 고정폭 문자열이라 비교 가능."""
    return (tx.date, tx.id)


def _iter_transactions(store: st.Storage):
    """저장 레코드를 Transaction 스트림으로 — 모든 조회의 공통 수원(水源)."""
    return (models.Transaction.from_record(r) for r in store.iter_records(st.TRANSACTIONS))


# ----- 검색 거름망 (조건 하나 = 거름망 하나, None이면 통과) -----
#
# 동작 방식:
# - 각 필터는 제너레이터 함수다. 체인 조립(stream = _filter_x(stream, ...)의 연쇄 =
#   함수 합성 f(g(h(x)))의 축차 표기) 시점에는 아무것도 실행·판정되지 않는다.
# - 소비가 시작되면 레코드가 "한 건씩" 체인 전체를 관통한다(단계별 일괄 처리가 아님).
#   체인 내에 동시에 존재하는 거래는 최대 1건.
# - 조건 불만족 시 yield가 실행되지 않으며, 하류로는 아무것도 전달되지 않는다
#   (빈 값이 전달되는 것이 아님). 해당 필터의 for 루프가 다음 반복으로 진행하며
#   상류에 다음 레코드를 요청하고, 이 요청이 연쇄되어 파일의 다음 줄이 읽힌다.
# - 필터의 직렬 연결이 곧 AND 결합이다: 모든 필터를 통과한 레코드만
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
    """메모 글자 부분일치, 대소문자 무시."""
    needle = query.casefold() if query is not None else None
    for tx in stream:
        if needle is None or needle in tx.memo.casefold():
            yield tx


def _filter_tag(stream, tag: str | None):
    """태그도 부분일치 + 대소문자 무시 (메모와 같은 철학으로 통일)."""
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
    """조건 검색 (모든 조건 AND). 결과는 최신순.

    거름망 5장을 쌓은 파이프라인 — 각 거래는 한 건씩 흘러가며 거름망마다
    "그 줄만 보면 확정되는" 판정을 받는다. limit 없으면 매칭 전부(메모리 O(결과 수)),
    있으면 선발 명단(O(limit)).
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

    숫자 2개 + 카테고리별 지출 dict가 누적의 전부.
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


# ----- 예산 (last-wins append / 현재 값 조회) -----

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
    """월별 요약 — 스트리밍 1-pass 누적.

    거름망(_filter_period)을 재사용하되 소비자는 누적기다. 본문에 yield가 없는 것은
    이 함수가 스트림의 중간 단계가 아니라 종단 소비자이기 때문이다 — 합계는 마지막
    레코드까지 확인해야 확정되므로, 완주 후 완성된 보고서를 return한다.
    누적은 "더하고 잊는" 방식이라 과거 레코드를 보유할 필요가 없고(top-N의 후보
    유지와 대비), 메모리는 카테고리 수에 비례한다.

    두 저장소(거래·예산)는 각각 정확히 1회씩, 상호 의존 없이 독립적으로 읽혀
    MonthSummary 보고서에서 결합된다. 사용률·초과액 등 파생값은 저장하지 않고
    property로 매번 계산한다 (파생값은 저장하지 않는다).
    """
    first_day, last_day = models.month_date_range(year_month)
    year_month = models.validate_year_month(year_month)
    stream = _filter_period(_iter_transactions(store), first_day, last_day)
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
    """지출 합계 상위 — (순위, 카테고리, 합계) 목록.

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
    """날짜 기준 최신순 상위 limit건.

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
    """카테고리를 등록한다. add의 등록 유도와 category add가 같이 쓰는 입구."""
    name = name.strip()
    if not name:
        raise ValidationError('카테고리 이름이 비어 있습니다', hint='한 단어 이름을 입력하세요 (예: 식비)')
    if category_registered(store, name):
        raise ValidationError(f'이미 존재하는 카테고리입니다: {name}',
                              hint='category list로 현재 목록을 확인하세요')
    store.append_record(st.CATEGORIES, {'name': name})
    return name


def _rewrite_transaction(store: st.Storage, tx_id: str, transform) -> models.Transaction:
    """대상 거래 하나를 변환(또는 제거)하며 거래 파일을 1-pass 재작성한다.

    처음부터 임시 파일에 복사하며 스캔하고, 대상 id를 끝까지 만나지 못하면
    제너레이터가 NotFoundError를 던져 교체를 무산시킨다(조건부 커밋 — 원본 무변화).
    transform(tx)가 None을 돌려주면 그 거래는 새 파일에서 제외(삭제)된다.

    수정 전략으로 last-wins append가 아닌 전체 재작성을 쓰는 이유: 거래 파일은
    읽기가 주업이라(list/search/summary 전 경로) "파일의 줄 = 살아있는 거래"라는
    단순한 읽기 의미가 더 값지다. 예산 파일(읽는 곳이 한 군데)과 반대의 선택.
    """
    matched: list[models.Transaction] = []  # 제너레이터 안에서 바깥으로 전달할 그릇

    def records():
        for record in store.iter_records(st.TRANSACTIONS):
            tx = models.Transaction.from_record(record)
            if tx.id != tx_id:
                yield record
                continue
            replacement = transform(tx)
            matched.append(replacement if replacement is not None else tx)
            if replacement is not None:
                yield replacement.to_record()
        if not matched:
            raise NotFoundError(f'존재하지 않는 거래 id입니다: {tx_id}',
                                hint='list 또는 search로 id를 확인하세요')

    store.rewrite_records(st.TRANSACTIONS, records())
    return matched[0]


@timed
@logged
def delete_transaction(store: st.Storage, tx_id: str) -> models.Transaction:
    """거래 삭제 — 삭제된 거래를 돌려준다 (사용자 확인 메시지용)."""
    return _rewrite_transaction(store, tx_id, lambda tx: None)


@timed
@logged
def update_transaction(
    store: st.Storage,
    tx_id: str,
    *,
    date: str | None = None,
    tx_type: str | None = None,
    category: str | None = None,
    amount: int | None = None,
    memo: str | None = None,
    tags: list[str] | None = None,
) -> models.Transaction:
    """거래 수정 — 옵션 기반: None은 유지, 값이 오면 교체 (빈 문자열 = 비우기).

    새 값들의 검증은 재작성을 시작하기 전에 끝낸다 — 검증 실패로
    파일 작업이 시작되는 일이 없게.
    """
    if all(v is None for v in (date, tx_type, category, amount, memo, tags)):
        raise ValidationError('수정할 필드를 하나 이상 지정해야 합니다',
                              hint='예: update --id TX-000001 --amount 20000')
    if date is not None:
        date = models.validate_date(date)
    if tx_type is not None:
        tx_type = models.validate_type(tx_type)
    if amount is not None:
        amount = models.validate_amount(amount)
    if category is not None and not category_registered(store, category):
        raise ValidationError(f'등록되지 않은 카테고리입니다: {category}',
                              hint='category add로 먼저 등록하세요')

    def apply(tx: models.Transaction) -> models.Transaction:
        return models.Transaction(
            id=tx.id,
            type=tx_type if tx_type is not None else tx.type,
            date=date if date is not None else tx.date,
            amount=amount if amount is not None else tx.amount,
            category=category if category is not None else tx.category,
            memo=memo if memo is not None else tx.memo,
            tags=tags if tags is not None else tx.tags,
        )

    return _rewrite_transaction(store, tx_id, apply)


# ----- import/export: 외부 교환 CSV (고정 스키마는 models.CSV_COLUMNS) -----
# 교환 파일은 data/ 밖의 사용자 지정 경로라 저장소(Storage) 소관이 아니다 —
# 파일 I/O는 여기서 직접 하되, OSError 번역은 같은 데코레이터를 빌린다.


def _export_range(year_month: str | None, date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    """export 조건을 기간으로 정규화. 두 방식 혼용은 오류, 무조건은 오류 (결정 9-3 부속)."""
    has_month = year_month is not None
    has_range = date_from is not None or date_to is not None
    if has_month and has_range:
        raise ValidationError('--month와 --from/--to는 함께 쓸 수 없습니다',
                              hint='둘 중 한 방식만 선택하세요')
    if not has_month and not has_range:
        raise ValidationError('기간 조건이 필요합니다',
                              hint='--month YYYY-MM 또는 --from/--to YYYY-MM-DD를 지정하세요')
    if has_month:
        return models.month_date_range(year_month)
    if date_from is not None:
        date_from = models.validate_date(date_from)
    if date_to is not None:
        date_to = models.validate_date(date_to)
    return date_from, date_to


@timed
@logged
@translates_oserrors
def export_to_csv(
    store: st.Storage,
    out_path: Path,
    *,
    year_month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """조건에 맞는 거래를 교환 CSV로 저장하고 건수를 돌려준다.

    search의 기간 필터·최신순을 재사용한다. id는 스키마에 없어 내보내지 않으므로
    export→import 왕복 시 새 id가 발급된다 (명세의 스키마 설계).
    """
    date_from, date_to = _export_range(year_month, date_from, date_to)
    transactions = search_transactions(store, date_from=date_from, date_to=date_to)
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(models.CSV_COLUMNS)
        for tx in transactions:
            writer.writerow(models.transaction_to_csv_row(tx))
    return len(transactions)


def _read_csv_rows(csv_path: Path):
    """교환 CSV를 행 단위로 스트리밍 — (행 번호, dict) yield. 헤더는 먼저 검사한다."""
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        missing = [c for c in models.CSV_REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValidationError(
                f'CSV 헤더가 스키마와 다릅니다 (누락: {", ".join(missing)})',
                hint=f'필요한 헤더: {", ".join(models.CSV_COLUMNS)}',
            )
        for line_no, row in enumerate(reader, start=2):  # 1행은 헤더
            if not any((value or '').strip() for value in row.values()):
                continue  # 빈 행은 조용히 무시
            yield line_no, row


@translates_oserrors
def scan_csv_categories(store: st.Storage, csv_path: Path) -> list[str]:
    """CSV에 등장하는 미등록 카테고리 목록 (결정 9-2의 사전 집계 pass, 등장 순서 보존)."""
    seen: dict[str, None] = {}
    for _, row in _read_csv_rows(csv_path):
        name = (row.get('Category') or '').strip()
        if name and name not in seen and not category_registered(store, name):
            seen[name] = None
    return list(seen)


@dataclass
class ImportReport:
    """import 결과 — 성공 건수와 실패 행(번호, 원인) 목록 (결정 9-1)."""

    imported: int = 0
    skipped: list[tuple[int, str]] = field(default_factory=list)


@timed
@logged
@translates_oserrors
def import_from_csv(store: st.Storage, csv_path: Path) -> ImportReport:
    """교환 CSV를 일괄 등록 — 행마다 add_transaction 단일 입구를 지난다.

    실패한 행은 건너뛰고 (행 번호, 원인)을 기록한다 (결정 9-1 스킵+리포트).
    미등록 카테고리의 사전 처리(일괄 등록 확인)는 cli가 scan_csv_categories로
    먼저 수행하며, 남아 있으면 해당 행은 여기서 검증 실패로 스킵된다.

    쓰기 전략은 append 가족이다: 행마다 즉시 append되어 각 행이 개별 커밋이고,
    메모리에는 흐르는 1행과 리포트만 존재한다. 중단 시 "거기까지 진행된 파일"이
    남으며(재작성 가족의 전부-아니면-전무와 대비), 교환 스키마에 id가 없어
    같은 CSV를 재실행하면 이미 등록된 행이 중복된다는 점을 사용자가 인지해야 한다.
    """
    report = ImportReport()
    for line_no, row in _read_csv_rows(csv_path):
        try:
            add_transaction(
                store,
                date=(row.get('Date') or '').strip(),
                tx_type=(row.get('Type') or '').strip(),
                category=(row.get('Category') or '').strip(),
                amount=(row.get('Amount') or '').strip(),
                memo=(row.get('Memo') or ''),
                tags=models.parse_tags(row.get('Tags') or ''),
            )
            report.imported += 1
        except BudgetError as err:
            report.skipped.append((line_no, err.cause))
    return report


def count_category_usage(store: st.Storage, name: str) -> int:
    """해당 카테고리를 쓰는 거래 건수 — 삭제 시점 전수 스캔.

    존재 여부는 첫 발견 즉시 명제가 참으로 확정되어 조기 종료가 가능하지만,
    건수는 전체를 읽어야 정확하다 — 서로 다른 질문이며 비용도 다르다.
    건수가 오류 메시지에서 사용자의 다음 행동 판단 재료가 되므로, 드물게
    실행되는 작업임을 근거로 비용이 더 큰 질문을 채택했다.
    """
    return sum(1 for tx in _iter_transactions(store) if tx.category == name)


@timed
@logged
def remove_category(store: st.Storage, name: str) -> None:
    """카테고리 삭제 — 사용 중이면 차단하고, 반영은 원자적 교체로 한다.

    검사 순서가 곧 정책이다: 등록 확인 → 사용 건수 집계 → 사용 중이면 파일을
    건드리기 전에 차단(읽기 전용 종료) → 미사용일 때만 재작성에 진입한다.
    재작성 단계에서는 원본을 읽는 스트림과 임시 파일 쓰기가 동시에 진행되지만
    서로 다른 파일이므로 안전하다.
    """
    name = name.strip()
    if not category_registered(store, name):
        raise NotFoundError(f'등록되지 않은 카테고리입니다: {name}',
                            hint='category list로 현재 목록을 확인하세요')
    usage = count_category_usage(store, name)
    if usage > 0:
        raise ValidationError(
            f"'{name}'은(는) 거래 {usage}건이 사용 중이라 삭제할 수 없습니다",
            hint='해당 거래의 카테고리를 변경한 뒤 다시 시도하세요',
        )
    remaining = (r for r in store.iter_records(st.CATEGORIES) if r.get('name') != name)
    store.rewrite_records(st.CATEGORIES, remaining)


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
    저장 순서: 카운터 선점 → 거래 append — 중간에 죽어도 번호 구멍만 남는다.
    """
    # 1) 전부 검증 — 하나라도 실패하면 카운터를 건드리기 전에 중단 (번호 낭비 없음)
    valid_type = models.validate_type(tx_type)
    valid_date = models.validate_date(date)
    valid_amount = models.validate_amount(amount)
    if not category_registered(store, category):
        raise ValidationError(f'등록되지 않은 카테고리입니다: {category}',
                              hint='category add로 먼저 등록하세요')
    # 2) 카운터 선점 → 3) append (이 사이에 죽으면 중복 대신 무해한 구멍)
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
