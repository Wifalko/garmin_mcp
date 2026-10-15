"""Tests for the local coach: sync into SQLite, metrics, signals, MCP tools."""

import asyncio
import datetime as dt
import json
import random

import pytest
from mcp.server.fastmcp import FastMCP

from garmin_mcp import coach_tools
from garmin_mcp.coach import metrics, store, sync


class FakeGarmin:
    """Returns payloads shaped like Garmin Connect's, for ~120 days of training."""

    def __init__(self, days=120, seed=7):
        rnd = random.Random(seed)
        today = dt.date.today()
        self.acts = []
        plan = {0: None, 1: "swim", 2: "run", 3: "bike", 4: "swim", 5: "bike", 6: "run"}
        aid = 1000
        for i in range(days, -1, -1):
            d = today - dt.timedelta(days=i)
            sport = plan[d.weekday()]
            if not sport:
                continue
            aid += 1
            dur = {"swim": 2700, "bike": 5400 if d.weekday() != 5 else 10800, "run": 3000 if d.weekday() != 6 else 5400}[sport]
            dur = dur * rnd.uniform(0.85, 1.15)
            key = {"swim": "lap_swimming", "bike": "road_biking", "run": "running"}[sport]
            dist = {"swim": 2400, "bike": dur / 3600 * 29000, "run": dur / 3600 * 11500}[sport]
            self.acts.append({
                "activityId": aid,
                "activityName": f"{sport} {d}",
                "startTimeLocal": f"{d} 07:00:00",
                "activityType": {"typeKey": key},
                "duration": dur, "movingDuration": dur, "distance": dist,
                "averageHR": rnd.randint(130, 150), "maxHR": rnd.randint(165, 182),
                "activityTrainingLoad": None if sport == "swim" else dur / 3600 * rnd.uniform(70, 110),
                "avgPower": 190 if sport == "bike" else None,
                "aerobicTrainingEffect": 3.1,
            })
        self.rnd = rnd

    def get_activities_by_date(self, start, end):
        return [a for a in self.acts if start <= a["startTimeLocal"][:10] <= end]

    def get_user_summary(self, day):
        return {"totalSteps": 9000, "restingHeartRate": 48 + self.rnd.randint(-2, 3),
                "averageStressLevel": 28, "bodyBatteryHighestValue": 82,
                "bodyBatteryLowestValue": 20, "bodyBatteryMostRecentValue": 55}

    def get_sleep_data(self, day):
        return {"dailySleepDTO": {"sleepTimeSeconds": 26000 + self.rnd.randint(-3000, 3000),
                                  "sleepScores": {"overall": {"value": 78}},
                                  "deepSleepSeconds": 5000, "remSleepSeconds": 6000}}

    def get_hrv_data(self, day):
        return {"hrvSummary": {"lastNightAvg": 62 + self.rnd.randint(-8, 6), "weeklyAvg": 63,
                               "status": "BALANCED",
                               "baseline": {"balancedLow": 56, "balancedUpper": 71}}}

    def get_training_readiness(self, day):
        return [{"score": 70, "level": "HIGH", "inputContext": "AFTER_WAKEUP_RESET",
                 "timestampLocal": f"{day}T07:00:00"}]

    def get_training_status(self, day):
        return {"mostRecentTrainingStatus": {"latestTrainingStatusData": {"123": {
                    "primaryTrainingDevice": True, "trainingStatusFeedbackPhrase": "PRODUCTIVE_1",
                    "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 520, "dailyTrainingLoadChronic": 480,
                                             "dailyAcuteChronicWorkloadRatio": 1.1}}}},
                "mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 54.2},
                                     "cycling": {"vo2MaxPreciseValue": 55.0}}}


@pytest.fixture
def coach_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_COACH_HOME", str(tmp_path))
    monkeypatch.setattr(sync, "CALL_PAUSE_S", 0)
    fake = FakeGarmin()
    monkeypatch.setattr(sync, "login", lambda: fake)
    return tmp_path


