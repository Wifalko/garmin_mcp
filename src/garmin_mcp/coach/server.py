"""Local dashboard: http://127.0.0.1:8787

Serves the page, a JSON API, and re-syncs Garmin in the background.

CLI:  uv run garmin-dashboard [--port 8787] [--interval 15] [--no-browser]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from garmin_mcp.coach import body, gcal, llm, metrics, plan, progress, store, sync, trainer

STATIC = Path(__file__).parent / "static"
CTYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".html": "text/html; charset=utf-8", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif", ".pdf": "application/pdf"}


def _log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def trainer_view(week_offset: int = 0) -> dict:
    today = dt.date.today()
    start = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=7 * week_offset)
    end = start + dt.timedelta(days=13)
    sessions = plan.list_plan(start, end)
    return {
        "today": today.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sessions": sessions,
        "unplanned": plan.unplanned_activities(start, end, sessions),
        "progress": progress.build_progress(),
        "llm": llm.status(),
        "gcal": gcal.status(),
    }


def activity_view(activity_id: int, refresh: bool = False) -> dict:
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        day = conn.execute("SELECT * FROM daily WHERE date=?", (row["date"],)).fetchone() if row else None
    finally:
        conn.close()
    if not row:
        return {"error": "Nie ma takiego treningu w bazie."}
    act = {k: row[k] for k in row.keys() if k != "raw"}
    try:
        detail = sync.fetch_activity_detail(activity_id, refresh=refresh)
    except Exception as exc:
        detail = {"error": f"Nie udało się pobrać szczegółów z Garmina: {exc}"}
    return {
        "activity": act,
        "detail": detail,
        "comparison": progress.compare_activity(activity_id),
        "day": dict(day) if day else None,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _guard(self, fn, *args):
        try:
            self._json(fn(*args))
        except Exception as exc:
            _log(f"Błąd API: {exc}")
            self._json({"error": str(exc)}, 500)

    def _host_ok(self) -> bool:
        # Blocks DNS-rebinding: only answer requests addressed to this machine.
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost")

    def _oauth_callback(self, q):
        if "error" in q:
            ok, msg = False, f"Google: {q['error'][0]}"
        else:
            try:
                res = gcal.finish_auth(q["code"][0], q["state"][0])
                ok, msg = res.get("ok", False), res.get("message", "")
            except Exception as exc:
                ok, msg = False, str(exc)
        _log(("Kalendarz Google połączony. " if ok else "Kalendarz Google: ") + msg)
        import html
        page = f"""<!doctype html><meta charset="utf-8"><title>Kalendarz Google</title>
