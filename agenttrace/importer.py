"""Dataset import: read an exported fixture back into a store.

The counterpart of :mod:`agenttrace.export` (spec M5 export/import).
A fixture is a JSON document ``{agenttrace_version, run, events}``.
Importing re-redacts everything on the way in, so a fixture that was
hand-edited (or exported by an older version) cannot smuggle a secret
into the database.

If the fixture's run id already exists in the store, a fresh id is
generated and every event is remapped to it, so importing the same
fixture twice never collides and never overwrites.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from .events import Event
from .redaction import Redactor
from .store import Store


def import_run(store: Store, fixture_path,
               redactor: Redactor | None = None) -> str:
    """Import an exported fixture into ``store``; return the run id used."""
    try:
        data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"no such fixture file: {fixture_path}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"fixture {fixture_path} is not valid JSON: {exc}")
    if not isinstance(data, dict) or "run" not in data or "events" not in data:
        raise ValueError(
            f"fixture {fixture_path} is not an AgentTrace export "
            "(expected keys: run, events)")
    if data.get("agenttrace_version") != 1:
        raise ValueError(
            f"fixture {fixture_path} has unsupported agenttrace_version: "
            f"{data.get('agenttrace_version')!r} (expected 1)")

    redactor = redactor or getattr(store, "redactor", None) or Redactor()
    run = redactor.redact(dict(data["run"]))
    events = [redactor.redact(dict(e)) for e in data["events"]]

    run_id = run.get("id") or ("run_" + secrets.token_hex(8))
    if store.get_run(run_id) is not None:
        # Collision: keep the fixture's data, mint a fresh id.
        new_id = "run_" + secrets.token_hex(8)
        for event in events:
            if event.get("run_id") == run_id:
                event["run_id"] = new_id
        run_id = new_id
    # Event ids are unique per store: mint fresh ones for any event id
    # that already exists (importing the same fixture twice).
    existing_ids = {row[0] for row in
                    store._conn.execute("SELECT id FROM events")}
    for event in events:
        if event.get("id") in existing_ids:
            event["id"] = (f"evt_{int(event.get('seq', 0)):04d}_"
                           f"{secrets.token_hex(4)}")

    store._conn.execute(
        "INSERT INTO runs (id, name, mode, replay_of, status, started_at,"
        " ended_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, run.get("name", "imported"),
         run.get("mode", "imported"), run.get("replay_of"),
         run.get("status", "completed"), run.get("started_at", ""),
         run.get("ended_at"), json.dumps(run.get("metadata", {}))),
    )
    for raw in events:
        event = Event.from_dict({**raw, "run_id": run_id})
        store._conn.execute(
            "INSERT INTO events (id, run_id, seq, trace_id, span_id,"
            " parent_span_id, type, name, started_at, ended_at, duration_ms,"
            " status, input_json, output_json, meta_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.id, run_id, event.seq, event.trace_id, event.span_id,
             event.parent_span_id, event.type, event.name,
             event.started_at, event.ended_at, event.duration_ms,
             event.status, json.dumps(event.input), json.dumps(event.output),
             json.dumps(event.meta)),
        )
    store._conn.commit()
    return run_id
