#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import json
import os
import re
import time
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
# 3) 화면 표시보다 API에 GV/무대인사 신호가 먼저 생기면 선행 감지 Discord 알림 전송
#
# 감시 범위
# ------------------------------------------------------------
# 오늘~49일 뒤: 50일 전체를 10초 목표로 순차 조회
# 실제 50일 조회 시간이 10초를 넘으면 조회 완료 즉시 다음 사이클 시작
# ============================================================


LOTTE_API = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"

SITE_NAME = "롯데시네마 월드타워"
CINEMA_ID = "1|0001|1016"
CINEMA_CODE = "1016"

TOTAL_DAYS = 50
SCAN_INTERVAL = 10.0

RUN_SECONDS = int(os.getenv("RUN_SECONDS", "19200"))

DISCORD_WEBHOOK = os.getenv(
    "DISCORD_LOTTE_WORLDTOWER",
    "",
).strip()

DISCORD_MENTION_ID = "1383846907847381184"

STATE_FILE = Path("seen_lotte_worldtower.json")
BASELINE_FILE = Path("baseline_lotte_worldtower.done")
BASELINE_SCHEMA = "LOTTE_CINEMA_GV_STAGE_V2"

KST = ZoneInfo("Asia/Seoul")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.lottecinema.co.kr/NLCHS/Ticketing",
    "Origin": "https://www.lottecinema.co.kr",
})


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

    response = SESSION.post(
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


def scan_all_50_days():
    dates = make_dates(0, TOTAL_DAYS)
    return scan_dates(dates, "FULL 50-DAY")


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
    # 예전에 정상 사용하던 GitHub Secret 값을 그대로 허용한다.
    # discord.com / discordapp.com / canary.discord.com 등 도메인 형태를
    # 코드에서 임의로 제한하지 않고, 실제 Discord 응답으로 유효성을 확인한다.
    if not DISCORD_WEBHOOK:
        raise RuntimeError(
            "DISCORD_LOTTE_WORLDTOWER Secret이 비어 있습니다."
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


def format_event_alert(show, status):
    """Discord 실제 알림 형식.

    선행 감지 / 상영준비중 / 예매 오픈을 서로 다른 첫 줄로 구분하되,
    회차 본문은 항상 같은 3줄 형식을 사용한다.
    """
    kind = event_name(show)
    url = booking_url(show)

    start_time = norm(show.get("time")) or "시간 확인 필요"
    end_time = norm(show.get("end_time"))
    when = f"{start_time}–{end_time}" if end_time else start_time

    movie = show.get("movie") or "영화명 확인 필요"
    screen = show.get("screen") or "상영관 정보 없음"

    if status == "DETECTED":
        status_line = f"🔎 {kind}가 감지됐습니다"
    elif status == "PREPARING":
        status_line = "⏳ 상영준비중"
    else:
        status_line = "🎟️ 예매가 열렸습니다"

    lines = [
        f"<@{DISCORD_MENTION_ID}>",
        status_line,
        f"[🎬 {SITE_NAME} · {kind}]({url})",
        f"📅 {show.get('date')}",
        f"🎟 {when} · {movie} · {screen}",
    ]

    return "\n".join(lines)


def send_alert(show, status):
    discord_post(format_event_alert(show, status))


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
    # 예전 가짜 테스트 메시지는 사용하지 않는다.
    # 현재 실제 GV 1개로 3종 실제 알림 형식을 그대로 테스트한다.
    send_existing_gv_test()


def send_existing_gv_test():
    """현재 50일 안의 실제 GV 1개로 Discord 3종 알림을 테스트한다.

    Discord에는 '테스트' 문구를 넣지 않는다.
    state/baseline 파일은 읽거나 수정하지 않는다.
    """
    log("")
    log("CURRENT GV DISCORD TEST: 실제 GV 1개를 조회합니다.")

    events, mode, errors = scan_all_50_days()
    log(f"FETCH MODE: {mode}")
    log(f"SCAN ERRORS: {errors}")

    gvs = [
        show
        for show in events.values()
        if show.get("event_type") == "GV"
    ]

    if not gvs:
        log("CURRENT GV DISCORD TEST: 현재 감지된 GV가 없습니다.")
        return

    gvs.sort(
        key=lambda show: (
            0 if show.get("booking_state") == "OPEN" else 1,
            show.get("date", ""),
            show.get("time", ""),
            show.get("movie", ""),
        )
    )

    show = gvs[0]

    # Discord에는 아래 3개가 각각 실제 운영 형식 그대로 전송된다.
    test_statuses = (
        "DETECTED",
        "PREPARING",
        "OPEN",
    )

    for status in test_statuses:
        send_alert(show, status)
        time.sleep(0.7)

    log(
        "CURRENT GV DISCORD TEST SENT: "
        "선행 감지 + 상영준비중 + 예매 오픈 / "
        f"{show.get('date')} / {show.get('movie')} / "
        f"{show.get('time')} / {show.get('screen')}"
    )
    log("STATE/BASELINE: 변경하지 않았습니다.")


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
# API 선행 감지 (Discord 전송)
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
    """API에 GV/무대인사 신호가 처음 생긴 순간을 Discord에 알린다.

    - 기존 state에 없는 회차만 최초 1회 대상
    - Discord 전송에 성공하면 임시 DETECTED 상태를 state에 남긴다.
      뒤의 예매오픈/상영준비중 전송이 실패해도 선행 감지가 반복되지 않는다.
    - 실제 booking 상태는 같은 사이클의 process()가 곧바로 덮어쓴다.
    """
    detected = 0
    discord_sent = 0

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
        log("🔎 API 선행 감지")
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

        try:
            send_alert(show, "DETECTED")
        except Exception as error:
            log(
                f"DISCORD ERROR (선행 감지): {kind} / "
                f"{show.get('movie')} / {show.get('date')} "
                f"{show.get('time')} / {error}"
            )
            continue

        # 임시 상태. process()가 현재 실제 상태로 교체한다.
        state[key] = record(show, "DETECTED")
        save_state(state)
        discord_sent += 1

    return detected, discord_sent


# ============================================================
# Baseline
# ============================================================

def baseline_schema_ok():
    if not BASELINE_FILE.exists():
        return False
    try:
        return BASELINE_FILE.read_text(encoding="utf-8").strip() == BASELINE_SCHEMA
    except Exception:
        return False


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
        BASELINE_SCHEMA,
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

    # 기준값에서는 회차별 원본 필드를 전부 출력하지 않는다.
    # 50일 스캔 요약과 개수만 남겨 Actions 로그가 잘리지 않게 한다.


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
        # 상태만 저장한다.
        #
        # 중요:
        # 이 전체 알리미에서는 매진 후 다시 OPEN으로 바뀌어도
        # 취소표 알림을 보내지 않는다.
        # 취소표 감시는 별도 특정회차 전용 알리미가 담당한다.
        # ----------------------------------------------------

        if current in {
            "SOLD_OUT",
            "CLOSED",
        }:
            state[key] = record(
                show,
                current,
            )
            save_state(state)
            continue

        # ----------------------------------------------------
        # 상영준비중 / 예매 가능
        # ----------------------------------------------------

        alert_status = None

        if current == "PREPARING":
            if previous in (
                None,
                "DETECTED",
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
                "DETECTED",
                "UNKNOWN",
                "PREPARING",
                "CLOSED",
            ):
                alert_status = "OPEN"

            # 매진 -> 예매가능은 취소표일 수 있으므로
            # 전체 알리미에서는 Discord 알림을 보내지 않고 상태만 갱신한다.
            elif previous == "SOLD_OUT":
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

            stored_status = (
                "OPEN"
                if alert_status == "OPEN"
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
            elif previous == "PREPARING":
                transition = "상영준비중 -> 예매가 열렸습니다"
                icon = "🚨"
            else:
                transition = "예매가 열렸습니다"
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
    log("선행 감지: API에서 GV/무대인사 신호가 처음 보이면 Discord에도 전송")
    log("취소표 감지: 사용 안 함 (특정 회차 전용 알리미로 분리)")
    log("감시 범위: 오늘 ~ +49일 (50일 전체)")
    log(f"목표 감시 주기: {SCAN_INTERVAL:.0f}초 (50일 전체 순차 조회)")
    log(f"RUN SECONDS: {RUN_SECONDS}")
    log("=" * 60)

    # 현재 실제 GV 1개를 Discord로 보내는 수동 테스트.
    # baseline/state는 읽거나 수정하지 않는다.
    if os.getenv(
        "LOTTE_EXISTING_GV_TEST",
        "",
    ).strip().lower() in {"1", "true", "yes", "on"}:
        send_existing_gv_test()
        return

    # 기존 강제 Discord 테스트. baseline/state는 건드리지 않음.
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

    # 최초 실행 또는 감지키/분류방식이 바뀐 버전은 50일 전체를
    # 다시 기준값으로 등록한다. 이 재기준화 실행에서는 Discord 알림을 보내지 않는다.
    # 예전 state의 키와 새 코드의 키가 달라 기존 회차가 '신규'로 오인되는 것을 방지한다.
    if not baseline_schema_ok():
        log("")
        log("기준값 버전이 없거나 이전 버전입니다. 현재 50일 회차를 알림 없이 다시 기준값으로 등록합니다.")
        events, mode, errors = scan_all_50_days()
        log(f"FETCH MODE: {mode}")
        log(f"SCAN ERRORS: {errors}")
        make_baseline(events)
        return

    started = time.time()
    cycle = 0

    while time.time() - started < RUN_SECONDS:
        cycle += 1
        cycle_started = time.time()

        log("")
        log("=" * 60)
        log(
            f"CYCLE #{cycle} "
            f"{now_kst().strftime('%Y-%m-%d %H:%M:%S KST')}"
        )
        log("50일 전체 스캔 시작")
        log("=" * 60)

        try:
            cycle_events, mode, errors = scan_all_50_days()
            log(f"FETCH MODE: {mode}")
            log(f"SCAN ERRORS: {errors}")

            print_counts(cycle_events)

            # process 전에 실행해야 state에 아직 없는 '최초 API 신호'를 잡을 수 있다.
            early_detected, early_discord_sent = log_new_event_diagnostics(
                cycle_events,
                state,
            )

            sent, unknown_logged = process(
                cycle_events,
                state,
            )

            save_state(state)

            log(
                f"이번 사이클 API 선행 진단: "
                f"{early_detected}건"
            )
            log(
                f"이번 사이클 Discord 알림: "
                f"{early_discord_sent + sent}건 "
                f"(선행 감지 {early_discord_sent} + 상태 알림 {sent})"
            )
            log(
                f"이번 사이클 미확인 상태 진단 로그: "
                f"{unknown_logged}건"
            )
            log(
                f"저장된 상태: "
                f"{len(state)}건"
            )

        except Exception as error:
            log(
                f"SCAN/PROCESS ERROR: "
                f"{type(error).__name__}: {error}"
            )

        elapsed = time.time() - cycle_started
        remaining = RUN_SECONDS - (time.time() - started)

        if remaining <= 0:
            break

        wait = min(
            max(
                0.0,
                SCAN_INTERVAL - elapsed,
            ),
            remaining,
        )

        log(
            f"사이클 소요시간: {elapsed:.2f}초"
        )

        if wait > 0:
            log(
                f"다음 50일 스캔까지 {wait:.2f}초 대기"
            )
            time.sleep(wait)
        else:
            log(
                "50일 조회 시간이 목표 10초 이상이므로 "
                "대기 없이 다음 사이클을 시작합니다."
            )

    log("")
    log("=" * 60)
    log("RUN COMPLETE")
    log("=" * 60)


if __name__ == "__main__":
    main()
