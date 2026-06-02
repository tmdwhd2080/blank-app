# -*- coding: utf-8 -*-
"""
테이블 CSV 내보내기 스크립트

4개 테이블을 CSV로 다운로드:
- BLBG_DB
- PFM_FCTR
- REGIME_QMS
- TS_IDX_DAILY
"""
# python util\export_tables_to_csv.py

import os
import sys
import time

sys.path.append(os.path.join(r'C:\Users\intern9', 'truston_quant_dev'))

from util.database2 import MSSQL


def export_tables_to_csv():
    """4개 테이블을 CSV로 내보내기"""
    
    # 1. 저장할 경로 설정
    save_dir = os.path.join(os.getcwd(), 'csv_export')
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    
    # 2. DB 연결
    print("=" * 60)
    print("테이블 CSV 내보내기")
    print("=" * 60)
    print("\n[1/2] DB 연결 중...")
    
    try:
        db = MSSQL()  # variables.py에서 자동으로 연결 정보 로드
        print("   DB 연결 성공!")
    except Exception as e:
        print(f"   [ERROR] DB 연결 실패: {e}")
        return
    
    # 3. 다운로드할 테이블 목록
    target_tables = [
        'BLBG_DB',      # 약 125MB
        'PFM_FCTR',     # 약 4.1MB
        'REGIME_QMS',   # 약 2.4MB
        'TS_IDX_DAILY'  # 약 2.9MB
    ]
    
    print(f"\n[2/2] 총 {len(target_tables)}개 테이블 다운로드 시작...")
    print("-" * 60)
    
    success_count = 0
    
    for i, table_name in enumerate(target_tables, 1):
        start_time = time.time()
        print(f"\n[{i}/{len(target_tables)}] {table_name}")
        print(f"   데이터 조회 중...", end=" ")
        
        try:
            # 4. 쿼리 실행
            df = db.SELECT(f"SELECT * FROM {table_name}")
            
            if df is None or len(df) == 0:
                print("데이터가 없습니다.")
                continue
            
            print(f"완료! ({len(df):,}행)")
            
            # 5. CSV 저장
            file_path = os.path.join(save_dir, f"{table_name}.csv")
            print(f"   CSV 저장 중...", end=" ")
            
            # 엑셀에서 한글 깨짐 방지를 위해 'utf-8-sig' 사용
            df.to_csv(file_path, index=False, encoding='utf-8-sig')
            
            elapsed = time.time() - start_time
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            
            print(f"완료! ({file_size_mb:.1f}MB, {elapsed:.1f}초)")
            print(f"   -> {file_path}")
            
            success_count += 1
            
        except Exception as e:
            print(f"\n   [ERROR] 처리 중 오류 발생: {e}")
    
    # 6. 연결 종료
    try:
        db.close()
    except:
        pass
    
    print("\n" + "=" * 60)
    print(f"완료! {success_count}/{len(target_tables)}개 테이블 저장됨")
    print(f"저장 위치: {save_dir}")
    print("=" * 60)


if __name__ == "__main__":
    export_tables_to_csv()
# python util\export_tables_to_csv.py