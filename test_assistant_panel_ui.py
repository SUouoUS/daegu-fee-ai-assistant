"""
test_assistant_panel_ui.py
대시보드에 상주하는 AI 비서 패널의 UI 동작을 검증한다.

Streamlit 내장 테스트 러너(AppTest)로 app.py를 실제로 실행하되,
google-genai SDK는 test_assistant_no_bill_data.py의 가짜 모듈로 대체하므로
실제 네트워크 호출은 일어나지 않는다. DB도 임시 디렉터리를 사용하며
프로젝트의 data/bills.db는 건드리지 않는다.

테스트 항목:
1. 최초 렌더에서는 API를 호출하지 않는다
2. 진입점이 하나뿐이다 (플로팅 버튼·다이얼로그 제거)
3. 단순 재실행으로는 호출되지 않는다
4. 고지서 선택 변경만으로는 호출되지 않는다
5. 입력창 전송 시 정확히 1회 호출되고, 이후 재실행으로 중복 처리되지 않는다
6. 예시 버튼 클릭 시 정확히 1회 호출된다
7. '선택한 고지서 요약' 버튼은 선택했을 때만 활성화된다
8. 선택한 고지서 데이터가 요청에 포함되지 않는다 (답변에는 포함된다)
9. 등록 화면을 다녀와도 대화가 유지된다
10. 503 혼잡은 저장된 고지서로 대체 답변하고, 401·403 인증 오류 안내는 그대로 보인다
11. 렌더된 마크다운에 고아 HTML 태그가 없다
12. 선택한 고지서가 삭제되면 선택 상태가 정리된다
13. 정리 후 전역 상태가 같은 프로세스에서 복원된다
14. 고지서 0건에서도 질문 UI를 쓸 수 있고, 첫 등록 안내만 표시된다
15. 0건 안내의 '고지서 등록' 버튼은 등록 화면으로 이동한다 (API 호출 없음)
16. 완료 고지서만 있으면 미납 없음 안내와 완료 건 보기 안내를 표시한다
17. 대화 길이에 따라 기록 영역 높이가 content → 340px → 420px로 정해진다

[검증 범위]
가짜 SDK를 쓰므로 여기서 확인하는 것은 'UI가 언제 몇 번 호출하는가'이며,
google-genai SDK의 실제 재시도·네트워크 동작은 검증하지 않는다.
"""

import os
import sys
import datetime
import tempfile

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except (AttributeError, OSError):
    pass

import types as pytypes  # noqa: E402

import database.db as db_module  # noqa: E402

# Streamlit은 google.protobuf를 쓴다. 가짜 모듈을 설치하기 전에 먼저 불러와
# 실제 google 네임스페이스 패키지가 sys.modules에 자리 잡게 한다.
import google  # noqa: E402
import google.protobuf  # noqa: E402,F401
from streamlit.testing.v1 import AppTest  # noqa: E402

# 가짜 SDK 골격은 중복 작성하지 않고 기존 테스트에서 가져온다.
# (이 import 시점에는 아직 sys.modules를 교체하지 않는다.)
import test_assistant_no_bill_data as privacy_test  # noqa: E402

_CAPTURED = privacy_test._CAPTURED
_NEXT_BEHAVIOR = privacy_test._NEXT_BEHAVIOR
FakeAPIError = privacy_test.FakeAPIError

# 'google' 자체는 교체하지 않는다. 교체하면 google.protobuf를 찾지 못해
# Streamlit이 import되지 않는다. 하위 모듈만 가짜로 바꾼다.
_PATCHED_MODULES = (
    "google.genai",
    "google.genai.types",
    "google.genai.errors",
    "httpx",
    "dotenv",
)


def _install_fake_sdk_keep_google():
    """google.genai / httpx / dotenv만 가짜로 교체한다.

    privacy_test._install_fake_sdk()는 'google' 네임스페이스 전체를 가짜 모듈로
    바꾸는데, 그러면 Streamlit이 필요로 하는 google.protobuf가 사라진다.
    여기서는 실제 google 패키지를 그대로 두고 하위 모듈만 바꾼다.
    """
    genai_mod = pytypes.ModuleType("google.genai")
    types_mod = pytypes.ModuleType("google.genai.types")
    errors_mod = pytypes.ModuleType("google.genai.errors")
    httpx_mod = pytypes.ModuleType("httpx")
    dotenv_mod = pytypes.ModuleType("dotenv")

    genai_mod.Client = privacy_test._FakeClient
    types_mod.GenerateContentConfig = privacy_test._FakeConfigObject
    types_mod.HttpOptions = privacy_test._FakeConfigObject
    types_mod.HttpRetryOptions = privacy_test._FakeConfigObject
    errors_mod.APIError = privacy_test.FakeAPIError

    class _FakeTimeout(Exception):
        pass

    class _FakeConnectError(Exception):
        pass

    httpx_mod.TimeoutException = _FakeTimeout
    httpx_mod.ConnectError = _FakeConnectError
    dotenv_mod.load_dotenv = lambda *a, **k: False  # 실제 .env를 읽지 않는다

    genai_mod.types = types_mod
    genai_mod.errors = errors_mod

    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = types_mod
    sys.modules["google.genai.errors"] = errors_mod
    sys.modules["httpx"] = httpx_mod
    sys.modules["dotenv"] = dotenv_mod

    # 'from google import genai'가 곧바로 가짜를 집도록 속성도 심는다.
    google.genai = genai_mod

