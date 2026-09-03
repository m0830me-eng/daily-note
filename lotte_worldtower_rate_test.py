#!/usr/bin/env python3
                        log(
                            f"⚠️ {fixed_now.strftime('%H:%M')} 5분 고정 18일 동시점검 오류 | "
                            f"{fixed_errors}일 / MODE={fixed_mode}"
                        )

                    log_new_event_diagnostics(
                        fixed_events,
                        state,
                    )

                    fixed_sent, fixed_unknown = process(
                        fixed_events,
                        state,
                    )
                    save_state(state)

                    fixed_elapsed = time.time() - fixed_started
                    fixed_counts = counts(fixed_events)

                    heartbeat_errors += fixed_errors
                    heartbeat_alerts += fixed_sent

                    if fixed_errors:
                        fixed_icon = "⚠️"
                    else:
                        fixed_icon = "🔎"

                    log(
                        f"{fixed_icon} {fixed_now.strftime('%H:%M')} "
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

        except Exception as error:
            heartbeat_errors += 1
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

        if wait > 0:
            time.sleep(wait)

    log("")
    log("=" * 60)
    log("RUN COMPLETE")
    log("=" * 60)


if __name__ == "__main__":
    main()
