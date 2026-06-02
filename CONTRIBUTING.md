# Contributing to rescue-archiving

Thank you for considering a contribution. This tool preserves ephemeral
documentation with verifiable provenance and is used in sensitive contexts, so
contributions are judged against its guardrails first and features second.

## The non-negotiable guardrails

Any change must preserve these (the README states them in full). A pull request
that weakens one will not be merged without an explicit maintainer decision
recorded in the README:

1. **Human-supplied input only.** No crawling, no feed or playlist expansion, no
   credentialed access to private accounts.
2. **Integrity.** Never modify or re-encode originals; hash on ingest; store
   originals read-only.
3. **Independent provenance.** Request a Wayback Machine snapshot for every web
   item.
4. **Source protection.** Contributor and uploader identity is never recorded or
   exported by default; sensitive fields stay in `item_sensitive` and out of
   default exports.
5. **Local and private.** No publication or cloud upload by default.
6. **Graphic content.** No automatic thumbnails or keyframes for flagged items
   without an explicit operator opt-in.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"      # add ".[media]" for pHash / EXIF / image-set support
pytest -q
```

## Testing principles

- **Offline by default.** Tests must not touch the network or invoke real
  capture tools. Simulate downloaders with `monkeypatch`, as the existing tests
  do.
- **Synthetic data only.** Never add a real captured file, a real source URL, or
  a real contributor handle to tests, fixtures, or examples. Use obviously fake
  values.
- Add or update a test for any behavior change, especially anything touching
  hashing, the read-only freeze, the custody log, export filtering, or dedup.
- A guardrail-relevant change should ship with a test that locks the guarantee
  in (for example: "this identity does not appear in a default export").

## Conventions

- External tools (yt-dlp, gallery-dl, ffmpeg, exiftool, archivebox) are optional
  and detected at runtime. New tool use must degrade gracefully when the tool is
  absent and record the skip in the custody log.
- The custody log is append-only, enforced by database triggers. Record actions
  there; never update or delete entries.
- Keep the default export free of identity-bearing data. Any new file type or
  sidecar that could carry identity must be excluded from default exports unless
  the operator passes `--include-sensitive` (which is itself logged).
- Match the existing style: small functions, clear docstrings, and the standard
  library where possible. The tool has only two hard dependencies (`typer` and
  `requests`); everything else is optional.

## Proposing changes

- Open an issue to discuss a substantial change before sending a large pull
  request.
- For anything security-relevant, follow `SECURITY.md`; do not open a public
  issue.
- Keep commits focused with clear messages, and run `pytest -q` before
  submitting.

## Provenance of contributions

By contributing you affirm that you have the right to submit the work under the
project's MIT License.
