# -*- coding: utf-8 -*-

import os, sys
sys.path.append(os.path.join(r'C:\Projects','truston_quant_dev'))
import re
import time
import pymssql
import oracledb
# import cx_Oracle
import datetime
import pandas as pd
import numpy as np
import collections
# from konlpy.utils import pprint
from util import variables as v
import logging
import requests
import json
from typing import *
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from _LJH.project_global_theme_rotation.config.settings import Config
from _LJH.project_global_theme_rotation.utils.calendar_utils import *
from _LJH.project_global_theme_rotation.data.data_processor import BaseDataProcessor
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import product


class MSSQL:
    def __init__(self, server=v.trst_server, id=v.trst_id, pw=v.trst_pw, db=v.trstdb):
        self.server = server
        self.id = id
        self.pw = pw
        self.db = db
        self._connect()

    def __del__(self):
        return

    def close(self):
        self._conn.close()
        self._conn_cp.close()


    def _connect(self):
        self._conn = pymssql.connect(server=self.server, user=self.id, password=self.pw, database=self.db, charset='utf8')
        self._cursor = self._conn.cursor()

        self._conn_cp = pymssql.connect(server=self.server, user=self.id, password=self.pw, database=self.db, charset='CP949')
        self._cursor_cp = self._conn_cp.cursor()

    def SELECT(self, Query:str, printYN:bool=False, output_type:str="DataFrame", index_col=None) -> Optional[pd.DataFrame]:
        '''
        :param Query: 쿼리를 string 형태로 입력
        :param printYN: 쿼리와 결과를 창에 출력할지
        :param output_type: list, array, pandas DataFrame, pandas Series 형태로 return 가능함
        :return: 결과
        '''
        if not output_type == "DataFrame" and index_col is not None:
            raise TypeError("If you want to use input variable 'index_col', 'output_type' must be 'DataFrame'")
        self._cursor.execute(Query)
        row = self._cursor.fetchone()
        desc_list = self._cursor.description
        desc_list = list(desc_list)
        header_str = ""
        header_list = []
        for desc in desc_list:
            desc = desc[0]
            header_list.append(desc)
            desc = str(desc)
            header_str = header_str + desc + '\t'
        if printYN:
            print(header_str)
        data_mat = []
        while row:
            row = list(row)
            row_list = []
            row_str = ""
            for r in row:
                try:
                    if isinstance(r, str):
                        r = r.encode('ISO-8859-1')  # 한글깨짐수정과정
                        r = r.decode('euc-kr')
                except:
                    pass
                row_list.append(r)
                r = str(r)
                row_str = row_str + r + '\t'

            if printYN:
                print(row_str)
            data_mat.append(row_list)
            row = self._cursor.fetchone()
        if output_type == "old_version":
            return data_mat
        data_array = np.array(data_mat)
        data_array = data_array.transpose()
        if len(data_array) == 0:
            return None
        data_dict = collections.OrderedDict()
        for i in range(len(header_list)):
            data_dict[header_list[i]] = data_array[i]
        data_df = pd.DataFrame(data_dict)
        if output_type == "DataFrame":
            columns = data_df.keys().to_list()
            if index_col is not None:
                if  index_col not in columns:
                    raise ValueError("A column 'index_col' is not in selected data set.")
                else:
                    columns.remove(index_col)
                    data_df = data_df.set_index(index_col)
            if len(columns) == 1:
                data_srs = data_df.loc[:, columns[0]]
                rlt = data_srs
            else:
                rlt = data_df
        elif output_type == "array":
            rlt = data_array
        elif output_type == "list":
            rlt = list(data_array)
        else:
            raise ValueError("Wrong output type")
        return rlt

    def DELETE(self, table, where=None) -> None:
        if where is None:
            print("example of where : where='BaseDate = '2020-01-01' AND LAG = '0'")
            raise SystemError(
                f"If you want to delete ALL DATA in the table '{table}', "
                f"please use database tool(ex. sql Server Mgmt Studio)")

        query = f"DELETE FROM {table} WHERE {where}"
        print(query)
        self._cursor.execute(query)
        self._conn.commit()

    def DELETE_RES(self, table, where=None, print_tf=False) -> int:
        if where is None:
            print("example of where : where='BaseDate = '2020-01-01' AND LAG = '0'")
            raise SystemError(
                f"If you want to delete ALL DATA in the table '{table}', "
                f"please use database tool(ex. sql Server Mgmt Studio)")

        query = f"DELETE FROM {table} WHERE {where}"
        if print_tf:
            print(query)
        self._cursor.execute(query)
        rows_affected = self._cursor.rowcount
        self._conn.commit()

        return rows_affected

    def INSERT(self, Data, table_name, header=None, print_query=False) -> None:
        '''
        :param Data: pandas DataFrame
        :param table_name: 테이블 이름
        :param header: 만약 Data의 컬럼명이 테이블의 각 컬럼명이라면 None, Data의 n번째행이 컬럼명이라면 header=n
        '''
        try:
            str_sql = "INSERT INTO "
            str_sql += table_name + " \n"
            if header is None:
                columns = list(Data.keys())
                start_num = 0
            else:
                columns = list(Data.iloc[header])
                start_num = header + 1
            str_sql += "("
            for i, key in enumerate(columns):
                str_sql += "[" + key + "]"
                if i < (len(Data.keys()) - 1):
                    str_sql += ", "
            str_sql += ")\n"
            Data.reset_index(drop=True, inplace=True)
            idx = list(Data.index)
            idx = idx[start_num:]
            for idx_num in idx:
                row = list(Data.loc[idx_num])
                Insert_Query_by_row = str_sql + "VALUES ("
                for j, col in enumerate(columns):
                    if pd.isnull(row[j]) or row[j] in ["nan", "Nan", "NaN"]:
                        check_nan = True
                    else:
                        check_nan = False
                    if check_nan:
                        Insert_Query_by_row += "NULL"
                    elif "'" in str(row[j]):
                        single_quotation_replaced_txt = str(row[j])
                        single_quotation_replaced_txt = single_quotation_replaced_txt.replace("'", "''")
                        Insert_Query_by_row += "'" + single_quotation_replaced_txt + "'"
                    else:
                        Insert_Query_by_row += "'" + str(row[j]) + "'"
                    if j < (len(columns) - 1):
                        Insert_Query_by_row += ", "
                Insert_Query_by_row += ")\n"
                if print_query:
                    print(Insert_Query_by_row)
                self._cursor.execute(Insert_Query_by_row)
                self._conn.commit()
            return True

        except Exception as ex:
            print(ex)
            print('ERROR Row: %d' % idx_num)
            print('ERROR Data: %s' % Data.loc[idx_num])
            print('ERROR query: %s' % Insert_Query_by_row)
            # self.comm.tl_send_msg(txt)
            # self.TM.send_message(txt, channel=v.tid_ctrl)
            return False

    def INSERT_RES(self, Data, tb_nm, header=None):
        '''
        :param Data: pandas DataFrame
        :param table_name: 테이블 이름
        :param header: 만약 Data의 컬럼명이 테이블의 각 컬럼명이라면 None, Data의 n번째행이 컬럼명이라면 header=n
        '''
        Query = "INSERT INTO %s\n(" % (tb_nm)
        if header is None:
            columns = list(Data.keys())
        else:
            columns = list(Data.iloc[header])
        for key in columns:
            Query += "[" + key + "], "
        Query = Query[:-2]
        Query += ")\n"
        cnt = 0
        for i, row in Data.iterrows():
            Insert_Query_by_row = Query + "VALUES ("
            for j in row.tolist():
                if (pd.isnull(j) or j in ["nan", "Nan", "NaN"]):
                    Insert_Query_by_row += "NULL, "
                else:
                    Insert_Query_by_row += "'%s', " % (j)
            Insert_Query_by_row = Insert_Query_by_row[:-2]
            Insert_Query_by_row += ");\nSELECT @@ROWCOUNT RC;"
            self._cursor.execute(Insert_Query_by_row)
            res = self._cursor.fetchone()
            cnt += res[0]
            self._conn.commit()
        return cnt

    def UPDATE_IF_ROWCOUNT_0_INSERT(self, Data, table_name, Primary_Key=None, header=None):
        if Primary_Key is None:
            raise ValueError("Primary_Key=None\nNeed to Input Primary Key column name as a string.")
        if header is None:
            columns = list(Data.keys())
            start_num = 0
            primary_key_idx = columns.index(Primary_Key)
        else:
            columns = list(Data.iloc[header])
            start_num = header + 1
            primary_key_idx = columns.index(Primary_Key)
        insert_table_query = "INSERT INTO %s\n(\n" % table_name
        for n in range(len(Data))[start_num:]:
            main_query = "UPDATE %s\nSET\n" % table_name
            insert_value_query = "VALUES ("
            row = list(Data.iloc[n])
            for i, key in enumerate(columns):
                if n == 0:
                    insert_table_query += "[%s]" % key
                    if i < (len(columns) - 1):
                        insert_table_query += ", "
                    else:
                        insert_table_query += ")"
                if pd.isnull(row[i]):
                    value = "NULL"
                elif "'" in str(row[i]):
                    value = str(row[i])
                    value = value.replace("'", "''")
                else:
                    value = str(row[i])
                if pd.isnull(row[i]):
                    main_query += "[%s] = %s" % (key, value)
                    insert_value_query += "%s" % value
                else:
                    main_query += "[%s] = '%s'" % (key, value)
                    insert_value_query += "'%s'" % value
                if i < (len(columns) - 1):
                    main_query += ", \n"
                    insert_value_query += ", "
                else:
                    main_query += "\n"
                    insert_value_query += ")"
            main_query += "WHERE\n[%s] = '%s'\n" % (Primary_Key, row[primary_key_idx])
            main_query += "IF @@ROWCOUNT=0\n"                                   ## @@ROWCOUNT가 ZERO라면
            main_query += "%s\n%s" % (insert_table_query, insert_value_query)   ## INSERT한다
            self._cursor.execute(main_query)
            self._conn.commit()

    def INSERT_IF_DOESNT_EXIST_FOR_A_COLUMN(self, data, insert_table, DB_data=None, query=None, column=None):
        if column is None:
            raise ValueError("Need column variable for comparing two DataFrames")
        elif isinstance(column, str):
            column = [column]
        if isinstance(query, str):
            InDB_data = self.SELECT(query)
        elif DB_data is not None:
            InDB_data = DB_data
        else:
            raise ValueError("One of the InDB_data or the query variable are required.\nInDB_data:pd.DataFrame, query:str")
        if InDB_data is None:
            insert_df = data
        else:
            InDB_data['Check'] = True
            insert_df = pd.merge(data, InDB_data[column + ['Check']], on=column, how='left')
            insert_df = insert_df[insert_df['Check'] != True]
            insert_df = insert_df.drop(columns=['Check'])
        if insert_df is None:
            print("All data already INSERTed")
        else:
            self.INSERT(insert_df, insert_table)

    def run_query_str(self, str_sql):
        # string type query 실행(UPDATE, EXEC PROC, ...)
        try:
            self._cursor.execute(str_sql+'; select @@rowcount rc;')
            row = self._cursor.fetchone()
            self._conn.commit()
        except Exception as e:
            logging.error(f'Failed: {e}')
            return False
        # self._conn.close()
        return row[0]

    def select_stockprice_dataframetype(self, start, end, ComCode, volume=False, WK_END=False):
        '''
        WFN DB에 있는 시계열 회사가격데이터를 간단히 불러옵니다
        zipline 사용용도임
        index는 date
        각각행은 순서대로 고가, 저가, 시가, 종가, 수정종가 순으로

        :param start: 시작일(datetime)
        :param end: 종료일(datetime)
        :param ComCode: 6자리 회사코드
        :return: 날짜와 고저시종수 가격 len(날짜)x6 matrix
        '''
        # if self.db != "WFNS2DB":
        #     raise ValueError("WFNS2DB로 접속해야합니다.")

        Query = com_adj_price(start, end, ComCode, WK_END=WK_END)

        df_mat = self.SELECT(Query, output_type="old_version")
        date_list = []
        Close_list = []
        High_list = []
        Low_list = []
        Open_list = []
        Volume = []
        for data in df_mat[1:]:
            date_list.append(pd.Timestamp(datetime.datetime.strptime(data[1], "%Y%m%d").date()))
            Close_list.append(float(data[2]))
            High_list.append(float(data[3]))
            Low_list.append(float(data[4]))
            Open_list.append(float(data[5]))
            Volume.append(float(data[6])) if volume else None

        df_mat = {"Date": date_list,
                  "High": High_list,
                  "Low": Low_list,
                  "Open": Open_list,
                  "Close": Close_list,
                  }
        columns = ["Date", "Open", "High", "Low", "Close"]
        # columns = ["Date", "Close"]

        if volume:
            df_mat["Volume"] = Volume
            columns.append("Volume")

        df_mat = pd.DataFrame(df_mat, columns=columns)

        return df_mat

    @staticmethod
    def isNull(value):
        if pd.isnull(value) or np.isnan(value) or value is None:
            return True
        else:
            return False


    #################################################################################################################
    ############################################# 대용량데이터 병렬처리 함수 ##########################################
    #################################################################################################################
    def add_row_number(self, base_query, *cols):
        order_by_clause = ", ".join(cols) if cols else "(SELECT NULL)"
        return f"""
            WITH NumberedData AS (
                SELECT *, ROW_NUMBER() OVER (ORDER BY {order_by_clause}) AS row_num
                FROM ({base_query}) AS subquery
            )
        """

    def fetch_data_by_row_number(self, cursor, base_query, start, end, index, *cols):
        print(f"Fetching chunk {index}: rows {start} to {end}")

        row_number_query = f"""
            {self.add_row_number(base_query, *cols)}
            SELECT * FROM NumberedData
            WHERE row_num BETWEEN {start} AND {end}
        """
        cursor.execute(row_number_query)
        rows = cursor.fetchall()
        columns = [column[0] for column in cursor.description]

        return pd.DataFrame.from_records(rows, columns=columns)

    def select_large_data_by_range_parallel(self, base_query, chunk_size=100000, max_workers=4, *cols):
        start_time = time.time()

        total_rows_query = f"""
            {self.add_row_number(base_query, *cols)}
            SELECT MAX(row_num) FROM NumberedData
        """
        self._cursor.execute(total_rows_query)
        total_rows = self._cursor.fetchone()[0]

        ranges = [(start, min(start + chunk_size - 1, total_rows)) for start in range(1, total_rows + 1, chunk_size)]

        dfs = []
        with self._conn.cursor() as cursor:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(self.fetch_data_by_row_number, cursor, base_query, r[0], r[1], idx + 1, *cols)
                    for idx, r in enumerate(ranges)
                ]
                for idx, future in enumerate(futures):
                    df_chunk = future.result()
                    dfs.append(df_chunk)
                    print(f"Processed range chunk {idx + 1}/{len(ranges)}... ({len(df_chunk)} rows)")

        final_df = pd.concat(dfs, ignore_index=True)
        elapsed_time = time.time() - start_time
        print(f"Total execution time: {elapsed_time:.2f} seconds")

        return final_df


    def fetch_data_by_condition(self, base_query, condition):
        """각 스레드에서 독립적인 커서를 생성하여 SQL을 실행하는 함수"""

        # ✅ SQL 조건문 생성 함수
        def format_condition(key, value):
            if isinstance(value, (list, set, tuple)):
                return f"[{key}] IN ({', '.join(map(str, value))})"
            else:
                return f"[{key}] = '{value}'"

        str_condition = " AND ".join(format_condition(k, v) for k, v in condition.items()) if condition else ""

        # ✅ WHERE 절 존재 여부 확인 후 적절히 추가
        base_query = base_query.strip()
        has_where = bool(re.search(r'\bWHERE\b', base_query, re.IGNORECASE))
        query = f"{base_query} {'AND' if has_where else 'WHERE'} {str_condition}" if str_condition else base_query

        print(f"Executing Query: {query}")  # 🔍 디버깅용

        try:
            with self._conn.cursor() as cursor:  # ✅ 각 스레드에서 개별 커서 사용
                cursor.execute(query)
                rows = cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                return pd.DataFrame.from_records(rows, columns=columns)
        except Exception as e:
            print(f"❌ SQL Execution Error: {e}")
            return pd.DataFrame()  # 오류 발생 시 빈 DataFrame 반환

    def select_large_data_by_conditions_parallel(self, base_query, max_workers=4, **conditions):
        """각 조건을 병렬로 실행하여 데이터를 조회하는 함수"""
        start_time = time.time()
        dfs = []

        # ✅ 조건 조합 생성
        keys, values = zip(*[(k, v if isinstance(v, (list, set, tuple)) else [v]) for k, v in conditions.items()])
        expanded_conditions = [dict(zip(keys, value_combination)) for value_combination in product(*values)]

        # ✅ 개별 커서를 사용하기 위해 커서를 공유하지 않음
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_condition = {
                executor.submit(self.fetch_data_by_condition, base_query, condition): condition
                for condition in expanded_conditions
            }

            for future in as_completed(future_to_condition, timeout=30):  # ✅ 타임아웃 추가 (30초)
                condition = future_to_condition[future]
                try:
                    df_chunk = future.result()  # ✅ 여기서 예외 발생 시 즉시 catch
                    if not df_chunk.empty:
                        dfs.append(df_chunk)
                    print(f"✅ Processed data chunk... ({len(df_chunk)} rows)")
                except Exception as e:
                    print(f"❌ Error processing query for condition {condition}: {e}")

        final_df = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
        elapsed_time = time.time() - start_time
        print(f"⏳ Total execution time: {elapsed_time:.2f} seconds")

        return final_df

    ################################################################################################################
    ######################################### End of 대용량데이터 병렬처리 함수 ######################################
    ################################################################################################################


    def fetch_large_data(self, query, chunk_size=100000, printYN = False):
        '''
        Fetches large amounts of data from a database in manageable chunks.

        Args:
            query (str): The SQL query to execute on the database.
            chunk_size (int, optional): The number of rows to fetch per chunk.
                                        Defaults to 100,000.

        Yields:
            pd.DataFrame: A DataFrame containing a chunk of the fetched data.

        Notes:
            - This method is useful for handling large datasets that cannot be
              loaded into memory all at once. It fetches data in chunks and
              yields them one at a time.
            - The connection cursor is closed after all chunks are fetched.

        Example:
            query = "SELECT * FROM large_table"
            for chunk in fetch_large_data(query):
                # Process each chunk of data
                process(chunk)

        '''
        # Execute the query
        self._cursor_cp.execute(query)

        # Initialize a counter for the number of chunks processed
        chunk_count = 0

        # Fetch data in chunks
        while True:
            chunk = self._cursor_cp.fetchmany(chunk_size)
            chunk_count += 1
            if printYN :
                print(f"Processed chunk {chunk_count}...")
            if not chunk:
                break
            yield pd.DataFrame(chunk, columns=[column[0] for column in self._cursor_cp.description])

        # Close the connection
        self._cursor_cp.close()

    def select_large_data(self, query, chunk_size=100000, printYN=False):
        '''
        Args:
            query: SQL query to execute.
            chunk_size: Number of rows to fetch per chunk.

        Returns:
            final_df: A concatenated DataFrame of all fetched chunks.

        Returns:

        '''
        # Create an empty list to store DataFrames
        dfs = []

        # Fetch data in chunks
        for chunk_df in self.fetch_large_data(query, chunk_size):
            dfs.append(chunk_df)
            if printYN :
                print(f"Processed {len(dfs) * chunk_size} rows...")

        # Combine all DataFrames
        final_df = pd.concat(dfs, ignore_index=True)

        return final_df


    # @ray.remote
    def fetch_chunk(self, query, offset, chunk_size):
        """
        Fetch a chunk of data from the database using an existing MSSQL connection.

        Args:
            query (str): SQL query with a placeholder for LIMIT and OFFSET.
            offset (int): Starting row for this chunk.
            chunk_size (int): Number of rows to fetch.

        Returns:
            pd.DataFrame: A DataFrame containing the fetched data.
        """
        try:
            # Format query with LIMIT and OFFSET
            paginated_query = f"{query} OFFSET {offset} ROWS FETCH NEXT {chunk_size} ROWS ONLY"
            self._cursor.execute(paginated_query)

            # Fetch data and convert to DataFrame
            data = self._cursor.fetchall()
            columns = [col[0] for col in self._cursor.description]
            return pd.DataFrame(data, columns=columns)

        except Exception as e:
            print(f"Error in fetch_chunk: {e}")
            return pd.DataFrame()

    def fetch_large_data_parallel(self, query, chunk_size=100000):
        """
        mssql이 ray 직렬화가 안 된다 ====> 사용불가
        Fetch large amounts of data using Ray for parallel processing without requiring total_rows.

        Args:
            query (str): SQL query to fetch data.
            chunk_size (int): Number of rows to fetch per chunk (default: 100,000).

        Returns:
            pd.DataFrame: Combined DataFrame containing all fetched chunks.
        """
        # Step 1: Calculate total rows dynamically
        count_query = f"SELECT COUNT(*) FROM ({query}) AS total_count"
        self._cursor.execute(count_query)
        total_rows = self._cursor.fetchone()[0]

        print(f"Total rows: {total_rows}")

        # Step 2: Generate offsets for each chunk
        offsets = list(range(0, total_rows, chunk_size))

        # Initialize Ray
        ray.init(ignore_reinit_error=True)

        # Launch Ray tasks
        futures = [
            self.fetch_chunk.remote(self, query, offset, chunk_size)
            for offset in offsets
        ]

        # Collect results from all Ray workers
        chunks = ray.get(futures)

        # Combine all chunks into a single DataFrame
        combined_df = pd.concat(chunks, ignore_index=True)

        # Shutdown Ray
        ray.shutdown()

        return combined_df


