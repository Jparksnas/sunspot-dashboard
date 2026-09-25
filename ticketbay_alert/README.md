# 티켓베이 매물 Gmail 알림

**10/3(토) 14:00 LG 트윈스 · 외야 그린석 · 4연석** 매물 중에서
**401 / 402 / 403구역**이면서 **1장 가격이 20,000원 이하**인 매물이 올라오면 Gmail로 알려줍니다.

- 기본 60초(+랜덤 0~18초)마다 확인하고, 한 번 알린 매물은 다시 알리지 않습니다(`notified.json`).
- 경기 시작 시간(10/3 14:00)이 지나면 자동으로 종료됩니다.
- 조건을 바꾸려면 `ticketbay_alert.py` 맨 위의 `TARGET_SECTIONS`, `MAX_PRICE`를 고치면 됩니다.

## 📱 휴대폰으로 쓰기 (GitHub Actions, 컴퓨터 필요 없음)

GitHub 서버가 대신 실행해 줍니다. 휴대폰에서는 처음에 비밀번호만 등록하면 되고, 알림은 Gmail 앱으로 받습니다.
(공개 저장소라 무료입니다. 15분마다 예약 실행되고, 한 번 실행되면 28분 동안 1분 간격으로 확인합니다.)

1. **Gmail 앱 비밀번호 만들기**: 아래 "2. Gmail 앱 비밀번호 만들기"와 같습니다.
2. **비밀번호 등록**: 휴대폰 브라우저(크롬/사파리)로 GitHub에 로그인해서
   저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**에서 아래 항목을 추가합니다.
   (GitHub 앱에는 이 메뉴가 없으니 브라우저를 쓰세요. 메뉴가 안 보이면 브라우저에서 "데스크톱 사이트"를 켜세요.)
   - `GMAIL_ADDRESS`: 보내는 Gmail 주소
   - `GMAIL_APP_PASSWORD`: 16자리 앱 비밀번호
   - `NOTIFY_TO`: 받는 주소 (선택. 없으면 GMAIL_ADDRESS로 보냄)
3. **바로 한 번 실행**: 저장소 → **Actions** → **티켓베이 매물 알림** → **Run workflow**.
   이후에는 15분마다 자동으로 실행됩니다.
4. **잘 되는지 확인**: Actions에서 실행 기록을 눌러 **매물 확인** 단계 로그에 `매물 후보 N건`이 나오는지 보세요.
   실행 기록 맨 아래 **Artifacts**의 `ticketbay-dump`에 마지막으로 본 페이지 화면(`page.png`)이 있습니다.
5. **끄기**: Actions → 티켓베이 매물 알림 → 오른쪽 위 `...` → **Disable workflow**.
   끄지 않아도 10/3 14:00 이후에는 실행되자마자 바로 끝납니다.

> GitHub 서버는 해외(미국)에 있어서 티켓베이가 접속을 막을 수도 있습니다.
> 4번에서 매물 후보가 계속 0건이거나 page.png에 목록이 안 보이면 컴퓨터에서 실행하는 방법을 쓰세요.

## 📅 다른 경기 감시 (10/5)

10/5(월·대체공휴일) 14:00 경기는 **티켓베이 매물 알림 (10/5)** 워크플로가 따로 감시합니다.
조건은 10/3과 같고, 알림 기록도 따로 관리됩니다. 끄려면 Actions에서 이 워크플로만 Disable 하면 됩니다.
경기 시간이 다르면 `.github/workflows/ticketbay-alert-1005.yml`의 `GAME`, `GAME_STOP` 두 줄을 고치세요.
컴퓨터에서는 `python ticketbay_alert.py --game "2026-10-05 14:00"`처럼 실행합니다.

두 워크플로 모두 실행이 정상적으로 끝나면 다음 감시를 스스로 이어 붙이므로,
GitHub 예약 실행이 늦거나 빠져도 감시가 끊기지 않습니다.

## 💻 컴퓨터에서 실행하기

## 1. 설치 (한 번만)

```bash
cd ticketbay_alert
pip install -r requirements.txt
python -m playwright install chromium
```

## 2. Gmail 앱 비밀번호 만들기

일반 Gmail 비밀번호로는 로그인되지 않습니다.
1. Google 계정에 **2단계 인증**을 켭니다.
2. https://myaccount.google.com/apppasswords 에서 앱 비밀번호(16자리)를 만듭니다.

## 3. 환경변수 설정

macOS / Linux
```bash
export GMAIL_ADDRESS="내주소@gmail.com"
export GMAIL_APP_PASSWORD="abcd efgh ijkl mnop"
export NOTIFY_TO="받을주소@gmail.com"   # 생략하면 GMAIL_ADDRESS로 보냄
```

Windows (PowerShell)
```powershell
$env:GMAIL_ADDRESS="내주소@gmail.com"
$env:GMAIL_APP_PASSWORD="abcd efgh ijkl mnop"
```

## 4. 실행

```bash
python ticketbay_alert.py --test-email   # 테스트 메일이 오는지 먼저 확인
python ticketbay_alert.py --once --dump  # 한 번 확인 + 페이지 내용을 dump/에 저장
python ticketbay_alert.py                # 계속 감시 (Ctrl+C로 종료)
python ticketbay_alert.py --interval 120 # 2분 간격으로 감시
```

컴퓨터가 켜져 있고 프로그램이 실행 중일 때만 감시합니다.

## 동작 방식과 주의사항

- 헤드리스 크롬으로 티켓베이 목록 페이지(4장·연석 필터 적용)를 열고,
  ① 페이지가 불러오는 API(JSON) 응답에서 매물을 찾고, 없으면 ② 화면에 보이는 매물 카드 텍스트에서
  구역 번호(401~403)와 가격(○○,○○○원)을 뽑아냅니다.
- 1장 가격과 총액이 함께 보이면 **작은 금액을 1장 가격**으로 봅니다.
- 이 프로그램은 티켓베이 실제 페이지로 검증되지 않았습니다(작성 환경에서 사이트 접속이 차단됨).
  처음 실행할 때 `--once --dump`로 로그의 "매물 후보 N건"이 실제 목록 수와 맞는지,
  `dump/page.png`, `dump/cards.json`을 확인해 주세요.
  맞지 않으면 dump 파일을 공유해 주시면 파서를 맞춰 드릴 수 있습니다.
- 사이트에 부담을 주지 않도록 간격을 너무 짧게(30초 미만) 설정하지 마세요.