def test_classify_sport():
    assert sync.classify_sport("lap_swimming") == "swim"
    assert sync.classify_sport("open_water_swimming") == "swim"
    assert sync.classify_sport("indoor_cycling") == "bike"
    assert sync.classify_sport("virtual_ride") == "bike"
    assert sync.classify_sport("trail_running") == "run"
    assert sync.classify_sport("treadmill_running") == "run"
    assert sync.classify_sport("strength_training") == "strength"
    assert sync.classify_sport("multi_sport") == "multi"
    assert sync.classify_sport("walking") == "other"


def test_sync_backfills_incrementally(coach_home):
    first = sync.sync(backfill_days=30, log=lambda m: None)
    assert first["ok"], first
    assert first["backfill_left"] == 30 - 2 - sync.MAX_BACKFILL_PER_RUN

    conn = store.connect()
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] > 90
    assert conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 2 + sync.MAX_BACKFILL_PER_RUN
    row = dict(conn.execute("SELECT * FROM daily WHERE date=?", (dt.date.today().isoformat(),)).fetchone())
    assert row["hrv_low"] == 56 and row["readiness"] == 70 and row["vo2max_run"] == 54.2
    conn.close()

    while sync.sync(backfill_days=30, log=lambda m: None).get("backfill_left"):
        pass
    conn = store.connect()
    assert conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 30
    conn.close()


def test_rate_limit_sets_cooldown(coach_home, monkeypatch):
    from garminconnect import GarminConnectTooManyRequestsError

    def boom(*a):
        raise GarminConnectTooManyRequestsError("429")

    fake = sync.login()
    monkeypatch.setattr(fake, "get_activities_by_date", boom)
    assert not sync.sync(log=lambda m: None)["ok"]
    again = sync.sync(log=lambda m: None)
    assert not again["ok"] and "przerwa" in again["message"]


def test_snapshot_metrics(coach_home):
    sync.sync(backfill_days=10, log=lambda m: None)
    snap = metrics.build_snapshot()
    p = snap["pmc"][-1]
    assert p["ctl"] > 0 and p["atl"] > 0
    assert p["ctl_swim"] > 0 and p["ctl_bike"] > p["ctl_swim"]
    assert len(snap["weekly"]) == 16
    assert snap["weekly"][-2].get("bike_h", 0) > 0
    # swims had no Garmin load -> estimated from HR
    assert snap["load_sources"].get("hr", 0) > 0
    assert snap["recommendation"]["title"]
    assert "Profil zawodnika" in snap["profile"]


def test_signals_flag_low_hrv(coach_home):
    sync.sync(backfill_days=10, log=lambda m: None)
    conn = store.connect()
    conn.execute("UPDATE daily SET hrv_last = 40")
    conn.commit()
    conn.close()
    snap = metrics.build_snapshot()
    assert any(s["level"] == "alert" and "HRV" in s["title"] for s in snap["signals"])
    assert snap["recommendation"]["level"] == "alert"


def test_coach_mcp_tools(coach_home):
    app = FastMCP("t")
    coach_tools.configure(object())
    coach_tools.register_tools(app)

    async def call(name, args):
        result = await app.call_tool(name, args)
        blocks = result[0] if isinstance(result, tuple) else result
        return blocks[0].text

    synced = json.loads(asyncio.run(call("sync_coach_data", {})))
    assert synced["ok"]
    snap = json.loads(asyncio.run(call("get_coach_snapshot", {"days": 14})))
    assert len(snap["daily_health_last_days"]) <= 14
    assert snap["athlete_profile"].startswith("# Profil")
    assert "pmc_last_days" in snap and snap["recent_activities"]

    msg = asyncio.run(call("update_athlete_profile", {"markdown": "# Cel\nIM 70.3"}))
    assert "Saved" in msg
    assert store.read_profile() == "# Cel\nIM 70.3"


# --- plan, progress, coach chat ----------------------------------------------

from garmin_mcp.coach import llm, plan, progress, trainer  # noqa: E402


