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
# LOTTE CINEMA WORLDTOWER GV / STAGE MONITOR
#
# 핵심 규칙
# - AccompanyTypeCode 30 = 무대인사
# - AccompanyTypeCode 40 = GV
# - IsBookingYN=Y는 "선행 후보"일 뿐 실제 오픈 확정 아님
# - 실제 오픈은 GetBookPossible 우선, GetSeats 보조 검증
#
# 로그 규칙
# - 날짜별 정상 API 로그 숨김
# - 회차별 EARLY/OPEN 보정 로그 숨김
# - 50일 스캔 진행은 10/50 단위만 표시
# - 실제 오픈 검증 진행은 25개 단위만 표시
# - 새 이벤트 / 상태 변화 / 오류는 즉시 표시
# - 정상 감시 요약은 10분마다 1줄
# ============================================================

LOTTE_API = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"

SITE_NAME = "롯데시네마 월드타워"
CINEMA_ID = "1|0001|1016"
CINEMA_CODE = "1016"

TOTAL_DAYS = 50
SCAN_INTERVAL = float(os.getenv("LOTTE_SCAN_INTERVAL", "10"))
STATUS_LOG_SECONDS = int(os.getenv("STATUS_LOG_SECONDS", "600"))
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "19200"))

# 정상 API 세부 로그를 정말 보고 싶을 때만 1
VERBOSE_API_LOGS = os.getenv("VERBOSE_API_LOGS", "0").strip() == "1"

# 취소표 재알림용 최소 매진 유지시간
CANCEL_REARM_SECONDS = 120

DISCORD_WEBHOOK = os.getenv(
    "DISCORD_LOTTE_WORLDTOWER",
    "",
).strip()

DISCORD_MENTION_ID = os.getenv(
    "DISCORD_USER_ID",
    "1383846907847381184",
).strip()

STATE_FILE = Path("seen_lotte_worldtower.json")
BASELINE_FILE = Path("baseline_lotte_worldtower.done")
BASELINE_SCHEMA = "LOTTE_WORLDTOWER_REALOPEN_COMPACT_V1"

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
# Basic
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


