# ETF Fair Value Experiment

This folder tests a long-only ETF fair-value entry model:

```text
obi_adj_bps = clip(P_obi * ETF_OBI - P_spread * spread_bps, -cap_bps, cap_bps)
fair_value = local_NAV * (1 + obi_adj_bps / 10000)
```

The signal is BUY-only. It buys only when fair value is above the executable ETF
ask by enough edge after the spread filter. There is no short/SELL entry signal.

## Data Plan

- `S_i`: get from KRX/PYKRX PDF via `get_etf_portfolio_deposit_file`.
- `U`: infer from PDF total amount and KIS official NAV, or override with
  `--creation-unit` if you know the ETF's creation unit.
- `C-F`: once `U` is known, estimate as:

```text
C-F = official_NAV * U - sum(PDF_amount_i)
```

You cannot reliably infer all `S_i` from 10 minutes of NAV/price data. That is an
underdetermined problem. Ten minutes can help calibrate `U` and `C-F` only after
the PDF holdings are already known.

## Commands

Top 30 ETFs by trading value:

```powershell
python -m etf_fair_value.run top --date 20260622 --limit 30 --out out\etf_top30.csv
```

Screen ETFs by trading-value rank and component count:

```powershell
python -m etf_fair_value.run screen --date 20260622 --scan-limit 100 --skip-top 5 --max-holdings 80 --select-limit 30 --out out\etf_screen.csv
```

For this strategy, excluding leveraged/inverse products is usually cleaner:

```powershell
python -m etf_fair_value.run screen --date 20260622 --scan-limit 100 --max-holdings 80 --exclude-name-keyword 레버리지 --exclude-name-keyword 인버스 --select-limit 30
```

Log BUY/HOLD signal snapshots for screened candidates:

```powershell
python -m etf_fair_value.run signal-scan --date 20260622 --scan-limit 100 --min-holdings 3 --max-holdings 40 --exclude-name-keyword 레버리지 --exclude-name-keyword 인버스 --duration-sec 21600 --interval-sec 30
```

One ETF snapshot:

```powershell
python -m etf_fair_value.run probe --code 069500 --date 20260622
```

If KRX/PYKRX PDF is unavailable because `KRX_ID/KRX_PW` is missing, the CLI falls
back to KIS ETF component weights and builds a pseudo per-share basket. That is
useful for fair-value probing, but it is not the exact `S_i`, `U`, `C-F` PDF
calculation.

Collect fair-value rows:

```powershell
python -m etf_fair_value.run collect --code 069500 --duration-sec 300 --interval-sec 1 --out out\fv_069500.csv
```

Check whether the local PDF component NAV is close to KIS official NAV:

```powershell
python -m etf_fair_value.run nav-check --code 069500 --date 20260622 --no-kis-component-fallback --require-full
```

Use component order-book microprices instead of last prices:

```powershell
python -m etf_fair_value.run nav-check --code 069500 --date 20260622 --price-type micro --micro-levels 1 --sleep-sec 0.25 --no-kis-component-fallback --require-full
```

Dry-run a BUY-only order decision. This does not send a Kiwoom order. By
default it calculates local component microprice NAV and places a passive
`bid1 + 1 tick` limit price when a BUY signal appears:

```powershell
python -m etf_fair_value.run trade-once --code 069500 --date 20260622 --qty 1 --price-type micro --micro-levels 1
```

Send a real Kiwoom order only when you explicitly add `--live-order`.

## Secrets

KIS keys are loaded by the existing `trading.kis` client from:

```text
trading/.kis.env
trading/.kis.env.local
```

Optional KRX login values can be placed in:

```text
etf_fair_value/.krx.env
```

with:

```text
KRX_ID=...
KRX_PW=...
```

The safer setup path is an interactive prompt:

```powershell
python -m etf_fair_value.run setup-krx
python -m etf_fair_value.run krx-check --code 069500 --date 20260622
```

The local secret file is ignored by git. Once configured, every command that
needs PYKRX PDF data loads it automatically and logs in before the PDF request.

## Practical Note

KIS REST polling is fine for probing and slow collection. For a production
microprice model across every component of 30 ETFs, REST polling will be too slow
and rate-limit constrained. That stage should move to a KIS real-time WebSocket
feed or a vendor feed, while keeping the same fair-value calculation layer.
