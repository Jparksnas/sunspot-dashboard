# --------------------------------------------
# 티켓베이 매물 알림
# 10/3(토) 14:00 LG 트윈스 경기, 외야 그린석 4연석 매물 중
# 401 / 402 / 403 구역이면서 1장 가격이 20,000원 이하인 매물이 올라오면
# Gmail로 알림을 보냅니다.
#
# 필요한 환경변수
#   GMAIL_ADDRESS       보내는 Gmail 주소
#   GMAIL_APP_PASSWORD  Gmail 앱 비밀번호(16자리, 일반 비밀번호 아님)
#   NOTIFY_TO           받는 주소 (생략하면 GMAIL_ADDRESS로 보냄)
# --------------------------------------------
import argparse
import hashlib
import json
import os
import random
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

TARGET_URL = os.environ.get("TICKETBAY_URL") or (
    "https://www.ticketbay.co.kr/product/6549/list/0"
    "?start_perform_date=2026-10-03+14%3A00%3A00"
    "&seat_grade=%EC%99%B8%EC%95%BC+%EA%B7%B8%EB%A6%B0%EC%84%9D"
    "&sale_quantity=4&is_together=YES"
)
TARGET_SECTIONS = {"401", "402", "403"}
MAX_PRICE = 20000                         # 1장 기준 최대 가격(원)
KST = timezone(timedelta(hours=9))
STOP_AFTER = datetime(2026, 10, 3, 14, 0, tzinfo=KST)  # 경기 시작 후에는 자동 종료

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "notified.json"
DUMP_DIR = BASE_DIR / "dump"

# 구역 번호: 앞뒤에 숫자/콤마가 붙지 않은 401~403, 뒤에 원·열·번·장이 오면 제외
SECTION_RE = re.compile(r"(?<![\d,.])(40[1-3])(?![\d,.])(?!\s*(?:원|열|번|장))")
PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{4,7})\s*원")
PRICE_KEY_RE = re.compile(r"price|amount|가격", re.I)
UNIT_PRICE_KEY_RE = re.compile(r"unit|per|each|one|장당", re.I)
ID_KEY_RE = re.compile(r"^(id|.*_id|.*Id|.*_no|.*No|seq)$")


def log(msg):
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------- 파싱 ----------------
def parse_text_listing(text):
    """매물 한 건의 화면 텍스트에서 구역과 1장 가격을 뽑아냅니다."""
    sections = sorted(set(SECTION_RE.findall(text)))
    prices = [int(p.replace(",", "")) for p in PRICE_RE.findall(text)]
    prices = [p for p in prices if p >= 1000]
    if not sections or not prices:
        return None
    # 1장 가격과 총액이 같이 보이는 경우가 있어 가장 작은 금액을 1장 가격으로 봅니다.
    return {"sections": sections, "price": min(prices), "text": " ".join(text.split())}


def _flatten_scalars(d, depth=0):
    out = []
    for k, v in d.items():
        if isinstance(v, dict) and depth < 2:
            out.extend(_flatten_scalars(v, depth + 1))
        elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
            out.append((str(k), v))
    return out


def parse_json_listings(obj):
    """API 응답(JSON) 어디에 있든 '가격 + 401~403 구역'을 가진 객체를 찾아냅니다."""
    found = []

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        pairs = _flatten_scalars(node)
        prices = []
        for k, v in pairs:
            if not PRICE_KEY_RE.search(k):
                continue
            if isinstance(v, str):
                v = v.replace(",", "").replace("원", "").strip()
                if not v.isdigit():
                    continue
            if int(v) >= 1000:
                prices.append((k, int(v)))
        seat_text = " ".join(str(v) for k, v in pairs
                             if isinstance(v, str) and not PRICE_KEY_RE.search(k))
        sections = sorted(set(SECTION_RE.findall(seat_text)))
        if prices and sections:
            unit = [p for k, p in prices if UNIT_PRICE_KEY_RE.search(k)]
            ident = next((str(v) for k, v in pairs if ID_KEY_RE.match(k)), "")
            found.append({
                "sections": sections,
                "price": min(unit) if unit else min(p for _, p in prices),
                "text": seat_text[:300],
                "id": ident,
            })
            return  # 이 객체 안쪽은 다시 보지 않음
        for v in node.values():
            if isinstance(v, (dict, list)):
                walk(v)

    walk(obj)
    return found


def is_match(listing):
    return bool(TARGET_SECTIONS & set(listing["sections"])) and listing["price"] <= MAX_PRICE


def listing_key(listing):
    base = listing.get("id") or f'{",".join(listing["sections"])}|{listing["price"]}|{listing["text"]}'
    return hashlib.sha1(base.encode()).hexdigest()[:16]


# ---------------- 페이지 가져오기 ----------------
# 가격(원)이 들어 있는 가장 작은 요소들의 텍스트 = 매물 카드 후보
CARD_TEXT_JS = r"""
() => {
  const priceRe = /\d[\d,]*\s*원/;
  const all = Array.from(document.querySelectorAll('body *'));
  const hits = all.filter(el => {
    const t = (el.innerText || '').trim();
    return t.length > 0 && t.length < 500 && priceRe.test(t) && /\d{3}/.test(t.replace(/[\d,]+\s*원/g, ''));
  });
  // 카드 안쪽 요소들도 후보가 되므로, 부모가 후보가 아닌 가장 바깥 요소만 남깁니다.
  const set = new Set(hits);
  return hits.filter(el => !set.has(el.parentElement)).map(el => el.innerText);
}
"""


