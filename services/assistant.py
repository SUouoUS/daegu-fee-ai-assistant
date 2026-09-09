"""
services/assistant.py
AI 비서 — 자연어 질문 → 의도 분류 → DB 조회 → 답변 생성.

모든 DB 조회는 status='미납' 고지서만 대상으로 한다.
"""

import os
import json
from dotenv import load_dotenv

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


def _classify_intent(question: str) -> str:
    """사용자 질문의 의도를 분류한다.

    Returns:
        _INTENT_LABELS의 키 중 하나 (str).
    """
    load_dotenv(override=False)
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "unknown"

    model_id = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return "unknown"

    client = genai.Client(api_key=api_key)

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
            contents=question,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        if response.text:
            result = json.loads(response.text)
            intent = result.get("intent", "unknown")
            if intent in _INTENT_LABELS:
                return intent
    except Exception:
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


def _generate_answer(question: str, intent: str, data: dict) -> str:
    """DB 조회 결과를 바탕으로 자연어 답변을 생성한다."""
    if intent == "unknown":
        return (
            "죄송합니다, 고지서 관련 질문만 답변할 수 있습니다. "
            "예를 들어 '이번 주에 낼 게 있어?', '이번 달 납부 금액은?', "
            "'가장 가까운 납부기한은?' 등으로 질문해 주세요."
        )

    load_dotenv(override=False)
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return _fallback_answer(intent, data)

    model_id = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return _fallback_answer(intent, data)

    client = genai.Client(api_key=api_key)

    system_instruction = (
        "당신은 대구광역시 행정 수수료·과태료 관리 AI 비서입니다.\n"
        "사용자의 질문과 DB 조회 결과(JSON)를 참고하여 친절하고 간결하게 답변하세요.\n"
        "금액은 원 단위로 쉼표 포함하여 표시하세요 (예: 45,200원).\n"
        "날짜는 YYYY-MM-DD 형식 그대로 사용하세요.\n"
        "DB에 데이터가 없으면 '현재 등록된 미납 고지서가 없습니다'라고 답하세요.\n"
        "추측이나 법률 판단을 하지 마세요."
    )

    user_content = (
        f"[사용자 질문]\n{question}\n\n"
        f"[DB 조회 결과]\n{json.dumps(data, ensure_ascii=False, default=str)}"
    )

    try:
        response = client.models.generate_content(
            model=model_id,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.3,
            ),
        )
        if response.text:
            return response.text
    except Exception:
        pass

    return _fallback_answer(intent, data)


def _fallback_answer(intent: str, data: dict) -> str:
    """API 없이도 기본적인 답변을 제공하는 폴백 함수."""
    if intent in ("this_week", "this_month", "list_all"):
        bills = data.get("bills", [])
        if not bills:
            return "현재 등록된 미납 고지서가 없습니다."
        lines = []
        for b in bills:
            amount = f"{b['amount']:,}원" if b.get("amount") else "금액 미확인"
            due = b.get("due_date", "기한 미확인")
            title = b.get("title", "제목 없음")
            lines.append(f"• {title} — {amount} (기한: {due})")
        return "\n".join(lines)

    elif intent == "monthly_total":
        total = data.get("total_amount", 0)
        count = data.get("count", 0)
        if count == 0:
            return "이번 달 납부 예정 고지서가 없습니다."
        return f"이번 달 납부 예정 금액: {total:,}원 ({count}건)"

    elif intent == "nearest_due":
        bill = data.get("bill")
        if not bill:
            return "현재 납부 기한이 다가오는 미납 고지서가 없습니다."
        amount = f"{bill['amount']:,}원" if bill.get("amount") else "금액 미확인"
        return (
            f"가장 가까운 납부 기한: {bill.get('due_date', '미확인')}\n"
            f"• {bill.get('title', '제목 없음')} — {amount}"
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
    answer = _generate_answer(question, intent, data)
    return answer
