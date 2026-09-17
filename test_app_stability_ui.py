"""
test_app_stability_ui.py
Streamlit 화면(AppTest)의 버튼 경로·세션 상태·표시 안정성 회귀 테스트.

금액·날짜 해석 규칙과 AI 답변 근거는 test_data_rules.py에서 따로 검증한다.
google-genai SDK는 가짜 모듈이므로 실제 API 호출은 없다. DB는 임시 디렉터리를 쓴다.

테스트 항목:
1. 납부 완료 → 미납 복원을 실제 버튼(확인 단계 포함)으로 수행하고 DB status·paid_at 변화를 확인
2. 삭제를 실제 버튼(취소 → 재시도 → 확인)으로 수행하고 선택 상태·상세 카드·AI 패널 정리를 확인
3. 선택·납부완료 보기·대화 기록이 등록 화면 왕복과 재실행 후에도 유지 (API 호출 0회)
4. 금액 표시: 0원 / 금액 미확인 / 문자열 '45,200' / 해석 불가 — 대시보드·상세 중단 없음
5. 날짜 배지: 오늘 마감 / 기한 지남 / 잘못된 날짜 — 요약 카드에 완료·잘못된 날짜 건이 오지 않음
6. 세션에 남은 존재하지 않는 선택 id(999999·'abc')는 해제되고 안내가 표시됨
7. 긴 제목·기관명과 HTML 문자열이 이스케이프되어 HTML 허용 마크다운에 원문 태그가 없음
8. 텍스트 직접 입력 → 추출 → 누락 항목 안내 → 자동 저장 없음 → 저장 클릭 시 None 필드로 저장
9. 정리 후 전역 상태가 같은 프로세스에서 복원되고 운영 DB 수정 시각이 그대로다
(0건 화면·완료 건만 있는 화면은 test_assistant_panel_ui.py 14·16에서 확인한다.)
"""

import datetime
import json
import os
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


def _save(title, *, due, amount=1000, agency=None):
    return db.save_bill({"title": title, "agency": agency or f"{title}기관",
                         "amount": amount, "due_date": due, "payment_method": "자동이체"})


def _texts(at):
    """화면 텍스트. CSS를 주입하는 <style> 마크다운은 클래스 이름이 들어 있어 제외한다."""
    return [t for t in ui._markdown_values(at) if not t.lstrip().startswith("<style>")]


def _has_block(at, key):
    return ui._find_block(at._tree, key) is not None


def _new_dashboard(selected=None):
    at = ui._new_app()
    if selected is not None:
        at.session_state["selected_bill_id"] = selected
    return ui._run(at)


# ── 테스트 ────────────────────────────────────────────────────


def test_pay_and_undo_buttons():
    """1: 버튼으로 납부 완료(확인 단계) → 미납 복원."""
    _clear()
    bid = _save("납부버튼고지서", due=_day(5))
    _CAPTURED.clear()
    at = _new_dashboard()
    ui._button(at, f"sel_btn_{bid}").click()
    ui._run(at)

    ui._button(at, f"btn_pay_{bid}").click()
    ui._run(at)
    assert any("정말로 납부 완료로 표시할까요?" in w.value for w in at.warning), "확인 단계가 없습니다."
    assert db.get_bill_by_id(bid)["status"] == "미납", "확인 전에 상태가 바뀌었습니다."

    ui._button(at, f"yes_pay_{bid}").click()
    ui._run(at)
    row = db.get_bill_by_id(bid)
    assert row["status"] == "납부완료" and row["paid_at"], f"납부완료 반영 실패: {row}"
    datetime.datetime.fromisoformat(row["paid_at"])  # 형식이 깨지면 ValueError
    assert at.session_state["selected_bill_id"] == bid, "납부 완료 후 선택이 풀렸습니다."
    assert any("납부 처리 일시" in t for t in _texts(at)), "납부 처리 일시가 표시되지 않습니다."
    ui._button(at, f"btn_undo_{bid}")  # 없으면 KeyError

    ui._button(at, f"btn_undo_{bid}").click()
    ui._run(at)
    row = db.get_bill_by_id(bid)
    assert row["status"] == "미납" and row["paid_at"] is None, f"미납 복원 실패: {row}"
    assert at.session_state["selected_bill_id"] == bid
    ui._button(at, f"btn_pay_{bid}")
    assert len(_CAPTURED) == 0, "상태 변경 중 API가 호출되었습니다."
    print("PASS 1: 납부 완료(확인 단계) -> status·paid_at 설정 -> 미납 복원 -> paid_at None / 선택 유지")


