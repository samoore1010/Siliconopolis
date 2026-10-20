"""CLI entry point.

  python -m pipeline run <researcher|creator|editor|qa|lister|analyst|fulfiller|daily>
  python -m pipeline run-all            # researcher -> creator -> editor -> qa -> lister -> analyst -> fulfiller
  python -m pipeline status
  python -m pipeline smoke
  python -m pipeline web                # village dashboard, /health, /status, Etsy OAuth callback
  python -m pipeline demo               # mock-mode loop to watch the village animate

Exit codes: 0 ok, 1 failure, 3 cap reached (so Railway cron shows failure/red when something is off).
"""

from __future__ import annotations

import argparse
import json
import sys

from pipeline.config import get_settings
from pipeline.logging_setup import configure_logging, get_logger

log = get_logger("pipeline.cli")


def _run_agents(names: list[str], *, stop_on_failure: bool) -> int:
    from pipeline.agents import get_agent
    from pipeline.db.session import dispose_engine

    worst = 0
    try:
        for name in names:
            outcome = get_agent(name).run()
            log.info("agent outcome", agent=name, status=outcome.status, items=outcome.items_processed,
                     cost_usd=round(outcome.cost_usd, 4), exit_code=outcome.exit_code)
            worst = max(worst, outcome.exit_code)
            if outcome.exit_code == 1 and stop_on_failure:
                break
    finally:
        dispose_engine()  # cron services must leave no open connections
    return worst


def cmd_run(args) -> int:
    from pipeline.agents import AGENT_NAMES, DAILY_CHAIN

    if args.agent == "daily":
        return _run_agents(list(DAILY_CHAIN), stop_on_failure=False)
    if args.agent not in AGENT_NAMES:
        print(f"unknown agent {args.agent!r}; choose from: {', '.join(AGENT_NAMES)}, daily", file=sys.stderr)
        return 2
    return _run_agents([args.agent], stop_on_failure=True)


def cmd_run_all(args) -> int:
    from pipeline.agents import AGENT_NAMES

    return _run_agents(list(AGENT_NAMES), stop_on_failure=False)


def cmd_status(args) -> int:
    from pipeline.commands.status import collect_status, format_status
    from pipeline.db.session import dispose_engine

    try:
        status = collect_status()
    finally:
        dispose_engine()
    print(json.dumps(status, indent=2) if args.json else format_status(status))
    return 0


def cmd_smoke(args) -> int:
    from pipeline.commands.smoke import run_smoke
    from pipeline.db.session import dispose_engine

    try:
        ok, results = run_smoke()
    finally:
        dispose_engine()
    if args.json:
        print(json.dumps({"ok": ok, "checks": results}, indent=2))
    else:
        for r in results:
            mark = "SKIP" if r.get("skipped") else ("OK  " if r["ok"] else "FAIL")
            detail = r.get("skipped") or r.get("error") or json.dumps(r.get("detail"))
            print(f"[{mark}] {r['check']:<15} {detail}")
        print("\nsmoke:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_web(args) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run("pipeline.web.app:app", host="0.0.0.0", port=args.port or settings.port, log_level="info")
    return 0


def cmd_demo(args) -> int:
    from pipeline.commands.demo import DemoRefused, run_demo

    try:
        return run_demo(cycles=args.cycles, sleep_seconds=args.sleep, reset=args.reset)
    except DemoRefused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2


def cmd_migrate(args) -> int:
    from alembic.config import Config

    from alembic import command
    from pipeline.config import PROJECT_ROOT

    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="live reads, no external writes (same as DRY_RUN=true)")
    p.add_argument("--log-format", choices=["json", "text"], help="override LOG_FORMAT")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one agent (or 'daily' = creator -> qa -> lister)")
    r.add_argument("agent")
    r.set_defaults(fn=cmd_run)

    ra = sub.add_parser("run-all", help="run every agent once, in pipeline order")
    ra.set_defaults(fn=cmd_run_all)

    st = sub.add_parser("status", help="counts by state, recent runs, spend")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_status)

    sm = sub.add_parser("smoke", help="connectivity checks (DB, storage, Anthropic, Etsy/Printify/images)")
    sm.add_argument("--json", action="store_true")
    sm.set_defaults(fn=cmd_smoke)

    w = sub.add_parser("web", help="start the optional web service")
    w.add_argument("--port", type=int, default=None)
    w.set_defaults(fn=cmd_web)

    d = sub.add_parser("demo", help="mock-mode loop that animates the village (refuses in production)")
    d.add_argument("--cycles", type=int, default=3)
    d.add_argument("--sleep", type=float, default=4.0, help="seconds between agent runs")
    d.add_argument("--reset", action="store_true", help="wipe products/runs/events first")
    d.set_defaults(fn=cmd_demo)

    m = sub.add_parser("migrate", help="alembic upgrade head")
    m.set_defaults(fn=cmd_migrate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        import os

        os.environ["DRY_RUN"] = "true"
        from pipeline.config import reset_settings_cache

        reset_settings_cache()
    settings = get_settings()
    configure_logging(settings.log_level, args.log_format or settings.log_format)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
