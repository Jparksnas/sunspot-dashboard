# --------------------------------------------
# 티켓베이 자동 주문 (무통장 입금 · 일반 가상계좌 발급까지)
#
#   python ticketbay_buy.py login                   # 로그인하고 로그인 상태 저장
#   python ticketbay_buy.py explore --url URL [--clicks "구매하기|..."]
#                                                   # 화면 탐색(주문하지 않음)
#   python ticketbay_buy.py order --url URL         # 가상계좌 발급까지 진행
#
# 필요한 환경변수 (GitHub Secrets)
#   TICKETBAY_ID / TICKETBAY_PASSWORD   티켓베이 아이디(이메일) 로그인
#   또는 KAKAO_ID / KAKAO_PASSWORD      카카오 로그인
#   GMAIL_ADDRESS / GMAIL_APP_PASSWORD  결과 메일 발송
#
# 안전장치
#   - 결제 수단은 '무통장 입금 > 일반 가상계좌'만 고릅니다. 카드·간편결제는 누르지 않습니다.
#   - '입장 안심 서비스'(유료)는 선택하지 않고, 주문서 총 결제 금액이 상품 금액과 다르면 멈춥니다.
#   - 하루 1건만 진행합니다(bought.json).
#   - 저장소가 공개라 실행 로그도 공개됩니다. 로그에는 이메일·전화·계좌번호 등을 가려서 남기고,
#     계좌번호는 메일로만 보냅니다. 로그인 상태는 비밀번호로 암호화해서 저장합니다.
# --------------------------------------------
import argparse
import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from ticketbay_alert import KST, log, send_gmail

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.enc"
BOUGHT_FILE = BASE_DIR / "bought.json"
LOGIN_URL = "https://www.ticketbay.co.kr/member/login"


# ---------------- 공통 ----------------
def redact(text):
    """공개 로그에 남기면 안 되는 개인정보를 가립니다."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[이메일]", text)
    text = re.sub(r"01[016789][-\s]?\d{3,4}[-\s]?\d{4}", "[전화]", text)
    return re.sub(r"\d[\d-]{7,}\d", "[번호]", text)


CLICKABLES_JS = r"""
() => Array.from(document.querySelectorAll(
    'a, button, [role=button], input[type=submit], input[type=button], input[type=radio], input[type=checkbox], label, select'))
  .filter(el => el.offsetParent !== null)
  .map(el => {
    const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
    const extra = (el.type === 'checkbox' || el.type === 'radio') ? ` [${el.checked ? 'V' : ' '}]` : '';
    return `${el.tagName.toLowerCase()}${extra} | ${t.slice(0, 50)}`;
  })
  .filter(s => !/\|\s*$/.test(s) || /\[.\]/.test(s))
