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

LOTTE_API = 'https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx'
SITE_NAME = '롯데시네마 월드타워'
CINEMA_ID = '1|0001|1016'
CINEMA_CODE = '1016'

TOTAL_DAYS = 50
SCAN_INTERVAL = 10.0

RUN_SECONDS = int(os.getenv('RUN_SECONDS', '19200'))
STATUS_LOG_SECONDS = int(os.getenv('STATUS_LOG_SECONDS', '600'))
VERBOSE_API_LOGS = os.getenv('VERBOSE_API_LOGS', '0').strip() == '1'

FIXED_SAFETY_SCAN_MINUTES = 5
FIXED_CONCURRENT_START_OFFSET = 4
FIXED_CONCURRENT_DAYS = 18
FIXED_CONCURRENT_WORKERS = 18

DISCORD_WEBHOOK = os.getenv('DISCORD_LOTTE_WORLDTOWER', '').strip()
DISCORD_MENTION_ID = '1383846907847381184'

STATE_FILE = Path('seen_lotte_worldtower.json')
BASELINE_FILE = Path('baseline_lotte_worldtower.done')

KST = ZoneInfo('Asia/Seoul')

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/151.0.0.0 Safari/537.36'
)

SESSION_HEADERS = {
    'User-Agent': UA,
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://www.lottecinema.co.kr/NLCHS/Ticketing',
    'Origin': 'https://www.lottecinema.co.kr',
}

_THREAD_LOCAL = threading.local()


def get_session():
    session = getattr(_THREAD_LOCAL, 'session', None)

    if session is None:
        session = requests.Session()
        session.headers.update(SESSION_HEADERS)
        _THREAD_LOCAL.session = session

    return session


def reset_session():
    session = getattr(_THREAD_LOCAL, 'session', None)

    if session is not None:
        try:
            session.close()
        except Exception:
            pass

    _THREAD_LOCAL.session = None


def log(message=''):
    print(message, flush=True)


def now_kst():
    return datetime.now(KST)


def norm(value):
    return ' '.join(str(value or '').split())


def compact(value):
    return re.sub(r'\s+', '', norm(value))


def bounded_gv(text):
    return bool(
        re.search(
            r'(?<![A-Z0-9])GV(?![A-Z0-9])',
            str(text or '').upper(),
        )
    )


def lotte_post(payload):
    files = {
        'paramList': (
            None,
            json.dumps(
                payload,
                ensure_ascii=False,
            ),
        )
    }

    transient_statuses = {
        429,
        500,
        502,
        503,
        504,
    }

    last_error = None

    for attempt in range(2):
        try:
            response = get_session().post(
                LOTTE_API,
                files=files,
                timeout=15,
            )

            if response.status_code in transient_statuses:
                if attempt == 0:
                    reset_session()
                    time.sleep(0.8)
                    continue

            response.raise_for_status()

            try:
                data = response.json()

            except ValueError as error:
                last_error = error

                if attempt == 0:
                    reset_session()
                    time.sleep(0.8)
                    continue

                raise

            return data, response

        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as error:
            last_error = error

            if attempt == 0:
                reset_session()
                time.sleep(0.8)
                continue

            raise

    if last_error is not None:
        raise last_error

    raise RuntimeError(
        'LOTTE API retry failed without an explicit error'
    )


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
    return ' | '.join(
        scalar_texts(obj)
    )


EVENT_FIELDS = (
    'AccompanyTypeCode',
    'AccompanyTypeNameKR',
    'AccompanyTypeNameUS',
    'AccompanyTypeName',
    'EventNameKR',
    'EventNameUS',
    'EventName',
    'EventTypeName',
    'EventTypeNameKR',
    'EventTypeNameUS',
    'SpecialName',
    'SpecialNameKR',
    'SpecialNameUS',
    'SpecialType',
    'SpecialTypeName',
    'SpecialTypeNameKR',
    'SpecialTypeNameUS',
    'SpecialMsg',
    'SpecialMsgKR',
    'SpecialMsgUS',
    'SpecialScreenName',
    'PlayKind',
    'PlayKindName',
    'PlayKindNameKR',
    'PlayKindNameUS',
    'MovieKindName',
    'RepresentationMovieTypeName',
)

SHORT_STAGE_FIELDS = (
    'EventNameKR',
    'EventName',
    'EventTypeName',
    'EventTypeNameKR',
    'SpecialName',
    'SpecialNameKR',
    'SpecialType',
    'SpecialTypeName',
    'SpecialTypeNameKR',
    'PlayKind',
    'PlayKindName',
    'PlayKindNameKR',
)

EVENT_CONTEXT_FIELDS = tuple(
    dict.fromkeys(
        EVENT_FIELDS
        + (
            'AccompanyTypeCode',
            'AccompanyTypeNameKR',
            'AccompanyTypeNameUS',
            'AccompanyTypeName',
        )
    )
)


def normalize_event_code(value):
    text = norm(value)

    if not text:
        return ''

    match = re.fullmatch(
        r'0*(\d+)(?:\.0+)?',
        text,
    )

    if match:
        try:
            return str(
                int(match.group(1))
            )
        except Exception:
            pass

    return text


