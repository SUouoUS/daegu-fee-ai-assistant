"""
database/db.py
고지서 정보를 SQLite에 저장하고 조회하는 모듈.
표준 라이브러리 sqlite3만 사용하며, ORM은 사용하지 않는다.

저장하는 정보: 고지서명, 발급 기관, 금액, 납부기한, 납부방법, 상태, 생성 시각
저장하지 않는 정보: PDF 원본, notice_text, PDF 비밀번호, API 키
"""

import datetime
import sqlite3
from pathlib import Path

# 프로젝트 루트/data/notices.db (테스트에서는 이 값을 임시 경로로 교체)
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "notices.db"

TABLE_NAME = "notices"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NULL,
    agency TEXT NULL,
    amount INTEGER NULL,
    due_date TEXT NULL,
    payment_method TEXT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_SELECT_COLUMNS = "id, title, agency, amount, due_date, payment_method, status, created_at"

DEFAULT_STATUS = "미납"


# ── 내부 헬퍼 ────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    """DB 폴더를 보장한 뒤 커넥션을 반환한다."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _validate_amount(value) -> int | None:
    """amount는 None 또는 0 이상의 int만 허용한다. (bool은 거절)"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("amount에 bool 값은 사용할 수 없습니다.")
    if not isinstance(value, int):
        raise ValueError("amount는 None 또는 정수여야 합니다.")
    if value < 0:
        raise ValueError("amount는 0 이상이어야 합니다.")
    return value


def _validate_date(value, field_name: str = "due_date") -> str | None:
    """None 또는 실제 존재하는 YYYY-MM-DD 문자열만 허용한다."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name}는 None 또는 YYYY-MM-DD 문자열이어야 합니다.")
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError(f"{field_name} 형식이 올바르지 않습니다. (YYYY-MM-DD)")
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field_name}가 존재하지 않는 날짜입니다.")
    return value


def _validate_text(value, field_name: str) -> str | None:
    """None 또는 문자열만 허용하고, 공백만 있으면 None으로 정규화한다."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name}는 None 또는 문자열이어야 합니다.")
    stripped = value.strip()
    return stripped or None


def _validate_status(value) -> str:
    """status는 NOT NULL이므로 비어 있으면 기본값을 사용한다."""
    if value is None:
        return DEFAULT_STATUS
    if not isinstance(value, str):
        raise ValueError("status는 문자열이어야 합니다.")
    return value.strip() or DEFAULT_STATUS


# ── 공개 함수 ────────────────────────────────────────────────
def init_db() -> None:
    """notices 테이블이 없으면 생성한다. (여러 번 호출해도 안전)"""
    with _connect() as conn:
        conn.execute(_CREATE_TABLE_SQL)


def save_notice(notice_info: dict) -> int:
    """추출된 고지서 정보를 저장하고 생성된 id를 반환한다.

    Args:
        notice_info: title/agency/amount/due_date/payment_method/status 키를 갖는 dict.

    Returns:
        저장된 행의 id.

    Raises:
        ValueError: 입력 값 검증 실패.
        sqlite3.Error: DB 오류 (숨기지 않고 그대로 전파).
    """
    if not isinstance(notice_info, dict):
        raise ValueError("notice_info는 dict여야 합니다.")

    title = _validate_text(notice_info.get("title"), "title")
    agency = _validate_text(notice_info.get("agency"), "agency")
    amount = _validate_amount(notice_info.get("amount"))
    due_date = _validate_date(notice_info.get("due_date"))
    payment_method = _validate_text(notice_info.get("payment_method"), "payment_method")
    status = _validate_status(notice_info.get("status"))

    # 실행 환경의 시간대에 영향받지 않도록 UTC로 기록한다.
    created_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO notices "
            "(title, agency, amount, due_date, payment_method, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, agency, amount, due_date, payment_method, status, created_at),
        )
        notice_id = cur.lastrowid

    return int(notice_id)


def list_notices() -> list[dict]:
    """저장된 고지서 전체를 반환한다.

    정렬: 납부기한 오름차순 → 기한 없는 항목은 마지막 → 같은 기한이면 최근 저장 순.
    """
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM notices "
            "ORDER BY (due_date IS NULL) ASC, due_date ASC, id DESC"
        ).fetchall()

    return [dict(row) for row in rows]


def get_notice(notice_id: int) -> dict | None:
    """id로 고지서 1건을 조회한다. 없으면 None."""
    if isinstance(notice_id, bool) or not isinstance(notice_id, int):
        raise ValueError("notice_id는 정수여야 합니다.")

    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM notices WHERE id = ?",
            (notice_id,),
        ).fetchone()

    return dict(row) if row is not None else None


def list_unpaid_notices_due_between(start_date: str, end_date: str) -> list[dict]:
    """지정 기간(양끝 포함)에 납부기한이 있는 '미납' 고지서를 기한 오름차순으로 반환한다.

    Args:
        start_date: 시작일 (YYYY-MM-DD).
        end_date: 종료일 (YYYY-MM-DD).

    Raises:
        ValueError: 날짜 형식이 올바르지 않거나 시작일이 종료일보다 늦은 경우.
    """
    if start_date is None or end_date is None:
        raise ValueError("start_date와 end_date는 YYYY-MM-DD 문자열이어야 합니다.")
    start_date = _validate_date(start_date, "start_date")
    end_date = _validate_date(end_date, "end_date")
    if start_date > end_date:
        raise ValueError("start_date는 end_date보다 늦을 수 없습니다.")

    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM notices "
            "WHERE status = ? AND due_date IS NOT NULL "
            "AND due_date >= ? AND due_date <= ? "
            "ORDER BY due_date ASC, id DESC",
            (DEFAULT_STATUS, start_date, end_date),
        ).fetchall()

    return [dict(row) for row in rows]


def get_nearest_upcoming_notice(today: str) -> dict | None:
    """오늘 또는 그 이후 기한을 가진 '미납' 고지서 중 가장 가까운 1건을 반환한다.

    Args:
        today: 기준일 (YYYY-MM-DD).

    Returns:
        해당 고지서 dict 또는 None.

    Raises:
        ValueError: today가 올바른 날짜 문자열이 아닌 경우.
    """
    if today is None:
        raise ValueError("today는 YYYY-MM-DD 문자열이어야 합니다.")
    today = _validate_date(today, "today")

    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM notices "
            "WHERE status = ? AND due_date IS NOT NULL AND due_date >= ? "
            "ORDER BY due_date ASC, id DESC LIMIT 1",
            (DEFAULT_STATUS, today),
        ).fetchone()

    return dict(row) if row is not None else None
