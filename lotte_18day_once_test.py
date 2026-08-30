#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

LOTTE_API = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"
CINEMA_ID = "1|0001|1016"
KST = ZoneInfo("Asia/Seoul")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

START_OFFSET = 4
DAY_COUNT = 18
WORKERS = 18
TIMEOUT = 15

start_barrier = threading.Barrier(DAY_COUNT)


def now_kst():
    return datetime.now(KST)


def make_dates():
    today = now_kst().date()
    return [
        (today + timedelta(days=START_OFFSET + i)).strftime("%Y-%m-%d")
        for i in range(DAY_COUNT)
    ]


def count_sequence_like_rows(obj):
    """
    테스트용 간단 카운터.
    응답 내부 list 중 dict 원소가 들어 있는 가장 큰 리스트 길이를 반환한다.
    운영 상태/Discord에는 전혀 사용하지 않는다.
    """
    best = 0

    def walk(value):
        nonlocal best
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            if value and all(isinstance(x, dict) for x in value):
                best = max(best, len(value))
            for child in value:
                walk(child)

    walk(obj)
    return best


def fetch_once(date):
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.lottecinema.co.kr/NLCHS/Ticketing",
        "Origin": "https://www.lottecinema.co.kr",
    })

    payload = {
        "MethodName": "GetPlaySequence",
        "channelType": "HO",
        "osType": "W",
        "osVersion": UA,
        "playDate": date,
        "cinemaID": CINEMA_ID,
        "representationMovieCode": "",
    }

    files = {
        "paramList": (
            None,
            json.dumps(payload, ensure_ascii=False),
        )
    }

    # 18개 작업이 모두 준비된 뒤 최대한 같은 순간에 시작
    try:
        start_barrier.wait(timeout=10)
    except threading.BrokenBarrierError:
        pass

    started = time.monotonic()

    try:
        response = session.post(
            LOTTE_API,
            files=files,
            timeout=TIMEOUT,
        )
        elapsed = time.monotonic() - started

        if response.status_code != 200:
            return {
                "date": date,
                "ok": False,
                "status": response.status_code,
                "elapsed": elapsed,
                "error": f"HTTP {response.status_code}",
            }

        try:
            data = response.json()
        except Exception as e:
            preview = response.text[:120].replace("\n", " ").replace("\r", " ")
            return {
                "date": date,
                "ok": False,
                "status": response.status_code,
                "elapsed": elapsed,
                "error": f"JSON ERROR {repr(e)} PREVIEW={preview!r}",
            }

        return {
            "date": date,
            "ok": True,
            "status": response.status_code,
            "elapsed": elapsed,
            "bytes": len(response.content),
            "rows_hint": count_sequence_like_rows(data),
        }

    except Exception as e:
        return {
            "date": date,
            "ok": False,
            "status": None,
            "elapsed": time.monotonic() - started,
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        session.close()


def main():
    dates = make_dates()

    print("=" * 72, flush=True)
    print("LOTTE WORLDTOWER +4~+21 DAYS / 18-WORKER ONE-SHOT TEST", flush=True)
    print("=" * 72, flush=True)
    print(f"DATES: {dates[0]} ~ {dates[-1]} ({len(dates)} dates)", flush=True)
    print("WORKERS: 18 (all 18 dates start together)", flush=True)
    print("API: GetPlaySequence / 월드타워 1016", flush=True)
    print("REPEAT: NO (one shot only)", flush=True)
    print("DISCORD/STATE WRITE: NO", flush=True)
    print(f"TIMEOUT: {TIMEOUT}s", flush=True)
    print("KST NOW:", now_kst().strftime("%Y-%m-%d %H:%M:%S"), flush=True)
    print("=" * 72, flush=True)

    total_started = time.monotonic()
    results = []

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(fetch_once, date) for date in dates]

        for future in as_completed(futures):
            results.append(future.result())

    total_elapsed = time.monotonic() - total_started
    results.sort(key=lambda x: x["date"])

    success = 0
    failed = []

    for item in results:
        if item["ok"]:
            success += 1
            print(
                f"{item['date']} | OK | HTTP={item['status']} | "
                f"{item['elapsed']:.2f}s | SIZE={item['bytes']:,} | "
                f"ROWS_HINT={item['rows_hint']}",
                flush=True,
            )
        else:
            failed.append(item["date"])
            print(
                f"{item['date']} | FAIL | {item['elapsed']:.2f}s | "
                f"{item['error']}",
                flush=True,
            )

    print("=" * 72, flush=True)

    if success == DAY_COUNT:
        print(
            f"✅ TEST RESULT: ALL 18 DATES SUCCESS | {total_elapsed:.2f}s",
            flush=True,
        )
        print(
            "판정: +4~+21일 18개 날짜를 동시에 시작해도 이번 테스트에서는 "
            "모두 정상 응답했습니다.",
            flush=True,
        )
    else:
        print(
            f"⚠️ TEST RESULT: PARTIAL FAILURE | success={success}/{DAY_COUNT} | "
            f"{total_elapsed:.2f}s",
            flush=True,
        )
        print("FAILED DATES:", ", ".join(failed), flush=True)

    print("STATE/DISCORD CHANGED: NO", flush=True)
    print("=" * 72, flush=True)


if __name__ == "__main__":
    main()
