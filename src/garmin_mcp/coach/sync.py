"""Pull Garmin Connect data into the local SQLite store.

Designed to be called often (every ~15 min) without hammering Garmin:
- each run refreshes today and yesterday (5 calls/day),
- older missing days are back-filled a few at a time,
- activities are fetched with one range call,
- a rate-limit response pauses syncing for 30 minutes.

CLI:  uv run garmin-coach-sync [--backfill-days 60]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import threading
import time
from typing import Any, Callable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from garmin_mcp.coach import store

BACKFILL_DAYS = 60          # health history kept in sync
ACTIVITY_HISTORY_DAYS = 180  # enough for a stable 42-day CTL
MAX_BACKFILL_PER_RUN = 10    # old days filled per run (5 calls each)
CALL_PAUSE_S = 0.35
COOLDOWN_MIN = 30

SPORT_MAP = {
    "swim": ("swim",),
    "bike": ("cycl", "bik", "ride", "virtual_ride", "spinning"),
    "run": ("run",),
    "strength": ("strength", "hiit", "yoga", "pilates", "fitness_equipment"),
}

_lock = threading.Lock()


class SyncError(RuntimeError):
    pass


def classify_sport(type_key: str | None) -> str:
    key = (type_key or "").lower()
    if key in ("multi_sport", "triathlon", "duathlon"):
        return "multi"
    if key.startswith("transition"):
        return "other"
    for sport, needles in SPORT_MAP.items():
        if any(n in key for n in needles):
            return sport
    return "other"


def _d(obj: Any) -> dict:
    return obj if isinstance(obj, dict) else {}


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def login() -> Garmin:
    from garmin_mcp.token_utils import get_token_path

    tokenstore = get_token_path()
    try:
        g = Garmin()
        g.login(tokenstore)
        return g
    except FileNotFoundError as exc:
        raise SyncError(
            f"Brak tokenów Garmin w {tokenstore}. Uruchom najpierw: uv run garmin-mcp-auth"
        ) from exc
    except GarminConnectAuthenticationError as exc:
        raise SyncError(
            "Tokeny Garmin wygasły lub są nieprawidłowe. Uruchom: uv run garmin-mcp-auth"
        ) from exc


# --- extraction --------------------------------------------------------------

def _pick_device_record(latest: dict) -> dict:
    records = [v for v in _d(latest).values() if isinstance(v, dict) and v]
    for r in records:
        if r.get("primaryTrainingDevice") is True:
            return r
    return records[0] if records else {}


def extract_day(g: Garmin, day: str, call: Callable) -> dict:
    out: dict[str, Any] = {}

    s = _d(call(g.get_user_summary, day))
    out.update(
        steps=_num(s.get("totalSteps")),
        rhr=_num(s.get("restingHeartRate")),
        stress_avg=_num(s.get("averageStressLevel")) if (s.get("averageStressLevel") or -1) >= 0 else None,
        bb_high=_num(s.get("bodyBatteryHighestValue")),
        bb_low=_num(s.get("bodyBatteryLowestValue")),
        bb_now=_num(s.get("bodyBatteryMostRecentValue")),
    )

    sleep = _d(_d(call(g.get_sleep_data, day)).get("dailySleepDTO"))
    if sleep.get("sleepTimeSeconds"):
        out.update(
            sleep_s=_num(sleep.get("sleepTimeSeconds")),
            sleep_score=_num(_d(_d(sleep.get("sleepScores")).get("overall")).get("value")),
            deep_s=_num(sleep.get("deepSleepSeconds")),
            rem_s=_num(sleep.get("remSleepSeconds")),
        )

    hrv = _d(_d(call(g.get_hrv_data, day)).get("hrvSummary"))
    if hrv:
        base = _d(hrv.get("baseline"))
        out.update(
            hrv_last=_num(hrv.get("lastNightAvg")),
            hrv_weekly=_num(hrv.get("weeklyAvg")),
            hrv_low=_num(base.get("balancedLow")),
            hrv_high=_num(base.get("balancedUpper")),
            hrv_status=hrv.get("status"),
        )

    tr = call(g.get_training_readiness, day)
    snaps = tr if isinstance(tr, list) else ([tr] if isinstance(tr, dict) and tr else [])
    snaps = [x for x in snaps if isinstance(x, dict) and x.get("score") is not None]
    if snaps:
        morning = [x for x in snaps if x.get("inputContext") == "AFTER_WAKEUP_RESET"]
        pick = (morning or sorted(snaps, key=lambda x: str(x.get("timestampLocal") or "")))[-1]
        out.update(readiness=_num(pick.get("score")), readiness_level=pick.get("level"))

    ts = _d(call(g.get_training_status, day))
    rec = _pick_device_record(_d(_d(ts.get("mostRecentTrainingStatus")).get("latestTrainingStatusData")))
    if rec:
        acwr = _d(rec.get("acuteTrainingLoadDTO"))
        out.update(
            acute_load=_num(acwr.get("dailyTrainingLoadAcute")),
            chronic_load=_num(acwr.get("dailyTrainingLoadChronic")),
            acwr=_num(acwr.get("dailyAcuteChronicWorkloadRatio")),
            training_status=rec.get("trainingStatusFeedbackPhrase"),
        )
    vo2 = _d(ts.get("mostRecentVO2Max"))
    out.update(
        vo2max_run=_num(_d(vo2.get("generic")).get("vo2MaxPreciseValue")),
        vo2max_bike=_num(_d(vo2.get("cycling")).get("vo2MaxPreciseValue")),
    )
    return {k: v for k, v in out.items() if v is not None}


def extract_activity(a: dict) -> tuple[int, dict] | None:
    aid = a.get("activityId")
    start = a.get("startTimeLocal") or ""
    if not aid or not start:
        return None
    type_key = _d(a.get("activityType")).get("typeKey")
    return int(aid), {
        "start": start,
        "date": start[:10],
        "sport": classify_sport(type_key),
        "type_key": type_key,
        "name": a.get("activityName"),
        "duration_s": _num(a.get("movingDuration")) or _num(a.get("duration")),
        "distance_m": _num(a.get("distance")),
        "avg_hr": _num(a.get("averageHR")),
        "max_hr": _num(a.get("maxHR")),
        "avg_speed": _num(a.get("averageSpeed")),
        "avg_power": _num(a.get("avgPower")),
        "norm_power": _num(a.get("normPower")),
        "tss": _num(a.get("trainingStressScore")),
        "garmin_load": _num(a.get("activityTrainingLoad")),
        "aerobic_te": _num(a.get("aerobicTrainingEffect")),
        "anaerobic_te": _num(a.get("anaerobicTrainingEffect")),
        "elevation_gain": _num(a.get("elevationGain")),
        "calories": _num(a.get("calories")),
    }


# --- orchestration -----------------------------------------------------------

def _now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def sync(backfill_days: int = BACKFILL_DAYS, log: Callable[[str], None] = print) -> dict:
    """Run one incremental sync. Returns a short status dict."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "message": "Synchronizacja już trwa."}
    conn = store.connect()
    try:
        cooldown = store.get_meta(conn, "cooldown_until")
        if cooldown and dt.datetime.fromisoformat(cooldown) > _now():
            msg = f"Garmin ograniczył zapytania — przerwa do {cooldown[11:16]}."
            return {"ok": False, "message": msg}

        store.set_meta(conn, "sync_started", _now().isoformat())
        conn.commit()
        g = login()
        calls = {"n": 0}

        def call(fn, *args):
            calls["n"] += 1
            time.sleep(CALL_PAUSE_S)
            try:
                return fn(*args)
            except GarminConnectTooManyRequestsError:
                raise
            except Exception as exc:  # one endpoint failing must not kill the sync
                log(f"  ! {fn.__name__}({', '.join(map(str, args))}): {exc}")
                return None

        today = dt.date.today()

        # 1) activities — one range call
        last_act = conn.execute("SELECT MAX(date) AS d FROM activities").fetchone()["d"]
        start = (
            dt.date.fromisoformat(last_act) - dt.timedelta(days=3)
            if last_act
            else today - dt.timedelta(days=ACTIVITY_HISTORY_DAYS)
        )
        acts = call(g.get_activities_by_date, start.isoformat(), today.isoformat()) or []
        n_acts = 0
        for a in acts:
            parsed = extract_activity(a) if isinstance(a, dict) else None
            if parsed:
                store.upsert_activity(conn, parsed[0], parsed[1], a)
                n_acts += 1
        conn.commit()

        # 2) health — today, yesterday, then a few missing older days
        have = {r["date"] for r in conn.execute("SELECT date FROM daily")}
        days = [today, today - dt.timedelta(days=1)]
        missing = [
            today - dt.timedelta(days=i)
            for i in range(2, backfill_days)
            if (today - dt.timedelta(days=i)).isoformat() not in have
        ]
        days += missing[:MAX_BACKFILL_PER_RUN]
        for d in days:
            values = extract_day(g, d.isoformat(), call)
            store.upsert_daily(conn, d.isoformat(), values, _now().isoformat())
            conn.commit()

        left = max(0, len(missing) - MAX_BACKFILL_PER_RUN)
        msg = f"OK: {n_acts} aktywności, {len(days)} dni zdrowia, {calls['n']} zapytań"
        if left:
            msg += f" (do uzupełnienia jeszcze {left} dni historii)"
        store.set_meta(conn, "last_sync", _now().isoformat())
        store.set_meta(conn, "last_sync_message", msg)
        store.set_meta(conn, "last_error", None)
        conn.commit()
        log(msg)
        return {"ok": True, "message": msg, "backfill_left": left}

    except GarminConnectTooManyRequestsError:
        until = (_now() + dt.timedelta(minutes=COOLDOWN_MIN)).isoformat()
        store.set_meta(conn, "cooldown_until", until)
        msg = f"Garmin zwrócił limit zapytań (429). Pauza do {until[11:16]}."
        store.set_meta(conn, "last_error", msg)
        conn.commit()
        log(msg)
        return {"ok": False, "message": msg}
    except SyncError as exc:
        store.set_meta(conn, "last_error", str(exc))
        conn.commit()
        log(str(exc))
        return {"ok": False, "message": str(exc)}
    except Exception as exc:
        msg = f"Błąd synchronizacji: {exc}"
        store.set_meta(conn, "last_error", msg)
        conn.commit()
        log(msg)
        return {"ok": False, "message": msg}
    finally:
        conn.close()
        _lock.release()