def test_delete_buttons():
    """2: 삭제 확인 단계와 삭제 후 선택 정리."""
    _clear()
    bid = _save("삭제버튼고지서", due=_day(3))
    keep = _save("남는고지서", due=_day(8))
    at = _new_dashboard()
    ui._button(at, f"sel_btn_{bid}").click()
    ui._run(at)

    ui._button(at, f"btn_del_{bid}").click()
    ui._run(at)
    assert any("정말로 삭제하시겠습니까?" in e.value for e in at.error), "삭제 확인 단계가 없습니다."
    assert db.get_bill_by_id(bid) is not None, "확인 전에 삭제되었습니다."

    ui._button(at, f"no_del_{bid}").click()
    ui._run(at)
    assert db.get_bill_by_id(bid) is not None and not any(
        "정말로 삭제" in e.value for e in at.error
    ), "취소가 동작하지 않았습니다."

    ui._button(at, f"btn_del_{bid}").click()
    ui._run(at)
    ui._button(at, f"yes_del_{bid}").click()
    ui._run(at)
    assert db.get_bill_by_id(bid) is None, "삭제되지 않았습니다."
    assert db.get_bill_by_id(keep) is not None, "다른 고지서까지 삭제되었습니다."
    assert at.session_state["selected_bill_id"] is None, "삭제 후 선택 id가 남았습니다."
    assert not _has_block(at, "bill_detail_card"), "삭제된 고지서의 상세 카드가 남았습니다."
    texts = _texts(at)
    assert any(ui.SELECT_HINT in t for t in texts), "AI 패널이 선택 안내로 돌아가지 않았습니다."
    assert not any("삭제버튼고지서" in t for t in texts), "삭제된 고지서가 화면에 남았습니다."
    ui._run(at)  # 재실행해도 예외 없음
    print("PASS 2: 삭제 확인 -> 취소 -> 재시도 -> 삭제 / 선택·상세·AI 대상 정리")


def test_session_survives_view_switch():
    """3: 선택·체크박스·대화가 등록 화면 왕복과 재실행 후에도 유지."""
    _clear()
    bid = _save("세션유지고지서", due=_day(4))
    paid = _save("완료세션고지서", due=_day(2))
    db.mark_as_paid(paid)
    chat = [{"role": "user", "content": "이전 질문"}, {"role": "assistant", "content": "이전 답변"}]

    _CAPTURED.clear()
    at = _new_dashboard()
    at.session_state["chat_messages"] = list(chat)
    ui._button(at, f"sel_btn_{bid}").click()
    ui._run(at)
    at.checkbox(key="chk_show_paid").check()
    ui._run(at)
    assert at.session_state["show_paid"] is True
    ui._button(at, f"sel_btn_{paid}")  # 완료 건이 목록에 보인다

    next(b for b in at.button if "고지서 추가" in b.label).click()
    ui._run(at)
    assert at.session_state["current_view"] == "register"
    next(b for b in at.button if "목록으로" in b.label).click()
    ui._run(at)
    for _ in range(2):
        ui._run(at)

    assert at.session_state["current_view"] == "dashboard"
    assert at.session_state["selected_bill_id"] == bid, "선택이 초기화되었습니다."
    assert at.session_state["show_paid"] is True, "납부완료 보기 상태가 초기화되었습니다."
    assert at.checkbox(key="chk_show_paid").value is True, "체크박스 표시가 초기화되었습니다."
    assert at.session_state["chat_messages"] == chat, "대화 기록이 바뀌었습니다."
    assert at.session_state["assistant_turn_count"] == 0 and len(_CAPTURED) == 0, (
        "화면 전환·재실행으로 질문이 전송되었습니다."
    )
    print("PASS 3: 등록 화면 왕복 + 재실행 2회 후 선택·체크박스·대화 유지 / API 호출 0회")


