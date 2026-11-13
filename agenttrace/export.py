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
    # Retroactive learning: prime the redactor with the run's persisted
    # learned secrets *before* building the payload, so a secret that
    # first appeared in free text and was only later seen under a
    # sensitive key is scrubbed from the export too — even when a
    # freshly reopened store exports a run it never recorded.
    redactor = redactor or getattr(store, "redactor", None) or Redactor()
    for digest, length in store.load_learned_secrets(run_id):
        redactor.register_hashed_secret(digest, length)
    # Re-redact the run's artifact files with the same set, so the
    # exported fixture and the files on disk agree.
    store.scrub_run_artifacts(run_id)
    events = store.get_events(run_id)
    payload = {
        "agenttrace_version": 1,
        "run": run,
        "events": [event.to_dict() for event in events],
    }
    # Reuse the store's redactor when no explicit one is given, so secrets
    # learned during recording are scrubbed from the export too.
    payload = redactor.redact(payload)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(out)
