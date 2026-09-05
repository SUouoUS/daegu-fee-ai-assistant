"""
services/parser.py
고지서 PDF 텍스트 추출 및 정규화 함수.
Streamlit에 의존하지 않으며, 어디서든 재사용 가능.
"""

import re
import pymupdf  # PyMuPDF


def normalize_notice_text(text: str) -> str:
    """고지서 텍스트를 정규화한다.

    - 줄바꿈 형식 통일 (\r\n → \n)
    - 줄 끝 불필요한 공백 제거
    - 과도한 연속 빈 줄을 최대 1줄로 정리
    - 문자열 앞뒤 공백 제거
    - 한글, 숫자, 금액 기호, 날짜, 계좌번호, 의미 있는 줄바꿈 보존
    """
    # \r\n → \n 통일
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 각 줄 끝의 공백 제거
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    # 연속 빈 줄 3개 이상 → 빈 줄 1개로
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 전체 앞뒤 공백 제거
    text = text.strip()
    return text


def pdf_info(
    pdf_bytes: bytes,
    password: str | None = None,
) -> dict:
    """PDF의 메타데이터를 반환한다.

    Returns:
        {
            "needs_password": bool,   # 열기 비밀번호가 필요한지
            "authenticated": bool,    # 비밀번호 인증 성공 여부
            "page_count": int | None, # 인증 후 접근 가능한 페이지 수
        }

    Raises:
        ValueError: 빈 입력, 손상된 PDF 등.
    """
    if not pdf_bytes:
        raise ValueError("PDF 데이터가 비어 있습니다.")

    doc = None
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(
            "유효한 PDF 파일이 아니거나 손상된 파일입니다."
        ) from e

    try:
        needs_password = bool(doc.needs_pass)

        if not needs_password:
            return {
                "needs_password": False,
                "authenticated": True,
                "page_count": doc.page_count,
            }

        # 비밀번호가 필요한 경우
        if password is None:
            return {
                "needs_password": True,
                "authenticated": False,
                "page_count": None,
            }

        auth_result = doc.authenticate(password)
        if auth_result == 0:
            return {
                "needs_password": True,
                "authenticated": False,
                "page_count": None,
            }

        return {
            "needs_password": True,
            "authenticated": True,
            "page_count": doc.page_count,
        }
    finally:
        if doc:
            doc.close()


def extract_text_from_pdf(
    pdf_bytes: bytes,
    password: str | None = None,
    page_index: int = 0,
) -> str:
    """업로드된 PDF의 bytes에서 선택한 페이지의 텍스트를 추출한다.

    Args:
        pdf_bytes: PDF 파일의 바이트 데이터.
        password: 열기 비밀번호. 필요 없으면 None.
        page_index: 추출할 페이지의 0-based 인덱스. 기본값 0(첫 페이지).

    Returns:
        정규화된 텍스트 문자열.

    Raises:
        ValueError: 빈 입력, 손상된 PDF, 비밀번호 관련, 페이지 범위 초과, 텍스트 없음.
    """
    if not pdf_bytes:
        raise ValueError("PDF 데이터가 비어 있습니다.")

    doc = None
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(
            "유효한 PDF 파일이 아니거나 손상된 파일입니다."
        ) from e

    try:
        # 비밀번호 처리
        if doc.needs_pass:
            if password is None:
                raise ValueError("이 PDF는 비밀번호가 필요합니다.")
            auth_result = doc.authenticate(password)
            if auth_result == 0:
                raise ValueError("비밀번호가 올바르지 않습니다. 다시 확인해 주세요.")

        # 페이지 범위 확인
        total_pages = doc.page_count
        if page_index < 0 or page_index >= total_pages:
            raise ValueError(
                f"유효하지 않은 페이지 번호입니다. "
                f"이 PDF는 총 {total_pages}페이지입니다. "
                f"(요청: {page_index + 1}페이지)"
            )

        # 선택된 한 페이지만 추출
        page = doc[page_index]
        # sort=True: 읽기 순서(위→아래, 왼→오)를 고려한 텍스트 추출
        page_text = page.get_text("text", sort=True)
        result = normalize_notice_text(page_text) if page_text else ""

        if not result:
            raise ValueError(
                "선택한 페이지에서 텍스트를 찾지 못했습니다. "
                "이미지 또는 스캔 페이지일 수 있습니다. "
                "다른 페이지를 선택하거나 "
                "'텍스트 직접 입력'을 이용해 주세요."
            )

        return result
    finally:
        if doc:
            doc.close()
