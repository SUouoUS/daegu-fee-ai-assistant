"""
test_assistant_api_fallback.py
Gemini API 일시 실패(키 누락·429·408/5xx·타임아웃·연결 실패) 시 로컬 대체 답변을 검증한다.
401·403 인증·권한 오류는 대체 답변으로 숨기지 않는다.

실제 네트워크 호출은 없다. google-genai SDK·httpx·dotenv는 기존 테스트의 가짜 모듈을
쓰고, DB는 임시 디렉터리를 사용한다. 프로젝트의 data/bills.db는 건드리지 않는다.

테스트 항목:
1. 키 누락·429·408/500/502/503/504·타임아웃·연결 실패에서
   5가지 핵심 질문이 저장된 고지서로 답변된다
   - 답변 첫 줄은 "저장된 고지서 기준으로 안내합니다." 이고 실패 원인은 노출하지 않는다
   - 키 누락은 API 호출 0회, 나머지는 분류 요청 1회뿐이며
     그 요청 내용은 질문 문자열 + 정적 지침으로, DB가 비어 있을 때와 동일하다
2. 대체 답변 대상이 아닌 경우는 원래 오류를 그대로 전파한다 (예외 타입까지 구분)
   - 대상이 아닌 질문, 납부 의무·법률 판단 질문, 401, 403, 404
   - ask() 자체의 오류 전파 동작은 바뀌지 않는다
3. 앱(AppTest)에서 429·타임아웃·키 누락·503이어도 예외 없이 대체 답변이 표시되고,
   401·403은 인증 안내가 표시된다
4. 정리 후 전역 상태가 같은 프로세스에서 복원된다

[검증 범위]
가짜 SDK로 확인하는 것은 '어떤 실패에서 어떤 답이 나오는가'이며,
google-genai SDK의 실제 재시도·네트워크 동작은 검증하지 않는다.
"""

import datetime
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

# 가짜 SDK·스냅샷/복원 헬퍼는 기존 UI 테스트의 것을 재사용한다.
# (import 시점에는 아직 sys.modules를 교체하지 않는다.)
import test_assistant_panel_ui as ui  # noqa: E402

db_module = ui.db_module
privacy_test = ui.privacy_test
_CAPTURED = privacy_test._CAPTURED
_NEXT_BEHAVIOR = privacy_test._NEXT_BEHAVIOR
FakeAPIError = privacy_test.FakeAPIError

# payload에서 찾기 쉬운 긴 고유 문자열. 짧은 숫자(금액·id)는 부분 문자열 검색으로
# 부재를 단언하지 않고, 요청 내용 동일성 비교로 확인한다.
BILL_A = {
    "title": "ZZ대체답변고지서A상하수도",
    "agency": "ZZ대체답변기관A수도사업본부",
    "amount": 31417,
    "payment_method": "ZZ대체답변납부방법A은행창구",
}
BILL_B = {
    "title": "ZZ대체답변고지서B도시가스",
    "agency": "ZZ대체답변기관B가스공사",
    "amount": 52718,
    "payment_method": "ZZ대체답변납부방법B자동이체",
}

Q_WEEK = "이번 주 일정 알려줘"
Q_MONTH = "이번 달 합계 얼마야?"
Q_NEAREST = "가장 가까운 납부기한은?"
Q_LIST = "전체 목록 보여줘"
Q_SUMMARY = "이 고지서 요약해 줘"


# ── 도우미 ────────────────────────────────────────────────────


def _expect_raises(exc_type, func, *args, **kwargs):
    """정확히 exc_type(또는 하위 클래스)이 발생하는지 확인하고 예외를 반환한다."""
    try:
        func(*args, **kwargs)
    except exc_type as e:
        return e
    except Exception as e:  # noqa: BLE001 - 원인을 그대로 드러내기 위함
        raise AssertionError(
            f"{exc_type.__name__}를 기대했지만 {type(e).__name__}가 발생: {e}"
        )
    raise AssertionError(f"{exc_type.__name__}가 발생하지 않음")


def _clear_bills():
    for bill in db_module.get_bills(include_paid=True):
        db_module.delete_bill(bill["id"])


def _static_part(call: dict) -> dict:
    """요청에서 질문 문자열을 뺀 나머지(모델·지침·설정)."""
    return {
        "model": call["model"],
        "system_instruction": call["system_instruction"],
        "config_repr": call["config_repr"],
    }


class _MissingKey:
    """GEMINI_API_KEY를 잠시 제거한다."""

    def __enter__(self):
        self.saved = os.environ.pop("GEMINI_API_KEY", None)
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            os.environ["GEMINI_API_KEY"] = self.saved
        return False


# ── 테스트 ────────────────────────────────────────────────────


