"""
database/db.py
SQLite 기반 고지서 데이터 관리 모듈.

bills 테이블:
- id, title, agency, amount, due_date, payment_method
- status ('미납' / '납부완료'), paid_at (납부 시점), created_at (저장 시점)
"""

import sqlite3
import datetime
import os

# DB 파일 경로: 프로젝트 루트의 data/bills.db
_DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH = os.path.join(_DB_DIR, "bills.db")


def _get_connection() -> sqlite3.Connection:
    """SQLite 연결을 반환한다. Row를 dict처럼 접근 가능하게 설정."""
    os.makedirs(_DB_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """bills 테이블을 생성한다. 이미 존재하면 무시.
    기존 테이블에 status/paid_at 컬럼이 없으면 마이그레이션도 수행.
    """
    conn = _get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bills (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT,
                agency          TEXT,
                amount          INTEGER,
                due_date        TEXT,
                payment_method  TEXT,
                status          TEXT NOT NULL DEFAULT '미납',
                paid_at         TEXT,
                created_at      TEXT NOT NULL
            )
        """)
        conn.commit()

        # 마이그레이션: 기존 테이블에 컬럼이 없는 경우 추가
        _migrate_if_needed(conn)
    finally:
        conn.close()


def _migrate_if_needed(conn: sqlite3.Connection) -> None:
    """기존 bills 테이블에 status·paid_at 컬럼이 없으면 추가하고,
    기존 row의 status를 '미납'으로 설정한다.
    """
    cursor = conn.execute("PRAGMA table_info(bills)")
    columns = {row["name"] for row in cursor.fetchall()}

    if "status" not in columns:
        conn.execute(
            "ALTER TABLE bills ADD COLUMN status TEXT NOT NULL DEFAULT '미납'"
        )
        conn.execute("UPDATE bills SET status = '미납' WHERE status IS NULL")
        conn.commit()

    if "paid_at" not in columns:
        conn.execute("ALTER TABLE bills ADD COLUMN paid_at TEXT")
        conn.commit()


# ── 저장 ──────────────────────────────────────────────────────


def save_bill(info: dict) -> int:
    """고지서 정보를 DB에 저장하고 생성된 id를 반환한다.

    Args:
        info: extract_notice_info()가 반환하는 dict.
              필수 키는 없으며, 누락된 필드는 NULL로 저장.

    Returns:
        생성된 bill의 id (정수).
    """
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO bills (title, agency, amount, due_date,
                               payment_method, status, created_at)
            VALUES (?, ?, ?, ?, ?, '미납', ?)
            """,
            (
                info.get("title"),
                info.get("agency"),
                info.get("amount"),
                info.get("due_date"),
                info.get("payment_method"),
                now,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


# ── 조회 ──────────────────────────────────────────────────────


def _status_filter(include_paid: bool) -> str:
    """WHERE 절에 추가할 status 필터 SQL 조각을 반환한다."""
    if include_paid:
        return "1=1"  # 모든 상태
    return "status = '미납'"


def get_bills(include_paid: bool = False) -> list[dict]:
    """저장된 고지서 목록을 반환한다.

    Args:
        include_paid: True이면 납부완료 고지서도 포함. 기본 False(미납만).

    Returns:
        dict 리스트. 각 dict는 bills 테이블의 한 행.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM bills
            WHERE {_status_filter(include_paid)}
            ORDER BY
                CASE WHEN due_date IS NOT NULL THEN 0 ELSE 1 END,
                due_date ASC,
                created_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_bills_due_this_week(include_paid: bool = False) -> list[dict]:
    """이번 주(월~일) 납부 기한인 고지서를 반환한다."""
    today = datetime.date.today()
    # 월요일 = 0
    monday = today - datetime.timedelta(days=today.weekday())
    sunday = monday + datetime.timedelta(days=6)

    conn = _get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM bills
            WHERE {_status_filter(include_paid)}
              AND due_date IS NOT NULL
              AND due_date >= ?
              AND due_date <= ?
            ORDER BY due_date ASC
            """,
            (monday.isoformat(), sunday.isoformat()),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_bills_due_this_month(include_paid: bool = False) -> list[dict]:
    """이번 달 납부 기한인 고지서를 반환한다."""
    today = datetime.date.today()
    first_day = today.replace(day=1)
    # 다음 달 1일 - 1일 = 이번 달 마지막 날
    if today.month == 12:
        last_day = today.replace(year=today.year + 1, month=1, day=1) - datetime.timedelta(days=1)
    else:
        last_day = today.replace(month=today.month + 1, day=1) - datetime.timedelta(days=1)

    conn = _get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM bills
            WHERE {_status_filter(include_paid)}
              AND due_date IS NOT NULL
              AND due_date >= ?
              AND due_date <= ?
            ORDER BY due_date ASC
            """,
            (first_day.isoformat(), last_day.isoformat()),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_total_amount_this_month(include_paid: bool = False) -> int:
    """이번 달 납부 예정 총 금액을 반환한다."""
    bills = get_bills_due_this_month(include_paid=include_paid)
    return sum(b["amount"] for b in bills if b.get("amount") is not None)


def get_nearest_due_bill(include_paid: bool = False) -> dict | None:
    """오늘 이후 가장 가까운 납부 기한의 고지서를 반환한다.
    없으면 None.
    """
    today = datetime.date.today().isoformat()
    conn = _get_connection()
    try:
        row = conn.execute(
            f"""
            SELECT * FROM bills
            WHERE {_status_filter(include_paid)}
              AND due_date IS NOT NULL
              AND due_date >= ?
            ORDER BY due_date ASC
            LIMIT 1
            """,
            (today,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── 상태 변경 ─────────────────────────────────────────────────


def mark_as_paid(bill_id: int) -> bool:
    """고지서를 납부완료로 표시한다.

    이미 납부완료 상태이면 에러 없이 True를 반환 (idempotent).

    Args:
        bill_id: 대상 고지서의 id.

    Returns:
        True: 성공 (상태 변경 또는 이미 납부완료).
        False: 해당 id가 존재하지 않음.
    """
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn = _get_connection()
    try:
        # 존재 여부 확인
        row = conn.execute(
            "SELECT id, status FROM bills WHERE id = ?", (bill_id,)
        ).fetchone()
        if row is None:
            return False

        if row["status"] == "납부완료":
            return True  # 이미 납부완료 — idempotent

        conn.execute(
            """
            UPDATE bills
            SET status = '납부완료', paid_at = ?
            WHERE id = ?
            """,
            (now, bill_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def mark_as_unpaid(bill_id: int) -> bool:
    """고지서를 미납으로 되돌린다.

    이미 미납 상태이면 에러 없이 True를 반환 (idempotent).

    Args:
        bill_id: 대상 고지서의 id.

    Returns:
        True: 성공 (상태 변경 또는 이미 미납).
        False: 해당 id가 존재하지 않음.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT id, status FROM bills WHERE id = ?", (bill_id,)
        ).fetchone()
        if row is None:
            return False

        if row["status"] == "미납":
            return True  # 이미 미납 — idempotent

        conn.execute(
            """
            UPDATE bills
            SET status = '미납', paid_at = NULL
            WHERE id = ?
            """,
            (bill_id,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


# ── 삭제 ──────────────────────────────────────────────────────


def delete_bill(bill_id: int) -> bool:
    """고지서를 삭제한다.

    Returns:
        True: 삭제 성공.
        False: 해당 id가 존재하지 않음.
    """
    conn = _get_connection()
    try:
        cursor = conn.execute("DELETE FROM bills WHERE id = ?", (bill_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
