"""Command-line interface for AgentTrace.

Usage:
    python3 -m agenttrace record [--name NAME] [--query QUERY] [--db PATH]
    python3 -m agenttrace record -- python3 examples/customer_support_agent.py
    python3 -m agenttrace replay RUN_ID [--mode exact|live|hybrid]
                       [--hybrid-tools TOOL,...] [--allow-side-effects]
    python3 -m agenttrace diff RUN_A RUN_B
    python3 -m agenttrace eval CASES.json|CASES.yaml [--ci] [--db PATH]
    python3 -m agenttrace export RUN_ID --out FILE
    python3 -m agenttrace import FILE [--db PATH]
    python3 -m agenttrace list
    python3 -m agenttrace timeline RUN_ID [--payloads]

``record`` runs the bundled demo customer-support agent (scripted, free,
deterministic) — or, with ``--`` followed by a Python script, runs that
script as a subprocess (it records itself with the SDK; see
``examples/customer_support_agent.py``) and stores its run in ``--db``.

``--db`` accepts a store directory or the ``agenttrace.db`` file inside
one. Bad input (missing case file, unknown run, malformed fixture)
prints a plain ``error: ...`` message and exits 2 — never a traceback.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from .demo_agent import FAKE_API_KEY, customer_support_agent
from .diff import diff_runs, render_diff
from .evals import CaseFileError, eval_exit_code, load_cases, run_eval
from .events import EventType
from .export import export_run
from .importer import import_run
from .model_stub import ScriptedModel
from .replay import ReplayRuntime
from .runtime import RecordRuntime
from .store import Store

DEFAULT_QUERY = "I want a refund for order ORD-1001"


def _store(db_path: str) -> Store:
    # --db accepts a store directory or the agenttrace.db file inside one.
    return Store(Store.resolve_base(db_path))


def _open_existing_store(db_path: str) -> Store:
    """Open a store that must already exist (read-only-ish commands).

    Creating an empty store directory as a side effect of a failed
    lookup (unknown run, missing store) would leave stray `.agenttrace/`
    directories behind, so these commands refuse a missing store
    instead of creating one.
    """
    base = Store.resolve_base(db_path)
    if not (base / "agenttrace.db").is_file():
        raise FileNotFoundError(f"no AgentTrace store at {base}")
    return Store(base)


def _print_error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def cmd_record(args) -> int:
    if args.script:
        return _record_script(args)
    store = _store(args.db)
    try:
        model = ScriptedModel(api_key=FAKE_API_KEY)
        started = time.perf_counter()
        with RecordRuntime(store, args.name, model=model,
                           api_key=FAKE_API_KEY,
                           metadata={"query": args.query}) as rt:
            answer = customer_support_agent(rt, args.query)
            run_id = rt.run_id
        events = store.get_events(run_id)
        cost = sum(e.meta.get("cost_usd", 0.0) for e in events
                   if e.type == EventType.MODEL_RESPONSE.value)
        print(f"recorded run {run_id}")
        print(f"  events: {len(events)}  latency: {(time.perf_counter() - started) * 1000:.1f} ms"
              f"  model cost: ${cost:.6f}")
        print(f"  final output: {answer}")
        return 0
    finally:
        store.close()


def _record_script(args) -> int:
    """Run an agent script as a subprocess; the script records itself.

    The store directory is passed via AGENTTRACE_DB so the script's run
    lands in the --db store the user asked for.
    """
    script = list(args.script)
    if script and script[0] in ("python", "python3", sys.executable):
        script = script[1:]
    if not script:
        _print_error("record -- <script>: no script given after '--'")
        return 2
    if args.query != DEFAULT_QUERY:
        print("note: --query is ignored when recording a script; "
              "the script controls its own query", file=sys.stderr)
    env = dict(os.environ)
    env["AGENTTRACE_DB"] = str(Store.resolve_base(args.db))
    result = subprocess.run([sys.executable, *script], env=env)
    return result.returncode


def cmd_replay(args) -> int:
    try:
        store = _open_existing_store(args.db)
    except FileNotFoundError as exc:
        _print_error(str(exc))
        return 2
    try:
        source = store.get_run(args.run_id)
        if source is None:
            _print_error(f"no such run: {args.run_id}")
            return 2
        query = source["metadata"].get("query", DEFAULT_QUERY)
        model = ScriptedModel(api_key=FAKE_API_KEY)
        hybrid = [t.strip() for t in args.hybrid_tools.split(",") if t.strip()] \
            if args.hybrid_tools else []
        with ReplayRuntime(store, args.run_id, mode=args.mode,
                           hybrid_tools=hybrid,
                           allow_side_effects=args.allow_side_effects,
                           model=model) as rt:
            answer = customer_support_agent(rt, query)
            new_run_id = rt.run_id
            report = rt.report
        print(f"replayed {args.run_id} in {args.mode} mode -> new run {new_run_id}")
        print(f"  mode: {report['mode']}  replayed calls: {report['replayed_calls']}"
              f"  live calls: {report['live_calls']}"
              f"  fallback live calls: {report['fallback_live_calls']}")
        print(f"  blocked side effects: {report['blocked_side_effects'] or 'none'}")
        print(f"  approved side effects: {report['approved_side_effects'] or 'none'}")
        print(f"  model stub calls during replay: {model.calls}")
        print(f"  final output: {answer}")
        return 0
    finally:
        store.close()


def cmd_diff(args) -> int:
    try:
        store = _open_existing_store(args.db)
    except FileNotFoundError as exc:
        _print_error(str(exc))
        return 2
    try:
        diff = diff_runs(store, args.run_a, args.run_b)
        print(render_diff(diff))
        return 0
    except ValueError as exc:
        _print_error(str(exc))
        return 2
    finally:
        store.close()


def cmd_eval(args) -> int:
    # Load (and validate) the cases before creating the store, so a bad
    # case file never leaves an empty store directory behind.
    try:
        cases = load_cases(args.cases)
    except CaseFileError as exc:
        _print_error(str(exc))
        return 2
    store = _store(args.db)
    try:
        report = run_eval(store, cases)
        for result in report["results"]:
            mark = "PASS" if result["ok"] else "FAIL"
            print(f"[{mark}] {result['name']} "
                  f"(latency {result['latency_ms']} ms, cost ${result['cost_usd']:.6f})")
            for check in result["checks"]:
                if not check["ok"]:
                    print(f"       failed check {check['name']}: "
                          f"expected {check['expected']!r}, got {check['actual']!r}")
        print(f"{report['passed']} passed, {report['failed']} failed")
        if args.ci:
            code = eval_exit_code(report)
            print(f"CI mode: exit {code}")
            return code
        return 0
    except CaseFileError as exc:
        _print_error(str(exc))
        return 2
    finally:
        store.close()


def cmd_export(args) -> int:
    try:
        store = _open_existing_store(args.db)
    except FileNotFoundError as exc:
        _print_error(str(exc))
        return 2
    try:
        out = export_run(store, args.run_id, args.out)
        print(f"exported {args.run_id} to {out}")
        return 0
    except ValueError as exc:
        _print_error(str(exc))
        return 2
    finally:
        store.close()


def cmd_import(args) -> int:
    store = _store(args.db)
    try:
        run_id = import_run(store, args.fixture)
        print(f"imported {args.fixture} as run {run_id}")
        return 0
    except ValueError as exc:
        _print_error(str(exc))
        return 2
    finally:
        store.close()


def cmd_list(args) -> int:
    try:
        store = _open_existing_store(args.db)
    except FileNotFoundError as exc:
        _print_error(str(exc))
        return 2
    try:
        runs = store.list_runs()
        if not runs:
            print("no runs recorded yet")
            return 0
        for run in runs:
            print(f"{run['id']}  {run['name']:<28} {run['mode']:<14} "
                  f"{run['status']:<10} {run['started_at']}")
        return 0
    finally:
        store.close()


def _short_payload(payload: dict, limit: int = 200) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def cmd_timeline(args) -> int:
    try:
        store = _open_existing_store(args.db)
    except FileNotFoundError as exc:
        _print_error(str(exc))
        return 2
    try:
        run = store.get_run(args.run_id)
        if run is None:
            _print_error(f"no such run: {args.run_id}")
            return 2
        print(f"run {run['id']} ({run['name']}, mode={run['mode']}, status={run['status']})")
        for event in store.get_events(args.run_id):
            dur = f" {event.duration_ms:.1f}ms" if event.duration_ms else ""
            print(f"  {event.seq:>3} {event.type:<16} {event.name:<16}"
                  f" {event.status}{dur}")
            if args.payloads:
                print(f"      in:  {_short_payload(event.input)}")
                print(f"      out: {_short_payload(event.output)}")
        if not args.payloads:
            print("  (use --payloads to show each event's input/output)")
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agenttrace",
        description="Record, replay, test, and debug AI-agent runs.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", help="record a demo agent run (or an agent script)")
    p.add_argument("--name", default="demo")
    p.add_argument("--query", default=DEFAULT_QUERY)
    p.add_argument("--db", default=".agenttrace")
    p.add_argument("script", nargs="*", help="optional script to run and record, "
                   "e.g. -- python3 examples/customer_support_agent.py")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("replay", help="replay a recorded run")
    p.add_argument("run_id")
    p.add_argument("--mode", choices=["exact", "live", "hybrid"], default="exact")
    p.add_argument("--hybrid-tools", default="")
    p.add_argument("--allow-side-effects", action="store_true")
    p.add_argument("--db", default=".agenttrace")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("diff", help="diff two runs")
    p.add_argument("run_a")
    p.add_argument("run_b")
    p.add_argument("--db", default=".agenttrace")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("eval", help="run regression cases (JSON or YAML)")
    p.add_argument("cases")
    p.add_argument("--ci", action="store_true")
    p.add_argument("--db", default=".agenttrace")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("export", help="export a run as a test fixture")
    p.add_argument("run_id")
    p.add_argument("--out", required=True)
    p.add_argument("--db", default=".agenttrace")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="import an exported fixture into the store")
    p.add_argument("fixture")
    p.add_argument("--db", default=".agenttrace")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("list", help="list recorded runs")
    p.add_argument("--db", default=".agenttrace")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("timeline", help="show a run's event timeline")
    p.add_argument("run_id")
    p.add_argument("--payloads", action="store_true",
                   help="show each event's input/output payloads (truncated)")
    p.add_argument("--db", default=".agenttrace")
    p.set_defaults(func=cmd_timeline)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError, KeyError, RecursionError, TypeError) as exc:
        # Last-resort guard for the "never a traceback" contract: any
        # failure the individual commands did not already turn into a
        # clean error still prints as `error: ...` with exit code 2.
        # (argparse errors exit 2 on their own; KeyboardInterrupt and
        # SystemExit are BaseExceptions and are not caught here.)
        _print_error(str(exc) or exc.__class__.__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
