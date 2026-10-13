"""Training metrics and rule-based coaching signals built from the local store.

Load per activity: Garmin's own training load (EPOC) when present; otherwise
TSS from a power meter; otherwise an hrTSS estimate; otherwise duration.
CTL (fitness) = 42-day EWMA, ATL (fatigue) = 7-day EWMA, TSB (form) = CTL-ATL
of the previous day. The scale follows Garmin's load, not TrainingPeaks TSS,
so read trends and ratios rather than absolute TSB thresholds.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from garmin_mcp.coach import store

SPORTS = ("swim", "bike", "run")
SPORT_PL = {"swim": "pływanie", "bike": "rower", "run": "bieg", "strength": "siła", "multi": "multisport", "other": "inne"}


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def activity_load(a: dict, lthr: float) -> tuple[float, str]:
    hours = (a.get("duration_s") or 0) / 3600
    if a.get("garmin_load"):
        return float(a["garmin_load"]), "garmin"
    if a.get("tss"):
        return float(a["tss"]), "tss"
    if a.get("avg_hr") and lthr:
        return round(hours * (a["avg_hr"] / lthr) ** 2 * 100, 1), "hr"
    return round(hours * 50, 1), "czas"


def _ewma(prev: float, value: float, days: int) -> float:
    return prev + (value - prev) / days


def _rows(conn, sql, *args) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]


def build_snapshot(days: int = 90) -> dict:
    conn = store.connect()
    try:
        acts = _rows(conn, "SELECT * FROM activities ORDER BY start")
        daily = _rows(conn, "SELECT * FROM daily ORDER BY date")
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    finally:
        conn.close()

    today = dt.date.today()
    max_hrs = [a["max_hr"] for a in acts if a.get("max_hr")]
    lthr = round(0.89 * max(max_hrs)) if max_hrs else 165

    for a in acts:
        a.pop("raw", None)
        a["load"], a["load_source"] = activity_load(a, lthr)

    # --- PMC ---------------------------------------------------------------
    by_day: dict[str, dict[str, float]] = {}
    for a in acts:
        d = by_day.setdefault(a["date"], {})
        d["total"] = d.get("total", 0) + a["load"]
        d[a["sport"]] = d.get(a["sport"], 0) + a["load"]

    pmc: list[dict] = []
    if acts:
        start = dt.date.fromisoformat(acts[0]["date"])
        ctl = atl = 0.0
        sport_ctl = {s: 0.0 for s in SPORTS}
        d = start
        while d <= today:
            key = d.isoformat()
            loads = by_day.get(key, {})
            tsb = ctl - atl
            ctl = _ewma(ctl, loads.get("total", 0), 42)
            atl = _ewma(atl, loads.get("total", 0), 7)
            for s in SPORTS:
                sport_ctl[s] = _ewma(sport_ctl[s], loads.get(s, 0), 42)
            pmc.append({
                "date": key, "load": round(loads.get("total", 0), 1),
                "ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(tsb, 1),
                **{f"ctl_{s}": round(sport_ctl[s], 1) for s in SPORTS},
            })
            d += dt.timedelta(days=1)

    # --- weekly volume -----------------------------------------------------
    weeks: dict[str, dict] = {}
    for a in acts:
        d = dt.date.fromisoformat(a["date"])
        wk = (d - dt.timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(wk, {"week": wk, "load": 0.0})
        s = a["sport"] if a["sport"] in (*SPORTS, "strength") else "other"
        w[f"{s}_h"] = round(w.get(f"{s}_h", 0) + (a["duration_s"] or 0) / 3600, 2)
        w[f"{s}_km"] = round(w.get(f"{s}_km", 0) + (a["distance_m"] or 0) / 1000, 2)
        w["load"] = round(w["load"] + a["load"], 1)
    first_week = today - dt.timedelta(days=today.weekday() + 7 * 15)
    weekly = []
    for i in range(16):
        wk = (first_week + dt.timedelta(days=7 * i)).isoformat()
        weekly.append(weeks.get(wk, {"week": wk, "load": 0.0}))

    cutoff = (today - dt.timedelta(days=days)).isoformat()
    daily_recent = [r for r in daily if r["date"] >= cutoff]
    latest = _latest_health(daily)

    snapshot = {
        "generated_at": dt.datetime.now().replace(microsecond=0).isoformat(),
        "sync": {
            "last_sync": meta.get("last_sync"),
            "message": meta.get("last_sync_message"),
            "error": meta.get("last_error"),
            "cooldown_until": meta.get("cooldown_until"),
        },
        "lthr_estimate": lthr,
        "today": latest,
        "pmc": [p for p in pmc if p["date"] >= cutoff],
        "daily": daily_recent,
        "weekly": weekly,
        "activities": list(reversed(acts[-30:])),
        "load_sources": _count(a["load_source"] for a in acts if a["date"] >= cutoff),
        "last_by_sport": {
            s: max((a["date"] for a in acts if a["sport"] == s), default=None) for s in SPORTS
        },
    }
    snapshot["signals"], snapshot["recommendation"] = coach_signals(snapshot, daily)
    snapshot["profile"] = store.read_profile()
    snapshot["profile_path"] = str(store.profile_path())
    return snapshot


def _count(items) -> dict:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


def _latest_health(daily: list[dict]) -> dict:
    """Most recent non-null value per metric (today's sleep may not be in yet)."""
    out: dict[str, Any] = {}
    for row in reversed(daily[-7:]):
        for k, v in row.items():
            if k not in out and v is not None:
                out[k] = v
                if k not in ("date", "updated_at"):
                    out[f"{k}__date"] = row["date"]
    return out


def coach_signals(snap: dict, daily: list[dict]) -> tuple[list[dict], dict]:
    sig: list[dict] = []
    t = snap["today"]
    today = dt.date.today()
    recent = daily[-8:]

    def add(level, title, detail):
        sig.append({"level": level, "title": title, "detail": detail})

    # HRV vs personal baseline
    hrv, low = t.get("hrv_last"), t.get("hrv_low")
    if hrv and low:
        streak = 0
        for r in reversed([r for r in recent if r.get("hrv_last")]):
            if r.get("hrv_low") and r["hrv_last"] < r["hrv_low"]:
                streak += 1
            else:
                break
        if streak >= 3:
            add("alert", f"HRV poniżej normy od {streak} nocy",
                f"Ostatnio {hrv:.0f} ms przy dolnej granicy {low:.0f} ms. Organizm nie nadąża z regeneracją.")
        elif hrv < low:
            note = ("Jedna noc to sygnał, nie wyrok — obserwuj jutro." if streak <= 1
                    else "Druga noc z rzędu — jutro trzecia oznacza dzień regeneracji.")
            add("warn", "HRV poniżej Twojej normy", f"{hrv:.0f} ms (norma od {low:.0f} ms). {note}")
        else:
            add("ok", "HRV w normie", f"{hrv:.0f} ms (norma {low:.0f}–{t.get('hrv_high') or 0:.0f} ms).")

    # Resting HR vs 7-day mean
    rhrs = [r["rhr"] for r in recent[:-1] if r.get("rhr")]
    if t.get("rhr") and len(rhrs) >= 4:
        base = statistics.mean(rhrs)
        if t["rhr"] >= base + 5:
            add("warn", "Podwyższone tętno spoczynkowe",
                f"{t['rhr']:.0f} bpm vs średnia {base:.0f}. Możliwe zmęczenie, stres lub początek infekcji.")

    # Sleep
    if t.get("sleep_s"):
        h = t["sleep_s"] / 3600
        sc = t.get("sleep_score")
        if h < 6.5 or (sc and sc < 60):
            add("warn", "Słaby sen", f"{h:.1f} h snu" + (f", wynik {sc:.0f}" if sc else "") + ".")

    # Readiness
    if t.get("readiness") is not None and t["readiness"] < 40:
        add("warn", "Niska gotowość treningowa (Garmin)", f"Wynik {t['readiness']:.0f}/100.")

    # Form & ramp from own PMC
    pmc = snap["pmc"]
    if pmc:
        p = pmc[-1]
        ctl, atl, tsb = p["ctl"], p["atl"], p["tsb"]
        ratio = atl / ctl if ctl else 0
        if ctl >= 10 and ratio > 1.5:
            add("alert", "Gwałtowny skok obciążenia",
                f"Zmęczenie (ATL {atl:.0f}) to {ratio:.1f}× Twojej formy bazowej (CTL {ctl:.0f}). Ryzyko przeciążenia.")
        elif ctl >= 10 and ratio > 1.3:
            add("warn", "Wysokie obciążenie względem bazy",
                f"ATL/CTL = {ratio:.2f}. Produktywny blok, ale pilnuj regeneracji.")
        if len(pmc) > 28:
            ramp = ctl - pmc[-8]["ctl"]
            if ramp > 0.12 * max(ctl, 1) and ctl >= 10:
                add("info", "Forma rośnie szybko", f"CTL +{ramp:.1f} w tydzień.")
        if ctl >= 10 and tsb > 0.25 * ctl:
            add("info", "Jesteś wypoczęty", "Dobra chwila na trening jakościowy albo test.")

    # Run volume ramp (injury risk)
    weekly = snap["weekly"]
    if len(weekly) >= 4:
        cur = weekly[-1].get("run_km", 0)
        prev = [w.get("run_km", 0) for w in weekly[-4:-1]]
        avg = sum(prev) / 3
        if avg > 5 and cur > avg * 1.3:
            add("warn", "Szybki wzrost kilometrażu biegowego",
                f"{cur:.0f} km w tym tygodniu vs średnio {avg:.0f} km. Bieg najbardziej obciąża ścięgna.")

    # Discipline balance
    for s, last in snap["last_by_sport"].items():
        if last:
            gap = (today - dt.date.fromisoformat(last)).days
            if gap >= 8:
                add("info", f"Dawno nie było: {SPORT_PL[s]}", f"Ostatni trening {gap} dni temu.")

    levels = [s["level"] for s in sig]
    if "alert" in levels:
        rec = {"level": "alert", "title": "Dziś regeneracja",
               "detail": "Wolne albo luźne 30–45 min w Z1 (najlepiej pływanie technika lub spokojny rower). Bez akcentów."}
    elif levels.count("warn") >= 2:
        rec = {"level": "warn", "title": "Dziś tylko tlen",
               "detail": "Przełóż interwały. Spokojny trening w Z2, krócej niż planowałeś."}
    elif "warn" in levels:
        rec = {"level": "warn", "title": "Trenuj, ale z rezerwą",
               "detail": "Plan może zostać, jeśli rozgrzewka idzie dobrze. Akcent skróć o ~20% lub zamień na Z2."}
    else:
        rec = {"level": "ok", "title": "Zielone światło",
               "detail": "Sygnały regeneracji w porządku — dobry dzień na zaplanowany akcent."}
    if not daily:
        rec = {"level": "info", "title": "Czekam na dane", "detail": "Uruchom synchronizację."}
    return sig, rec


def compact_snapshot(snap: dict, days: int = 28) -> dict:
    """Smaller snapshot for an LLM: what a coach needs, without chart-only series."""
    keep_daily = ("date", "hrv_last", "hrv_low", "hrv_high", "hrv_status", "rhr", "sleep_s",
                  "sleep_score", "bb_high", "readiness", "stress_avg", "acwr", "training_status")
    acts = [
        {k: a.get(k) for k in ("id", "start", "sport", "name", "duration_s", "distance_m", "avg_hr",
                               "avg_power", "norm_power", "load", "load_source", "aerobic_te", "anaerobic_te")}
        for a in snap["activities"]
    ]
    return {
        "athlete_profile": snap["profile"],
        "today_recommendation_rule_based": snap["recommendation"],
        "signals": snap["signals"],
        "latest_health": {k: v for k, v in snap["today"].items() if not k.endswith("__date")},
        "load_model": "Garmin training load per activity; CTL=42d EWMA, ATL=7d EWMA, TSB=CTL-ATL (prev. day). Not TrainingPeaks TSS scale.",
        "pmc_last_days": snap["pmc"][-days:],
        "weekly_volume_16w": snap["weekly"],
        "daily_health_last_days": [{k: r.get(k) for k in keep_daily} for r in snap["daily"][-days:]],
        "recent_activities": acts,
        "last_session_by_sport": snap["last_by_sport"],
        "sync": snap["sync"],
    }
