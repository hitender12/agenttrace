"""Local-first recorder: SQLite run/event store + artifact directory.

Runs are persisted to ``<base>/agenttrace.db``; request/response payloads
larger than :data:`ARTIFACT_THRESHOLD` are stored as JSON files under
``<base>/artifacts/<run_id>/`` and referenced from the row, so the database
stays small while payloads remain inspectable on disk.

Every payload is redacted before it is written, so a secret recorded by
mistake never lands in the database file or an artifact file.

Learning is retroactive: secrets the redactor learns during a run are
persisted per run (``learned_secrets`` table), and when the run
finishes, :meth:`Store.finish_run` re-redacts the run's stored
payloads — including artifact files — with the run's full learned
set. A secret that first appears in free text and is only later seen
under a sensitive key is therefore scrubbed from the earlier
occurrence too, and a freshly reopened store re-redacts from the
persisted set on export, so the fix survives a restart.

The persisted set stores only each secret's **SHA-256 digest and
length, never the secret itself** — the database file contains no
recoverable secret, yet a fresh store can still scrub it (the
redactor matches windows of the stored length against the digest).
``PRAGMA secure_delete=ON`` is set on every connection so that bytes
a re-redaction frees (e.g. a secret an earlier write stored verbatim)
are zeroed instead of lingering in free pages.
"""
from __future__ import annotations

