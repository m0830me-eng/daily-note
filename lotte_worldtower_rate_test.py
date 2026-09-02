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
# 핵심 변경
# ------------------------------------------------------------
# 1) AccompanyTypeCode
#    30 = 무대인사
#    40 = GV
#
# 2) IsBookingYN
#    Y = "실제 예매 오픈 확정"으로 사용하지 않음
#        -> 선행 신호 EARLY 로만 취급
#    E = 매진
#    N = 예매 불가/마감
#
# 3) 실제 예매 오픈
#    GetBookPossible / GetSeats 추가 검증 성공 시 OPEN
#
# 4) 상영준비중
#    기존처럼 별도 PREPARING 상태 유지
#
# 예상 흐름
# ------------------------------------------------------------
# GV API 최초 등장
# -> 🔎 선행 감지
#
# 상영준비중 문구 등장 시
# -> ⏳ 상영준비중
#
# 실제 예매 가능 검증 성공
# -> 🚨 예매 오픈
# ============================================================


LOTTE_API = (
    "https://www.lottecinema.co.kr/"
    "LCWS/Ticketing/TicketingData.aspx"
)

SITE_NAME = "롯데시네마 월드타워"

CINEMA_ID = "1|0001|1016"
CINEMA_CODE = "1016"

TOTAL_DAYS = 50
SCAN_INTERVAL = 10.0

RUN_SECONDS = int(
    os.getenv("RUN_SECONDS", "19200")
)

DISCORD_WEBHOOK = os.getenv(
    "DISCORD_LOTTE_WORLDTOWER",
    "",
).strip()

DISCORD_MENTION_ID = os.getenv(
    "DISCORD_MENTION_ID",
    "1383846907847381184",
).strip()

STATE_FILE = Path(
    "seen_lotte_worldtower.json"
)

BASELINE_FILE = Path(
    "baseline_lotte_worldtower.done"
)

BASELINE_SCHEMA = (
    "LOTTE_CINEMA_GV_STAGE_REAL_OPEN_V1"
)

KST = ZoneInfo("Asia/Seoul")

UA = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/151.0.0.0 "
    "Safari/537.36"
)

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": (
        "https://www.lottecinema.co.kr/"
        "NLCHS/Ticketing"
    ),
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
    return " ".join(
        str(value or "").split()
    )


def compact(value):
    return re.sub(
        r"\s+",
        "",
        norm(value),
    )


def normalize_event_code(value):
    text = norm(value)

    if not text:
        return ""

    try:
        return str(int(float(text)))
    except Exception:
        return text


def all_scalar_text(value):
    out = []

    def walk(x):
        if isinstance(x, dict):
            for child in x.values():
                walk(child)

        elif isinstance(x, list):
            for child in x:
                walk(child)

        elif x is not None:
            out.append(str(x))

    walk(value)

    return " | ".join(out)


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

    data = response.json()

    if (
        isinstance(data, dict)
        and data.get("IsOK") is False
    ):
        raise RuntimeError(
            "롯데 API 실패: "
            f"{data.get('ResultMessage') or data}"
        )

    return data


# ============================================================
# Event classification
# ============================================================

EVENT_CONTEXT_FIELDS = (
    "AccompanyTypeCode",
    "AccompanyTypeNameKR",
    "AccompanyTypeName",
    "EventNameKR",
    "EventName",
    "EventTypeNameKR",
    "SpecialNameKR",
    "SpecialTypeNameKR",
    "PlayKindNameKR",
)


def _event_context_from_dict(value):
    result = {}

    if not isinstance(value, dict):
        return result

    for key in EVENT_CONTEXT_FIELDS:
        if (
            key in value
            and norm(value.get(key))
        ):
            result[key] = value.get(key)

    return result