def _iso(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def test_plan_ops_and_matching(coach_home):
    sync.sync(backfill_days=5, log=lambda m: None)
    yesterday = dt.date.today() - dt.timedelta(days=1)
    sport_y = sync.classify_sport({0: None, 1: "lap_swimming", 2: "running", 3: "road_biking",
                                   4: "lap_swimming", 5: "road_biking", 6: "running"}[yesterday.weekday()])
    r = plan.apply_ops({"upsert": [
        {"date": yesterday.isoformat(), "sport": sport_y if sport_y != "other" else "run", "title": "wczoraj"},
        {"date": _iso(-2), "sport": "strength", "title": "siłownia"},
        {"date": _iso(3), "sport": "bike", "title": "Z2", "duration_min": "90"},
        {"date": "zła-data", "sport": "run"},
        {"sport": "run"},
    ]})
    assert r["added"] == 3 and len(r["errors"]) == 2
    rows = plan.list_plan(dt.date.today() - dt.timedelta(days=7), dt.date.today() + dt.timedelta(days=7))
    by_title = {s["title"]: s for s in rows}
    if sport_y != "other":
        assert by_title["wczoraj"]["state"] == "done" and by_title["wczoraj"]["activity"]
    assert by_title["siłownia"]["state"] == "missed"
    assert by_title["Z2"]["state"] == "planned" and by_title["Z2"]["duration_min"] == 90.0

    sid = by_title["Z2"]["id"]
    r = plan.apply_ops({"upsert": [{"id": sid, "date": _iso(4), "status": "skipped"}],
                        "delete": [by_title["siłownia"]["id"]]})
    assert r["updated"] == 1 and r["deleted"] == 1
    moved = [s for s in plan.plan_for_prompt() if s["id"] == sid][0]
    assert moved["date"] == _iso(4) and moved["state"] == "skipped"


def test_progress_detects_improvement(coach_home):
    sync.sync(backfill_days=5, log=lambda m: None)
    conn = store.connect()
    # make the last 4 weeks of runs 6% faster at the same heart rate
    conn.execute("UPDATE activities SET avg_hr = 140")
    conn.execute("UPDATE activities SET avg_speed = 3.0 WHERE sport='run'")
    conn.execute("UPDATE activities SET avg_speed = 3.18 WHERE sport='run' AND date > ?", (_iso(-28),))
    conn.commit()
    conn.close()
    p = {s["sport"]: s for s in progress.build_progress()["sports"]}
    assert p["run"]["verdict"] == "progres" and 5 < p["run"]["change_pct"] < 7
    assert p["bike"]["verdict"] == "stabilnie"
    assert p["swim"]["current"] > 0
    assert len(p["run"]["weekly"]) == 16

    run_id = store.connect().execute(
        "SELECT id FROM activities WHERE sport='run' ORDER BY date DESC").fetchone()[0]
    cmp = progress.compare_activity(run_id)
    assert cmp["n_similar"] > 3 and cmp["this"] > 0


def test_extract_plan_block():
    text, ops, err = trainer.extract_plan(
        'Zmieniam środę.\n```plan\n{"upsert":[{"id":3,"date":"2026-10-15"}],"delete":[4]}\n```')
    assert text == "Zmieniam środę." and ops["delete"] == [4] and err is None
    text, ops, err = trainer.extract_plan("Bez zmian.")
    assert ops is None and text == "Bez zmian."
    text, ops, err = trainer.extract_plan("x\n```plan\n{zepsute}\n```")
    assert ops is None and err


def test_chat_applies_plan_and_keeps_history(coach_home, monkeypatch):
    sync.sync(backfill_days=5, log=lambda m: None)
    seen = {}

    def fake_complete(system, messages, max_tokens=4000):
        seen["system"], seen["messages"] = system, messages
        return ("Ok, plan na jutro.\n```plan\n"
                + json.dumps({"upsert": [{"date": _iso(1), "sport": "run", "title": "Easy 40'",
                                          "duration_min": 40, "intensity": "Z2"}]})
                + "\n```")

    monkeypatch.setattr(llm, "complete", fake_complete)
    r = trainer.chat("general", "Ułóż plan na jutro")
    assert r["ok"] and r["reply"] == "Ok, plan na jutro."
    assert r["meta"]["plan_changes"]["added"] == 1
    assert "<aktualny_plan>" in seen["system"] and "Profil zawodnika" in seen["system"]
    assert any(s["title"] == "Easy 40'" for s in plan.plan_for_prompt())

    r = trainer.chat("general", "Dzięki")
    assert [m["role"] for m in seen["messages"]] == ["user", "assistant", "user"]
    assert len(trainer.thread_messages("general")) == 4

    aid = store.connect().execute("SELECT id FROM activities ORDER BY date DESC").fetchone()[0]
    monkeypatch.setattr(sync, "fetch_activity_detail", lambda i, refresh=False: {"laps": [{"distance": 1000}]})
    trainer.chat(f"activity:{aid}", "Co poprawić?", aid)
    assert "<omawiany_trening>" in seen["system"] and "porownanie_z_podobnymi" in seen["system"]
    assert len(trainer.thread_messages(f"activity:{aid}")) == 2


def test_chat_without_backend_reports_error(coach_home, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_claude_cli", lambda: None)
    assert llm.status()["backend"] is None
    r = trainer.chat("general", "hej")
    assert not r["ok"] and "klucz API" in r["error"]
    # error replies are not sent back to the model as history
    monkeypatch.setattr(llm, "complete", lambda s, m, max_tokens=4000: "ok")
    seen = []
    monkeypatch.setattr(llm, "complete", lambda s, m, max_tokens=4000: seen.append(m) or "ok")
    trainer.chat("general", "drugi raz")
    assert [x["role"] for x in seen[0]] == ["user"]


def test_llm_status_and_config(coach_home, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_claude_cli", lambda: "/usr/bin/claude")
    assert llm.status()["backend"] == "cli"
    store.write_config({"anthropic_api_key": "sk-ant-test-1234"})
    st = llm.status()
    assert st["backend"] == "api" and st["api_key_hint"] == "…1234"
    store.write_config({"backend": "cli"})
    assert llm.status()["backend"] == "cli"
    store.write_config({"anthropic_api_key": "", "backend": "api"})
    assert llm.status()["backend"] is None


def test_mcp_plan_tools(coach_home):
    app = FastMCP("t")
    coach_tools.configure(object())
    coach_tools.register_tools(app)

    async def call(name, args):
        result = await app.call_tool(name, args)
        blocks = result[0] if isinstance(result, tuple) else result
        return blocks[0].text

    assert asyncio.run(call("get_training_plan", {})) == "Plan is empty."
    res = json.loads(asyncio.run(call("update_training_plan", {"upsert": [
        {"date": _iso(2), "sport": "swim", "title": "Technika", "duration_min": 45}]})))
    assert res["added"] == 1
    rows = json.loads(asyncio.run(call("get_training_plan", {})))
    assert rows[0]["title"] == "Technika"
    snap = json.loads(asyncio.run(call("get_coach_snapshot", {})))
    assert snap["training_plan"][0]["title"] == "Technika" and "progress_4w_vs_prev_4w" in snap


def test_server_rejects_foreign_requests(coach_home):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from garmin_mcp.coach import server

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert urllib.request.urlopen(base + "/api/trainer").status == 200
        body = json.dumps({"upsert": [{"date": _iso(1), "sport": "run"}]}).encode()
        req = urllib.request.Request(base + "/api/plan", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
        req.add_header("X-Coach", "1")
        assert json.loads(urllib.request.urlopen(req).read())["added"] == 1
        req = urllib.request.Request(base + "/api/data", headers={"Host": "evil.example"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
    finally:
        httpd.shutdown()


# --- body composition from photos --------------------------------------------

import base64  # noqa: E402

from garmin_mcp.coach import body  # noqa: E402

PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082")).decode()


def _scan(date, weight, smm, fat_pct=15.0, seg=None):
    return {"date": date, "device": "InBody 570", "weight_kg": weight, "skeletal_muscle_kg": smm,
            "body_fat_pct": fat_pct, "segmental_lean_kg": seg, "confidence": "high"}


def test_body_import_merge_and_analysis(coach_home, monkeypatch):
    calls = []

    def fake_read(prompt, files, max_tokens=6000):
        calls.append([f.name for f in files])
        assert all(f.exists() for f in files) and "measurements" in prompt
        return "```json\n" + json.dumps({"measurements": [
            _scan(_iso(-70), "80,4 kg", 38.0),
            _scan(_iso(-40), 77.5, 37.4),
            dict(_scan(_iso(-1), 74.0, 36.6, seg={"left_arm": 3.4, "right_arm": 3.5, "trunk": 28.1,
                                                     "left_leg": 9.3, "right_leg": 9.9}),
                 source_files=[2, 3], other={"Kąt fazowy": "6,1°"}),
            {"date": None, "weight_kg": 70, "source_files": [4]},
        ], "unreadable": [{"file": 5, "reason": "rozmazane"}]}) + "\n```"

    monkeypatch.setattr(body.llm, "read_files", fake_read)
    files = [{"name": f"skan{i}.png", "data_b64": "data:image/png;base64," + PNG_1PX} for i in range(6)]
    r = body.import_photos(files)
    assert len(calls) == 1 and r["files"] == 6
    statuses = [x["status"] for x in r["results"]]
    assert statuses.count("added") == 3 and statuses.count("error") == 1  # undated scan without fallback
    assert r["unreadable"][0]["reason"] == "rozmazane"

    scans = body.list_scans()
    assert [s["weight_kg"] for s in scans] == [80.4, 77.5, 74.0]
    assert scans[-1]["extra"]["other"]["Kąt fazowy"] == "6,1°"
    assert len(scans[-1]["files"]) == 2 and body.photo_path(scans[-1]["id"], 0).exists()

    # same day again -> merged, new values fill gaps
    monkeypatch.setattr(body.llm, "read_files", lambda p, f, max_tokens=6000: json.dumps(
        {"measurements": [{"date": _iso(-1), "water_l": 45.2, "weight_kg": None}]}))
    r = body.import_photos(files[:1])
    assert r["results"][0]["status"] == "merged"
    last = body.list_scans()[-1]
    assert last["water_l"] == 45.2 and last["weight_kg"] == 74.0 and len(last["files"]) == 3

    a = body.analyse()
    titles = " ".join(s["title"] for s in a["signals"])
    assert "Szybki spadek masy" in titles          # ~ -1%/week
    assert "Spadek mięśni" in titles               # -1.4 kg SMM
    assert "Asymetria nóg" in titles               # 9.3 vs 9.9 kg
    assert a["changes"]["weight_kg"]["vs_first"] == -6.4

    # correction marks the row verified; date clash is refused
    sid = last["id"]
    assert body.update_scan(sid, {"weight_kg": "74,6", "notes": "po treningu"})["updated"] == 1
    fixed = [s for s in body.list_scans() if s["id"] == sid][0]
    assert fixed["weight_kg"] == 74.6 and fixed["verified"] == 1
    with pytest.raises(ValueError):
        body.update_scan(sid, {"date": _iso(-40)})
    assert body.delete_scan(sid)["deleted"] == 1


def test_body_fallback_date_and_bad_files(coach_home, monkeypatch):
    monkeypatch.setattr(body.llm, "read_files", lambda p, f, max_tokens=6000: json.dumps(
        {"measurements": [{"date": None, "weight_kg": 72.1}]}))
    r = body.import_photos([{"name": "a.jpg", "data_b64": PNG_1PX}], fallback_date=_iso(-3))
    assert r["results"][0]["date"] == _iso(-3)
    with pytest.raises(ValueError):
        body.save_upload("wirus.exe", PNG_1PX)
    monkeypatch.setattr(body.llm, "read_files", lambda p, f, max_tokens=6000: "Nie widzę wyników.")
    with pytest.raises(ValueError):
        body.import_photos([{"name": "b.jpg", "data_b64": PNG_1PX}])


def test_body_reaches_coach_and_mcp(coach_home, monkeypatch):
    body.upsert_measurement(_scan(_iso(-30), 76.0, 37.0), ["x.jpg"], None)
    body.upsert_measurement(_scan(_iso(-2), 75.5, 37.2), ["y.jpg"], None)
    sys_prompt = trainer.system_prompt()
    assert "<sklad_ciala>" in sys_prompt and "75.5" in sys_prompt

    app = FastMCP("t")
    coach_tools.configure(object())
    coach_tools.register_tools(app)

    async def call(name, args):
        result = await app.call_tool(name, args)
        blocks = result[0] if isinstance(result, tuple) else result
        return blocks[0].text

    scans = json.loads(asyncio.run(call("get_body_scans", {})))
    assert len(scans["measurements"]) == 2 and "files" not in scans["measurements"][0]
    snap = json.loads(asyncio.run(call("get_coach_snapshot", {})))
    assert snap["body_composition"]["history_last_12"][-1]["weight_kg"] == 75.5


def test_cli_auth_error_is_explained():
    class Out:
        stderr = "Failed to authenticate: OAuth session expired and could not be refreshed"
        stdout = ""
    msg = llm._cli_error(Out())
    assert "claude auth login" in msg and "OAuth session expired" in msg

    class Other:
        stderr = "boom"
        stdout = ""
    assert llm._cli_error(Other()).startswith("Claude Code zakończył się błędem")


# --- Google Calendar mirror ----------------------------------------------------

from garmin_mcp.coach import gcal  # noqa: E402


class FakeGoogle:
    """In-memory stand-in for the OAuth token endpoint and Calendar API."""

    def __init__(self):
        self.calendars, self.events, self.calls, self.cancelled = {}, {}, [], set()
        self.token_error = None

    class Resp:
        def __init__(self, code, data=None):
            self.status_code, self._data = code, data
            self.content = b"" if data is None else json.dumps(data).encode()
            self.text = self.content.decode()

        def json(self):
            return self._data

    def post_token(self, url, data=None, timeout=None):
        if self.token_error:
            return self.Resp(400, {"error": self.token_error})
        out = {"access_token": "at", "expires_in": 3600}
        if data.get("grant_type") == "authorization_code":
            out["refresh_token"] = "rt"
        return self.Resp(200, out)

    def request(self, method, url, headers=None, timeout=None, json=None, params=None):
        path = url.split("/calendar/v3", 1)[1]
        self.calls.append((method, path))
        parts = path.strip("/").split("/")
        if method == "POST" and parts == ["calendars"]:
            self.calendars["cal1"] = json
            return self.Resp(200, {"id": "cal1", **json})
        if method == "GET" and len(parts) == 2:
            return self.Resp(200, {"id": parts[1]}) if parts[1] in self.calendars else self.Resp(404, {"error": {"message": "nf"}})
        if parts[-1] == "events" and method == "GET":
            return self.Resp(200, {"items": list(self.events.values())})
        if parts[-1] == "events" and method == "POST":
            if json["id"] in self.events or json["id"] in self.cancelled:
                return self.Resp(409, {"error": {"message": "duplicate"}})
            self.events[json["id"]] = json
            return self.Resp(200, json)
        eid = parts[-1]
        if method == "PUT":
            self.cancelled.discard(eid)
            self.events[eid] = json
            return self.Resp(200, json)
        if method == "DELETE":
            self.events.pop(eid, None)
            self.cancelled.add(eid)
            return self.Resp(204)
        raise AssertionError((method, path))


@pytest.fixture
def google(coach_home, monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(gcal.requests, "post", fake.post_token)
    monkeypatch.setattr(gcal.requests, "request", fake.request)
    monkeypatch.setattr(gcal, "schedule_sync", lambda delay=3.0: None)
    gcal._token_cache.clear()
    gcal.save_client("123-abc.apps.googleusercontent.com", "GOCSPX-secret")
    return fake


def test_gcal_oauth_and_mirror(google):
    url = gcal.auth_url("http://127.0.0.1:8787/")
    assert "calendar.app.created" in url and "code_challenge=" in url and "access_type=offline" in url
    state = url.split("state=")[1].split("&")[0]
    with pytest.raises(gcal.GCalError):
        gcal.finish_auth("code", "wrong-state")

    ids = plan.apply_ops({"upsert": [
        {"date": _iso(1), "sport": "bike", "title": "Sweet spot", "duration_min": 75, "intensity": "Z3",
         "description": "3×12 min"},
        {"date": _iso(2), "sport": "run", "title": "Easy", "duration_min": 40},
        {"date": _iso(3), "sport": "rest", "title": "Odpoczynek"},
    ]})
    assert ids["added"] == 3
    r = gcal.finish_auth("code", state)            # connects, creates calendar, first sync
    assert r["ok"] and r["added"] == 2             # rest days are not exported
    assert gcal.connected() and "cal1" in google.calendars
    ev = next(e for e in google.events.values() if "Sweet spot" in e["summary"])
    assert ev["start"] == {"date": _iso(1)} and ev["end"] == {"date": _iso(2)}
    assert "75 min" not in ev["summary"] and "1 h 15 min" in ev["summary"] and "3×12 min" in ev["description"]

    # nothing changed -> no writes
    google.calls.clear()
    assert gcal.sync_now()["added"] == 0 and not [c for c in google.calls if c[0] != "GET"]

    sessions = {s["title"]: s for s in plan.plan_for_prompt()}
    plan.apply_ops({"upsert": [{"id": sessions["Sweet spot"]["id"], "date": _iso(4)},
                               {"id": sessions["Easy"]["id"], "status": "skipped"}]})
    r = gcal.sync_now()
    assert (r["updated"], r["deleted"]) == (1, 1)
    assert next(iter(google.events.values()))["start"] == {"date": _iso(4)}

    # un-skip: same event id comes back via 409 -> PUT
    plan.apply_ops({"upsert": [{"id": sessions["Easy"]["id"], "status": "planned"}]})
    r = gcal.sync_now()
    assert r["added"] == 1 and len(google.events) == 2

    # calendar deleted in Google -> recreated
    google.calendars.clear()
    google.events.clear()
    assert gcal.sync_now()["added"] == 2 and "cal1" in google.calendars


def test_gcal_revoked_token_and_schedule_guard(google, monkeypatch):
    store.write_config({"gcal_refresh_token": "rt"})
    google.token_error = "invalid_grant"
    r = gcal.sync_now()
    assert not r["ok"] and "połącz ponownie" in r["message"]
    assert not gcal.connected() and gcal.status()["last_error"]
    with pytest.raises(ValueError):
        gcal.save_client("zle-id", "x")


def test_plan_change_triggers_gcal_sync(coach_home, monkeypatch):
    calls = []
    monkeypatch.setattr(gcal, "schedule_sync", lambda delay=3.0: calls.append(1))
    plan.apply_ops({"upsert": [{"date": _iso(1), "sport": "swim"}]})
    plan.apply_ops({"upsert": []})
    assert calls == [1]


# --- push planned sessions to Garmin Connect -----------------------------------

from garmin_mcp.coach import garmin_push  # noqa: E402


class FakeWorkoutClient:
    def __init__(self):
        self.next_id, self.workouts, self.schedules, self.deleted, self.unscheduled = 100, {}, {}, [], []

    def upload_workout(self, w):
        self.next_id += 1
        self.workouts[self.next_id] = w
        return {"workoutId": self.next_id}

    def schedule_workout(self, wid, date):
        sid = wid + 5000
        self.schedules[sid] = (wid, date)
        return {"workoutScheduleId": sid}

    def unschedule_workout(self, sid):
        self.unscheduled.append(sid)
        self.schedules.pop(sid, None)

    def delete_workout(self, wid):
        self.deleted.append(wid)
        self.workouts.pop(wid, None)


INTERVALS = [
    {"type": "warmup", "duration_min": 15, "target": {"hr_zone": 2}},
    {"type": "repeat", "count": 3, "steps": [
        {"type": "interval", "duration_min": 12, "target": {"power_pct_ftp": [88, 92]}, "note": "sweet spot"},
        {"type": "recovery", "duration_min": 5, "target": {"hr_zone": 1}}]},
    {"type": "cooldown", "duration_min": 10},
]


def test_build_workout_targets():
    w = garmin_push.build_workout({"sport": "bike", "title": "SS", "steps": INTERVALS, "description": "x"})
    assert w["workoutName"] == "Trener: SS" and w["sportType"]["sportTypeId"] == 2
    steps = w["workoutSegments"][0]["workoutSteps"]
    assert [s["stepOrder"] for s in steps] == [1, 2, 5]
    rep = steps[1]
    assert rep["type"] == "RepeatGroupDTO" and rep["numberOfIterations"] == 3
    assert rep["endCondition"]["conditionTypeId"] == 7
    work = rep["workoutSteps"][0]
    assert work["targetType"]["workoutTargetTypeKey"] == "power.zone"
    assert (work["targetValueOne"], work["targetValueTwo"]) == (88.0, 92.0)
    assert work["endConditionValue"] == 720.0 and work["description"] == "sweet spot"
    assert steps[0]["zoneNumber"] == 2 and steps[2]["targetType"]["workoutTargetTypeId"] == 1

    run = garmin_push.build_workout({"sport": "run", "title": "T", "steps": [
        {"type": "interval", "distance_m": 1000, "target": {"pace_km": ["4:30", "4:20"]}}]})
    st = run["workoutSegments"][0]["workoutSteps"][0]
    assert st["endCondition"]["conditionTypeId"] == 3 and st["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert st["targetValueOne"] < st["targetValueTwo"] and abs(st["targetValueTwo"] - 1000 / 260) < 0.01

    swim = garmin_push.build_workout({"sport": "swim", "title": "CSS", "steps": [
        {"type": "repeat", "count": 10, "steps": [{"type": "interval", "distance_m": 100,
                                                   "target": {"pace_100m": "1:50"}}]}]})
    assert swim["poolLength"] == 25.0 and swim["sportType"]["sportTypeId"] == 4

    fb = garmin_push.build_workout({"sport": "run", "title": "Easy", "duration_min": 45, "intensity": "Z2"})
    one = fb["workoutSegments"][0]["workoutSteps"][0]
    assert one["endConditionValue"] == 2700.0 and one["zoneNumber"] == 2

    with pytest.raises(ValueError):
        garmin_push.build_workout({"sport": "run", "steps": [
            {"type": "interval", "target": {"pace_km": ["fast", "4:00"]}}]})


def test_garmin_push_sync(coach_home, monkeypatch):
    monkeypatch.setattr(garmin_push, "schedule_sync", lambda delay=4.0: None)
    monkeypatch.setattr(gcal, "schedule_sync", lambda delay=3.0: None)
    fake = FakeWorkoutClient()
    assert not garmin_push.sync_now(fake)["ok"]          # disabled by default
    garmin_push.set_enabled(True)

    plan.apply_ops({"upsert": [
        {"date": _iso(1), "sport": "bike", "title": "SS", "steps": INTERVALS},
        {"date": _iso(2), "sport": "run", "title": "Easy", "duration_min": 40, "intensity": "Z2"},
        {"date": _iso(3), "sport": "strength", "title": "Core"},          # not pushed
        {"date": _iso(30), "sport": "run", "title": "Daleko"},            # beyond 14 days
    ]})
    r = garmin_push.sync_now(fake)
    assert r["ok"] and r["added"] == 2 and len(fake.workouts) == 2
    assert sorted(d for _, d in fake.schedules.values()) == [_iso(1), _iso(2)]
    rows = {s["title"]: s for s in plan.plan_for_prompt(0, 40)}
    assert rows["SS"]["steps"][1]["count"] == 3
    assert set(garmin_push.status()["pushed_plan_ids"]) == {rows["SS"]["id"], rows["Easy"]["id"]}

    assert garmin_push.sync_now(fake)["added"] == 0 and not fake.deleted   # unchanged

    # move + change -> old workout removed, new one scheduled on the new date
    plan.apply_ops({"upsert": [{"id": rows["SS"]["id"], "date": _iso(4)}]})
    r = garmin_push.sync_now(fake)
    assert r["updated"] == 1 and len(fake.deleted) == 1 and len(fake.workouts) == 2
    assert _iso(4) in [d for _, d in fake.schedules.values()]

    # skip one, delete the other -> both removed from Garmin
    plan.apply_ops({"upsert": [{"id": rows["Easy"]["id"], "status": "skipped"}], "delete": [rows["SS"]["id"]]})
    r = garmin_push.sync_now(fake)
    assert r["removed"] == 2 and not fake.workouts

    # upload failure is reported, not fatal
    plan.apply_ops({"upsert": [{"date": _iso(1), "sport": "run", "title": "Zły", "steps": [
        {"type": "interval", "target": {"pace_km": ["x", "y"]}}]}]})
    r = garmin_push.sync_now(fake)
    assert not r["ok"] and "Zły" in r["errors"][0] and garmin_push.status()["last_error"]


def test_coach_prompt_documents_steps(coach_home):
    p = trainer.system_prompt()
    assert '"steps"' in p and "power_pct_ftp" in p and "{{" not in p
