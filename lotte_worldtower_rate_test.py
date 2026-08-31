#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import json
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests


# ============================================================
# LOTTE CINEMA WORLDTOWER MONITOR
#
# 실데이터로 확인된 롯데 상태/이벤트 값
# ------------------------------------------------------------
# AccompanyTypeCode=30 -> 무대인사
# AccompanyTypeCode=40 -> GV
#
# IsBookingYN=Y -> 예매 가능
# IsBookingYN=E -> 매진
# IsBookingYN=N -> 예매 마감/상영 시작 등 예매 불가
#
# "상영준비중"의 전용 코드값은 아직 실데이터 표본이 없음.
# 따라서:
# 1) API 응답에 "상영준비중/예매준비중" 문구가 직접 있으면 상영준비중으로 처리
# 2) Y/E/N 이외의 새 상태는 GitHub Actions 진단 로그에만 기록
# 3) 화면 표시보다 API에 GV/무대인사 신호가 먼저 생기면 선행 진단 로그 기록
#
# 감시 범위
# ------------------------------------------------------------
# 오늘~49일 뒤: 43일 전체를 10초 목표로 순차 조회
# 실제 43일 조회 시간이 10초를 넘으면 조회 완료 즉시 다음 사이클 시작
# ============================================================


LOTTE_API = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"

SITE_NAME = "롯데시네마 월드타워"
CINEMA_ID = "1|0001|1016"
CINEMA_CODE = "1016"

TOTAL_DAYS = 43

# CGV와 같은 날짜별 분산 감시
INTERVAL_TODAY = 300.0       # 오늘: 5분
INTERVAL_TOMORROW = 20.0     # 내일(+1): 20초
INTERVAL_2_4 = 90.0          # +2~+4일: 90초
INTERVAL_5_14 = 30.0         # +5~+14일: 30초
INTERVAL_15_30 = 60.0        # +15~+30일: 60초
INTERVAL_31_42 = 300.0       # +31~+42일: 5분
INTERVAL_PRIORITY = 20.0     # 상영준비중/매진 날짜: 20초
IDLE_SLEEP = 0.15

RUN_SECONDS = int(os.getenv("RUN_SECONDS", "19200"))

# GitHub Actions 로그 폭증 방지:
# - 정상 API 날짜별 로그는 숨김
# - 첫 43일 스캔 완료 시 1회 요약
# - 이후 10분마다 정상 동작 요약(환경변수로 변경 가능)
# - 새 이벤트/상태 변화/API 오류는 즉시 상세 로그
STATUS_LOG_SECONDS = int(os.getenv("STATUS_LOG_SECONDS", "600"))
VERBOSE_API_LOGS = os.getenv("VERBOSE_API_LOGS", "0").strip() == "1"

# 현재 연속 50일 감시는 그대로 유지한다.
# KST 매시 00/05/10/.../55분에는 +4일~+21일(18일)을
# 18개 worker로 동시에 확인하는 실험용 고정 점검을 추가한다.
FIXED_SAFETY_SCAN_MINUTES = 5
FIXED_CONCURRENT_START_OFFSET = 4
FIXED_CONCURRENT_DAYS = 18
FIXED_CONCURRENT_WORKERS = 18

# 같은 1석의 짧은 매진 <-> 예매가능 흔들림 반복 알림 방지
CANCEL_REARM_SECONDS = 120

DISCORD_WEBHOOK = os.getenv(
    "DISCORD_LOTTE_WORLDTOWER",
    "",
).strip()

DISCORD_MENTION_ID = "1383846907847381184"

STATE_FILE = Path("seen_lotte_worldtower.json")
BASELINE_FILE = Path("baseline_lotte_worldtower.done")

KST = ZoneInfo("Asia/Seoul")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

SESSION_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.lottecinema.co.kr/NLCHS/Ticketing",
    "Origin": "https://www.lottecinema.co.kr",
}

_THREAD_LOCAL = threading.local()

def get_session():
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(SESSION_HEADERS)
        _THREAD_LOCAL.session = session
    return session


# ============================================================
# Basic helpers
# ============================================================

def log(message=""):
    print(message, flush=True)


def now_kst():
    return datetime.now(KST)


def norm(value):
    return " ".join(str(value or "").split())


def compact(value):
    return re.sub(r"\s+", "", norm(value))


def bounded_gv(text):
    return bool(
        re.search(
            r"(?<![A-Z0-9])GV(?![A-Z0-9])",
            str(text or "").upper(),
        )
    )


def lotte_post(payload):
    files = {
        "paramList": (
            None,
            json.dumps(
                payload,
                ensure_ascii=False,
            ),
        )
    }

    response = get_session().post(
        LOTTE_API,
        files=files,
        timeout=15,
    )
    response.raise_for_status()

    return response.json(), response


def scalar_texts(obj):
    result = []

    def walk(value):
        if isinstance(value, dict):
            for child in value.values():
                walk(child)

        elif isinstance(value, list):
            for child in value:
                walk(child)

        elif value is not None:
            text = norm(value)
            if text:
                result.append(text)

    walk(obj)
    return result


def all_scalar_text(obj):
    return " | ".join(
        scalar_texts(obj)
    )


# ============================================================
# Event classification
# ============================================================

# 롯데가 행사정보를 넣는 대표 필드들.
# MovieName/ScreenName 같은 일반 표시 필드는 GV 오탐 방지를 위해 제외한다.
EVENT_FIELDS = (
    "AccompanyTypeCode",
    "AccompanyTypeNameKR",
    "AccompanyTypeNameUS",
    "AccompanyTypeName",
    "EventNameKR",
    "EventNameUS",
    "EventName",
    "EventTypeName",
    "EventTypeNameKR",
    "EventTypeNameUS",
    "SpecialName",
    "SpecialNameKR",
    "SpecialNameUS",
    "SpecialType",
    "SpecialTypeName",
    "SpecialTypeNameKR",
    "SpecialTypeNameUS",
    "SpecialMsg",
    "SpecialMsgKR",
    "SpecialMsgUS",
    "SpecialScreenName",
    "PlayKind",
    "PlayKindName",
    "PlayKindNameKR",
    "PlayKindNameUS",
    "MovieKindName",
    "RepresentationMovieTypeName",
)

SHORT_STAGE_FIELDS = (
    "EventNameKR",
    "EventName",
    "EventTypeName",
    "EventTypeNameKR",
    "SpecialName",
    "SpecialNameKR",
    "SpecialType",
    "SpecialTypeName",
    "SpecialTypeNameKR",
    "PlayKind",
    "PlayKindName",
    "PlayKindNameKR",
)

