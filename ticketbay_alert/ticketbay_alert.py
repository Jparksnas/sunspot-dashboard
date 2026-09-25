# --------------------------------------------
# 티켓베이 매물 알림
# LG 트윈스 경기(기본 10/3(토) 14:00, --game 으로 변경), 외야 그린석 4연석 매물 중
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

KST = timezone(timedelta(hours=9))
WEEKDAYS = "월화수목금토일"


def game_url(game):
    return (
        "https://www.ticketbay.co.kr/product/6549/list/0"
        f"?start_perform_date={game:%Y-%m-%d+%H}%3A{game:%M}%3A00"
        "&seat_grade=%EC%99%B8%EC%95%BC+%EA%B7%B8%EB%A6%B0%EC%84%9D"
        "&sale_quantity=4&is_together=YES"
    )


GAME = datetime(2026, 10, 3, 14, 0, tzinfo=KST)  # 경기 일시. 시작 후에는 자동 종료
TARGET_URL = os.environ.get("TICKETBAY_URL") or game_url(GAME)
TARGET_SECTIONS = {"401", "402", "403"}
MAX_PRICE = 20000                         # 1장 기준 최대 가격(원)

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "notified.json"
DUMP_DIR = BASE_DIR / "dump"


def game_label():
    return f"{GAME.month}/{GAME.day}({WEEKDAYS[GAME.weekday()]}) {GAME:%H:%M}"

# 구역 번호: "117구역"처럼 구역/블록이 붙은 숫자. 없으면 앞뒤에 숫자가 붙지 않은 3자리 숫자
SECTION_RE = re.compile(r"(?<![\d,.])(\d{1,4})\s*(?:구역|블록|블럭)")
BARE_NUM_RE = re.compile(r"(?<![\d,.:])(\d{3})(?![\d,.:])(?!\s*(?:원|열|번|장|매))")
SECTION_KEY_RE = re.compile(r"block|area|zone|section|구역", re.I)
PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{4,7})\s*원")
PRICE_KEY_RE = re.compile(r"price|amount|가격", re.I)
UNIT_PRICE_KEY_RE = re.compile(r"unit|per|each|one|장당", re.I)
ID_KEY_RE = re.compile(r"^(id|.*_id|.*Id|.*_no|.*No|seq)$")


def log(msg):
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------- 파싱 ----------------
def find_sections(text):
    explicit = set(SECTION_RE.findall(text))
    if explicit:
        return sorted(explicit)
    # "구역" 표기가 없으면 감시 대상 구역 번호만 인정합니다(날짜·가격 숫자 오인 방지).
    return sorted(set(BARE_NUM_RE.findall(text)) & TARGET_SECTIONS)


AISLE_WORD = "통로"


def is_aisle(listing):
    return AISLE_WORD in listing["text"] + listing.get("detail", "")


def parse_text_listing(text, url=""):
    """매물 한 건의 화면 텍스트에서 구역과 1장 가격을 뽑아냅니다."""
    sections = find_sections(text)
    prices = [int(p.replace(",", "")) for p in PRICE_RE.findall(text)]
    prices = [p for p in prices if p >= 1000]
    if not sections or not prices:
        return None
    # 1장 가격과 총액이 같이 보이는 경우가 있어 가장 작은 금액을 1장 가격으로 봅니다.
    return {"sections": sections, "price": min(prices), "text": " ".join(text.split()), "url": url}


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
        seat_text = " ".join(
            f"{v}구역" if SECTION_KEY_RE.search(k) and str(v).isdigit() else str(v)
            for k, v in pairs
            if not PRICE_KEY_RE.search(k) and (isinstance(v, str) or SECTION_KEY_RE.search(k)))
        sections = find_sections(seat_text)
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
# 가격(원)과 구역 번호를 함께 담은 가장 작은 요소 = 매물 카드 한 장
# (여러 카드를 감싼 목록 전체를 한 매물로 읽지 않도록 가장 안쪽 요소만 고릅니다)
CARD_TEXT_JS = r"""
() => {
  const priceRe = /\d[\d,]*\s*원/;
  const hits = Array.from(document.querySelectorAll('body *')).filter(el => {
    const t = (el.innerText || '').trim();
    return t.length > 0 && t.length < 800 && priceRe.test(t) && /\d{3}/.test(t.replace(/[\d,]+\s*원/g, ''));
  });
  const hasInnerHit = new Set();
  for (const el of hits) {
    for (let p = el.parentElement; p; p = p.parentElement) hasInnerHit.add(p);
  }
  // 카드가 링크로 되어 있으면 매물 상세 주소도 함께 가져옵니다.
  return hits.filter(el => !hasInnerHit.has(el)).map(el => {
    const a = el.closest('a[href]') || el.querySelector('a[href]');
    return {text: el.innerText, url: a ? a.href : ''};
  });
}
"""


