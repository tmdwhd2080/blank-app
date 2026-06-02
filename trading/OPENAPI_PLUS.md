# Kiwoom OpenAPI+ 수집기

이 수집기는 키움 REST API가 아니라 영웅문/KOA Studio 기반 OpenAPI+ OCX를 사용한다.
REST의 `appkey`/`secretkey`를 쓰지 않고, 키움 로그인 창을 통해 연결한다.

## 필요한 것

- Windows
- 키움 OpenAPI+ 설치 및 사용 신청
- KOA Studio에서 로그인/버전처리 정상 확인
- 32-bit Python 환경. Python 3.10 또는 3.11 32-bit 권장
- `pip install -r trading/requirements-openapi-plus.txt`

현재 기본 Python이 64-bit라면 OpenAPI+ OCX를 직접 띄울 수 없다. 32-bit Python에서 아래 명령을 실행해야 한다.

## 환경 점검

```bash
python -m trading.openapi_plus_collect doctor
python -m trading.openapi_plus_collect doctor --control
```

## 선물 코드 목록 저장

```bash
python -m trading.openapi_plus_collect future-codes --out out/openapi_future_codes.csv
```

## 최근월물로 실시간 선물 시세/호가 저장

```bash
python -m trading.openapi_plus_collect futures-realtime --front --seconds 60 --out out/kospi200_futures_realtime.csv
```

## 특정 선물 코드로 실시간 저장

```bash
python -m trading.openapi_plus_collect futures-realtime 101T6000 --seconds 60 --out out/kospi200_futures_realtime.csv
```

코드는 `future-codes` 결과를 보고 넣는다.

## KOA Studio TR을 CSV로 저장

KOA Studio에서 TR 입력명과 출력 필드명을 확인한 뒤 그대로 넘긴다.

```bash
python -m trading.openapi_plus_collect tr ^
  --tr opt50001 ^
  --input 종목코드=101T6000 ^
  --field 현재가 ^
  --field 이론가 ^
  --field 괴리율 ^
  --out out/opt50001_snapshot.csv
```

필드명은 KOA Studio 출력명과 정확히 같아야 한다.
