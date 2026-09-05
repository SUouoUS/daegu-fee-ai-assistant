"""
app.py
고지서 AI 비서 – PDF/텍스트 입력 → notice_text 생성.
기능 검증용 최소 화면. UI 디자인은 이후 단계에서 담당.
"""

import hashlib

import streamlit as st
from services.parser import extract_text_from_pdf, normalize_notice_text, pdf_info
from services.extractor import extract_notice_info

# ── 페이지 설정 ──────────────────────────────────────────────
st.set_page_config(page_title="고지서 AI 비서", page_icon="📄")

# ── session_state 초기화 ─────────────────────────────────────
_DEFAULTS = {
    "notice_text": "",
    "notice_info": None,        # 추출된 AI 정보 (dict)
    "pdf_file_id": None,        # 현재 PDF 식별용 (이름+크기+해시)
    "pdf_needs_pw": False,      # 비밀번호 필요 여부
    "pdf_authenticated": False, # 인증 성공 여부
    "pdf_password": None,       # 인증된 비밀번호 (세션 메모리에서만)
    "pdf_page_count": None,     # 전체 페이지 수
    "pdf_page_num": 1,          # 선택된 페이지 번호 (1-based)
}
for key, default in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default


def _clear_all():
    """입력 방식 변경 또는 PDF 교체 시 모든 PDF 상태와 결과를 초기화."""
    for key, default in _DEFAULTS.items():
        st.session_state[key] = default


def _clear_result():
    """텍스트 직접 입력 변경 시 결과만 초기화."""
    st.session_state["notice_text"] = ""
    st.session_state["notice_info"] = None


def _on_page_change():
    """분석 페이지 변경 시 이전 결과만 초기화 (인증 상태 유지)."""
    st.session_state["notice_text"] = ""
    st.session_state["notice_info"] = None


def _get_file_id(uploaded_file) -> str:
    """업로드된 파일의 고유 식별자를 생성한다. (이름+크기+내용 해시)"""
    file_bytes = uploaded_file.getvalue()
    content_hash = hashlib.md5(file_bytes).hexdigest()
    return f"{uploaded_file.name}|{len(file_bytes)}|{content_hash}"


# ── 헤더 ─────────────────────────────────────────────────────
st.title("고지서 AI 비서")
st.write("고지서 PDF를 올리거나 내용을 직접 입력해 주세요.")

# ── 입력 방식 선택 ───────────────────────────────────────────
MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

input_method = st.radio(
    "입력 방식을 선택하세요",
    ("PDF 업로드", "텍스트 직접 입력"),
    horizontal=True,
    on_change=_clear_all,
)

# ── 입력 위젯 ────────────────────────────────────────────────
PLACEHOLDER_TEXT = (
    "[예시] 대구광역시 수성구\n"
    "상하수도 사용료 납부 고지서\n"
    "납부자: 홍길동\n"
    "부과금액: 45,200원\n"
    "납부기한: 2026-09-30\n"
    "가상계좌: 000-0000-0000-00"
)

