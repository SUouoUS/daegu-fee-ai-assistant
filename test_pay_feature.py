"""
test_pay_feature.py
납부 완료 표시 기능 테스트.

테스트 항목:
1. 미납 고지서를 납부완료로 표시하면 목록 조회에서 제외되는지
2. AI 비서 계산 함수에서 납부완료 고지서가 제외되는지
3. status 컬럼과 paid_at이 DB에 정상적으로 반영되는지
"""

import os
import sys
import sqlite3
import datetime

# 프로젝트 루트를 sys.path에 추가
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 테스트용 DB 경로 설정 (실제 DB와 분리)
_TEST_DB_PATH = os.path.join(_project_root, "data", "test_bills.db")

# db 모듈의 DB 경로를 테스트용으로 오버라이드
import database.db as db_module
db_module._DB_PATH = _TEST_DB_PATH

from database.db import (
    init_db,
    save_bill,
    get_bills,
    get_bills_due_this_week,
    get_bills_due_this_month,
    get_total_amount_this_month,
    get_nearest_due_bill,
    mark_as_paid,
    mark_as_unpaid,
)


def _cleanup():
    """테스트 DB 파일을 삭제한다."""
    if os.path.exists(_TEST_DB_PATH):
        os.remove(_TEST_DB_PATH)
    # WAL/SHM 파일도 정리
    for ext in ("-wal", "-shm"):
        path = _TEST_DB_PATH + ext
        if os.path.exists(path):
            os.remove(path)


def _setup():
    """테스트 환경을 초기화한다."""
    _cleanup()
    init_db()


def _insert_sample_bills():
    """테스트용 고지서를 삽입한다."""
    today = datetime.date.today()

    # 이번 주 내 기한인 고지서
    this_week_due = today + datetime.timedelta(days=1)

    # 이번 달 내 기한인 고지서
    this_month_due = today.replace(day=28) if today.day < 28 else today

    # 다음 달 기한인 고지서
    if today.month == 12:
        next_month_due = today.replace(year=today.year + 1, month=1, day=15)
    else:
        next_month_due = today.replace(month=today.month + 1, day=15)

    bills = [
        {
            "title": "상하수도 사용료",
            "agency": "대구광역시 수성구",
            "amount": 45200,
            "due_date": this_week_due.isoformat(),
            "payment_method": "가상계좌",
        },
        {
            "title": "가스요금",
            "agency": "대구도시가스",
            "amount": 32100,
            "due_date": this_month_due.isoformat(),
            "payment_method": "자동이체",
        },
        {
            "title": "재산세",
            "agency": "대구광역시",
            "amount": 150000,
            "due_date": next_month_due.isoformat(),
            "payment_method": "카드납부",
        },
    ]

    ids = []
    for bill in bills:
        bill_id = save_bill(bill)
        ids.append(bill_id)

    return ids


# ── 테스트 케이스 ─────────────────────────────────────────────

def test_1_paid_excluded_from_list():
    """테스트 1: 납부완료 표시 후 기본 목록 조회에서 제외되는지."""
    print("\n[테스트 1] 납부완료 → 목록 제외")
    _setup()
    ids = _insert_sample_bills()

    # 초기: 3건 모두 미납
    all_bills = get_bills(include_paid=False)
    assert len(all_bills) == 3, f"초기 미납 목록 3건 예상, 실제 {len(all_bills)}건"

    # 첫 번째 고지서를 납부완료로 표시
    result = mark_as_paid(ids[0])
    assert result is True, "mark_as_paid 실패"

    # 미납 목록: 2건
    unpaid_bills = get_bills(include_paid=False)
    assert len(unpaid_bills) == 2, f"납부완료 후 미납 2건 예상, 실제 {len(unpaid_bills)}건"

    # 납부완료 포함 목록: 3건
    all_with_paid = get_bills(include_paid=True)
    assert len(all_with_paid) == 3, f"전체 3건 예상, 실제 {len(all_with_paid)}건"

    # 납부완료 고지서의 id가 미납 목록에 없는지
    unpaid_ids = {b["id"] for b in unpaid_bills}
    assert ids[0] not in unpaid_ids, "납부완료 고지서가 미납 목록에 포함됨"

    print("  ✅ 납부완료 고지서가 미납 목록에서 정상 제외됨")


