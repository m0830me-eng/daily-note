#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from curl_cffi import requests

try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass

KST = ZoneInfo("Asia/Seoul")

API_URL = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"
BOOKING_PAGE = "https://www.lottecinema.co.kr/NLCHS/Ticketing"

CINEMA_ID = "1|0001|1016"
DAYS = 50

# CGV 때와 같은 자동 속도 탐색
START_SECONDS = 12.0
MIN_SECONDS = 10.0
MAX_SECONDS = 60.0
STEP_SECONDS = 1.0

# 20번 연속 정상 작동하면 1초 더 빠르게
CLEAN_CYCLES_TO_SPEEDUP = 20

RUN_SECONDS = int(
    os.environ.get(
        "RUN_SECONDS",
        "3600",
    )
)

BLOCK_CODES = {
    403,
    429,
    503,
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": BOOKING_PAGE,
    "Origin": "https://www.lottecinema.co.kr",
}


def norm(value):
    return " ".join(
        str(value or "").split()
    )


def all_text(obj):
    parts = []

    def walk(x):
        if isinstance(x, dict):
            for value in x.values():
                walk(value)

        elif isinstance(x, list):
            for value in x:
                walk(value)

        elif x is not None:
            text = norm(x)

            if text:
                parts.append(text)

    walk(obj)

    return " | ".join(parts)


def event_type(row):
    # 실제 롯데 API에서 확인한 코드
    # 30 = 무대인사
    # 40 = GV

    code = norm(
        row.get(
            "AccompanyTypeCode"
        )
    )

    if code == "30":
        return "STAGE"

    if code == "40":
        return "GV"

    # 혹시 코드가 바뀔 경우 문구도 같이 확인
    text = all_text(row)

    compact = re.sub(
        r"\s+",
        "",
        text,
    )

    if (
        "무대인사" in compact
        or "舞台挨拶" in text
    ):
        return "STAGE"

    if "관객과의대화" in compact:
        return "GV"

    if re.search(
        r"(?<![A-Z0-9])GV(?![A-Z0-9])",
        text.upper(),
    ):
        return "GV"

    return None


def post_lotte(
    session,
    payload,
):
    started = time.monotonic()

    response = session.post(
        API_URL,
        data={
            "paramList": json.dumps(
                payload,
                ensure_ascii=False,
            )
        },
        headers=HEADERS,
        impersonate="chrome",
        timeout=20,
    )

    elapsed = (
        time.monotonic()
        - started
    )

    return (
        response,
        elapsed,
    )


def extract_rows(
    data,
    date,
):
    found = []

    def walk(x):
        if isinstance(
            x,
            dict,
        ):
            start = norm(
                x.get("StartTime")
                or x.get(
                    "PlayStartTime"
                )
                or x.get("StartTm")
            )

            screen = norm(
                x.get("ScreenNameKR")
                or x.get(
                    "ScreenName"
                )
                or x.get("ScreenID")
            )

            if (
                start
                and screen
            ):
                row = dict(x)

                row["_date"] = (
                    date
                )

                found.append(
                    row
                )

            for value in x.values():
                walk(value)

        elif isinstance(
            x,
            list,
        ):
            for value in x:
                walk(value)

    walk(data)

    dedup = {}

    for row in found:
        key = "|".join(
            [
                date,

                norm(
                    row.get(
                        "RepresentationMovieCode"
                    )
                    or row.get(
                        "MovieCode"
                    )
                    or row.get(
                        "MovieNameKR"
                    )
                ),

                norm(
                    row.get(
                        "ScreenID"
                    )
                    or row.get(
                        "ScreenNameKR"
                    )
                ),

                norm(
                    row.get(
                        "PlaySequence"
                    )
                ),

                norm(
                    row.get(
                        "StartTime"
                    )
                ),
            ]
        )

        dedup[key] = row

    return list(
        dedup.values()
    )


def source_line(row):
    movie = norm(
        row.get(
            "MovieNameKR"
        )
        or row.get(
            "MovieName"
        )
        or "영화명없음"
    )

    screen = norm(
        row.get(
            "ScreenNameKR"
        )
        or row.get(
            "ScreenName"
        )
        or row.get(
            "ScreenID"
        )
    )

    start = norm(
        row.get(
            "StartTime"
        )
    )

    end = norm(
        row.get(
            "EndTime"
        )
    )

    booking = (
        norm(
            row.get(
                "IsBookingYN"
            )
        )
        or "?"
    )

    accompany_code = (
        norm(
            row.get(
                "AccompanyTypeCode"
            )
        )
        or "-"
    )

    accompany_name = (
        norm(
            row.get(
                "AccompanyTypeNameKR"
            )
        )
        or "-"
    )

    kind = (
        event_type(row)
        or "NORMAL"
    )

    result = (
        f"{row.get('_date', '')} "
        f"{start}"
    )

    if end:
        result += (
            f"~{end}"
        )

    result += (
        f" | {screen}"
        f" | {movie}"
        f" | {kind}"
        f" | IsBookingYN={booking}"
        f" | Accompany="
        f"{accompany_code}/"
        f"{accompany_name}"
    )

    return result


