"""
database/migrate_add_status.py
기존 bills 테이블에 status·paid_at 컬럼을 추가하는 마이그레이션 스크립트.

사용법:
    python -m database.migrate_add_status

이미 컬럼이 있으면 건너뛰며, 기존 row의 status를 '미납'으로 설정합니다.
db.init_db() 호출 시에도 동일한 마이그레이션이 자동 실행되므로,
이 스크립트는 수동 실행이 필요한 경우에만 사용하면 됩니다.
"""

import os
import sys

# 프로젝트 루트를 sys.path에 추가 (직접 실행 시)
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from database.db import init_db, _DB_PATH


def main():
    print(f"DB 경로: {_DB_PATH}")

    if not os.path.exists(_DB_PATH):
        print("DB 파일이 존재하지 않습니다. 새로 생성합니다.")

    # init_db()가 테이블 생성 + 마이그레이션을 모두 수행
    init_db()
    print("마이그레이션 완료: bills 테이블에 status·paid_at 컬럼이 확인되었습니다.")


if __name__ == "__main__":
    main()
