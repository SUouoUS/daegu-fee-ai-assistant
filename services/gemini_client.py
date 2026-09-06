"""
services/gemini_client.py
Gemini API 클라이언트 공용 설정 모듈.
API 키·모델명·재시도 정책과 오류 문구 변환을 extractor와 assistant가 공유한다.
"""

import os

from dotenv import load_dotenv

DEFAULT_MODEL_ID = "gemini-3.6-flash"

# 일시적 혼잡/네트워크 오류로 간주하여 재시도할 HTTP 상태 코드.
# 429는 제외한다: 무료 한도 소진 시 재시도는 성공 가능성이 낮고 요청만 늘어난다.
TRANSIENT_STATUS_CODES = (408, 500, 502, 503, 504)

# 재시도하지 않고 즉시 사용량 초과로 안내할 상태 코드
RATE_LIMIT_STATUS_CODE = 429

# google-genai는 http_options.retry_options를 지정하지 않으면 재시도하지 않으므로
# (기본값 None → stop_after_attempt(1)) SDK 재시도와 중복되지 않는다.
# SDK 재시도를 최초 요청 포함 3회로 설정하고, 대기 시간은 약 1초 → 2초로 증가한다.
MAX_ATTEMPTS = 3
RETRY_INITIAL_DELAY = 1.0
RETRY_EXP_BASE = 2.0


class GeminiError(ValueError):
    """Gemini 호출 관련 사용자 안내용 오류. (기존 ValueError 처리와 호환)"""


class GeminiBusyError(GeminiError):
    """재시도 후에도 실패한 일시적 혼잡·타임아웃·연결 실패 오류."""


def import_sdk():
    """google-genai SDK와 httpx를 import한다.

    Returns:
        (genai, types, APIError, httpx) 튜플.

    Raises:
        GeminiError: 패키지가 설치되지 않은 경우.
    """
    try:
        import httpx
        from google import genai
        from google.genai import types
        from google.genai.errors import APIError
    except ImportError:
        raise GeminiError("google-genai 패키지가 설치되지 않았습니다.")
    return genai, types, APIError, httpx


def load_api_key(purpose: str) -> str:
    """.env에서 GEMINI_API_KEY를 읽는다.

    Args:
        purpose: 오류 문구에 사용할 기능 이름 (예: "AI 정보 추출").

    Raises:
        GeminiError: 키가 없는 경우.
    """
    load_dotenv(override=False)
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise GeminiError(
            "GEMINI_API_KEY가 설정되지 않았습니다. "
            f"{purpose} 기능을 사용하려면 .env 파일에 API 키를 추가해 주세요."
        )
    return api_key


def get_model_id() -> str:
    """사용할 모델 ID를 반환한다."""
    load_dotenv(override=False)
    return os.getenv("GEMINI_MODEL", DEFAULT_MODEL_ID)


def create_client(api_key: str):
    """공통 재시도 정책이 적용된 Gemini 클라이언트를 생성한다.

    일시적 오류(408/429/5xx, 연결 실패·타임아웃)만 SDK가 점진적 대기 후 재시도한다.
    인증 오류(401/403), 모델 없음(404), 잘못된 요청은 재시도 대상이 아니다.
    """
    genai, types, _, _ = import_sdk()
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(
                attempts=MAX_ATTEMPTS,  # 최초 요청 포함 최대 3회
                initial_delay=RETRY_INITIAL_DELAY,
                exp_base=RETRY_EXP_BASE,
                http_status_codes=list(TRANSIENT_STATUS_CODES),
            )
        ),
    )


def raise_for_api_error(error, *, model_id: str, busy_message: str):
    """APIError를 사용자 안내용 오류로 변환해 발생시킨다.

    Args:
        error: google.genai.errors.APIError 인스턴스.
        model_id: 안내 문구에 사용할 모델 ID.
        busy_message: 일시적 오류 소진 시 사용할 문구.

    Raises:
        GeminiBusyError: 일시적 혼잡 오류.
        GeminiError: 그 외 인증·모델·요청 오류.
    """
    err_msg = str(error).lower()
    # 오류 메시지에 키나 본문 등 민감정보가 노출되지 않도록, message를 제한적으로 활용
    e_msg = getattr(error, "message", "")
    code = getattr(error, "code", None)

    if code in (401, 403) or "api key" in err_msg:
        raise GeminiError("API 인증에 실패했습니다. 유효한 API 키인지 확인해 주세요.")
    if code == 404 or "not found" in err_msg:
        raise GeminiError(
            f"모델({model_id})을 찾을 수 없거나 접근이 제한되었습니다. (원인: {e_msg})"
        )
    if code == RATE_LIMIT_STATUS_CODE or "quota" in err_msg:
        # 429는 재시도하지 않고 즉시 안내한다.
        raise GeminiError("API 사용량이 초과되었거나 요청이 제한되었습니다.")
    if code in TRANSIENT_STATUS_CODES:
        # 최대 3회 요청(SDK 재시도 포함) 후에도 계속 실패한 일시적 혼잡 오류
        raise GeminiBusyError(busy_message)
    raise GeminiError(
        f"API 연결 실패 또는 모델에서 오류가 발생했습니다. (원인: {e_msg})"
    )