def test_fallback_answers(assistant, httpx_mod, baseline_static, id_a):
    """1: 키 누락·403·429·타임아웃에서 5가지 질문이 로컬 대체 답변으로 처리된다."""
    notice = assistant.FALLBACK_NOTICE
    amount_a = f"{BILL_A['amount']:,}원"

    # 질문 → (선택 id, 답변에 있어야 할 값, 없어야 할 값)
    expectations = {
        Q_WEEK: (None, [BILL_A["title"], amount_a], [BILL_B["title"]]),
        Q_MONTH: (None, ["납부 예정 금액", amount_a], [BILL_B["title"]]),
        Q_NEAREST: (None, ["가장 가까운 납부 기한", BILL_A["title"]], [BILL_B["title"]]),
        Q_LIST: (None, [BILL_A["title"], BILL_B["title"]], []),
        Q_SUMMARY: (id_a, [BILL_A["title"], f"발급 기관: {BILL_A['agency']}", amount_a],
                    [BILL_B["title"]]),
    }

    failures = [
        ("API 키 누락", None),
        ("사용량 초과(429)", ("raise", FakeAPIError(429))),
        ("요청 시간 초과(408)", ("raise", FakeAPIError(408))),
        ("서버 오류(500)", ("raise", FakeAPIError(500))),
        ("게이트웨이 오류(502)", ("raise", FakeAPIError(502))),
        ("일시적 혼잡(503)", ("raise", FakeAPIError(503))),
        ("게이트웨이 시간 초과(504)", ("raise", FakeAPIError(504))),
        ("타임아웃", ("raise", httpx_mod.TimeoutException("timeout"))),
        ("연결 실패", ("raise", httpx_mod.ConnectError("connect"))),
    ]

    for label, behavior in failures:
        for question, (selected, must_have, must_not_have) in expectations.items():
            _CAPTURED.clear()
            if behavior is None:
                with _MissingKey():
                    answer = assistant.ask_with_fallback(question, selected_bill_id=selected)
            else:
                _NEXT_BEHAVIOR[0] = behavior
                answer = assistant.ask_with_fallback(question, selected_bill_id=selected)

            assert answer.startswith(notice), (
                f"{label}/{question}: 대체 답변 안내로 시작하지 않습니다.\n답변: {answer}"
            )
            for text in must_have:
                assert text in answer, (
                    f"{label}/{question}: 답변에 {text!r}가 없습니다.\n답변: {answer}"
                )
            for text in must_not_have:
                assert text not in answer, (
                    f"{label}/{question}: 답변에 {text!r}가 섞였습니다.\n답변: {answer}"
                )
            # 실패 원인을 과도하게 드러내지 않는다.
            for leak in ("API", "사용량", "인증", "혼잡", "GEMINI_API_KEY", "오류"):
                assert leak not in answer, (
                    f"{label}/{question}: 실패 원인 문구 {leak!r}가 노출됩니다.\n답변: {answer}"
                )

            # 외부 전송 범위: 키 누락은 호출 0회, 나머지는 분류 요청 1회뿐.
            if behavior is None:
                assert len(_CAPTURED) == 0, (
                    f"{label}/{question}: 키가 없는데 API 호출이 {len(_CAPTURED)}회 발생"
                )
            else:
                assert len(_CAPTURED) == 1, (
                    f"{label}/{question}: 호출이 {len(_CAPTURED)}회입니다. "
                    "대체 답변 과정에서 추가 요청이 생기면 안 됩니다."
                )
                call = _CAPTURED[0]
                assert call["contents"] == question, (
                    f"{label}/{question}: 전송 내용이 질문 문자열과 다릅니다: {call['contents']!r}"
                )
                assert _static_part(call) == baseline_static, (
                    f"{label}/{question}: 요청의 지침·설정이 DB가 비었을 때와 다릅니다. "
                    "고지서 데이터가 요청에 섞였을 수 있습니다."
                )
                privacy_test._assert_absent(
                    [BILL_A["title"], BILL_A["agency"], BILL_A["payment_method"],
                     BILL_B["title"], BILL_B["agency"], BILL_B["payment_method"]],
                    privacy_test._payload_text(),
                    f"{label}/{question}",
                )
        print(f"PASS 1: {label} -> 5가지 핵심 질문 대체 답변 / 전송 범위 유지")