# app.py가 끌어오는 프로젝트 모듈. 정리 때 제거하지 않으면 같은 프로세스의
# 이후 코드가 가짜 dotenv를 물고 있게 된다.
_PROJECT_MODULES = (
    "services",
    "services.assistant",
    "services.gemini_client",
    "services.parser",
    "services.extractor",
)

_TRACKED_ENV = privacy_test._TRACKED_ENV

# 화면에서 확인할 문구
PANEL_TITLE = "고지서 AI 비서"        # 페이지 상단 서비스명
PANEL_HEADING = "납부 일정 물어보기"   # 비서 패널 제목
PANEL_CAPTION = "AI 비서"             # 패널 제목 옆 역할 표시
# 패널의 상태 안내 (한 상태에 한 문구만 보인다)
SELECT_HINT = "전체 납부 일정을 물어보거나 고지서를 선택해 주세요."  # 고지서 있음·미선택
NO_BILL_HINT = "고지서를 등록하면 일정과 금액을 확인할 수 있습니다."   # 저장된 고지서 0건

BILL_A = privacy_test.BILL_A
BILL_B = privacy_test.BILL_B


# ── 상태 스냅샷 / 복원 / 복원 검증 ────────────────────────────


_MISSING = object()


def _snapshot_state() -> dict:
    return {
        "db_path": db_module._DB_PATH,
        "db_dir": db_module._DB_DIR,
        "env": {name: os.environ.get(name) for name in _TRACKED_ENV},
        "modules": {
            name: sys.modules.get(name)
            for name in _PATCHED_MODULES + _PROJECT_MODULES
        },
        "google_genai_attr": getattr(google, "genai", _MISSING),
    }


def _restore_state(snapshot: dict) -> None:
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

    if snapshot["google_genai_attr"] is _MISSING:
        if hasattr(google, "genai"):
            delattr(google, "genai")
    else:
        google.genai = snapshot["google_genai_attr"]


def _verify_restored(snapshot: dict) -> None:
    """정리 후 복원 여부를 '오염을 일으킨 같은 프로세스'에서 확인한다."""
    assert db_module._DB_PATH == snapshot["db_path"], "_DB_PATH가 복원되지 않았습니다."
    assert db_module._DB_DIR == snapshot["db_dir"], "_DB_DIR가 복원되지 않았습니다."
    print(f"PASS 13: database.db 경로 복원 확인 -> {db_module._DB_PATH}")

    for name, value in snapshot["env"].items():
        current = os.environ.get(name)
        assert current == value, f"환경변수 {name} 복원 실패 (기대 {value!r}, 실제 {current!r})"
    print("PASS 13: 환경변수 복원 확인")

    for name, module in snapshot["modules"].items():
        current = sys.modules.get(name)
        if module is None:
            assert current is None, f"sys.modules['{name}']이 남아 있습니다: {current!r}"
        else:
            assert current is module, f"sys.modules['{name}']이 원본으로 복원되지 않았습니다."
    print("PASS 13: sys.modules 복원 확인")

    current_attr = getattr(google, "genai", _MISSING)
    assert current_attr is snapshot["google_genai_attr"], (
        "google.genai 속성이 복원되지 않았습니다."
    )
    print("PASS 13: google.genai 속성 복원 확인")


# ── 도우미 ────────────────────────────────────────────────────


def _new_app():
    """app.py를 실행할 AppTest 인스턴스를 만든다.

    pymupdf import 때문에 첫 실행이 느릴 수 있어 timeout을 넉넉히 둔다.
    """
    return AppTest.from_file(os.path.join(_project_root, "app.py"), default_timeout=60)


def _run(at):
    at.run()
    assert not at.exception, f"앱 실행 중 예외 발생: {at.exception}"
    return at


def _ok_response(intent: str = "list_all"):
    _NEXT_BEHAVIOR[0] = ("text", '{"intent": "%s"}' % intent)


def _button(at, key):
    """key로 버튼을 찾는다. 없으면 KeyError."""
    return at.button(key=key)


def _markdown_values(at) -> list[str]:
    return [el.value for el in at.markdown] + [el.value for el in at.caption]


def _assistant_messages(at) -> list[str]:
    return [
        m["content"]
        for m in at.session_state["chat_messages"]
        if m["role"] == "assistant"
    ]


def _save(bill: dict, days: int = 5) -> int:
    info = dict(bill)
    info["due_date"] = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    return db_module.save_bill(info)


