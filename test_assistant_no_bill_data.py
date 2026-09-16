"""
test_assistant_no_bill_data.py
AI 비서가 Gemini로 보내는 요청에 고지서 데이터가 섞이지 않는지 검증한다.

README의 "외부 전송 범위 — Gemini에는 사용자 질문 텍스트만 전송하며
DB 조회 결과는 요청에 추가하지 않는다"를 코드로 강제하는 테스트다.

테스트 항목:
1. Gemini 호출이 의도 분류 1회뿐이고, contents가 질문 문자열과 정확히 일치
2. 전송 payload 어디에도 고지서 sentinel 값이 없음
3. 답변에는 고지서 내용이 들어감 (= 로컬에서 조립됨)
4. 인증 실패·키 누락·404·429는 GeminiError로 전파
5. 혼잡·타임아웃은 GeminiBusyError로 전파
6. 빈 응답·잘못된 JSON·알 수 없는 intent만 규칙 기반 안내로 흡수
7. 정리 후 전역 상태가 실행 전으로 복원됨 (같은 프로세스에서 확인)

[검증 범위 — 재시도는 포함되지 않는다]
가짜 SDK의 generate_content는 호출 즉시 예외를 던지므로, 여기서 확인하는 것은
assistant/gemini_client의 '오류 분기'뿐이다. google-genai SDK가 실제로 몇 번
요청하고 얼마나 대기하는지(create_client()의 HttpRetryOptions 설정)는 이 테스트가
검증하지 않는다. 재시도 동작을 확인하려면 별도의 실제 SDK 테스트가 필요하다.

실제 네트워크 호출은 하지 않는다. google SDK와 dotenv는 모두 가짜 모듈로
대체하므로 실제 .env 파일이나 실제 API 키를 읽지 않는다.
"""

import os
import re
import sys
import types as pytypes
import json
import datetime
import tempfile

# 프로젝트 루트를 sys.path에 추가
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Windows 기본 콘솔(cp949)에는 일부 기호가 없다. 인코딩 오류로 테스트가
# 중단되지 않도록 출력 실패만 대체 문자로 넘긴다. (콘솔 인코딩은 그대로 둔다)
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except (AttributeError, OSError):  # 리다이렉트 등으로 불가능한 경우
    pass

# database.db는 dotenv나 google SDK를 import하지 않으므로 가짜 SDK 설치 전에
# 불러와도 무방하다. 상태 스냅샷을 뜨려면 모듈 객체가 먼저 있어야 한다.
import database.db as db_module  # noqa: E402

# _install_fake_sdk()가 교체하는 모듈들.
_PATCHED_MODULES = (
    "google",
    "google.genai",
    "google.genai.types",
    "google.genai.errors",
    "httpx",
    "dotenv",
)

# 테스트 도중 가짜 SDK 아래에서 새로 import되는 프로젝트 모듈들.
# 정리 때 제거하지 않으면 같은 프로세스의 이후 코드가 가짜 dotenv를 물고 있게 된다.
_PROJECT_MODULES = ("services", "services.assistant", "services.gemini_client")

# 복원 여부를 추적할 환경변수.
_TRACKED_ENV = ("GEMINI_API_KEY", "GEMINI_MODEL")

# ── 고지서 sentinel 값 ────────────────────────────────────────
# 전송 payload에서 찾기 쉬운 고유 문자열을 쓴다.
SENTINELS = {
    "title": "ZZ테스트고지서",
    "agency": "ZZ테스트기관",
    "amount": 1234567,
    "payment_method": "ZZ가상계좌9876",
}

# 캡처된 generate_content 호출 기록
_CAPTURED = []

# 다음 호출에서 가짜 SDK가 할 동작을 지정한다.
#   ("text", "...")   → 그 문자열을 response.text로 반환
#   ("raise", 예외)    → 그 예외를 던짐
_NEXT_BEHAVIOR = [("text", '{"intent": "list_all"}')]


# ── 가짜 google-genai SDK ─────────────────────────────────────


class FakeAPIError(Exception):
    """google.genai.errors.APIError 대역."""

    def __init__(self, code, message="fake api error"):
        super().__init__(message)
        self.code = code
        self.message = message


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def generate_content(self, *, model, contents, config=None):
        _CAPTURED.append(
            {
                "model": model,
                "contents": contents,
                "system_instruction": getattr(config, "system_instruction", None),
                "config_repr": repr(vars(config)) if config is not None else "",
            }
        )
        kind, payload = _NEXT_BEHAVIOR[0]
        if kind == "raise":
            raise payload
        return _FakeResponse(payload)


