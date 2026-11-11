# Security Policy

## Reporting a vulnerability

**Do not open a public GitHub issue for a security vulnerability.**

Report it privately through GitHub's private vulnerability reporting
for this repository (Security tab → "Report a vulnerability"), or
contact a maintainer directly on GitHub. Include:

- a description of the vulnerability and what it affects (recorder,
  replay engine, redaction, export, eval/CI, CLI);
- steps to reproduce, with the smallest possible agent or fixture;
- the impact you believe it has — for example, a secret that survives
  redaction, a side-effecting tool that executes on replay without
  approval, or a fixture that leaks data it should not;
- whether you have already disclosed it anywhere else.

We will acknowledge the report, investigate, and keep you informed.
Please give us a reasonable time to fix the issue before disclosing it
publicly, and do not use the vulnerability against other users' data
while it is being fixed. If the vulnerability is confirmed, we will
fix it, add a regression test, and credit you in the release notes
unless you prefer to stay anonymous.

## Supported versions

| Version | Supported with security fixes |
| ------- | ----------------------------- |
| 0.1.x   | Yes (current release)         |
| < 0.1   | No                            |

AgentTrace is at v0.1.0 (see `CHANGELOG.md`). Only the latest 0.1.x
release receives security fixes. There is no long-term-support branch
and no server deployment to patch — this is a local-first Python
reference implementation, and the supported version is the code in
this repository.

## Scope and honest limits

AgentTrace is a local debugging and regression-testing tool, not a
security boundary. Do not rely on it as one:

- **Local storage has no access control.** Runs live in a single
  SQLite file (`.agenttrace/agenttrace.db`, by default) with no
  multi-user access control, no encryption at rest, and no
  authentication. Anyone who can read that directory can read every
  recorded run. Do not record runs on a shared machine or in a shared
  directory.
- **Redaction is best-effort, not a guarantee.** See the trace-data
  sensitivity section below. Redaction catches known secret shapes;
  it cannot recognise every credential format, and it cannot know
  that a particular model output or tool result is sensitive unless
  you tell it.
- **Replay is not a sandbox.** Exact replay returns recorded outputs
  and never invokes the model stub, but live and hybrid replay execute
  the current tool stubs, and `--allow-side-effects` executes
  side-effecting tools for real. Replay a run only in an environment
  where that is safe.
- **No production-security claim.** This project has not had an
  independent security review. The Rust engine, OTLP ingestion,
  TypeScript SDK, MCP integration, PostgreSQL server mode, and
  dashboard described in `ARCHITECTURE.md` are not built here, so
  their security properties are not in scope for this policy.

## Trace-data sensitivity

Recorded runs are sensitive by nature. A single run can contain all
of the following, and you should treat it as confidential data:

- **Secrets:** API keys, tokens, passwords, and authorization headers
  that travelled with a model request or a tool call. In this
  reference implementation the demo deliberately records a fake API
  key in the model request headers to prove redaction works — a real
  agent can do the same thing by accident with a real key.
- **Model outputs:** generated text may repeat or paraphrase user
  input, retrieved documents, or confidential content the model was
  given. A recorded `model_response` is not safe to share just
  because it is "the model's words".
- **Tool results and retrieved documents:** order details, customer
  data, internal knowledge-base text, and anything a tool returned.
  The demo's `get_order` tool returns order totals and items; a real
  tool can return far more.
- **User input:** the original query, including names, order IDs,
  email addresses, and anything else the user typed.

### Redaction rules (what the code does)

Redaction is applied in two layers, in `agenttrace/redaction.py`,
and at two points in the pipeline:

1. **On write** — `Store.add_event` redacts every payload before it
   is written to SQLite or to an artifact file, so a secret never
   lands in the database file.
2. **On export** — `export_run` redacts the payload a second time on
   the way out, so an exported fixture is scrubbed even if it was
   recorded before a redaction rule existed.

The rules:

- **Key-based:** any field whose name (case-insensitive) is in
  `SENSITIVE_KEYS` is replaced with `[REDACTED]`: `api_key`,
  `apikey`, `password`, `passwd`, `secret`, `client_secret`,
  `token`, `access_token`, `refresh_token`, `authorization`,
  `bearer`, `private_key`. Extend with
  `Redactor(extra_keys=[...])`.
- **Pattern-based:** secret-shaped values in free text are replaced:
  `sk-...` API keys, `ghp_...` GitHub tokens, `AKIA...` AWS access
  keys, `xox...` Slack tokens, and `Bearer ...` headers. Extend
  with `Redactor(extra_patterns=[(regex, replacement), ...])`.
- **Custom redactor:** pass your own `Redactor` to the store —
  `Store(".agenttrace", redactor=Redactor(extra_keys=["ssn"],
  extra_patterns=[...]))` — before recording anything sensitive.

### What redaction does not do

- It does not redact values under an unlisted key name. If your tool
  returns a credential under a field called `credential_value` or
  `x_api_key_v2`, add that key to `extra_keys` before recording.
- It does not recognise every credential format. A base64 blob, a
  JWT, a database connection string, or a vendor-specific token may
  pass through untouched unless you add a pattern for it.
- It does not judge content sensitivity. A customer's address, a
  medical note, or an internal document returned by a tool is not a
  "secret" by shape, so it is recorded and exported as-is. If your
  agent handles personal or regulated data, add the relevant field
  names to `extra_keys` or do not record those runs at all.
- It cannot un-record a secret. If a secret was recorded before a
  rule existed, or under a key the rules miss, it is in the database
  file. Rotate the credential, delete the affected run data, and
  re-record.

### Safe handling of recorded runs and exported fixtures

- **Keep `.agenttrace/` local.** It is scratch data, not something to
  commit, email, or upload. Do not add it to a public repository, a
  shared drive, or a support ticket.
- **Check before you share.** Before attaching an exported fixture
  to an issue, a PR, or a message, open the JSON and search it for
  secrets, names, email addresses, and any data you would not want
  public. Exports are redacted a second time, but the check is still
  yours — redaction is best-effort (see above).
- **Prefer a minimal fixture.** When reporting a bug, export the
  smallest run that reproduces it, or hand-write a minimal fixture
  in the shape of `export_run` output
  (`{agenttrace_version, run, events}`) with synthetic data, instead
  of sharing a real user session.
- **Delete data you no longer need.** There is no retention policy
  or per-run delete command in the CLI; delete the run's rows from
  the SQLite file or remove the whole `.agenttrace/` directory.
  Payloads over 4 KB live as files under
  `.agenttrace/artifacts/<run_id>/`, so removing the database alone
  does not remove them — delete the artifact directory too.
- **Rotate anything that leaked.** If you discover a real secret in a
  recorded run or an exported fixture that was shared, treat it as
  compromised: rotate it immediately, even if the fixture has since
  been deleted. Deleting the copy does not undo the exposure.
- **Do not record production traffic.** This reference implementation
  is for local development and regression testing with scripted or
  test data. Do not point the recorder at production agents handling
  real user data unless you have assessed the sensitivity of what
  will be stored and who can read the store directory.