# 부모 객체에 행사정보가 있고 실제 회차(StartTime/Screen)가 자식 객체인 경우
# 자식 회차로 이어받을 필드.
EVENT_CONTEXT_FIELDS = tuple(dict.fromkeys(EVENT_FIELDS + (
    "AccompanyTypeCode",
    "AccompanyTypeNameKR",
    "AccompanyTypeNameUS",
    "AccompanyTypeName",
)))


def normalize_event_code(value):
    """30, '30', '030', '30.0'을 모두 30으로 통일."""
    text = norm(value)
    if not text:
        return ""

    m = re.fullmatch(r"0*(\d+)(?:\.0+)?", text)
    if m:
        try:
            return str(int(m.group(1)))
        except Exception:
            pass
    return text


def classify_event(row):
    # 실데이터로 확인된 가장 강한 신호.
    accompany_code = normalize_event_code(
        row.get("AccompanyTypeCode")
    )

    # 70은 4K로 확인된 코드. 절대 GV/무대인사로 취급하지 않는다.
    if accompany_code == "70":
        return None

    if accompany_code == "30":
        return "STAGE"

    if accompany_code == "40":
        return "GV"

    focused = " | ".join(
        norm(row.get(key))
        for key in EVENT_FIELDS
        if norm(row.get(key))
    )
    fc = compact(focused)

    # 행사명 기반 보조 판별.
    if (
        "무대인사" in fc
        or "舞台挨拶" in focused
    ):
        return "STAGE"

    if any(
        compact(row.get(key)) == "무대"
        for key in SHORT_STAGE_FIELDS
    ):
        return "STAGE"

    if (
        "관객과의대화" in fc
        or bounded_gv(focused)
    ):
        return "GV"

    return None


# ============================================================
# Booking status
# ============================================================

BOOKING_TEXT_FIELDS = (
    "BookingStatusName",
    "BookingStatusNameKR",
    "BookingStateName",
    "BookingStateNameKR",
    "TicketingStatusName",
    "TicketingStatusNameKR",
    "SaleStatusName",
    "SaleStatusNameKR",
    "StatusName",
    "StatusNameKR",
)


def booking_state(row):
    # --------------------------------------------------------
    # 1) 실제 문구가 API에 내려오면 가장 우선
    # --------------------------------------------------------

    status_text = " | ".join(
        norm(row.get(key))
        for key in BOOKING_TEXT_FIELDS
        if norm(row.get(key))
    )

    # 상태 전용 필드에 없더라도 row 전체에서 한 번 더 확인.
    full_text = (
        status_text
        + " | "
        + all_scalar_text(row)
    )

    fc = compact(full_text)

    if (
        "상영준비중" in fc
        or "예매준비중" in fc
    ):
        return (
            "PREPARING",
            "explicit_preparing_text",
            "",
        )

    if (
        "예매가능" in fc
        or "예매하기" in fc
    ):
        return (
            "OPEN",
            "explicit_open_text",
            "",
        )

    # --------------------------------------------------------
    # 2) 실제 전국/월드타워 API로 확인된 IsBookingYN
    # --------------------------------------------------------

    value = row.get("IsBookingYN")

    if value is None:
        for key in (
            "BookingYN",
            "IsBookingYn",
            "isBookingYN",
            "SaleYN",
        ):
            if key in row:
                value = row.get(key)
                break

    code = str(
        value or ""
    ).strip().upper()

    if code in {
        "Y",
        "YES",
        "TRUE",
        "1",
    }:
        return (
            "OPEN",
            "IsBookingYN=Y",
            code,
        )

    # 실데이터: 매진
    if code == "E":
        return (
            "SOLD_OUT",
            "IsBookingYN=E",
            code,
        )

    # 실데이터: 이미 예매 불가능한 회차
    # (상영 시작/예매 마감 등)
    if code in {
        "N",
        "NO",
        "FALSE",
        "0",
    }:
        return (
            "CLOSED",
            "IsBookingYN=N",
            code,
        )

    return (
        "UNKNOWN",
        f"IsBookingYN={code or 'missing'}",
        code or "(blank)",
    )


# ============================================================
# Sequence extraction
# ============================================================


def _event_context_from_dict(value):
    result = {}
    if not isinstance(value, dict):
        return result

    for key in EVENT_CONTEXT_FIELDS:
        if key in value and norm(value.get(key)):
            result[key] = value.get(key)
    return result


def extract_sequences(
    data,
    date,
    fallback_movie="",
    fallback_code="",
):
    rows = []

    def walk(value, inherited_event=None):
        inherited_event = dict(inherited_event or {})

        if isinstance(value, dict):
            # 부모의 행사 필드 + 현재 객체의 행사 필드 결합.
            # 현재 객체의 값이 있으면 현재 값이 우선한다.
            event_context = dict(inherited_event)
            event_context.update(_event_context_from_dict(value))

            start = norm(
                value.get("StartTime")
                or value.get("PlayStartTime")
                or value.get("StartTm")
            )
            screen = norm(
                value.get("ScreenNameKR")
                or value.get("ScreenName")
                or value.get("ScreenID")
            )

            if start and screen:
                # 실제 회차 객체에 부모 행사정보를 합쳐 판별한다.
                merged_raw = dict(event_context)
                merged_raw.update(value)

                state, source, raw_status = booking_state(merged_raw)

                rows.append({
                    "date": date,
                    "movie": norm(
                        merged_raw.get("MovieNameKR")
                        or merged_raw.get("MovieName")
                        or fallback_movie
                    ),
                    "movie_code": norm(
                        merged_raw.get("RepresentationMovieCode")
                        or merged_raw.get("MovieCode")
                        or fallback_code
                    ),
                    "time": start,
                    "end_time": norm(merged_raw.get("EndTime")),
                    "screen": screen,
                    "screen_id": norm(
                        merged_raw.get("ScreenID")
                        or merged_raw.get("ScreenId")
                        or merged_raw.get("ScreenCode")
                    ),
                    "play_sequence": norm(
                        merged_raw.get("PlaySequence")
                        or merged_raw.get("PlaySeq")
                        or merged_raw.get("Sequence")
                    ),
                    "remain": norm(
                        merged_raw.get("BookingSeatCount")
                        or merged_raw.get("RemainSeatCount")
                        or merged_raw.get("RemainingSeatCount")
                    ),
                    "total": norm(
                        merged_raw.get("TotalSeatCount")
                        or merged_raw.get("SeatCount")
                    ),
                    "event_type": classify_event(merged_raw),
                    "booking_state": state,
                    "booking_state_source": source,
                    "raw_booking_code": raw_status,
                    "raw": merged_raw,
                })

            for child in value.values():
                walk(child, event_context)

        elif isinstance(value, list):
            for child in value:
                walk(child, inherited_event)

    walk(data, {})
    return rows