def lotte_post(payload, timeout=15):
    files = {
        "paramList": (
            None,
            json.dumps(payload, ensure_ascii=False),
        )
    }

    response = SESSION.post(
        LOTTE_API,
        files=files,
        timeout=timeout,
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
    return " | ".join(scalar_texts(obj))


# ============================================================
# Event classification
# ============================================================

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

EVENT_CONTEXT_FIELDS = tuple(dict.fromkeys(EVENT_FIELDS + (
    "AccompanyTypeCode",
    "AccompanyTypeNameKR",
    "AccompanyTypeNameUS",
    "AccompanyTypeName",
)))


def normalize_event_code(value):
    text = norm(value)
    if not text:
        return ""

    match = re.fullmatch(r"0*(\d+)(?:\.0+)?", text)
    if match:
        try:
            return str(int(match.group(1)))
        except Exception:
            pass

    return text


def classify_event(row):
    code = normalize_event_code(row.get("AccompanyTypeCode"))

    # 70은 4K 코드로 확인됨
    if code == "70":
        return None
    if code == "30":
        return "STAGE"
    if code == "40":
        return "GV"

    focused = " | ".join(
        norm(row.get(key))
        for key in EVENT_FIELDS
        if norm(row.get(key))
    )
    fc = compact(focused)

    if "무대인사" in fc or "舞台挨拶" in focused:
        return "STAGE"

    if any(
        compact(row.get(key)) == "무대"
        for key in SHORT_STAGE_FIELDS
    ):
        return "STAGE"

    if "관객과의대화" in fc or bounded_gv(focused):
        return "GV"

    return None


# ============================================================
# Booking state
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
    status_text = " | ".join(
        norm(row.get(key))
        for key in BOOKING_TEXT_FIELDS
        if norm(row.get(key))
    )

    full_text = status_text + " | " + all_scalar_text(row)
    fc = compact(full_text)

    if "상영준비중" in fc or "예매준비중" in fc:
        return "PREPARING", "explicit_preparing_text", ""

    if "예매가능" in fc or "예매하기" in fc:
        return "EARLY", "explicit_open_text_unverified", ""

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

    code = str(value or "").strip().upper()

    if code in {"Y", "YES", "TRUE", "1"}:
        return "EARLY", "IsBookingYN=Y_unverified", code

    if code == "E":
        return "SOLD_OUT", "IsBookingYN=E", code

    if code in {"N", "NO", "FALSE", "0"}:
        return "CLOSED", "IsBookingYN=N", code

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


def extract_sequences(data, date, fallback_movie="", fallback_code=""):
    rows = []

    def walk(value, inherited_event=None):
        inherited_event = dict(inherited_event or {})

        if isinstance(value, dict):
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
                raw = dict(event_context)
                raw.update(value)

                state, source, raw_status = booking_state(raw)

                rows.append({
                    "date": date,
                    "movie": norm(
                        raw.get("MovieNameKR")
                        or raw.get("MovieName")
                        or fallback_movie
                    ),
                    "movie_code": norm(
                        raw.get("RepresentationMovieCode")
                        or raw.get("MovieCode")
                        or fallback_code
                    ),
                    "time": start,
                    "end_time": norm(raw.get("EndTime")),
                    "screen": screen,
                    "screen_id": norm(
                        raw.get("ScreenID")
                        or raw.get("ScreenId")
                        or raw.get("ScreenCode")
                    ),
                    "cinema_id": norm(
                        raw.get("CinemaID")
                        or CINEMA_CODE
                    ),
                    "screen_division_code": norm(
                        raw.get("ScreenDivisionCode")
                        or raw.get("ScreenDiv")
                    ),
                    "play_date": norm(
                        raw.get("PlayDt")
                        or raw.get("PlayDate")
                        or date
                    ),
                    "play_sequence": norm(
                        raw.get("PlaySequence")
                        or raw.get("PlaySeq")
                        or raw.get("Sequence")
                    ),
                    "remain": norm(
                        raw.get("BookingSeatCount")
                        or raw.get("RemainSeatCount")
                        or raw.get("RemainingSeatCount")
                    ),
                    "total": norm(
                        raw.get("TotalSeatCount")
                        or raw.get("SeatCount")
                    ),
                    "event_type": classify_event(raw),
                    "booking_state": state,
                    "booking_state_source": source,
                    "raw_booking_code": raw_status,
                    "raw": raw,
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
    score = 0

    if show.get("event_type") in {"GV", "STAGE"}:
        score += 1000

    if normalize_event_code(raw.get("AccompanyTypeCode")) in {"30", "40"}:
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
# Lotte scan
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
    if not rows:
        return False

    try:
        target = datetime.strptime(date, "%Y-%m-%d").date()
        offset = (target - now_kst().date()).days
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
    rows = list(primary_rows)

    # 먼 날짜는 전체조회에 행사정보가 빠지는 표본이 있어 영화별 보완.
    if _future_enrich_needed(date, primary_rows):
        movie_map = {}

        for row in primary_rows:
            code = norm(row.get("movie_code"))
            name = norm(row.get("movie"))
            if code:
                movie_map.setdefault(code, name)

        for movie_code, movie_name in movie_map.items():
            try:
                rows.extend(
                    fetch_movie_specific(
                        date,
                        movie_code,
                        movie_name,
                    )
                )
            except Exception as error:
                # 정상 성공 로그는 숨기되 오류는 즉시 표시.
                log(
                    f"⚠️ {date} 영화별 보완 조회 오류: "
                    f"{type(error).__name__}: {error}"
                )

        rows = dedupe(rows)

    if VERBOSE_API_LOGS:
        event_count = sum(
            row.get("event_type") in {"GV", "STAGE"}
            for row in rows
        )
        log(
            f"API {date} | {response.status_code} | "
            f"{time.time() - started:.2f}s | "
            f"ROWS={len(rows)} | EVENTS={event_count}"
        )

    return rows


def make_dates():
    today = now_kst().date()
    return [
        (today + timedelta(days=index)).strftime("%Y-%m-%d")
        for index in range(TOTAL_DAYS)
    ]


def scan_all_50_days(show_progress=True):
    dates = make_dates()
    all_rows = []
    errors = 0

    for index, date in enumerate(dates, start=1):
        try:
            all_rows.extend(fetch_date_primary(date))
        except Exception as error:
            errors += 1
            log(
                f"⚠️ {date} 조회 오류: "
                f"{type(error).__name__}: {error}"
            )

        if show_progress and (
            index % 10 == 0 or index == len(dates)
        ):
            log(f"⏳ 50일 조회: {index}/{len(dates)}")

    events = {}

    for show in dedupe(all_rows):
        if show.get("event_type") in {"GV", "STAGE"}:
            events[show_key(show)] = show

    return events, errors


# ============================================================
# Real open verification
# ============================================================

def _api_is_ok(data):
    if not isinstance(data, dict):
        return None

    value = data.get("IsOK")

    if isinstance(value, bool):
        return value

    if value is None:
        return None

    return str(value).strip().upper() in {
        "Y", "YES", "TRUE", "1"
    }


def _has_seat_stage_payload(data):
    if not isinstance(data, dict):
        return False

    for key in (
        "ScreenSeatInfo",
        "Seats",
        "BookingSeats",
        "Fees",
        "PlaySeqsDetails",
    ):
        if key in data and data.get(key) is not None:
            return True

    for value in data.values():
        if isinstance(value, dict) and _has_seat_stage_payload(value):
            return True

    return False


def _numeric_cinema_id(value):
    text = norm(value)

    if text.isdigit():
        return int(text)

    # 1|0001|1016 같은 형식이면 마지막 숫자 사용.
    match = re.search(r"(\d+)$", text)
    if match:
        return int(match.group(1))

    return int(CINEMA_CODE)


def verify_real_open(show):
    screen_id = norm(show.get("screen_id"))
    play_sequence = norm(show.get("play_sequence"))
    play_date = norm(show.get("play_date") or show.get("date"))
    screen_division_code = norm(show.get("screen_division_code"))
    cinema_id = norm(show.get("cinema_id") or CINEMA_CODE)

    if not screen_id or not play_sequence or not play_date:
        return False, "open_verify_missing_keys", "필수 키 부족"

    # 1) GetBookPossible 우선
    try:
        data, _ = lotte_post({
            "MethodName": "GetBookPossible",
            "channelType": "HO",
            "osType": "W",
            "osVersion": UA,
            "multiLanguageID": "KR",
            "screenID": screen_id,
            "playDate": play_date,
            "playSequence": play_sequence,
        })

        ok = _api_is_ok(data)
        message = norm(
            data.get("ResultMessage")
            if isinstance(data, dict)
            else ""
        )

        if ok is True:
            return True, "GetBookPossible=OK", message or "SUCCESS"

        if ok is False:
            return False, "GetBookPossible=NOT_OPEN", message or "IsOK=false"

    except Exception as error:
        book_possible_error = f"{type(error).__name__}: {error}"
    else:
        book_possible_error = "IsOK missing"

    # 2) GetSeats 보조
    if not screen_division_code:
        return (
            False,
            "GetSeats=SKIP",
            f"ScreenDivisionCode 없음 / {book_possible_error}",
        )

    try:
        data, _ = lotte_post({
            "MethodName": "GetSeats",
            "channelType": "HO",
            "osType": "W",
            "osVersion": UA,
            "cinemaId": _numeric_cinema_id(cinema_id),
            "screenId": int(str(screen_id).strip()),
            "playDate": play_date,
            "playSequence": int(str(play_sequence).strip()),
            "screenDivisionCode": int(str(screen_division_code).strip()),
        })

        ok = _api_is_ok(data)
        message = norm(
            data.get("ResultMessage")
            if isinstance(data, dict)
            else ""
        )

        if ok is True and _has_seat_stage_payload(data):
            return True, "GetSeats=OK", message or "SUCCESS"

        return (
            False,
            "GetSeats=NOT_OPEN",
            message or "좌석단계 미확인",
        )

    except Exception as error:
        return (
            False,
            "GetSeats=ERROR",
            f"{type(error).__name__}: {error}",
        )


def finalize_booking_states(events, show_progress=True):
    candidates = [
        (key, show)
        for key, show in events.items()
        if show.get("booking_state") == "EARLY"
    ]

    total = len(candidates)

    for index, (_, show) in enumerate(candidates, start=1):
        verified, source, detail = verify_real_open(show)

        show["open_verify_source"] = source
        show["open_verify_detail"] = detail

        if verified:
            show["booking_state"] = "OPEN"
            show["booking_state_source"] = source
        else:
            show["booking_state"] = "EARLY"
            show["booking_state_source"] = source

        # 회차별 상태 보정 로그는 절대 출력하지 않는다.
        if show_progress and total > 0 and (
            index % 25 == 0 or index == total
        ):
            log(f"⏳ 실제 오픈 확인: {index}/{total}")

    return events


# ============================================================
# State
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
        return data if isinstance(data, dict) else {}
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
    result = {
        "status": status,
        "event_type": show.get("event_type"),
        "date": show.get("date"),
        "movie": show.get("movie"),
        "movie_code": show.get("movie_code"),
        "time": show.get("time"),
        "screen": show.get("screen"),
        "screen_id": show.get("screen_id"),
        "play_sequence": show.get("play_sequence"),
        "booking_state_source": show.get("booking_state_source"),
        "raw_booking_code": show.get("raw_booking_code"),
        "open_verify_source": show.get("open_verify_source"),
        "open_verify_detail": show.get("open_verify_detail"),
        "updated_at_kst": now_kst().isoformat(timespec="seconds"),
    }

    return result


def baseline_ok():
    if not BASELINE_FILE.exists():
        return False

    try:
        value = BASELINE_FILE.read_text(encoding="utf-8").strip()
        # 기존 baseline 파일도 그대로 인정해서 불필요한 재기준화를 막는다.
        return bool(value)
    except Exception:
        return True


def make_baseline(events):
    state = {}

    for key, show in events.items():
        state[key] = record(
            show,
            show.get("booking_state", "UNKNOWN"),
        )

    save_state(state)
    BASELINE_FILE.write_text(
        BASELINE_SCHEMA,
        encoding="utf-8",
    )

    c = count_events(events)
    log(
        "✅ 기준값 등록 완료 | "
        f"GV {c['GV']} | 무대인사 {c['STAGE']} | "
        f"상영준비중 {c['PREPARING']} | "
        f"실제 예매가능 {c['OPEN']}"
    )


# ============================================================
# Discord
# ============================================================

def booking_url(show):
    params = {
        "link_channelCode": "naver",
        "link_cinemaCode": CINEMA_CODE,
        "link_date": show.get("date", ""),
        "link_movieCd": show.get("movie_code", ""),
        "link_screenId": show.get("screen_id", ""),
        "link_time": show.get("time", ""),
    }

    if (
        params["link_movieCd"]
        and params["link_screenId"]
        and params["link_time"]
    ):
        return (
            "https://www.lottecinema.co.kr/NLCMW/ticketing?"
            + urlencode(params)
        )

    return "https://www.lottecinema.co.kr/NLCMW/ticketing"


def event_name(show):
    return (
        "무대인사"
        if show.get("event_type") == "STAGE"
        else "GV"
    )


def show_when(show):
    start = show.get("time", "")
    end = show.get("end_time", "")
    return f"{start}–{end}" if end else start


def discord_post(content):
    if not DISCORD_WEBHOOK:
        raise RuntimeError(
            "DISCORD_LOTTE_WORLDTOWER Secret이 비어 있습니다."
        )

    payload = {
        "content": content,
        "flags": 4,
        "allowed_mentions": {
            "parse": [],
            "users": [DISCORD_MENTION_ID] if DISCORD_MENTION_ID else [],
        },
    }

    response = requests.post(
        DISCORD_WEBHOOK,
        json=payload,
        timeout=15,
    )
    response.raise_for_status()


def send_alert(show, status):
    kind = event_name(show)
    url = booking_url(show)

    lines = []

    if DISCORD_MENTION_ID:
        lines.append(f"<@{DISCORD_MENTION_ID}>")

    lines.extend([
        f"**🎬 {SITE_NAME} · {kind}**",
        f"📅 {show.get('date')}",
        (
            f"🎟 {show_when(show)} · "
            f"{show.get('movie') or '영화명 확인 필요'} · "
            f"{show.get('screen') or '상영관 정보 없음'}"
        ),
    ])

    if status == "PREPARING":
        lines.append("⏳ 상영준비중")
    elif status == "CANCEL_TICKET":
        lines.append("🎟️ 취소표가 생겼습니다")
    elif status == "OPEN":
        lines.append("🚨 예매가 열렸습니다")

    lines.append(f"🔗 {url}")

    discord_post("\n".join(lines))


# ============================================================
# Process
# ============================================================

def count_events(events):
    values = list(events.values())

    return {
        "GV": sum(x.get("event_type") == "GV" for x in values),
        "STAGE": sum(x.get("event_type") == "STAGE" for x in values),
        "PREPARING": sum(x.get("booking_state") == "PREPARING" for x in values),
        "OPEN": sum(x.get("booking_state") == "OPEN" for x in values),
        "EARLY": sum(x.get("booking_state") == "EARLY" for x in values),
        "SOLD_OUT": sum(x.get("booking_state") == "SOLD_OUT" for x in values),
        "UNKNOWN": sum(x.get("booking_state") == "UNKNOWN" for x in values),
    }


def log_new_event_signals(events, state):
    for key, show in events.items():
        if key in state:
            continue

        log(
            f"🔎 {event_name(show)}가 감지됐습니다 | "
            f"{show.get('date')} {show.get('time')} | "
            f"{show.get('movie') or '영화명 확인 필요'}"
        )


def process(events, state):
    sent = 0

    for key, show in events.items():
        current = show.get("booking_state", "UNKNOWN")
        previous_record = state.get(key) or {}
        previous = previous_record.get("status")

        # 선행 후보는 내부에만 저장. 회차별 보정 로그 절대 없음.
        if current == "EARLY":
            state[key] = record(show, "EARLY")
            continue

        if current == "UNKNOWN":
            previous_raw = previous_record.get("raw_booking_code")
            current_raw = show.get("raw_booking_code")

            if (
                previous is None
                or previous != "UNKNOWN"
                or previous_raw != current_raw
            ):
                log(
                    f"⚠️ {event_name(show)} 미확인 예매 상태 | "
                    f"{show.get('date')} {show.get('time')} | "
                    f"{show.get('movie') or '영화명 확인 필요'}"
                )

            state[key] = record(show, previous or "UNKNOWN")
            continue

        if current in {"SOLD_OUT", "CLOSED"}:
            new_record = record(show, current)

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
            continue

        alert_status = None

        if current == "PREPARING":
            if previous in {
                None,
                "EARLY",
                "UNKNOWN",
                "CLOSED",
                "SOLD_OUT",
            }:
                alert_status = "PREPARING"
            elif previous == "OPEN":
                # 이미 실제 오픈이 확인된 회차의 일시적 준비중 흔들림은 무시.
                state[key] = record(show, "OPEN")
                continue

        elif current == "OPEN":
            if previous in {
                None,
                "EARLY",
                "UNKNOWN",
                "PREPARING",
                "CLOSED",
            }:
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

                if (
                    sold_out_seconds is None
                    or sold_out_seconds >= CANCEL_REARM_SECONDS
                ):
                    alert_status = "CANCEL_TICKET"
                else:
                    state[key] = record(show, "OPEN")
                    continue

        if alert_status:
            try:
                send_alert(show, alert_status)
            except Exception as error:
                log(
                    f"⚠️ Discord 전송 오류 | "
                    f"{event_name(show)} | "
                    f"{show.get('date')} {show.get('time')} | "
                    f"{type(error).__name__}: {error}"
                )
                continue

            stored_status = (
                "OPEN"
                if alert_status in {"OPEN", "CANCEL_TICKET"}
                else "PREPARING"
            )

            state[key] = record(show, stored_status)
            sent += 1

            if alert_status == "PREPARING":
                text = "상영준비중"
            elif alert_status == "CANCEL_TICKET":
                text = "취소표가 생겼습니다"
            else:
                text = "예매가 열렸습니다"

            log(
                f"✅ {event_name(show)} {text} | "
                f"{show.get('date')} {show.get('time')} | "
                f"{show.get('movie') or '영화명 확인 필요'}"
            )
            continue

        # 동일 상태
        state[key] = record(show, previous or current)

    save_state(state)
    return sent


# ============================================================
# Main
# ============================================================

def main():
    log("=" * 60)
    log("LOTTE CINEMA GV/STAGE MONITOR - WORLDTOWER 1016")
    log("=" * 60)
    log("대상: GV / 무대인사")
    log("실제 오픈: GetBookPossible 우선 + GetSeats 보조 확인")
    log("감시 범위: 오늘 ~ +49일 / 50일")
    log(f"목표 주기: {SCAN_INTERVAL:g}초")
    log(f"RUN SECONDS: {RUN_SECONDS}")
    log("로그: 회차별 보정 출력 안 함 / 정상 요약 10분 간격")
    log("=" * 60)

    state = load_state()

    # 최초 기준값
    if not baseline_ok():
        log("⏳ 최초 50일 기준값 등록 시작")
        events, errors = scan_all_50_days(show_progress=True)
        finalize_booking_states(events, show_progress=True)

        if errors:
            log(f"⚠️ 기준값 조회 오류: {errors}건")

        make_baseline(events)
        return

    started = time.time()
    cycle = 0
    last_status_log = 0.0

    while time.time() - started < RUN_SECONDS:
        cycle += 1
        cycle_started = time.time()

        try:
            # 첫 사이클은 진행상황을 보여주고,
            # 이후에도 긴 스캔에서 멈춘 것처럼 보이지 않게 10일 단위만 표시.
            log(
                f"⏳ CYCLE #{cycle} 50일 스캔 시작 | "
                f"{now_kst().strftime('%H:%M:%S')}"
            )

            events, errors = scan_all_50_days(show_progress=True)

            # GV/무대인사 후보만 실제 오픈 검증.
            finalize_booking_states(events, show_progress=True)

            # 새 행사 신호는 1회만 간단히 표시.
            log_new_event_signals(events, state)

            sent = process(events, state)

            counts = count_events(events)
            now_mono = time.time()

            if (
                last_status_log == 0.0
                or now_mono - last_status_log >= STATUS_LOG_SECONDS
                or sent > 0
                or errors > 0
            ):
                log(
                    "✅ 정상 감시중 | "
                    f"GV {counts['GV']} | "
                    f"무대인사 {counts['STAGE']} | "
                    f"상영준비중 {counts['PREPARING']} | "
                    f"실제 예매가능 {counts['OPEN']} | "
                    f"오류 {errors} | 알림 {sent}"
                )
                last_status_log = now_mono

        except Exception as error:
            log(
                f"⚠️ SCAN ERROR: "
                f"{type(error).__name__}: {error}"
            )

        elapsed = time.time() - cycle_started
        remaining = RUN_SECONDS - (time.time() - started)

        if remaining <= 0:
            break

        wait = min(
            max(0.0, SCAN_INTERVAL - elapsed),
            remaining,
        )

        if wait > 0:
            time.sleep(wait)

    log("RUN COMPLETE")


if __name__ == "__main__":
    main()
