"""
services/assistant.py
AI 비서 — 자연어 질문 → 의도 분류 → DB 조회 → 답변 생성.

일정·합계 의도(this_week/this_month/monthly_total/nearest_due/list_all)의 DB 조회는
status='미납' 고지서만 대상으로 한다. 반면 사용자가 목록에서 직접 선택한 고지서
한 건에 대한 질문(bill_* 의도)은 납부완료 고지서도 답변 대상이다.

[외부 전송 범위]
Gemini API를 호출하는 지점은 _classify_intent() 하나뿐이며, 전송하는 내용은
사용자가 입력한 질문 텍스트와 정적인 분류 지침뿐이다. DB 조회·금액 합산·날짜 계산·
답변 문장 생성은 모두 로컬에서 수행하므로 고지서명·발급 기관·금액·납부기한·납부방법은
외부로 전송되지 않는다. 선택한 고지서의 id조차 전송하지 않으며, 이 모듈이 id로
DB를 다시 읽는다. (test_assistant_no_bill_data.py에서 검증)
"""

import json
import re
import datetime

from services import gemini_client
from database.db import (
    get_bills,
    get_bill_by_id,
    get_bills_due_this_week,
    get_bills_due_this_month,
    get_total_amount_this_month,
    get_nearest_due_bill,
    parse_amount,
    parse_due_date,
)


# ── 의도 분류 스키마 ──────────────────────────────────────────

_INTENT_LABELS = {
    # 일정·합계 (미납 고지서만 조회)
    "this_week": "이번 주 납부 기한 고지서 조회 (예: '이번 주에 낼 게 있어?')",
    "this_month": "이번 달 납부 기한 고지서 조회 (예: '이번 달에 납부할 고지서 보여줘')",
    "monthly_total": "이번 달 총 납부 예정 금액 (예: '이번 달 미납 합계가 얼마야?')",
    "nearest_due": "가장 가까운 납부 기한 고지서 (예: '제일 급한 게 뭐야?')",
    "list_all": "미납 고지서 전체 목록 (예: '안 낸 거 전부 보여줘')",
    # 사용자가 선택한 고지서 한 건 (납부완료 고지서도 대상)
    "bill_summary": (
        "선택한 고지서 한 건의 전체 요약 "
        "(예: '이 고지서 요약해 줘', '이거 어떤 고지서야?')"
    ),
    "bill_due": (
        "선택한 고지서의 납부 기한이 '언제'인지 "
        "(예: '이거 언제까지 내야 해?', '납부기한이 며칠이야?')"
    ),
    "bill_amount": (
        "선택한 고지서의 납부 금액이 '얼마'인지 "
        "(예: '금액이 얼마야?', '이거 얼마 내야 해?')"
    ),
    "bill_payment": (
        "선택한 고지서의 납부 방법·납부 수단 "
        "(예: '납부 방법은?', '어떻게 내면 돼?')"
    ),
    "bill_obligation": (
        "선택한 고지서를 꼭 내야 하는지·안 내도 되는지 묻는 납부 의무 판단 요청 "
        "(예: '이 고지서 꼭 내야 해?', '안 내도 돼?', '이거 면제 안 되나?')"
    ),
    "unknown": "고지서와 관련 없는 질문 또는 파악 불가",
}

# 선택한 고지서 한 건을 대상으로 하는 의도.
_BILL_INTENTS = frozenset(
    {"bill_summary", "bill_due", "bill_amount", "bill_payment", "bill_obligation"}
)


_UNKNOWN_ANSWER = (
    "죄송합니다, 고지서 관련 질문만 답변할 수 있습니다. "
    "예를 들어 '이번 주에 낼 게 있어?', '이번 달 납부 금액은?', "
    "'가장 가까운 납부기한은?' 등으로 질문해 주세요. "
    "고지서를 선택한 뒤 '이 고지서 요약해 줘'처럼 물어볼 수도 있습니다."
)