def classify_event(row):
    accompany_code = normalize_event_code(
        row.get('AccompanyTypeCode')
    )

    if accompany_code == '70':
        return None

    if accompany_code == '30':
        return 'STAGE'

    if accompany_code == '40':
        return 'GV'

    focused = ' | '.join(
        norm(row.get(key))
        for key in EVENT_FIELDS
        if norm(row.get(key))
    )

    fc = compact(focused)

    if (
        '무대인사' in fc
        or '舞台挨拶' in focused
    ):
        return 'STAGE'

    if any(
        compact(row.get(key)) == '무대'
        for key in SHORT_STAGE_FIELDS
    ):
        return 'STAGE'

    if (
        '관객과의대화' in fc
        or bounded_gv(focused)
    ):
        return 'GV'

    return None


BOOKING_TEXT_FIELDS = (
    'BookingStatusName',
    'BookingStatusNameKR',
    'BookingStateName',
    'BookingStateNameKR',
    'TicketingStatusName',
    'TicketingStatusNameKR',
    'SaleStatusName',
    'SaleStatusNameKR',
    'StatusName',
    'StatusNameKR',
)


def booking_state(row):
    status_text = ' | '.join(
        norm(row.get(key))
        for key in BOOKING_TEXT_FIELDS
        if norm(row.get(key))
    )

    full_text = (
        status_text
        + ' | '
        + all_scalar_text(row)
    )

    fc = compact(full_text)

    if (
        '상영준비중' in fc
        or '예매준비중' in fc
    ):
        return (
            'PREPARING',
            'explicit_preparing_text',
            '',
        )

    if (
        '예매가능' in fc
        or '예매하기' in fc
    ):
        return (
            'OPEN',
            'explicit_open_text',
            '',
        )

    value = row.get('IsBookingYN')

    if value is None:
        for key in (
            'BookingYN',
            'IsBookingYn',
            'isBookingYN',
            'SaleYN',
        ):
            if key in row:
                value = row.get(key)
                break

    code = str(
        value or ''
    ).strip().upper()

    if code in {
        'Y',
        'YES',
        'TRUE',
        '1',
    }:
        return (
            'OPEN',
            'IsBookingYN=Y',
            code,
        )

    if code == 'E':
        return (
            'SOLD_OUT',
            'IsBookingYN=E',
            code,
        )

    if code in {
        'N',
        'NO',
        'FALSE',
        '0',
    }:
        return (
            'CLOSED',
            'IsBookingYN=N',
            code,
        )

    return (
        'UNKNOWN',
        f"IsBookingYN={code or 'missing'}",
        code or '(blank)',
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


def extract_sequences(
    data,
    date,
    fallback_movie='',
    fallback_code='',
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
                _event_context_from_dict(value)
            )

            start = norm(
                value.get('StartTime')
                or value.get('PlayStartTime')
                or value.get('StartTm')
            )

            screen = norm(
                value.get('ScreenNameKR')
                or value.get('ScreenName')
                or value.get('ScreenID')
            )

            if start and screen:
                merged_raw = dict(
                    event_context
                )

                merged_raw.update(
                    value
                )

                state, source, raw_status = booking_state(
                    merged_raw
                )

                rows.append(
                    {
                        'date': date,
                        'movie': norm(
                            merged_raw.get('MovieNameKR')
                            or merged_raw.get('MovieName')
                            or fallback_movie
                        ),
                        'movie_code': norm(
                            merged_raw.get('RepresentationMovieCode')
                            or merged_raw.get('MovieCode')
                            or fallback_code
                        ),
                        'time': start,
                        'end_time': norm(
                            merged_raw.get('EndTime')
                        ),
                        'screen': screen,
                        'screen_id': norm(
                            merged_raw.get('ScreenID')
                            or merged_raw.get('ScreenId')
                            or merged_raw.get('ScreenCode')
                        ),
                        'play_sequence': norm(
                            merged_raw.get('PlaySequence')
                            or merged_raw.get('PlaySeq')
                            or merged_raw.get('Sequence')
                        ),
                        'remain': norm(
                            merged_raw.get('BookingSeatCount')
                            or merged_raw.get('RemainSeatCount')
                            or merged_raw.get('RemainingSeatCount')
                        ),
                        'total': norm(
                            merged_raw.get('TotalSeatCount')
                            or merged_raw.get('SeatCount')
                        ),
                        'event_type': classify_event(
                            merged_raw
                        ),
                        'booking_state': state,
                        'booking_state_source': source,
                        'raw_booking_code': raw_status,
                        'raw': merged_raw,
                    }
                )

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

    walk(
        data,
        {},
    )

    return rows


def show_key(show):
    raw = '|'.join(
        [
            CINEMA_CODE,
            show.get('date', ''),
            show.get('movie_code', '')
            or compact(
                show.get('movie', '')
            ),
            show.get('screen_id', '')
            or compact(
                show.get('screen', '')
            ),
            show.get('play_sequence', '')
            or show.get('time', ''),
            show.get('time', ''),
        ]
    )

    return hashlib.sha1(
        raw.encode('utf-8')
    ).hexdigest()


def _show_quality(show):
    raw = show.get('raw') or {}
    event = show.get('event_type')

    score = 0

    if event in {
        'GV',
        'STAGE',
    }:
        score += 1000

    code = normalize_event_code(
        raw.get('AccompanyTypeCode')
    )

    if code in {
        '30',
        '40',
    }:
        score += 500

    score += 50 * sum(
        bool(norm(raw.get(key)))
        for key in (
            'AccompanyTypeNameKR',
            'EventNameKR',
            'EventTypeNameKR',
            'SpecialNameKR',
            'SpecialTypeNameKR',
            'PlayKindNameKR',
        )
    )

    score += sum(
        bool(show.get(field))
        for field in (
            'movie_code',
            'screen_id',
            'play_sequence',
            'end_time',
            'remain',
            'total',
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


MOVIE_CACHE = {
    'ts': 0.0,
    'items': [],
}


def get_movies(ttl=900):
    now = time.time()

    if (
        MOVIE_CACHE['items']
        and now - MOVIE_CACHE['ts'] < ttl
    ):
        return MOVIE_CACHE['items']

    payload = {
        'MethodName': 'GetTicketingPageTOBE',
        'channelType': 'HO',
        'osType': 'W',
        'osVersion': UA,
        'memberOnNo': '0',
    }

    data, _ = lotte_post(
        payload
    )

    found = []

    def walk(value):
        if isinstance(value, dict):
            name = norm(
                value.get('MovieNameKR')
                or value.get('MovieName')
            )

            code = norm(
                value.get('RepresentationMovieCode')
                or value.get('MovieCode')
            )

            if name and code:
                found.append(
                    (
                        code,
                        name,
                    )
                )

            for child in value.values():
                walk(child)

        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)

    items = list(
        dict.fromkeys(found)
    )

    MOVIE_CACHE['ts'] = now
    MOVIE_CACHE['items'] = items

    return items


def fetch_movie_specific(
    date,
    movie_code,
    movie_name='',
):
    payload = {
        'MethodName': 'GetPlaySequence',
        'channelType': 'HO',
        'osType': 'W',
        'osVersion': UA,
        'playDate': date,
        'cinemaID': CINEMA_ID,
        'representationMovieCode': movie_code,
    }

    data, _ = lotte_post(
        payload
    )

    return extract_sequences(
        data,
        date,
        fallback_movie=movie_name,
        fallback_code=movie_code,
    )


def _future_enrich_needed(
    date,
    rows,
):
    if not rows:
        return False

    try:
        target_date = datetime.strptime(
            date,
            '%Y-%m-%d',
        ).date()

        offset = (
            target_date
            - now_kst().date()
        ).days

    except Exception:
        return False

    return offset >= 14


def fetch_date_primary(date):
    payload = {
        'MethodName': 'GetPlaySequence',
        'channelType': 'HO',
        'osType': 'W',
        'osVersion': UA,
        'playDate': date,
        'cinemaID': CINEMA_ID,
        'representationMovieCode': '',
    }

    started = time.time()

    data, response = lotte_post(
        payload
    )

    primary_rows = dedupe(
        extract_sequences(
            data,
            date,
        )
    )

    primary_event_count = sum(
        row.get('event_type')
        in {
            'GV',
            'STAGE',
        }
        for row in primary_rows
    )

    rows = list(
        primary_rows
    )

    enrich_calls = 0
    enrich_added_events = 0

    if _future_enrich_needed(
        date,
        primary_rows,
    ):
        movie_map = {}

        for row in primary_rows:
            code = norm(
                row.get('movie_code')
            )

            name = norm(
                row.get('movie')
            )

            if code:
                movie_map.setdefault(
                    code,
                    name,
                )

        for movie_code, movie_name in movie_map.items():
            try:
                specific_rows = fetch_movie_specific(
                    date,
                    movie_code,
                    movie_name,
                )

                enrich_calls += 1

                rows.extend(
                    specific_rows
                )

            except Exception as error:
                log(
                    f"ENRICH {date.replace('-', '')} "
                    f"MOVIE={movie_code} ERROR: "
                    f"{type(error).__name__}: {error}"
                )

        rows = dedupe(
            rows
        )

        final_event_count = sum(
            row.get('event_type')
            in {
                'GV',
                'STAGE',
            }
            for row in rows
        )

        enrich_added_events = max(
            0,
            final_event_count
            - primary_event_count,
        )

    else:
        final_event_count = (
            primary_event_count
        )

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
        'ALL-MOVIES MODE RETURNED 0 TOTAL ROWS '
        f'-> FALLBACK {len(movies)} MOVIES'
    )

    all_rows = []

    for date in dates:
        for movie_code, movie_name in movies:
            try:
                all_rows.extend(
                    fetch_movie_specific(
                        date,
                        movie_code,
                        movie_name,
                    )
                )

            except Exception:
                pass

    return dedupe(
        all_rows
    )


def make_dates(
    start_offset,
    day_count,
):
    today = now_kst().date()

    return [
        (
            today
            + timedelta(
                days=start_offset
                + index
            )
        ).strftime(
            '%Y-%m-%d'
        )
        for index in range(
            day_count
        )
    ]


def scan_dates(
    dates,
    label,
):
    all_rows = []
    errors = 0

    if VERBOSE_API_LOGS:
        log('')

        log(
            f'{label} SCAN: '
            f'{dates[0]} ~ {dates[-1]} '
            f'({len(dates)} DAYS)'
        )

    for date in dates:
        try:
            all_rows.extend(
                fetch_date_primary(
                    date
                )
            )

        except Exception as error:
            errors += 1

            log(
                f"API {date.replace('-', '')} ERROR: "
                f"{type(error).__name__}: {error}"
            )

    mode = (
        'ALL_MOVIES+FUTURE_ENRICH'
    )

    if (
        not all_rows
        and errors < len(dates)
    ):
        try:
            all_rows = fallback_scan_dates(
                dates
            )

            mode = 'FALLBACK'

        except Exception as error:
            log(
                f'FALLBACK ERROR: '
                f'{type(error).__name__}: '
                f'{error}'
            )

            mode = 'ERROR'

    events = {}

    for show in dedupe(
        all_rows
    ):
        if show.get(
            'event_type'
        ) in {
            'GV',
            'STAGE',
        }:
            events[
                show_key(show)
            ] = show

    return (
        events,
        mode,
        errors,
    )


def scan_all_50_days():
    dates = make_dates(
        0,
        TOTAL_DAYS,
    )

    return scan_dates(
        dates,
        'FULL 50-DAY',
    )


def scan_fixed_18_days_concurrent():
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
            executor.submit(
                fetch_date_primary,
                date,
            ): date
            for date in dates
        }

        for future in as_completed(
            future_map
        ):
            date = future_map[
                future
            ]

            try:
                rows_by_date[
                    date
                ] = future.result()

            except Exception as error:
                errors += 1

                if (
                    len(error_examples)
                    < 3
                ):
                    error_examples.append(
                        f'{date}: '
                        f'{type(error).__name__}: '
                        f'{error}'
                    )

    all_rows = []

    for date in dates:
        all_rows.extend(
            rows_by_date.get(
                date,
                [],
            )
        )

    events = {}

    for show in dedupe(
        all_rows
    ):
        if show.get(
            'event_type'
        ) in {
            'GV',
            'STAGE',
        }:
            events[
                show_key(show)
            ] = show

    if error_examples:
        log(
            '⚠️ 18일 동시점검 오류 예시: '
            + ' | '.join(
                error_examples
            )
        )

    return (
        events,
        'CONCURRENT_18DAY',
        errors,
    )


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(
            STATE_FILE.read_text(
                encoding='utf-8'
            )
        )

        return (
            data
            if isinstance(
                data,
                dict,
            )
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
        encoding='utf-8',
    )