def dates_50():
    today = (
        datetime.now(
            KST
        ).date()
    )

    return [
        (
            today
            + timedelta(
                days=index
            )
        ).strftime(
            "%Y-%m-%d"
        )
        for index in range(
            DAYS
        )
    ]


def warmup(session):
    payload = {
        "MethodName":
            "GetTicketingPageTOBE",

        "channelType":
            "HO",

        "osType":
            "W",

        "osVersion":
            UA,

        "memberOnNo":
            "0",
    }

    response, elapsed = (
        post_lotte(
            session,
            payload,
        )
    )

    print(
        "WARMUP "
        "GetTicketingPageTOBE | "
        f"HTTP={response.status_code} | "
        f"TIME={elapsed:.2f}s | "
        f"SIZE={len(response.content):,}"
    )

    if (
        response.status_code
        in BLOCK_CODES
    ):
        print(
            "!!! LOTTE RATE LIMIT "
            f"/ BLOCK STATUS="
            f"{response.status_code} !!!"
        )

        return False

    if (
        response.status_code
        != 200
    ):
        return False

    data = response.json()

    print(
        "IsOK:",
        data.get("IsOK"),
    )

    print(
        "ResultMessage:",
        data.get(
            "ResultMessage"
        ),
    )

    return True


def fetch_date(
    session,
    date,
):
    payload = {
        "MethodName":
            "GetPlaySequence",

        "channelType":
            "HO",

        "osType":
            "W",

        "osVersion":
            UA,

        "playDate":
            date,

        "cinemaID":
            CINEMA_ID,

        "representationMovieCode":
            "",
    }

    response, elapsed = (
        post_lotte(
            session,
            payload,
        )
    )

    status = (
        response.status_code
    )

    if status in BLOCK_CODES:
        print(
            f"API "
            f"{date.replace('-', '')} "
            f"STATUS={status} "
            f"TIME={elapsed:.2f}s "
            f"SIZE="
            f"{len(response.content):,} "
            f"bytes"
        )

        return (
            None,
            status,
        )

    if status != 200:
        raise RuntimeError(
            f"HTTP {status}"
        )

    data = response.json()

    rows = extract_rows(
        data,
        date,
    )

    print(
        f"API "
        f"{date.replace('-', '')} "
        f"STATUS=200 "
        f"TIME={elapsed:.2f}s "
        f"SIZE="
        f"{len(response.content):,} "
        f"bytes "
        f"ROWS={len(rows)}"
    )

    return (
        rows,
        None,
    )


def scan_cycle(
    session,
    cycle_number,
    target_seconds,
):
    started = (
        time.monotonic()
    )

    date_count = 0
    errors = 0

    blocked_code = None
    blocked_date = None

    total_rows = 0

    gv_count = 0
    stage_count = 0

    source_samples = []

    print()

    print(
        "=" * 72
    )

    print(
        f"CYCLE #{cycle_number} "
        f"START | "
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} "
        f"KST | "
        f"50 DAYS | "
        f"TARGET="
        f"{target_seconds:.0f}s"
    )

    print(
        "=" * 72
    )

    for date in dates_50():

        try:
            rows, block = (
                fetch_date(
                    session,
                    date,
                )
            )

            if block is not None:
                blocked_code = (
                    block
                )

                blocked_date = (
                    date
                )

                print(
                    "!!! LOTTE RATE LIMIT "
                    "/ BLOCK "
                    f"STATUS={block} "
                    f"ON "
                    f"{date.replace('-', '')} "
                    "!!!"
                )

                break

            date_count += 1

            total_rows += (
                len(rows)
            )

            for row in rows:

                # 실제 소스 확인용
                if (
                    len(
                        source_samples
                    )
                    < 5
                ):
                    source_samples.append(
                        row
                    )

                kind = (
                    event_type(
                        row
                    )
                )

                if kind == "GV":
                    gv_count += 1

                    print(
                        "TARGET SOURCE:",
                        source_line(
                            row
                        ),
                    )

                elif (
                    kind
                    == "STAGE"
                ):
                    stage_count += 1

                    print(
                        "TARGET SOURCE:",
                        source_line(
                            row
                        ),
                    )

        except Exception as error:
            errors += 1

            print(
                f"API "
                f"{date.replace('-', '')} "
                f"ERROR: "
                f"{type(error).__name__}: "
                f"{error}"
            )

    elapsed = (
        time.monotonic()
        - started
    )

    print()

    print(
        "LIVE SOURCE SAMPLE"
    )

    if source_samples:

        for row in source_samples:
            print(
                "  ",
                source_line(
                    row
                ),
            )

    else:
        print(
            "  no rows returned"
        )

    print(
        f"TARGET COUNT: "
        f"GV={gv_count} "
        f"STAGE={stage_count} "
        f"TOTAL="
        f"{gv_count + stage_count}"
    )

    if (
        blocked_code
        is not None
    ):
        print(
            f"CYCLE #{cycle_number} "
            f"BLOCKED | "
            f"STATUS={blocked_code} | "
            f"DATE="
            f"{blocked_date.replace('-', '')} | "
            f"DATES="
            f"{date_count}/{DAYS} | "
            f"ERRORS={errors} | "
            f"ELAPSED="
            f"{elapsed:.2f}s"
        )

    else:
        print(
            f"CYCLE #{cycle_number} "
            f"DONE | "
            f"DATES="
            f"{date_count}/{DAYS} | "
            f"ROWS={total_rows} | "
            f"ERRORS={errors} | "
            f"ELAPSED="
            f"{elapsed:.2f}s"
        )

    return (
        blocked_code,
        date_count,
        errors,
        elapsed,
    )