def test_non_fallback_cases_still_raise(assistant, httpx_mod, id_a):
    """2: 대체 답변 대상이 아니면 원래 오류를 그대로 전파한다."""
    from services.gemini_client import GeminiBusyError, GeminiError

    # 대상이 아닌 질문(지원하지 않는 질문·납부 의무) — 429여도 오류 전파
    for question in ("오늘 날씨 어때?", "이 고지서 꼭 내야 해?", "이거 안 내면 법적으로 문제 돼?"):
        _CAPTURED.clear()
        _NEXT_BEHAVIOR[0] = ("raise", FakeAPIError(429))
        raised = _expect_raises(
            GeminiError, assistant.ask_with_fallback, question, selected_bill_id=id_a
        )
        assert not isinstance(raised, GeminiBusyError), (
            f"{question!r}: GeminiBusyError가 아니라 GeminiError여야 합니다."
        )
        assert "사용량" in str(raised), f"{question!r}: 사용량 초과 안내가 아닙니다: {raised}"
        print(f"PASS 2: 429 + 대상 아닌 질문 {question!r} -> GeminiError 전파")

    # 503 + 대상 아닌 질문 — 혼잡 안내 그대로
    _NEXT_BEHAVIOR[0] = ("raise", FakeAPIError(503))
    raised = _expect_raises(GeminiBusyError, assistant.ask_with_fallback, "오늘 날씨 어때?")
    assert str(raised) == assistant.BUSY_MESSAGE, f"혼잡 안내가 아닙니다: {raised}"
    print("PASS 2: 503 + 대상 아닌 질문 -> GeminiBusyError(혼잡 안내) 전파")

    # 키 누락 + 대상 아닌 질문
    with _MissingKey():
        raised = _expect_raises(GeminiError, assistant.ask_with_fallback, "오늘 날씨 어때?")
    assert "GEMINI_API_KEY" in str(raised), f"키 누락 안내가 아닙니다: {raised}"
    print("PASS 2: 키 누락 + 대상 아닌 질문 -> GeminiError 전파")

    # 401·403 인증·권한 오류는 대상 질문이어도 숨기지 않는다. 404도 설정 오류라 전파.
    for code, must_contain in ((401, "인증"), (403, "인증"), (404, "모델")):
        for question in (Q_WEEK, Q_SUMMARY):
            _CAPTURED.clear()
            _NEXT_BEHAVIOR[0] = ("raise", FakeAPIError(code))
            raised = _expect_raises(
                GeminiError, assistant.ask_with_fallback, question, selected_bill_id=id_a
            )
            assert not isinstance(raised, GeminiBusyError), (
                f"{code}: GeminiBusyError가 아니라 GeminiError여야 합니다."
            )
            assert must_contain in str(raised), f"{code}: 기존 안내가 아닙니다: {raised}"
            assert assistant.FALLBACK_NOTICE not in str(raised)
            assert len(_CAPTURED) == 1, f"{code}: API 호출이 {len(_CAPTURED)}회입니다."
        print(f"PASS 2: {code} + 대상 질문 -> GeminiError 전파 (대체 답변으로 숨기지 않음)")

    # ask() 자체는 여전히 오류를 전파한다 (기존 계약)
    for label, exc, expected in (
        ("429", FakeAPIError(429), GeminiError),
        ("타임아웃", httpx_mod.TimeoutException("timeout"), GeminiBusyError),
    ):
        _NEXT_BEHAVIOR[0] = ("raise", exc)
        raised = _expect_raises(expected, assistant.ask, Q_WEEK)
        if expected is GeminiError:
            assert not isinstance(raised, GeminiBusyError), f"ask() {label}: 타입이 다릅니다."
        print(f"PASS 2: ask() {label} -> {expected.__name__} 전파 (기존 동작 유지)")

    with _MissingKey():
        _expect_raises(GeminiError, assistant.ask, Q_WEEK)
    print("PASS 2: ask() 키 누락 -> GeminiError 전파 (기존 동작 유지)")