def test_amount_display():
    """4: 금액 표시와 문자열 금액 안정성."""
    _clear()
    zero = _save("영원고지서", due=_day(3), amount=0)
    none = _save("금액없음고지서", due=_day(4), amount=None)
    text = _save("문자열금액고지서", due=_day(1), amount="45,200")
    weird = _save("해석불가금액고지서", due=_day(6), amount="약 4만원")

    at = _new_dashboard()
    values = _texts(at)
    assert "0원" in values, "0원 고지서가 0원으로 표시되지 않습니다."
    assert values.count("금액 미확인") == 2, f"금액 미확인은 None·해석 불가 2건이어야 합니다: {values.count('금액 미확인')}"
    assert "45,200원" in values, "문자열 금액이 45,200원으로 표시되지 않습니다."
    assert any("nearest-card" in t and "45,200원" in t for t in values), "요약 카드 금액 표시 실패"

    for bid, expected in ((zero, "0원"), (none, "확인되지 않음"), (text, "45,200원"), (weird, "확인되지 않음")):
        at = _new_dashboard(selected=bid)
        grids = [t for t in _texts(at) if "detail-grid" in t]
        assert grids and expected in grids[0], f"상세 금액 표시 오류 ({bid}): {grids}"
    print("PASS 4: 0원 / 금액 미확인 / '45,200' -> 45,200원 / 해석 불가 -> 미확인, 목록·요약·상세 중단 없음")


def test_date_badges_and_summary_card():
    """5: 오늘 마감·기한 지남·잘못된 날짜 표시와 요약 카드 대상."""
    _clear()
    _save("오늘마감고지서", due=_day(0))
    _save("기한지남고지서", due=_day(-2))
    bad_ids = [_save(f"잘못된날짜{i}", due=d) for i, d in enumerate(["2026-99-99", "확인불가", "", "2026/09/18"])]
    paid = _save("완료된내일고지서", due=_day(-1))
    db.mark_as_paid(paid)

    at = ui._new_app()
    at.session_state["show_paid"] = True
    ui._run(at)
    values = _texts(at)
    badges = " ".join(t for t in values if "status-badge" in t)
    assert "오늘 마감" in badges and "기한 지남" in badges and "완료" in badges, badges
    for bid in bad_ids:
        ui._button(at, f"sel_btn_{bid}")  # 잘못된 날짜도 목록에 표시된다
    cards = [t for t in values if "nearest-card" in t]
    assert cards and "오늘마감고지서" in cards[0], f"요약 카드 대상이 다릅니다: {cards}"
    assert "잘못된날짜" not in cards[0] and "완료된" not in cards[0]

    # 오늘 이후 유효 기한이 없으면 잘못된 날짜가 아니라 '없음' 안내
    _clear()
    _save("잘못된날짜만", due="2026-99-99")
    at = _new_dashboard()
    cards = [t for t in _texts(at) if "nearest-card" in t]
    assert cards and "예정된 미납 고지서가 없습니다" in cards[0], f"요약 카드: {cards}"
    print("PASS 5: 오늘 마감·기한 지남·완료 배지 / 잘못된 날짜 목록 표시 유지·요약 카드 제외")


def test_invalid_selected_id_in_session():
    """6: 세션에 남은 존재하지 않는 선택 id."""
    _clear()
    _save("기존고지서", due=_day(2))
    for bad in (999999, "abc"):
        _CAPTURED.clear()
        at = _new_dashboard(selected=bad)
        assert at.session_state["selected_bill_id"] is None, f"{bad!r} 선택이 해제되지 않았습니다."
        assert any("찾을 수 없어" in i.value for i in at.info), f"{bad!r}: 안내가 없습니다."
        assert not _has_block(at, "bill_detail_card")
        assert len(_CAPTURED) == 0
    print("PASS 6: 존재하지 않는 선택 id(999999·'abc') -> 선택 해제·안내 / 중단 없음")


def test_long_and_html_text_escaped():
    """7: 긴 텍스트와 HTML 문자열."""
    _clear()
    long_title = "<script>alert(1)</script><b>굵게</b> " + "아주긴고지서명" * 30
    long_agency = "<img src=x onerror=alert(2)> " + "대구광역시수성구상하수도사업본부" * 15
    bid = _save(long_title, due=_day(1), agency=long_agency)

    at = _new_dashboard(selected=bid)
    at.session_state["chat_messages"] = [{"role": "user", "content": "<img src=y onerror=alert(3)>질문"}]
    ui._run(at)

    html_md = [
        el for el in at.markdown
        if getattr(el.proto, "allow_html", False) and not el.value.lstrip().startswith("<style>")
    ]
    assert html_md, "HTML 허용 마크다운을 찾지 못했습니다."
    for el in html_md:
        for raw in ("<script", "<img", "<b>굵게"):
            assert raw not in el.value, f"HTML 허용 영역에 원문 태그 {raw!r}가 그대로 들어갔습니다."
    assert any("&lt;script&gt;" in el.value for el in html_md), "제목이 이스케이프되지 않았습니다."
    chat_md = [el for el in at.markdown if "onerror=alert(3)" in el.value]
    assert chat_md and all(not getattr(el.proto, "allow_html", False) for el in chat_md), (
        "사용자 질문이 HTML 허용 마크다운으로 출력됩니다."
    )
    print("PASS 7: 긴 제목·기관명 렌더 / HTML 문자열 이스케이프 / 질문은 HTML 비허용 출력")