# ── 테스트 ────────────────────────────────────────────────────


def test_first_render_makes_no_call():
    """1: 화면을 처음 그릴 때는 API를 호출하지 않는다."""
    _CAPTURED.clear()
    _ok_response()
    at = _run(_new_app())

    assert len(_CAPTURED) == 0, (
        f"최초 렌더에서 API가 {len(_CAPTURED)}회 호출되었습니다."
    )
    assert at.session_state["assistant_turn_count"] == 0

    texts = _markdown_values(at)
    assert any(PANEL_TITLE in t for t in texts), "서비스명이 없습니다."
    assert any(PANEL_HEADING in t for t in texts), "패널 제목이 없습니다."
    assert any(PANEL_CAPTION in t for t in texts), "패널의 AI 비서 표시가 없습니다."
    # 고지서가 있고 선택은 없는 상태의 안내가 한 번만 보인다.
    assert any(SELECT_HINT in t for t in texts), "질문 대상 안내가 없습니다."
    assert sum(t.count(SELECT_HINT) for t in texts) == 1, (
        "고지서 선택 안내가 패널 안에 중복 표시됩니다."
    )
    assert not any(NO_BILL_HINT in t for t in texts), (
        "고지서가 있는데 0건 안내가 표시됩니다."
    )
    print("PASS 1: 최초 렌더 API 호출 0회 / 제목·설명·질문 대상 안내 표시")
    return at


def _find_block(node, key):
    """블록 트리에서 key로 컨테이너를 찾는다."""
    if getattr(node, "key", None) == key:
        return node
    for child in getattr(node, "children", {}).values():
        found = _find_block(child, key)
        if found is not None:
            return found
    return None


def test_dashboard_two_column_structure(at):
    """1-1: 목록과 비서가 '같은 행'의 2열로 배치된다.

    CSS로 위치를 옮기는 것이 아니라 st.columns 구조 자체가 2열이어야 한다.
    - 두 열이 같은 가로 블록(stHorizontalBlock)의 자식이어야 한다
    - 비율은 왼쪽 0.7 / 오른쪽 0.3
    - wrap=False여야 왼쪽 내용 폭 때문에 오른쪽 열이 아래 줄로 밀리지 않는다
    """
    split = _find_block(at._tree, "dash_split")
    assert split is not None, "dash_split 컨테이너를 찾지 못했습니다."

    hblocks = [
        child
        for child in split.children.values()
        if getattr(child.proto, "DESCRIPTOR", None) is not None
        and child.proto.DESCRIPTOR.name == "Block"
        and child.proto.HasField("flex_container")
        and child.proto.flex_container.direction
        == child.proto.flex_container.Direction.HORIZONTAL
    ]
    assert len(hblocks) == 1, (
        f"dash_split 안에 가로 블록이 {len(hblocks)}개입니다. 2열 구조가 아닙니다."
    )
    hblock = hblocks[0]

    assert hblock.proto.flex_container.wrap is False, (
        "st.columns(wrap=False)가 아닙니다. wrap=True면 왼쪽 열의 내용 폭 때문에 "
        "오른쪽 비서 열이 다음 줄로 밀려 세로로 배치됩니다."
    )

    columns = list(hblock.children.values())
    assert len(columns) == 2, f"열이 {len(columns)}개입니다. 2열이어야 합니다."

    inner_keys = [list(col.children.values())[0].key for col in columns]
    assert inner_keys == ["dash_left", "dash_right"], (
        f"열 구성이 예상과 다릅니다: {inner_keys}"
    )

    # AppTest의 Column 노드는 proto가 Column 하위 메시지 자체다.
    weights = [round(col.proto.weight, 3) for col in columns]
    assert weights == [0.7, 0.3], (
        f"열 비율이 70/30이 아닙니다: {weights}"
    )

    # 비서 패널이 오른쪽 열 '안'에 있어야 한다. (목록 아래가 아니라)
    right = _find_block(columns[1], "ai_panel")
    assert right is not None, "AI 비서 패널이 오른쪽 열 안에 없습니다."
    assert _find_block(columns[0], "ai_panel") is None, (
        "AI 비서 패널이 왼쪽 열에도 들어 있습니다."
    )
    assert _find_block(columns[0], "bill_list_card") is not None, (
        "고지서 목록이 왼쪽 열 안에 없습니다."
    )
    print("PASS 1-1: 목록|비서가 같은 행의 2열 (0.7/0.3, wrap=False)")


def test_single_entry_point(at):
    """2: 플로팅 버튼·다이얼로그가 없고 입력창은 하나뿐이다."""
    try:
        _button(at, "floating_ai_assistant_btn")
        raise AssertionError("플로팅 AI 비서 버튼이 아직 남아 있습니다.")
    except KeyError:
        pass

    assert len(at.chat_input) == 1, (
        f"질문 입력창이 {len(at.chat_input)}개입니다. 진입점은 하나여야 합니다."
    )
    for key in ("ai_ex_week", "ai_ex_month", "ai_ex_bill"):
        _button(at, key)  # 없으면 KeyError
    print("PASS 2: 진입점 1개 (플로팅 버튼·다이얼로그 없음, 예시 버튼 3개)")