def test_app_shows_fallback(httpx_mod, id_a):
    """3: 앱에서 실패해도 예외 없이 대체 답변이 표시된다."""
    import services.assistant as assistant

    notice = assistant.FALLBACK_NOTICE

    # 429 + 예시 버튼 '이번 주 일정'
    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("raise", FakeAPIError(429))
    at = ui._run(ui._new_app())
    ui._button(at, "ai_ex_week").click()
    ui._run(at)  # 앱 예외가 있으면 여기서 실패한다
    answer = ui._assistant_messages(at)[-1]
    assert answer.startswith(notice) and BILL_A["title"] in answer, (
        f"429에서 대체 답변이 표시되지 않았습니다.\n답변: {answer}"
    )
    assert len(_CAPTURED) == 1, f"API 호출이 {len(_CAPTURED)}회입니다."
    assert at.session_state["assistant_turn_count"] == 1
    print("PASS 3: 앱 429 + '이번 주 일정' 버튼 -> 대체 답변 표시 / 예외 없음")

    # 타임아웃 + 직접 입력 '전체 목록'
    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("raise", httpx_mod.TimeoutException("timeout"))
    at.chat_input[0].set_value(Q_LIST)
    ui._run(at)
    answer = ui._assistant_messages(at)[-1]
    assert answer.startswith(notice) and BILL_B["title"] in answer, (
        f"타임아웃에서 대체 답변이 표시되지 않았습니다.\n답변: {answer}"
    )
    print("PASS 3: 앱 타임아웃 + 직접 입력 '전체 목록' -> 대체 답변 표시 / 예외 없음")

    # 키 누락 + 고지서 선택 후 '선택 고지서 요약'
    with _MissingKey():
        _CAPTURED.clear()
        ui._button(at, f"sel_btn_{id_a}").click()
        ui._run(at)
        ui._button(at, "ai_ex_bill").click()
        ui._run(at)
        answer = ui._assistant_messages(at)[-1]
        assert answer.startswith(notice), f"키 누락에서 대체 답변이 아닙니다.\n답변: {answer}"
        assert f"발급 기관: {BILL_A['agency']}" in answer, (
            f"선택한 고지서 요약이 아닙니다.\n답변: {answer}"
        )
        assert len(_CAPTURED) == 0, "키가 없는데 API 호출이 발생했습니다."
    print("PASS 3: 앱 키 누락 + '선택 고지서 요약' 버튼 -> 대체 답변 표시 / 호출 0회")

    # 503 + 직접 입력 '이번 달 합계'
    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("raise", FakeAPIError(503))
    at.chat_input[0].set_value(Q_MONTH)
    ui._run(at)
    answer = ui._assistant_messages(at)[-1]
    assert answer.startswith(notice) and f"{BILL_A['amount']:,}원" in answer, (
        f"503에서 대체 답변이 표시되지 않았습니다.\n답변: {answer}"
    )
    assert assistant.BUSY_MESSAGE not in answer
    assert len(_CAPTURED) == 1, f"API 호출이 {len(_CAPTURED)}회입니다."
    print("PASS 3: 앱 503 + 직접 입력 '이번 달 합계' -> 대체 답변 표시 / 예외 없음")

    # 401·403 인증·권한 오류는 대화에 기존 인증 안내가 표시되고 앱은 계속 동작한다.
    for code in (401, 403):
        _NEXT_BEHAVIOR[0] = ("raise", FakeAPIError(code))
        at.chat_input[0].set_value(Q_WEEK)
        ui._run(at)
        answer = ui._assistant_messages(at)[-1]
        assert "인증" in answer and notice not in answer, (
            f"{code}은 기존 인증 안내여야 합니다.\n답변: {answer}"
        )
        print(f"PASS 3: 앱 {code} -> 기존 인증 안내 유지 / 예외 없음")
    ui._ok_response()


# ── 실행 ──────────────────────────────────────────────────────


def main():
    snapshot = ui._snapshot_state()

    tmpdir = tempfile.TemporaryDirectory()
    try:
        ui._install_fake_sdk_keep_google()
        os.environ["GEMINI_API_KEY"] = "TEST-DUMMY-KEY-NOT-REAL"
        os.environ["GEMINI_MODEL"] = "test-model"
        db_module._DB_DIR = tmpdir.name
        db_module._DB_PATH = os.path.join(tmpdir.name, "test_fallback.db")
        db_module.init_db()

        import services.assistant as assistant

        httpx_mod = sys.modules["httpx"]

        # DB가 비었을 때의 요청 지침·설정을 기준으로 잡는다.
        _clear_bills()
        _CAPTURED.clear()
        _NEXT_BEHAVIOR[0] = ("text", '{"intent": "this_week"}')
        assistant.ask(Q_WEEK)
        assert len(_CAPTURED) == 1
        baseline_static = _static_part(_CAPTURED[0])

        today = datetime.date.today().isoformat()
        id_a = db_module.save_bill(dict(BILL_A, due_date=today))
        db_module.save_bill(dict(BILL_B, due_date=None))  # 기한 없음: 일정·합계에서 제외

        test_fallback_answers(assistant, httpx_mod, baseline_static, id_a)
        test_non_fallback_cases_still_raise(assistant, httpx_mod, id_a)
        test_app_shows_fallback(httpx_mod, id_a)
        print("\n전체 통과: API 실패 시 핵심 질문은 저장된 고지서로 답하고, 전송 범위는 그대로입니다.")
    finally:
        ui._restore_state(snapshot)
        tmpdir.cleanup()

    print("\n[정리 후 상태 복원 검증 - 테스트를 실행한 같은 프로세스]")
    ui._verify_restored(snapshot)
    assert not os.path.exists(tmpdir.name), f"임시 디렉터리가 남아 있습니다: {tmpdir.name}"
    print(f"PASS 4: 임시 테스트 DB 디렉터리 삭제 확인 -> {tmpdir.name}")
    print("\n전체 통과: 전역 상태가 실행 전으로 복원되었습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
