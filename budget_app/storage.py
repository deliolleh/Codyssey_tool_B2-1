"""저장소 계층 — 파일 경로와 파일 I/O를 아는 유일한 곳.

주소(경로)와 포장 규격(JSONL 문법), 나르고 쌓는 법(읽기/append)을 알지만
상자 속 내용물의 의미는 모른다: 깨진 JSON(포장 손상)은 여기서 잡고,
말이 안 되는 값(-5000원 거래 등)의 판정은 모델 소관이다.
경계 규칙: input/print 금지. 읽기는 전부 제너레이터(스트리밍)로 한다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from budget_app.errors import StorageError

TRANSACTIONS = 'transactions.jsonl'
CATEGORIES = 'categories.jsonl'
BUDGETS = 'budgets.jsonl'
META = 'meta.jsonl'
DATA_FILES = (TRANSACTIONS, CATEGORIES, BUDGETS, META)

DEFAULT_DATA_DIR = Path('data')


class Storage:
    """data 폴더 하나를 책임지는 저장소."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def path(self, filename: str) -> Path:
        return self.data_dir / filename

    def ensure_files(self) -> list[str]:
        """없는 저장 파일을 만들고, 새로 만든 파일 이름들을 돌려준다."""
        created: list[str] = []
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in DATA_FILES:
            file_path = self.path(name)
            if not file_path.exists():
                file_path.touch()
                created.append(name)
        return created

    def iter_records(self, filename: str) -> Iterator[dict]:
        """JSONL 파일을 한 줄씩 읽어 dict로 yield한다.

        파일 전체를 메모리에 올리지 않는다 — 소비자가 당기는 만큼만 읽힌다.
        """
        with open(self.path(filename), encoding='utf-8') as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    raise StorageError(
                        f'{filename} {line_no}번째 줄이 올바른 JSON이 아닙니다',
                        hint='파일이 손상됐을 수 있습니다. 해당 줄을 확인하세요',
                    ) from None

    def append_record(self, filename: str, record: dict) -> None:
        """파일 끝에 레코드 한 줄을 덧붙인다."""
        with open(self.path(filename), 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    # ----- meta: 거래 id 카운터 -----

    def last_tx_number(self) -> int:
        """지금까지 발급된 마지막 거래 번호 (없으면 0)."""
        number = 0
        for record in self.iter_records(META):
            if 'last_tx_no' in record:
                number = int(record['last_tx_no'])
        return number

    def claim_tx_number(self) -> int:
        """다음 거래 번호를 선점한다.

        규칙(결정 0-4): 카운터를 먼저 갱신하고 거래는 그 뒤에 저장한다.
        중간에 중단돼도 중복 id 대신 무해한 번호 구멍만 남는다.
        카운터는 삭제와 무관하게 단조 증가하며, 번호는 재사용되지 않는다.
        """
        number = self.last_tx_number() + 1
        self._write_meta(number)
        return number

    def _write_meta(self, last_tx_no: int) -> None:
        # 한 줄짜리 파일 전체 덮어쓰기. 원자적 교체로의 강화는 결정 8-2에서 다룬다.
        with open(self.path(META), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'last_tx_no': last_tx_no}, ensure_ascii=False) + '\n')