class _FakeClient:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.models = _FakeModels()


class _FakeConfigObject:
    """GenerateContentConfig / HttpOptions / HttpRetryOptions 공용 대역."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def _install_fake_sdk():
    """google-genai, httpx, dotenv를 가짜 모듈로 교체한다.

    원본 복원은 호출 전에 떠 둔 _snapshot_state()가 담당한다.

    Returns:
        가짜 httpx 모듈 (예외 클래스를 테스트에서 쓰기 위해).
    """
    google_mod = pytypes.ModuleType("google")
    genai_mod = pytypes.ModuleType("google.genai")
    types_mod = pytypes.ModuleType("google.genai.types")
    errors_mod = pytypes.ModuleType("google.genai.errors")
    httpx_mod = pytypes.ModuleType("httpx")
    dotenv_mod = pytypes.ModuleType("dotenv")

    genai_mod.Client = _FakeClient
    types_mod.GenerateContentConfig = _FakeConfigObject
    types_mod.HttpOptions = _FakeConfigObject
    types_mod.HttpRetryOptions = _FakeConfigObject
    errors_mod.APIError = FakeAPIError

    class _FakeTimeout(Exception):
        pass

    class _FakeConnectError(Exception):
        pass

    httpx_mod.TimeoutException = _FakeTimeout
    httpx_mod.ConnectError = _FakeConnectError

    # 실제 .env를 읽지 않도록 load_dotenv를 무력화한다.
    dotenv_mod.load_dotenv = lambda *a, **k: False

    genai_mod.types = types_mod
    genai_mod.errors = errors_mod
    google_mod.genai = genai_mod

    sys.modules["google"] = google_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = types_mod
    sys.modules["google.genai.errors"] = errors_mod
    sys.modules["httpx"] = httpx_mod
    sys.modules["dotenv"] = dotenv_mod

    return httpx_mod


# ── 전역 상태 스냅샷 / 복원 / 복원 검증 ───────────────────────


def _snapshot_state() -> dict:
    """테스트가 건드릴 전역 상태를 오염 전에 기록한다."""
    return {
        "db_path": db_module._DB_PATH,
        "db_dir": db_module._DB_DIR,
        "env": {name: os.environ.get(name) for name in _TRACKED_ENV},
        "modules": {
            name: sys.modules.get(name)
            for name in _PATCHED_MODULES + _PROJECT_MODULES
        },
    }


def _restore_state(snapshot: dict) -> None:
    """스냅샷 시점의 전역 상태로 되돌린다."""
    db_module._DB_PATH = snapshot["db_path"]
    db_module._DB_DIR = snapshot["db_dir"]

    for name, value in snapshot["env"].items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    for name, module in snapshot["modules"].items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _verify_restored(snapshot: dict) -> None:
    """정리가 끝난 뒤 '같은 프로세스'에서 복원 여부를 확인한다.

    새 Python 프로세스를 띄워 값을 출력하는 것은 복원의 증거가 되지 못한다.
    새 프로세스는 애초에 오염된 적이 없기 때문이다. 그래서 오염을 일으킨
    이 프로세스 안에서 실행 전 스냅샷과 직접 비교한다.

    Raises:
        AssertionError: 복원되지 않은 항목이 하나라도 있는 경우.
    """
    assert db_module._DB_PATH == snapshot["db_path"], (
        f"_DB_PATH가 복원되지 않았습니다.\n"
        f"기대: {snapshot['db_path']!r}\n실제: {db_module._DB_PATH!r}"
    )
    print(f"PASS 7: database.db._DB_PATH 복원 확인 → {db_module._DB_PATH}")

    assert db_module._DB_DIR == snapshot["db_dir"], (
        f"_DB_DIR가 복원되지 않았습니다.\n"
        f"기대: {snapshot['db_dir']!r}\n실제: {db_module._DB_DIR!r}"
    )
    print(f"PASS 7: database.db._DB_DIR 복원 확인 → {db_module._DB_DIR}")

    for name, value in snapshot["env"].items():
        current = os.environ.get(name)
        assert current == value, (
            f"환경변수 {name}이 복원되지 않았습니다.\n"
            f"기대: {value!r}\n실제: {current!r}"
        )
        state = "원래도 없음 → 없음" if value is None else "원래 값으로 복원"
        print(f"PASS 7: 환경변수 {name} 복원 확인 ({state})")

    for name, module in snapshot["modules"].items():
        current = sys.modules.get(name)
        if module is None:
            assert current is None, (
                f"sys.modules['{name}']이 테스트 후에도 남아 있습니다: {current!r}"
            )
            print(f"PASS 7: sys.modules['{name}'] 제거 확인 (원래 없었음)")
        else:
            # 같은 이름의 다른 객체로 바뀌었는지까지 보려면 is 비교여야 한다.
            assert current is module, (
                f"sys.modules['{name}']이 원본 객체로 복원되지 않았습니다.\n"
                f"기대: {module!r}\n실제: {current!r}"
            )
            print(f"PASS 7: sys.modules['{name}'] 원본 객체 복원 확인")


# ── 검증 도우미 ───────────────────────────────────────────────


def _payload_text() -> str:
    """캡처된 모든 전송 내용을 하나의 문자열로 직렬화한다."""
    return json.dumps(_CAPTURED, ensure_ascii=False, default=str)


def _assert_no_bill_data():
    """전송 payload에 고지서 sentinel 값이 없는지 확인한다."""
    blob = _payload_text()
    for field, value in SENTINELS.items():
        assert str(value) not in blob, (
            f"고지서 데이터가 Gemini 요청에 포함됨: {field}={value!r}\n"
            f"payload: {blob}"
        )
    assert "[DB 조회 결과]" not in blob, "DB 조회 결과 블록이 요청에 포함됨"


def _expect_raises(exc_type, func, *args):
    try:
        func(*args)
    except exc_type as e:
        return e
    except Exception as e:  # noqa: BLE001 - 원인을 그대로 드러내기 위함
        raise AssertionError(
            f"{exc_type.__name__}를 기대했지만 {type(e).__name__}가 발생: {e}"
        )
    raise AssertionError(f"{exc_type.__name__}가 발생하지 않음")


# ── 선택한 고지서 테스트용 데이터 ─────────────────────────────
# payload에서 찾기 쉬운 고유 문자열을 쓴다. 숫자 금액도 서로 다르게 둔다.
BILL_A = {
    "title": "ZZ고지서A상하수도",
    "agency": "ZZ기관A수도사업본부",
    "amount": 31417,
    "payment_method": "ZZ납부방법A은행창구",
}
BILL_B = {
    "title": "ZZ고지서B도시가스",
    "agency": "ZZ기관B가스공사",
    "amount": 52718,
    "payment_method": "ZZ납부방법B자동이체",
}


def _clear_bills():
    """임시 DB의 고지서를 모두 지운다. (테스트 간 격리)"""
    for bill in db_module.get_bills(include_paid=True):
        db_module.delete_bill(bill["id"])


def _save(bill: dict, *, days: int | None = 5) -> int:
    """테스트용 고지서를 저장하고 id를 반환한다. days=None이면 기한 없음."""
    info = dict(bill)
    if days is None:
        info["due_date"] = None
    else:
        info["due_date"] = (
            datetime.date.today() + datetime.timedelta(days=days)
        ).isoformat()
    return db_module.save_bill(info)


def _request_signature(call: dict) -> dict:
    """캡처된 호출에서 '실제로 전송된 내용'만 뽑는다."""
    return {
        "model": call["model"],
        "contents": call["contents"],
        "system_instruction": call["system_instruction"],
        "config_repr": call["config_repr"],
    }


def _assert_absent(values, blob: str, label: str):
    """payload에 특정 문자열들이 없는지 확인한다.

    id처럼 짧은 숫자는 정적 지침·모델명과 우연히 겹칠 수 있으므로 여기에 넣지 않는다.
    id가 전송되지 않는다는 것은 A/B 요청 내용 동일성으로 확인한다.
    """
    for value in values:
        assert str(value) not in blob, (
            f"{label}: 고지서 데이터가 Gemini 요청에 포함됨 → {value!r}\npayload: {blob}"
        )


# ── 테스트 ────────────────────────────────────────────────────


def test_question_only_is_sent(assistant, save_bill, question):
    """1~3: 질문만 전송되고, 답변은 로컬에서 조립된다."""
    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "list_all"}')

    today = datetime.date.today()
    save_bill(
        {
            "title": SENTINELS["title"],
            "agency": SENTINELS["agency"],
            "amount": SENTINELS["amount"],
            "due_date": (today + datetime.timedelta(days=2)).isoformat(),
            "payment_method": SENTINELS["payment_method"],
        }
    )

    answer = assistant.ask(question)

    assert len(_CAPTURED) == 1, (
        f"Gemini 호출은 의도 분류 1회여야 하는데 {len(_CAPTURED)}회 발생했습니다. "
        "답변 생성 단계에서 추가 호출이 일어나고 있습니다."
    )
    call = _CAPTURED[0]
    assert call["contents"] == question, (
        f"contents가 질문 문자열과 다릅니다.\n기대: {question!r}\n실제: {call['contents']!r}"
    )
    assert isinstance(call["contents"], str), "contents는 문자열 하나여야 합니다."

    _assert_no_bill_data()

    assert SENTINELS["title"] in answer, (
        f"답변에 고지서 제목이 없습니다. 로컬 조립이 동작하지 않았습니다.\n답변: {answer}"
    )
    assert f"{SENTINELS['amount']:,}" in answer, "답변에 금액이 포맷되지 않았습니다."
    print("PASS 1-3: 질문만 전송 / payload에 고지서 데이터 없음 / 답변은 로컬 조립")


def test_api_errors_propagate(assistant, httpx_mod, question):
    """4~5: 인증·404·429는 GeminiError, 혼잡·타임아웃은 GeminiBusyError."""
    from services.gemini_client import GeminiError, GeminiBusyError

    error_cases = [
        ("인증 실패(401)", FakeAPIError(401), GeminiError),
        ("권한 없음(403)", FakeAPIError(403), GeminiError),
        ("모델 없음(404)", FakeAPIError(404), GeminiError),
        ("사용량 초과(429)", FakeAPIError(429), GeminiError),
    ]
    for label, exc, expected in error_cases:
        _CAPTURED.clear()
        _NEXT_BEHAVIOR[0] = ("raise", exc)
        raised = _expect_raises(expected, assistant.ask, question)
        # GeminiBusyError는 GeminiError의 하위 클래스이므로 구분해 둔다.
        assert not isinstance(raised, GeminiBusyError), (
            f"{label}은 GeminiBusyError가 아니라 GeminiError여야 합니다."
        )
        _assert_no_bill_data()
        print(f"PASS 4: {label} → GeminiError 전파 ({raised})")

    busy_cases = [
        ("일시적 혼잡(503)", FakeAPIError(503)),
        ("타임아웃", httpx_mod.TimeoutException("timeout")),
        ("연결 실패", httpx_mod.ConnectError("connect")),
    ]
    for label, exc in busy_cases:
        _CAPTURED.clear()
        _NEXT_BEHAVIOR[0] = ("raise", exc)
        _expect_raises(GeminiBusyError, assistant.ask, question)
        _assert_no_bill_data()
        print(f"PASS 5: {label} → GeminiBusyError 전파")


def test_missing_api_key_raises(assistant, question):
    """4: 키 누락도 숨기지 않고 GeminiError로 알린다."""
    from services.gemini_client import GeminiError

    _CAPTURED.clear()
    saved = os.environ.pop("GEMINI_API_KEY", None)
    try:
        raised = _expect_raises(GeminiError, assistant.ask, question)
        assert "GEMINI_API_KEY" in str(raised), f"키 누락 안내가 아닙니다: {raised}"
        assert len(_CAPTURED) == 0, "키가 없는데 API 호출이 발생했습니다."
        print(f"PASS 4: 키 누락 → GeminiError 전파 ({raised})")
    finally:
        if saved is not None:
            os.environ["GEMINI_API_KEY"] = saved


def test_bad_response_falls_back(assistant, question):
    """6: 빈 응답·잘못된 JSON·알 수 없는 intent만 규칙 기반 안내."""
    cases = [
        ("빈 응답", ""),
        ("잘못된 JSON", "not json at all"),
        ("알 수 없는 intent", '{"intent": "무엇이든_아닌_값"}'),
    ]
    for label, text in cases:
        _CAPTURED.clear()
        _NEXT_BEHAVIOR[0] = ("text", text)
        answer = assistant.ask(question)
        assert "고지서 관련 질문만" in answer, (
            f"{label}: 규칙 기반 안내가 아닙니다.\n답변: {answer}"
        )
        _assert_no_bill_data()
        print(f"PASS 6: {label} → 규칙 기반 안내")


# ── 선택한 고지서 관련 테스트 ─────────────────────────────────


def test_selected_bill_id_not_sent(assistant):
    """8: 선택 ID를 A→B로 바꿔도 전송 내용은 동일하고, 답변만 달라진다.

    id 문자열을 payload에서 단순 검색하면 id가 1·2일 때 모델명이나 정적 지침의
    숫자에 걸려 잘못 실패한다. 그래서 '요청 내용이 완전히 같은지'로 확인한다.
    """
    _clear_bills()
    id_a = _save(BILL_A, days=5)
    id_b = _save(BILL_B, days=9)

    question = "이 고지서 요약해 줘"

    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "bill_summary"}')
    answer_a = assistant.ask(question, selected_bill_id=id_a)
    assert len(_CAPTURED) == 1, f"분류 1회여야 하는데 {len(_CAPTURED)}회 호출됨"
    sig_a = _request_signature(_CAPTURED[0])
    blob_a = _payload_text()

    _CAPTURED.clear()
    answer_b = assistant.ask(question, selected_bill_id=id_b)
    assert len(_CAPTURED) == 1, f"분류 1회여야 하는데 {len(_CAPTURED)}회 호출됨"
    sig_b = _request_signature(_CAPTURED[0])
    blob_b = _payload_text()

    assert sig_a["contents"] == question, (
        f"contents가 질문과 다릅니다.\n기대: {question!r}\n실제: {sig_a['contents']!r}"
    )
    assert sig_a == sig_b, (
        "선택한 고지서가 달라졌는데 요청 내용이 달라졌습니다. "
        f"선택 정보가 전송되고 있습니다.\nA: {sig_a}\nB: {sig_b}"
    )

    all_values = list(BILL_A.values()) + list(BILL_B.values())
    _assert_absent(all_values, blob_a, "A 선택")
    _assert_absent(all_values, blob_b, "B 선택")

    assert BILL_A["title"] in answer_a and BILL_B["title"] not in answer_a, (
        f"A를 선택했는데 답변이 A의 정보가 아닙니다.\n답변: {answer_a}"
    )
    assert BILL_B["title"] in answer_b and BILL_A["title"] not in answer_b, (
        f"B를 선택했는데 답변이 B의 정보가 아닙니다.\n답변: {answer_b}"
    )
    print("PASS 8: 선택 ID를 바꿔도 요청 내용 동일 / 답변만 A·B로 달라짐")


def test_bill_intents_answer_from_db(assistant):
    """9: bill_* 의도가 각각 선택한 고지서의 저장값으로 답한다."""
    _clear_bills()
    id_a = _save(BILL_A, days=5)
    id_b = _save(BILL_B, days=9)

    checks = [
        ("bill_amount", "금액이 얼마야?", lambda b: f"{b['amount']:,}원"),
        ("bill_payment", "납부 방법은?", lambda b: b["payment_method"]),
        ("bill_summary", "이 고지서 요약해 줘", lambda b: b["agency"]),
    ]
    for intent, question, expected_of in checks:
        for bill_id, mine, other in ((id_a, BILL_A, BILL_B), (id_b, BILL_B, BILL_A)):
            _CAPTURED.clear()
            _NEXT_BEHAVIOR[0] = ("text", json.dumps({"intent": intent}))
            answer = assistant.ask(question, selected_bill_id=bill_id)
            assert len(_CAPTURED) == 1, f"{intent}: SDK 호출이 1회가 아님"
            assert expected_of(mine) in answer, (
                f"{intent}: 선택한 고지서의 값이 답변에 없습니다.\n답변: {answer}"
            )
            assert other["title"] not in answer, (
                f"{intent}: 선택하지 않은 고지서가 답변에 섞였습니다.\n답변: {answer}"
            )

    # bill_due는 저장된 기한과 남은 일수를 함께 안내한다.
    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "bill_due"}')
    answer = assistant.ask("이거 언제까지 내야 해?", selected_bill_id=id_a)
    due_a = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
    assert due_a in answer and "5일 남음" in answer, (
        f"bill_due 답변에 기한·남은 일수가 없습니다.\n답변: {answer}"
    )
    print("PASS 9: bill_summary/due/amount/payment가 각 고지서의 저장값으로 답변")


def test_amount_zero_and_none(assistant):
    """10: amount=0은 '0원', amount=None은 '금액 미확인'."""
    _clear_bills()
    id_zero = _save({**BILL_A, "amount": 0}, days=3)
    id_none = _save({**BILL_B, "amount": None}, days=3)

    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "bill_amount"}')

    _CAPTURED.clear()
    answer_zero = assistant.ask("금액이 얼마야?", selected_bill_id=id_zero)
    assert "0원" in answer_zero and "금액 미확인" not in answer_zero, (
        f"amount=0은 '0원'이어야 합니다.\n답변: {answer_zero}"
    )

    _CAPTURED.clear()
    answer_none = assistant.ask("금액이 얼마야?", selected_bill_id=id_none)
    assert "금액 미확인" in answer_none and "0원" not in answer_none, (
        f"amount=None은 '금액 미확인'이어야 합니다.\n답변: {answer_none}"
    )
    print("PASS 10: amount 0 → '0원' / None → '금액 미확인'")


def test_missing_fields_guidance(assistant):
    """11: 기한·납부방법·기관이 없을 때의 안내."""
    _clear_bills()
    # 납부방법 없음 + 기관 있음
    id_no_method = _save({**BILL_A, "payment_method": None}, days=4)
    # 납부방법·기관 모두 없음 + 기한도 없음
    id_bare = _save(
        {**BILL_B, "payment_method": None, "agency": None}, days=None
    )

    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "bill_payment"}')
    answer = assistant.ask("납부 방법은?", selected_bill_id=id_no_method)
    assert f"발급 기관({BILL_A['agency']})에 확인" in answer, (
        f"기관이 있으면 기관 확인을 안내해야 합니다.\n답변: {answer}"
    )

    _CAPTURED.clear()
    answer = assistant.ask("납부 방법은?", selected_bill_id=id_bare)
    assert "고지서 원문을 확인" in answer, (
        f"기관도 없으면 원문 확인을 안내해야 합니다.\n답변: {answer}"
    )

    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "bill_due"}')
    answer = assistant.ask("이거 언제까지 내야 해?", selected_bill_id=id_bare)
    assert "확인되지 않음" in answer, (
        f"기한이 없으면 확인되지 않았다고 안내해야 합니다.\n답변: {answer}"
    )

    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "bill_summary"}')
    answer = assistant.ask("이 고지서 요약해 줘", selected_bill_id=id_bare)
    assert answer.count("확인되지 않음") >= 2, (
        f"요약에서 없는 항목을 확인되지 않음으로 표시해야 합니다.\n답변: {answer}"
    )
    print("PASS 11: 기한·납부방법·기관 누락 시 안내 문구 확인")


def test_never_invents_account_or_link(assistant):
    """12: 저장되지 않은 계좌번호·링크를 만들어내지 않는다."""
    _clear_bills()
    bill_id = _save({**BILL_A, "payment_method": None, "agency": None}, days=None)

    account_re = re.compile(r"\d{2,}-\d{2,}-\d{2,}")
    for intent in ("bill_summary", "bill_due", "bill_amount", "bill_payment",
                   "bill_obligation"):
        _CAPTURED.clear()
        _NEXT_BEHAVIOR[0] = ("text", json.dumps({"intent": intent}))
        answer = assistant.ask("납부 방법 알려줘", selected_bill_id=bill_id)
        assert "http" not in answer, f"{intent}: 링크가 생성됨\n답변: {answer}"
        assert account_re.search(answer) is None, (
            f"{intent}: 계좌번호 형태의 문자열이 생성됨\n답변: {answer}"
        )
    print("PASS 12: 계좌번호·납부 링크를 생성하지 않음")


def test_obligation_questions(assistant):
    """13: 납부 의무 질문은 분류가 빗나가도 고정 안내로 처리된다."""
    _clear_bills()
    bill_id = _save(BILL_A, days=6)
    due = (datetime.date.today() + datetime.timedelta(days=6)).isoformat()

    # 분류기가 bill_due 또는 unknown으로 잘못 보내도 고정 안내가 나와야 한다.
    cases = [
        ("이 고지서 꼭 내야 해?", "bill_due"),
        ("안 내도 돼?", "unknown"),
        ("이거 안 내도 되나요?", "bill_due"),
        ("이 고지서 꼭 내야 해?", "bill_obligation"),
    ]
    for question, fake_intent in cases:
        _CAPTURED.clear()
        _NEXT_BEHAVIOR[0] = ("text", json.dumps({"intent": fake_intent}))
        answer = assistant.ask(question, selected_bill_id=bill_id)
        assert assistant.OBLIGATION_ANSWER in answer, (
            f"{question!r}({fake_intent}): 납부 의무 안내가 없습니다.\n답변: {answer}"
        )
        assert due not in answer, (
            f"{question!r}: 의무 질문에 기한을 답하고 있습니다.\n답변: {answer}"
        )
        print(f"PASS 13: {question!r} (분류 {fake_intent}) → 납부 의무 고정 안내")


def test_obligation_override_boundaries(assistant):
    """14: 기한 질문과 일정 질문은 의무 안내로 바뀌지 않는다."""
    _clear_bills()
    bill_id = _save(BILL_A, days=6)
    due = (datetime.date.today() + datetime.timedelta(days=6)).isoformat()

    # (1) 시점을 묻는 질문은 그대로 기한 답변
    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "bill_due"}')
    answer = assistant.ask("이 고지서 언제까지 내야 해?", selected_bill_id=bill_id)
    assert due in answer, f"기한 답변이 아닙니다.\n답변: {answer}"
    assert assistant.OBLIGATION_ANSWER not in answer, (
        f"기한 질문이 의무 안내로 과잉 교정되었습니다.\n답변: {answer}"
    )

    # (2) 기간을 묻는 일정 질문은 기존 목록 답변 유지
    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "this_month"}')
    answer = assistant.ask("이번 달에 꼭 내야 하는 거 있어?")
    assert assistant.OBLIGATION_ANSWER not in answer, (
        f"일정 질문이 의무 안내로 바뀌었습니다.\n답변: {answer}"
    )
    assert BILL_A["title"] in answer, (
        f"이번 달 목록 답변이 아닙니다.\n답변: {answer}"
    )
    print("PASS 14: '언제까지' 기한 질문과 '이번 달' 일정 질문은 교정되지 않음")


def test_paid_bill_still_answers_detail(assistant):
    """15: 납부완료 고지서도 상세 답변이 되고, 집계에서는 계속 제외된다."""
    _clear_bills()
    paid_id = _save(BILL_A, days=2)
    unpaid_id = _save(BILL_B, days=3)
    assert db_module.mark_as_paid(paid_id) is True

    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "bill_summary"}')
    answer = assistant.ask("이 고지서 요약해 줘", selected_bill_id=paid_id)
    assert BILL_A["title"] in answer, f"납부완료 고지서 상세가 안 나옵니다.\n{answer}"
    assert f"{BILL_A['amount']:,}원" in answer, f"금액이 없습니다.\n{answer}"
    assert "납부완료" in answer, f"관리 상태 표시가 없습니다.\n{answer}"

    # 기존 집계 기준(미납만)은 그대로여야 한다.
    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "this_month"}')
    answer = assistant.ask("이번 달에 낼 거 있어?")
    assert BILL_A["title"] not in answer, (
        f"납부완료 고지서가 이번 달 집계에 포함되었습니다.\n답변: {answer}"
    )
    assert BILL_B["title"] in answer, (
        f"미납 고지서가 집계에서 빠졌습니다.\n답변: {answer}"
    )
    assert db_module.mark_as_unpaid(unpaid_id) is True  # 상태 확인용 호출
    print("PASS 15: 납부완료 상세 답변 가능 / 이번 달 집계는 미납 기준 유지")


def test_no_selection_asks_to_select(assistant):
    """16: 선택이 없으면 임의의 고지서로 답하지 않고 선택을 안내한다."""
    _clear_bills()
    _save(BILL_A, days=5)
    _save(BILL_B, days=9)

    for intent in ("bill_summary", "bill_due", "bill_amount", "bill_payment"):
        _CAPTURED.clear()
        _NEXT_BEHAVIOR[0] = ("text", json.dumps({"intent": intent}))
        answer = assistant.ask("납부 방법은?")
        assert "선택" in answer, f"{intent}: 선택 안내가 아닙니다.\n답변: {answer}"
        assert BILL_A["title"] not in answer and BILL_B["title"] not in answer, (
            f"{intent}: 선택하지 않았는데 특정 고지서를 답했습니다.\n답변: {answer}"
        )

    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "bill_obligation"}')
    answer = assistant.ask("꼭 내야 해?")
    assert assistant.OBLIGATION_ANSWER in answer, (
        f"선택이 없어도 의무 안내는 나와야 합니다.\n답변: {answer}"
    )
    assert BILL_A["title"] not in answer and BILL_B["title"] not in answer
    print("PASS 16: 선택 없음 → 선택 안내 (임의 고지서로 답하지 않음)")


def test_deleted_selection_not_found(assistant):
    """17: 삭제된 고지서 id로 물으면 찾을 수 없다고 안내한다."""
    _clear_bills()
    bill_id = _save(BILL_A, days=5)
    _save(BILL_B, days=9)
    assert db_module.delete_bill(bill_id) is True

    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "bill_summary"}')
    answer = assistant.ask("이 고지서 요약해 줘", selected_bill_id=bill_id)
    assert "찾을 수 없습니다" in answer, (
        f"삭제된 고지서 안내가 아닙니다.\n답변: {answer}"
    )
    assert BILL_B["title"] not in answer, (
        f"삭제된 고지서 대신 다른 고지서를 답했습니다.\n답변: {answer}"
    )
    print("PASS 17: 삭제된 선택 → '찾을 수 없습니다' 안내")


def test_get_bill_by_id():
    """18: 단건 조회는 읽기 전용이며 납부완료도 조회된다."""
    _clear_bills()
    bill_id = _save(BILL_A, days=5)

    before = db_module.get_bills(include_paid=True)
    row = db_module.get_bill_by_id(bill_id)
    assert row is not None and row["title"] == BILL_A["title"]

    db_module.mark_as_paid(bill_id)
    paid_row = db_module.get_bill_by_id(bill_id)
    assert paid_row is not None and paid_row["status"] == "납부완료", (
        f"납부완료 고지서가 조회되지 않습니다: {paid_row}"
    )
    db_module.mark_as_unpaid(bill_id)

    assert db_module.get_bill_by_id(999999) is None
    assert db_module.get_bill_by_id(None) is None
    assert db_module.get_bill_by_id("abc") is None

    after = db_module.get_bills(include_paid=True)
    assert before == after, "조회 함수가 데이터를 변경했습니다."
    print("PASS 18: get_bill_by_id - 납부완료 포함 조회 / 잘못된 id는 None / 읽기 전용")


# ── 실행 ──────────────────────────────────────────────────────


def main():
    # 어떤 것도 바꾸기 전에 스냅샷을 뜬다.
    snapshot = _snapshot_state()

    # 테스트 DB는 프로젝트의 data/ 가 아니라 임시 디렉터리에 만든다.
    # 실제 data/bills.db는 건드리지 않으며, 잔여 파일도 남기지 않는다.
    tmpdir = tempfile.TemporaryDirectory()
    try:
        httpx_mod = _install_fake_sdk()
        os.environ["GEMINI_API_KEY"] = "TEST-DUMMY-KEY-NOT-REAL"
        os.environ["GEMINI_MODEL"] = "test-model"
        db_module._DB_DIR = tmpdir.name
        db_module._DB_PATH = os.path.join(tmpdir.name, "test_assistant.db")
        db_module.init_db()

        import services.assistant as assistant

        question = "이번 달에 낼 거 있어?"
        test_question_only_is_sent(assistant, db_module.save_bill, question)
        test_api_errors_propagate(assistant, httpx_mod, question)
        test_missing_api_key_raises(assistant, question)
        test_bad_response_falls_back(assistant, question)

        print("\n[선택한 고지서 관련]")
        test_selected_bill_id_not_sent(assistant)
        test_bill_intents_answer_from_db(assistant)
        test_amount_zero_and_none(assistant)
        test_missing_fields_guidance(assistant)
        test_never_invents_account_or_link(assistant)
        test_obligation_questions(assistant)
        test_obligation_override_boundaries(assistant)
        test_paid_bill_still_answers_detail(assistant)
        test_no_selection_asks_to_select(assistant)
        test_deleted_selection_not_found(assistant)
        test_get_bill_by_id()
        print("\n전체 통과: Gemini 요청에 고지서 데이터가 포함되지 않습니다.")
    finally:
        _restore_state(snapshot)
        tmpdir.cleanup()

    # 정리가 끝난 뒤, 오염을 일으킨 바로 이 프로세스에서 복원을 확인한다.
    print("\n[정리 후 상태 복원 검증 — 테스트를 실행한 같은 프로세스]")
    _verify_restored(snapshot)
    assert not os.path.exists(tmpdir.name), (
        f"임시 테스트 디렉터리가 남아 있습니다: {tmpdir.name}"
    )
    print(f"PASS 7: 임시 테스트 DB 디렉터리 삭제 확인 → {tmpdir.name}")
    print("\n전체 통과: 전역 상태가 실행 전으로 복원되었습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