def test_plain_rerun_makes_no_call(at):
    """3: 상호작용 없이 재실행해도 호출되지 않는다."""
    _CAPTURED.clear()
    _run(at)
    _run(at)
    assert len(_CAPTURED) == 0, (
        f"단순 재실행으로 API가 {len(_CAPTURED)}회 호출되었습니다."
    )
    assert at.session_state["assistant_turn_count"] == 0
    print("PASS 3: 단순 재실행 2회 -> API 호출 0회")


def test_selection_change_makes_no_call(at, bill_id):
    """4: 고지서를 선택해도 호출되지 않는다."""
    _CAPTURED.clear()
    _button(at, f"sel_btn_{bill_id}").click()
    _run(at)

    assert at.session_state["selected_bill_id"] == bill_id, "선택이 반영되지 않았습니다."
    assert len(_CAPTURED) == 0, (
        f"고지서 선택만으로 API가 {len(_CAPTURED)}회 호출되었습니다."
    )
    texts = _markdown_values(at)
    target_blocks = [t for t in texts if 'class="ai-target"' in t]
    assert len(target_blocks) == 1, (
        f"질문 대상 영역이 {len(target_blocks)}개입니다. 1개여야 합니다."
    )
    assert "질문 대상" in target_blocks[0] and BILL_A["title"] in target_blocks[0], (
        f"선택한 고지서명이 질문 대상으로 표시되지 않았습니다.\n{target_blocks[0]}"
    )
    assert not any(SELECT_HINT in t for t in texts), (
        "고지서를 선택했는데 선택 안내가 남아 있습니다."
    )
    assert not any(NO_BILL_HINT in t for t in texts), (
        "고지서를 선택했는데 0건 안내가 표시됩니다."
    )
    print("PASS 4: 고지서 선택 -> API 호출 0회 / 질문 대상 표시 갱신")


def test_chat_input_submits_exactly_once(at):
    """5: 입력창 전송은 1회만 호출하고, 이후 재실행으로 중복되지 않는다."""
    _CAPTURED.clear()
    _ok_response("this_week")
    before_msgs = len(at.session_state["chat_messages"])

    at.chat_input[0].set_value("이번 주에 낼 게 있어?")
    _run(at)

    assert len(_CAPTURED) == 1, f"전송 1회에 API가 {len(_CAPTURED)}회 호출되었습니다."
    assert at.session_state["assistant_turn_count"] == 1
    assert len(at.session_state["chat_messages"]) == before_msgs + 2, (
        "질문과 답변이 각각 1건씩 기록되어야 합니다."
    )
    # 전송 직후 화면의 아바타 (기본 아바타가 아니라 지정한 단색 아이콘)
    avatars_after_send = {(m.name, m.avatar) for m in at.chat_message}
    assert avatars_after_send == {
        ("user", ":material/person:"),
        ("assistant", ":material/support_agent:"),
    }, f"전송 직후 아바타가 다릅니다: {avatars_after_send}"

    # 상호작용 없이 다시 실행 -> 같은 질문이 또 처리되면 안 된다.
    _run(at)
    assert {(m.name, m.avatar) for m in at.chat_message} == avatars_after_send, (
        "재실행 후 기록의 아바타가 전송 직후와 다릅니다."
    )
    assert len(_CAPTURED) == 1, (
        f"재실행으로 같은 질문이 중복 처리되었습니다. (누적 {len(_CAPTURED)}회)"
    )
    assert at.session_state["assistant_turn_count"] == 1
    print("PASS 5: 입력창 전송 -> 정확히 1회 호출 / 재실행 시 중복 처리 없음")


def test_example_buttons_submit_once(at):
    """6: 예시 버튼도 클릭당 정확히 1회 호출한다."""
    for key, intent in (("ai_ex_week", "this_week"), ("ai_ex_month", "monthly_total")):
        _CAPTURED.clear()
        _ok_response(intent)
        before_msgs = len(at.session_state["chat_messages"])
        before_turns = at.session_state["assistant_turn_count"]

        _button(at, key).click()
        _run(at)

        assert len(_CAPTURED) == 1, f"{key}: API 호출이 {len(_CAPTURED)}회입니다."
        assert at.session_state["assistant_turn_count"] == before_turns + 1
        assert len(at.session_state["chat_messages"]) == before_msgs + 2
        print(f"PASS 6: {key} 클릭 -> API 1회 호출")


