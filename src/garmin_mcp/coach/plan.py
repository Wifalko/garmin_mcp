"""Training plan: stored sessions, matched against what was actually done."""

from __future__ import annotations

import datetime as dt
from typing import Any

from garmin_mcp.coach import store

PLAN_SPORTS = ("swim", "bike", "run", "strength", "brick", "rest", "other")
STATUSES = ("planned", "skipped", "done")
WEEKDAYS_PL = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]
_FIELDS = ("date", "sport", "title", "duration_min", "distance_km", "intensity", "description", "status")


def _now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _clean(item: dict) -> dict:
    out: dict[str, Any] = {}
    for k in _FIELDS:
        if k in item and item[k] not in (None, ""):
            out[k] = item[k]
    if "date" in out:
        out["date"] = dt.date.fromisoformat(str(out["date"])[:10]).isoformat()
    if "sport" in out:
        s = str(out["sport"]).lower().strip()
        out["sport"] = s if s in PLAN_SPORTS else "other"
    if "status" in out and out["status"] not in STATUSES:
        out.pop("status")
    for k in ("duration_min", "distance_km"):
        if k in out:
            try:
                out[k] = round(float(out[k]), 1)
            except (TypeError, ValueError):
                out.pop(k)
    return out


def apply_ops(ops: dict, source: str = "coach") -> dict:
    """Apply {"upsert": [...], "delete": [ids]}; an upsert with "id" updates that session."""
    added = updated = deleted = 0
    errors: list[str] = []
    conn = store.connect()
    try:
        for raw_id in ops.get("delete") or []:
            try:
                deleted += conn.execute("DELETE FROM plan WHERE id=?", (int(raw_id),)).rowcount
            except (TypeError, ValueError):
                errors.append(f"zły id do usunięcia: {raw_id}")
        for item in ops.get("upsert") or []:
            try:
                clean = _clean(item)
            except ValueError as exc:
                errors.append(f"zła data w {item}: {exc}")
                continue
            sid = item.get("id")
            if sid:
                if not clean:
                    continue
                sets = ", ".join(f"{k}=?" for k in clean)
                n = conn.execute(
                    f"UPDATE plan SET {sets}, updated_at=? WHERE id=?",
                    [*clean.values(), _now(), int(sid)],
                ).rowcount
                if n:
                    updated += 1
                    continue
            if "date" not in clean or "sport" not in clean:
                errors.append(f"brak daty lub dyscypliny: {item}")
                continue
            clean.setdefault("status", "planned")
            cols = list(clean)
            conn.execute(
                f"INSERT INTO plan ({', '.join(cols)}, source, created_at, updated_at) "
                f"VALUES ({', '.join('?' for _ in cols)}, ?, ?, ?)",
                [*clean.values(), source, _now(), _now()],
            )
            added += 1
        conn.commit()
    finally:
        conn.close()
    return {"added": added, "updated": updated, "deleted": deleted, "errors": errors}


def list_plan(start: dt.date, end: dt.date) -> list[dict]:
    """Planned sessions in [start, end], each matched to an activity of the same sport/day."""
    conn = store.connect()
    try:
        sessions = [dict(r) for r in conn.execute(
            "SELECT * FROM plan WHERE date BETWEEN ? AND ? ORDER BY date, id",
            (start.isoformat(), end.isoformat()))]
        acts = [dict(r) for r in conn.execute(
            "SELECT id, date, start, sport, name, duration_s, distance_m FROM activities "
            "WHERE date BETWEEN ? AND ? ORDER BY start", (start.isoformat(), end.isoformat()))]
    finally:
        conn.close()

    today = dt.date.today().isoformat()
    used: set[int] = set()
    for s in sessions:
        s["weekday"] = WEEKDAYS_PL[dt.date.fromisoformat(s["date"]).weekday()]
        want = ("bike", "run") if s["sport"] == "brick" else (s["sport"],)
        match = next((a for a in acts if a["date"] == s["date"] and a["sport"] in want
                      and a["id"] not in used), None)
        if match:
            used.add(match["id"])
            s["activity"] = match
        if s["status"] == "skipped" or s["sport"] == "rest":
            s["state"] = s["status"] if s["sport"] != "rest" else "rest"
        elif match or s["status"] == "done":
            s["state"] = "done"
        elif s["date"] < today:
            s["state"] = "missed"
        elif s["date"] == today:
            s["state"] = "today"
        else:
            s["state"] = "planned"
    return sessions


def unplanned_activities(start: dt.date, end: dt.date, sessions: list[dict]) -> list[dict]:
    matched = {s["activity"]["id"] for s in sessions if s.get("activity")}
    conn = store.connect()
    try:
        acts = [dict(r) for r in conn.execute(
            "SELECT id, date, start, sport, name, duration_s, distance_m FROM activities "
            "WHERE date BETWEEN ? AND ? ORDER BY start", (start.isoformat(), end.isoformat()))]
    finally:
        conn.close()
    return [a for a in acts if a["id"] not in matched]


def plan_for_prompt(days_back: int = 7, days_ahead: int = 28) -> list[dict]:
    today = dt.date.today()
    rows = list_plan(today - dt.timedelta(days=days_back), today + dt.timedelta(days=days_ahead))
    keep = ("id", "date", "weekday", "sport", "title", "duration_min", "distance_km",
            "intensity", "description", "state")
    return [{k: r.get(k) for k in keep if r.get(k) is not None} for r in rows]
