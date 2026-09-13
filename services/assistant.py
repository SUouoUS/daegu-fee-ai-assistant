"""
services/assistant.py
AI 비서 — 자연어 질문 → 의도 분류 → DB 조회 → 답변 생성.

모든 DB 조회는 status='미납' 고지서만 대상으로 한다.

[외부 전송 범위]
Gemini API를 호출하는 지점은 _classify_intent() 하나뿐이며, 전송하는 내용은
사용자가 입력한 질문 텍스트뿐이다. DB 조회·금액 합산·날짜 계산·답변 문장 생성은
모두 로컬에서 수행하므로 고지서명·발급 기관·금액·납부기한·납부방법은
외부로 전송되지 않는다. (test_assistant_no_bill_data.py에서 검증)
"""

import json
import datetime

from services import gemini_client
from database.db import (
    get_bills,
    get_bills_due_this_week,
    get_bills_due_this_month,
    get_total_amount_this_month,
    get_nearest_due_bill,
)


# ── 의도 분류 스키마 ──────────────────────────────────────────

_INTENT_LABELS = {
    "this_week": "이번 주 납부 기한 고지서 조회",
    "this_month": "이번 달 납부 기한 고지서 조회",
    "monthly_total": "이번 달 총 납부 예정 금액",
    "nearest_due": "가장 가까운 납부 기한 고지서",
    "list_all": "미납 고지서 전체 목록",
    "unknown": "고지서와 관련 없는 질문 또는 파악 불가",
}


_UNKNOWN_ANSWER = (
    "죄송합니다, 고지서 관련 질문만 답변할 수 있습니다. "
    "예를 들어 '이번 주에 낼 게 있어?', '이번 달 납부 금액은?', "
    "'가장 가까운 납부기한은?' 등으로 질문해 주세요."
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

    system_instruction = (
        "당신은 고지서 관리 시스템의 의도 분류기입니다.\n"
        "사용자의 질문을 읽고, 아래 의도 중 하나를 선택하세요.\n"
        "반드시 JSON 형식으로 intent 키만 반환하세요.\n\n"
        f"[의도 목록]\n{intent_descriptions}\n\n"
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
        raise gemini_client.GeminiBusyError(BUSY_MESSAGE)

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


def _query_data(intent: str) -> dict:
    """의도에 맞는 DB 조회를 수행하고 결과를 dict로 반환한다.

    모든 조회는 include_paid=False (미납 고지서만 대상).
    """
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
    return _compose_answer(intent, data)


# ── 규칙 기반 답변 조립 (로컬 전용) ───────────────────────────


def _format_amount(value) -> str:
    """금액을 '1,234원' 형식으로 만든다. 값이 없으면 '금액 미확인'."""
    # 0원도 유효한 금액이므로 falsy 검사가 아닌 None 비교를 사용한다.
    if value is None:
        return "금액 미확인"
    return f"{value:,}원"


def _bill_line(bill: dict) -> str:
    """고지서 한 건을 불릿 한 줄로 만든다."""
    title = bill.get("title") or "제목 없음"
    amount = _format_amount(bill.get("amount"))
    due = bill.get("due_date") or "기한 미확인"
    return f"• {title} — {amount} (기한: {due})"


def _sum_amounts(bills: list) -> int:
    """금액이 있는 고지서의 합계를 구한다."""
    return sum(b.get("amount") or 0 for b in bills)


def _days_left(due_date: str):
    """납부기한까지 남은 일수. 파싱 실패 시 None."""
    try:
        due = datetime.date.fromisoformat(due_date)
    except (TypeError, ValueError):
        return None
    return (due - datetime.date.today()).days


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


def ask(question: str) -> str:
    """사용자 질문에 대한 AI 비서 답변을 반환한다.

    흐름: 질문 → 의도 분류 → DB 조회(미납만) → 답변 생성

    Args:
        question: 사용자의 자연어 질문.

    Returns:
        AI 비서의 답변 문자열.
    """
    if not question or not question.strip():
        return "질문을 입력해 주세요."

    intent = _classify_intent(question)
    data = _query_data(intent)
    answer = _generate_answer(intent, data)
    return answer