def fetch_listings(dump=False, skip_keys=()):
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
        cards = page.evaluate(CARD_TEXT_JS)
        body_text = page.inner_text("body")

        if dump:
            DUMP_DIR.mkdir(exist_ok=True)
            (DUMP_DIR / "page.html").write_text(page.content(), encoding="utf-8")
            (DUMP_DIR / "page.txt").write_text(body_text, encoding="utf-8")
            (DUMP_DIR / "cards.json").write_text(
                json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
            (DUMP_DIR / "responses.json").write_text(
                json.dumps([{"url": u, "body": b} for u, b in json_bodies],
                           ensure_ascii=False, indent=2), encoding="utf-8")
            page.screenshot(path=str(DUMP_DIR / "page.png"), full_page=True)
            log(f"디버그 파일 저장: {DUMP_DIR}")

        card_listings = [l for l in (parse_text_listing(c["text"], c["url"]) for c in cards) if l]
        listings = []
        for _, body in json_bodies:
            listings.extend(parse_json_listings(body))
        source = "api"
        if listings:
            # API 매물에 같은 구역·가격의 화면 카드 주소를 붙입니다.
            for l in listings:
                card = next((c for c in card_listings
                             if c["sections"] == l["sections"] and c["price"] == l["price"]), None)
                l["url"] = card["url"] if card else ""
        else:  # API에서 못 찾으면 화면 텍스트로
            source = "화면"
            listings = card_listings

        # 새로 알릴 매물은 상세 페이지 설명까지 읽어서 '통로' 여부를 확인합니다.
        for l in listings:
            if is_match(l) and l.get("url") and listing_key(l) not in skip_keys:
                try:
                    page.goto(l["url"], wait_until="networkidle", timeout=30000)
                    l["detail"] = " ".join(page.inner_text("body").split())[:3000]
                except Exception as e:
                    log(f"상세 페이지를 열지 못했습니다: {e!r}")
        browser.close()
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


def check_once(dump=False, test=False):
    notified = set() if test else load_state()
    listings, source, _ = fetch_listings(dump=dump, skip_keys=notified)
    matches = [l for l in listings if is_match(l)]
    log(f"매물 후보 {len(listings)}건({source}), 조건 충족 {len(matches)}건")

    new = [l for l in matches if listing_key(l) not in notified]
    if not new:
        return
    # '통로' 매물을 맨 위에, 바로 들어갈 수 있는 링크와 함께 보여 줍니다.
    new.sort(key=lambda l: (not is_aisle(l), l["price"]))
    aisle = [l for l in new if is_aisle(l)]
    for l in new:
        log(f"  알림: {'/'.join(l['sections'])}구역 {l['price']:,}원"
            f"{' [통로]' if is_aisle(l) else ''} {l.get('url') or '(링크 없음)'}")

    def line(l):
        head = f"{'🚪 [통로] ' if is_aisle(l) else '- '}{'/'.join(l['sections'])}구역 · 1장 {l['price']:,}원"
        link = f"\n  👉 {l['url']}" if l.get("url") else ""
        return f"{head}{link}\n  {l['text'][:200]}"

    top = ""
    if aisle:
        top = ("🚪 설명에 '통로'가 들어간 매물이 있습니다. 바로 확인하세요!\n"
               f"👉 {aisle[0].get('url') or TARGET_URL}\n\n")
    body = (
        top
        + ("[테스트 실행] " if test else "")
        + f"{game_label()} 외야 그린석 4연석 조건에 맞는 매물이 올라왔습니다.\n"
        f"(구역 {', '.join(sorted(TARGET_SECTIONS))} · 1장 {MAX_PRICE:,}원 이하)\n\n"
        + "\n\n".join(line(l) for l in new)
        + f"\n\n목록 바로가기: {TARGET_URL}\n"
    )
    prefix = "[티켓베이 테스트]" if test else "[티켓베이]"
    tag = "🚪[통로] " if aisle else ""
    send_gmail(f"{tag}{prefix} 조건 매물 {len(new)}건 - {GAME.month}/{GAME.day} 4연석", body)
    if not test:
        save_state(notified | {listing_key(l) for l in new})


def main():
    ap = argparse.ArgumentParser(description="티켓베이 매물 Gmail 알림")
    ap.add_argument("--interval", type=int, default=60, help="확인 간격(초), 기본 60")
    ap.add_argument("--once", action="store_true", help="한 번만 확인하고 종료")
    ap.add_argument("--dump", action="store_true", help="페이지/응답을 dump/ 폴더에 저장(디버그)")
    ap.add_argument("--max-minutes", type=float, help="이 시간(분)이 지나면 종료 (GitHub Actions용)")
    ap.add_argument("--game", help='감시할 경기 일시 (예: "2026-10-05 18:30"). 기본 2026-10-03 14:00')
    ap.add_argument("--sections", help="감시 구역(쉼표로 구분). 지정하면 테스트 실행: 알림 기록을 남기지 않음")
    ap.add_argument("--max-price", type=int, help="1장 최대 가격. 지정하면 테스트 실행")
    ap.add_argument("--url", help="확인할 티켓베이 목록 주소. 지정하면 테스트 실행")
    ap.add_argument("--test-email", action="store_true", help="테스트 메일만 보내고 종료")
    args = ap.parse_args()

    for var in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        if not os.environ.get(var):
            sys.exit(f"환경변수 {var} 가 설정되지 않았습니다. README를 참고하세요.")

    global TARGET_SECTIONS, MAX_PRICE, TARGET_URL, GAME, STATE_FILE, DUMP_DIR
    if args.game:
        GAME = datetime.strptime(args.game, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        TARGET_URL = game_url(GAME)
        # 경기마다 알림 기록과 디버그 파일을 따로 둡니다.
        STATE_FILE = BASE_DIR / f"notified-{GAME:%m%d}.json"
        DUMP_DIR = BASE_DIR / f"dump-{GAME:%m%d}"
    test = bool(args.sections or args.max_price or args.url)
    if args.sections:
        TARGET_SECTIONS = {x.strip() for x in args.sections.split(",") if x.strip()}
    if args.max_price:
        MAX_PRICE = args.max_price
    if args.url:
        TARGET_URL = args.url
    log(f"경기: {game_label()} · 조건: 구역 {', '.join(sorted(TARGET_SECTIONS))} · 1장 {MAX_PRICE:,}원 이하"
        + (" (테스트 실행)" if test else ""))

    if args.test_email:
        send_gmail("[티켓베이 알림] 테스트 메일", "알림 설정이 정상입니다.\n" + TARGET_URL)
        return

    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None
    while True:
        if datetime.now(KST) >= GAME:
            log("경기 시작 시간이 지나 종료합니다.")
            return
        try:
            check_once(dump=args.dump, test=test)
        except Exception as e:  # 일시적인 오류로 감시가 멈추지 않도록
            log(f"오류: {e!r}")
        if args.once or (deadline and time.time() + args.interval >= deadline):
            return
        time.sleep(args.interval + random.uniform(0, args.interval * 0.3))


if __name__ == "__main__":
    main()