def classify_event(row):
    code = normalize_event_code(
        row.get("AccompanyTypeCode")
    )

    if code == "30":
        return "STAGE"

    if code == "40":
        return "GV"

    text = compact(
        " ".join(
            norm(row.get(key))
            for key in EVENT_CONTEXT_FIELDS
        )
    ).upper()

    if (
        "무대인사" in text
        or "STAGEGREETING" in text
    ):
        return "STAGE"

    if (
        "GV" in text
        or "관객과의대화" in text
        or "시사회" in text
    ):
        return "GV"

    return ""


def event_name(show):
    if (
        show.get("event_type")
        == "STAGE"
    ):
        return "무대인사"

    return "GV"


# ============================================================
# Booking state
# ============================================================

BOOKING_TEXT_FIELDS = (
    "BookingStatusNameKR",
    "BookingStatusName",
    "SaleStatusNameKR",
    "SaleStatusName",
    "StatusNameKR",
    "StatusName",
    "ButtonNameKR",
    "ButtonName",
)


def booking_state(row):
    status_text = " | ".join(
        norm(row.get(key))
        for key in BOOKING_TEXT_FIELDS
        if norm(row.get(key))
    )

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

    value = row.get(
        "IsBookingYN"
    )

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

    # 중요:
    # Y는 실제 오픈 확정이 아니다.
    if code in {
        "Y",
        "YES",
        "TRUE",
        "1",
    }:
        return (
            "EARLY",
            "IsBookingYN=Y_pending_real_open",
            code,
        )

    if code == "E":
        return (
            "SOLD_OUT",
            "IsBookingYN=E",
            code,
        )

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

def extract_sequences(
    data,
    date,
    fallback_movie="",
    fallback_code="",
):
    rows = []

    def walk(
        value,
        inherited_event=None,
    ):
        inherited_event = dict(
            inherited_event or {}
        )

        if isinstance(value, dict):
            event_context = dict(
                inherited_event
            )

            event_context.update(
                _event_context_from_dict(
                    value
                )
            )

            start = norm(
                value.get("StartTime")
                or value.get(
                    "PlayStartTime"
                )
                or value.get("StartTm")
            )

            screen = norm(
                value.get("ScreenNameKR")
                or value.get("ScreenName")
                or value.get("ScreenID")
            )

            if start and screen:
                merged_raw = dict(
                    event_context
                )

                merged_raw.update(
                    value
                )

                state, source, raw_status = (
                    booking_state(
                        merged_raw
                    )
                )

                rows.append({
                    "date": date,

                    "movie": norm(
                        merged_raw.get(
                            "MovieNameKR"
                        )
                        or merged_raw.get(
                            "MovieName"
                        )
                        or fallback_movie
                    ),

                    "movie_code": norm(
                        merged_raw.get(
                            "RepresentationMovieCode"
                        )
                        or merged_raw.get(
                            "MovieCode"
                        )
                        or fallback_code
                    ),

                    "time": start,

                    "end_time": norm(
                        merged_raw.get(
                            "EndTime"
                        )
                    ),

                    "screen": screen,

                    "screen_id": norm(
                        merged_raw.get(
                            "ScreenID"
                        )
                        or merged_raw.get(
                            "ScreenId"
                        )
                        or merged_raw.get(
                            "ScreenCode"
                        )
                    ),

                    "screen_division_code": norm(
                        merged_raw.get(
                            "ScreenDivisionCode"
                        )
                    ),

                    "play_sequence": norm(
                        merged_raw.get(
                            "PlaySequence"
                        )
                        or merged_raw.get(
                            "PlaySeq"
                        )
                        or merged_raw.get(
                            "Sequence"
                        )
                    ),

                    "play_date": norm(
                        merged_raw.get(
                            "PlayDt"
                        )
                        or date
                    ),

                    "remain": norm(
                        merged_raw.get(
                            "BookingSeatCount"
                        )
                        or merged_raw.get(
                            "RemainSeatCount"
                        )
                        or merged_raw.get(
                            "RemainingSeatCount"
                        )
                    ),

                    "total": norm(
                        merged_raw.get(
                            "TotalSeatCount"
                        )
                        or merged_raw.get(
                            "SeatCount"
                        )
                    ),

                    "event_type": classify_event(
                        merged_raw
                    ),

                    "booking_state": state,

                    "booking_state_source": (
                        source
                    ),

                    "raw_booking_code": (
                        raw_status
                    ),

                    "raw": merged_raw,
                })

            for child in value.values():
                walk(
                    child,
                    event_context,
                )

        elif isinstance(value, list):
            for child in value:
                walk(
                    child,
                    inherited_event,
                )

    walk(data, {})

    return rows