def show_key(show):
    raw = "|".join([
        CINEMA_CODE,
        show.get("date", ""),
        show.get("movie_code", "") or compact(show.get("movie", "")),
        show.get("screen_id", "") or compact(show.get("screen", "")),
        show.get("play_sequence", "") or show.get("time", ""),
        show.get("time", ""),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _show_quality(show):
    raw = show.get("raw") or {}
    event = show.get("event_type")

    score = 0
    if event in {"GV", "STAGE"}:
        score += 1000

    # 코드 30/40이 직접 있으면 행사명 텍스트만 있는 것보다 더 강한 표본.
    code = normalize_event_code(raw.get("AccompanyTypeCode"))
    if code in {"30", "40"}:
        score += 500

    score += 50 * sum(
        bool(norm(raw.get(key)))
        for key in (
            "AccompanyTypeNameKR",
            "EventNameKR",
            "EventTypeNameKR",
            "SpecialNameKR",
            "SpecialTypeNameKR",
            "PlayKindNameKR",
        )
    )

    score += sum(
        bool(show.get(field))
        for field in (
            "movie_code",
            "screen_id",
            "play_sequence",
            "end_time",
            "remain",
            "total",
        )
    )
    return score


def dedupe(shows):
    result = {}

    for show in shows:
        key = show_key(show)
        old = result.get(key)

        if old is None or _show_quality(show) > _show_quality(old):
            result[key] = show

    return list(result.values())


# ============================================================
# Lotte API scan
# ============================================================

MOVIE_CACHE = {
    "ts": 0.0,
    "items": [],
}


def get_movies(ttl=900):
    now = time.time()

    if MOVIE_CACHE["items"] and now - MOVIE_CACHE["ts"] < ttl:
        return MOVIE_CACHE["items"]

    payload = {
        "MethodName": "GetTicketingPageTOBE",
        "channelType": "HO",
        "osType": "W",
        "osVersion": UA,
        "memberOnNo": "0",
    }

    data, _ = lotte_post(payload)
    found = []

    def walk(value):
        if isinstance(value, dict):
            name = norm(value.get("MovieNameKR") or value.get("MovieName"))
            code = norm(
                value.get("RepresentationMovieCode")
                or value.get("MovieCode")
            )
            if name and code:
                found.append((code, name))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    items = list(dict.fromkeys(found))
    MOVIE_CACHE["ts"] = now
    MOVIE_CACHE["items"] = items
    return items


def fetch_movie_specific(date, movie_code, movie_name=""):
    payload = {
        "MethodName": "GetPlaySequence",
        "channelType": "HO",
        "osType": "W",
        "osVersion": UA,
        "playDate": date,
        "cinemaID": CINEMA_ID,
        "representationMovieCode": movie_code,
    }

    data, _ = lotte_post(payload)
    return extract_sequences(
        data,
        date,
        fallback_movie=movie_name,
        fallback_code=movie_code,
    )


def _future_enrich_needed(date, rows):
    """
    롯데는 먼 날짜에서 전체조회에는 회차만 주고 행사정보를 생략하는 표본이 있음.
    오늘+14일 이후에 실제 회차가 있으면, 그 날짜에 등장한 영화코드만 영화별 재조회한다.
    """
    if not rows:
        return False

    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
        offset = (d - now_kst().date()).days
    except Exception:
        return False

    return offset >= 14


def fetch_date_primary(date):
    payload = {
        "MethodName": "GetPlaySequence",
        "channelType": "HO",
        "osType": "W",
        "osVersion": UA,
        "playDate": date,
        "cinemaID": CINEMA_ID,
        "representationMovieCode": "",
    }

    started = time.time()
    data, response = lotte_post(payload)

    primary_rows = dedupe(extract_sequences(data, date))
    primary_event_count = sum(
        row.get("event_type") in {"GV", "STAGE"}
        for row in primary_rows
    )

    rows = list(primary_rows)
    enrich_calls = 0
    enrich_added_events = 0

    # 미래 sparse 회차는 영화별 조회에서 AccompanyTypeCode가 보이는 경우를 보완.
    if _future_enrich_needed(date, primary_rows):
        movie_map = {}
        for row in primary_rows:
            code = norm(row.get("movie_code"))
            name = norm(row.get("movie"))
            if code:
                movie_map.setdefault(code, name)

        for movie_code, movie_name in movie_map.items():
            try:
                specific_rows = fetch_movie_specific(
                    date,
                    movie_code,
                    movie_name,
                )
                enrich_calls += 1
                rows.extend(specific_rows)
            except Exception as error:
                log(
                    f"ENRICH {date.replace('-', '')} "
                    f"MOVIE={movie_code} ERROR: "
                    f"{type(error).__name__}: {error}"
                )

        rows = dedupe(rows)
        final_event_count = sum(
            row.get("event_type") in {"GV", "STAGE"}
            for row in rows
        )
        enrich_added_events = max(0, final_event_count - primary_event_count)
    else:
        final_event_count = primary_event_count

    if VERBOSE_API_LOGS:
        log(
            f"API {date.replace('-', '')} "
            f"STATUS={response.status_code} "
            f"TIME={time.time() - started:.2f}s "
            f"SIZE={len(response.content):,} bytes "
            f"ROWS={len(rows)} "
            f"EVENTS={final_event_count} "
            f"ENRICH_CALLS={enrich_calls} "
            f"ENRICH_NEW_EVENTS={enrich_added_events}"
        )

    return rows


def fallback_scan_dates(dates):
    movies = get_movies()

    log(
        "ALL-MOVIES MODE RETURNED 0 TOTAL ROWS "
        f"-> FALLBACK {len(movies)} MOVIES"
    )

    all_rows = []
    for date in dates:
        for movie_code, movie_name in movies:
            try:
                all_rows.extend(
                    fetch_movie_specific(date, movie_code, movie_name)
                )
            except Exception:
                pass

    return dedupe(all_rows)


def make_dates(start_offset, day_count):
    today = now_kst().date()
    return [
        (today + timedelta(days=start_offset + index)).strftime("%Y-%m-%d")
        for index in range(day_count)
    ]


def scan_dates(dates, label):
    all_rows = []
    errors = 0

    if VERBOSE_API_LOGS:
        log("")
        log(
            f"{label} SCAN: "
            f"{dates[0]} ~ {dates[-1]} "
            f"({len(dates)} DAYS)"
        )

    for date in dates:
        try:
            all_rows.extend(fetch_date_primary(date))
        except Exception as error:
            errors += 1
            log(
                f"API {date.replace('-', '')} ERROR: "
                f"{type(error).__name__}: {error}"
            )

    mode = "ALL_MOVIES+FUTURE_ENRICH"

    if not all_rows and errors < len(dates):
        try:
            all_rows = fallback_scan_dates(dates)
            mode = "FALLBACK"
        except Exception as error:
            log(f"FALLBACK ERROR: {type(error).__name__}: {error}")
            mode = "ERROR"

    events = {}
    for show in dedupe(all_rows):
        if show.get("event_type") in {"GV", "STAGE"}:
            events[show_key(show)] = show

    return events, mode, errors


def scan_all_43_days():
    dates = make_dates(0, TOTAL_DAYS)
    return scan_dates(dates, "FULL 43-DAY")



def reset_thread_session():
    try:
        _THREAD_LOCAL.session = None
    except Exception:
        pass


def base_interval_for_offset(offset):
    if offset == 0:
        return INTERVAL_TODAY
    if offset == 1:
        return INTERVAL_TOMORROW
    if 2 <= offset <= 4:
        return INTERVAL_2_4
    if 5 <= offset <= 14:
        return INTERVAL_5_14
    if 15 <= offset <= 30:
        return INTERVAL_15_30
    return INTERVAL_31_42


def priority_date(date, state):
    for item in state.values():
        if not isinstance(item, dict):
            continue
        if item.get("date") != date:
            continue
        if item.get("status") in {"PREPARING", "SOLD_OUT"}:
            return True
    return False


def interval_for_date(date, state):
    today = now_kst().date()
    target = datetime.strptime(date, "%Y-%m-%d").date()
    offset = (target - today).days

    base = base_interval_for_offset(offset)
    if priority_date(date, state):
        return min(base, INTERVAL_PRIORITY)
    return base


def scan_one_date_with_retry(date):
    """
    날짜 1개만 조회.
    ConnectionError/JSON 오류 등 일시 장애는 세션을 새로 만들고 1회 재시도.
    2번 모두 실패하면 None을 반환하고 기존 상태를 건드리지 않는다.
    """
    last_error = None

    for attempt in (1, 2):
        try:
            rows = fetch_date_primary(date)

            events = {}
            for show in dedupe(rows):
                if show.get("event_type") in {"GV", "STAGE"}:
                    events[show_key(show)] = show

            return events, None

        except Exception as error:
            last_error = error
            reset_thread_session()

            if attempt == 1:
                time.sleep(1.0)

    return None, last_error


def scan_fixed_18_days_concurrent():
    """
    매 5분 고정 안전점검용.
    +4일~+21일 18개 날짜를 18 worker로 동시에 시작한다.
    각 worker는 독립 requests.Session을 사용한다.

    주의: fetch_date_primary()의 기존 미래 날짜 보강(enrich) 로직도 그대로 유지된다.
    따라서 실제 요청 수는 날짜당 1회보다 늘어날 수 있다.
    이번 버전은 롯데 서버가 이 동시성을 버티는지 확인하는 테스트 버전이다.
    """
    dates = make_dates(
        FIXED_CONCURRENT_START_OFFSET,
        FIXED_CONCURRENT_DAYS,
    )

    rows_by_date = {}
    errors = 0
    error_examples = []

    with ThreadPoolExecutor(
        max_workers=FIXED_CONCURRENT_WORKERS
    ) as executor:
        future_map = {
            executor.submit(fetch_date_primary, date): date
            for date in dates
        }

        for future in as_completed(future_map):
            date = future_map[future]
            try:
                rows_by_date[date] = future.result()
            except Exception as error:
                errors += 1
                if len(error_examples) < 3:
                    error_examples.append(
                        f"{date}: {type(error).__name__}: {error}"
                    )

    all_rows = []
    for date in dates:
        all_rows.extend(rows_by_date.get(date, []))

    events = {}
    for show in dedupe(all_rows):
        if show.get("event_type") in {"GV", "STAGE"}:
            events[show_key(show)] = show

    if error_examples:
        log(
            "⚠️ 18일 동시점검 오류 예시: "
            + " | ".join(error_examples)
        )

    return events, "CONCURRENT_18DAY", errors


# ============================================================
# State
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        return (
            data
            if isinstance(data, dict)
            else {}
        )

    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def record(show, status):
    return {
        "status": status,

        "event_type": show.get(
            "event_type"
        ),

        "date": show.get(
            "date"
        ),

        "movie": show.get(
            "movie"
        ),

        "movie_code": show.get(
            "movie_code"
        ),

        "time": show.get(
            "time"
        ),

        "screen": show.get(
            "screen"
        ),

        "screen_id": show.get(
            "screen_id"
        ),

        "play_sequence": show.get(
            "play_sequence"
        ),

        "booking_state_source": show.get(
            "booking_state_source"
        ),

        "raw_booking_code": show.get(
            "raw_booking_code"
        ),

        "updated_at_kst": now_kst().isoformat(
            timespec="seconds"
        ),
    }


# ============================================================
# Discord
# ============================================================

def booking_url(show):
    params = {
        "link_channelCode": "naver",
        "link_cinemaCode": CINEMA_CODE,
        "link_date": show.get(
            "date",
            "",
        ),
        "link_movieCd": show.get(
            "movie_code",
            "",
        ),
        "link_screenId": show.get(
            "screen_id",
            "",
        ),
        "link_time": show.get(
            "time",
            "",
        ),
    }

    if (
        params["link_movieCd"]
        and params["link_screenId"]
        and params["link_time"]
    ):
        return (
            "https://www.lottecinema.co.kr/"
            "NLCMW/ticketing?"
            + urlencode(params)
        )

    return (
        "https://www.lottecinema.co.kr/"
        "NLCMW/ticketing"
    )


def event_name(show):
    return (
        "무대인사"
        if show.get("event_type") == "STAGE"
        else "GV"
    )


def discord_post(content):
    if not DISCORD_WEBHOOK.startswith(
        "https://discord.com/api/webhooks/"
    ):
        raise RuntimeError(
            "DISCORD_LOTTE_WORLDTOWER "
            "Secret이 올바르지 않습니다."
        )

    payload = {
        "content": content,

        # Discord link preview 제거
        "flags": 4,

        "allowed_mentions": {
            "parse": [],
            "users": [
                DISCORD_MENTION_ID
            ],
        },
    }

    response = requests.post(
        DISCORD_WEBHOOK,
        json=payload,
        timeout=15,
    )

    response.raise_for_status()


def show_when(show):
    start = show.get(
        "time",
        "",
    )

    end = show.get(
        "end_time",
        "",
    )

    if end:
        return f"{start} ~ {end}"

    return start


def send_alert(
    show,
    status,
):
    # 취소표 알림은 별도 간결 형식
    if status == "CANCEL_TICKET":
        start = show.get("time", "")
        end = show.get("end_time", "")
        time_text = (
            f"{start}–{end}"
            if end
            else start
        )

        movie = (
            show.get("movie")
            or "영화명 확인 필요"
        )
        screen = (
            show.get("screen")
            or "상영관 정보 없음"
        )
        link = booking_url(show)

        content = (
            f"<@{DISCORD_MENTION_ID}>\n"
            f"**🎟️ 취소표가 생겼습니다**\n"
            f"**🎬 {SITE_NAME} · {event_name(show)}**\n"
            f"**📅 {show.get('date')}**\n"
            f"**[🎟 {time_text} · {movie} · {screen}]({link})**"
        )

        discord_post(
            content
        )
        return

    if status == "PREPARING":
        title = (
            f"⏳ {SITE_NAME} "
            f"{event_name(show)} 상영준비중"
        )

        action = (
            "예매가 열리는지 계속 확인합니다."
        )

    else:
        title = (
            f"🚨 {SITE_NAME} "
            f"{event_name(show)} 예매 오픈!"
        )

        action = (
            "지금 바로 예매 확인해."
        )

    seat = ""

    if show.get("remain"):
        seat = (
            f"\n💺 잔여 "
            f"{show['remain']}"
        )

        if show.get("total"):
            seat += (
                f" / {show['total']}"
            )

    content = (
        f"<@{DISCORD_MENTION_ID}>\n"
        f"**{title}**\n"
        f"🎬 "
        f"{show.get('movie') or '영화명 확인 필요'}\n"
        f"📅 {show.get('date')}   "
        f"⏰ {show_when(show)}\n"
        f"🎞️ "
        f"{show.get('screen') or '상영관 정보 없음'}"
        f"{seat}\n"
        f"{action}\n"
        f"🎟️ {booking_url(show)}"
    )

    discord_post(
        content
    )


def diagnostic_fields(show):
    row = show.get(
        "raw"
    ) or {}

    result = {}

    for key, value in row.items():
        if isinstance(
            value,
            (
                dict,
                list,
            ),
        ):
            continue

        text = norm(value)

        if not text:
            continue

        key_lower = str(
            key
        ).lower()

        if (
            any(
                word in key_lower
                for word in (
                    "event",
                    "special",
                    "kind",
                    "type",
                    "accompany",
                    "booking",
                    "sale",
                    "screen",
                    "movie",
                    "play",
                    "status",
                    "sequence",
                )
            )
            or "무대" in compact(text)
            or "관객과의대화" in compact(text)
            or bounded_gv(text)
        ):
            result[
                str(key)
            ] = text

    return result


def send_unknown_status(show):
    fields = diagnostic_fields(
        show
    )

    field_lines = []

    for key, value in fields.items():
        line = (
            f"• {key} = {value}"
        )

        # Discord 2000자 제한 대비
        if sum(
            len(x)
            for x in field_lines
        ) < 900:
            field_lines.append(
                line
            )

    details = "\n".join(
        field_lines
    )

    content = (
        f"<@{DISCORD_MENTION_ID}>\n"
        f"**⚠️ 롯데시네마 월드타워 "
        f"{event_name(show)} 미확인 예매 상태 발견**\n"
        f"🎬 "
        f"{show.get('movie') or '영화명 확인 필요'}\n"
        f"📅 {show.get('date')}   "
        f"⏰ {show_when(show)}\n"
        f"🎞️ "
        f"{show.get('screen') or '상영관 정보 없음'}\n"
        f"🔎 상태: "
        f"{show.get('booking_state_source')}\n"
        f"아직 정의하지 않은 롯데 상태입니다. "
        f"상영준비중 표본일 수 있으니 확인 필요.\n"
    )

    if details:
        content += (
            "\n"
            + details
        )

    # Discord 길이 안전 처리
    discord_post(
        content[:1950]
    )


def send_test():
    discord_post(
        f"<@{DISCORD_MENTION_ID}>\n"
        f"**⏳ {SITE_NAME} "
        f"무대인사 상영준비중 테스트**\n"
        f"상영준비중 알림 전송 정상.\n"
        f"※ 실제 회차가 아닌 테스트입니다."
    )

    time.sleep(1)

    discord_post(
        f"<@{DISCORD_MENTION_ID}>\n"
        f"**🚨 {SITE_NAME} "
        f"무대인사 예매 오픈 테스트**\n"
        f"예매 오픈 알림 전송 정상.\n"
        f"※ 실제 회차가 아닌 테스트입니다."
    )

    time.sleep(1)

    discord_post(
        f"<@{DISCORD_MENTION_ID}>\n"
        f"**🎟️ {SITE_NAME} "
        f"무대인사 취소표가 생겼습니다 테스트**\n"
        f"취소표 알림 전송 정상.\n"
        f"※ 실제 회차가 아닌 테스트입니다."
    )

    log(
        "DISCORD TEST COMPLETE: "
        "상영준비중 + 예매 오픈 + 취소표"
    )


# ============================================================
# Diagnostics
# ============================================================

def counts(events):
    values = list(
        events.values()
    )

    return {
        "GV": sum(
            item.get("event_type") == "GV"
            for item in values
        ),

        "STAGE": sum(
            item.get("event_type") == "STAGE"
            for item in values
        ),

        "PREPARING": sum(
            item.get("booking_state") == "PREPARING"
            for item in values
        ),

        "OPEN": sum(
            item.get("booking_state") == "OPEN"
            for item in values
        ),

        "SOLD_OUT": sum(
            item.get("booking_state") == "SOLD_OUT"
            for item in values
        ),

        "CLOSED": sum(
            item.get("booking_state") == "CLOSED"
            for item in values
        ),

        "UNKNOWN": sum(
            item.get("booking_state") == "UNKNOWN"
            for item in values
        ),
    }


def print_counts(
    events,
    prefix="",
):
    c = counts(
        events
    )

    if prefix:
        log(prefix)

    log(
        f"전체 GV/무대인사 회차: "
        f"{len(events)}"
    )
    log(
        f"GV: {c['GV']}"
    )
    log(
        f"무대인사: {c['STAGE']}"
    )
    log(
        f"상영준비중: {c['PREPARING']}"
    )
    log(
        f"예매 가능: {c['OPEN']}"
    )
    log(
        f"매진: {c['SOLD_OUT']}"
    )
    log(
        f"예매 불가/마감: {c['CLOSED']}"
    )
    log(
        f"미확인 상태: {c['UNKNOWN']}"
    )


def print_all_events(events):
    log("")
    log(
        "=" * 60
    )

    log(
        "전체 GV/무대인사 회차"
    )

    log(
        "=" * 60
    )

    if not events:
        log(
            "GV/무대인사 회차 없음"
        )
        return

    ordered = sorted(
        events.values(),
        key=lambda show: (
            show.get(
                "date",
                "",
            ),
            show.get(
                "time",
                "",
            ),
            show.get(
                "movie",
                "",
            ),
            show.get(
                "screen",
                "",
            ),
        ),
    )

    for show in ordered:
        log(
            f"[{event_name(show)}] "
            f"{show.get('date')} / "
            f"{show.get('movie')} / "
            f"{show.get('time')} / "
            f"{show.get('screen')} / "
            f"{state_name_korean(show.get('booking_state'))} "
            f"({show.get('booking_state_source')})"
        )

        fields = diagnostic_fields(
            show
        )

        for key, value in fields.items():
            log(
                f"  {key} = {value}"
            )

        log(
            "-" * 60
        )


# ============================================================
# API 선행 진단 로그 (Discord 전송 안 함)
# ============================================================

def state_name_korean(status):
    return {
        "PREPARING": "상영준비중",
        "OPEN": "예매 가능",
        "SOLD_OUT": "매진",
        "CLOSED": "예매 불가/마감",
        "UNKNOWN": "미확인",
    }.get(status, norm(status) or "미확인")


def log_new_event_diagnostics(events, state):
    """
    화면 표시 여부와 무관하게 롯데 API에 GV/무대인사 신호가 먼저 생기면
    GitHub Actions 로그에 최초 1회 표시한다.
    Discord 알림은 여기서 보내지 않는다.
    """
    detected = 0

    ordered = sorted(
        events.items(),
        key=lambda item: (
            item[1].get("date", ""),
            item[1].get("time", ""),
            item[1].get("movie", ""),
            item[1].get("screen", ""),
        ),
    )

    for key, show in ordered:
        # 이미 state에 있으면 이전 사이클/기준값에서 본 회차이므로 반복 출력 안 함.
        if key in state:
            continue

        kind = event_name(show)
        row = show.get("raw") or {}
        accompany_code = norm(row.get("AccompanyTypeCode")) or "-"
        accompany_name = norm(
            row.get("AccompanyTypeNameKR")
            or row.get("AccompanyTypeName")
        ) or "-"

        log("")
        log("🔎 API 선행 진단")
        log(f"{kind}가 감지됐습니다")
        log(
            f"{show.get('date')} / "
            f"{show.get('movie') or '영화명 확인 필요'} / "
            f"{show_when(show)} / "
            f"{show.get('screen') or '상영관 정보 없음'}"
        )
        log(
            f"상태: {state_name_korean(show.get('booking_state'))} / "
            f"AccompanyTypeCode={accompany_code} / "
            f"AccompanyTypeNameKR={accompany_name}"
        )
        detected += 1

    return detected


# ============================================================
# Baseline
# ============================================================

def make_baseline(events):
    state = {
        key: record(
            show,
            show.get(
                "booking_state",
                "UNKNOWN",
            ),
        )
        for key, show in events.items()
    }

    save_state(
        state
    )

    BASELINE_FILE.write_text(
        now_kst().isoformat(
            timespec="seconds"
        ),
        encoding="utf-8",
    )

    log("")
    log(
        "=" * 60
    )

    log(
        "INITIAL BASELINE"
    )

    log(
        "=" * 60
    )

    log(
        "현재 GV / 무대인사 회차를 "
        "알림 없이 기준값으로 등록합니다."
    )

    print_counts(
        events
    )

    log(
        f"BASELINE EVENT COUNT: "
        f"{len(events)}"
    )

    log(
        f"STATE SAVED: "
        f"{len(state)}"
    )

    log(
        "BASELINE COMPLETE"
    )

    log(
        "이번 실행에서는 실제 회차 "
        "Discord 알림을 보내지 않았습니다."
    )

    log(
        "기준값 상세 목록 출력은 로그 절약을 위해 생략합니다."
    )


# ============================================================
# State transition processing
# ============================================================

def process(
    events,
    state,
):
    sent = 0
    unknown_logged = 0

    for key, show in events.items():
        current = show.get(
            "booking_state",
            "UNKNOWN",
        )

        previous_record = (
            state.get(key)
            or {}
        )

        previous = previous_record.get(
            "status"
        )

        # ----------------------------------------------------
        # 미확인 예매 상태: Discord가 아니라 Actions 로그에만 기록
        # ----------------------------------------------------

        if current == "UNKNOWN":
            previous_raw = previous_record.get(
                "raw_booking_code"
            )
            current_raw = show.get(
                "raw_booking_code"
            )

            should_diagnose = (
                previous is None
                or previous != "UNKNOWN"
                or previous_raw != current_raw
            )

            if should_diagnose:
                log("")
                log("⚠️ 예매 상태 진단")
                log(f"{event_name(show)}가 감지됐습니다")
                log(
                    f"{show.get('date')} / "
                    f"{show.get('movie') or '영화명 확인 필요'} / "
                    f"{show_when(show)} / "
                    f"{show.get('screen') or '상영관 정보 없음'}"
                )
                log(
                    f"아직 정의하지 않은 예매 상태: "
                    f"{show.get('booking_state_source')}"
                )

                fields = diagnostic_fields(show)
                for field_key, field_value in fields.items():
                    log(f"  {field_key} = {field_value}")

                unknown_logged += 1

            state[key] = record(
                show,
                "UNKNOWN",
            )
            save_state(state)
            continue

        # ----------------------------------------------------
        # 매진 / 마감은 알림 없음.
        # 매진 시각은 저장해서 이후 취소표 재오픈을 구분한다.
        # ----------------------------------------------------

        if current in {
            "SOLD_OUT",
            "CLOSED",
        }:
            new_record = record(
                show,
                current,
            )

            if current == "SOLD_OUT":
                if (
                    previous == "SOLD_OUT"
                    and previous_record.get("sold_out_since_kst")
                ):
                    new_record["sold_out_since_kst"] = (
                        previous_record["sold_out_since_kst"]
                    )
                else:
                    new_record["sold_out_since_kst"] = (
                        now_kst().isoformat(timespec="seconds")
                    )

            state[key] = new_record
            save_state(state)
            continue

        # ----------------------------------------------------
        # 상영준비중 / 예매 가능
        # ----------------------------------------------------

        alert_status = None

        if current == "PREPARING":
            if previous in (
                None,
                "UNKNOWN",
                "CLOSED",
                "SOLD_OUT",
            ):
                alert_status = "PREPARING"

            # 이미 예매 가능했던 회차가 준비중으로 되돌아오는 이상 신호는
            # 오판 방지를 위해 준비중 알림을 재전송하지 않는다.
            elif previous == "OPEN":
                state[key] = record(
                    show,
                    "OPEN",
                )
                save_state(state)
                continue

        elif current == "OPEN":
            if previous in (
                None,
                "UNKNOWN",
                "PREPARING",
                "CLOSED",
            ):
                alert_status = "OPEN"

            elif previous == "SOLD_OUT":
                sold_out_since_text = previous_record.get(
                    "sold_out_since_kst"
                )
                sold_out_seconds = None

                if sold_out_since_text:
                    try:
                        sold_out_since = datetime.fromisoformat(
                            sold_out_since_text
                        )
                        sold_out_seconds = (
                            now_kst() - sold_out_since
                        ).total_seconds()
                    except Exception:
                        sold_out_seconds = None

                # 120초 이상 매진 상태였다가 Y로 돌아오면 취소표로 구분.
                # 구버전 state에 매진 시작 시각이 없으면 최초 1회는 허용.
                if (
                    sold_out_seconds is None
                    or sold_out_seconds >= CANCEL_REARM_SECONDS
                ):
                    alert_status = "CANCEL_TICKET"
                else:
                    log(
                        f"짧은 매진↔예매가능 흔들림 억제: "
                        f"{event_name(show)} / "
                        f"{show.get('movie')} / "
                        f"{show.get('date')} "
                        f"{show.get('time')} / "
                        f"매진 유지 {sold_out_seconds:.0f}초 "
                        f"< {CANCEL_REARM_SECONDS}초"
                    )

                    state[key] = record(
                        show,
                        "OPEN",
                    )
                    save_state(state)
                    continue

        if alert_status:
            try:
                send_alert(
                    show,
                    alert_status,
                )
            except Exception as error:
                log(
                    f"DISCORD ERROR: "
                    f"{event_name(show)} / "
                    f"{show.get('movie')} / "
                    f"{show.get('date')} "
                    f"{show.get('time')} / "
                    f"{error}"
                )
                continue

            # 취소표 알림도 현재 실제 상태는 OPEN으로 저장해야
            # 다음 사이클에 같은 알림이 반복되지 않는다.
            stored_status = (
                "OPEN"
                if alert_status in {"OPEN", "CANCEL_TICKET"}
                else "PREPARING"
            )

            state[key] = record(
                show,
                stored_status,
            )
            save_state(state)
            sent += 1

            if alert_status == "PREPARING":
                transition = "상영준비중"
                icon = "⏳"
            elif alert_status == "CANCEL_TICKET":
                transition = "취소표가 생겼습니다"
                icon = "🎟️"
            elif previous == "PREPARING":
                transition = "상영준비중 -> 예매 오픈"
                icon = "🚨"
            else:
                transition = "예매 오픈"
                icon = "🚨"

            log(
                f"{icon} {transition}: "
                f"{event_name(show)} / "
                f"{show.get('movie')} / "
                f"{show.get('date')} "
                f"{show.get('time')}"
            )
            continue

        # 동일 상태 유지.
        state[key] = record(
            show,
            previous or current,
        )
        save_state(state)

    return (
        sent,
        unknown_logged,
    )


# ============================================================
# Main
# ============================================================

def main():
    log("=" * 60)
    log("LOTTE CINEMA GV/STAGE MONITOR - WORLDTOWER 1016")
    log("=" * 60)
    log("대상: GV / 무대인사")
    log("이벤트 코드: AccompanyTypeCode 30=무대인사, 40=GV")
    log("예매 상태: Y=예매 가능 / E=매진 / N=예매 불가·마감")
    log("상영준비중: API의 상영준비중/예매준비중 문구를 감지")
    log("선행 진단: API에서 GV/무대인사 신호가 처음 보이면 Actions 로그에 표시")
    log("취소표: 매진 후 재오픈 시 '취소표가 생겼습니다'로 별도 알림")
    log("감시 범위: 오늘 ~ +42일 (43일 전체)")
    log(
        "분산 감시: 오늘 5분 / 내일(+1) 20초 / +2~+4일 90초 / "
        "+5~+14일 30초 / +15~+30일 60초 / +31~+42일 5분 / "
        "상영준비중·매진 날짜 20초"
    )
    log(f"RUN SECONDS: {RUN_SECONDS}")
    log(f"상태 로그: 첫 43일 확인 완료 즉시 + 이후 {STATUS_LOG_SECONDS // 60}분마다 요약")
    log("5분 고정 동시점검: KST 매시 00/05/10/.../55분에 +4~+21일 18일을 18개 worker로 동시 확인")
    log("정상 날짜별 API 로그: 생략 (오류/새 신호/상태 변화는 즉시 표시)")
    log("=" * 60)

    # 강제 Discord 테스트. baseline/state는 건드리지 않음.
    if (
        os.getenv(
            "LOTTE_ALERT_TEST",
            "",
        ).strip()
        == "1"
    ):
        send_test()
        return

    state = load_state()

    # 최초 정상 실행은 43일 전체 baseline.
    if not BASELINE_FILE.exists():
        events, mode, errors = scan_all_43_days()
        log(f"FETCH MODE: {mode}")
        log(f"SCAN ERRORS: {errors}")
        make_baseline(events)
        return

    started = time.time()

    heartbeat_started = started
    heartbeat_requests = 0
    heartbeat_success = 0
    heartbeat_errors = 0
    heartbeat_alerts = 0

    dates = make_dates(0, TOTAL_DAYS)
    date_event_cache = {}

    # 실행 직후 43일 전체를 한 번 확인해 현재 캐시를 만든다.
    initial_started = time.time()
    initial_events, initial_mode, initial_errors = scan_all_43_days()

    for date in dates:
        date_event_cache[date] = {
            key: event
            for key, event in initial_events.items()
            if event.get("date") == date
        }

    log_new_event_diagnostics(initial_events, state)
    initial_sent, _ = process(initial_events, state)
    save_state(state)

    initial_elapsed = time.time() - initial_started
    initial_counts = counts(initial_events)

    log(
        f"✅ 첫 43일 감시 확인 완료 | {initial_elapsed:.1f}초 | "
        f"GV {initial_counts['GV']} | 무대인사 {initial_counts['STAGE']} | "
        f"상영준비중 {initial_counts['PREPARING']} | "
        f"예매가능 {initial_counts['OPEN']} | 매진 {initial_counts['SOLD_OUT']} | "
        f"오류 {initial_errors} | 알림 {initial_sent}"
    )

    now_mono = time.monotonic()
    next_due = {}

    today_date = now_kst().date()
    for date in dates:
        target = datetime.strptime(date, "%Y-%m-%d").date()
        offset = (target - today_date).days
        interval = base_interval_for_offset(offset)

        # 첫 요청이 한 순간에 다시 몰리지 않도록 각 구간에 자연스럽게 분산.
        # 이미 방금 43일 전체를 확인했으므로 interval 안에서 나눠 시작한다.
        same_band = [
            d for d in dates
            if base_interval_for_offset(
                (datetime.strptime(d, "%Y-%m-%d").date() - today_date).days
            ) == interval
        ]
        idx = same_band.index(date)
        spread = interval * (idx + 1) / max(1, len(same_band))
        next_due[date] = now_mono + spread

    # 실행 직후 같은 5분 슬롯은 다시 동시점검하지 않고 다음 경계부터.
    fixed_now = now_kst()
    last_fixed_scan_slot = (
        fixed_now.strftime("%Y%m%d%H"),
        fixed_now.minute // FIXED_SAFETY_SCAN_MINUTES,
    )

    log(
        "📡 날짜별 분산 감시 시작 | 오늘 5분 / 내일(+1) 20초 / "
        "+2~+4일 90초 / +5~+14일 30초 / +15~+30일 60초 / "
        "+31~+42일 5분 | 상영준비중·매진 날짜 20초"
    )
    log(
        "🔎 5분 고정 동시점검 유지 | 매시 00/05/10/.../55분 | "
        "+4~+21일 18일 완전 동시"
    )

    while time.time() - started < RUN_SECONDS:
        remaining = RUN_SECONDS - (time.time() - started)
        if remaining <= 0:
            break

        # --------------------------------------------------------
        # 5분 고정 +4~+21일 18일 동시점검
        # --------------------------------------------------------
        fixed_now = now_kst()
        fixed_scan_slot = (
            fixed_now.strftime("%Y%m%d%H"),
            fixed_now.minute // FIXED_SAFETY_SCAN_MINUTES,
        )

        if fixed_scan_slot != last_fixed_scan_slot:
            last_fixed_scan_slot = fixed_scan_slot
            fixed_started = time.time()

            try:
                fixed_events, fixed_mode, fixed_errors = scan_fixed_18_days_concurrent()

                # 성공한 날짜들만 cache 반영. 실패 날짜는 기존 cache 유지.
                successful_dates = {
                    event.get("date")
                    for event in fixed_events.values()
                    if event.get("date")
                }
                for date in successful_dates:
                    date_event_cache[date] = {
                        key: event
                        for key, event in fixed_events.items()
                        if event.get("date") == date
                    }

                log_new_event_diagnostics(fixed_events, state)
                fixed_sent, _ = process(fixed_events, state)
                save_state(state)

                fixed_elapsed = time.time() - fixed_started
                fixed_counts = counts(fixed_events)

                heartbeat_requests += FIXED_CONCURRENT_DAYS
                heartbeat_success += max(0, FIXED_CONCURRENT_DAYS - fixed_errors)
                heartbeat_errors += fixed_errors
                heartbeat_alerts += fixed_sent

                icon = "🔎" if fixed_errors == 0 else "⚠️"
                log(
                    f"{icon} {fixed_now.strftime('%H:%M')} "
                    f"5분 고정 18일 동시점검 완료 | +4~+21일 | "
                    f"GV {fixed_counts['GV']} | 무대인사 {fixed_counts['STAGE']} | "
                    f"상영준비중 {fixed_counts['PREPARING']} | "
                    f"예매가능 {fixed_counts['OPEN']} | 매진 {fixed_counts['SOLD_OUT']} | "
                    f"오류 {fixed_errors} | 알림 {fixed_sent} | "
                    f"{fixed_elapsed:.1f}초"
                )

            except Exception as fixed_error:
                heartbeat_errors += 1
                log(
                    f"⚠️ {fixed_now.strftime('%H:%M')} "
                    f"5분 고정 18일 동시점검 실패 | "
                    f"{type(fixed_error).__name__}: {fixed_error}"
                )

        # --------------------------------------------------------
        # 날짜별 분산 감시: 지금 due인 날짜 1개씩 처리
        # --------------------------------------------------------
        now_mono = time.monotonic()
        due_dates = [
            date for date in dates
            if next_due.get(date, 0) <= now_mono
        ]

        if due_dates:
            due_dates.sort(key=lambda d: next_due.get(d, 0))
            date = due_dates[0]

            events, error = scan_one_date_with_retry(date)
            heartbeat_requests += 1

            if error is not None or events is None:
                heartbeat_errors += 1
                log(
                    f"⚠️ 날짜 조회 실패(기존 상태 유지) | {date} | "
                    f"{type(error).__name__}: {error}"
                )
            else:
                heartbeat_success += 1
                date_event_cache[date] = events

                log_new_event_diagnostics(events, state)
                sent, _ = process(events, state)
                save_state(state)
                heartbeat_alerts += sent

            # 조회가 끝난 현재 상태 기준으로 다음 주기 결정.
            interval = interval_for_date(date, state)
            next_due[date] = time.monotonic() + interval
            continue

        # --------------------------------------------------------
        # 10분 요약
        # --------------------------------------------------------
        heartbeat_elapsed = time.time() - heartbeat_started
        if heartbeat_elapsed >= STATUS_LOG_SECONDS:
            all_cached_events = {}
            for events in date_event_cache.values():
                all_cached_events.update(events)

            c = counts(all_cached_events)
            minutes = max(1, round(heartbeat_elapsed / 60))
            icon = "💚" if heartbeat_errors == 0 else "⚠️"

            log(
                f"{icon} 감시중 | 최근 {minutes}분 날짜조회 "
                f"{heartbeat_requests}회 / 성공 {heartbeat_success}회 | "
                f"43일 캐시 | GV {c['GV']} | 무대인사 {c['STAGE']} | "
                f"상영준비중 {c['PREPARING']} | 예매가능 {c['OPEN']} | "
                f"매진 {c['SOLD_OUT']} | 오류 {heartbeat_errors} | "
                f"알림 {heartbeat_alerts} | "
                f"{now_kst().strftime('%H:%M:%S KST')}"
            )

            heartbeat_started = time.time()
            heartbeat_requests = 0
            heartbeat_success = 0
            heartbeat_errors = 0
            heartbeat_alerts = 0

        time.sleep(IDLE_SLEEP)

    log("")
    log("=" * 60)
    log("RUN COMPLETE")
    log("=" * 60)


if __name__ == "__main__":
    main()