def record(
    show,
    status,
):
    return {
        'status': status,
        'event_type': show.get(
            'event_type'
        ),
        'date': show.get(
            'date'
        ),
        'movie': show.get(
            'movie'
        ),
        'movie_code': show.get(
            'movie_code'
        ),
        'time': show.get(
            'time'
        ),
        'screen': show.get(
            'screen'
        ),
        'screen_id': show.get(
            'screen_id'
        ),
        'play_sequence': show.get(
            'play_sequence'
        ),
        'booking_state_source': show.get(
            'booking_state_source'
        ),
        'raw_booking_code': show.get(
            'raw_booking_code'
        ),
        'updated_at_kst': now_kst().isoformat(
            timespec='seconds'
        ),
    }


def booking_url(show):
    params = {
        'link_channelCode': 'naver',
        'link_cinemaCode': CINEMA_CODE,
        'link_date': show.get(
            'date',
            '',
        ),
        'link_movieCd': show.get(
            'movie_code',
            '',
        ),
        'link_screenId': show.get(
            'screen_id',
            '',
        ),
        'link_time': show.get(
            'time',
            '',
        ),
    }

    if (
        params['link_movieCd']
        and params['link_screenId']
        and params['link_time']
    ):
        return (
            'https://www.lottecinema.co.kr/'
            'NLCMW/ticketing?'
            + urlencode(params)
        )

    return (
        'https://www.lottecinema.co.kr/'
        'NLCMW/ticketing'
    )


