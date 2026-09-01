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

        # Discord 링크 미리보기 제거
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


def pretty_alert_date(value):
    text = norm(value)

    for fmt in (
        "%Y-%m-%d",
        "%Y%m%d",
    ):
        try:
            dt = datetime.strptime(
                text,
                fmt,
            )

            weekdays = [
                "월",
                "화",
                "수",
                "목",
                "금",
                "토",
                "일",
            ]

            return (
                f"{dt.year}.{dt.month}.{dt.day}"
                f"({weekdays[dt.weekday()]})"
            )

        except ValueError:
            pass

    return text


def alert_time_range(show):
    start = norm(
        show.get("time")
    )

    end = norm(
        show.get("end_time")
    )

    if start and end:
        return f"{start}–{end}"

    return start or end or "시간 정보 없음"


def alert_line(show):
    text = (
        f"🎟 {alert_time_range(show)} · "
        f"{show.get('movie') or '영화명 확인 필요'} · "
        f"{show.get('screen') or '상영관 정보 없음'}"
    )

    # 긴 URL은 본문에 그대로 노출하지 않고
    # 회차 문구 자체를 클릭 가능한 예매 링크로 만든다.
    return f"**[{text}]({booking_url(show)})**"


def send_alert_group(
    shows,
    status,
):
    if not shows:
        return

    shows = sorted(
        shows,
        key=lambda item: (
            norm(item.get("time")),
            norm(item.get("movie")),
            norm(item.get("screen")),
        ),
    )

    first = shows[0]
    kind = event_name(first)

    if status == "PREPARING":
        detected = (
            f"⏳ {kind} 상영준비중이 감지됐습니다"
        )

    else:
        detected = (
            f"🔎 {kind}가 감지됐습니다"
        )

    header = [
        f"<@{DISCORD_MENTION_ID}>",
        f"**{detected}**",
        f"**🎬 {SITE_NAME} · {kind}**",
        f"**📅 {pretty_alert_date(first.get('date'))}**",
    ]

    # 같은 날짜 + 같은 종류 + 같은 상태의 회차는
    # 한 Discord 메시지로 묶는다.
    #
    # Discord 2000자 제한을 넘을 정도로 많을 경우에만
    # 같은 제목으로 자동 분할한다.
    current = list(header)

    for show in shows:
        line = alert_line(show)

        candidate = "\n".join(
            current + [line]
        )

        if (
            len(candidate) > 1900
            and len(current) > len(header)
        ):
            discord_post(
                "\n".join(current)
            )

            current = list(header)

        current.append(line)

    if len(current) > len(header):
        discord_post(
            "\n".join(current)
        )
