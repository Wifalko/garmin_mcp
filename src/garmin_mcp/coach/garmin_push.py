"""Push planned swim/bike/run sessions to Garmin Connect as structured workouts.

Each upcoming session (today … +14 days) becomes a workout named
"Coach: <English name>" scheduled on its date, so it shows up on the watch.
When the coach changes or deletes a session, the old workout is removed and,
if needed, a new one is uploaded — tracked in the `garmin_workouts` table.

Session "steps" (written by the coach) use a compact schema:
  {"type": "warmup|interval|recovery|rest|cooldown",
   "duration_min": 10 | "distance_m": 400 | (neither = lap button),
   "target": {"hr_zone": 2} | {"hr_bpm": [130, 145]} | {"power_pct_ftp": [88, 92]}
             | {"power_zone": 3} | {"power_w": [200, 230]} | {"pace_km": ["5:10", "5:00"]}
             | {"pace_100m": ["1:55", "1:50"]},
   "note": "short text shown on the watch"}
  {"type": "repeat", "count": 4, "steps": [ ...steps... ]}
Without steps a session is pushed as a single step of its duration, with the
HR zone parsed from its intensity ("Z2" -> zone 2).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time
import unicodedata

from garmin_mcp.coach import store

SPORTS = {
    "run": {"sportTypeId": 1, "sportTypeKey": "running"},
    "bike": {"sportTypeId": 2, "sportTypeKey": "cycling"},
    "swim": {"sportTypeId": 4, "sportTypeKey": "lap_swimming"},
}
STEP_TYPES = {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5}
NAME_PREFIX = "Coach: "
SPORT_EN = {"run": "Run", "bike": "Bike", "swim": "Swim"}
DAYS_AHEAD = 14

_lock = threading.Lock()
_pending = threading.Event()
_worker: threading.Thread | None = None


def enabled() -> bool:
    return bool(store.read_config().get("garmin_push", False))


def set_enabled(on: bool) -> dict:
    store.write_config({"garmin_push": bool(on)})
    if on:
        schedule_sync(0.5)
    return status()


def status() -> dict:
    conn = store.connect()
    try:
        meta = {k: store.get_meta(conn, k) for k in ("gpush_last_sync", "gpush_last_message", "gpush_last_error")}
        pushed = {r["plan_id"]: dict(r) for r in conn.execute("SELECT * FROM garmin_workouts")}
    finally:
        conn.close()
    return {"enabled": enabled(), "last_sync": meta["gpush_last_sync"],
            "last_message": meta["gpush_last_message"], "last_error": meta["gpush_last_error"],
            "pushed_plan_ids": sorted(pushed)}


# --- building Garmin workout JSON ----------------------------------------------------

def _pace_to_mps(value: str, meters: float) -> float:
    m = re.match(r"^\s*(\d+):(\d{1,2})\s*$", str(value))
    if not m:
        raise ValueError(f"Zły format tempa: {value!r} (oczekiwane m:ss)")
    secs = int(m.group(1)) * 60 + int(m.group(2))
    return round(meters / secs, 3)


def _pair(v) -> tuple[float, float]:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ValueError(f"Zakres musi mieć dwie wartości: {v!r}")
    a, b = float(v[0]), float(v[1])
    return (a, b) if a <= b else (b, a)


def _target(t: dict | None, sport: str) -> dict:
    no = {"targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}}
    if not t:
        return no
    hr = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}
    if "hr_zone" in t:
        return {"targetType": hr, "zoneNumber": max(1, min(5, int(t["hr_zone"])))}
    if "hr_bpm" in t:
        lo, hi = _pair(t["hr_bpm"])
        return {"targetType": hr, "targetValueOne": lo, "targetValueTwo": hi}
    if sport == "bike" and "power_zone" in t:
        return {"targetType": {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone"},
                "zoneNumber": max(1, min(7, int(t["power_zone"])))}
    if sport == "bike" and "power_pct_ftp" in t:
        lo, hi = _pair(t["power_pct_ftp"])
        return {"targetType": {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone"},
                "targetValueOne": lo, "targetValueTwo": hi, "targetValueUnit": {"unitKey": "percent"}}
    if sport == "bike" and "power_w" in t:
        lo, hi = _pair(t["power_w"])
        return {"targetType": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "power.between"},
                "targetValueOne": lo, "targetValueTwo": hi}
    for key, meters in (("pace_km", 1000.0), ("pace_100m", 100.0)):
        if key in t and sport in ("run", "swim"):
            v = t[key]
            speeds = [_pace_to_mps(x, meters) for x in (v if isinstance(v, (list, tuple)) else [v, v])]
            lo, hi = min(speeds), max(speeds)
            return {"targetType": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"},
                    "targetValueOne": lo, "targetValueTwo": hi}
    return no


def _end(step: dict) -> dict:
    if step.get("duration_min"):
        return {"endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": float(round(float(step["duration_min"]) * 60))}
    if step.get("distance_m"):
        return {"endCondition": {"conditionTypeId": 3, "conditionTypeKey": "distance"},
                "endConditionValue": float(step["distance_m"])}
    return {"endCondition": {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}}


def _steps(steps: list, sport: str, order: list[int]) -> list[dict]:
    out = []
    for s in steps:
        order[0] += 1
        if s.get("type") == "repeat":
            my_order = order[0]
            inner = _steps(s.get("steps") or [], sport, order)
            if not inner:
                continue
            out.append({
                "type": "RepeatGroupDTO", "stepOrder": my_order,
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                "numberOfIterations": max(1, int(s.get("count") or 1)),
                "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
                "endConditionValue": float(max(1, int(s.get("count") or 1))),
                "smartRepeat": False, "workoutSteps": inner,
            })
            continue
        kind = s.get("type") if s.get("type") in STEP_TYPES else "interval"
        step = {"type": "ExecutableStepDTO", "stepOrder": order[0],
                "stepType": {"stepTypeId": STEP_TYPES[kind], "stepTypeKey": kind},
                **_end(s), **_target(s.get("target"), sport)}
        if s.get("note"):
            step["description"] = str(s["note"])[:120]
        out.append(step)
    return out


def _fallback_steps(session: dict) -> list[dict]:
    zone = re.search(r"Z\s*([1-5])", session.get("intensity") or "", re.I)
    return [{"type": "interval", "duration_min": session.get("duration_min") or None,
             "target": {"hr_zone": int(zone.group(1))} if zone else None}]


def english_name(session: dict) -> str:
    """English workout name for Garmin: the coach's name_en, else built from the session."""
    if session.get("name_en"):
        name = str(session["name_en"])
    else:
        parts = [SPORT_EN.get(session["sport"], "Workout")]
        m = session.get("duration_min")
        if m:
            m = int(round(m))
            parts.append(f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m} min")
        if session.get("intensity"):
            parts.append(str(session["intensity"]))
        name = " ".join(parts)
    # plain ASCII: some watches render Polish diacritics poorly
    name = unicodedata.normalize("NFKD", name.replace("ł", "l").replace("Ł", "L"))
    name = "".join(c for c in name if not unicodedata.combining(c)).encode("ascii", "ignore").decode()
    return (NAME_PREFIX + name.strip())[:80]


