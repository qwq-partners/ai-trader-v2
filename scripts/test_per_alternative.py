#!/usr/bin/env python3
"""PER/PBR 대체 API 탐색"""

import asyncio
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")

from src.utils.kis_token_manager import get_token_manager
import aiohttp


async def main():
    tm = get_token_manager()
    token = await tm.get_access_token()
    base = tm.base_url

    session = aiohttp.ClientSession()

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": tm.app_key,
        "appsecret": tm.app_secret,
    }

    # 1. 주식 기본 시세 조회 (FHKST01010100)
    print("=== 1. 주식 기본시세 (FHKST01010100) - 삼성전자 ===")
    h1 = dict(headers, tr_id="FHKST01010100")
    url1 = f"{base}/uapi/domestic-stock/v1/quotations/inquire-price"
    p1 = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": "005930",
    }
    async with session.get(url1, headers=h1, params=p1) as resp:
        data = await resp.json()
        output = data.get("output", {})
        per_keys = {k: v for k, v in output.items()
                   if any(x in k.lower() for x in ['per', 'pbr', 'eps', 'bps'])}
        print(f"  PER/PBR 관련 필드: {json.dumps(per_keys, ensure_ascii=False)}")

    # 2. 주식 투자자별 매매동향 (FHKST01010900) - 개별 종목
    print("\n=== 2. 체결/투자 정보 ===")
    h2 = dict(headers, tr_id="FHKST01010900")
    url2 = f"{base}/uapi/domestic-stock/v1/quotations/inquire-member"
    p2 = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": "005930",
    }
    async with session.get(url2, headers=h2, params=p2) as resp:
        data = await resp.json()
        print(f"  rt_cd='{data.get('rt_cd')}', msg='{data.get('msg1','')}'")

    # 3. 재무비율 조회 (FHKST66430300)
    print("\n=== 3. 재무비율 조회 시도 ===")
    for tr_id in ["FHKST66430300", "FHKST03010100"]:
        h3 = dict(headers, tr_id=tr_id)
        url3 = f"{base}/uapi/domestic-stock/v1/finance/financial-ratio"
        p3 = {
            "FID_DIV_CLS_CODE": "0",
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": "005930",
        }
        try:
            async with session.get(url3, headers=h3, params=p3) as resp:
                data = await resp.json()
                print(f"  [{tr_id}] rt_cd='{data.get('rt_cd')}', msg='{data.get('msg1','')}'")
                output = data.get("output", [])
                if isinstance(output, list) and output:
                    print(f"  첫 항목: {json.dumps(output[0], ensure_ascii=False)[:300]}")
                elif isinstance(output, dict) and output:
                    print(f"  output: {json.dumps(output, ensure_ascii=False)[:300]}")
        except Exception as e:
            print(f"  [{tr_id}] 오류: {e}")

    # 4. 종목 조건검색 (HHKST03900300) - 밸류에이션 조건
    print("\n=== 4. PER/PBR 포함 시세조회 (FHKST03010100) ===")
    h4 = dict(headers, tr_id="FHKST03010100")
    url4 = f"{base}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
    p4 = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": "005930",
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    async with session.get(url4, headers=h4, params=p4) as resp:
        data = await resp.json()
        print(f"  rt_cd='{data.get('rt_cd')}', msg='{data.get('msg1','')}'")
        output = data.get("output", [])
        if isinstance(output, list) and output:
            per_fields = {k: v for k, v in output[0].items()
                         if any(x in k.lower() for x in ['per', 'pbr', 'eps'])}
            print(f"  PER 필드: {per_fields}")

    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
