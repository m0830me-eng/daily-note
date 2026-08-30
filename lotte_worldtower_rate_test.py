import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from curl_cffi import requests

KST = ZoneInfo("Asia/Seoul")

API_URL = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"
TICKETING_URL = "https://www.lottecinema.co.kr/NLCHS/Ticketing"

WORLD_TOWER_ID = "1016"
TARGET_INTERVAL = float(os.environ.get("TARGET_INTERVAL", "19"))
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "300"))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


def post_param(session, payload):
    started = time.monotonic()

    r = session.post(
        API_URL,
        data={"paramList": json.dumps(payload, ensure_ascii=False)},
        headers={
            "User-Agent": UA,
            "Referer": TICKETING_URL,
            "Origin": "https://www.lottecinema.co.kr",
        },
        impersonate="chrome",
        timeout=20,
    )

    elapsed = time.monotonic() - started
    return r, elapsed


def find_items(node):
    if isinstance(node, dict):
        items = node.get("Items")
        if isinstance(items, list):
            yield items
        for value in node.values():
            yield from find_items(value)
    elif isinstance(node, list):
        for value in node:
            yield from find_items(value)


def inspect_ticketing_page(data):
    cinema_found = False
    cinema_name = ""
    movie_codes = set()

    for items in find_items(data):
        for item in items:
            if not isinstance(item, dict):
                continue

            cid = str(item.get("CinemaID") or "")
            if cid == WORLD_TOWER_ID:
                cinema_found = True
                cinema_name = str(item.get("CinemaNameKR") or "월드타워")

            code = str(item.get("RepresentationMovieCode") or "")
            if code:
                movie_codes.add(code)

    return cinema_found, cinema_name, len(movie_codes)


def main():
    print("=" * 72)
    print("LOTTE WORLD TOWER RATE TEST")
    print("CINEMA: 월드타워 / 1016")
    print(f"INTERVAL: {TARGET_INTERVAL:g} seconds")
    print(f"RUN SECONDS: {RUN_SECONDS}")
    print("METHOD: GetTicketingPageTOBE")
    print("=" * 72)

    session = requests.Session(impersonate="chrome")

    payload = {
        "MethodName": "GetTicketingPageTOBE",
        "channelType": "HO",
        "osType": "W",
        "osVersion": UA,
        "memberOnNo": "0",
    }

    started_all = time.monotonic()
    cycle = 0
    ok_count = 0
    fail_count = 0

    while time.monotonic() - started_all < RUN_SECONDS:
        cycle += 1
        cycle_started = time.monotonic()

        try:
            r, req_elapsed = post_param(session, payload)

            print()
            print(
                f"CYCLE #{cycle} | "
                f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} KST | "
                f"HTTP={r.status_code} | {req_elapsed:.2f}s | "
                f"SIZE={len(r.content):,}"
            )

            if r.status_code != 200:
                fail_count += 1
                print("FAIL: HTTP STATUS", r.status_code)
            else:
                data = r.json()
                is_ok = data.get("IsOK")
                result_message = data.get("ResultMessage")

                cinema_found, cinema_name, movie_count = inspect_ticketing_page(data)

                print("IsOK:", is_ok)
                print("ResultMessage:", result_message)
                print("WORLD TOWER FOUND:", cinema_found, cinema_name)
                print("MOVIE CODE COUNT:", movie_count)

                if is_ok is True and cinema_found:
                    ok_count += 1
                else:
                    fail_count += 1
                    print("FAIL: API response not healthy")

        except Exception as e:
            fail_count += 1
            print("ERROR:", repr(e))

        cycle_elapsed = time.monotonic() - cycle_started
        wait = max(0.0, TARGET_INTERVAL - cycle_elapsed)

        print(
            f"SUMMARY: OK={ok_count} FAIL={fail_count} "
            f"CYCLE_ELAPSED={cycle_elapsed:.2f}s WAIT={wait:.2f}s"
        )

        if wait > 0:
            time.sleep(wait)

    print()
    print("=" * 72)
    print(f"FINAL: OK={ok_count} FAIL={fail_count}")
    if fail_count == 0:
        print(f"RESULT: {TARGET_INTERVAL:g}s TEST PASSED")
    else:
        print(f"RESULT: {TARGET_INTERVAL:g}s TEST HAD FAILURES")
    print("=" * 72)


if __name__ == "__main__":
    main()