def show_key(show):
    raw = "|".join([
        CINEMA_CODE,
        show.get("date", ""),
        (
            show.get("movie_code", "")
            or compact(
                show.get("movie", "")
            )
        ),
        (
            show.get("screen_id", "")
            or compact(
                show.get("screen", "")
            )
        ),
        (
            show.get(
                "play_sequence",
                "",
            )
            or show.get("time", "")
        ),
        show.get("time", ""),
    ])

    return hashlib.sha1(
        raw.encode("utf-8")
    ).hexdigest()


def _show_quality(show):
    raw = show.get("raw") or {}

    score = 0

    if show.get(
        "event_type"
    ) in {
        "GV",
        "STAGE",
    }:
        score += 1000

    code = normalize_event_code(
        raw.get(
            "AccompanyTypeCode"
        )
    )

    if code in {
        "30",
        "40",
    }:
        score += 500

    score += 50 * sum(
        bool(
            norm(
                raw.get(key)
            )
        )
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
        bool(
            show.get(field)
        )
        for field in (
            "movie_code",
            "screen_id",
            "screen_division_code",
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

        if (
            old is None
            or _show_quality(show)
            > _show_quality(old)
        ):
            result[key] = show

    return list(
        result.values()
    )


# ============================================================
# Lotte scanning
# ============================================================

MOVIE_CACHE = {
    "ts": 0.0,
    "items": [],
}


def get_movies(ttl=900):
    now = time.time()

    if (
        MOVIE_CACHE["items"]
        and now
        - MOVIE_CACHE["ts"]
        < ttl
    ):
        return MOVIE_CACHE[
            "items"
        ]

    payload = {
        "MethodName": (
            "GetTicketingPageTOBE"
        ),
        "channelType": "HO",
        "osType": "W",
        "osVersion": UA,
        "memberOnNo": "0",
    }

    data = lotte_post(
        payload
    )

    found = []

    def walk(value):
        if isinstance(
            value,
            dict,
        ):
            name = norm(
                value.get(
                    "MovieNameKR"
                )
                or value.get(
                    "MovieName"
                )
            )

            code = norm(
                value.get(
                    "RepresentationMovieCode"
                )
                or value.get(
                    "MovieCode"
                )
            )

            if name and code:
                found.append(
                    (code, name)
                )

            for child in (
                value.values()
            ):
                walk(child)

        elif isinstance(
            value,
            list,
        ):
            for child in value:
                walk(child)

    walk(data)

    items = list(
        dict.fromkeys(
            found
        )
    )

    MOVIE_CACHE["ts"] = (
        now
    )

    MOVIE_CACHE["items"] = (
        items
    )

    return items


def fetch_movie_specific(
    date,
    movie_code,
    movie_name="",
):
    payload = {
        "MethodName": (
            "GetPlaySequence"
        ),
        "channelType": "HO",
        "osType": "W",
        "osVersion": UA,
        "playDate": date,
        "cinemaID": CINEMA_ID,
        "representationMovieCode": (
            movie_code
        ),
    }

    data = lotte_post(
        payload
    )

    return extract_sequences(
        data,
        date,
        fallback_movie=movie_name,
        fallback_code=movie_code,
    )


def fetch_date(date):
    rows = []

    payload = {
        "MethodName": (
            "GetPlaySequence"
        ),
        "channelType": "HO",
        "osType": "W",
        "osVersion": UA,
        "playDate": date,
        "cinemaID": CINEMA_ID,
        "representationMovieCode": "",
    }

    try:
        data = lotte_post(
            payload
        )

        rows.extend(
            extract_sequences(
                data,
                date,
            )
        )

    except Exception as error:
        log(
            f"전체조회 실패 "
            f"{date}: {error}"
        )

    # 전체조회 결과에 행사정보가 없거나
    # 회차 정보가 부족할 수 있어
    # 필요한 영화별 조회를 추가한다.
    if not any(
        row.get(
            "event_type"
        )
        for row in rows
    ):
        try:
            movies = get_movies()

            for (
                movie_code,
                movie_name,
            ) in movies:
                try:
                    extra = (
                        fetch_movie_specific(
                            date,
                            movie_code,
                            movie_name,
                        )
                    )

                    rows.extend(
                        extra
                    )

                except Exception:
                    continue

        except Exception:
            pass

    rows = dedupe(rows)

    return [
        row
        for row in rows
        if row.get(
            "event_type"
        )
        in {
            "GV",
            "STAGE",
        }
    ]


def scan_all_50_days():
    events = {}

    errors = 0

    today = now_kst().date()

    for offset in range(
        TOTAL_DAYS
    ):
        date = (
            today
            + timedelta(
                days=offset
            )
        ).isoformat()

        try:
            rows = fetch_date(
                date
            )

            for show in rows:
                events[
                    show_key(show)
                ] = show

        except Exception as error:
            errors += 1

            log(
                f"SCAN ERROR "
                f"{date}: {error}"
            )

    return (
        events,
        "50_DAY_SCAN",
        errors,
    )


# ============================================================
# Actual booking verification
# ============================================================

def get_book_possible(show):
    """
    회차가 실제 예매 가능한 단계까지
    진입 가능한지 추가 확인.

    이 API는 응답 구조가 회차/시점에 따라
    달라질 수 있으므로 명확한 성공 신호가
    있을 때만 True 처리한다.
    """

    if not (
        show.get("screen_id")
        and show.get(
            "play_sequence"
        )
    ):
        return None

    candidates = []

    try:
        payload = {
            "MethodName": (
                "GetBookPossible"
            ),
            "channelType": "HO",
            "osType": "W",
            "osVersion": UA,
            "cinemaId": int(
                CINEMA_CODE
            ),
            "screenId": int(
                show[
                    "screen_id"
                ]
            ),
            "playDate": (
                show.get(
                    "play_date"
                )
                or show.get(
                    "date"
                )
            ),
            "playSequence": int(
                show[
                    "play_sequence"
                ]
            ),
        }

        data = lotte_post(
            payload
        )

        candidates.append(
            data
        )

    except Exception:
        pass

    for data in candidates:
        text = compact(
            all_scalar_text(
                data
            )
        ).upper()

        if any(
            token in text
            for token in (
                "예매가능",
                "BOOKPOSSIBLE",
                "BOOKINGPOSSIBLE",
            )
        ):
            return True

        if (
            isinstance(data, dict)
            and data.get("IsOK")
            is True
        ):
            # 단순 IsOK만으로 확정하지 않고
            # 명시적인 실패/준비 문구가 없을 때만
            # 보조 신호로 사용.
            if not any(
                token in text
                for token in (
                    "상영준비중",
                    "예매준비중",
                    "예매불가",
                    "마감",
                )
            ):
                return True

    return None


def get_seats(show):
    """
    기존 롯데 자동예매에서 실제 사용했던
    GetSeats 호출 구조.

    여기까지 정상 응답이 오면
    사용자가 회차를 선택해 좌석 단계로
    진입 가능한 상태에 매우 가까운 신호로 본다.
    """

    required = (
        show.get("screen_id"),
        show.get(
            "play_sequence"
        ),
        show.get(
            "screen_division_code"
        ),
    )

    if not all(required):
        return None

    play_date = (
        show.get("play_date")
        or show.get("date")
    )

    try:
        payload = {
            "MethodName": (
                "GetSeats"
            ),
            "channelType": "HO",
            "osType": "W",
            "osVersion": UA,
            "cinemaId": int(
                CINEMA_CODE
            ),
            "screenId": int(
                show[
                    "screen_id"
                ]
            ),
            "playDate": (
                play_date
            ),
            "playSequence": int(
                show[
                    "play_sequence"
                ]
            ),
            "screenDivisionCode": int(
                show[
                    "screen_division_code"
                ]
            ),
        }

        data = lotte_post(
            payload
        )

        if data is None:
            return None

        text = compact(
            all_scalar_text(
                data
            )
        )

        if any(
            token in text
            for token in (
                "상영준비중",
                "예매준비중",
                "예매불가",
            )
        ):
            return False

        return True

    except Exception:
        return None


def verify_real_open(show):
    """
    Y만으로 OPEN 처리하지 않는다.

    1) GetBookPossible
    2) GetSeats

    둘 중 하나라도 실제 예매 가능 신호가
    확인돼야 OPEN.
    """

    possible = (
        get_book_possible(
            show
        )
    )

    if possible is True:
        return (
            True,
            "GetBookPossible",
        )

    seats = get_seats(
        show
    )

    if seats is True:
        return (
            True,
            "GetSeats",
        )

    return (
        False,
        "",
    )


# ============================================================
# State
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
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


def record(
    show,
    status,
):
    return {
        "status": status,

        "event_type": (
            show.get(
                "event_type"
            )
        ),

        "date": (
            show.get(
                "date"
            )
        ),

        "movie": (
            show.get(
                "movie"
            )
        ),

        "movie_code": (
            show.get(
                "movie_code"
            )
        ),

        "time": (
            show.get(
                "time"
            )
        ),

        "screen": (
            show.get(
                "screen"
            )
        ),

        "screen_id": (
            show.get(
                "screen_id"
            )
        ),

        "screen_division_code": (
            show.get(
                "screen_division_code"
            )
        ),

        "play_sequence": (
            show.get(
                "play_sequence"
            )
        ),

        "booking_state_source": (
            show.get(
                "booking_state_source"
            )
        ),

        "raw_booking_code": (
            show.get(
                "raw_booking_code"
            )
        ),

        "updated_at_kst": (
            now_kst().isoformat(
                timespec="seconds"
            )
        ),
    }


# ============================================================
# Discord
# ============================================================

def booking_url(show):
    params = {
        "link_channelCode": "naver",
        "link_cinemaCode": (
            CINEMA_CODE
        ),
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
        params[
            "link_movieCd"
        ]
        and params[
            "link_screenId"
        ]
        and params[
            "link_time"
        ]
    ):
        return (
            "https://www.lottecinema.co.kr/"
            "NLCMW/ticketing?"
            + urlencode(
                params
            )
        )

    return (
        "https://www.lottecinema.co.kr/"
        "NLCMW/ticketing"
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

        "flags": 4,

        "allowed_mentions": {
            "parse": [],
            "users": (
                [DISCORD_MENTION_ID]
                if DISCORD_MENTION_ID
                else []
            ),
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
        return (
            f"{start} ~ {end}"
        )

    return start


def send_alert(
    show,
    status,
):
    if status == "PREPARING":
        title = (
            f"⏳ {SITE_NAME} "
            f"{event_name(show)} "
            f"상영준비중"
        )

        action = (
            "예매가 열리는지 "
            "계속 확인합니다."
        )

    elif status == "OPEN":
        title = (
            f"🚨 {SITE_NAME} "
            f"{event_name(show)} "
            f"예매 오픈!"
        )

        action = (
            "지금 바로 "
            "예매 확인해."
        )

    else:
        return

    seat = ""

    if show.get(
        "remain"
    ):
        seat = (
            f"\n💺 잔여 "
            f"{show['remain']}"
        )

        if show.get(
            "total"
        ):
            seat += (
                f" / "
                f"{show['total']}"
            )

    content = (
        (
            f"<@{DISCORD_MENTION_ID}>\n"
            if DISCORD_MENTION_ID
            else ""
        )
        + f"**{title}**\n"
        + f"🎬 "
        + (
            show.get("movie")
            or "영화명 확인 필요"
        )
        + "\n"
        + f"📅 "
        + f"{show.get('date')}   "
        + f"⏰ {show_when(show)}\n"
        + f"🎞️ "
        + (
            show.get("screen")
            or "상영관 정보 없음"
        )
        + seat
        + "\n"
        + action
        + "\n"
        + f"🎟️ {booking_url(show)}"
    )

    discord_post(
        content
    )


# ============================================================
# Early diagnostics
# ============================================================

def state_name_korean(status):
    return {
        "EARLY": "선행 신호",
        "PREPARING": "상영준비중",
        "OPEN": "예매 가능",
        "SOLD_OUT": "매진",
        "CLOSED": "예매 불가/마감",
        "UNKNOWN": "미확인",
    }.get(
        status,
        norm(status)
        or "미확인",
    )


def log_new_event_diagnostics(
    events,
    state,
):
    detected = 0

    ordered = sorted(
        events.items(),
        key=lambda item: (
            item[1].get(
                "date",
                "",
            ),
            item[1].get(
                "time",
                "",
            ),
            item[1].get(
                "movie",
                "",
            ),
            item[1].get(
                "screen",
                "",
            ),
        ),
    )

    for (
        key,
        show,
    ) in ordered:
        if key in state:
            continue

        kind = event_name(
            show
        )

        row = (
            show.get("raw")
            or {}
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
                or row.get(
                    "AccompanyTypeName"
                )
            )
            or "-"
        )

        log("")
        log(
            "🔎 API 선행 진단"
        )
        log(
            f"{kind}가 감지됐습니다"
        )

        log(
            f"{show.get('date')} / "
            f"{show.get('movie') or '영화명 확인 필요'} / "
            f"{show_when(show)} / "
            f"{show.get('screen') or '상영관 정보 없음'}"
        )

        log(
            "상태: "
            f"{state_name_korean(show.get('booking_state'))} / "
            f"AccompanyTypeCode={accompany_code} / "
            f"AccompanyTypeNameKR={accompany_name}"
        )

        detected += 1

    return detected


# ============================================================
# Baseline
# ============================================================

def make_baseline(events):
    state = {}

    for key, show in (
        events.items()
    ):
        current = show.get(
            "booking_state",
            "UNKNOWN",
        )

        # 기존 Y는 OPEN으로 저장하지 않고
        # EARLY로 보존한다.
        if current == "EARLY":
            stored = "EARLY"
        else:
            stored = current

        state[key] = record(
            show,
            stored,
        )

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
    log("=" * 60)
    log("INITIAL BASELINE")
    log("=" * 60)

    log(
        "현재 GV / 무대인사 회차를 "
        "알림 없이 기준값으로 등록합니다."
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


# ============================================================
# Processing
# ============================================================

def process(
    events,
    state,
):
    sent = 0

    for (
        key,
        show,
    ) in events.items():

        detected_state = (
            show.get(
                "booking_state",
                "UNKNOWN",
            )
        )

        previous_record = (
            state.get(key)
            or {}
        )

        previous = (
            previous_record.get(
                "status"
            )
        )

        # ----------------------------------------------------
        # EARLY:
        # IsBookingYN=Y 이지만 아직 실제 오픈 검증 전
        # ----------------------------------------------------

        if detected_state == "EARLY":
            real_open, source = (
                verify_real_open(
                    show
                )
            )

            if real_open:
                current = "OPEN"

                show[
                    "booking_state_source"
                ] = (
                    "real_open:"
                    + source
                )

            else:
                current = "EARLY"

        else:
            current = (
                detected_state
            )

        # ----------------------------------------------------
        # 기존 구버전 상태 보정
        #
        # 과거 코드가 IsBookingYN=Y만 보고
        # OPEN으로 저장했더라도,
        # 현재 실오픈 검증이 실패하면 EARLY로 되돌린다.
        # ----------------------------------------------------

        if (
            previous == "OPEN"
            and detected_state
            == "EARLY"
            and current
            == "EARLY"
        ):
            old_source = norm(
                previous_record.get(
                    "booking_state_source"
                )
            )

            if (
                "IsBookingYN=Y"
                in old_source
                or not old_source
            ):
                log(
                    "기존 조기 OPEN 상태 보정: "
                    f"{event_name(show)} / "
                    f"{show.get('movie')} / "
                    f"{show.get('date')} "
                    f"{show.get('time')} "
                    "-> EARLY"
                )

                state[key] = record(
                    show,
                    "EARLY",
                )

                save_state(
                    state
                )

                continue

        # ----------------------------------------------------
        # UNKNOWN
        # ----------------------------------------------------

        if current == "UNKNOWN":
            state[key] = record(
                show,
                "UNKNOWN",
            )

            save_state(
                state
            )

            continue

        # ----------------------------------------------------
        # EARLY
        # ----------------------------------------------------

        if current == "EARLY":
            # 선행 신호는 Discord 오픈 알림 없음.
            state[key] = record(
                show,
                "EARLY",
            )

            save_state(
                state
            )

            continue

        # ----------------------------------------------------
        # SOLD OUT / CLOSED
        # ----------------------------------------------------

        if current in {
            "SOLD_OUT",
            "CLOSED",
        }:
            state[key] = record(
                show,
                current,
            )

            save_state(
                state
            )

            continue

        alert_status = None

        # ----------------------------------------------------
        # PREPARING
        # ----------------------------------------------------

        if current == "PREPARING":
            if previous in {
                None,
                "UNKNOWN",
                "EARLY",
                "CLOSED",
                "SOLD_OUT",
            }:
                alert_status = (
                    "PREPARING"
                )

            elif previous == "OPEN":
                # 이미 실제 OPEN 확정된 회차가
                # 다시 준비중처럼 보이는 일시 신호는
                # 재알림하지 않음.
                state[key] = record(
                    show,
                    "OPEN",
                )

                save_state(
                    state
                )

                continue

        # ----------------------------------------------------
        # OPEN
        # ----------------------------------------------------

        elif current == "OPEN":
            if previous in {
                None,
                "UNKNOWN",
                "EARLY",
                "PREPARING",
                "CLOSED",
                "SOLD_OUT",
            }:
                alert_status = (
                    "OPEN"
                )

        # ----------------------------------------------------
        # Alert
        # ----------------------------------------------------

        if alert_status:
            try:
                send_alert(
                    show,
                    alert_status,
                )

            except Exception as error:
                log(
                    "DISCORD ERROR: "
                    f"{event_name(show)} / "
                    f"{show.get('movie')} / "
                    f"{show.get('date')} "
                    f"{show.get('time')} / "
                    f"{error}"
                )

                continue

            state[key] = record(
                show,
                alert_status,
            )

            save_state(
                state
            )

            sent += 1

            if (
                alert_status
                == "PREPARING"
            ):
                log(
                    "⏳ 상영준비중: "
                    f"{event_name(show)} / "
                    f"{show.get('movie')} / "
                    f"{show.get('date')} "
                    f"{show.get('time')}"
                )

            else:
                log(
                    "🚨 실제 예매 오픈: "
                    f"{event_name(show)} / "
                    f"{show.get('movie')} / "
                    f"{show.get('date')} "
                    f"{show.get('time')}"
                )

            continue

        # 동일 상태 유지
        state[key] = record(
            show,
            previous
            or current,
        )

        save_state(
            state
        )

    return sent


# ============================================================
# Main
# ============================================================

def main():
    log("=" * 60)
    log(
        "LOTTE CINEMA "
        "GV/STAGE MONITOR "
        "- WORLDTOWER 1016"
    )
    log("=" * 60)

    log(
        "대상: GV / 무대인사"
    )

    log(
        "이벤트 코드: "
        "AccompanyTypeCode "
        "30=무대인사, 40=GV"
    )

    log(
        "IsBookingYN=Y: "
        "실제 오픈 확정 아님 / "
        "선행 신호로만 사용"
    )

    log(
        "상영준비중: "
        "API의 상영준비중/"
        "예매준비중 문구 감지"
    )

    log(
        "실제 오픈: "
        "GetBookPossible / "
        "GetSeats 추가 검증"
    )

    log(
        "감시 범위: "
        "오늘 ~ +49일 "
        "(50일 전체)"
    )

    log(
        f"목표 감시 주기: "
        f"{SCAN_INTERVAL:.0f}초"
    )

    log(
        f"RUN SECONDS: "
        f"{RUN_SECONDS}"
    )

    log("=" * 60)

    state = load_state()

    if not BASELINE_FILE.exists():
        (
            events,
            mode,
            errors,
        ) = scan_all_50_days()

        log(
            f"FETCH MODE: {mode}"
        )

        log(
            f"SCAN ERRORS: {errors}"
        )

        make_baseline(
            events
        )

        return

    started = time.time()
    cycle = 0

    while (
        time.time()
        - started
        < RUN_SECONDS
    ):
        cycle += 1

        cycle_started = (
            time.time()
        )

        log("")
        log("=" * 60)

        log(
            f"CYCLE #{cycle} "
            f"{now_kst().strftime('%Y-%m-%d %H:%M:%S KST')}"
        )

        log(
            "50일 전체 스캔 시작"
        )

        log("=" * 60)

        try:
            (
                cycle_events,
                mode,
                errors,
            ) = scan_all_50_days()

            log(
                f"FETCH MODE: "
                f"{mode}"
            )

            log(
                f"SCAN ERRORS: "
                f"{errors}"
            )

            early_detected = (
                log_new_event_diagnostics(
                    cycle_events,
                    state,
                )
            )

            sent = process(
                cycle_events,
                state,
            )

            save_state(
                state
            )

            log(
                "이번 사이클 "
                "API 선행 진단: "
                f"{early_detected}건"
            )

            log(
                "이번 사이클 "
                "Discord 알림: "
                f"{sent}건"
            )

            log(
                f"저장된 상태: "
                f"{len(state)}건"
            )

        except Exception as error:
            log(
                "SCAN/PROCESS ERROR: "
                f"{type(error).__name__}: "
                f"{error}"
            )

        elapsed = (
            time.time()
            - cycle_started
        )

        remaining = (
            RUN_SECONDS
            - (
                time.time()
                - started
            )
        )

        if remaining <= 0:
            break

        wait = min(
            max(
                0.0,
                SCAN_INTERVAL
                - elapsed,
            ),
            remaining,
        )

        log(
            f"사이클 소요시간: "
            f"{elapsed:.2f}초"
        )

        if wait > 0:
            log(
                "다음 50일 스캔까지 "
                f"{wait:.2f}초 대기"
            )

            time.sleep(
                wait
            )

        else:
            log(
                "50일 조회 시간이 "
                "목표 주기 이상이므로 "
                "대기 없이 다음 사이클을 "
                "시작합니다."
            )

    log("")
    log("=" * 60)
    log("RUN COMPLETE")
    log("=" * 60)


if __name__ == "__main__":
    main()