# 목록형 의도(this_week/this_month/list_all)의 첫 문장과 빈 결과 문구.
_HEADER_MESSAGES = {
    "this_week": "이번 주에 납부 기한이 있는 미납 고지서는 {count}건입니다.",
    "this_month": "이번 달에 납부 기한이 있는 미납 고지서는 {count}건입니다.",
    "list_all": "현재 등록된 미납 고지서는 {count}건입니다.",
}

_EMPTY_MESSAGES = {
    "this_week": "이번 주에 납부 기한이 있는 미납 고지서가 없습니다.",
    "this_month": "이번 달에 납부 기한이 있는 미납 고지서가 없습니다.",
    "list_all": "현재 등록된 미납 고지서가 없습니다.",
}

# 재시도 후에도 실패한 일시적 오류에 사용할 안내 문구
BUSY_MESSAGE = "현재 AI 서버가 혼잡합니다. 잠시 후 다시 질문해 주세요."

# ── 선택한 고지서 관련 고정 문구 ─────────────────────────────

# 납부 의무는 저장된 정보로 판단할 수 없다. 이 문구는 한 줄로 그대로 사용한다.
OBLIGATION_ANSWER = (
    "저장된 정보만으로 납부 의무를 판단할 수 없습니다. 발급 기관에 확인해 주세요."
)

NO_SELECTION_ANSWER = (
    "어떤 고지서에 대한 질문인지 알 수 없습니다. "
    "목록에서 고지서를 먼저 선택한 뒤 다시 질문해 주세요."
)

MISSING_BILL_ANSWER = (
    "선택한 고지서를 찾을 수 없습니다. 삭제되었을 수 있으니 "
    "목록에서 다시 선택해 주세요."
)

_UNKNOWN_FIELD = "확인되지 않음"


# ── 납부 의무 질문 로컬 보정 ──────────────────────────────────
# 의도 분류는 확률적이므로, 납부 의무를 묻는 대표적인 표현은 로컬에서 한 번 더
# 확인해 고정 안내로 처리한다. Gemini 호출이 끝난 뒤에 도는 순수 로컬 판정이라
# API 호출 횟수나 전송 내용에는 영향이 없다.
#
# 한계: 정규식은 아래 표현과 그 변형만 잡는다. 모든 자연어 표현을 판별하지 못한다.

_OBLIGATION_RE = re.compile(
    r"(꼭\s*내야|반드시\s*내야|꼭\s*납부|무조건\s*내야"
    r"|안\s*내도\s*(되|돼|됨|될)|내지\s*않아도\s*(되|돼|됨|될)"
    r"|안\s*내면\s*(어떻게|어떻|되)|납부\s*안\s*해도|납부하지\s*않아도"
    r"|납부\s*의무|내야\s*하(나|는지|나요|냐)|면제\s*(되|받|돼|대상))"
)

# '이 고지서'처럼 한 건을 가리키는 표현. '하는 거'의 '거'와 겹치지 않도록
# '이거/이것/이건'만 지시 표현으로 본다.
_BILL_SCOPE_RE = re.compile(
    r"(이\s*고지서|그\s*고지서|해당\s*고지서|선택(한|된)|이거|이건|이것)"
)

# '이번 달에 꼭 내야 하는 거 있어?'처럼 기간·목록을 묻는 일정 질문의 표지.
# 이 표현이 있으면 의무 질문으로 보정하지 않는다.
_SCHEDULE_SCOPE_RE = re.compile(
    r"(이번\s*주|이번\s*달|다음\s*주|다음\s*달|이달|금주|금월"
    r"|전체|전부|모두|모든|목록|리스트|합계|총액|몇\s*건)"
)


