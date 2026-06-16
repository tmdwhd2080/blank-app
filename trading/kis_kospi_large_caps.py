# -*- coding: utf-8 -*-
"""Export current KOSPI large-cap names from the KIS KOSPI master file."""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests


MASTER_URL = "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"
KST = ZoneInfo("Asia/Seoul")

FIELD_SPECS = [
    2,
    1,
    4,
    4,
    4,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    9,
    5,
    5,
    1,
    1,
    1,
    2,
    1,
    1,
    1,
    2,
    2,
    2,
    3,
    1,
    3,
    12,
    12,
    8,
    15,
    21,
    2,
    7,
    1,
    1,
    1,
    1,
    1,
    9,
    9,
    9,
    5,
    9,
    8,
    9,
    3,
    1,
    1,
    1,
]

PART2_COLUMNS = [
    "그룹코드",
    "시가총액규모",
    "지수업종대분류",
    "지수업종중분류",
    "지수업종소분류",
    "제조업",
    "저유동성",
    "지배구조지수종목",
    "KOSPI200섹터업종",
    "KOSPI100",
    "KOSPI50",
    "KRX",
    "ETP",
    "ELW발행",
    "KRX100",
    "KRX자동차",
    "KRX반도체",
    "KRX바이오",
    "KRX은행",
    "SPAC",
    "KRX에너지화학",
    "KRX철강",
    "단기과열",
    "KRX미디어통신",
    "KRX건설",
    "Non1",
    "KRX증권",
    "KRX선박",
    "KRX섹터_보험",
    "KRX섹터_운송",
    "SRI",
    "기준가",
    "매매수량단위",
    "시간외수량단위",
    "거래정지",
    "정리매매",
    "관리종목",
    "시장경고",
    "경고예고",
    "불성실공시",
    "우회상장",
    "락구분",
    "액면변경",
    "증자구분",
    "증거금비율",
    "신용가능",
    "신용기간",
    "전일거래량",
    "액면가",
    "상장일자",
    "상장주수",
    "자본금",
    "결산월",
    "공모가",
    "우선주",
    "공매도과열",
    "이상급등",
    "KRX300",
    "KOSPI",
    "매출액",
    "영업이익",
    "경상이익",
    "당기순이익",
    "ROE",
    "기준년월",
    "시가총액",
    "그룹사코드",
    "회사신용한도초과",
    "담보대출가능",
    "대주가능",
]

SIZE_LABELS = {
    "0": "제외",
    "1": "대형주",
    "2": "중형주",
    "3": "소형주",
}

GROUP_LABELS = {
    "ST": "주권",
    "MF": "증권투자회사",
    "RT": "부동산투자회사",
    "SC": "선박투자회사",
    "IF": "사회간접자본투융자회사",
    "DR": "주식예탁증서",
    "EW": "ELW",
    "EF": "ETF",
    "SW": "신주인수권증권",
    "SR": "신주인수권증서",
    "BC": "수익증권",
    "FE": "해외ETF",
    "FS": "외국주권",
}


def download_master_rows() -> list[str]:
    response = requests.get(MASTER_URL, timeout=30)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        member = next(
            name
            for name in archive.namelist()
            if name.lower().endswith("kospi_code.mst")
        )
        data = archive.read(member)

    return data.decode("cp949").splitlines()


def parse_master(rows: list[str]) -> pd.DataFrame:
    part1_records = []
    part2_lines = []

    for row in rows:
        part1 = row[: len(row) - 228]
        part1_records.append(
            {
                "단축코드": part1[0:9].strip(),
                "표준코드": part1[9:21].strip(),
                "한글명": part1[21:].strip(),
            }
        )
        part2_lines.append(row[-228:])

    df1 = pd.DataFrame(part1_records)
    df2 = pd.read_fwf(
        io.StringIO("\n".join(part2_lines)),
        widths=FIELD_SPECS,
        names=PART2_COLUMNS,
        dtype=str,
    )
    df = pd.concat([df1, df2], axis=1)

    for column in df.columns:
        df[column] = df[column].fillna("").astype(str).str.strip()

    return df


def build_large_caps(df: pd.DataFrame, downloaded_at: str) -> pd.DataFrame:
    large = df[(df["그룹코드"] == "ST") & (df["시가총액규모"] == "1")].copy()

    large["시장"] = "KOSPI"
    large["시가총액규모명"] = large["시가총액규모"].map(SIZE_LABELS).fillna("")
    large["증권그룹"] = large["그룹코드"].map(GROUP_LABELS).fillna(large["그룹코드"])
    large["주식종류"] = large["우선주"].map({"Y": "우선주"}).replace("", "보통주")
    large["기준가_원"] = pd.to_numeric(large["기준가"], errors="coerce").astype("Int64")
    large["시가총액_억원"] = pd.to_numeric(large["시가총액"], errors="coerce").astype("Int64")
    large["상장주수"] = pd.to_numeric(large["상장주수"], errors="coerce").astype("Int64")
    large["source_downloaded_at_kst"] = downloaded_at

    large = large.sort_values(
        ["시가총액_억원", "단축코드"],
        ascending=[False, True],
        na_position="last",
    ).reset_index(drop=True)
    large.insert(0, "순위", large.index + 1)

    columns = [
        "순위",
        "시장",
        "단축코드",
        "표준코드",
        "한글명",
        "시가총액규모명",
        "시가총액_억원",
        "기준가_원",
        "상장주수",
        "주식종류",
        "증권그룹",
        "KOSPI200섹터업종",
        "KOSPI100",
        "KOSPI50",
        "KRX100",
        "KRX300",
        "지수업종대분류",
        "지수업종중분류",
        "지수업종소분류",
        "상장일자",
        "거래정지",
        "관리종목",
        "시장경고",
        "기준년월",
        "source_downloaded_at_kst",
    ]
    return large[columns]


def export_files(df: pd.DataFrame, output_dir: Path, base_name: str) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / f"{base_name}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    paths = {"csv": str(csv_path)}
    xlsx_path = output_dir / f"{base_name}.xlsx"
    try:
        df.to_excel(xlsx_path, index=False)
    except ImportError:
        pass
    else:
        paths["xlsx"] = str(xlsx_path)

    meta_path = output_dir / f"{base_name}.meta.json"
    meta = {
        "source": "KIS KOSPI master file",
        "source_url": MASTER_URL,
        "filter": {"그룹코드": "ST", "시가총액규모": "1"},
        "row_count": int(len(df)),
        "generated_at_kst": df["source_downloaded_at_kst"].iloc[0] if len(df) else None,
        "files": paths,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["meta"] = str(meta_path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path("out"))
    parser.add_argument("--base-name", default=None)
    args = parser.parse_args()

    downloaded_at = datetime.now(KST).replace(microsecond=0).isoformat()
    base_name = args.base_name or f"kis_kospi_large_caps_{datetime.now(KST):%Y%m%d}"

    rows = download_master_rows()
    master = parse_master(rows)
    large_caps = build_large_caps(master, downloaded_at)
    paths = export_files(large_caps, args.out_dir, base_name)

    print(json.dumps({"row_count": len(large_caps), "paths": paths}, ensure_ascii=False))


if __name__ == "__main__":
    main()