def fetch_listings(dump=False):
    from playwright.sync_api import sync_playwright

    json_bodies = []
    with sync_playwright() as p:
        launch_args = {"headless": True}
        if os.environ.get("CHROMIUM_PATH"):
            launch_args["executable_path"] = os.environ["CHROMIUM_PATH"]
        browser = p.chromium.launch(**launch_args)
        page = browser.new_page(locale="ko-KR", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))

        def on_response(resp):
            if "json" in (resp.headers.get("content-type") or ""):
                try:
                    json_bodies.append((resp.url, resp.json()))
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        # 목록이 스크롤로 더 불러와지는 경우를 대비해 몇 번 내려봅니다.
        for _ in range(5):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(800)
        card_texts = page.evaluate(CARD_TEXT_JS)
        body_text = page.inner_text("body")

        if dump:
            DUMP_DIR.mkdir(exist_ok=True)
            (DUMP_DIR / "page.html").write_text(page.content(), encoding="utf-8")
            (DUMP_DIR / "page.txt").write_text(body_text, encoding="utf-8")
            (DUMP_DIR / "cards.json").write_text(
                json.dumps(card_texts, ensure_ascii=False, indent=2), encoding="utf-8")
            (DUMP_DIR / "responses.json").write_text(
                json.dumps([{"url": u, "body": b} for u, b in json_bodies],
                           ensure_ascii=False, indent=2), encoding="utf-8")
            page.screenshot(path=str(DUMP_DIR / "page.png"), full_page=True)
            log(f"디버그 파일 저장: {DUMP_DIR}")
        browser.close()

    listings = []
    for _, body in json_bodies:
        listings.extend(parse_json_listings(body))
    source = "api"
    if not listings:  # API에서 못 찾으면 화면 텍스트로
        source = "화면"
        listings = [l for l in map(parse_text_listing, card_texts) if l]
    return listings, source, body_text


# ---------------- 알림 ----------------
def send_gmail(subject, body):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    to = os.environ.get("NOTIFY_TO") or sender
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.sendmail(sender, [a.strip() for a in to.split(",")], msg.as_string())
    log(f"메일 발송 완료 → {to}")


def load_state():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def save_state(keys):
    STATE_FILE.write_text(json.dumps(sorted(keys)))


def check_once(dump=False):
    listings, source, body_text = fetch_listings(dump=dump)
    matches = [l for l in listings if is_match(l)]
    log(f"매물 후보 {len(listings)}건({source}), 조건 충족 {len(matches)}건")
    if not listings and "로그인" in body_text:
        log("경고: 페이지에 '로그인' 문구가 보입니다. 로그인해야 목록이 보이는지 --dump로 확인해 주세요.")

    notified = load_state()
    new = [l for l in matches if listing_key(l) not in notified]
    if not new:
        return
    lines = [f"- {'/'.join(l['sections'])}구역 · 1장 {l['price']:,}원\n  {l['text'][:200]}" for l in new]
    body = (
        "10/3(토) 14:00 외야 그린석 4연석 조건에 맞는 매물이 올라왔습니다.\n"
        f"(구역 {', '.join(sorted(TARGET_SECTIONS))} · 1장 {MAX_PRICE:,}원 이하)\n\n"
        + "\n".join(lines)
        + f"\n\n바로가기: {TARGET_URL}\n"
    )
    send_gmail(f"[티켓베이] 조건 매물 {len(new)}건 - 10/3 외야 그린석 4연석", body)
    save_state(notified | {listing_key(l) for l in new})


def main():
    ap = argparse.ArgumentParser(description="티켓베이 매물 Gmail 알림")
    ap.add_argument("--interval", type=int, default=60, help="확인 간격(초), 기본 60")
    ap.add_argument("--once", action="store_true", help="한 번만 확인하고 종료")
    ap.add_argument("--dump", action="store_true", help="페이지/응답을 dump/ 폴더에 저장(디버그)")
    ap.add_argument("--max-minutes", type=float, help="이 시간(분)이 지나면 종료 (GitHub Actions용)")
    ap.add_argument("--test-email", action="store_true", help="테스트 메일만 보내고 종료")
    args = ap.parse_args()

    for var in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        if not os.environ.get(var):
            sys.exit(f"환경변수 {var} 가 설정되지 않았습니다. README를 참고하세요.")

    if args.test_email:
        send_gmail("[티켓베이 알림] 테스트 메일", "알림 설정이 정상입니다.\n" + TARGET_URL)
        return

    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None
    while True:
        if datetime.now(KST) >= STOP_AFTER:
            log("경기 시작 시간이 지나 종료합니다.")
            return
        try:
            check_once(dump=args.dump)
        except Exception as e:  # 일시적인 오류로 감시가 멈추지 않도록
            log(f"오류: {e!r}")
        if args.once or (deadline and time.time() + args.interval >= deadline):
            return
        time.sleep(args.interval + random.uniform(0, args.interval * 0.3))


if __name__ == "__main__":
    main()
