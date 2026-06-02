# Korea Investment & Securities Open API

KIS Open API 수집기는 `trading.kis_collect`로 실행한다. API 키는 코드에는 저장하지 않고, 환경변수 또는 로컬 전용 파일에서 읽는다.

로컬 전용 파일:

```text
trading/.kis.env
trading/.kis.env.local
```

이 파일들은 `.gitignore`에 포함되어야 하며 공개 저장소에 올리면 안 된다.

## 환경변수

```bash
set KIS_APP_KEY=...
set KIS_APP_SECRET=...
set KIS_ENV=real
```

PowerShell:

```powershell
$env:KIS_APP_KEY="..."
$env:KIS_APP_SECRET="..."
$env:KIS_ENV="real"
```

## 토큰 확인

```bash
python -m trading.kis_collect token --force
```

## KOSPI200 선물 코드 마스터 저장

```bash
python -m trading.kis_collect index-futures-master --out out/kis_index_futures_master.csv
```

## 최근월물 후보 자동 선택 후 현재가 저장

```bash
python -m trading.kis_collect futures-price --out out/kis_futures_price.csv
```

## 특정 코드 현재가/호가 저장

```bash
python -m trading.kis_collect futures-price 101W09 --out out/kis_futures_price.csv
python -m trading.kis_collect futures-asking-price 101W09 --out out/kis_futures_asking_price.csv
```

KIS 공식 샘플 기준 국내선물옵션 현재가 REST는 `inquire-price`, 호가는 `inquire-asking-price`를 사용한다.