def is_running() -> bool:
    return _lock.locked()


# --- single-activity detail (fetched on demand, cached) ----------------------

_client: Garmin | None = None
_client_lock = threading.Lock()
_LAP_SKIP = ("lat", "lon", "Id", "uuid", "GMT", "Timestamp", "messageIndex", "bmr")


def _cached_client() -> Garmin:
    global _client
    with _client_lock:
        if _client is None:
            _client = login()
        return _client


def _trim_lap(lap: dict) -> dict:
    out = {}
    for k, v in lap.items():
        if isinstance(v, (int, float, str, bool)) and not any(s in k for s in _LAP_SKIP):
            out[k] = round(v, 2) if isinstance(v, float) else v
    lengths = lap.get("lengthDTOs") or []
    if lengths:
        out["lengths"] = [
            {k: (round(v, 2) if isinstance(v, float) else v) for k, v in ln.items()
             if k in ("lengthIndex", "duration", "averageSpeed", "totalNumberOfStrokes",
                      "averageSWOLF", "swimStroke", "averageHR")}
            for ln in lengths[:80]
        ]
    return out


def _zones(payload: Any) -> list[dict]:
    items = payload if isinstance(payload, list) else (_d(payload).get("zones") or [])
    return [
        {"zone": z.get("zoneNumber"), "secs": round(z.get("secsInZone") or 0),
         "low": z.get("zoneLowBoundary")}
        for z in items if isinstance(z, dict)
    ]