def test_2_paid_excluded_from_queries():
    """테스트 2: AI 비서 계산용 조회 함수에서 납부완료가 제외되는지."""
    print("\n[테스트 2] 납부완료 → AI 비서 계산에서 제외")
    _setup()
    ids = _insert_sample_bills()

    # 이번 주 납부 기한 고지서 확인 (최소 1건)
    this_week_before = get_bills_due_this_week(include_paid=False)
    week_count_before = len(this_week_before)
    print(f"  이번 주 납부 기한 (납부완료 전): {week_count_before}건")

    # 이번 달 총 금액 (납부완료 전)
    total_before = get_total_amount_this_month(include_paid=False)
    print(f"  이번 달 총 금액 (납부완료 전): {total_before:,}원")

    # 첫 번째 고지서 (이번 주 기한) 납부완료 처리
    mark_as_paid(ids[0])

    # 이번 주 납부 기한 고지서 재확인
    this_week_after = get_bills_due_this_week(include_paid=False)
    week_count_after = len(this_week_after)
    print(f"  이번 주 납부 기한 (납부완료 후): {week_count_after}건")

    # 납부완료한 고지서가 이번 주 목록에서 제외되었는지
    week_ids_after = {b["id"] for b in this_week_after}
    assert ids[0] not in week_ids_after, "납부완료 고지서가 이번 주 목록에 포함됨"

    # 이번 달 총 금액 재확인 (45200원 감소 또는 동일)
    total_after = get_total_amount_this_month(include_paid=False)
    print(f"  이번 달 총 금액 (납부완료 후): {total_after:,}원")

    # 가장 가까운 납부 기한 확인
    nearest = get_nearest_due_bill(include_paid=False)
    if nearest:
        assert nearest["id"] != ids[0], "납부완료 고지서가 가장 가까운 기한으로 반환됨"
        print(f"  가장 가까운 기한: {nearest['due_date']} ({nearest['title']})")

    print("  ✅ 납부완료 고지서가 모든 조회 함수에서 정상 제외됨")


def test_3_db_columns_updated():
    """테스트 3: status·paid_at 컬럼이 DB에 정상 반영되는지."""
    print("\n[테스트 3] DB 컬럼 반영 확인")
    _setup()
    ids = _insert_sample_bills()

    # 초기 상태 확인
    conn = sqlite3.connect(_TEST_DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, paid_at FROM bills WHERE id = ?", (ids[0],)
    ).fetchone()
    assert row["status"] == "미납", f"초기 status '미납' 예상, 실제 '{row['status']}'"
    assert row["paid_at"] is None, f"초기 paid_at NULL 예상, 실제 '{row['paid_at']}'"
    print(f"  초기 상태: status='{row['status']}', paid_at={row['paid_at']}")

    # 납부완료 처리
    mark_as_paid(ids[0])

    row = conn.execute(
        "SELECT status, paid_at FROM bills WHERE id = ?", (ids[0],)
    ).fetchone()
    assert row["status"] == "납부완료", f"납부완료 후 status '납부완료' 예상, 실제 '{row['status']}'"
    assert row["paid_at"] is not None, "납부완료 후 paid_at이 NULL"
    print(f"  납부완료 후: status='{row['status']}', paid_at='{row['paid_at']}'")

    # idempotent 확인: 이미 납부완료인데 다시 호출
    paid_at_before = row["paid_at"]
    result = mark_as_paid(ids[0])
    assert result is True, "이미 납부완료인데 mark_as_paid가 False 반환"
    row = conn.execute(
        "SELECT paid_at FROM bills WHERE id = ?", (ids[0],)
    ).fetchone()
    assert row["paid_at"] == paid_at_before, "idempotent: paid_at이 변경됨"
    print("  idempotent 확인: 중복 호출 시 paid_at 변경 없음")

    # 미납으로 되돌리기
    mark_as_unpaid(ids[0])

    row = conn.execute(
        "SELECT status, paid_at FROM bills WHERE id = ?", (ids[0],)
    ).fetchone()
    assert row["status"] == "미납", f"되돌리기 후 status '미납' 예상, 실제 '{row['status']}'"
    assert row["paid_at"] is None, f"되돌리기 후 paid_at NULL 예상, 실제 '{row['paid_at']}'"
    print(f"  되돌리기 후: status='{row['status']}', paid_at={row['paid_at']}")

    # 존재하지 않는 id
    result = mark_as_paid(99999)
    assert result is False, "존재하지 않는 id인데 mark_as_paid가 True 반환"
    print("  존재하지 않는 id: False 반환 확인")

    conn.close()
    print("  ✅ status·paid_at 컬럼 정상 반영 확인")


def main():
    print("=" * 60)
    print("납부 완료 표시 기능 테스트")
    print("=" * 60)

    try:
        test_1_paid_excluded_from_list()
        test_2_paid_excluded_from_queries()
        test_3_db_columns_updated()

        print("\n" + "=" * 60)
        print("✅ 모든 테스트 통과!")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n❌ 테스트 실패: {e}")
        raise
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
