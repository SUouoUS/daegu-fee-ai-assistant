"""
test_data_rules.py
금액·날짜 해석, 기간 조회, AI 답변의 DB 근거, PDF 파서, 저장 무결성 회귀 테스트.

화면(AppTest) 검증은 test_app_stability_ui.py에서 따로 한다.
google-genai SDK·httpx·dotenv는 기존 테스트의 가짜 모듈을 쓰므로 실제 API 호출은 없다.
DB는 임시 디렉터리를 사용하며 프로젝트의 data/bills.db는 건드리지 않는다.

테스트 항목:
1. parse_amount: None·bool·int·float·문자열·음수·해석 불가 값
2. parse_due_date: 실제 YYYY-MM-DD만 인정 ('2026-99-99', '2026-02-31', '2026/09/18', '확인불가', 빈 값 제외)
3. 기간 조회: 잘못된 날짜는 가장 가까운 기한·이번 주·이번 달에서 제외, 완료 건 제외,
   정상 데이터의 결과·순서는 기존 문자열 비교 결과와 동일
4. 금액 합산: 문자열 금액 '45,200' 포함, 0원 유지, None·해석 불가 금액 제외
5. AI 답변의 DB 근거: this_week/this_month/monthly_total/nearest_due/list_all/unknown,
   데이터 0건 안내, 선택 고지서만 근거로 사용, 존재하지 않는 id 안내
6. 로컬 fallback 예시: API 키 없이 호출 0회로 DB 기준 답변
7. 외부 전송 코드 점검: assistant.py에 json.dumps·DB 직렬화가 없다
8. PDF 파서: 텍스트 PDF, 빈 페이지, 빈 바이트, 손상 파일, 암호 PDF, 페이지 범위
9. 저장 무결성: 바인딩할 수 없는 값으로 저장 실패 시 부분 저장이 남지 않는다
10. 정리 후 전역 상태가 같은 프로세스에서 복원되고 운영 DB 수정 시각이 그대로다
"""

import datetime
import inspect
import json
import os
import sqlite3
import sys
import tempfile

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except (AttributeError, OSError):
    pass

# 가짜 SDK·스냅샷/복원 헬퍼 재사용 (import 시점에는 sys.modules를 바꾸지 않는다)
import test_assistant_panel_ui as ui  # noqa: E402

db = ui.db_module
privacy_test = ui.privacy_test
_CAPTURED = privacy_test._CAPTURED
_NEXT_BEHAVIOR = privacy_test._NEXT_BEHAVIOR

_PROD_DB = os.path.join(_project_root, "data", "bills.db")


def _mtime(path):
    return os.stat(path).st_mtime_ns if os.path.exists(path) else None


def _clear():
    for bill in db.get_bills(include_paid=True):
        db.delete_bill(bill["id"])