def build_workout(session: dict) -> dict:
    sport = session["sport"]
    steps = session.get("steps") or _fallback_steps(session)
    workout = {
        "workoutName": english_name(session),
        "description": (session.get("description") or "")[:1000],
        "sportType": SPORTS[sport],
        "workoutSegments": [{"segmentOrder": 1, "sportType": SPORTS[sport],
                             "workoutSteps": _steps(steps, sport, [0])}],
    }
    if sport == "swim":
        pool = float(store.read_config().get("pool_length_m") or 25)
        workout["poolLength"] = pool
        workout["poolLengthUnit"] = {"unitId": 1, "unitKey": "meter", "factor": 100.0}
    if not workout["workoutSegments"][0]["workoutSteps"]:
        raise ValueError("Sesja nie ma żadnych kroków.")
    # same preflight checks the MCP upload_workout tool uses (Garmin silently
    # misreads some id/key combinations, so catch them before upload)
    from garmin_mcp import workouts as wk
    wk._normalize_workout_steps(workout)
    wk._validate_end_condition_steps(workout)
    wk._validate_target_type_steps(workout)
    return workout


# --- sync ----------------------------------------------------------------------------

def _set_meta(**values) -> None:
    conn = store.connect()
    try:
        for k, v in values.items():
            store.set_meta(conn, k, v)
        conn.commit()
    finally:
        conn.close()


def _desired() -> dict[int, dict]:
    from garmin_mcp.coach import plan

    today = dt.date.today()
    out = {}
    for s in plan.list_plan(today, today + dt.timedelta(days=DAYS_AHEAD)):
        if s["sport"] in SPORTS and s.get("status") != "skipped" and s.get("state") in ("planned", "today"):
            out[s["id"]] = s
    return out


