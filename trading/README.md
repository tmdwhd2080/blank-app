# trading — 키움 REST API (실전계좌 전용)

> **모든 명령은 실전계좌 기준입니다.** 거래는 즉시 체결되어 실제 손익이 발생할 수 있습니다.

## 명령

| 명령 | 용도 |
|---|---|
| `account_check`     | 예수금 / 보유종목 / 미체결 / 합계 조회 |
| `data_collect`      | 일봉 / 분봉 / 종가 매트릭스 (REST) |
| `realtime_collect`  | WebSocket 실시간 — NXT 프리마켓 포함 |
| `place_order`       | 매수 / 매도 / 정정 / 취소 (실행 전 yes 확인) |

## 0. 사전 준비

```bash
pip install requests websockets python-dotenv pandas
```

키 등록은 `trading/.env.example` 또는 `trading/.env` 둘 중 어디든 가능 — 코드가 둘 다 읽음.
**둘 다 git에서 제외되어 있어** (`.gitignore` 138, 148-150 라인) 안전.

```dotenv
KIWOOM_APP_KEY=...
KIWOOM_APP_SECRET=...
KIWOOM_ENV=real
KIWOOM_ACCOUNT_NO=65578195
```

## 실행

> 작업 디렉토리는 `c:\Users\intern9\Desktop\blank-app`. 두 형태 모두 동작:
> - `python trading/account_check.py`
> - `python -m trading.account_check`

### 1) 계좌 조회

```bash
python trading/account_check.py
```

### 2) 데이터 수집 (REST 차트)

```bash
# 단일 종목 일봉
python trading/data_collect.py daily 005930
python trading/data_collect.py daily 005930 --start 20240101 --end 20260428

# 종가 매트릭스 (백테스트 입력)
python trading/data_collect.py close 005930 000660 035720 --start 20260101

# 분봉 (1/3/5/10/15/30/45/60)
python trading/data_collect.py minute 005930 --tic 5

# CSV 저장
python trading/data_collect.py daily 005930 --save out/005930.csv
```

### 3) NXT 프리마켓 실시간 수집

```bash
# 8시까지 대기 → 8:50까지 NXT 만 필터링해서 CSV 저장
python trading/realtime_collect.py 005930 000660 035720 \
    --start 08:00 --end 08:50 --exchange NXT \
    --out out/nxt_premarket_$(date +%Y%m%d).csv
```

### 4) 주문

```bash
# 지정가 매수 — 실행 직전 'yes' 확인 받음
python trading/place_order.py buy 005930 1 70000

# 시장가 매도
python trading/place_order.py sell 005930 1 --market

# 정정 / 취소
python trading/place_order.py modify 0001234 005930 1 71000
python trading/place_order.py cancel 0001234 005930

# 자동화 (확인 생략)
python trading/place_order.py buy 005930 1 70000 --yes
```

## 모듈 구조

```
config.py                  도메인 / TR 라우팅 / .env / .env.example 동시 로드
kiwoom/
  auth.py                  토큰 24h 캐시 (~/.kiwoom/token_real.json)
  http_client.py           레이트리밋 + 페이지네이션
  models.py                응답 DTO
  websocket_client.py      실시간 (asyncio + 자동 재연결)
  exceptions.py
  tr/
    market_data.py         ka10081/ka10080/ka10004
    account.py             kt00001/kt00018/kt00007
    order.py               kt10000~kt10003
service/
  data_loader.py           DataFrame 변환
  order_manager.py         주문 상태머신 (HTTP+WS 합성)

account_check.py           ── CLI 진입점
data_collect.py            ── CLI 진입점
realtime_collect.py        ── CLI 진입점 (WebSocket)
place_order.py             ── CLI 진입점
```

## 안전장치

- 토큰 파일(`~/.kiwoom/token_real.json`) 외부 노출 금지
- `.env`, `.env.example` 둘 다 `.gitignore` 처리
- 주문 함수 input 검증 (qty > 0, 단가 필요 시 > 0)
- 시장가 류는 단가 자동 빈 문자열 처리
- 실행 직전 `yes` 확인 (자동화는 `--yes`)
- WS 자동 재연결 (지수 backoff, 최대 30초)
