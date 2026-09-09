"""
app.py
고지서 AI 비서 – 차분하고 정돈된 반응형 웹 대시보드 리뉴얼
기존 백엔드, Gemini API, PDF 파싱, SQLite 동작 100% 보존
"""

import datetime
import hashlib
import html
import os
import streamlit as st

from services.parser import extract_text_from_pdf, normalize_notice_text, pdf_info
from services.extractor import extract_notice_info
from services.assistant import ask as assistant_ask
from database.db import (
    init_db,
    save_bill,
    get_bills,
    get_nearest_due_bill,
    mark_as_paid,
    mark_as_unpaid,
    delete_bill,
)

# ── DB 초기화 (앱 시작 시 1회) ────────────────────────────────
init_db()

# ── 페이지 설정 ──────────────────────────────────────────────
st.set_page_config(
    page_title="고지서 AI 비서",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS 로드 ──────────────────────────────────────────────────
def load_css():
    css_path = os.path.join(os.path.dirname(__file__), "assets", "styles.css")
    if os.path.exists(css_path):
        try:
            with open(css_path, "r", encoding="utf-8") as f:
                css_content = f.read()
            st.markdown(f"<style>{css_content}</style>", unsafe_allow_html=True)
        except Exception:
            pass

load_css()

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
    "chat_messages": [],        # AI 비서 채팅 이력
    "current_view": "dashboard",# "dashboard" | "register"
    "selected_bill_id": None,   # 선택된 고지서 ID
    "show_paid": False,         # 납부완료 고지서 포함 여부
}

for key, default in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default


def _clear_all():
    """입력 방식 변경 또는 PDF 교체 시 모든 PDF 상태와 결과를 초기화."""
    for key, default in _DEFAULTS.items():
        if key in ("chat_messages", "current_view", "selected_bill_id", "show_paid"):
            continue  # 글로벌 설정 및 이력은 유지
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


# ── 날짜 및 상태 계산 헬퍼 ───────────────────────────────────
def calculate_status(bill: dict) -> tuple[str, str, str]:
    """고지서 상태 문자열, 배지 CSS 클래스, D-day 텍스트를 계산한다.
    규칙:
    - 완료 상태: 완료
    - 기한이 지난 미납: 기한 지남
    - 오늘 마감: 오늘 마감
    - 1~3일 남음: N일 남음
    - 그 외: 미납
    """
    if bill.get("status") == "납부완료":
        return "완료", "status-badge paid", "완료"
    due_date_str = bill.get("due_date")
    if not due_date_str:
        return "미납", "status-badge unpaid", "미납"
    try:
        due_date = datetime.date.fromisoformat(due_date_str.strip())
        today = datetime.date.today()
        days_left = (due_date - today).days

        if days_left < 0:
            return "기한 지남", "status-badge urgent", "기한 지남"
        elif days_left == 0:
            return "오늘 마감", "status-badge urgent", "오늘 마감"
        elif 1 <= days_left <= 3:
            return f"{days_left}일 남음", "status-badge urgent", f"{days_left}일 남음"
        else:
            return "미납", "status-badge unpaid", "미납"
    except Exception:
        return "미납", "status-badge unpaid", "미납"


# ── UI 렌더링 함수 ───────────────────────────────────────────

