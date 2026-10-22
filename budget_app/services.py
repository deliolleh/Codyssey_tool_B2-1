"""서비스 계층 — 업무 절차(매뉴얼)를 아는 곳.

"무엇을 어떤 순서로 하는가"만 안다. 값의 옳고 그름은 스스로 판정하지 않고
모델의 검사기(validate_*)를 빌려 쓴다 — 품질 기준은 모델 소관, 절차는 여기 소관.
파일 경로(storage 소관)와 사용자 대화(cli 소관)는 모른다. print 대신 보고서
(InitReport 등)를 돌려주는 이유가 그것이다.
입구(대화형 add, CSV import, …)가 어디든 업무 절차는 여기 한 곳에만 존재한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from budget_app import storage as st
from budget_app.decorators import logged, timed

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
