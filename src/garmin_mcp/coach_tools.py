"""Coach tools: compact, pre-computed context for an AI triathlon coach.

These read the local store filled by ``garmin-coach-sync`` / ``garmin-dashboard``
so the model gets weeks of history in one small call instead of dozens of
raw Garmin requests.
"""

import json

from garmin_mcp.coach import body, metrics, plan, progress, store, sync

garmin_client = None


def configure(client):
    global garmin_client
    garmin_client = client


def register_tools(app):
    @app.tool()
    async def get_coach_snapshot(days: int = 28) -> str:
        """Triathlon coach context in one call: athlete profile and goals, rule-based
        readiness signals, fitness/fatigue/form (CTL/ATL/TSB) overall and per sport,
        16 weeks of swim/bike/run volume, recent daily HRV/sleep/resting HR/body battery,
        the last 30 activities, progress/regression per sport, the current training plan
        (shared with the local dashboard) and body-composition trends from analyser scans. Read this FIRST before giving training advice.

        Args:
            days: How many recent days of daily series to include (default 28, max 90).
        """
        try:
            days = max(7, min(int(days), 90))
            snap = metrics.build_snapshot(days=max(days, 30))
            out = metrics.compact_snapshot(snap, days)
            out["training_plan"] = plan.plan_for_prompt()
            prog = progress.build_progress()
            for s in prog["sports"]:
                s["weekly"] = [w["value"] for w in s["weekly"]]
            for v in prog["vo2max"].values():
                v.pop("series", None)
            out["progress_4w_vs_prev_4w"] = prog
            out["body_composition"] = body.for_prompt()
            return json.dumps(out, ensure_ascii=False, default=str)
        except Exception as e:
            return f"Error: {e}"

    @app.tool()
    async def sync_coach_data() -> str:
        """Pull the latest data from Garmin Connect into the local coach store
        (today, yesterday, new activities and a few missing history days).
        Use when the snapshot's last_sync is old or the athlete just finished a session."""
        try:
            result = sync.sync(log=lambda _m: None)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return f"Error: {e}"

    @app.tool()
    async def update_athlete_profile(markdown: str) -> str:
        """Replace the athlete profile (goals, race dates, weekly hours, thresholds,
        injuries, preferences). Only call when the athlete explicitly asks to save
        changes to their profile; pass the complete updated Markdown.

        Args:
            markdown: Full new content of the profile file.
        """
        try:
            path = store.profile_path()
            path.write_text(markdown, encoding="utf-8")
            return f"Saved profile to {path}"
        except Exception as e:
            return f"Error: {e}"

    @app.tool()
    async def get_body_scans() -> str:
        """All body-composition measurements the athlete imported from photos of analyser
        printouts (InBody, Tanita, …): weight, body fat, skeletal muscle, lean mass, water,
        visceral fat, BMR, segmental lean/fat mass, plus trends and signals (weight-loss rate,
        muscle change, leg asymmetry). Bioimpedance varies with hydration — judge trends."""
        try:
            rows = body.list_scans()
            if not rows:
                return "No body-composition measurements imported yet."
            for r in rows:
                r.pop("files", None)
            return json.dumps({"measurements": rows, "analysis": body.analyse(rows)},
                              ensure_ascii=False, default=str)
        except Exception as e:
            return f"Error: {e}"

    @app.tool()
    async def get_training_plan(days_back: int = 7, days_ahead: int = 28) -> str:
        """The athlete's training plan (shared with the local dashboard), each session with
        id, date, sport, title, duration, intensity, description and state
        (planned/today/done/missed/skipped) matched against completed Garmin activities.

        Args:
            days_back: Past days to include (default 7).
            days_ahead: Future days to include (default 28).
        """
        try:
            rows = plan.plan_for_prompt(max(0, int(days_back)), max(1, int(days_ahead)))
            return json.dumps(rows, ensure_ascii=False) if rows else "Plan is empty."
        except Exception as e:
            return f"Error: {e}"

    @app.tool()
    async def update_training_plan(upsert: list[dict] | None = None, delete: list[int] | None = None) -> str:
        """Add, change, move or remove sessions in the athlete's training plan (shown in the
        dashboard calendar). Check get_training_plan first to avoid duplicates.

        Args:
            upsert: Sessions to add or change. New: {"date": "YYYY-MM-DD", "sport":
                "swim|bike|run|strength|brick|rest", "title", "duration_min", "distance_km",
                "intensity" (e.g. "Z2"), "description" (warm-up / main set with zones / cool-down),
                "name_en" (short English workout name for the watch, e.g. "Sweet Spot 3x12"),
                "steps" (structured workout for the Garmin watch, swim/bike/run; step notes in English): list of
                {"type": "warmup|interval|recovery|rest|cooldown", "duration_min" or "distance_m",
                "target": {"hr_zone": 1-5 | "hr_bpm": [lo, hi] | "power_pct_ftp": [lo, hi] |
                "power_zone": 1-7 | "power_w": [lo, hi] | "pace_km": ["5:10", "5:00"] |
                "pace_100m": ["1:55", "1:50"]}, "note"} or {"type": "repeat", "count": n, "steps": [...]}}.
                If the athlete enabled Garmin push, upcoming sessions are uploaded to Garmin
                Connect and scheduled on their date automatically.
                Change existing: include its "id" and only the changed fields; move = new "date";
                "status" may be "planned", "skipped" or "done".
            delete: Session ids to remove.
        """
        try:
            result = plan.apply_ops({"upsert": upsert or [], "delete": delete or []}, source="mcp")
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return f"Error: {e}"

    return app