def test_third_example_requires_selection(at, bill_id):
    """7: '선택한 고지서 요약'은 선택했을 때만 활성화된다."""
    # 선택 해제 상태 만들기
    at.session_state["selected_bill_id"] = None
    _run(at)
    assert _button(at, "ai_ex_bill").disabled is True, (
        "고지서를 선택하지 않았는데 요약 버튼이 활성화되어 있습니다."
    )

    _button(at, f"sel_btn_{bill_id}").click()
    _run(at)
    assert _button(at, "ai_ex_bill").disabled is False, (
        "고지서를 선택했는데도 요약 버튼이 비활성 상태입니다."
    )
    print("PASS 7: 요약 버튼은 선택 시에만 활성화")


def test_bill_question_passes_id_not_data(at, bill_id):
    """8: 선택한 고지서 데이터가 요청에 없고, 답변에는 있다."""
    assert at.session_state["selected_bill_id"] == bill_id

    _CAPTURED.clear()
    _ok_response("bill_summary")
    _button(at, "ai_ex_bill").click()
    _run(at)

    assert len(_CAPTURED) == 1, f"API 호출이 {len(_CAPTURED)}회입니다."
    call = _CAPTURED[0]
    assert call["contents"] == "이 고지서 요약해 줘", (
        f"전송된 질문이 다릅니다: {call['contents']!r}"
    )

    blob = privacy_test._payload_text()
    privacy_test._assert_absent(list(BILL_A.values()), blob, "UI bill_summary")

    answer = _assistant_messages(at)[-1]
    assert BILL_A["title"] in answer and f"{BILL_A['amount']:,}원" in answer, (
        f"답변에 선택한 고지서 정보가 없습니다.\n답변: {answer}"
    )
    print("PASS 8: 요청에는 고지서 데이터 없음 / 답변에는 있음")


def test_history_survives_view_switch(at):
    """9: 등록 화면을 다녀와도 대화가 유지된다."""
    before = list(at.session_state["chat_messages"])
    assert before, "이 테스트 전에 대화가 있어야 합니다."
    captured_before = len(_CAPTURED)

    add_btn = next(b for b in at.button if "고지서 추가" in b.label)
    add_btn.click()
    _run(at)
    assert at.session_state["current_view"] == "register"

    back_btn = next(b for b in at.button if "목록으로" in b.label)
    back_btn.click()
    _run(at)
    assert at.session_state["current_view"] == "dashboard"

    assert at.session_state["chat_messages"] == before, "대화 기록이 유지되지 않았습니다."
    rendered = _markdown_values(at)
    assert any(before[-1]["content"] in t for t in rendered), (
        "복귀 후 대화가 다시 그려지지 않았습니다."
    )
    assert len(_CAPTURED) == captured_before, (
        "화면 전환 중에 API가 호출되었습니다."
    )
    print("PASS 9: 등록 화면 왕복 후에도 대화 유지 / 추가 호출 없음")


def test_busy_error_shows_curated_message(at):
    """10: 503 혼잡은 저장된 고지서로 대체 답변하고, 401·403 인증 오류는 숨기지 않는다."""
    import services.assistant as assistant

    question = "이번 주에 낼 게 있어?"

    # 503: 예외 없이(_run이 확인) 저장된 고지서 기준 대체 답변이 표시된다.
    _CAPTURED.clear()
    _NEXT_BEHAVIOR[0] = ("raise", FakeAPIError(503))
    at.chat_input[0].set_value(question)
    _run(at)

    answer = _assistant_messages(at)[-1]
    assert answer.startswith(assistant.FALLBACK_NOTICE), (
        f"503에서 대체 답변 안내가 첫 줄에 없습니다.\n답변: {answer}"
    )
    # 답변 본문은 같은 DB에서 로컬로 조립한 이번 주 답변과 정확히 같아야 한다.
    expected_body = assistant._generate_answer(
        "this_week", assistant._query_data("this_week")
    )
    assert answer == f"{assistant.FALLBACK_NOTICE}\n\n{expected_body}", (
        f"저장된 고지서 기준 답변과 다릅니다.\n기대 본문: {expected_body}\n실제: {answer}"
    )
    assert assistant.BUSY_MESSAGE not in answer, f"혼잡 문구가 노출됩니다: {answer}"
    # 대체 답변 과정에서 추가 요청이 없고, 요청에는 질문만 담긴다.
    assert len(_CAPTURED) == 1, f"503 처리 중 API 호출이 {len(_CAPTURED)}회입니다."
    assert _CAPTURED[0]["contents"] == question, (
        f"전송 내용이 질문과 다릅니다: {_CAPTURED[0]['contents']!r}"
    )
    privacy_test._assert_absent(
        [BILL_A["title"], BILL_A["agency"], BILL_A["payment_method"],
         BILL_B["title"], BILL_B["agency"], BILL_B["payment_method"]],
        privacy_test._payload_text(),
        "UI 503 fallback",
    )

    # 401·403: 대체 답변으로 숨기지 않고 인증 안내를 그대로 보여준다.
    for code in (401, 403):
        _CAPTURED.clear()
        _NEXT_BEHAVIOR[0] = ("raise", FakeAPIError(code))
        at.chat_input[0].set_value(question)
        _run(at)
        answer = _assistant_messages(at)[-1]
        assert "인증" in answer, f"{code}: 인증 실패 안내가 아닙니다: {answer}"
        assert assistant.FALLBACK_NOTICE not in answer, (
            f"{code}: 인증 오류가 대체 답변으로 숨겨졌습니다: {answer}"
        )
        assert "답변 생성 중 오류가 발생했습니다" not in answer, (
            f"{code}: 인증 오류가 일반 문구로 숨겨졌습니다: {answer}"
        )
        assert len(_CAPTURED) == 1, f"{code}: API 호출이 {len(_CAPTURED)}회입니다."
    _ok_response()
    print("PASS 10: 503 -> 저장된 고지서 기준 대체 답변 / 401·403 -> 인증 안내 유지")


