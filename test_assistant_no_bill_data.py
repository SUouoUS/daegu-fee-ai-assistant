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
import sys
import types as pytypes
import json
import datetime
import tempfile

# 프로젝트 루트를 sys.path에 추가
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

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