def test_text_input_register_flow():
    """8: 텍스트 직접 입력 → 추출 → 누락 안내 → 저장."""
    _clear()
    # 추출기가 쓰는 설정 클래스만 가짜 types 모듈에 보탠다 (정리 시 가짜 모듈째 제거됨).
    sys.modules["google.genai.types"].AutomaticFunctionCallingConfig = privacy_test._FakeConfigObject

    at = _new_dashboard()
    next(b for b in at.button if "고지서 추가" in b.label).click()
    ui._run(at)
    at.radio[0].set_value("텍스트 직접 입력")
    ui._run(at)

    # 빈 입력
    next(b for b in at.button if b.label == "텍스트 확인").click()
    ui._run(at)
    assert any("고지서 내용을 입력해 주세요" in w.value for w in at.warning), "빈 입력 안내가 없습니다."
    assert at.session_state["notice_text"] == ""

    notice = "수성구 상하수도 사용료 납부 고지서\n발급: 대구광역시 수성구"
    at.text_area[0].input(notice)
    ui._run(at)
    next(b for b in at.button if b.label == "텍스트 확인").click()
    ui._run(at)
    assert at.session_state["notice_text"] == notice

    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", json.dumps({
        "title": "수성구 상하수도 사용료", "agency": "대구광역시 수성구",
        "amount": None, "due_date": None, "payment_method": None,
    }))
    next(b for b in at.button if "AI로 고지서 분석" in b.label).click()
    ui._run(at)
    assert len(_CAPTURED) == 1 and _CAPTURED[0]["contents"] == notice, "추출 요청이 1회·원문 전송이 아닙니다."
    info_texts = [i.value for i in at.info]
    assert any("납부 금액" in t and "납부 기한" in t and "납부 방법" in t for t in info_texts), info_texts
    assert not db.get_bills(include_paid=True), "저장 버튼 없이 자동 저장되었습니다."

    ui._ok_response()
    next(b for b in at.button if b.label == "고지서 저장").click()
    ui._run(at)
    rows = db.get_bills(include_paid=True)
    assert len(rows) == 1, f"저장 건수 {len(rows)}"
    row = rows[0]
    assert row["title"] == "수성구 상하수도 사용료" and row["amount"] is None and row["due_date"] is None
    assert at.session_state["current_view"] == "dashboard"
    assert at.session_state["selected_bill_id"] == row["id"]
    assert at.session_state["notice_info"] is None and at.session_state["notice_text"] == ""
    assert "금액 미확인" in _texts(at)
    print("PASS 8: 빈 입력 안내 -> 추출 1회 -> 누락 안내·자동 저장 없음 -> None 필드 저장·대시보드 선택")


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
        db._DB_PATH = os.path.join(tmpdir.name, "test_app_stability.db")
        db.init_db()
        ui._ok_response()

        test_pay_and_undo_buttons()
        test_delete_buttons()
        test_session_survives_view_switch()
        test_amount_display()
        test_date_badges_and_summary_card()
        test_invalid_selected_id_in_session()
        test_long_and_html_text_escaped()
        test_text_input_register_flow()
        print("\n전체 통과: 화면 버튼 경로·세션 상태·표시가 안정적으로 동작합니다.")
    finally:
        ui._restore_state(snapshot)
        tmpdir.cleanup()

    print("\n[정리 후 상태 복원 검증 - 테스트를 실행한 같은 프로세스]")
    ui._verify_restored(snapshot)
    assert not os.path.exists(tmpdir.name), f"임시 디렉터리가 남아 있습니다: {tmpdir.name}"
    assert _mtime(_PROD_DB) == prod_before, "운영 DB(data/bills.db) 수정 시각이 바뀌었습니다."
    print("PASS 9: 임시 DB 삭제 / 운영 DB 수정 시각 동일")
    return 0


if __name__ == "__main__":
    sys.exit(main())