<body style="font:16px system-ui;padding:40px;max-width:560px;margin:auto">
<h2>{"✓ Połączono z Kalendarzem Google" if ok else "Nie udało się połączyć"}</h2>
<p>{html.escape(msg)}</p><p><a href="/#trener">Wróć do dashboardu</a></p>
{"<script>setTimeout(()=>location.href='/#trener',2500)</script>" if ok else ""}</body>"""
        self._send(200, page.encode("utf-8"), CTYPES[".html"])

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        url = urlparse(self.path)
        path, q = url.path, parse_qs(url.query)
        if path == "/" and "state" in q and ("code" in q or "error" in q):
            return self._oauth_callback(q)
        if path == "/oauth/google/start":
            try:
                port = self.server.server_address[1]
                url = gcal.auth_url(f"http://127.0.0.1:{port}/")
            except Exception as exc:
                return self._send(400, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(), CTYPES[".html"])
        elif path.startswith("/static/"):
            f = (STATIC / path[len("/static/"):]).resolve()
            if f.parent != STATIC.resolve() or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            self._send(200, f.read_bytes(), CTYPES.get(f.suffix, "application/octet-stream"))
        elif path == "/api/data":
            def data():
                snap = metrics.build_snapshot()
                snap["sync"]["running"] = sync.is_running()
                return snap
            self._guard(data)
        elif path == "/api/status":
            self._json({"running": sync.is_running()})
        elif path == "/api/trainer":
            self._guard(trainer_view, int((q.get("week") or ["0"])[0]))
        elif path.startswith("/api/activity/"):
            refresh = (q.get("refresh") or ["0"])[0] == "1"
            self._guard(activity_view, int(path.rsplit("/", 1)[1]), refresh)
        elif path == "/api/chat":
            thread = (q.get("thread") or ["general"])[0]
            self._guard(lambda: {"thread": thread, "messages": trainer.thread_messages(thread)})
        elif path == "/api/settings":
            self._json(llm.status())
        elif path == "/api/gcal":
            self._json(gcal.status())
        elif path == "/api/body":
            self._guard(lambda: {"scans": body.list_scans(), "analysis": body.analyse(),
                                 "labels": body.LABELS, "llm": llm.status()})
        elif path.startswith("/api/body/photo/"):
            try:
                _, scan_id, idx = path.rsplit("/", 2)
                f = body.photo_path(int(scan_id), int(idx))
            except ValueError:
                f = None
            if not f:
                return self._send(404, b"not found", "text/plain")
            self._send(200, f.read_bytes(), CTYPES.get(f.suffix.lower(), "application/octet-stream"))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # The custom header forces a CORS preflight, which this server never approves,
        # so other websites open in the browser cannot post here.
        if not self._host_ok() or self.headers.get("X-Coach") != "1":
            return self._send(403, b"forbidden", "text/plain")
        path = urlparse(self.path).path
        req = self._body()
        if path == "/api/sync":
            if sync.is_running():
                return self._json({"started": False, "message": "Synchronizacja już trwa."})
            threading.Thread(target=sync.sync, kwargs={"log": _log}, daemon=True).start()
            time.sleep(0.2)
            return self._json({"started": True})
        if path == "/api/chat":
            aid = req.get("activity_id")
            return self._guard(trainer.chat, req.get("thread") or "general",
                               req.get("message") or "", int(aid) if aid else None)
        if path == "/api/chat/clear":
            trainer.clear_thread(req.get("thread") or "general")
            return self._json({"ok": True})
        if path == "/api/plan":
            return self._guard(plan.apply_ops, req, "manual")
        if path == "/api/body/upload":
            return self._guard(body.import_photos, req.get("files") or [], req.get("fallback_date") or None)
        if path == "/api/body/update":
            return self._guard(body.update_scan, int(req.get("id") or 0), req)
        if path == "/api/body/delete":
            return self._guard(body.delete_scan, int(req.get("id") or 0))
        if path == "/api/gcal/client":
            return self._guard(gcal.save_client, req.get("client_id"), req.get("client_secret"))
        if path == "/api/gcal/sync":
            return self._guard(gcal.sync_now)
        if path == "/api/gcal/disconnect":
            return self._guard(gcal.disconnect)
        if path == "/api/settings":
            allowed = {k: req[k] for k in ("anthropic_api_key", "model", "backend", "claude_cli_path") if k in req}
            store.write_config(allowed)
            return self._json(llm.status())
        self._send(404, b"not found", "text/plain")


def _scheduler(interval_min: float) -> None:
    while True:
        _log("Synchronizacja z Garmin…")
        sync.sync(log=_log)
        if gcal.connected():  # new activities may turn sessions into "done"
            res = gcal.sync_now()
            if not res.get("ok"):
                _log("Kalendarz Google: " + res.get("message", ""))
        time.sleep(max(5.0, interval_min) * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lokalny dashboard treningowy Garmin.")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--interval", type=float, default=15, help="minuty między synchronizacjami (min. 5)")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-sync", action="store_true", help="tylko pokazuj dane z bazy")
    args = parser.parse_args()

    store.connect().close()
    _log(f"Baza danych: {store.db_path()}")
    _log(f"Profil zawodnika: {store.profile_path()}")
    st = llm.status()
    _log("Trener: " + {"api": f"API Anthropic ({st['model']})", "cli": "Claude Code (subskrypcja)"}.get(
        st["backend"], "brak połączenia z Claude — ustaw w zakładce Trener"))
    if not args.no_sync:
        threading.Thread(target=_scheduler, args=(args.interval,), daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    _log(f"Dashboard: {url}   (Ctrl+C aby zakończyć)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
