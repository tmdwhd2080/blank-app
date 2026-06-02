import os
import sys
import re
import time
import pymssql
import datetime
import pandas as pd
import numpy as np
import collections
import logging
from typing import Optional, List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import product
sys.path.append(os.path.join(r'C:\Users\intern9', 'truston_quant_dev'))

from util import variables as v

class DBConfig:
    MSSQL_SERVER = v.trst_server
    MSSQL_USER = v.trst_id
    MSSQL_PASSWORD = v.trst_pw
    TRSTDEV_DB = v.trstdb
    TRUSTQUANT_DB = getattr(v, 'trustquantdb', 'TRUSTQUANTDB')
    WFNS_DB = getattr(v, 'wfnsdb', 'WFNS2DB')


FACTOR_MAPPING = {
    'CP_V': 'Value',
    'CP_G': 'Growth',
    'CP_Q': 'Quality',
    'CP_LV': 'LowVol',
    'CP_MOM': 'Momentum',
    'CP_S': 'Size',
}

FACTOR_CODE_MAPPING = {v: k for k, v in FACTOR_MAPPING.items()}
#지금은 사용 X
INDEX_MAPPING = {
    'IKS001': 'KOSPI',
    'IKS200': 'KOSPI200',
    'IKQ150': 'KOSDAQ150',
}

INDEX_CODE_MAPPING = {v: k for k, v in INDEX_MAPPING.items()}