def _day(offset: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


def _save(title, *, due, amount=1000, agency=None, method=None):
    return db.save_bill(
        {"title": title, "agency": agency or f"{title}기관", "amount": amount,
         "due_date": due, "payment_method": method}
    )


def _ask(assistant, intent, question, selected=None):
    _NEXT_BEHAVIOR[0] = ("text", json.dumps({"intent": intent}))
    return assistant.ask(question, selected_bill_id=selected)


# ── 1·2. 순수 해석 함수 ───────────────────────────────────────


def test_parse_amount():
    cases = [
        (None, None), (True, None), (False, None),
        (0, 0), (45200, 45200), (-1, None),
        (45200.0, 45200), (0.0, 0), (45200.5, None), (-3.0, None), (float("nan"), None),
        (float("inf"), None),
        ("45,200", 45200), ("45,200원", 45200), (" 45 200 원 ", 45200), ("0", 0), ("0원", 0),
        ("", None), ("원", None), ("-1,000", None), ("12.5", None), ("abc", None),
        ("약 4만원", None), ("１２３", None),  # 전각 숫자는 정수 문자열로 보지 않는다
        ([1], None), ({"a": 1}, None), (b"100", None),
    ]
    for value, expected in cases:
        got = db.parse_amount(value)
        assert got == expected and type(got) is type(expected), (
            f"parse_amount({value!r}) = {got!r} ({type(got).__name__}), 기대 {expected!r}"
        )
    print(f"PASS 1: parse_amount {len(cases)}개 사례 (0은 0, 음수·소수·해석 불가는 None)")


def test_parse_due_date():
    valid = {"2026-09-17": datetime.date(2026, 9, 17), "2024-02-29": datetime.date(2024, 2, 29)}
    invalid = [
        "2026-99-99", "2026-02-31", "2025-02-29", "2026/09/18", "확인불가", "", " ",
        "2026-09-17 ", " 2026-09-17", "2026-09-17T00:00", "20260917", "2026-9-17",
        None, 20260917, datetime.date(2026, 9, 17),
    ]
    for value, expected in valid.items():
        assert db.parse_due_date(value) == expected, f"{value!r} -> {db.parse_due_date(value)!r}"
    for value in invalid:
        assert db.parse_due_date(value) is None, (
            f"parse_due_date({value!r})는 None이어야 합니다: {db.parse_due_date(value)!r}"
        )
    print(f"PASS 2: parse_due_date 유효 {len(valid)}개 / 무효 {len(invalid)}개")


# ── 3·4. 기간 조회와 합산 ─────────────────────────────────────


def _legacy_string_query(sql_range_start, sql_range_end=None):
    """수정 전과 같은 문자열 비교 쿼리 (정상 데이터 결과 동일성 비교용)."""
    conn = sqlite3.connect(db._DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if sql_range_end is None:
            rows = conn.execute(
                "SELECT * FROM bills WHERE status='미납' AND due_date IS NOT NULL "
                "AND due_date >= ? ORDER BY due_date ASC", (sql_range_start,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bills WHERE status='미납' AND due_date IS NOT NULL "
                "AND due_date >= ? AND due_date <= ? ORDER BY due_date ASC",
                (sql_range_start, sql_range_end),
            ).fetchall()
        return [dict(r)["id"] for r in rows]
    finally:
        conn.close()


def test_period_queries_exclude_invalid_dates():
    _clear()
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    sunday = monday + datetime.timedelta(days=6)
    first = today.replace(day=1)
    in_week = monday.isoformat()

    invalid_ids = [
        _save(f"잘못된날짜{i}", due=due)
        for i, due in enumerate([
            f"{today.year}-99-99", f"{today.year + 1}-02-31", f"{today.year}/{today.month:02d}/{today.day:02d}",
            "확인불가", "", f"{in_week}T00:00", f"{in_week} ", f"{today.year}-{today.month:02d}-00",
        ])
    ]
    assert not db.get_bills_due_this_week(), "잘못된 날짜가 이번 주 조회에 포함되었습니다."
    assert not db.get_bills_due_this_month(), "잘못된 날짜가 이번 달 조회에 포함되었습니다."
    assert db.get_nearest_due_bill() is None, "잘못된 날짜가 가장 가까운 기한으로 선택되었습니다."

    # 정상 데이터 추가: 결과는 정상 건만, 완료 건 제외
    ok_today = _save("정상오늘", due=today.isoformat(), amount=1000)
    ok_sunday = _save("정상일요일", due=sunday.isoformat(), amount=0)
    ok_next = _save("정상다음달", due=(first.replace(day=28) + datetime.timedelta(days=10)).isoformat())
    paid = _save("완료건", due=today.isoformat(), amount=777)
    db.mark_as_paid(paid)

    near = db.get_nearest_due_bill()
    assert near is not None and near["id"] == ok_today, f"가장 가까운 기한이 다릅니다: {near}"
    week_ids = [b["id"] for b in db.get_bills_due_this_week()]
    month_ids = [b["id"] for b in db.get_bills_due_this_month()]
    assert paid not in week_ids and paid not in month_ids, "완료 건이 기간 조회에 포함되었습니다."
    for bad in invalid_ids:
        assert bad not in week_ids and bad not in month_ids, "잘못된 날짜가 기간 조회에 섞였습니다."

    # 정상 데이터만 남긴 상태에서 기존 문자열 비교 결과와 id·순서가 같아야 한다.
    for bad in invalid_ids:
        db.delete_bill(bad)
    last = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    assert week_ids == _legacy_string_query(monday.isoformat(), sunday.isoformat()), (
        f"정상 데이터의 이번 주 결과가 달라졌습니다: {week_ids}"
    )
    assert month_ids == _legacy_string_query(first.isoformat(), last.isoformat()), (
        f"정상 데이터의 이번 달 결과가 달라졌습니다: {month_ids}"
    )
    assert near["id"] == _legacy_string_query(today.isoformat())[0]
    assert ok_sunday in week_ids and ok_next not in month_ids
    print("PASS 3: 잘못된 날짜 8종 제외 / 완료 건 제외 / 정상 데이터 결과·순서는 기존과 동일")


def test_amount_totals():
    _clear()
    today = _day(0)
    _save("문자열금액", due=today, amount="45,200")
    _save("영원", due=today, amount=0)
    _save("금액없음", due=today, amount=None)
    _save("해석불가", due=today, amount="약 4만원")
    paid = _save("완료금액", due=today, amount=999)
    db.mark_as_paid(paid)
    assert db.get_bills_due_this_month()[0]["amount"] in ("45,200", 0, None, "약 4만원")
    total = db.get_total_amount_this_month()
    assert total == 45200, f"이번 달 합계가 {total}입니다. 45200이어야 합니다."
    print("PASS 4: 합계 = 문자열 '45,200' + 0원, None·해석 불가·완료 건 제외 -> 45200")


# ── 5·6·7. AI 답변 근거 ───────────────────────────────────────


def test_assistant_answers_from_db(assistant):
    _clear()
    # 0건 안내
    empty_expect = {
        "this_week": "없습니다", "this_month": "없습니다", "monthly_total": "없습니다",
        "nearest_due": "없습니다", "list_all": "없습니다", "unknown": "고지서 관련 질문만",
    }
    for intent, needle in empty_expect.items():
        answer = _ask(assistant, intent, "질문")
        assert needle in answer, f"0건 {intent}: {answer}"
    print("PASS 5: 데이터 0건에서 의도별 안내 문구")

    today = datetime.date.today()
    a = _save("ZZ근거A상수도", due=today.isoformat(), amount="45,200", agency="ZZ근거A기관", method="ZZ근거A방법")
    b = _save("ZZ근거B가스", due=_day(40), amount=0, agency="ZZ근거B기관", method="ZZ근거B방법")
    bad = _save("ZZ근거C잘못된날짜", due="2026-99-99", amount=500)
    paid = _save("ZZ근거D완료", due=today.isoformat(), amount=321)
    db.mark_as_paid(paid)

    week = _ask(assistant, "this_week", "이번 주")
    assert "ZZ근거A상수도" in week and "45,200원" in week, week
    for absent in ("ZZ근거B가스", "ZZ근거C잘못된날짜", "ZZ근거D완료"):
        assert absent not in week, f"this_week에 {absent}: {week}"

    month_total = _ask(assistant, "monthly_total", "이번 달 합계")
    assert "45,200원" in month_total and "ZZ근거D완료" not in month_total, month_total

    this_month = _ask(assistant, "this_month", "이번 달")
    assert "ZZ근거A상수도" in this_month and "ZZ근거C잘못된날짜" not in this_month, this_month

    nearest = _ask(assistant, "nearest_due", "가장 가까운")
    assert "ZZ근거A상수도" in nearest and "ZZ근거C잘못된날짜" not in nearest, nearest

    listed = _ask(assistant, "list_all", "전체")
    for present in ("ZZ근거A상수도", "ZZ근거B가스", "ZZ근거C잘못된날짜"):
        assert present in listed, f"list_all에 {present} 없음: {listed}"
    assert "ZZ근거D완료" not in listed and "0원" in listed, listed

    unknown = _ask(assistant, "unknown", "날씨")
    assert "ZZ근거" not in unknown, unknown
    print("PASS 5: this_week/this_month/monthly_total/nearest_due/list_all/unknown이 DB 결과로만 답변")

    # 선택 고지서 범위
    summary_b = _ask(assistant, "bill_summary", "이 고지서 요약", selected=b)
    assert "ZZ근거B가스" in summary_b and "0원" in summary_b and "금액 미확인" not in summary_b
    for other in ("ZZ근거A", "ZZ근거C", "ZZ근거D"):
        assert other not in summary_b, f"선택하지 않은 고지서 {other}가 섞임: {summary_b}"
    amount_a = _ask(assistant, "bill_amount", "금액?", selected=a)
    assert "45,200원" in amount_a and "ZZ근거B" not in amount_a, amount_a
    for bad_id in (999999, "abc", -1):
        answer = _ask(assistant, "bill_summary", "이 고지서 요약", selected=bad_id)
        assert "찾을 수 없습니다" in answer and "ZZ근거" not in answer, f"{bad_id!r}: {answer}"
    print("PASS 5: 선택 고지서만 근거 / 존재하지 않는 id(999999·'abc'·-1)는 안내")


def test_local_fallback_without_api(assistant):
    saved = os.environ.pop("GEMINI_API_KEY", None)
    try:
        _CAPTURED.clear()
        answer = assistant.ask_with_fallback("가장 가까운 납부기한은?")
        assert answer.startswith(assistant.FALLBACK_NOTICE) and "ZZ근거A상수도" in answer, answer
        assert len(_CAPTURED) == 0, f"API 키가 없는데 호출 {len(_CAPTURED)}회"
    finally:
        if saved is not None:
            os.environ["GEMINI_API_KEY"] = saved
    print("PASS 6: API 키 없이 '가장 가까운 납부기한은?' -> 호출 0회 / DB 기준 대체 답변")


def test_no_bill_serialization_in_assistant(assistant):
    source = inspect.getsource(assistant)
    assert "json.dumps" not in source, "assistant.py에 json.dumps가 있습니다."
    classify = inspect.getsource(assistant._classify_intent)
    for forbidden in ("get_bills", "get_bill_by_id", "selected_bill_id", "[DB 조회 결과]"):
        assert forbidden not in classify, f"분류 요청 함수에 {forbidden!r}가 있습니다."
    assert "contents=question" in classify, "분류 요청 contents가 질문 문자열이 아닙니다."
    print("PASS 7: assistant.py에 json.dumps 없음 / 분류 요청에 DB 조회·선택 id 없음")


# ── 8. PDF 파서 ───────────────────────────────────────────────


def test_pdf_parser():
    import pymupdf
    from services.parser import extract_text_from_pdf, pdf_info

    def make_pdf(text=None, pages=1, **save_kwargs):
        doc = pymupdf.open()
        try:
            for i in range(pages):
                page = doc.new_page()
                if text:
                    page.insert_text((72, 72), f"{text} page{i + 1}")
            return doc.tobytes(**save_kwargs)
        finally:
            doc.close()

    text_pdf = make_pdf("Water bill 45200 due 2026-09-30", pages=2)
    assert pdf_info(text_pdf) == {"needs_password": False, "authenticated": True, "page_count": 2}
    assert "45200" in extract_text_from_pdf(text_pdf, page_index=1)

    def expect_value_error(needle, func, *args, **kwargs):
        try:
            func(*args, **kwargs)
        except ValueError as e:
            assert needle in str(e), f"안내 문구가 다릅니다: {e}"
            return
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"ValueError를 기대했지만 {type(e).__name__}: {e}")
        raise AssertionError("ValueError가 발생하지 않았습니다.")

    expect_value_error("텍스트를 찾지 못했습니다", extract_text_from_pdf, make_pdf(None))
    expect_value_error("비어 있습니다", extract_text_from_pdf, b"")
    expect_value_error("비어 있습니다", pdf_info, b"")
    expect_value_error("유효한 PDF 파일이 아니거나", pdf_info, b"this is not a pdf")
    expect_value_error("유효한 PDF 파일이 아니거나", extract_text_from_pdf, b"%PDF-1.4 broken")
    expect_value_error("유효하지 않은 페이지 번호", extract_text_from_pdf, text_pdf, page_index=5)

    locked = make_pdf("Secret bill", encryption=pymupdf.PDF_ENCRYPT_AES_256,
                      user_pw="userpw", owner_pw="ownerpw")
    assert pdf_info(locked)["needs_password"] is True and pdf_info(locked)["authenticated"] is False
    expect_value_error("비밀번호가 필요", extract_text_from_pdf, locked)
    expect_value_error("비밀번호가 올바르지", extract_text_from_pdf, locked, password="wrong")
    assert "Secret bill" in extract_text_from_pdf(locked, password="userpw")
    print("PASS 8: PDF 텍스트 추출 / 빈 페이지·빈 바이트·손상 파일·페이지 범위·암호 PDF 안내")


# ── 9. 저장 무결성 ────────────────────────────────────────────


def test_failed_save_leaves_no_partial_row():
    _clear()
    before = db.get_bills(include_paid=True)
    try:
        db.save_bill({"title": "바인딩불가", "amount": {"not": "bindable"}})
    except sqlite3.Error:
        pass
    else:
        raise AssertionError("바인딩할 수 없는 값인데 저장이 성공했습니다.")
    assert db.get_bills(include_paid=True) == before, "실패한 저장이 부분 행을 남겼습니다."
    print("PASS 9: 저장 실패 시 부분 저장 없음")


# ── 실행 ──────────────────────────────────────────────────────


def main():
    prod_before = _mtime(_PROD_DB)
    snapshot = ui._snapshot_state()
    tmpdir = tempfile.TemporaryDirectory()
    try:
        ui._install_fake_sdk_keep_google()
        os.environ["GEMINI_API_KEY"] = "TEST-DUMMY-KEY-NOT-REAL"
        os.environ["GEMINI_MODEL"] = "test-model"
        db._DB_DIR = tmpdir.name
        db._DB_PATH = os.path.join(tmpdir.name, "test_data_rules.db")
        db.init_db()

        import services.assistant as assistant

        test_parse_amount()
        test_parse_due_date()
        test_period_queries_exclude_invalid_dates()
        test_amount_totals()
        test_assistant_answers_from_db(assistant)
        test_local_fallback_without_api(assistant)
        test_no_bill_serialization_in_assistant(assistant)
        test_pdf_parser()
        test_failed_save_leaves_no_partial_row()
        print("\n전체 통과: 금액·날짜 해석과 DB 근거 답변 규칙이 유지됩니다.")
    finally:
        ui._restore_state(snapshot)
        tmpdir.cleanup()

    print("\n[정리 후 상태 복원 검증 - 테스트를 실행한 같은 프로세스]")
    ui._verify_restored(snapshot)
    assert not os.path.exists(tmpdir.name), f"임시 디렉터리가 남아 있습니다: {tmpdir.name}"
    assert _mtime(_PROD_DB) == prod_before, "운영 DB(data/bills.db) 수정 시각이 바뀌었습니다."
    print("PASS 10: 임시 DB 삭제 / 운영 DB 수정 시각 동일")
    return 0


if __name__ == "__main__":
    sys.exit(main())
