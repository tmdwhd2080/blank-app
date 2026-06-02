# -*- coding: utf-8 -*-

import os
from collections import OrderedDict
from pathlib import Path

try:
    from dotenv import load_dotenv
    # 루트 .env(로컬 전용, gitignore) 에서 DB 접속정보 로드
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass


####################################################################################
# 파일위치관련
project_path = os.path.join("C:\\Projects", "truston_quant_dev")

####################################################################################
# MSSQL Server 관련 — 자격증명은 환경변수/루트 .env 에서만 로드 (하드코딩 금지)
trst_server = os.environ.get("TRST_DB_SERVER", "")
trst_id = os.environ.get("TRST_DB_ID", "")
trst_pw = os.environ.get("TRST_DB_PW", "")
trstdb = os.environ.get("TRST_DB_NAME", "TRSTDEV")