def test_no_dangling_html_tags(at):
    """11: 여닫기 태그만 따로 내보내는 방식이 남아 있지 않다."""
    for value in _markdown_values(at):
        stripped = value.strip()
        assert stripped != "</div>", "고아 </div> 마크다운이 남아 있습니다."
        if "<div" in stripped:
            opens = stripped.count("<div")
            closes = stripped.count("</div>")
            assert opens == closes, (
                f"태그가 짝이 맞지 않는 마크다운이 있습니다 "
                f"(<div {opens}개 / </div> {closes}개):\n{stripped[:200]}"
            )
    print("PASS 11: 고아/미완결 HTML 태그 없음")


def test_deleted_selection_clears_state(at, bill_id):
    """12: 선택한 고지서가 삭제되면 선택 상태가 정리된다."""
    at.session_state["selected_bill_id"] = bill_id
    _run(at)
    assert at.session_state["selected_bill_id"] == bill_id

    assert db_module.delete_bill(bill_id) is True

    _CAPTURED.clear()
    _run(at)
    assert at.session_state["selected_bill_id"] is None, (
        "삭제된 고지서의 선택이 해제되지 않았습니다."
    )
    assert len(_CAPTURED) == 0, "선택 정리 과정에서 API가 호출되었습니다."

    texts = _markdown_values(at)
    assert any(SELECT_HINT in t for t in texts), (
        "선택 해제 후 안내 문구가 표시되지 않았습니다."
    )
    print("PASS 12: 삭제된 선택 자동 해제 / API 호출 0회")


def test_question_ui_available_with_no_bills():
    """14: 고지서가 0건이어도 질문 UI는 사용할 수 있다.

    선택이 필요한 기능(선택한 고지서 요약)만 비활성화되어야 한다.
    """
    for bill in db_module.get_bills(include_paid=True):
        db_module.delete_bill(bill["id"])

    _CAPTURED.clear()
    _ok_response("this_week")
    at = _run(_new_app())

    assert len(at.chat_input) == 1, "고지서가 없을 때 질문 입력창이 사라졌습니다."
    assert _button(at, "ai_ex_week").disabled is False, (
        "고지서가 없어도 '이번 주 일정'은 사용할 수 있어야 합니다."
    )
    assert _button(at, "ai_ex_month").disabled is False, (
        "고지서가 없어도 '이번 달 합계'는 사용할 수 있어야 합니다."
    )
    assert _button(at, "ai_ex_bill").disabled is True, (
        "선택한 고지서가 없으면 요약 버튼은 비활성이어야 합니다."
    )
    texts = _markdown_values(at)
    assert any("첫 고지서를 등록해 보세요." in t for t in texts), (
        "고지서가 없을 때 목록의 첫 등록 안내가 없습니다."
    )
    # 패널 안내도 0건 상태 문구여야 한다. (있음·미선택 문구와 구분)
    assert any(NO_BILL_HINT in t for t in texts), "0건일 때 패널 안내가 없습니다."
    assert sum(t.count(NO_BILL_HINT) for t in texts) == 1, "0건 안내가 중복 표시됩니다."
    assert not any(SELECT_HINT in t for t in texts), (
        "0건인데 '고지서를 선택해 주세요' 안내가 표시됩니다."
    )
    assert not any("예정된 미납 고지서가 없습니다" in t for t in texts), (
        "고지서가 0건인데 '가장 가까운 납부기한' 빈 카드가 중복 안내로 남아 있습니다."
    )
    assert len(_CAPTURED) == 0, "고지서가 없는 화면을 그리며 API가 호출되었습니다."

    # 실제로 질문이 동작하는지 확인 (0건이어도 답변이 나와야 한다)
    at.chat_input[0].set_value("이번 주에 낼 게 있어?")
    _run(at)
    assert len(_CAPTURED) == 1, f"API 호출이 {len(_CAPTURED)}회입니다."
    answer = _assistant_messages(at)[-1]
    assert "없습니다" in answer, f"0건일 때 답변이 이상합니다: {answer}"
    print("PASS 14: 고지서 0건에서도 입력창·일정 질문 버튼 사용 가능 / 요약만 비활성")