def _apply_local_overrides(question: str, intent: str) -> str:
    """분류 결과를 로컬에서 한 번 보정한다. (외부 호출 없음)

    '안 내도 돼?'처럼 명확한 납부 의무 질문이 bill_due나 unknown으로 분류되어도
    고정 안내로 처리되도록 bill_obligation으로 교정한다. 다만
    '이번 달에 꼭 내야 하는 거 있어?' 같은 일정 질문은 교정하지 않는다.

    Returns:
        교정된 의도 키. 해당 없으면 입력 intent 그대로.
    """
    if not _OBLIGATION_RE.search(question):
        return intent

    # '이 고지서'처럼 한 건을 가리키면 기간 표현이 섞여 있어도 의무 질문으로 본다.
    if intent in _BILL_INTENTS or _BILL_SCOPE_RE.search(question):
        return "bill_obligation"

    # 지시 표현이 없더라도 기간·목록 표지가 없으면 의무 질문으로 본다.
    # ('안 내도 돼?'가 unknown으로 분류되는 경우를 여기서 건진다.)
    if not _SCHEDULE_SCOPE_RE.search(question):
        return "bill_obligation"

    return intent


def _classify_intent(question: str) -> str:
    """사용자 질문의 의도를 분류한다.

    이 함수는 모듈에서 유일하게 Gemini API를 호출하는 지점이며,
    전송하는 내용은 question 문자열뿐이다. 고지서 데이터를 여기에
    추가해서는 안 된다. (README '외부 전송 범위' 참고)

    Returns:
        _INTENT_LABELS의 키 중 하나 (str).
        빈 응답·잘못된 JSON·목록에 없는 의도인 경우 "unknown".

    Raises:
        GeminiError: API 키 누락, SDK 미설치, 인증 실패(401/403),
            모델 없음(404), 사용량 초과(429) 등 사용자 조치가 필요한 오류.
        GeminiBusyError: 재시도 후에도 계속된 혼잡·타임아웃·연결 실패.
    """
    api_key = gemini_client.load_api_key("AI 비서")
    model_id = gemini_client.get_model_id()

    _, types, APIError, httpx = gemini_client.import_sdk()
    client = gemini_client.create_client(api_key)

    intent_descriptions = "\n".join(
        f"- {key}: {desc}" for key, desc in _INTENT_LABELS.items()
    )

    # 아래 지침은 전부 정적이다. DB 조회 결과·선택한 고지서의 id나 이름·
    # 대화 기록·PDF 원문을 여기에 넣어서는 안 된다.
    system_instruction = (
        "당신은 고지서 관리 시스템의 의도 분류기입니다.\n"
        "사용자의 질문을 읽고, 아래 의도 중 하나를 선택하세요.\n"
        "반드시 JSON 형식으로 intent 키만 반환하세요.\n\n"
        f"[의도 목록]\n{intent_descriptions}\n\n"
        "[분류 규칙]\n"
        "- bill_due는 '언제', '며칠까지', '기한', '마감'처럼 시점을 묻는 경우에만 고릅니다.\n"
        "- '꼭 내야 해?', '안 내도 돼?', '납부 의무가 있어?', '면제 안 되나?'처럼\n"
        "  납부를 해야 하는지 여부를 묻는 경우는 bill_due가 아니라 bill_obligation입니다.\n"
        "- '이 고지서', '이거', '해당 고지서', '선택한 고지서'처럼 한 건을 가리키는\n"
        "  표현이 있으면 bill_ 로 시작하는 의도를 우선합니다.\n"
        "- '납부 방법은?', '금액이 얼마야?'처럼 대상이 생략된 짧은 질문도\n"
        "  선택한 고지서에 대한 질문으로 보고 bill_ 의도를 고릅니다.\n"
        "- '이번 주', '이번 달', '전체 목록'처럼 기간이나 여러 건을 묻는 질문은\n"
        "  bill_ 의도가 아니라 일정·합계 의도입니다.\n\n"
        "[출력 형식]\n"
        '{"intent": "의도_키"}'
    )

    try:
        response = client.models.generate_content(
            model=model_id,
            contents=question,  # 질문 문자열만 전송한다. 고지서 데이터 추가 금지.
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
    except APIError as e:
        # 인증 실패·모델 없음·사용량 초과는 숨기지 않고 안내 문구로 전파한다.
        gemini_client.raise_for_api_error(
            e, model_id=model_id, busy_message=BUSY_MESSAGE
        )
    except (httpx.TimeoutException, httpx.ConnectError):
        raise gemini_client.GeminiBusyError(
            BUSY_MESSAGE, reason=gemini_client.REASON_TIMEOUT
        )

    # 여기부터는 응답 형식 문제이므로 규칙 기반 안내로 흡수한다.
    try:
        if not response.text:
            return "unknown"
        intent = json.loads(response.text).get("intent", "unknown")
        if intent in _INTENT_LABELS:
            return intent
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass

    return "unknown"


def _query_data(intent: str, selected_bill_id=None) -> dict:
    """의도에 맞는 DB 조회를 수행하고 결과를 dict로 반환한다.

    일정·합계 의도의 조회는 기존과 동일하게 include_paid=False (미납만)이다.
    선택한 고지서 한 건을 묻는 bill_* 의도는 id로 직접 조회하며, 이때는
    납부완료 고지서도 대상이다.

    Args:
        intent: 분류된 의도 키.
        selected_bill_id: UI에서 선택된 고지서의 id. bill_* 의도에서만 사용한다.
    """
    if intent in _BILL_INTENTS:
        # 선택하지 않았다면 임의의 고지서로 답하지 않는다.
        if selected_bill_id is None:
            return {"intent": intent, "bill": None, "selection": "none"}
        bill = get_bill_by_id(selected_bill_id)
        if bill is None:
            return {"intent": intent, "bill": None, "selection": "missing"}
        return {"intent": intent, "bill": bill, "selection": "ok"}

    if intent == "this_week":
        bills = get_bills_due_this_week(include_paid=False)
        return {"intent": intent, "bills": bills, "count": len(bills)}

    elif intent == "this_month":
        bills = get_bills_due_this_month(include_paid=False)
        return {"intent": intent, "bills": bills, "count": len(bills)}

    elif intent == "monthly_total":
        total = get_total_amount_this_month(include_paid=False)
        bills = get_bills_due_this_month(include_paid=False)
        return {"intent": intent, "total_amount": total, "bills": bills, "count": len(bills)}

    elif intent == "nearest_due":
        bill = get_nearest_due_bill(include_paid=False)
        return {"intent": intent, "bill": bill}

    elif intent == "list_all":
        bills = get_bills(include_paid=False)
        return {"intent": intent, "bills": bills, "count": len(bills)}

    else:
        return {"intent": "unknown"}


def _generate_answer(intent: str, data: dict) -> str:
    """DB 조회 결과를 바탕으로 답변 문장을 만든다.

    전 과정이 로컬에서 수행된다. 고지서 데이터는 외부로 전송되지 않는다.
    """
    if intent == "unknown":
        return _UNKNOWN_ANSWER

    if intent in _BILL_INTENTS:
        selection = data.get("selection")
        if selection == "missing":
            return MISSING_BILL_ANSWER
        if selection == "none":
            if intent == "bill_obligation":
                # 의무 판단 거절은 어떤 고지서든 동일하므로 안내는 하되,
                # 임의의 고지서를 대상으로 삼지 않았음을 분명히 한다.
                return f"{OBLIGATION_ANSWER}\n{NO_SELECTION_ANSWER}"
            return NO_SELECTION_ANSWER
        return _compose_bill_answer(intent, data["bill"])

    return _compose_answer(intent, data)


# ── 규칙 기반 답변 조립 (로컬 전용) ───────────────────────────


def _format_amount(value) -> str:
    """금액을 '1,234원' 형식으로 만든다. 값이 없거나 해석할 수 없으면 '금액 미확인'."""
    # 0원도 유효한 금액이므로 falsy 검사가 아닌 None 비교를 사용한다.
    amount = parse_amount(value)
    if amount is None:
        return "금액 미확인"
    return f"{amount:,}원"


def _bill_line(bill: dict) -> str:
    """고지서 한 건을 불릿 한 줄로 만든다."""
    title = bill.get("title") or "제목 없음"
    amount = _format_amount(bill.get("amount"))
    due = bill.get("due_date") or "기한 미확인"
    return f"• {title} — {amount} (기한: {due})"


def _sum_amounts(bills: list) -> int:
    """금액을 해석할 수 있는 고지서의 합계를 구한다."""
    return sum(parse_amount(b.get("amount")) or 0 for b in bills)


def _days_left(due_date: str):
    """납부기한까지 남은 일수. YYYY-MM-DD 실제 날짜가 아니면 None."""
    due = parse_due_date(due_date)
    if due is None:
        return None
    return (due - datetime.date.today()).days


# ── 선택한 고지서 한 건 답변 (로컬 전용) ─────────────────────


def _bill_name(bill: dict) -> str:
    """답변에 쓸 고지서 이름. 대화 기록에서 대상이 섞이지 않도록 항상 붙인다."""
    return bill.get("title") or "제목 없는 고지서"


def _status_label(bill: dict) -> str:
    """관리 상태 문구. 실제 납부 여부가 아니라 사용자가 관리하는 표시다."""
    return bill.get("status") or "미납"


def _due_phrase(bill: dict) -> str:
    """납부기한과 남은 일수를 한 구절로 만든다. 기한이 없으면 '확인되지 않음'."""
    due = bill.get("due_date")
    if not due:
        return _UNKNOWN_FIELD

    remaining = _days_left(due)
    if remaining is None:
        return due
    if remaining == 0:
        return f"{due} (오늘 마감)"
    if remaining > 0:
        return f"{due} ({remaining}일 남음)"
    return f"{due} (기한이 {abs(remaining)}일 지났습니다)"


def _payment_guidance(bill: dict) -> str:
    """납부방법이 없을 때의 안내.

    계좌번호·납부 링크·세부 절차는 만들어내지 않고, 확인할 곳만 알려준다.
    """
    agency = bill.get("agency")
    if agency:
        return f"납부 방법이 저장되어 있지 않습니다. 발급 기관({agency})에 확인해 주세요."
    return (
        "납부 방법과 발급 기관이 모두 저장되어 있지 않습니다. "
        "고지서 원문을 확인해 주세요."
    )


def _compose_bill_answer(intent: str, bill: dict) -> str:
    """선택한 고지서 한 건에 대한 답변을 조립한다. (저장된 정보만 사용)"""
    name = _bill_name(bill)
    amount = parse_amount(bill.get("amount"))
    agency = bill.get("agency")
    due = bill.get("due_date")
    method = bill.get("payment_method")

    if intent == "bill_obligation":
        lines = [f"'{name}'에 대한 답변입니다.", OBLIGATION_ANSWER]
        if agency:
            lines.append(f"(발급 기관: {agency})")
        return "\n".join(lines)

    if intent == "bill_amount":
        lines = [f"'{name}'의 납부 금액은 {_format_amount(amount)}입니다."]
        if amount is None:
            lines.append("저장된 금액이 없습니다. 고지서 원문을 확인해 주세요.")
        return "\n".join(lines)

    if intent == "bill_due":
        if due:
            lines = [f"'{name}'의 납부 기한은 {_due_phrase(bill)}입니다."]
        else:
            lines = [
                f"'{name}'의 납부 기한은 {_UNKNOWN_FIELD}입니다.",
                "고지서 원문을 확인해 주세요.",
            ]
        if _status_label(bill) == "납부완료":
            lines.append(
                "이 고지서는 납부완료로 표시되어 있습니다. "
                "(실제 납부 여부가 아니라 사용자가 관리하는 표시입니다.)"
            )
        return "\n".join(lines)

    if intent == "bill_payment":
        if method:
            return f"'{name}'의 납부 방법은 {method}입니다."
        return f"'{name}' — {_payment_guidance(bill)}"

    # bill_summary
    lines = [
        f"'{name}' 정보입니다.",
        f"• 발급 기관: {agency or _UNKNOWN_FIELD}",
        f"• 납부 금액: {_format_amount(amount)}",
        f"• 납부 기한: {_due_phrase(bill)}",
        f"• 납부 방법: {method or _UNKNOWN_FIELD}",
        f"• 관리 상태: {_status_label(bill)}",
    ]
    if not method:
        lines.append(_payment_guidance(bill))
    lines.append(
        "※ 관리 상태는 사용자가 표시한 값이며 실제 납부 여부 확인 결과가 아닙니다."
    )
    return "\n".join(lines)


def _compose_answer(intent: str, data: dict) -> str:
    """의도별 답변 문장을 Python에서 직접 조립한다.

    금액 합계와 날짜 계산도 여기서 수행하며 외부 호출은 하지 않는다.
    """
    if intent in ("this_week", "this_month", "list_all"):
        bills = data.get("bills", [])
        if not bills:
            return _EMPTY_MESSAGES[intent]
        lines = [_HEADER_MESSAGES[intent].format(count=len(bills))]
        lines.extend(_bill_line(b) for b in bills)
        lines.append(f"합계: {_sum_amounts(bills):,}원")
        return "\n".join(lines)

    if intent == "monthly_total":
        bills = data.get("bills", [])
        count = data.get("count", 0)
        if count == 0:
            return "이번 달 납부 예정 고지서가 없습니다."
        total = data.get("total_amount") or 0
        lines = [f"이번 달 납부 예정 금액은 {total:,}원입니다. ({count}건)"]
        lines.extend(_bill_line(b) for b in bills)
        return "\n".join(lines)

    if intent == "nearest_due":
        bill = data.get("bill")
        if not bill:
            return "현재 납부 기한이 다가오는 미납 고지서가 없습니다."
        due = bill.get("due_date") or "미확인"
        remaining = _days_left(bill.get("due_date"))
        if remaining is None:
            when = ""
        elif remaining == 0:
            when = " (오늘 마감)"
        else:
            when = f" ({remaining}일 남음)"
        return (
            f"가장 가까운 납부 기한은 {due}입니다.{when}\n"
            f"{_bill_line(bill)}"
        )

    return "답변을 생성할 수 없습니다."


# ── 공개 인터페이스 ───────────────────────────────────────────


def ask(question: str, *, selected_bill_id=None) -> str:
    """사용자 질문에 대한 AI 비서 답변을 반환한다.

    흐름: 질문 → 의도 분류(Gemini 1회) → 로컬 보정 → DB 조회 → 답변 생성

    Args:
        question: 사용자의 자연어 질문.
        selected_bill_id: UI에서 선택된 고지서의 id. **id만** 전달받으며,
            고지서 내용은 이 함수가 DB에서 직접 다시 읽는다. Gemini로는
            전송되지 않는다. 선택하지 않았으면 None.

    Returns:
        AI 비서의 답변 문자열.

    Raises:
        GeminiError: API 키 누락, 인증 실패, 모델 없음, 사용량 초과 등.
        GeminiBusyError: 재시도 후에도 계속된 혼잡·타임아웃·연결 실패.
    """
    if not question or not question.strip():
        return "질문을 입력해 주세요."

    intent = _classify_intent(question)
    intent = _apply_local_overrides(question, intent)
    data = _query_data(intent, selected_bill_id=selected_bill_id)
    answer = _generate_answer(intent, data)
    return answer


# ── API 실패 시 로컬 대체 답변 ────────────────────────────────
# 의도 분류(Gemini)가 실패해도 핵심 질문은 저장된 고지서로 답할 수 있게 한다.
# 이 경로는 네트워크를 쓰지 않는다. 질문 문자열을 정규식으로만 판정하고,
# 기존 _query_data()/_generate_answer()로 SQLite에서 답을 조립한다.
# 따라서 실패 처리 과정에서 고지서 데이터가 Gemini로 전송될 일이 없다.

# 대체 답변 앞에 붙이는 안내. 실패 원인은 드러내지 않는다.
FALLBACK_NOTICE = "저장된 고지서 기준으로 안내합니다."

# 대체 답변을 허용하는 실패 원인: 일시적이거나 키가 아직 없는 상황.
# 401·403(인증·권한 설정 오류)과 404(모델 설정 오류)는 숨기지 않고 원래 안내를 보여준다.
_FALLBACK_REASONS = frozenset(
    {
        gemini_client.REASON_MISSING_KEY,  # API 키 누락
        gemini_client.REASON_RATE_LIMIT,   # 429
        gemini_client.REASON_BUSY,         # 408·500·502·503·504 (재시도 소진)
        gemini_client.REASON_TIMEOUT,      # 타임아웃·연결 실패
    }
)

# 로컬 판정 규칙. 위에서부터 먼저 맞는 규칙을 쓴다.
# 한계: 아래 표현과 그 변형만 잡는다. 맞지 않는 질문은 대체 답변을 만들지 않는다.
_FALLBACK_RULES = (
    # 선택 고지서 요약 — 기간·목록 표현이 없을 때만
    ("bill_summary", re.compile(r"요약|이\s*고지서.*(정보|알려|뭐|어떤)")),
    ("this_week", re.compile(r"이번\s*주|금주")),
    ("monthly_total", re.compile(r"이번\s*달|이달|금월")),
    ("nearest_due", re.compile(r"가장\s*(가까운|급한|빠른)|제일\s*(가까운|급한|빠른)|임박")),
    ("list_all", re.compile(r"전체|전부|모두|모든|목록|리스트")),
)


def _local_fallback_intent(question: str) -> str | None:
    """질문을 로컬 규칙으로만 판정한다. 대상이 아니면 None. (외부 호출 없음)

    납부 의무 질문('꼭 내야 해?' 등)은 저장된 정보로 판단할 수 없으므로
    대체 답변 대상에서 제외한다.
    """
    if _OBLIGATION_RE.search(question):
        return None
    for intent, pattern in _FALLBACK_RULES:
        if not pattern.search(question):
            continue
        if intent == "bill_summary" and _SCHEDULE_SCOPE_RE.search(question):
            continue  # '이번 달 요약'은 선택 고지서 요약이 아니다.
        return intent
    return None


def ask_with_fallback(question: str, *, selected_bill_id=None) -> str:
    """ask()와 같지만, 일부 API 실패에서는 저장된 고지서로 대신 답한다.

    대체 답변 조건 (모두 만족해야 함):
    - 실패 원인이 키 누락·429·408/5xx 혼잡·타임아웃/연결 실패
      (401·403 인증·권한 오류와 404는 대상이 아니다)
    - 질문이 이번 주 일정·이번 달 합계·가장 가까운 납부기한·전체 목록·
      선택 고지서 요약 중 하나로 로컬에서 판정됨

    조건에 맞지 않으면 원래 GeminiError를 그대로 다시 발생시킨다.
    ask()의 동작(오류 전파)은 바꾸지 않는다.

    Raises:
        GeminiError / GeminiBusyError: 대체 답변 조건에 해당하지 않는 실패.
    """
    try:
        return ask(question, selected_bill_id=selected_bill_id)
    except gemini_client.GeminiError as error:
        if error.reason not in _FALLBACK_REASONS:
            raise
        intent = _local_fallback_intent(question or "")
        if intent is None:
            raise
        data = _query_data(intent, selected_bill_id=selected_bill_id)
        answer = _generate_answer(intent, data)
        return f"{FALLBACK_NOTICE}\n\n{answer}"