def main():
    print(
        "=" * 72
    )

    print(
        "LOTTE WORLDTOWER "
        "50-DAY AUTO SPEED TEST "
        "- LIVE SOURCE"
    )

    print(
        "CINEMA: "
        "월드타워 / 1016"
    )

    print(
        "SOURCE: "
        "GetPlaySequence"
    )

    print(
        "TARGET: "
        "GV / STAGE"
    )

    print(
        "DATE RANGE: "
        "TODAY ~ +49 DAYS"
    )

    print(
        f"START TARGET: "
        f"{START_SECONDS:.0f} "
        "SECONDS"
    )

    print(
        f"AUTO TUNE: "
        f"RATE LIMIT -> "
        f"+{STEP_SECONDS:.0f}s | "
        f"{CLEAN_CYCLES_TO_SPEEDUP} "
        f"CLEAN CYCLES -> "
        f"-{STEP_SECONDS:.0f}s"
    )

    print(
        f"TUNE RANGE: "
        f"{MIN_SECONDS:.0f}s ~ "
        f"{MAX_SECONDS:.0f}s"
    )

    print(
        f"RUN SECONDS: "
        f"{RUN_SECONDS}"
    )

    print(
        "=" * 72
    )

    session = (
        requests.Session(
            impersonate="chrome"
        )
    )

    if not warmup(
        session
    ):
        print(
            "WARMUP FAILED"
        )

        return

    run_started = (
        time.monotonic()
    )

    cycle_number = 0

    target_seconds = (
        START_SECONDS
    )

    clean_streak = 0

    while (
        time.monotonic()
        - run_started
        < RUN_SECONDS
    ):
        cycle_number += 1

        (
            block,
            date_count,
            errors,
            elapsed,
        ) = scan_cycle(
            session,
            cycle_number,
            target_seconds,
        )

        # 차단되면 1초 느리게
        if block is not None:

            old_target = (
                target_seconds
            )

            target_seconds = min(
                MAX_SECONDS,
                target_seconds
                + STEP_SECONDS,
            )

            clean_streak = 0

            print(
                f"AUTO SLOWDOWN: "
                f"{old_target:.0f}s "
                f"-> "
                f"{target_seconds:.0f}s "
                f"(STATUS={block})"
            )

        # 50일 전체 정상 완료
        elif (
            errors == 0
            and date_count == DAYS
        ):
            clean_streak += 1

            print(
                f"CLEAN CYCLE STREAK: "
                f"{clean_streak}/"
                f"{CLEAN_CYCLES_TO_SPEEDUP}"
            )

            # 20번 정상 → 1초 더 빠르게
            if (
                clean_streak
                >= CLEAN_CYCLES_TO_SPEEDUP
            ):
                old_target = (
                    target_seconds
                )

                target_seconds = max(
                    MIN_SECONDS,
                    target_seconds
                    - STEP_SECONDS,
                )

                clean_streak = 0

                if (
                    target_seconds
                    < old_target
                ):
                    print(
                        f"AUTO SPEEDUP: "
                        f"{old_target:.0f}s "
                        f"-> "
                        f"{target_seconds:.0f}s "
                        f"AFTER "
                        f"{CLEAN_CYCLES_TO_SPEEDUP} "
                        f"CLEAN CYCLES"
                    )

                else:
                    print(
                        "AUTO SPEEDUP: "
                        "ALREADY AT MIN "
                        f"{MIN_SECONDS:.0f}s"
                    )

        else:
            clean_streak = 0

            print(
                "CLEAN CYCLE STREAK RESET: "
                "incomplete/error cycle"
            )

        remaining = (
            RUN_SECONDS
            - (
                time.monotonic()
                - run_started
            )
        )

        if remaining <= 0:
            break

        wait_time = min(
            max(
                0.0,
                target_seconds
                - elapsed,
            ),
            remaining,
        )

        if wait_time > 0:

            print(
                f"WAIT "
                f"{wait_time:.2f}s "
                f"TO KEEP "
                f"{target_seconds:.0f}s "
                f"CYCLE"
            )

            time.sleep(
                wait_time
            )

        else:
            print(
                f"CYCLE TOOK >= "
                f"{target_seconds:.0f}s "
                "- START NEXT CYCLE "
                "IMMEDIATELY"
            )

    print()

    print(
        "=" * 72
    )

    print(
        "AUTO SPEED TEST COMPLETE"
    )

    print(
        f"FINAL TARGET: "
        f"{target_seconds:.0f}s"
    )

    print(
        "=" * 72
    )


if __name__ == "__main__":
    main()
