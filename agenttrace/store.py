"""Local-first recorder: SQLite run/event store + artifact directory.

Runs are persisted to ``<base>/agenttrace.db``; request/response payloads
larger than :data:`ARTIFACT_THRESHOLD` are stored as JSON files under
``<base>/artifacts/<run_id>/`` and referenced from the row, so the database
stays small while payloads remain inspectable on disk.

Every payload is redacted before it is written, so a secret recorded by
mistake never lands in the database file or an artifact file.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path

from .events import Event, now_iso
from .redaction import Redactor

ARTIFACT_THRESHOLD = 4096


class Store:
    def __init__(self, base_dir, redactor: Redactor | None = None):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = self.base / "artifacts"
        self.artifacts_dir.mkdir(exist_ok=True)
        self.db_path = self.base / "agenttrace.db"
        self.redactor = redactor or Redactor()
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    @staticmethod
    def resolve_base(path) -> Path:
        """Resolve a ``--db`` argument to a store directory.

        Accepts a directory (the store) or a path to the ``agenttrace.db``
        file inside a store — the file's parent directory is used.
        """
        p = Path(path)
        if p.suffix == ".db" or (p.is_file() and not p.is_dir()):
            return p.parent
        return p

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mode TEXT NOT NULL,
                replay_of TEXT,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                seq INTEGER NOT NULL,
                trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL,
                parent_span_id TEXT,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_ms REAL,
                status TEXT NOT NULL,
                input_json TEXT NOT NULL DEFAULT '{}',
                output_json TEXT NOT NULL DEFAULT '{}',
                meta_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);
            """
        )
        self._conn.commit()

    # -- runs -----------------------------------------------------------
    def create_run(self, name: str, mode: str = "record",
                   metadata: dict | None = None, replay_of: str | None = None) -> str:
        run_id = "run_" + secrets.token_hex(8)
        # Run metadata is persisted too: redact it (and learn any secret
        # it carries) before it reaches the database.
        clean_metadata = self.redactor.redact(metadata or {})
        self._conn.execute(
            "INSERT INTO runs (id, name, mode, replay_of, status, started_at, metadata_json)"
            " VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (run_id, name, mode, replay_of, now_iso(),
             json.dumps(clean_metadata)),
        )
        self._conn.commit()
        return run_id

    def finish_run(self, run_id: str, status: str = "completed") -> None:
        self._conn.execute(
            "UPDATE runs SET status = ?, ended_at = ? WHERE id = ?",
            (status, now_iso(), run_id),
        )
        self._conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["metadata"] = json.loads(d.pop("metadata_json"))
        return d

    def list_runs(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC"
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d.pop("metadata_json"))
            out.append(d)
        return out

    # -- events ---------------------------------------------------------
    def _store_payload(self, run_id: str, event_id: str, field: str, payload: dict) -> str:
        payload = self.redactor.redact(payload)
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > ARTIFACT_THRESHOLD:
            rel = f"{run_id}/{event_id}_{field}.json"
            path = self.artifacts_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return json.dumps({"$artifact": rel})
        return text

    def _load_payload(self, text: str):
        data = json.loads(text)
        if isinstance(data, dict) and "$artifact" in data:
            path = self.artifacts_dir / data["$artifact"]
            return json.loads(path.read_text(encoding="utf-8"))
        return data

    def add_event(self, run_id: str, event: Event) -> str:
        self._conn.execute(
            "INSERT INTO events (id, run_id, seq, trace_id, span_id, parent_span_id,"
            " type, name, started_at, ended_at, duration_ms, status,"
            " input_json, output_json, meta_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id, run_id, event.seq, event.trace_id, event.span_id,
                event.parent_span_id, event.type, event.name,
                event.started_at, event.ended_at, event.duration_ms, event.status,
                self._store_payload(run_id, event.id, "input", event.input),
                self._store_payload(run_id, event.id, "output", event.output),
                self._store_payload(run_id, event.id, "meta", event.meta),
            ),
        )
        self._conn.commit()
        return event.id

    def get_events(self, run_id: str) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY seq ASC", (run_id,)
        ).fetchall()
        events = []
        for row in rows:
            d = dict(row)
            events.append(Event(
                id=d["id"], run_id=d["run_id"], seq=d["seq"],
                trace_id=d["trace_id"], span_id=d["span_id"],
                parent_span_id=d["parent_span_id"], type=d["type"],
                name=d["name"], started_at=d["started_at"],
                ended_at=d["ended_at"], duration_ms=d["duration_ms"],
                status=d["status"],
                input=self._load_payload(d["input_json"]),
                output=self._load_payload(d["output_json"]),
                meta=self._load_payload(d["meta_json"]),
            ))
        return events

    def close(self) -> None:
        self._conn.close()