class ORACLE:
    def __init__(self, server=v.hana_server, id=v.hana_ID, pw=v.hana_pw):
        self.server = server
        self.id = id
        self.pw = pw
        oracledb.init_oracle_client()
        self._connect()
        # os.environ["NLS_LANG"] = ".AL32UTF8"

    def __del__(self):
        return

    def close(self):
        self._conn.close()

    def _connect(self):
        # self._conn = cx_Oracle.Connection("cx_Oracle/dev@t11g")
        self._conn = oracledb.connect(user=self.id, password=self.pw, dsn=self.server)
        # self._conn = cx_Oracle.connect(self.id, self.pw, self.server)
        self._cursor = self._conn.cursor()

    def SELECT(self, Query, printYN=False, output_type="DataFrame"):
        '''
        :param Query: 쿼리를 string 형태로 입력
        :param printYN: 쿼리와 결과를 창에 출력할지
        :param output_type: list, array, pandas DataFrame 형태로 return 가능함
        :return: 결과
        '''
        self._cursor.execute(Query)
        row = self._cursor.fetchone()
        desc_list = self._cursor.description
        desc_list = list(desc_list)
        header_str = ""
        header_list = []
        for desc in desc_list:
            desc = desc[0]
            header_list.append(desc)
            desc = str(desc)
            header_str = header_str + desc + '\t'
        if printYN:
            print(header_str)
        data_mat = []
        while row:
            row = list(row)
            row_list = []
            row_str = ""
            for r in row:
                row_list.append(r)
                r = str(r)
                row_str = row_str + r + '\t'
            if printYN:
                print(row_str)
            data_mat.append(row_list)
            row = self._cursor.fetchone()
        if output_type == "old_version":
            return data_mat
        data_array = np.array(data_mat)
        data_array = data_array.transpose()
        if len(data_array) == 0:
            return None
        data_dict = collections.OrderedDict()
        for i in range(len(header_list)):
            data_dict[header_list[i]] = data_array[i]
        data_df = pd.DataFrame(data_dict)
        if output_type == "DataFrame":
            rlt = data_df
        elif output_type == "array":
            rlt = data_array
        elif output_type == "list":
            rlt = list(data_array)
        else:
            raise ValueError("Wrong output type")
        return rlt