def _history_block(at):
    """대화 기록 컨테이너와 그 높이 설정을 돌려준다. (없으면 (None, None))"""
    for key in ("ai_history", "ai_history_empty"):
        block = _find_block(at._tree, key)
        if block is not None:
            return key, block.proto.height_config
    return None, None


def test_history_height_steps(bill_id):
    """17: 대화 길이에 따라 기록 영역 높이가 content → 340 → 420으로 정해진다."""
    import services.assistant as assistant

    long_answer = (
        "'긴 답변 예시 고지서' 정보입니다.\n"
        "• 발급 기관: 대구광역시 수성구 상하수도사업본부\n"
        "• 납부 금액: 45,200원\n"
        "• 납부 기한: 2026-09-30 (5일 남음)\n"
        "• 납부 방법: 위택스, 가상계좌, 금융기관 창구\n"
        "• 관리 상태: 미납\n"
        "납부 방법이 저장되어 있지 않습니다. 발급 기관에 확인해 주세요.\n"
        "※ 관리 상태는 사용자가 표시한 값이며 실제 납부 여부 확인 결과가 아닙니다."
    )
    cases = [
        # (라벨, 대화 기록, 기대 key, 기대 높이)
        ("대화 없음", [], "ai_history_empty", None),
        (
            "짧은 대화",
            [{"role": "user", "content": "이번 주 일정"},
             {"role": "assistant", "content": "이번 주에 납부 기한이 있는 미납 고지서가 없습니다."}],
            "ai_history",
            "content",
        ),
        (
            # 추정 높이가 content 기준(300px)은 넘고 420px 단계에는 못 미치는 길이
            "보통 길이",
            [{"role": "user", "content": "이번 달 합계"},
             {"role": "assistant", "content": "\n".join(
                 ["이번 달 납부 예정 금액입니다."] + [f"• 항목 {i} — 10,000원" for i in range(10)]
             )}],
            "ai_history",
            340,
        ),
        (
            "긴 선택 고지서 요약",
            [{"role": "user", "content": "이 고지서 요약해 줘"},
             {"role": "assistant", "content": long_answer},
             {"role": "user", "content": "납부 방법은?"},
             {"role": "assistant", "content": long_answer}],
            "ai_history",
            420,
        ),
    ]

    for label, messages, expected_key, expected_height in cases:
        _CAPTURED.clear()
        at = _new_app()
        at.session_state["chat_messages"] = list(messages)
        at.session_state["selected_bill_id"] = bill_id
        _run(at)

        key, height = _history_block(at)
        assert key == expected_key, f"{label}: 컨테이너가 {key}입니다. {expected_key}여야 합니다."
        if expected_height is None:
            assert not _find_block(at._tree, "ai_history"), f"{label}: 고정 높이 영역이 생겼습니다."
        elif expected_height == "content":
            assert height.use_content, (
                f"{label}: 내용 높이가 아니라 {height}입니다. 짧은 대화에 빈 공간이 생깁니다."
            )
        else:
            assert height.pixel_height == expected_height, (
                f"{label}: 높이가 {height.pixel_height}입니다. {expected_height}여야 합니다."
            )
            assert height.pixel_height > 280, "기존 고정 높이(280)보다 커야 합니다."

        # 기록 영역 밖의 예시 버튼·입력창은 그대로 있어야 한다.
        for btn_key in ("ai_ex_week", "ai_ex_month", "ai_ex_bill"):
            _button(at, btn_key)
        assert len(at.chat_input) == 1, f"{label}: 입력창이 사라졌습니다."
        assert len(_CAPTURED) == 0, f"{label}: 기록만 그렸는데 API가 호출되었습니다."
        print(f"PASS 17: {label} -> {expected_key} / {expected_height or '고정 높이 없음'}")

    # 질문을 보낸 실행에서는 답변 길이를 미리 알 수 없으므로 최소 340px를 쓴다.
    # (짧은 답변이 와도 그 실행에서는 340px여야 첫 답변이 잘리지 않는다)
    _CAPTURED.clear()
    _ok_response("this_week")
    at = _new_app()
    at.session_state["selected_bill_id"] = bill_id
    _run(at)
    at.chat_input[0].set_value("이번 주에 낼 게 있어?")
    _run(at)

    key, height = _history_block(at)
    assert key == "ai_history" and height.pixel_height == 340, (
        f"질문 전송 직후 높이가 {key}/{height}입니다. 340px여야 합니다."
    )
    assert len(_CAPTURED) == 1, f"전송 1회에 API가 {len(_CAPTURED)}회 호출되었습니다."
    print("PASS 17: 질문 전송 직후(답변 길이 미상) -> 340px")


