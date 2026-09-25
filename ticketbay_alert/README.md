# 티켓베이 매물 Gmail 알림

**10/3(토) 14:00 LG 트윈스 · 외야 그린석 · 4연석** 매물 중에서
**401 / 402 / 403구역**이면서 **1장 가격이 20,000원 이하**인 매물이 올라오면 Gmail로 알려줍니다.

- 기본 60초(+랜덤 0~18초)마다 확인하고, 한 번 알린 매물은 다시 알리지 않습니다(`notified.json`).
- 경기 시작 시간(10/3 14:00)이 지나면 자동으로 종료됩니다.
- 조건을 바꾸려면 `ticketbay_alert.py` 맨 위의 `TARGET_SECTIONS`, `MAX_PRICE`를 고치면 됩니다.

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