def com_adj_price(start, end, ComCode, WK_END=False):
    '''
    :param start: datetime
    :param end: datetime
    :param ComCode: 6자리 회사코드
    :return: Query for WFNS2DB
    '''
    start_ymd = datetime.datetime.strftime(start, "%Y%m%d")
    end_ymd = datetime.datetime.strftime(end, "%Y%m%d")

    query = "DECLARE @STT_DT char(08)\n"
    query += "DECLARE @END_DT char(08)\n"
    query += "DECLARE @ComCode char(06)\n\n"
    query += "SET @STT_DT = '" + start_ymd + "'\n"
    query += "SET @END_DT = '" + end_ymd + "'\n"
    query += "SET @ComCode = '" + ComCode + "'\n\n"
    query += "SELECT \n"
    query += "TD.STK_CD AS 'ComCode', TD.TRD_DT AS 'Date', \n"
    query += "TD.STK_CD AS 'ComCode', TD.TRD_DT AS 'Date', \n"
    query += "CONVERT(FLOAT, ROUND(TD.CLOSE_PRC/TA.AADJ,2)) AS 'Close', \n"
    query += "CONVERT(FLOAT, ROUND(TD.HIGH_PRC/TA.AADJ,2)) AS 'High', \n"
    query += "CONVERT(FLOAT, ROUND(TD.LOW_PRC/TA.AADJ,2)) AS 'Low', \n"
    query += "CONVERT(FLOAT, ROUND(TD.Open_PRC/TA.AADJ,2)) AS 'Open', \n"
    query += "TD.TRD_QTY AS 'Volume' \n\n"
    query += "FROM WFNS2DB..TS_STK_DAILY TD \n"
    query += "LEFT JOIN WFNS2DB..TS_STK_ADJ_FACTOR TA ON TD.STK_CD = TA.STK_CD AND TD.TRD_DT BETWEEN TA.START_DT AND TA.TRD_DT \n\n"
    if WK_END:
        query += "LEFT JOIN TZ_DATE TZD ON TD.TRD_DT = TZD.DT \n"
    query += "WHERE TD.TRD_DT BETWEEN @STT_DT and @END_DT AND TD.STK_CD = @ComCode \n"
    if WK_END:
        query += "and TZD.WK_END_YN = 1 \n"
    query += "ORDER BY 'Date' ASC;"
    # print(query)
    return query