def test_empty_state_register_button():
    """15: 0건 안내의 '고지서 등록' 버튼은 기존 등록 화면으로 이동하며 API를 부르지 않는다."""
    assert not db_module.get_bills(include_paid=True), "이 테스트는 고지서 0건에서 실행해야 합니다."

    _CAPTURED.clear()
    at = _run(_new_app())
    _button(at, "empty_register_btn").click()
    _run(at)

    assert at.session_state["current_view"] == "register", (
        "'고지서 등록' 버튼이 등록 화면으로 이동하지 않았습니다."
    )
    assert any("목록으로" in b.label for b in at.button), "등록 화면이 그려지지 않았습니다."
    assert len(_CAPTURED) == 0, "등록 화면 이동 중에 API가 호출되었습니다."
    print("PASS 15: 0건 안내 '고지서 등록' -> 등록 화면 이동 / API 호출 0회")


def test_paid_only_empty_state():
    """16: 완료 고지서만 있으면 '미납 고지서가 없습니다'와 완료 건 보기 안내를 보여준다."""
    bill_id = _save(BILL_B, days=3)
    assert db_module.mark_as_paid(bill_id) is True

    _CAPTURED.clear()
    at = _run(_new_app())
    texts = _markdown_values(at)
    assert any("미납 고지서가 없습니다." in t for t in texts), "완료 건만 있을 때의 안내가 없습니다."
    assert any("납부완료 고지서도 보기" in t for t in texts), "완료 건 보기 안내가 없습니다."
    assert not any("첫 고지서를 등록해 보세요." in t for t in texts), (
        "완료 고지서가 있는데 첫 등록 안내가 표시되었습니다."
    )
    # 목록에서 숨겨져 있어도 저장된 고지서가 있는 상태이므로 0건 안내와 구분한다.
    assert any(SELECT_HINT in t for t in texts), (
        "완료 건만 있을 때 패널 안내가 '있음·미선택' 문구가 아닙니다."
    )
    assert not any(NO_BILL_HINT in t for t in texts), (
        "완료 고지서가 있는데 0건 안내가 표시됩니다."
    )
    assert len(at.checkbox) == 1, "완료 건을 볼 수 있는 체크박스가 없습니다."
    try:
        _button(at, "empty_register_btn")
        raise AssertionError("완료 고지서가 있는데 첫 등록 버튼이 표시되었습니다.")
    except KeyError:
        pass

    # 체크박스를 켜면 완료 건이 목록에 나타난다 (기존 필터 동작 유지)
    at.checkbox[0].check()
    _run(at)
    _button(at, f"sel_btn_{bill_id}")  # 없으면 KeyError
    assert len(_CAPTURED) == 0, "빈 상태 화면에서 API가 호출되었습니다."
    print("PASS 16: 완료 건만 있을 때 미납 없음 안내 / 체크박스로 완료 건 표시")


# ── 실행 ──────────────────────────────────────────────────────


def main():
    snapshot = _snapshot_state()

    tmpdir = tempfile.TemporaryDirectory()
    try:
        _install_fake_sdk_keep_google()
        os.environ["GEMINI_API_KEY"] = "TEST-DUMMY-KEY-NOT-REAL"
        os.environ["GEMINI_MODEL"] = "test-model"

        # app.py는 import 시점에 init_db()를 부르므로 실행 전에 경로를 바꾼다.
        db_module._DB_DIR = tmpdir.name
        db_module._DB_PATH = os.path.join(tmpdir.name, "test_ui.db")
        db_module.init_db()

        id_a = _save(BILL_A, days=5)
        _save(BILL_B, days=9)

        at = test_first_render_makes_no_call()
        test_dashboard_two_column_structure(at)
        test_single_entry_point(at)
        test_plain_rerun_makes_no_call(at)
        test_selection_change_makes_no_call(at, id_a)
        test_chat_input_submits_exactly_once(at)
        test_example_buttons_submit_once(at)
        test_third_example_requires_selection(at, id_a)
        test_bill_question_passes_id_not_data(at, id_a)
        test_history_survives_view_switch(at)
        test_busy_error_shows_curated_message(at)
        test_no_dangling_html_tags(at)
        test_deleted_selection_clears_state(at, id_a)
        test_history_height_steps(id_a)
        test_question_ui_available_with_no_bills()
        # 14번은 0건 상태에서 질문 1회를 남긴다. 등록 버튼 테스트는 새 세션에서 0건을 확인한다.
        test_empty_state_register_button()
        test_paid_only_empty_state()
        print("\n전체 통과: 클릭·전송 시에만 1회 호출되며 재실행·선택으로는 호출되지 않습니다.")
    finally:
        _restore_state(snapshot)
        tmpdir.cleanup()

    print("\n[정리 후 상태 복원 검증 - 테스트를 실행한 같은 프로세스]")
    _verify_restored(snapshot)
    assert not os.path.exists(tmpdir.name), f"임시 디렉터리가 남아 있습니다: {tmpdir.name}"
    print(f"PASS 13: 임시 테스트 DB 디렉터리 삭제 확인 -> {tmpdir.name}")
    print("\n전체 통과: 전역 상태가 실행 전으로 복원되었습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
