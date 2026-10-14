"""Mirror the training plan into a dedicated Google Calendar as all-day events.

- Uses the narrow scope `calendar.app.created`: the app can only manage the
  calendar it creates ("Trening"), never touch your other calendars.
- OAuth "installed app" flow with a loopback redirect to the local dashboard;
  your own Google Cloud OAuth client (Desktop type) is entered in the dashboard.
- Event ids are derived from plan ids, so syncing is idempotent: each run
  inserts new sessions, updates changed ones and deletes removed/skipped ones.
- A sync runs a few seconds after every plan change (dashboard, coach chat or
  Claude Desktop via MCP) and after each Garmin sync (to mark sessions done).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import secrets
import threading
import time
from urllib.parse import quote, urlencode

import requests

from garmin_mcp.coach import store

SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
CALENDAR_NAME = "Trening"
WINDOW_BACK_DAYS = 14
WINDOW_AHEAD_DAYS = 180
B32HEX = "0123456789abcdefghijklmnopqrstuv"
EMOJI = {"swim": "🏊", "bike": "🚴", "run": "🏃", "strength": "🏋️", "brick": "🚴🏃", "other": "💪"}

_sync_lock = threading.Lock()
_pending = threading.Event()
_worker: threading.Thread | None = None
_token_cache: dict = {}
_auth_states: dict[str, dict] = {}


class GCalError(RuntimeError):
    pass


# --- config & status -----------------------------------------------------------

def _cfg() -> dict:
    return store.read_config()


def connected() -> bool:
    c = _cfg()
    return bool(c.get("gcal_client_id") and c.get("gcal_refresh_token"))


def status() -> dict:
    c = _cfg()
    conn = store.connect()
    try:
        last = store.get_meta(conn, "gcal_last_sync")
        msg = store.get_meta(conn, "gcal_last_message")
        err = store.get_meta(conn, "gcal_last_error")
    finally:
        conn.close()
    return {
        "configured": bool(c.get("gcal_client_id") and c.get("gcal_client_secret")),
        "connected": connected(),
        "calendar_id": c.get("gcal_calendar_id"),
        "client_id_hint": (c.get("gcal_client_id") or "")[:12] + "…" if c.get("gcal_client_id") else None,
        "last_sync": last,
        "last_message": msg,
        "last_error": err,
    }


def save_client(client_id: str, client_secret: str) -> dict:
    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    if not client_id.endswith(".apps.googleusercontent.com") or not client_secret:
        raise ValueError("Podaj Client ID (kończy się na .apps.googleusercontent.com) i Client secret.")
    store.write_config({"gcal_client_id": client_id, "gcal_client_secret": client_secret,
                        "gcal_refresh_token": ""})
    _token_cache.clear()
    return status()


def disconnect() -> dict:
    store.write_config({"gcal_refresh_token": ""})
    _token_cache.clear()
    return status()


def _set_meta(**values) -> None:
    conn = store.connect()
    try:
        for k, v in values.items():
            store.set_meta(conn, k, v)
        conn.commit()
    finally:
        conn.close()


# --- OAuth -------------------------------------------------------------------------

def auth_url(redirect_uri: str) -> str:
    c = _cfg()
    if not c.get("gcal_client_id"):
        raise GCalError("Najpierw wpisz Client ID i Client secret.")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _auth_states[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "at": time.time()}
    return AUTH_URL + "?" + urlencode({
        "client_id": c["gcal_client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


def finish_auth(code: str, state: str) -> dict:
    st = _auth_states.pop(state, None)
    if not st or time.time() - st["at"] > 900:
        raise GCalError("Sesja logowania wygasła lub jest nieprawidłowa — kliknij „Połącz z Google” jeszcze raz.")
    c = _cfg()
    r = requests.post(TOKEN_URL, data={
        "code": code, "client_id": c["gcal_client_id"], "client_secret": c["gcal_client_secret"],
        "redirect_uri": st["redirect_uri"], "grant_type": "authorization_code",
        "code_verifier": st["verifier"],
    }, timeout=30)
    data = r.json() if r.content else {}
    if r.status_code != 200 or "refresh_token" not in data:
        raise GCalError(f"Google odrzucił logowanie: {data.get('error_description') or data.get('error') or r.text[:200]}")
    store.write_config({"gcal_refresh_token": data["refresh_token"]})
    _token_cache.update(token=data["access_token"], exp=time.time() + int(data.get("expires_in", 3600)) - 60)
    return sync_now()


def _access_token() -> str:
    if _token_cache.get("token") and _token_cache.get("exp", 0) > time.time():
        return _token_cache["token"]
    c = _cfg()
    if not c.get("gcal_refresh_token"):
        raise GCalError("Kalendarz Google nie jest połączony.")
    r = requests.post(TOKEN_URL, data={
        "client_id": c["gcal_client_id"], "client_secret": c["gcal_client_secret"],
        "refresh_token": c["gcal_refresh_token"], "grant_type": "refresh_token",
    }, timeout=30)
    data = r.json() if r.content else {}
    if r.status_code != 200:
        if data.get("error") == "invalid_grant":
            store.write_config({"gcal_refresh_token": ""})
            raise GCalError("Połączenie z Google wygasło albo zostało cofnięte — połącz ponownie. "
                            "(Jeśli dzieje się to co 7 dni: w Google Cloud ustaw aplikację na „In production”.)")
        raise GCalError(f"Nie udało się odświeżyć dostępu do Google: {data.get('error_description') or r.text[:200]}")
    _token_cache.update(token=data["access_token"], exp=time.time() + int(data.get("expires_in", 3600)) - 60)
    return _token_cache["token"]


def _api(method: str, path: str, ok=(200, 204), **kw):
    r = requests.request(method, API + path, headers={"Authorization": f"Bearer {_access_token()}"},
                         timeout=30, **kw)
    if r.status_code not in ok:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            msg = r.text
        err = GCalError(f"Google Calendar ({r.status_code}): {msg[:300]}")
        err.status = r.status_code  # type: ignore[attr-defined]
        raise err
    return r.json() if r.content else None


# --- calendar & events -------------------------------------------------------------

def _ensure_calendar() -> str:
    cid = _cfg().get("gcal_calendar_id")
    if cid:
        try:
            _api("GET", f"/calendars/{quote(cid)}")
            return cid
        except GCalError as exc:
            if getattr(exc, "status", None) not in (403, 404):
                raise
    cal = _api("POST", "/calendars", json={
        "summary": CALENDAR_NAME,
        "description": "Plan treningowy od trenera (dashboard Garmin). Zmieniaj plan w dashboardzie — "
                       "ręczne zmiany tutaj zostaną nadpisane.",
    })
    store.write_config({"gcal_calendar_id": cal["id"]})
    return cal["id"]


def _salt() -> str:
    c = _cfg()
    if not c.get("gcal_salt"):
        store.write_config({"gcal_salt": "".join(secrets.choice(B32HEX) for _ in range(8))})
    return _cfg()["gcal_salt"]


def _fmt_dur(m) -> str:
    if not m:
        return ""
    m = int(round(m))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def build_event(s: dict, salt: str) -> dict:
    day = dt.date.fromisoformat(s["date"])
    title = s.get("title") or s["sport"]
    dur = _fmt_dur(s.get("duration_min"))
    prefix = "✓ " if s.get("state") == "done" else ""
    summary = f"{prefix}{EMOJI.get(s['sport'], '💪')} {title}" + (f" · {dur}" if dur else "")
    lines = []
    if s.get("intensity"):
        lines.append(f"Intensywność: {s['intensity']}")
    if dur:
        lines.append(f"Czas: {dur}")
    if s.get("distance_km"):
        lines.append(f"Dystans: {s['distance_km']:g} km")
    if s.get("description"):
        lines.append("")
        lines.append(s["description"])
    if s.get("state") == "done":
        lines.append("")
        lines.append("✓ Wykonane (dopasowano trening z Garmina)")
    lines.append("")
    lines.append("— plan trenera z dashboardu Garmin")
    body = {
        "summary": summary,
        "description": "\n".join(lines).strip(),
        "start": {"date": day.isoformat()},
        "end": {"date": (day + dt.timedelta(days=1)).isoformat()},
        "transparency": "transparent",
        "status": "confirmed",
        "reminders": {"useDefault": False},
    }
    digest = hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    body["id"] = f"tc{salt}{s['id']}"
    body["extendedProperties"] = {"private": {"coach": "1", "hash": digest, "plan_id": str(s["id"])}}
    return body


def desired_events() -> dict[str, dict]:
    from garmin_mcp.coach import plan  # local import: plan triggers gcal syncs

    today = dt.date.today()
    sessions = plan.list_plan(today - dt.timedelta(days=WINDOW_BACK_DAYS),
                              today + dt.timedelta(days=WINDOW_AHEAD_DAYS))
    salt = _salt()
    out = {}
    for s in sessions:
        if s["sport"] == "rest" or s.get("status") == "skipped":
            continue
        ev = build_event(s, salt)
        out[ev["id"]] = ev
    return out


def _existing_events(cid: str) -> dict[str, dict]:
    time_min = (dt.date.today() - dt.timedelta(days=WINDOW_BACK_DAYS)).isoformat() + "T00:00:00Z"
    out, page = {}, None
    while True:
        params = {"timeMin": time_min, "maxResults": 2500, "privateExtendedProperty": "coach=1",
                  "singleEvents": "true"}
        if page:
            params["pageToken"] = page
        data = _api("GET", f"/calendars/{quote(cid)}/events", params=params)
        for e in data.get("items", []):
            out[e["id"]] = e
        page = data.get("nextPageToken")
        if not page:
            return out


def sync_now() -> dict:
    """Make the Google calendar match the plan. Safe to call any time."""
    if not connected():
        return {"ok": False, "message": "Kalendarz Google nie jest połączony."}
    with _sync_lock:
        try:
            cid = _ensure_calendar()
            want = desired_events()
            have = _existing_events(cid)
            added = updated = deleted = 0
            for eid, ev in want.items():
                cur = have.get(eid)
                if cur and (cur.get("extendedProperties", {}).get("private", {}).get("hash")
                            == ev["extendedProperties"]["private"]["hash"]):
                    continue
                if cur:
                    _api("PUT", f"/calendars/{quote(cid)}/events/{eid}", json=ev)
                    updated += 1
                    continue
                try:
                    _api("POST", f"/calendars/{quote(cid)}/events", json=ev)
                    added += 1
                except GCalError as exc:
                    if getattr(exc, "status", None) != 409:
                        raise
                    # id exists as a cancelled event (deleted earlier): bring it back
                    _api("PUT", f"/calendars/{quote(cid)}/events/{eid}", json=ev)
                    added += 1
            for eid in set(have) - set(want):
                _api("DELETE", f"/calendars/{quote(cid)}/events/{eid}", ok=(200, 204, 404, 410))
                deleted += 1
            msg = f"Kalendarz zaktualizowany: +{added}, zmienione {updated}, usunięte {deleted}."
            _set_meta(gcal_last_sync=dt.datetime.now().replace(microsecond=0).isoformat(),
                      gcal_last_message=msg, gcal_last_error=None)
            return {"ok": True, "added": added, "updated": updated, "deleted": deleted, "message": msg}
        except (GCalError, requests.RequestException) as exc:
            _set_meta(gcal_last_error=str(exc))
            return {"ok": False, "message": str(exc)}


def schedule_sync(delay: float = 3.0) -> None:
    """Debounced background sync after plan edits (several edits -> one sync)."""
    global _worker
    if not connected():
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

    _worker = threading.Thread(target=run, name="gcal-sync", daemon=True)
    _worker.start()