def K200_price(start, end=None):
    '''
    :param start: datetime
    :param end: datetime
    :return: query for KRX_R
    '''
    start_ymd = datetime.datetime.strftime(start, "%Y%m%d")
    query = "SELECT\n"
    query += "K200C.FILEDATE AS 'Date', K200C.INDEX_NAME_EN AS 'Name', K200C.INDEX_CODE2 AS 'IDX_Code', K200C.CLOSE_VALUE AS 'Close',\n"
    query += "ROUND(K200C.INDEX_MARKET_CAP_CLS/1000000000,2) AS 'Mkt Cap(bn)', K200C.PER AS 'P/E', K200C.PBR AS 'P/B',\n"
    query += "K200C.DIVIDEND_YIELD AS 'Div Yld'\n"
    query += "From\n"
    query += "KOS200M_INDEX_CLS K200C\n"
    query += "WHERE\n"
    query += "INDEX_CODE2 = '029'\n"
    query += "and FILEDATE >= '" + start_ymd + "'"
    if end is not None:
        end_ymd = datetime.datetime.strftime(end, "%Y%m%d")
        query += "\nand FILEDATE <= '" + end_ymd + "'"
    return query



if __name__ == '__main__':
    '''
    ★ 아래에 사용법 설명이 나와있습니다 ★
    '''

    # bloomberg 데이터베이스 연결 테스트
    try:
        df = bdh("KOSPI Index", "PX_LAST", "20250101", "20250331", "180")
        print(df)
    except Exception as e:
        print(f"Error fetching data: {e}")

    Qry = "SELECT * FROM PAIR_Signal WHERE BaseDate = '2018-10-19' AND (ComCode1 = '005930' or ComCode1 = '005380')"
    path = os.path.join(v.project_path, "data")



    ##################### !!!! 사용법 !!!! #####################
    # 1. DB접속하여 데이터 읽기
    # 1-1) Server, ID, Password, DB명을 입력한다. 14,15,16,17열에 있음.
    #      WFN데이터베이스를 쓰면 db만 달라질 것
    trstdb = MSSQL(v.trst_server, v.trst_id, v.trst_pw, v.trstdb)
    wfndb = MSSQL(v.trst_server, v.trst_id, v.trst_pw, v.wfnsdb)
    krxdb = MSSQL(v.trst_server, v.trst_id, v.trst_pw, v.krxdb)

    # 1-2) Query를 입력한다. pandas DataFrame 형태로 return
    TRUSTQUANTDB_test_data = trstdb.SELECT(Qry)

    start = datetime.datetime(2019, 1, 1)
    end = datetime.datetime(2019, 2, 2)

    query = K200_price(start, end=end)
    rlt = krxdb.SELECT(query, printYN=True)

    # WFNS2DB_test_data = wfndb.SELECT(Qry)
    # pprint(TRUSTQUANTDB_test_data)
    TRUSTQUANTDB_test_data.to_csv(os.path.join(path, "test.csv"), encoding='ms949')


    # 2. 작업이 끝나면 DB를 종료합니다.
    trstdb.close()
    wfndb.close()

    # 3. 혹시 다시 연결하고싶다면(언더바connect)
    trstdb._connect()
    wfndb._connect()





    # HANA FUND ORACLE SERVER 설명
    # 거의 MSSQL과 동일하다
    # 1. ORACLE 연결, id, pw, server는 거의 안바뀐다
    # hanadb = ORACLE(server="192.168.23.135:16000/CDMTR", id="CDM_SEL", pw="Hanafund12!")
    hanadb = ORACLE()

    # 2-1. 쿼리를 만들고
    Query = f"""
    SELECT S.거래유형코드, S.기준일자, S.펀드코드, SM.단축코드, SM.종목명, S.매수일자, S.매수일련번호,
           S.주식수량, S.매매단가, S.매매금액, S.거래비용, S.매매수수료, S.주식수수료구분코드, S.세금, S.조사분석수수료, 
           S.결제금액, S.실제결제일자 
    FROM 매매_주식 S
    INNER JOIN 종목_주식 SM ON S.종목코드 = SM.종목코드 AND SM.종료일자 >= '2019-07-24'
    WHERE S.주식수량 > '0' AND (S.기준일자 BETWEEN '2019-07-17' AND '2019-07-24') AND (S.펀드코드 = 'A06501')
    """
    # 2-2. SELECT
    x = hanadb.SELECT(Query)

    # 3. 종료
    hanadb.close()

    # 4. 다시연결
    hanadb._connect()