def event_name(show):
    return (
        '무대인사'
        if show.get(
            'event_type'
        ) == 'STAGE'
        else 'GV'
    )


def discord_post(content):
    if not DISCORD_WEBHOOK.startswith(
        'https://discord.com/api/webhooks/'
    ):
        raise RuntimeError(
            'DISCORD_LOTTE_WORLDTOWER '
            'Secret이 올바르지 않습니다.'
        )

    payload = {
        'content': content,
        'flags': 4,
        'allowed_mentions': {
            'parse': [],
            'users': [
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
        'time',
        '',
    )

    end = show.get(
        'end_time',
        '',
    )

    if end:
        return (
            f'{start} ~ {end}'
        )

    return start


def pretty_alert_date(value):
    text = norm(value)

    for fmt in (
        '%Y-%m-%d',
        '%Y%m%d',
    ):
        try:
            date = datetime.strptime(
                text,
                fmt,
            )

            weekdays = [
                '월',
                '화',
                '수',
                '목',
                '금',
                '토',
                '일',
            ]

            return (
                f'{date.year}.'
                f'{date.month}.'
                f'{date.day}'
                f'({weekdays[date.weekday()]})'
            )

        except ValueError:
            pass

    return text


def alert_time_range(show):
    start = norm(
        show.get('time')
    )

    end = norm(
        show.get('end_time')
    )

    if start and end:
        return (
            f'{start}–{end}'
        )

    return (
        start
        or end
        or '시간 정보 없음'
    )


def alert_line(show):
    text = (
        f"🎟 {alert_time_range(show)} · "
        f"{show.get('movie') or '영화명 확인 필요'} · "
        f"{show.get('screen') or '상영관 정보 없음'}"
    )

    return (
        f'**[{text}]'
        f'({booking_url(show)})**'
    )


def send_alert_group(
    shows,
    status,
):
    if not shows:
        return

    shows = sorted(
        shows,
        key=lambda item: (
            norm(item.get('time')),
            norm(item.get('movie')),
            norm(item.get('screen')),
        ),
    )

    first = shows[0]

    kind = event_name(
        first
    )

    if status == 'PREPARING':
        detected = (
            f'⏳ {kind} '
            f'상영준비중이 감지됐습니다'
        )

    else:
        detected = (
            f'🔎 {kind}가 감지됐습니다'
        )

    header = [
        f'<@{DISCORD_MENTION_ID}>',
        f'**{detected}**',
        f'**🎬 {SITE_NAME} · {kind}**',
        f"**📅 {pretty_alert_date(first.get('date'))}**",
    ]

    current = list(
        header
    )

    for show in shows:
        line = alert_line(
            show
        )

        candidate = '\n'.join(
            current
            + [
                line
            ]
        )

        if (
            len(candidate) > 1900
            and len(current)
            > len(header)
        ):
            discord_post(
                '\n'.join(
                    current
                )
            )

            current = list(
                header
            )

        current.append(
            line
        )

    if (
        len(current)
        > len(header)
    ):
        discord_post(
            '\n'.join(
                current
            )
        )


def diagnostic_fields(show):
    row = show.get(
        'raw'
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

        text = norm(
            value
        )

        if not text:
            continue

        key_lower = str(
            key
        ).lower()

        if (
            any(
                word in key_lower
                for word in (
                    'event',
                    'special',
                    'kind',
                    'type',
                    'accompany',
                    'booking',
                    'sale',
                    'screen',
                    'movie',
                    'play',
                    'status',
                    'sequence',
                )
            )
            or '무대' in compact(text)
            or '관객과의대화'
            in compact(text)
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
            f'• {key} = {value}'
        )

        if sum(
            len(x)
            for x in field_lines
        ) < 900:
            field_lines.append(
                line
            )

    details = '\n'.join(
        field_lines
    )

    content = (
        f'<@{DISCORD_MENTION_ID}>\n'
        f'**⚠️ 롯데시네마 월드타워 '
        f'{event_name(show)} '
        f'미확인 예매 상태 발견**\n'
        f"🎬 {show.get('movie') or '영화명 확인 필요'}\n"
        f"📅 {show.get('date')}   "
        f'⏰ {show_when(show)}\n'
        f"🎞️ {show.get('screen') or '상영관 정보 없음'}\n"
        f"🔎 상태: {show.get('booking_state_source')}\n"
        f'아직 정의하지 않은 롯데 상태입니다. '
        f'상영준비중 표본일 수 있으니 확인 필요.\n'
    )

    if details:
        content += (
            '\n'
            + details
        )

    discord_post(
        content[:1950]
    )


def send_test():
    sample_date = now_kst().strftime(
        '%Y-%m-%d'
    )

    samples = [
        {
            'event_type': 'STAGE',
            'date': sample_date,
            'time': '17:35',
            'end_time': '19:34',
            'movie': '알림 테스트',
            'screen': '3관',
            'movie_code': '',
            'screen_id': '',
        },
        {
            'event_type': 'STAGE',
            'date': sample_date,
            'time': '20:05',
            'end_time': '22:04',
            'movie': '알림 테스트',
            'screen': '3관',
            'movie_code': '',
            'screen_id': '',
        },
    ]

    send_alert_group(
        samples,
        'OPEN',
    )

    time.sleep(1)

    send_alert_group(
        samples[:1],
        'PREPARING',
    )

    log(
        'DISCORD TEST COMPLETE: '
        '통일 문구 + 같은 날짜/종류 회차 묶음'
    )


def counts(events):
    values = list(
        events.values()
    )

    return {
        'GV': sum(
            item.get('event_type')
            == 'GV'
            for item in values
        ),
        'STAGE': sum(
            item.get('event_type')
            == 'STAGE'
            for item in values
        ),
        'PREPARING': sum(
            item.get('booking_state')
            == 'PREPARING'
            for item in values
        ),
        'OPEN': sum(
            item.get('booking_state')
            == 'OPEN'
            for item in values
        ),
        'SOLD_OUT': sum(
            item.get('booking_state')
            == 'SOLD_OUT'
            for item in values
        ),
        'CLOSED': sum(
            item.get('booking_state')
            == 'CLOSED'
            for item in values
        ),
        'UNKNOWN': sum(
            item.get('booking_state')
            == 'UNKNOWN'
            for item in values
        ),
    }


def print_counts(
    events,
    prefix='',
):
    c = counts(
        events
    )

    if prefix:
        log(
            prefix
        )

    log(
        f'전체 GV/무대인사 회차: '
        f'{len(events)}'
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
    log('')
    log('=' * 60)
    log('전체 GV/무대인사 회차')
    log('=' * 60)

    if not events:
        log(
            'GV/무대인사 회차 없음'
        )
        return

    ordered = sorted(
        events.values(),
        key=lambda show: (
            show.get(
                'date',
                '',
            ),
            show.get(
                'time',
                '',
            ),
            show.get(
                'movie',
                '',
            ),
            show.get(
                'screen',
                '',
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
                f'  {key} = {value}'
            )

        log(
            '-' * 60
        )


def state_name_korean(status):
    return {
        'PREPARING': '상영준비중',
        'OPEN': '예매 가능',
        'SOLD_OUT': '매진',
        'CLOSED': '예매 불가/마감',
        'UNKNOWN': '미확인',
    }.get(
        status,
        norm(status)
        or '미확인',
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
                'date',
                '',
            ),
            item[1].get(
                'time',
                '',
            ),
            item[1].get(
                'movie',
                '',
            ),
            item[1].get(
                'screen',
                '',
            ),
        ),
    )

    for key, show in ordered:
        if key in state:
            continue

        kind = event_name(
            show
        )

        row = show.get(
            'raw'
        ) or {}

        accompany_code = norm(
            row.get(
                'AccompanyTypeCode'
            )
        ) or '-'

        accompany_name = norm(
            row.get(
                'AccompanyTypeNameKR'
            )
            or row.get(
                'AccompanyTypeName'
            )
        ) or '-'

        log('')
        log(
            '🔎 API 선행 진단'
        )
        log(
            f'{kind}가 감지됐습니다'
        )
        log(
            f"{show.get('date')} / "
            f"{show.get('movie') or '영화명 확인 필요'} / "
            f"{show_when(show)} / "
            f"{show.get('screen') or '상영관 정보 없음'}"
        )
        log(
            f"상태: "
            f"{state_name_korean(show.get('booking_state'))} / "
            f"AccompanyTypeCode={accompany_code} / "
            f"AccompanyTypeNameKR={accompany_name}"
        )

        detected += 1

    return detected


def make_baseline(events):
    state = {
        key: record(
            show,
            show.get(
                'booking_state',
                'UNKNOWN',
            ),
        )
        for key, show in events.items()
    }

    save_state(
        state
    )

    BASELINE_FILE.write_text(
        now_kst().isoformat(
            timespec='seconds'
        ),
        encoding='utf-8',
    )

    log('')
    log('=' * 60)
    log('INITIAL BASELINE')
    log('=' * 60)

    log(
        '현재 GV / 무대인사 회차를 '
        '알림 없이 기준값으로 등록합니다.'
    )

    print_counts(
        events
    )

    log(
        f'BASELINE EVENT COUNT: '
        f'{len(events)}'
    )

    log(
        f'STATE SAVED: '
        f'{len(state)}'
    )

    log(
        'BASELINE COMPLETE'
    )

    log(
        '이번 실행에서는 실제 회차 '
        'Discord 알림을 보내지 않았습니다.'
    )

    log(
        '기준값 상세 목록 출력은 '
        '로그 절약을 위해 생략합니다.'
    )


def process(
    events,
    state,
):
    sent = 0
    unknown_logged = 0
    pending_alerts = []

    for key, show in events.items():
        current = show.get(
            'booking_state',
            'UNKNOWN',
        )

        previous_record = (
            state.get(key)
            or {}
        )

        previous = (
            previous_record.get(
                'status'
            )
        )

        if current == 'UNKNOWN':
            previous_raw = (
                previous_record.get(
                    'raw_booking_code'
                )
            )

            current_raw = show.get(
                'raw_booking_code'
            )

            should_diagnose = (
                previous is None
                or previous != 'UNKNOWN'
                or previous_raw != current_raw
            )

            if should_diagnose:
                log('')
                log(
                    '⚠️ 예매 상태 진단'
                )

                log(
                    f'{event_name(show)}가 감지됐습니다'
                )

                log(
                    f"{show.get('date')} / "
                    f"{show.get('movie') or '영화명 확인 필요'} / "
                    f"{show_when(show)} / "
                    f"{show.get('screen') or '상영관 정보 없음'}"
                )

                log(
                    f'아직 정의하지 않은 예매 상태: '
                    f"{show.get('booking_state_source')}"
                )

                fields = diagnostic_fields(
                    show
                )

                for (
                    field_key,
                    field_value,
                ) in fields.items():
                    log(
                        f'  {field_key} = '
                        f'{field_value}'
                    )

                unknown_logged += 1

            state[key] = record(
                show,
                'UNKNOWN',
            )

            save_state(
                state
            )

            continue

        if current in {
            'SOLD_OUT',
            'CLOSED',
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

        if current == 'PREPARING':
            if previous in (
                None,
                'UNKNOWN',
                'CLOSED',
                'SOLD_OUT',
            ):
                alert_status = (
                    'PREPARING'
                )

            elif previous == 'OPEN':
                state[key] = record(
                    show,
                    'OPEN',
                )

                save_state(
                    state
                )

                continue

        elif current == 'OPEN':
            if previous in (
                None,
                'UNKNOWN',
                'PREPARING',
                'CLOSED',
            ):
                alert_status = (
                    'OPEN'
                )

            elif previous == 'SOLD_OUT':
                state[key] = record(
                    show,
                    'OPEN',
                )

                save_state(
                    state
                )

                continue

        if alert_status:
            pending_alerts.append(
                (
                    key,
                    show,
                    alert_status,
                    previous,
                )
            )

            continue

        state[key] = record(
            show,
            previous or current,
        )

        save_state(
            state
        )

    grouped = {}

    for item in pending_alerts:
        (
            key,
            show,
            alert_status,
            previous,
        ) = item

        group_key = (
            alert_status,
            show.get(
                'event_type'
            ),
            show.get(
                'date'
            ),
        )

        grouped.setdefault(
            group_key,
            [],
        ).append(
            item
        )

    for (
        group_key,
        items,
    ) in grouped.items():
        (
            alert_status,
            _,
            _,
        ) = group_key

        shows = [
            item[1]
            for item in items
        ]

        try:
            send_alert_group(
                shows,
                alert_status,
            )

        except Exception as error:
            first = shows[0]

            log(
                f'DISCORD ERROR: '
                f'{event_name(first)} / '
                f"{first.get('date')} / "
                f'{len(shows)}개 회차 / '
                f'{error}'
            )

            continue

        for (
            key,
            show,
            _,
            previous,
        ) in items:
            stored_status = (
                'OPEN'
                if alert_status
                == 'OPEN'
                else 'PREPARING'
            )

            state[key] = record(
                show,
                stored_status,
            )

            sent += 1

            if alert_status == 'PREPARING':
                transition = (
                    '상영준비중'
                )

                icon = '⏳'

            elif previous == 'PREPARING':
                transition = (
                    '상영준비중 -> 예매 오픈'
                )

                icon = '🚨'

            else:
                transition = (
                    '예매 오픈'
                )

                icon = '🚨'

            log(
                f'{icon} {transition}: '
                f'{event_name(show)} / '
                f"{show.get('movie')} / "
                f"{show.get('date')} "
                f"{show.get('time')}"
            )

        save_state(
            state
        )

    return (
        sent,
        unknown_logged,
    )


def main():
    log('=' * 60)
    log(
        'LOTTE CINEMA GV/STAGE MONITOR - WORLDTOWER 1016'
    )
    log('=' * 60)

    log(
        '대상: GV / 무대인사'
    )

    log(
        '이벤트 코드: '
        'AccompanyTypeCode 30=무대인사, 40=GV'
    )

    log(
        '예매 상태: '
        'Y=예매 가능 / E=매진 / N=예매 불가·마감'
    )

    log(
        '상영준비중: '
        'API의 상영준비중/예매준비중 문구를 감지'
    )

    log(
        '선행 진단: '
        'API에서 GV/무대인사 신호가 처음 보이면 '
        'Actions 로그에 표시'
    )

    log(
        '취소표 감지: 사용 안 함 '
        '(특정 회차 전용 알리미로 분리)'
    )

    log(
        '감시 범위: '
        '오늘 ~ +49일 (50일 전체)'
    )

    log(
        f'목표 감시 주기: '
        f'{SCAN_INTERVAL:.0f}초 '
        f'(50일 전체 순차 조회)'
    )

    log(
        f'RUN SECONDS: '
        f'{RUN_SECONDS}'
    )

    log(
        f'상태 로그: '
        f'첫 50일 스캔 완료 즉시 + 이후 '
        f'{STATUS_LOG_SECONDS // 60}분마다 요약'
    )

    log(
        '5분 고정 동시점검: '
        'KST 매시 00/05/10/.../55분에 '
        '+4~+21일 18일을 '
        '18개 worker로 동시 확인'
    )

    log(
        '정상 날짜별 API 로그: '
        '생략 '
        '(오류/새 신호/상태 변화는 즉시 표시)'
    )

    log('=' * 60)

    if (
        os.getenv(
            'LOTTE_ALERT_TEST',
            '',
        ).strip()
        == '1'
    ):
        send_test()
        return

    state = load_state()

    if not BASELINE_FILE.exists():
        (
            events,
            mode,
            errors,
        ) = scan_all_50_days()

        log(
            f'FETCH MODE: {mode}'
        )

        log(
            f'SCAN ERRORS: {errors}'
        )

        make_baseline(
            events
        )

        return

    started = time.time()
    cycle = 0

    heartbeat_started = (
        started
    )

    heartbeat_cycles = 0
    heartbeat_errors = 0
    heartbeat_alerts = 0

    fixed_now = now_kst()

    last_fixed_scan_slot = (
        fixed_now.strftime(
            '%Y%m%d%H'
        ),
        fixed_now.minute
        // FIXED_SAFETY_SCAN_MINUTES,
    )

    while (
        time.time() - started
        < RUN_SECONDS
    ):
        cycle += 1
        cycle_started = time.time()

        try:
            (
                cycle_events,
                mode,
                errors,
            ) = scan_all_50_days()

            if errors:
                log(
                    f'⚠️ CYCLE #{cycle} '
                    f'50일 조회 오류: '
                    f'{errors}일 / MODE={mode}'
                )

            log_new_event_diagnostics(
                cycle_events,
                state,
            )

            (
                sent,
                unknown_logged,
            ) = process(
                cycle_events,
                state,
            )

            save_state(
                state
            )

            heartbeat_cycles += 1
            heartbeat_errors += errors
            heartbeat_alerts += sent

            elapsed = (
                time.time()
                - cycle_started
            )

            c = counts(
                cycle_events
            )

            if cycle == 1:
                log(
                    f'✅ 첫 50일 스캔 완료 | '
                    f'CYCLE #1 | '
                    f'{elapsed:.1f}초 | '
                    f"GV {c['GV']} | "
                    f"무대인사 {c['STAGE']} | "
                    f'오류 {errors} | '
                    f'알림 {sent}'
                )

            heartbeat_elapsed = (
                time.time()
                - heartbeat_started
            )

            if (
                heartbeat_elapsed
                >= STATUS_LOG_SECONDS
            ):
                minutes = max(
                    1,
                    round(
                        heartbeat_elapsed
                        / 60
                    ),
                )

                log(
                    f'💚 정상 감시중 | '
                    f'최근 {minutes}분 '
                    f'{heartbeat_cycles}사이클 완료 | '
                    f'누적 CYCLE #{cycle} | '
                    f'50일 조회 | '
                    f"GV {c['GV']} | "
                    f"무대인사 {c['STAGE']} | "
                    f"상영준비중 {c['PREPARING']} | "
                    f"예매가능 {c['OPEN']} | "
                    f"매진 {c['SOLD_OUT']} | "
                    f'오류 {heartbeat_errors} | '
                    f'알림 {heartbeat_alerts} | '
                    f"{now_kst().strftime('%H:%M:%S KST')}"
                )

                heartbeat_started = (
                    time.time()
                )

                heartbeat_cycles = 0
                heartbeat_errors = 0
                heartbeat_alerts = 0

            fixed_now = now_kst()

            fixed_scan_slot = (
                fixed_now.strftime(
                    '%Y%m%d%H'
                ),
                fixed_now.minute
                // FIXED_SAFETY_SCAN_MINUTES,
            )

            if (
                fixed_scan_slot
                != last_fixed_scan_slot
            ):
                last_fixed_scan_slot = (
                    fixed_scan_slot
                )

                fixed_started = (
                    time.time()
                )

                try:
                    (
                        fixed_events,
                        fixed_mode,
                        fixed_errors,
                    ) = (
                        scan_fixed_18_days_concurrent()
                    )

                    if fixed_errors:
                        log(
                            f"⚠️ {fixed_now.strftime('%H:%M')} "
                            f'5분 고정 18일 동시점검 오류 | '
                            f'{fixed_errors}일 / '
                            f'MODE={fixed_mode}'
                        )

                    log_new_event_diagnostics(
                        fixed_events,
                        state,
                    )

                    (
                        fixed_sent,
                        fixed_unknown,
                    ) = process(
                        fixed_events,
                        state,
                    )

                    save_state(
                        state
                    )

                    fixed_elapsed = (
                        time.time()
                        - fixed_started
                    )

                    fixed_counts = counts(
                        fixed_events
                    )

                    heartbeat_errors += (
                        fixed_errors
                    )

                    heartbeat_alerts += (
                        fixed_sent
                    )

                    if fixed_errors:
                        fixed_icon = '⚠️'

                    else:
                        fixed_icon = '🔎'

                    log(
                        f'{fixed_icon} '
                        f"{fixed_now.strftime('%H:%M')} "
                        f'5분 고정 18일 동시점검 완료 | '
                        f'+4~+21일 | '
                        f"GV {fixed_counts['GV']} | "
                        f"무대인사 {fixed_counts['STAGE']} | "
                        f"상영준비중 {fixed_counts['PREPARING']} | "
                        f"예매가능 {fixed_counts['OPEN']} | "
                        f"매진 {fixed_counts['SOLD_OUT']} | "
                        f'오류 {fixed_errors} | '
                        f'알림 {fixed_sent} | '
                        f'{fixed_elapsed:.1f}초'
                    )

                except Exception as fixed_error:
                    heartbeat_errors += 1

                    log(
                        f"⚠️ {fixed_now.strftime('%H:%M')} "
                        f'5분 고정 18일 동시점검 실패 | '
                        f'{type(fixed_error).__name__}: '
                        f'{fixed_error}'
                    )

        except Exception as error:
            heartbeat_errors += 1

            log(
                f'SCAN/PROCESS ERROR: '
                f'{type(error).__name__}: '
                f'{error}'
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

        if wait > 0:
            time.sleep(
                wait
            )

    log('')
    log('=' * 60)
    log('RUN COMPLETE')
    log('=' * 60)


if __name__ == '__main__':
    main()