def fetch_activity_detail(activity_id: int, refresh: bool = False) -> dict:
    """Laps, HR/power zones and summary extras for one activity (cached in SQLite)."""
    global _client
    conn = store.connect()
    try:
        row = conn.execute("SELECT data FROM activity_details WHERE id=?", (activity_id,)).fetchone()
        if row and not refresh:
            return json.loads(row["data"])
    finally:
        conn.close()

    g = _cached_client()
    detail: dict[str, Any] = {"activity_id": activity_id}
    try:
        summary = _d(_d(g.get_activity(activity_id)).get("summaryDTO"))
        keep = ("trainingEffectLabel", "aerobicTrainingEffectMessage", "anaerobicTrainingEffectMessage",
                "averageRunCadence", "maxRunCadence", "strideLength", "groundContactTime",
                "verticalOscillation", "verticalRatio", "normalizedPower", "intensityFactor",
                "trainingStressScore", "averageBikingCadenceInRevPerMinute", "averageSwolf",
                "averageSwimCadenceInStrokesPerMinute", "poolLength", "elevationGain",
                "averageTemperature", "directWorkoutFeel", "directWorkoutRpe", "recoveryHeartRate",
                "minActivityLapDuration", "moderateIntensityMinutes", "vigorousIntensityMinutes")
        detail["summary"] = {k: summary[k] for k in keep if summary.get(k) is not None}
    except Exception as exc:
        detail["summary_error"] = str(exc)
    try:
        laps = _d(g.get_activity_splits(activity_id)).get("lapDTOs") or []
        detail["laps"] = [_trim_lap(l) for l in laps[:60] if isinstance(l, dict)]
    except Exception as exc:
        detail["laps_error"] = str(exc)
    try:
        detail["hr_zones"] = _zones(g.get_activity_hr_in_timezones(activity_id))
    except Exception as exc:
        detail["hr_zones_error"] = str(exc)
    try:
        pz = _zones(g.get_activity_power_in_timezones(activity_id))
        if any(z["secs"] for z in pz):
            detail["power_zones"] = pz
    except Exception:
        pass

    if "summary_error" in detail and "laps_error" in detail:
        _client = None  # tokens may have expired; log in again next time
        return detail
    conn = store.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO activity_details (id, fetched_at, data) VALUES (?, ?, ?)",
            (activity_id, _now().isoformat(), json.dumps(detail)),
        )
        conn.commit()
    finally:
        conn.close()
    return detail


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronizuj dane Garmin do lokalnej bazy.")
    parser.add_argument("--backfill-days", type=int, default=BACKFILL_DAYS)
    parser.add_argument(
        "--all", action="store_true",
        help="Powtarzaj aż cała historia zdrowia zostanie uzupełniona.",
    )
    args = parser.parse_args()
    print(f"Baza: {store.db_path()}")
    while True:
        result = sync(args.backfill_days)
        if not (args.all and result.get("ok") and result.get("backfill_left")):
            break
        time.sleep(5)
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