import hashlib
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
        try:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            # Zero freed content instead of leaving it in free pages:
            # a secret an earlier write stored verbatim must not linger
            # in the file after a re-redaction overwrites it.
            self._conn.execute("PRAGMA secure_delete=ON")
            self._init_schema()
        except sqlite3.Error as exc:
            # A corrupt or unreadable database must be a plain,
            # catchable error — never a traceback at the CLI.
            raise ValueError(
                f"store at {self.base} is not a valid AgentTrace "
                f"database: {exc}") from exc

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
            -- Secrets learned during a run, persisted per run so that
            -- learning is retroactive: a fresh Store reopening this
            -- database can re-redact earlier occurrences on export.
            -- Only the SHA-256 digest and length are stored, never
            -- the secret itself: the file contains no recoverable
            -- secret, yet a fresh store can still scrub it.
            CREATE TABLE IF NOT EXISTS learned_secrets (
                run_id TEXT NOT NULL,
                secret_hash TEXT NOT NULL,
                secret_len INTEGER NOT NULL,
                PRIMARY KEY (run_id, secret_hash)
            );
            """
        )
        self._conn.commit()

    # -- learned secrets (retroactive learning) -------------------------
    def _persist_learned_secrets(self, run_id: str, redactor=None) -> None:
        """Persist the redactor's learned secrets for ``run_id``.

        Only digests and lengths are stored — never the secret itself
        (see the table comment above). Called after every write so a
        crash mid-run still leaves the secrets learned so far on disk
        for a later export.
        """
        redactor = redactor or self.redactor
        known = redactor.known_secrets
        if not known:
            return
        rows = []
        for secret in known:
            digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
            rows.append((run_id, digest, len(secret)))
        self._conn.executemany(
            "INSERT OR IGNORE INTO learned_secrets"
            " (run_id, secret_hash, secret_len) VALUES (?, ?, ?)",
            rows)
        self._conn.commit()

    def load_learned_secrets(self, run_id: str) -> set:
        """(digest, length) pairs learned during ``run_id`` (persisted).

        Survives a restart; the secret itself is not recoverable from
        these, but a redactor primed with them can still scrub it.
        """
        rows = self._conn.execute(
            "SELECT secret_hash, secret_len FROM learned_secrets"
            " WHERE run_id = ?", (run_id,)).fetchall()
        return {(row[0], row[1]) for row in rows}

    def _scrub_artifact_file(self, rel: str) -> None:
        """Re-redact one artifact file in place with the current redactor."""
        path = self.artifacts_dir / rel
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        clean = self.redactor.redact(content)
        path.write_text(json.dumps(clean, ensure_ascii=False),
                        encoding="utf-8")

    def scrub_run_artifacts(self, run_id: str) -> None:
        """Re-redact a run's artifact files with its persisted learned set.

        Used on export (and by :meth:`_rescrub_run`): the artifact file
        is rewritten in place, so its database reference stays valid.
        """
        for digest, length in self.load_learned_secrets(run_id):
            self.redactor.register_hashed_secret(digest, length)
        rows = self._conn.execute(
            "SELECT input_json, output_json, meta_json FROM events"
            " WHERE run_id = ?", (run_id,)).fetchall()
        for row in rows:
            for field in ("input_json", "output_json", "meta_json"):
                try:
                    data = json.loads(row[field])
                except ValueError:
                    continue
                if isinstance(data, dict) and "$artifact" in data:
                    self._scrub_artifact_file(data["$artifact"])

    def _rescrub_payload_text(self, run_id: str, event_id: str,
                              field: str, text: str) -> str:
        """Re-redact one stored payload with the full learned set."""
        try:
            data = json.loads(text)
        except ValueError:
            return text
        if isinstance(data, dict) and "$artifact" in data:
            # Artifact: rewrite the file in place; the reference is
            # unchanged, so the stored text stays valid.
            self._scrub_artifact_file(data["$artifact"])
            return text
        clean = self.redactor.redact(data)
        new_text = json.dumps(clean, ensure_ascii=False)
        if len(new_text) > ARTIFACT_THRESHOLD:
            return self._store_payload(run_id, event_id, field, clean)
        return new_text

    def _rescrub_run(self, run_id: str) -> None:
        """Re-redact a finished run with its full learned-secret set.

        Catches every occurrence a forward-only pass missed: a secret
        that first appeared in free text and was only later seen under
        a sensitive key is scrubbed from the earlier rows, artifact
        files, run name, and metadata. Idempotent — running it twice
        changes nothing the second time.
        """
        self._persist_learned_secrets(run_id)
        for digest, length in self.load_learned_secrets(run_id):
            self.redactor.register_hashed_secret(digest, length)
        run = self.get_run(run_id)
        if run is not None:
            clean_name = self.redactor.redact_text(str(run["name"]))
            clean_meta = self.redactor.redact(run.get("metadata") or {})
            self._conn.execute(
                "UPDATE runs SET name = ?, metadata_json = ? WHERE id = ?",
                (clean_name, json.dumps(clean_meta), run_id))
        rows = self._conn.execute(
            "SELECT id, type, name, input_json, output_json, meta_json"
            " FROM events WHERE run_id = ?", (run_id,)).fetchall()
        for row in rows:
            updates = {}
            clean_type = self.redactor.redact_text(str(row["type"]))
            clean_name = self.redactor.redact_text(str(row["name"]))
            if clean_type != row["type"]:
                updates["type"] = clean_type
            if clean_name != row["name"]:
                updates["name"] = clean_name
            for field in ("input_json", "output_json", "meta_json"):
                new_text = self._rescrub_payload_text(
                    run_id, row["id"], field[:-5], row[field])
                if new_text != row[field]:
                    updates[field] = new_text
            if updates:
                self._conn.execute(
                    "UPDATE events SET "
                    + ", ".join(f"{k} = ?" for k in updates)
                    + " WHERE id = ?",
                    (*updates.values(), row["id"]))
        self._conn.commit()
        self._persist_learned_secrets(run_id)

    # -- runs -----------------------------------------------------------
    def create_run(self, name: str, mode: str = "record",
                   metadata: dict | None = None, replay_of: str | None = None) -> str:
        run_id = "run_" + secrets.token_hex(8)
        # Run names are user-controlled free text: redact them like any
        # other payload, so a secret used as a run name never lands raw
        # in the database.
        clean_name = self.redactor.redact_text(str(name))
        # Run metadata is persisted too: redact it (and learn any secret
        # it carries) before it reaches the database.
        clean_metadata = self.redactor.redact(metadata or {})
        self._conn.execute(
            "INSERT INTO runs (id, name, mode, replay_of, status, started_at, metadata_json)"
            " VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (run_id, clean_name, mode, replay_of, now_iso(),
             json.dumps(clean_metadata)),
        )
        self._conn.commit()
        self._persist_learned_secrets(run_id)
        return run_id

    def finish_run(self, run_id: str, status: str = "completed") -> None:
        # Retroactive learning: re-redact the run with its full learned
        # set before marking it finished, so earlier occurrences of a
        # secret learned late in the run are scrubbed too.
        self._rescrub_run(run_id)
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
        # Event type/name are free text too (an error event is named by
        # its caller): redact them like the payloads, so a secret used
        # as an event name never lands raw in the database.
        clean_type = self.redactor.redact_text(str(event.type))
        clean_name = self.redactor.redact_text(str(event.name))
        self._conn.execute(
            "INSERT INTO events (id, run_id, seq, trace_id, span_id, parent_span_id,"
            " type, name, started_at, ended_at, duration_ms, status,"
            " input_json, output_json, meta_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id, run_id, event.seq, event.trace_id, event.span_id,
                event.parent_span_id, clean_type, clean_name,
                event.started_at, event.ended_at, event.duration_ms, event.status,
                self._store_payload(run_id, event.id, "input", event.input),
                self._store_payload(run_id, event.id, "output", event.output),
                self._store_payload(run_id, event.id, "meta", event.meta),
            ),
        )
        self._conn.commit()
        self._persist_learned_secrets(run_id)
        return event.id

    def get_events(self, run_id: str) -> list[Event]:
        # Prime the redactor with this run's persisted learned secrets,
        # so a freshly reopened store still scrubs them from anything
        # it writes next (replay, export) — retroactive learning
        # survives a restart.
        for digest, length in self.load_learned_secrets(run_id):
            self.redactor.register_hashed_secret(digest, length)
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
