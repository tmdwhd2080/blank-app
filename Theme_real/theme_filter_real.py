# -*- coding: utf-8 -*-
"""
테마 유동성 필터 (Theme_real용)
- 구성종목 수 미달 필터(F1)만 적용
- 제로 리턴 비율 필터(F2)는 사용하지 않음
- CSV 기반 데이터(beta_rebound_detail)에서 구성종목 수를 직접 계산
"""

import pandas as pd
import numpy as np


def build_constituent_counts_from_csv(detail_df: pd.DataFrame) -> dict:
    """
    beta_rebound_detail CSV에서 테마별 구성종목 수를 계산한다.

    Parameters
    ----------
    detail_df : beta_rebound_detail_YYYYMMDD.csv 데이터프레임
                (theme_no, 테마명, 종목코드, ... 등 포함)

    Returns
    -------
    dict : {theme_no(str) → 구성종목 수(int)}
    """
    if detail_df.empty:
        return {}

    # 종목코드 기준으로 고유 종목 수 계산
    if '종목코드' in detail_df.columns:
        counts = (detail_df
                  .groupby('theme_no')['종목코드']
                  .nunique()
                  .to_dict())
    else:
        # 종목코드가 없으면 종목명 기준
        counts = (detail_df
                  .groupby('theme_no')['종목명']
                  .nunique()
                  .to_dict())

    # key를 str로 통일
    return {str(k): v for k, v in counts.items()}


def apply_constituent_filter(theme_adj_df: pd.DataFrame,
                              constituent_counts: dict,
                              min_constituents: int = 3) -> tuple:
    """
    구성종목 수 미달 필터를 적용한다.

    Parameters
    ----------
    theme_adj_df : 테마별 베타조정수익률 데이터 (theme_no, 베타조정수익률 등)
    constituent_counts : {theme_no(str) → 구성종목 수(int)}
    min_constituents : 최소 구성종목 수

    Returns
    -------
    valid_themes : 필터 통과 theme_no 리스트 (str)
    filter_stats : 필터 통계 dict
    """
    all_themes = theme_adj_df['theme_no'].astype(str).unique().tolist()
    valid_themes = []
    n_filtered = 0

    for theme_id in all_themes:
        n_const = constituent_counts.get(theme_id, 0)
        if n_const < min_constituents:
            n_filtered += 1
            continue
        valid_themes.append(theme_id)

    stats = {
        'total': len(all_themes),
        'f1_constituents': n_filtered,
        'passed': len(valid_themes),
    }
    return valid_themes, stats


def summarize_filter_stats(filter_log: list, config: dict = None) -> None:
    """필터 통계 요약 출력"""
    if not filter_log:
        return

    df = pd.DataFrame(filter_log)

    print(f"\n  📊 유동성 필터 통계 (총 {len(df)}회)")
    print(f"  {'─'*50}")
    print(f"  전체 테마 수 (평균):      {df['total'].mean():.1f}개")

    if config:
        print(f"  F1 구성종목 미달 (평균):  {df['f1_constituents'].mean():.1f}개 "
              f"(임계값: {config.get('min_constituents', '?')}개 미만)")
    else:
        print(f"  F1 구성종목 미달 (평균):  {df['f1_constituents'].mean():.1f}개")

    print(f"  필터 통과 (평균):         {df['passed'].mean():.1f}개 "
          f"({df['passed'].mean()/df['total'].mean()*100:.1f}%)")
    print(f"  {'─'*50}")
    print(f"  필터 통과 (최소/중앙/최대): "
          f"{df['passed'].min()} / {df['passed'].median():.0f} / {df['passed'].max()}개")