def render_header():
    """상단 공통 서비스 헤더"""
    col_left, col_right = st.columns([3, 1])
    with col_left:
        st.markdown(
            """
            <div class="gov-header-left">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#1F4E79" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink: 0;">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                    <polyline points="14 2 14 8 20 8"></polyline>
                    <line x1="16" y1="13" x2="8" y2="13"></line>
                    <line x1="16" y1="17" x2="8" y2="17"></line>
                    <polyline points="10 9 9 9 8 9"></polyline>
                </svg>
                <div class="gov-header-title-group">
                    <h1 class="gov-header-title">고지서 AI 비서</h1>
                    <p class="gov-header-subtitle">행정 고지서를 한곳에서 간편하게 관리하세요.</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col_right:
        if st.session_state["current_view"] == "dashboard":
            if st.button("+ 고지서 추가", type="primary", use_container_width=True):
                st.session_state["current_view"] = "register"
                st.rerun()


def render_nearest_notice():
    """가장 가까운 납부기한 요약 카드"""
    nearest = get_nearest_due_bill(include_paid=False)
    if not nearest:
        st.markdown(
            """
            <div class="nearest-card">
                <div class="nearest-label">가장 가까운 납부기한</div>
                <div style="color: #667085; font-size: 15px;">예정된 미납 고지서가 없습니다.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    title = html.escape(nearest.get("title") or "고지서명 미확인")
    amount = nearest.get("amount")
    amount_str = f"{amount:,}원" if amount is not None else "금액 미확인"
    due_date = html.escape(nearest.get("due_date") or "미확인")
    status_text, badge_cls, dday_text = calculate_status(nearest)

    st.markdown(
        f"""
        <div class="nearest-card">
            <div style="display: flex; flex-direction: column; gap: 4px;">
                <div class="nearest-label">가장 가까운 납부기한</div>
                <div class="nearest-title">{title}</div>
            </div>
            <div class="nearest-details">
                <div class="nearest-amount">{amount_str}</div>
                <div class="nearest-date">{due_date}</div>
                <span class="{badge_cls}">{dday_text}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_notice_list(bills: list[dict]):
    """저장된 고지서 목록 카드/테이블 렌더링"""
    with st.container(border=True):
        col_t1, col_t2 = st.columns([3, 1])
        with col_t1:
            st.markdown(
                f'<div class="card-section-title"><span>고지서 목록</span> <span style="font-size: 14px; font-weight: 500; color: #667085;">(총 {len(bills)}건)</span></div>',
                unsafe_allow_html=True,
            )
        with col_t2:
            show_paid_val = st.checkbox("납부완료 고지서도 보기", value=st.session_state["show_paid"], key="chk_show_paid")
            if show_paid_val != st.session_state["show_paid"]:
                st.session_state["show_paid"] = show_paid_val
                st.rerun()

        if not bills:
            if st.session_state["show_paid"]:
                st.info("저장된 고지서가 없습니다. '고지서 추가' 버튼을 눌러 고지서를 신규 등록해 보세요.")
            else:
                st.info("미납 고지서가 없습니다. 완료된 고지서를 확인하려면 상단 체크박스를 선택해 주세요.")
            return

    # 헤더 행
    st.markdown(
        """
        <div style="display: grid; grid-template-columns: 2.5fr 2fr 1.5fr 1.5fr 1fr 1fr; padding: 10px 16px; background-color: #F8FAFC; border-radius: 6px; font-weight: 600; font-size: 13px; color: #667085; margin-bottom: 8px; align-items: center;">
            <div>고지서명</div>
            <div>발급 기관</div>
            <div>금액</div>
            <div>납부기한</div>
            <div>상태</div>
            <div style="text-align: center;">선택</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 행 단위 표시
    for bill in bills:
        bill_id = bill["id"]
        title = html.escape(bill.get("title") or "제목 없음")
        agency = html.escape(bill.get("agency") or "미확인")
        amount = bill.get("amount")
        amount_str = f"{amount:,}원" if amount is not None else "금액 미확인"
        due_date = html.escape(bill.get("due_date") or "미확인")
        status_text, badge_cls, dday_text = calculate_status(bill)

        is_selected = (st.session_state["selected_bill_id"] == bill_id)
        highlight_style = "background-color: #F0F5FF; border-left: 3px solid #2563EB; padding: 0 10px; min-height: 64px; display: flex; align-items: center; border-bottom: 1px solid #E4E7EC;" if is_selected else "padding: 0 10px; min-height: 64px; display: flex; align-items: center; border-bottom: 1px solid #E4E7EC;"

        st.markdown(f'<div style="{highlight_style}">', unsafe_allow_html=True)
        c1, c2, c3, c4, c5, c6 = st.columns([2.5, 2, 1.5, 1.5, 1, 1])
        with c1:
            if bill.get("status") == "납부완료":
                st.markdown(f"~~{title}~~")
            else:
                st.markdown(f"**{title}**")
        with c2:
            st.caption(agency)
        with c3:
            st.write(amount_str)
        with c4:
            st.caption(due_date)
        with c5:
            st.markdown(f'<span class="{badge_cls}">{dday_text}</span>', unsafe_allow_html=True)
        with c6:
            btn_label = "선택됨" if is_selected else "선택"
            if st.button(btn_label, key=f"sel_btn_{bill_id}", use_container_width=True):
                st.session_state["selected_bill_id"] = bill_id
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)


def render_notice_detail(bills: list[dict]):
    """선택한 고지서 상세정보 및 액션 버튼 카드"""
    selected_id = st.session_state.get("selected_bill_id")
    if not selected_id:
        return

    selected_bill = next((b for b in bills if b["id"] == selected_id), None)
    if not selected_bill:
        st.session_state["selected_bill_id"] = None
        return

    st.markdown('<div class="dashboard-card">', unsafe_allow_html=True)
    title = html.escape(selected_bill.get("title") or "확인되지 않음")
    agency = html.escape(selected_bill.get("agency") or "확인되지 않음")
    amount = selected_bill.get("amount")
    amount_str = f"{amount:,}원" if amount is not None else "확인되지 않음"
    due_date = html.escape(selected_bill.get("due_date") or "확인되지 않음")
    payment_method = html.escape(selected_bill.get("payment_method") or "확인되지 않음")
    status_text, badge_cls, dday_text = calculate_status(selected_bill)
    is_paid = selected_bill.get("status") == "납부완료"

    st.markdown(
        f"""
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px;">
            <div style="display: flex; align-items: center; gap: 10px;">
                <h3 style="font-size: 20px; font-weight: 700; margin: 0; color: #182230;">{title}</h3>
                <span class="{badge_cls}">{dday_text}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 2열/Grid 상세 정보
    st.markdown(
        f"""
        <div class="detail-grid">
            <div class="detail-item">
                <div class="detail-item-label">발급 기관</div>
                <div class="detail-item-value">{agency}</div>
            </div>
            <div class="detail-item">
                <div class="detail-item-label">납부 금액</div>
                <div class="detail-item-value">{amount_str}</div>
            </div>
            <div class="detail-item">
                <div class="detail-item-label">납부 기한</div>
                <div class="detail-item-value">{due_date}</div>
            </div>
            <div class="detail-item">
                <div class="detail-item-label">납부 방법</div>
                <div class="detail-item-value">{payment_method}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if is_paid and selected_bill.get("paid_at"):
        st.caption(f"납부 처리 일시: {selected_bill['paid_at']}")

    st.markdown('<div style="height: 12px;"></div>', unsafe_allow_html=True)

    # 액션 버튼 (완료 / 미납 복원 / 삭제)
    col_act1, col_act2 = st.columns([3, 1])
    with col_act1:
        if not is_paid:
            confirm_pay_key = f"confirm_pay_{selected_id}"
            if st.session_state.get(confirm_pay_key):
                st.warning("정말로 납부 완료로 표시할까요?")
                c_y, c_n = st.columns(2)
                with c_y:
                    if st.button("확인", key=f"yes_pay_{selected_id}", type="primary"):
                        result = mark_as_paid(selected_id)
                        if result:
                            st.toast("납부 완료로 표시했습니다.")
                        else:
                            st.error("고지서를 찾을 수 없습니다.")
                        st.session_state[confirm_pay_key] = False
                        st.rerun()
                with c_n:
                    if st.button("취소", key=f"no_pay_{selected_id}"):
                        st.session_state[confirm_pay_key] = False
                        st.rerun()
            else:
                if st.button("납부 완료로 표시", key=f"btn_pay_{selected_id}", type="primary"):
                    st.session_state[confirm_pay_key] = True
                    st.rerun()
        else:
            if st.button("미납으로 되돌리기", key=f"btn_undo_{selected_id}"):
                result = mark_as_unpaid(selected_id)
                if result:
                    st.toast("미납으로 되돌렸습니다.")
                else:
                    st.error("고지서를 찾을 수 없습니다.")
                st.rerun()

    with col_act2:
        confirm_del_key = f"confirm_del_{selected_id}"
        if st.session_state.get(confirm_del_key):
            st.error("정말로 삭제하시겠습니까?")
            c_dy, c_dn = st.columns(2)
            with c_dy:
                if st.button("삭제 확인", key=f"yes_del_{selected_id}", type="primary"):
                    result = delete_bill(selected_id)
                    if result:
                        st.toast("고지서가 삭제되었습니다.")
                        st.session_state["selected_bill_id"] = None
                    else:
                        st.error("고지서를 찾을 수 없습니다.")
                    st.session_state[confirm_del_key] = False
                    st.rerun()
            with c_dn:
                if st.button("취소", key=f"no_del_{selected_id}"):
                    st.session_state[confirm_del_key] = False
                    st.rerun()
        else:
            if st.button("삭제", key=f"btn_del_{selected_id}"):
                st.session_state[confirm_del_key] = True
                st.rerun()

    st.markdown(
        """
        <div style="margin-top: 16px; font-size: 13px; color: #667085;">
            ※ 미납은 초기 관리 상태이며 실제 납부 여부 확인 결과가 아닙니다.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('</div>', unsafe_allow_html=True)


def render_dashboard_view():
    """대시보드 메인 화면"""
    render_header()
    render_nearest_notice()

    bills = get_bills(include_paid=st.session_state["show_paid"])
    render_notice_list(bills)
    render_notice_detail(bills)


# ── AI 비서 다이얼로그 모달 ──────────────────────────────────
@st.dialog("AI 납부 일정 비서", width="large")
def render_assistant_dialog():
    st.caption(
        "저장된 미납 고지서를 바탕으로 질문에 답변합니다. "
        "(예: '이번 주에 낼 게 있어?', '가장 가까운 납부기한은?', '이번 달 총 납부 금액은?')"
    )

    # 채팅 이력 표시
    for msg in st.session_state["chat_messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # 사용자 입력
    if user_input := st.chat_input("질문을 입력하세요…"):
        st.session_state["chat_messages"].append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            with st.spinner("AI가 답변을 생성 중입니다..."):
                try:
                    answer = assistant_ask(user_input)
                except Exception as e:
                    answer = f"답변 생성 중 오류가 발생했습니다: {e}"
            st.markdown(answer)

        st.session_state["chat_messages"].append({"role": "assistant", "content": answer})


# ── 등록 화면 ───────────────────────────────────────────────
def render_register_view():
    """고지서 등록 전용 화면"""
    col_back, col_space = st.columns([1, 4])
    with col_back:
        if st.button("← 목록으로", use_container_width=True):
            st.session_state["current_view"] = "dashboard"
            st.rerun()

    with st.container(border=True):
        st.markdown('<h2 style="font-size: 22px; font-weight: 700; color: #182230; margin: 0 0 8px 0;">신규 고지서 등록</h2>', unsafe_allow_html=True)
        st.markdown('<p style="font-size: 14px; color: #667085; margin-bottom: 24px;">고지서 PDF를 업로드하거나 내용을 직접 입력하면 AI가 정보를 자동으로 추출합니다.</p>', unsafe_allow_html=True)

        MAX_FILE_SIZE_MB = 10
        MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

        input_method = st.radio(
            "입력 방식을 선택하세요",
            ("PDF 업로드", "텍스트 직접 입력"),
            horizontal=True,
            on_change=_clear_all,
        )

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

            if uploaded_file is None:
                if st.session_state["pdf_file_id"] is not None:
                    _clear_all()
            else:
                current_id = _get_file_id(uploaded_file)
                if current_id != st.session_state["pdf_file_id"]:
                    _clear_all()
                    st.session_state["pdf_file_id"] = current_id

                    file_bytes = uploaded_file.getvalue()
                    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
                        st.warning(
                            f"파일 크기가 {MAX_FILE_SIZE_MB} MB를 초과합니다. "
                            "더 작은 파일을 업로드해 주세요."
                        )
                    else:
                        try:
                            info = pdf_info(file_bytes)
                            st.session_state["pdf_needs_pw"] = info["needs_password"]
                            if not info["needs_password"]:
                                st.session_state["pdf_authenticated"] = True
                                st.session_state["pdf_page_count"] = info["page_count"]
                        except ValueError as e:
                            st.error(str(e))

            if (
                uploaded_file is not None
                and st.session_state["pdf_needs_pw"]
                and not st.session_state["pdf_authenticated"]
            ):
                st.info("이 PDF는 비밀번호가 필요합니다.")
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

            if uploaded_file is not None and st.session_state["pdf_authenticated"]:
                page_count = st.session_state["pdf_page_count"]
                if st.session_state["pdf_needs_pw"]:
                    st.success("PDF 비밀번호 인증 완료")

                page_num = st.session_state["pdf_page_num"]
                st.write(f"총 **{page_count}페이지** 중 **{page_num}페이지** 분석 대상")

                if page_count > 1:
                    with st.expander("다른 페이지 선택"):
                        st.caption("첫 페이지에서 필요한 정보가 확인되지 않으면 다른 페이지를 선택해 보세요.")
                        new_page = st.number_input(
                            "분석할 페이지",
                            min_value=1,
                            max_value=page_count,
                            value=st.session_state["pdf_page_num"],
                            step=1,
                            key="page_selector",
                            on_change=_on_page_change,
                        )
                        if new_page != st.session_state["pdf_page_num"]:
                            st.session_state["pdf_page_num"] = new_page
                            st.session_state["notice_text"] = ""

                if st.button("텍스트 확인", type="primary"):
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
                            st.error("PDF 처리 중 예상치 못한 오류가 발생했습니다. 파일을 확인한 후 다시 시도해 주세요.")

        else:  # 텍스트 직접 입력
            raw_text = st.text_area(
                "고지서 내용을 입력하세요",
                height=200,
                placeholder=PLACEHOLDER_TEXT,
                on_change=_clear_result,
            )

            if st.button("텍스트 확인", type="primary"):
                st.session_state["notice_text"] = ""
                stripped = raw_text.strip()
                if not stripped:
                    st.warning("고지서 내용을 입력해 주세요.")
                else:
                    with st.spinner("텍스트를 처리하는 중…"):
                        notice_text = normalize_notice_text(stripped)
                        st.session_state["notice_text"] = notice_text

        # ── 추출 결과 표시 ─────────────────────────────────────────
        if st.session_state["notice_text"]:
            st.success(f"텍스트 추출 완료 (총 {len(st.session_state['notice_text'])}자)")
            with st.expander("추출된 원문 확인", expanded=False):
                st.text_area(
                    "notice_text 원문",
                    value=st.session_state["notice_text"],
                    height=200,
                    disabled=True,
                )

            st.divider()
            st.markdown("### AI 정보 추출")
            st.caption("분석 시 추출된 텍스트가 Google Gemini API로 전송됩니다.")

            if st.button("AI로 고지서 분석", type="primary"):
                st.session_state["notice_info"] = None
                with st.spinner("AI가 고지서 정보를 분석 중입니다..."):
                    try:
                        info = extract_notice_info(st.session_state["notice_text"])
                        st.session_state["notice_info"] = info
                    except ValueError as e:
                        st.error(str(e))
                    except Exception as e:
                        st.error("알 수 없는 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")

        # ── AI 분석 결과 카드 표시 ───────────────────────────────
        if st.session_state["notice_info"]:
            info = st.session_state["notice_info"]
            st.success("AI 분석 완료")
            st.caption("AI 추출 결과이므로 원문과 대조하여 확인해 주세요.")

            missing_fields = []
            field_names = {
                "title": "고지서명",
                "agency": "발급 기관",
                "amount": "납부 금액",
                "due_date": "납부 기한",
                "payment_method": "납부 방법"
            }
            for k, name in field_names.items():
                if info.get(k) is None:
                    missing_fields.append(name)

            if missing_fields:
                st.info(f"다음 항목은 확인되지 않았습니다: {', '.join(missing_fields)}")

            title = html.escape(info.get("title") or "확인되지 않음")
            agency = html.escape(info.get("agency") or "확인되지 않음")
            amount = info.get("amount")
            amount_str = f"{amount:,}원" if amount is not None else "확인되지 않음"
            due_date = html.escape(info.get("due_date") or "확인되지 않음")
            payment_method = html.escape(info.get("payment_method") or "확인되지 않음")

            st.markdown(
                f"""
                <div class="detail-grid">
                    <div class="detail-item">
                        <div class="detail-item-label">고지서명</div>
                        <div class="detail-item-value">{title}</div>
                    </div>
                    <div class="detail-item">
                        <div class="detail-item-label">발급 기관</div>
                        <div class="detail-item-value">{agency}</div>
                    </div>
                    <div class="detail-item">
                        <div class="detail-item-label">납부 금액</div>
                        <div class="detail-item-value">{amount_str}</div>
                    </div>
                    <div class="detail-item">
                        <div class="detail-item-label">납부 기한</div>
                        <div class="detail-item-value">{due_date}</div>
                    </div>
                    <div class="detail-item" style="grid-column: span 2;">
                        <div class="detail-item-label">납부 방법</div>
                        <div class="detail-item-value">{payment_method}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            with st.expander("JSON 원본 보기"):
                st.json(info)

            st.divider()
            if st.button("고지서 저장", type="primary", use_container_width=True):
                try:
                    bill_id = save_bill(st.session_state["notice_info"])
                    st.toast(f"고지서가 성공적으로 저장되었습니다. (ID: {bill_id})")
                    st.session_state["notice_info"] = None
                    st.session_state["notice_text"] = ""
                    st.session_state["selected_bill_id"] = bill_id
                    st.session_state["current_view"] = "dashboard"
                    st.rerun()
                except Exception as e:
                    st.error(f"저장 중 오류가 발생했습니다: {e}")



# ── 메인 컨트롤러 ────────────────────────────────────────────
def main():
    if st.session_state["current_view"] == "dashboard":
        render_dashboard_view()
    elif st.session_state["current_view"] == "register":
        render_register_view()

    # 우측 하단 고정 AI 비서 버튼
    st.markdown(
        """
        <div id="ai-btn-anchor"></div>
        <style>
        /* Anchor hack to target the next element container which holds the button */
        div[data-testid="stVerticalBlock"] > div.element-container:has(#ai-btn-anchor) + div.element-container {
            position: fixed;
            bottom: 24px;
            right: 24px;
            z-index: 99999;
            width: auto !important;
        }
        div[data-testid="stVerticalBlock"] > div.element-container:has(#ai-btn-anchor) + div.element-container button {
            height: 44px !important;
            min-height: 44px !important;
            border-radius: 22px !important;
            padding: 0 24px !important;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1) !important;
        }
        /* Dialog max-width */
        div[role="dialog"] {
            max-width: 640px !important;
            width: 100% !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    if st.button("AI 비서", key="floating_ai_assistant_btn", type="primary"):
        render_assistant_dialog()


if __name__ == "__main__":
    main()