def _remove(g, row: dict) -> None:
    for fn, arg in ((getattr(g, "unschedule_workout", None), row.get("schedule_id")),
                    (getattr(g, "delete_workout", None), row.get("workout_id"))):
        if fn and arg:
            try:
                fn(arg)
            except Exception:  # already gone on Garmin's side
                pass


def sync_now(client=None) -> dict:
    if not enabled():
        return {"ok": False, "message": "Wysyłanie treningów do Garmina jest wyłączone."}
    from garmin_mcp.coach import sync as garmin_sync

    with _lock:
        today = dt.date.today().isoformat()
        conn = store.connect()
        try:
            rows = {r["plan_id"]: dict(r) for r in conn.execute("SELECT * FROM garmin_workouts")}
        finally:
            conn.close()
        try:
            g = client or garmin_sync._cached_client()
            want = _desired()
            added = updated = removed = 0
            errors = []
            plan_exists = set()
            conn = store.connect()
            try:
                plan_exists = {r[0] for r in conn.execute("SELECT id FROM plan")}
            finally:
                conn.close()

            # remove workouts whose session was deleted / skipped / no longer pushable
            for pid, row in rows.items():
                if pid in want:
                    continue
                if pid in plan_exists and row["date"] and row["date"] < today:
                    continue  # past session: keep the workout as history
                _remove(g, row)
                _forget(pid)
                removed += 1

            for pid, s in want.items():
                try:
                    workout = build_workout(s)
                except (ValueError, TypeError) as exc:
                    errors.append(f"{s['date']} {s.get('title')}: {exc}")
                    continue
                digest = hashlib.sha1(json.dumps([workout, s["date"]], sort_keys=True,
                                                 ensure_ascii=False).encode()).hexdigest()[:16]
                row = rows.get(pid)
                if row and row["hash"] == digest:
                    continue
                if row:
                    _remove(g, row)
                try:
                    res = g.upload_workout(workout) or {}
                    wid = res.get("workoutId")
                    if not wid:
                        raise RuntimeError(f"Garmin nie zwrócił workoutId: {str(res)[:200]}")
                    sched = g.schedule_workout(wid, s["date"]) or {}
                    sid = (sched.get("workoutScheduleId") or sched.get("scheduleId")
                           or sched.get("id")) if isinstance(sched, dict) else None
                except Exception as exc:
                    errors.append(f"{s['date']} {s.get('title')}: {exc}")
                    if row:
                        _forget(pid)
                    continue
                _remember(pid, wid, sid, s["date"], digest)
                if row:
                    updated += 1
                else:
                    added += 1

            msg = f"Garmin Connect: +{added}, zmienione {updated}, usunięte {removed}."
            if errors:
                msg += f" Błędy: {len(errors)}."
            _set_meta(gpush_last_sync=dt.datetime.now().replace(microsecond=0).isoformat(),
                      gpush_last_message=msg, gpush_last_error="; ".join(errors)[:1000] or None)
            return {"ok": not errors, "added": added, "updated": updated, "removed": removed,
                    "errors": errors, "message": msg}
        except Exception as exc:
            _set_meta(gpush_last_error=f"Wysyłanie do Garmina: {exc}")
            return {"ok": False, "message": str(exc)}


def _remember(pid, wid, sid, date, digest) -> None:
    conn = store.connect()
    try:
        conn.execute("INSERT OR REPLACE INTO garmin_workouts VALUES (?, ?, ?, ?, ?, ?)",
                     (pid, wid, sid, date, digest, dt.datetime.now().replace(microsecond=0).isoformat()))
        conn.commit()
    finally:
        conn.close()


def _forget(pid) -> None:
    conn = store.connect()
    try:
        conn.execute("DELETE FROM garmin_workouts WHERE plan_id=?", (pid,))
        conn.commit()
    finally:
        conn.close()


def schedule_sync(delay: float = 4.0) -> None:
    """Debounced background push after plan edits."""
    global _worker
    if not enabled():
        return
    _pending.set()
    if _worker and _worker.is_alive():
        return

    def run():
        while _pending.is_set():
            _pending.clear()
            time.sleep(delay)
            if not _pending.is_set():
                sync_now()

    _worker = threading.Thread(target=run, name="garmin-push", daemon=True)
    _worker.start()
