"""Progress / regression per discipline, and how one session compares to similar ones.

Fitness is read from efficiency, not raw speed, so easy and hard days compare fairly:
- run:  pace you would hold at a fixed reference heart rate (speed x ref_hr / avg_hr),
- bike: power at the reference heart rate (needs a power meter),
- swim: pace per 100 m on pool/open-water swims of 400 m+.
The scaling to a reference HR is a linear approximation — trust the trend, not the
exact number.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from garmin_mcp.coach import store

LABELS = {"run": "Bieg", "bike": "Rower", "swim": "Pływanie"}
MIN_DURATION_S = 15 * 60
THRESHOLD_PCT = 1.5


def _rows(sql: str, *args) -> list[dict]:
    conn = store.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _ref_hr(acts: list[dict], sport: str) -> float | None:
    hrs = [a["avg_hr"] for a in acts if a["sport"] == sport and a.get("avg_hr")
           and (a.get("duration_s") or 0) >= MIN_DURATION_S]
    return round(statistics.median(hrs) / 5) * 5 if len(hrs) >= 3 else None


def metric(a: dict, ref_hr: float | None) -> float | None:
    """Higher-is-better for run/bike (m/s, W); seconds per 100 m for swim (lower is better)."""
    dur, hr = a.get("duration_s") or 0, a.get("avg_hr")
    if a["sport"] == "swim":
        d = a.get("distance_m") or 0
        return round(dur / (d / 100), 1) if d >= 400 and dur else None
    if dur < MIN_DURATION_S or not hr or not ref_hr:
        return None
    if a["sport"] == "run" and a.get("avg_speed"):
        return a["avg_speed"] * ref_hr / hr
    if a["sport"] == "bike":
        p = a.get("norm_power") or a.get("avg_power")
        return p * ref_hr / hr if p else None
    return None


def _change(cur: float | None, prev: float | None, sport: str) -> tuple[float | None, str]:
    if cur is None or prev is None or not prev:
        return None, "brak danych"
    pct = (cur - prev) / prev * 100
    if sport == "swim":
        pct = -pct  # lower seconds per 100 m = faster
    pct = round(pct, 1)
    verdict = "progres" if pct >= THRESHOLD_PCT else "regres" if pct <= -THRESHOLD_PCT else "stabilnie"
    return pct, verdict


def _median(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def build_progress(weeks: int = 16) -> dict:
    today = dt.date.today()
    since = (today - dt.timedelta(days=7 * weeks + 30)).isoformat()
    acts = _rows("SELECT * FROM activities WHERE date >= ? ORDER BY start", since)
    daily = _rows("SELECT date, vo2max_run, vo2max_bike FROM daily WHERE date >= ? ORDER BY date", since)

    d28 = (today - dt.timedelta(days=28)).isoformat()
    d56 = (today - dt.timedelta(days=56)).isoformat()
    first_week = today - dt.timedelta(days=today.weekday() + 7 * (weeks - 1))

    sports = []
    for sport in ("run", "bike", "swim"):
        ref = _ref_hr(acts, sport)
        vals = [(a, metric(a, ref)) for a in acts if a["sport"] == sport]
        vals = [(a, v) for a, v in vals if v is not None]
        cur = _median([v for a, v in vals if a["date"] > d28])
        prev = _median([v for a, v in vals if d56 < a["date"] <= d28])
        pct, verdict = _change(cur, prev, sport)
        weekly = []
        for i in range(weeks):
            ws = first_week + dt.timedelta(days=7 * i)
            we = ws + dt.timedelta(days=6)
            wv = _median([v for a, v in vals if ws.isoformat() <= a["date"] <= we.isoformat()])
            weekly.append({"week": ws.isoformat(), "value": round(wv, 3) if wv else None})
        sports.append({
            "sport": sport,
            "label": LABELS[sport],
            "ref_hr": ref,
            "metric": {"run": "tempo przy stałym tętnie", "bike": "moc przy stałym tętnie",
                       "swim": "tempo na 100 m"}[sport],
            "unit": {"run": "m/s", "bike": "W", "swim": "s/100m"}[sport],
            "better": "lower" if sport == "swim" else "higher",
            "current": round(cur, 3) if cur else None,
            "previous": round(prev, 3) if prev else None,
            "change_pct": pct,
            "verdict": verdict,
            "n_current": sum(1 for a, _ in vals if a["date"] > d28),
            "n_previous": sum(1 for a, _ in vals if d56 < a["date"] <= d28),
            "weekly": weekly,
            "note": ("brak treningów z pomiarem mocy" if sport == "bike" and not vals
                     else "za mało treningów z tętnem" if not vals else None),
        })

    volume = []
    for sport in ("swim", "bike", "run"):
        cur_h = sum((a["duration_s"] or 0) for a in acts if a["sport"] == sport and a["date"] > d28) / 3600
        prev_h = sum((a["duration_s"] or 0) for a in acts if a["sport"] == sport and d56 < a["date"] <= d28) / 3600
        volume.append({"sport": sport, "label": LABELS[sport], "current_h": round(cur_h, 1), "previous_h": round(prev_h, 1)})

    vo2 = {}
    for key, col in (("run", "vo2max_run"), ("bike", "vo2max_bike")):
        series = [{"date": r["date"], "value": r[col]} for r in daily if r.get(col)]
        old = [s["value"] for s in series if s["date"] <= d56]
        vo2[key] = {
            "latest": series[-1]["value"] if series else None,
            "eight_weeks_ago": old[-1] if old else (series[0]["value"] if series else None),
            "series": series,
        }
    return {"sports": sports, "volume": volume, "vo2max": vo2}


def compare_activity(activity_id: int) -> dict[str, Any]:
    acts = _rows("SELECT * FROM activities WHERE date >= ? ORDER BY start",
                 (dt.date.today() - dt.timedelta(days=150)).isoformat())
    me = next((a for a in acts if a["id"] == activity_id), None)
    if not me:
        rows = _rows("SELECT * FROM activities WHERE id = ?", activity_id)
        if not rows:
            return {}
        me = rows[0]
        acts.append(me)
    ref = _ref_hr(acts, me["sport"])
    mine = metric(me, ref)
    dur = me.get("duration_s") or 0
    similar = [
        a for a in acts
        if a["sport"] == me["sport"] and a["id"] != me["id"] and dur
        and 0.6 <= (a.get("duration_s") or 0) / dur <= 1.6
    ]
    sim_vals = [(a, metric(a, ref)) for a in similar]
    sim_vals = [(a, v) for a, v in sim_vals if v is not None]
    med = _median([v for _, v in sim_vals])
    pct, verdict = _change(mine, med, me["sport"])
    return {
        "sport": me["sport"],
        "ref_hr": ref,
        "metric": {"run": "tempo przy stałym tętnie", "bike": "moc przy stałym tętnie",
                   "swim": "tempo na 100 m"}.get(me["sport"]),
        "this": round(mine, 3) if mine else None,
        "median_similar": round(med, 3) if med else None,
        "n_similar": len(sim_vals),
        "change_pct": pct,
        "verdict": verdict,
        "similar_recent": [
            {"id": a["id"], "date": a["date"], "name": a["name"], "duration_s": a["duration_s"],
             "distance_m": a["distance_m"], "avg_hr": a["avg_hr"], "value": round(v, 3)}
            for a, v in sim_vals[-6:]
        ],
    }
