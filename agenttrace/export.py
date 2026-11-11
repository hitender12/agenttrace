"""Dataset export: selected runs as portable test fixtures (JSON).

The export is redacted a second time on the way out, so a fixture shared
with a teammate never carries a secret that was recorded by mistake.
"""
from __future__ import annotations

import json
from pathlib import Path

from .redaction import Redactor
from .store import Store


def export_run(store: Store, run_id: str, out_path,
               redactor: Redactor | None = None) -> str:
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"no such run: {run_id}")
    events = store.get_events(run_id)
    payload = {
        "agenttrace_version": 1,
        "run": run,
        "events": [event.to_dict() for event in events],
    }
    payload = (redactor or Redactor()).redact(payload)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(out)