"""


def dump(page, step, chars=2500):
    log(f"===== [{step}] {redact(page.url)}")
    text = redact(" ".join(page.inner_text("body").split()))
    print(text[:chars], flush=True)
    items = page.evaluate(CLICKABLES_JS)
    print(f"--- 누를 수 있는 요소 {len(items)}개 ---", flush=True)
    for s in items[:70]:
        print("  " + redact(s), flush=True)


def body_text(page):
    return " ".join(page.inner_text("body").split())


def won(text, label):
    m = re.search(label + r"\s*([\d,]+)\s*원", text)
    return int(m.group(1).replace(",", "")) if m else None


def click_first(page, texts, exact=False):
    """보이는 버튼/링크/글자 중 처음 찾은 것을 누르고, 누른 글자를 돌려줍니다."""
    for t in texts:
        for loc in (page.get_by_role("button", name=t, exact=exact),
                    page.get_by_role("link", name=t, exact=exact),
                    page.get_by_text(t, exact=exact)):
            loc = loc.filter(visible=True)
            if loc.count():
                loc.first.scroll_into_view_if_needed()
                loc.first.click()
                return t
    return None


def settle(page, ms=1500):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


# ---------------- 로그인 상태 저장(암호화) ----------------
def _secret():
    return os.environ.get("TICKETBAY_PASSWORD") or os.environ.get("KAKAO_PASSWORD") or ""


def _fernet():
    from cryptography.fernet import Fernet
    key = hashlib.sha256(("ticketbay-session:" + _secret()).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def load_session():
    try:
        return json.loads(_fernet().decrypt(SESSION_FILE.read_bytes()))
    except Exception:
        return None


def save_session(context):
    SESSION_FILE.write_bytes(_fernet().encrypt(json.dumps(context.storage_state()).encode()))
    log("로그인 상태를 암호화해서 저장했습니다.")


class Browser:
    def __init__(self, p):
        launch = {"headless": True}
        if os.environ.get("CHROMIUM_PATH"):
            launch["executable_path"] = os.environ["CHROMIUM_PATH"]
        self.browser = p.chromium.launch(**launch)
        state = load_session()
        # 사용자가 보는 휴대폰 화면과 같게 엽니다.
        self.context = self.browser.new_context(
            **p.devices["Pixel 7"], locale="ko-KR", storage_state=state)
        self.page = self.context.new_page()
        self.dialogs = []

        def on_dialog(d):
            self.dialogs.append(d.message)
            log(f"알림창: {redact(d.message)}")
            d.accept()

        self.context.on("page", lambda pg: pg.on("dialog", on_dialog))
        self.page.on("dialog", on_dialog)

    def latest_page(self):
        self.page = self.context.pages[-1]
        return self.page

    def close(self):
        self.browser.close()


# ---------------- 로그인 ----------------
def logged_in(page):
    page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)
    # 로그인된 상태면 로그인 페이지에 머물지 않고 다른 곳으로 보내집니다.
    return "/member/login" not in page.url


def login(b):
    page = b.page
    if logged_in(page):
        log("이미 로그인된 상태입니다.")
        return True
    if not _secret():
        log("로그인 정보(TICKETBAY_ID/PASSWORD 또는 KAKAO_ID/PASSWORD)가 없습니다.")
        return False

    if os.environ.get("TICKETBAY_ID"):
        log("티켓베이 아이디로 로그인합니다.")
        page.get_by_placeholder("아이디(이메일) 입력").fill(os.environ["TICKETBAY_ID"])
        page.get_by_placeholder("비밀번호 입력").fill(os.environ["TICKETBAY_PASSWORD"])
        page.get_by_role("button", name="로그인", exact=True).click()
    else:
        log("카카오로 로그인합니다.")
        click_first(page, ["카카오 1초 로그인"])
        page.wait_for_url(re.compile(r"kakao\.com"), timeout=30000)
        settle(page)
        page.locator("input[name=loginId], input[type=email], input[type=text]").first.fill(os.environ["KAKAO_ID"])
        page.locator("input[name=password], input[type=password]").first.fill(os.environ["KAKAO_PASSWORD"])
        click_first(page, ["로그인"], exact=True)

    # 카카오 추가 인증(카카오톡 승인)이 뜨면 최대 4분 기다립니다.
    host = urlparse(LOGIN_URL).netloc
    asked = False
    for _ in range(80):
        page.wait_for_timeout(3000)
        page = b.latest_page()
        url = page.url
        if host in url and "/member/login" not in url:
            break
        if any(re.search(r"일치하지|잘못|확인해", d) for d in b.dialogs):
            log("아이디/비밀번호가 맞지 않는다는 알림이 떴습니다.")
            return False
        text = body_text(page)
        if "동의하고 계속하기" in text:
            click_first(page, ["동의하고 계속하기"])
            continue
        if not asked and re.search(r"카카오톡.*(확인|인증|승인)|인증번호|2단계", text):
            asked = True
            log("카카오 추가 인증이 필요합니다. 휴대폰에서 승인해 주세요(최대 4분 대기).")
            try:
                send_gmail("[티켓베이 자동주문] 카카오 로그인 승인이 필요합니다",
                           "GitHub 서버에서 카카오 로그인을 시도하고 있습니다.\n"
                           "휴대폰 카카오톡에 온 로그인 요청을 4분 안에 승인해 주세요.")
            except Exception as e:
                log(f"승인 요청 메일 발송 실패: {e!r}")
        if re.search(r"비밀번호.*(일치하지|잘못)|아이디.*(확인|존재하지)", text) and "/member/login" in url:
            break
    else:
        dump(page, "로그인 대기 시간 초과")
        return False

    ok = logged_in(b.page)
    if ok:
        log("로그인 성공")
        save_session(b.context)
    else:
        dump(b.page, "로그인 실패")
    return ok


# ---------------- 주문 ----------------
def bought_today():
    try:
        return json.loads(BOUGHT_FILE.read_text()).get("date") == f"{datetime.now(KST):%Y-%m-%d}"
    except Exception:
        return False


def mark_bought(url):
    BOUGHT_FILE.write_text(json.dumps({"date": f"{datetime.now(KST):%Y-%m-%d}", "url": url}))


ACCOUNT_RE = re.compile(r"(가상계좌|입금\s*계좌|계좌\s*번호)")
DEADLINE_RE = re.compile(r"입금\s*(기한|마감|기간)")


def issued(text):
    return ACCOUNT_RE.search(text) and DEADLINE_RE.search(text) and re.search(r"\d[\d-]{9,}\d", text)


def order(b, url, max_price):
    """상품 페이지 → 구매하기 → 무통장(일반 가상계좌) → 동의 → 다음 → 가상계좌 발급 확인"""
    page = b.page
    page.goto(url, wait_until="networkidle", timeout=60000)
    text = body_text(page)
    unit, total = won(text, "한 매 가격"), won(text, "총 가격")
    log(f"상품 페이지: 1장 {unit}원 · 총 {total}원")
    if not unit or unit > max_price or not total:
        dump(page, "가격 확인 실패 - 중단")
        return None

    if not click_first(page, ["구매하기"], exact=True):
        dump(page, "구매하기 버튼 없음 - 중단")
        return None
    settle(page)
    page = b.latest_page()
    if "/member/login" in page.url or any("로그인" in d for d in b.dialogs):
        log("로그인이 풀려 있습니다. 먼저 login 을 다시 실행해야 합니다.")
        return None

    dump(page, "주문서")
    text = body_text(page)
    if "주문서" not in text and "결제 수단" not in text:
        log("주문서 화면이 아닙니다 - 중단")
        return None

    # 결제 수단: 무통장 입금 > 일반 가상계좌로 입금
    click_first(page, ["무통장 입금"])
    page.wait_for_timeout(700)
    if not click_first(page, ["일반 가상계좌로 입금"]):
        dump(page, "일반 가상계좌 선택 실패 - 중단")
        return None
    page.wait_for_timeout(700)

    # 필수 동의 두 개만 체크합니다(입장 안심 서비스는 건드리지 않음).
    for t in ["주문상품 구매조건 확인", "천재지변"]:
        if not click_first(page, [t]):
            dump(page, f"동의 항목 '{t}' 없음 - 중단")
            return None
        page.wait_for_timeout(400)

    text = body_text(page)
    pay = won(text, "총 결제 금액")
    log(f"주문서 총 결제 금액: {pay}원 (상품 총 가격 {total}원)")
    if pay != total:
        dump(page, "결제 금액이 상품 금액과 다름(유료 서비스 등) - 중단")
        return None

    b.dialogs.clear()
    click_first(page, ["다음"], exact=True)

    # '다음' 이후: 가상계좌 발급 화면이 나올 때까지 확인 버튼만 따라갑니다.
    for step in range(5):
        settle(page, 2500)
        page = b.latest_page()
        text = body_text(page)
        if issued(text):
            return text
        dump(page, f"다음 이후 {step + 1}")
        if b.dialogs and any(re.search(r"동의|선택|입력", d) for d in b.dialogs):
            log("필수 항목이 빠졌다는 알림이 떠서 멈춥니다.")
            return None
        sel = page.locator("select").filter(visible=True)
        if sel.count():  # 은행 선택 등: 비어 있지 않은 첫 항목
            try:
                sel.first.select_option(index=1)
            except Exception:
                pass
        if not click_first(page, ["가상계좌 발급", "발급받기", "결제하기", "주문하기", "확인", "다음"], exact=True):
            break
    dump(b.latest_page(), "가상계좌 화면을 찾지 못함")
    return None


def order_and_notify(url, max_price, label=""):
    """감시 프로그램에서 부르는 진입점. 성공하면 True."""
    from playwright.sync_api import sync_playwright

    if bought_today():
        log("오늘은 이미 가상계좌를 발급받아서 자동 주문을 건너뜁니다.")
        return False
    with sync_playwright() as p:
        b = Browser(p)
        try:
            if not login(b):
                send_gmail("[티켓베이 자동주문] 로그인 실패",
                           f"자동 주문을 하려 했지만 로그인에 실패했습니다.\n직접 구매해 주세요: {url}")
                return False
            text = order(b, url, max_price)
        except Exception as e:
            log(f"자동 주문 중 오류: {e!r}")
            text = None
        finally:
            b.close()
    if not text:
        send_gmail("[티켓베이 자동주문] 진행 실패 - 직접 구매해 주세요",
                   f"{label}\n자동 주문을 끝까지 진행하지 못했습니다(자세한 내용은 GitHub 실행 로그).\n{url}")
        return False
    mark_bought(url)
    m = ACCOUNT_RE.search(text)
    start = max(0, (m.start() if m else 0) - 300)
    send_gmail("💳 [티켓베이 자동주문] 가상계좌 발급 완료 - 입금하세요",
               f"{label}\n상품: {url}\n\n--- 주문 완료 화면 ---\n{text[start:start + 1500]}\n")
    log("가상계좌 발급 완료 - 계좌 정보는 메일로 보냈습니다.")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["login", "explore", "order"])
    ap.add_argument("--url")
    ap.add_argument("--clicks", default="")
    ap.add_argument("--max-price", type=int, default=20000)
    args = ap.parse_args()

    if args.action == "order":
        sys.exit(0 if order_and_notify(args.url, args.max_price, "[수동 실행]") else 1)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = Browser(p)
        try:
            ok = login(b)
            if args.action == "explore":
                b.page.goto(args.url, wait_until="networkidle", timeout=60000)
                dump(b.page, "시작")
                for i, t in enumerate(x.strip() for x in args.clicks.split("|") if x.strip()):
                    if not click_first(b.page, [t]):
                        log(f"'{t}' 를 찾지 못해 멈춥니다.")
                        break
                    settle(b.page)
                    dump(b.latest_page(), f"{i + 1}. '{t}' 누른 뒤")
        finally:
            b.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