if input_method == "PDF 업로드":
    uploaded_file = st.file_uploader(
        "PDF 파일을 선택하세요 (최대 10 MB)",
        type=["pdf"],
    )

    # ── PDF 파일 변경 감지 ────────────────────────────────────
    if uploaded_file is None:
        # 파일 삭제됨
        if st.session_state["pdf_file_id"] is not None:
            _clear_all()
    else:
        current_id = _get_file_id(uploaded_file)
        if current_id != st.session_state["pdf_file_id"]:
            # 새 파일 또는 다른 파일
            _clear_all()
            st.session_state["pdf_file_id"] = current_id

            # 파일 크기 확인
            file_bytes = uploaded_file.getvalue()
            if len(file_bytes) > MAX_FILE_SIZE_BYTES:
                st.warning(
                    f"파일 크기가 {MAX_FILE_SIZE_MB} MB를 초과합니다. "
                    "더 작은 파일을 업로드해 주세요."
                )
            else:
                # PDF 메타데이터 확인
                try:
                    info = pdf_info(file_bytes)
                    st.session_state["pdf_needs_pw"] = info["needs_password"]
                    if not info["needs_password"]:
                        st.session_state["pdf_authenticated"] = True
                        st.session_state["pdf_page_count"] = info["page_count"]
                except ValueError as e:
                    st.error(str(e))

    # ── 비밀번호 입력 (필요한 경우) ──────────────────────────
    if (
        uploaded_file is not None
        and st.session_state["pdf_needs_pw"]
        and not st.session_state["pdf_authenticated"]
    ):
        st.info("🔒 이 PDF는 비밀번호가 필요합니다.")
        pw_input = st.text_input(
            "PDF 비밀번호",
            type="password",
            key="pw_input_widget",
        )
        if st.button("PDF 열기"):
            if not pw_input:
                st.warning("비밀번호를 입력해 주세요.")
            else:
                file_bytes = uploaded_file.getvalue()
                try:
                    info = pdf_info(file_bytes, password=pw_input)
                    if info["authenticated"]:
                        st.session_state["pdf_authenticated"] = True
                        st.session_state["pdf_password"] = pw_input
                        st.session_state["pdf_page_count"] = info["page_count"]
                        st.rerun()
                    else:
                        st.error("비밀번호가 올바르지 않습니다. 다시 확인해 주세요.")
                except ValueError as e:
                    st.error(str(e))

    # ── 인증 완료 후: 페이지 선택 + 텍스트 확인 ──────────────
    if uploaded_file is not None and st.session_state["pdf_authenticated"]:
        page_count = st.session_state["pdf_page_count"]

        if st.session_state["pdf_needs_pw"]:
            st.success("🔓 PDF 비밀번호 인증 완료")

        # 페이지 정보 표시
        page_num = st.session_state["pdf_page_num"]
        st.write(f"📄 총 **{page_count}페이지** · **{page_num}페이지** 분석")

        # 여러 페이지인 경우만 페이지 선택 UI
        if page_count > 1:
            with st.expander("다른 페이지 선택"):
                st.caption(
                    "첫 페이지에서 필요한 정보가 확인되지 않으면 "
                    "다른 페이지를 선택해 보세요."
                )
                new_page = st.number_input(
                    "분석할 페이지",
                    min_value=1,
                    max_value=page_count,
                    value=st.session_state["pdf_page_num"],
                    step=1,
                    key="page_selector",
                    on_change=_on_page_change,
                )
                # number_input 변경 반영
                if new_page != st.session_state["pdf_page_num"]:
                    st.session_state["pdf_page_num"] = new_page
                    st.session_state["notice_text"] = ""

        # 텍스트 확인 버튼
        if st.button("텍스트 확인"):
            st.session_state["notice_text"] = ""
            file_bytes = uploaded_file.getvalue()
            page_index = st.session_state["pdf_page_num"] - 1
            pw = st.session_state["pdf_password"]

            with st.spinner("PDF에서 텍스트를 추출하는 중…"):
                try:
                    notice_text = extract_text_from_pdf(
                        file_bytes, password=pw, page_index=page_index
                    )
                    st.session_state["notice_text"] = notice_text
                except ValueError as e:
                    st.error(str(e))
                except Exception:
                    st.error(
                        "PDF 처리 중 예상치 못한 오류가 발생했습니다. "
                        "파일을 확인한 후 다시 시도해 주세요."
                    )

    elif uploaded_file is None:
        # 파일 미선택 상태에서 안내 없음 (기존 동작 유지)
        pass

else:  # 텍스트 직접 입력
    raw_text = st.text_area(
        "고지서 내용을 입력하세요",
        height=250,
        placeholder=PLACEHOLDER_TEXT,
        on_change=_clear_result,
    )

    if st.button("텍스트 확인"):
        st.session_state["notice_text"] = ""
        stripped = raw_text.strip()
        if not stripped:
            st.warning("고지서 내용을 입력해 주세요.")
        else:
            with st.spinner("텍스트를 처리하는 중…"):
                notice_text = normalize_notice_text(stripped)
                st.session_state["notice_text"] = notice_text

# ── 결과 표시 ────────────────────────────────────────────────
if st.session_state["notice_text"]:
    st.success("텍스트 추출 완료!")
    st.write(f"**문자 수:** {len(st.session_state['notice_text'])}자")
    st.text_area(
        "추출된 고지서 텍스트 (notice_text)",
        value=st.session_state["notice_text"],
        height=300,
        disabled=True,
    )
    
    st.divider()
    st.subheader("AI 정보 추출")
    st.caption("분석 시 추출된 텍스트가 Google Gemini API로 전송됩니다.")
    
    if st.button("AI로 고지서 분석"):
        st.session_state["notice_info"] = None
        with st.spinner("AI가 고지서 정보를 분석 중입니다..."):
            try:
                info = extract_notice_info(st.session_state["notice_text"])
                st.session_state["notice_info"] = info
            except ValueError as e:
                st.error(str(e))
            except Exception as e:
                st.error("알 수 없는 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")

if st.session_state["notice_info"]:
    st.success("AI 분석 완료!")
    st.caption("⚠️ AI 추출 결과이므로 원문과 확인해 주세요.")
    st.caption("ℹ️ '미납' 상태는 초기 관리 상태이며 실제 납부 여부 확인 결과가 아닙니다.")
    
    # 누락된 항목을 한국어로 표시하기 위해 필터링 (null인 필드가 있는지 확인용)
    missing_fields = []
    field_names = {
        "title": "고지서명",
        "agency": "발급 기관",
        "amount": "납부 금액",
        "due_date": "납부 기한",
        "payment_method": "납부 방법"
    }
    
    for key, name in field_names.items():
        if st.session_state["notice_info"].get(key) is None:
            missing_fields.append(name)
            
    if missing_fields:
        st.info(f"다음 항목은 확인되지 않았습니다: {', '.join(missing_fields)}")

    st.json(st.session_state["notice_info"])