class MSSQL:
    def __init__(self,
                 server: str = DBConfig.MSSQL_SERVER,
                 user: str = DBConfig.MSSQL_USER,
                 password: str = DBConfig.MSSQL_PASSWORD,
                 database: str = DBConfig.TRSTDEV_DB):

        self.server = server
        self.user = user
        self.password = password
        self.database = database
        self._conn = None
        self._cursor = None
        self._conn_cp = None
        self._cursor_cp = None
        self._connect()

    def __del__(self):
        try:
            self.close()
        except:
            pass

    def _connect(self):
        try:
            self._conn = pymssql.connect(
                server=self.server,
                user=self.user,
                password=self.password,
                database=self.database,
                charset='utf8'
            )
            self._cursor = self._conn.cursor()

            self._conn_cp = pymssql.connect(
                server=self.server,
                user=self.user,
                password=self.password,
                database=self.database,
                charset='CP949'
            )
            self._cursor_cp = self._conn_cp.cursor()

            logging.info(f"Connected to {self.database}")

        except Exception as e:
            logging.error(f"Connection failed: {e}")
            raise

    def close(self):
        if self._conn:
            self._conn.close()
        if self._conn_cp:
            self._conn_cp.close()

    def reconnect(self):
        self.close()
        self._connect()

    def SELECT(self,
               query: str,
               print_query: bool = False,
               output_type: str = "DataFrame",
               index_col: str = None) -> Optional[pd.DataFrame]:
        if output_type != "DataFrame" and index_col is not None:
            raise TypeError("index_col을 사용하려면 output_type이 'DataFrame'이어야 합니다.")

        try:
            self._cursor.execute(query)
            row = self._cursor.fetchone()

            header_list = [desc[0] for desc in self._cursor.description]

            if print_query:
                print('\t'.join(header_list))

            data_mat = []
            while row:
                row = list(row)
                row_list = []
                for r in row:
                    try:
                        if isinstance(r, str):
                            r = r.encode('ISO-8859-1').decode('euc-kr')
                    except:
                        pass
                    row_list.append(r)

                if print_query:
                    print('\t'.join(str(x) for x in row_list))

                data_mat.append(row_list)
                row = self._cursor.fetchone()

            if len(data_mat) == 0:
                return None

            if output_type == "old_version":
                return data_mat

            data_df = pd.DataFrame(data_mat, columns=header_list)

            if output_type == "DataFrame":
                if index_col is not None:
                    if index_col not in data_df.columns:
                        raise ValueError(f"'{index_col}' 컬럼이 존재하지 않습니다.")
                    data_df = data_df.set_index(index_col)

                if len(data_df.columns) == 1:
                    return data_df.iloc[:, 0]
                return data_df

            elif output_type == "array":
                return np.array(data_mat).T

            elif output_type == "list":
                return list(np.array(data_mat).T)

            else:
                raise ValueError(f"잘못된 output_type: {output_type}")

        except Exception as e:
            logging.error(f"SELECT 실패: {e}")
            raise

    def INSERT(self,
               data: pd.DataFrame,
               table_name: str,
               header: int = None,
               print_query: bool = False) -> bool:
        try:
            if header is None:
                columns = list(data.columns)
                start_num = 0
            else:
                columns = list(data.iloc[header])
                start_num = header + 1

            base_query = f"INSERT INTO {table_name}\n("
            base_query += ", ".join([f"[{col}]" for col in columns])
            base_query += ")\n"

            data = data.reset_index(drop=True)

            for idx in range(start_num, len(data)):
                row = list(data.iloc[idx])
                insert_query = base_query + "VALUES ("

                values = []
                for j, val in enumerate(row):
                    if pd.isnull(val) or str(val) in ["nan", "Nan", "NaN"]:
                        values.append("NULL")
                    elif "'" in str(val):
                        values.append(f"'{str(val).replace(chr(39), chr(39)+chr(39))}'")
                    else:
                        values.append(f"'{val}'")

                insert_query += ", ".join(values) + ")"

                if print_query:
                    print(insert_query)

                self._cursor.execute(insert_query)
                self._conn.commit()

            return True

        except Exception as e:
            logging.error(f"INSERT 실패 (행 {idx}): {e}")
            return False

    def DELETE(self, table: str, where: str = None) -> None:
        if where is None:
            raise SystemError("전체 삭제는 DB 툴을 사용하세요. WHERE 조건을 입력해주세요.")

        query = f"DELETE FROM {table} WHERE {where}"
        print(query)
        self._cursor.execute(query)
        self._conn.commit()

    def DELETE_RES(self, table: str, where: str = None, print_query: bool = False) -> int:
        if where is None:
            raise SystemError("WHERE 조건을 입력해주세요.")

        query = f"DELETE FROM {table} WHERE {where}"
        if print_query:
            print(query)

        self._cursor.execute(query)
        rows_affected = self._cursor.rowcount
        self._conn.commit()

        return rows_affected

    def UPDATE(self,
               table: str,
               set_clause: str,
               where: str) -> int:
        query = f"UPDATE {table} SET {set_clause} WHERE {where}"
        self._cursor.execute(query)
        rows_affected = self._cursor.rowcount
        self._conn.commit()
        return rows_affected

    def run_query(self, query: str) -> int:
        try:
            self._cursor.execute(query + '; SELECT @@ROWCOUNT rc;')
            row = self._cursor.fetchone()
            self._conn.commit()
            return row[0]
        except Exception as e:
            logging.error(f"Query 실행 실패: {e}")
            return -1

    def fetch_large_data(self,
                         query: str,
                         chunk_size: int = 100000,
                         print_progress: bool = False):
        self._cursor_cp.execute(query)
        chunk_count = 0

        while True:
            chunk = self._cursor_cp.fetchmany(chunk_size)
            chunk_count += 1

            if print_progress:
                print(f"Processed chunk {chunk_count}...")

            if not chunk:
                break

            columns = [col[0] for col in self._cursor_cp.description]
            yield pd.DataFrame(chunk, columns=columns)

    def select_large_data(self,
                          query: str,
                          chunk_size: int = 100000,
                          print_progress: bool = False) -> pd.DataFrame:
        dfs = []
        for chunk_df in self.fetch_large_data(query, chunk_size, print_progress):
            dfs.append(chunk_df)

        if not dfs:
            return pd.DataFrame()

        return pd.concat(dfs, ignore_index=True)

    def select_large_data_parallel(self,
                                   base_query: str,
                                   chunk_size: int = 100000,
                                   max_workers: int = 4,
                                   order_by_cols: List[str] = None) -> pd.DataFrame:
        start_time = time.time()

        order_clause = ", ".join(order_by_cols) if order_by_cols else "(SELECT NULL)"

        count_query = f"""
            WITH NumberedData AS (
                SELECT *, ROW_NUMBER() OVER (ORDER BY {order_clause}) AS row_num
                FROM ({base_query}) AS subquery
            )
            SELECT MAX(row_num) FROM NumberedData
        """
        self._cursor.execute(count_query)
        total_rows = self._cursor.fetchone()[0]

        if not total_rows:
            return pd.DataFrame()

        print(f"Total rows: {total_rows}")

        ranges = [
            (start, min(start + chunk_size - 1, total_rows))
            for start in range(1, total_rows + 1, chunk_size)
        ]

        dfs = []

        def fetch_chunk(start: int, end: int, idx: int):
            print(f"Fetching chunk {idx}: rows {start} to {end}")
            query = f"""
                WITH NumberedData AS (
                    SELECT *, ROW_NUMBER() OVER (ORDER BY {order_clause}) AS row_num
                    FROM ({base_query}) AS subquery
                )
                SELECT * FROM NumberedData
                WHERE row_num BETWEEN {start} AND {end}
            """
            with self._conn.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
                columns = [col[0] for col in cursor.description]
                return pd.DataFrame.from_records(rows, columns=columns)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(fetch_chunk, r[0], r[1], idx + 1)
                for idx, r in enumerate(ranges)
            ]
            for future in as_completed(futures):
                df_chunk = future.result()
                dfs.append(df_chunk)

        final_df = pd.concat(dfs, ignore_index=True)
        elapsed = time.time() - start_time
        print(f"Total execution time: {elapsed:.2f} seconds")

        return final_df

    # 팩터 관련 메서드
    def get_pfm_fctr(self,
                     freq: str = None,
                     lag: int = None,
                     model: str = None,
                     fld_name: str = None,
                     factor_name: str = None,
                     start_date: str = None,
                     end_date: str = None,
                     limit: int = None) -> pd.DataFrame:
        if factor_name and not fld_name:
            fld_name = FACTOR_CODE_MAPPING.get(factor_name)
            if not fld_name:
                raise ValueError(f"잘못된 factor_name: {factor_name}. "
                                 f"가능한 값: {list(FACTOR_CODE_MAPPING.keys())}")

        query = "SELECT "
        if limit:
            query += f"TOP {limit} "
        query += "ID, MODEL, FLD_NAME, UG, FREQ, LAG, BaseDate, BuyDate, SellDate "
        query += "FROM PFM_FCTR WHERE 1=1"

        if freq:
            query += f" AND FREQ = '{freq}'"
        if lag is not None:
            query += f" AND LAG = {lag}"
        if model:
            query += f" AND MODEL = '{model}'"
        if fld_name:
            query += f" AND FLD_NAME = '{fld_name}'"
        if start_date:
            query += f" AND BaseDate >= '{start_date}'"
        if end_date:
            query += f" AND BaseDate <= '{end_date}'"

        query += " ORDER BY BaseDate DESC"

        return self.SELECT(query)

    def get_factor_returns(self,
                           factor_name: str = None,
                           fld_name: str = None,
                           start_date: str = None,
                           end_date: str = None,
                           limit: int = None) -> pd.DataFrame:
        return self.get_pfm_fctr(
            freq='W',
            lag=1,
            model='COM_FCTR',
            fld_name=fld_name,
            factor_name=factor_name,
            start_date=start_date,
            end_date=end_date,
            limit=limit
        )

    def get_all_factor_returns(self,
                               start_date: str = None,
                               end_date: str = None) -> pd.DataFrame:
        query = """
            SELECT
                ID, MODEL, FLD_NAME, UG, FREQ, LAG,
                BaseDate, BuyDate, SellDate
            FROM PFM_FCTR
            WHERE FREQ = 'W'
            AND LAG = 1
            AND MODEL = 'COM_FCTR'
        """

        if start_date:
            query += f" AND BaseDate >= '{start_date}'"
        if end_date:
            query += f" AND BaseDate <= '{end_date}'"

        query += " ORDER BY BaseDate DESC, FLD_NAME"

        df = self.SELECT(query)

        if df is not None and len(df) > 0:
            df['FactorName'] = df['FLD_NAME'].map(FACTOR_MAPPING)

        return df

    def get_factor_data_pivot(self,
                              start_date: str = None,
                              end_date: str = None) -> pd.DataFrame:
        df = self.get_all_factor_returns(start_date, end_date)

        if df is None or len(df) == 0:
            return None

        pivot_df = df.pivot_table(
            index='BaseDate',
            columns='FactorName',
            values='SellDate',
            aggfunc='first'
        )

        col_order = ['Value', 'Growth', 'Quality', 'LowVol', 'Momentum', 'Size']
        existing_cols = [c for c in col_order if c in pivot_df.columns]
        pivot_df = pivot_df[existing_cols]

        return pivot_df

    def get_regime_qms(self,
                       start_date: str = None,
                       end_date: str = None,
                       limit: int = None) -> pd.DataFrame:
        query = "SELECT "
        if limit:
            query += f"TOP {limit} "
        query += "* FROM REGIME_QMS WHERE 1=1"

        if start_date:
            query += f" AND BaseDate >= '{start_date}'"
        if end_date:
            query += f" AND BaseDate <= '{end_date}'"

        return self.SELECT(query)

    # 지수 관련 메서드
  
    def get_ts_idx_daily(self,
                         sec_cd: str = None,
                         index_name: str = None,
                         start_date: str = None,
                         end_date: str = None,
                         limit: int = None) -> pd.DataFrame:
        if index_name and not sec_cd:
            sec_cd = INDEX_CODE_MAPPING.get(index_name)
            if not sec_cd:
                raise ValueError(f"잘못된 index_name: {index_name}. "
                                 f"가능한 값: {list(INDEX_CODE_MAPPING.keys())}")

        query = "SELECT "
        if limit:
            query += f"TOP {limit} "
        query += """
            TRD_DT, SEC_CD, ARITH_AVG_PRC, WGT_AVG_PRC,
            OPEN_PRC, HIGH_PRC, LOW_PRC, CLOSE_PRC, TRD_QTY
        """
        query += " FROM TS_IDX_DAILY WHERE 1=1"

        if sec_cd:
            query += f" AND SEC_CD = '{sec_cd}'"
        if start_date:
            query += f" AND TRD_DT >= '{start_date}'"
        if end_date:
            query += f" AND TRD_DT <= '{end_date}'"

        query += " ORDER BY TRD_DT DESC"

        df = self.SELECT(query)

        if df is not None and len(df) > 0:
            df['IndexName'] = df['SEC_CD'].map(INDEX_MAPPING)

        return df

    def get_kospi(self,
                  start_date: str = None,
                  end_date: str = None,
                  limit: int = None) -> pd.DataFrame:
        return self.get_ts_idx_daily(
            sec_cd='IKS001',
            start_date=start_date,
            end_date=end_date,
            limit=limit
        )

    def get_kospi200(self,
                     start_date: str = None,
                     end_date: str = None,
                     limit: int = None) -> pd.DataFrame:
        return self.get_ts_idx_daily(
            sec_cd='IKS200',
            start_date=start_date,
            end_date=end_date,
            limit=limit
        )

    def get_kosdaq150(self,
                      start_date: str = None,
                      end_date: str = None,
                      limit: int = None) -> pd.DataFrame:
        return self.get_ts_idx_daily(
            sec_cd='IKQ150',
            start_date=start_date,
            end_date=end_date,
            limit=limit
        )

    # Risk-Free Rate 관련 메서드
    def get_risk_free_rate(self,
                           start_date: str,
                           end_date: str,
                           source: str = 'TB3Y',
                           freq: str = 'W') -> pd.DataFrame:

        rf_file_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'risk-free rate.xlsx'
        )

        # 날짜 형식 정규화
        start_dt = pd.to_datetime(self._normalize_date_yyyymmdd(start_date))
        end_dt = pd.to_datetime(self._normalize_date_yyyymmdd(end_date))

        print(f"[LOAD] Risk-free rate loading... (source: Excel file)")
        print(f"   File: {rf_file_path}")
        print(f"   Period: {start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')}")

        try:
            df_rf = pd.read_excel(
                rf_file_path,
                skiprows=4,
                usecols=[0, 1],
                names=['date', 'rf_annual']
            )

        except Exception as e:
            logging.error(f"엑셀 파일 읽기 실패: {e}")
            raise

        if df_rf is None or len(df_rf) == 0:
            raise ValueError(f"Risk-free rate 데이터 없음: {start_date} ~ {end_date}")

        try:
            df_rf['date'] = pd.to_datetime(df_rf['date'], format='%d-%b-%y', errors='coerce')
        except:
            df_rf['date'] = pd.to_datetime(df_rf['date'], errors='coerce')
        
        # 연도가 1900년대로 잘못 파싱된 경우 보정 (예: 1926 -> 2026)
        if df_rf['date'].notna().any():
            mask = df_rf['date'].dt.year < 2000
            if mask.any():
                df_rf.loc[mask, 'date'] = df_rf.loc[mask, 'date'] + pd.DateOffset(years=100)

        # 수치 변환 (결측치 #N/A 등 처리)
        df_rf['rf_annual'] = pd.to_numeric(df_rf['rf_annual'], errors='coerce')

        # 결측치 제거 (날짜 및 금리 모두 유효한 행만)
        df_rf = df_rf.dropna(subset=['date', 'rf_annual'])

        # 기간 필터링
        df_rf = df_rf[(df_rf['date'] >= start_dt) & (df_rf['date'] <= end_dt)]
        df_rf = df_rf.sort_values('date').reset_index(drop=True)

        # 각 주기별 수익률 계산 (연율 → 주기별-> 리밸런싱 주기 맞춰서 weekly 만 사용)
        df_rf['rf_daily'] = (1 + df_rf['rf_annual'] / 100) ** (1 / 365) - 1
        df_rf['rf_weekly'] = (1 + df_rf['rf_annual'] / 100) ** (1 / 52) - 1
        df_rf['rf_monthly'] = (1 + df_rf['rf_annual'] / 100) ** (1 / 12) - 1

        # 금리 레짐 설정
        df_rf['rf_regime'] = pd.cut(
            df_rf['rf_annual'],
            bins=[-np.inf, 1.0, 2.5, 4.0, np.inf],
            labels=[0, 1, 2, 3]
        ).astype(int)

        # 금리 변화율 (전일 대비)
        df_rf['rf_change'] = df_rf['rf_annual'].diff().fillna(0)

        # 금리 추세(20일 이동 평균 + 20일 표준편차)
        df_rf['rf_ma20'] = df_rf['rf_annual'].rolling(window=20, min_periods=1).mean()
        df_rf['rf_std20'] = df_rf['rf_annual'].rolling(window=20, min_periods=1).std().fillna(0)

        # 금리 모멘텀 (상승/하락/횡보)-> 20일 이동평균 보다 2% 상/하
        df_rf['rf_direction'] = np.where(
            df_rf['rf_annual'] > df_rf['rf_ma20'] * 1.02, 1,
            np.where(df_rf['rf_annual'] < df_rf['rf_ma20'] * 0.98, -1, 0)
        )

        # 주간/월간 리샘플링(여기는 w))
        if freq == 'W':
            df_rf = self._resample_rf_data(df_rf, freq='W', agg='mean')
        elif freq == 'M':
            df_rf = self._resample_rf_data(df_rf, freq='M', agg='mean')

        print(f"[OK] Risk-free rate loaded: {len(df_rf)} rows")
        print(f"   Period: {df_rf['date'].min().strftime('%Y-%m-%d')} ~ "
              f"{df_rf['date'].max().strftime('%Y-%m-%d')}")
        print(f"   Average Rate: {df_rf['rf_annual'].mean():.2f}%")
        print(f"   Rate Range: {df_rf['rf_annual'].min():.2f}% ~ "
              f"{df_rf['rf_annual'].max():.2f}%")

        return df_rf

    def _resample_rf_data(self, df: pd.DataFrame, freq: str = 'W', agg: str = 'mean') -> pd.DataFrame:
#일주일 간 결측치 제외  평균으로 주간 금리 계산
        df = df.copy()
        df = df.set_index('date')

        if freq == 'W':
            if agg == 'mean':
            
                df_resampled = df.resample('W-FRI').mean()
            else:
                df_resampled = df.resample('W-FRI').last()
        elif freq == 'M':
            if agg == 'mean':
                df_resampled = df.resample('ME').mean()
            else:
                df_resampled = df.resample('ME').last()
        else:
            return df.reset_index()

        df_resampled = df_resampled.dropna(subset=['rf_annual'])
        df_resampled = df_resampled.reset_index()

        return df_resampled

    def get_risk_free_rate_with_regime(self,
                                       start_date: str,
                                       end_date: str,
                                       source: str = 'CD91') -> pd.DataFrame:
        df_rf = self.get_risk_free_rate(start_date, end_date, source, freq='D')

        # 레짐명 매핑
        regime_names = {0: '초저금리', 1: '저금리', 2: '중립', 3: '고금리'}
        df_rf['rf_regime_name'] = df_rf['rf_regime'].map(regime_names)

        # 추세 매핑
        trend_names = {-1: '하락', 0: '횡보', 1: '상승'}
        df_rf['rf_trend'] = df_rf['rf_direction'].map(trend_names)

        # 변동성 레짐(중앙값 기준 1.5 이상이면 불안정)
        vol_median = df_rf['rf_std20'].median()
        df_rf['rf_volatility_regime'] = pd.cut(
            df_rf['rf_std20'],
            bins=[-np.inf, vol_median * 0.5, vol_median * 1.5, np.inf],
            labels=['안정', '보통', '불안정']
        )

        return df_rf

    def get_multiple_rates(self,
                           start_date: str,
                           end_date: str,
                           sources: List[str] = None,
                           freq: str = 'W') -> pd.DataFrame:

        print("⚠️ 현재 엑셀 파일 기반으로 TB3Y(3년물 국고채)만 지원됩니다.")
        
        df = self.get_risk_free_rate(start_date, end_date, source='TB3Y', freq=freq)
        if df is not None:
            df = df[['date', 'rf_annual']].rename(columns={'rf_annual': 'rate_TB3Y'})
        
        return df


    def _normalize_date_yyyymmdd(self, date_str: str) -> str:
    #날짜 형식을 YYYYMMDD로 정규화
        date_str = str(date_str).replace('-', '').replace('/', '').replace('.', '')
        if len(date_str) == 8:
            return date_str
        else:
            raise ValueError(f"잘못된 날짜 형식: {date_str}")

    
    # 유틸리티 메서드

    @staticmethod
    def is_null(value) -> bool:
        if value is None:
            return True
        if pd.isnull(value):
            return True
        try:
            if np.isnan(value):
                return True
        except:
            pass
        return False

    def INSERT_RES(self, data: pd.DataFrame, table_name: str, header: int = None) -> int:
        if header is None:
            columns = list(data.columns)
        else:
            columns = list(data.iloc[header])

        query = f"INSERT INTO {table_name}\n("
        query += ", ".join([f"[{col}]" for col in columns])
        query += ")\n"

        cnt = 0
        for i, row in data.iterrows():
            insert_query = query + "VALUES ("
            values = []
            for val in row.tolist():
                if pd.isnull(val) or str(val) in ["nan", "Nan", "NaN"]:
                    values.append("NULL")
                else:
                    values.append(f"'{val}'")
            insert_query += ", ".join(values)
            insert_query += ");\nSELECT @@ROWCOUNT RC;"

            self._cursor.execute(insert_query)
            res = self._cursor.fetchone()
            cnt += res[0]
            self._conn.commit()

        return cnt

    def UPDATE_IF_ROWCOUNT_0_INSERT(self,
                                     data: pd.DataFrame,
                                     table_name: str,
                                     primary_key: str = None,
                                     header: int = None) -> None:
        if primary_key is None:
            raise ValueError("primary_key 파라미터가 필요합니다.")

        if header is None:
            columns = list(data.columns)
            start_num = 0
            pk_idx = columns.index(primary_key)
        else:
            columns = list(data.iloc[header])
            start_num = header + 1
            pk_idx = columns.index(primary_key)

        insert_table_query = f"INSERT INTO {table_name}\n(\n"

        for n in range(len(data))[start_num:]:
            main_query = f"UPDATE {table_name}\nSET\n"
            insert_value_query = "VALUES ("
            row = list(data.iloc[n])

            for i, key in enumerate(columns):
                if n == start_num:
                    insert_table_query += f"[{key}]"
                    if i < (len(columns) - 1):
                        insert_table_query += ", "
                    else:
                        insert_table_query += ")"

                if pd.isnull(row[i]):
                    value = "NULL"
                elif "'" in str(row[i]):
                    value = str(row[i]).replace("'", "''")
                else:
                    value = str(row[i])

                if pd.isnull(row[i]):
                    main_query += f"[{key}] = {value}"
                    insert_value_query += f"{value}"
                else:
                    main_query += f"[{key}] = '{value}'"
                    insert_value_query += f"'{value}'"

                if i < (len(columns) - 1):
                    main_query += ", \n"
                    insert_value_query += ", "
                else:
                    main_query += "\n"
                    insert_value_query += ")"

            main_query += f"WHERE\n[{primary_key}] = '{row[pk_idx]}'\n"
            main_query += "IF @@ROWCOUNT=0\n"
            main_query += f"{insert_table_query}\n{insert_value_query}"

            self._cursor.execute(main_query)
            self._conn.commit()

    def INSERT_IF_DOESNT_EXIST_FOR_A_COLUMN(self,
                                             data: pd.DataFrame,
                                             insert_table: str,
                                             db_data: pd.DataFrame = None,
                                             query: str = None,
                                             column: str = None) -> None:
        if column is None:
            raise ValueError("column 파라미터가 필요합니다.")
        elif isinstance(column, str):
            column = [column]

        if isinstance(query, str):
            in_db_data = self.SELECT(query)
        elif db_data is not None:
            in_db_data = db_data
        else:
            raise ValueError("db_data 또는 query 파라미터가 필요합니다.")

        if in_db_data is None:
            insert_df = data
        else:
            in_db_data['_Check'] = True
            insert_df = pd.merge(data, in_db_data[column + ['_Check']], on=column, how='left')
            insert_df = insert_df[insert_df['_Check'] != True]
            insert_df = insert_df.drop(columns=['_Check'])

        if insert_df is None or len(insert_df) == 0:
            print("All data already inserted")
        else:
            self.INSERT(insert_df, insert_table)

    def _add_row_number(self, base_query: str, *cols) -> str:
        order_by_clause = ", ".join(cols) if cols else "(SELECT NULL)"
        return f"""
            WITH NumberedData AS (
                SELECT *, ROW_NUMBER() OVER (ORDER BY {order_by_clause}) AS row_num
                FROM ({base_query}) AS subquery
            )
        """

    def _fetch_data_by_row_number(self, cursor, base_query: str, start: int, end: int, idx: int, *cols) -> pd.DataFrame:
        print(f"Fetching chunk {idx}: rows {start} to {end}")

        query = f"""
            {self._add_row_number(base_query, *cols)}
            SELECT * FROM NumberedData
            WHERE row_num BETWEEN {start} AND {end}
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        columns = [column[0] for column in cursor.description]

        return pd.DataFrame.from_records(rows, columns=columns)

    def select_large_data_by_range_parallel(self,
                                             base_query: str,
                                             chunk_size: int = 100000,
                                             max_workers: int = 4,
                                             *cols) -> pd.DataFrame:
        start_time = time.time()

        total_rows_query = f"""
            {self._add_row_number(base_query, *cols)}
            SELECT MAX(row_num) FROM NumberedData
        """
        self._cursor.execute(total_rows_query)
        total_rows = self._cursor.fetchone()[0]

        if not total_rows:
            return pd.DataFrame()

        print(f"Total rows: {total_rows}")

        ranges = [
            (start, min(start + chunk_size - 1, total_rows))
            for start in range(1, total_rows + 1, chunk_size)
        ]

        dfs = []
        with self._conn.cursor() as cursor:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(self._fetch_data_by_row_number, cursor, base_query, r[0], r[1], idx + 1, *cols)
                    for idx, r in enumerate(ranges)
                ]
                for idx, future in enumerate(futures):
                    df_chunk = future.result()
                    dfs.append(df_chunk)
                    print(f"Processed range chunk {idx + 1}/{len(ranges)}... ({len(df_chunk)} rows)")

        final_df = pd.concat(dfs, ignore_index=True)
        elapsed = time.time() - start_time
        print(f"Total execution time: {elapsed:.2f} seconds")

        return final_df

    def _fetch_data_by_condition(self, base_query: str, condition: Dict) -> pd.DataFrame:
        def format_condition(key, value):
            if isinstance(value, (list, set, tuple)):
                return f"[{key}] IN ({', '.join(map(str, value))})"
            else:
                return f"[{key}] = '{value}'"

        str_condition = " AND ".join(format_condition(k, v) for k, v in condition.items()) if condition else ""

        base_query = base_query.strip()
        has_where = bool(re.search(r'\bWHERE\b', base_query, re.IGNORECASE))
        query = f"{base_query} {'AND' if has_where else 'WHERE'} {str_condition}" if str_condition else base_query

        try:
            with self._conn.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                return pd.DataFrame.from_records(rows, columns=columns)
        except Exception as e:
            print(f"SQL Execution Error: {e}")
            return pd.DataFrame()

    def select_large_data_by_conditions_parallel(self,
                                                  base_query: str,
                                                  max_workers: int = 4,
                                                  **conditions) -> pd.DataFrame:
        start_time = time.time()
        dfs = []

        keys, values = zip(*[(k, v if isinstance(v, (list, set, tuple)) else [v]) for k, v in conditions.items()])
        expanded_conditions = [dict(zip(keys, value_combination)) for value_combination in product(*values)]

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_condition = {
                executor.submit(self._fetch_data_by_condition, base_query, condition): condition
                for condition in expanded_conditions
            }

            for future in as_completed(future_to_condition, timeout=30):
                condition = future_to_condition[future]
                try:
                    df_chunk = future.result()
                    if not df_chunk.empty:
                        dfs.append(df_chunk)
                    print(f"Processed condition {condition}... ({len(df_chunk)} rows)")
                except Exception as e:
                    print(f"Error processing condition {condition}: {e}")

        final_df = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
        elapsed = time.time() - start_time
        print(f"Total execution time: {elapsed:.2f} seconds")

        return final_df


# DB 인스턴스 생성 헬퍼 함수

def get_trstdev_db() -> MSSQL:
    """TRSTDEV 데이터베이스 연결"""
    return MSSQL(database=DBConfig.TRSTDEV_DB)


def get_trustquant_db() -> MSSQL:
    """TRUSTQUANTDB 데이터베이스 연결 (RL용)"""
    return MSSQL(database=DBConfig.TRUSTQUANT_DB)


def get_wfns_db() -> MSSQL:
    """WFNS2DB 데이터베이스 연결"""
    return MSSQL(database=DBConfig.WFNS_DB)



# 테스트 코드


if __name__ == '__main__':
    print("=" * 60)
    print("MSSQL 데이터베이스 연결 테스트")
    print("=" * 60)

    db = MSSQL()
    print("✅ 데이터베이스 연결 성공")

    # Risk-free rate 테스트 (엑셀 파일)
 
    print("\n" + "=" * 60)
    print("7. Risk-Free Rate 조회 테스트 (엑셀 파일)")
    print("=" * 60)

    try:
        # 3년물 국고채 조회 (일간)
        print("\n[3년물 국고채 (일간)]")
        df_rf = db.get_risk_free_rate(
            start_date='2024-01-01',
            end_date='2025-12-31',
            source='TB3Y',
            freq='D'
        )
        print(df_rf.head(10))
        print(f"\n컬럼: {list(df_rf.columns)}")

        # 주간 리샘플링 (평균값)
        print("\n[3년물 국고채 (주간 평균)]")
        df_rf_weekly = db.get_risk_free_rate(
            start_date='2024-01-01',
            end_date='2025-12-31',
            source='TB3Y',
            freq='W'
        )
        print(df_rf_weekly.head(10))

        # 레짐 정보 포함
        print("\n[금리 레짐 정보]")
        df_rf_regime = db.get_risk_free_rate_with_regime(
            start_date='2024-01-01',
            end_date='2025-12-31',
            source='TB3Y'
        )
        print(df_rf_regime[['date', 'rf_annual', 'rf_regime_name', 'rf_trend']].head(20))

        # 금리 레짐별 통계
        print("\n[금리 레짐별 통계]")
        regime_stats = df_rf_regime.groupby('rf_regime_name')['rf_annual'].agg(['mean', 'count'])
        print(regime_stats)

    except Exception as e:
        import traceback
        print(f"⚠️ Risk-free rate 테스트 실패: {e}")
        traceback.print_exc()

    print("\n" + "=" * 60)
    print("2. 팩터 수익률 조회")
    print("=" * 60)

    print("\n[Value 팩터 수익률]")
    df_value = db.get_factor_returns(factor_name='Value', limit=10)
    if df_value is not None:
        print(df_value)

    print("\n" + "=" * 60)
    print("3. 지수 데이터 조회")
    print("=" * 60)

    print("\n[KOSPI200 지수]")
    df_k200 = db.get_kospi200(start_date='20240101', limit=10)
    if df_k200 is not None:
        print(df_k200)
    # ==========================================
    # Regime 데이터 테스트
    # ==========================================
    print("\n" + "=" * 60)
    print("4. Regime 데이터 조회")
    print("=" * 60)

    try:
        # 전체 레짐 데이터 조회
        print("\n[REGIME_QMS 전체 조회 (최근 10건)]")
        df_regime = db.get_regime_qms(
            start_date='2024-01-01',
            end_date='2025-12-31',
            limit=10
        )
        if df_regime is not None:
            print(df_regime)
            print(f"\n컬럼: {list(df_regime.columns)}")
        else:
            print("데이터 없음")

        # 레짐 코드별 데이터 조회
        print("\n[레짐 코드별 상세 조회]")
        regime_codes = {
            'RG00101': 'DRAI',
            'RG00211': 'MACRO_GROWTH',
            'RG00311': 'MACRO_INFLATION'
        }
        
        query = """
        SELECT TOP 30
            LookBackDate as date,
            RegimeCode,
            STATES,
            PROB_POSITIVE,
            PROB_NEUTRAL,
            PROB_NEGATIVE
        FROM REGIME_QMS
        WHERE RegimeCode IN ('RG00101', 'RG00211', 'RG00311')
          AND LookBackDate BETWEEN '2024-01-01' AND '2025-12-31'
          AND RECENT = 1
        ORDER BY LookBackDate DESC, RegimeCode
        """
        df_regime_detail = db.SELECT(query)
        
        if df_regime_detail is not None:
            # 레짐 코드명 매핑
            df_regime_detail['RegimeName'] = df_regime_detail['RegimeCode'].map(regime_codes)
            print(df_regime_detail)
            
            # 레짐별 상태 분포
            print("\n[레짐별 상태(STATES) 분포]")
            for code, name in regime_codes.items():
                regime_data = df_regime_detail[df_regime_detail['RegimeCode'] == code]
                if len(regime_data) > 0:
                    print(f"\n  {name} ({code}):")
                    state_counts = regime_data['STATES'].value_counts().sort_index()
                    for state, count in state_counts.items():
                        print(f"    State {state}: {count}건")
        else:
            print("레짐 상세 데이터 없음")

        # 피벗 형태로 변환
        print("\n[날짜별 레짐 상태 (피벗)]")
        query_pivot = """
        SELECT 
            LookBackDate as date,
            RegimeCode,
            STATES
        FROM REGIME_QMS
        WHERE RegimeCode IN ('RG00101', 'RG00211', 'RG00311')
          AND LookBackDate BETWEEN '2024-06-01' AND '2024-12-31'
          AND RECENT = 1
        ORDER BY LookBackDate DESC, RegimeCode
        """
        df_pivot_raw = db.SELECT(query_pivot)
        
        if df_pivot_raw is not None:
            df_pivot_raw['STATES'] = pd.to_numeric(df_pivot_raw['STATES'], errors='coerce')
            df_pivot = df_pivot_raw.pivot_table(
                index='date',
                columns='RegimeCode',
                values='STATES',
                aggfunc='first'
            ).reset_index()
            df_pivot = df_pivot.rename(columns=regime_codes)
            df_pivot = df_pivot.sort_values('date', ascending=False)
            print(df_pivot.head(20))
            
            # 레짐 상태 설명
            print("\n[레짐 상태 설명]")
            print("  DRAI: 0=Risk-On, 1=Risk-Off")
            print("  MACRO_GROWTH: 0=Contraction, 1=Recovery, 2=Expansion, 3=Slowdown")
            print("  MACRO_INFLATION: 0=Deflation, 1=Low, 2=Moderate, 3=High")
        else:
            print("피벗 데이터 없음")

    except Exception as e:
        import traceback
        print(f"⚠️ Regime 데이터 테스트 실패: {e}")
        traceback.print_exc()

    print("\n" + "=" * 60)
    print("5. 코드 매핑 정보")
    print("=" * 60)

    print("\n[팩터 코드 매핑]")
    for code, name in FACTOR_MAPPING.items():
        print(f"  {code:8} → {name}")

    print("\n[지수 코드 매핑]")
    for code, name in INDEX_MAPPING.items():
        print(f"  {code:8} → {name}")

    db.close()
#python util/database2.py