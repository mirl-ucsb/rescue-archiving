# rescue-archive

A local-first, human-in-the-loop counter-archival capture pipeline. Given a
single source item supplied by an operator (a URL or a file), it captures the
media, records an independent timestamped copy, computes integrity and
similarity hashes, extracts available metadata, logs an append-only
provenance trail, and exports a portable manifest.

Project 3 of the Beirut 2026 / Latent Ground slate. MIRL, UCSB.

> This tool preserves ephemeral citizen documentation with verifiable
> provenance, for scholarship and possible future accountability use. It is a
> capture system, not an autonomous scraper, and not a verification engine.
> Verification is performed by human analysts; the tool only records it.

## Guardrails (non-negotiable)

These are enforced in code, not just documented. See `SECURITY` notes below.

1. **Human-supplied input only, no credentialed access.** Operators add one
   specific URL or file per `add`. There is no crawling and no feed expansion:
   yt-dlp runs with `--no-playlist`, gallery-dl runs with `--range 1`, directory
   ingest is refused, and a single item that yields an unusually large number of
   media files is flagged in the custody log for review. Neither downloader
   loads ambient user config (`--ignore-config` / `--config-ignore`), so a stray
   cookie or stored credential cannot leak a private session into a capture.
2. **Integrity.** Originals are never modified or re-encoded. Files are hashed
   on ingest and frozen read-only (mode 0444). `check` re-verifies them later.
3. **Independent provenance.** Every web item also requests a Wayback Machine
   snapshot, so a third party holds a timestamped copy. Failures are recorded,
   not hidden.
4. **Source protection.** Contributor and uploader identities are never
   recorded or exported by default. When an operator explicitly flags a
   handle, it is stored in a separate, access-controlled table
   (`item_sensitive`) and is excluded from every default export. Disclosing it
   requires an explicit `--include-sensitive` flag, and that disclosure is
   itself written to the custody log. Provenance sidecars (yt-dlp `info.json`
   and EXIF `exif.json`, which carry uploader handle, source URL, GPS, and
   device data) are stored read-only inside the data tree but are never copied
   into an export bundle unless `--include-sensitive` is passed; the manifest
   still lists their hashes so chain-of-custody is preserved. gallery-dl is
   pinned to identity-free filenames so a handle cannot enter the manifest via
   a filename.
5. **Local and private.** No publication and no cloud upload by default. The
   data tree is locked to the owner (mode 0700); the database is 0600.
6. **Graphic content.** Items can be flagged graphic. Keyframe and thumbnail
   generation is skipped for flagged items unless the operator opts in with
   `--make-thumbnails`.

## Status

Milestones M1 through M5 are implemented:

- M1: repo skeleton, SQLite schema, `add` / `list` / `show`.
- M2: capture (yt-dlp, gallery-dl), SHA-256, read-only storage, custody log.
- M3: Wayback Save API, EXIF sidecars, video keyframe extraction.
- M4: pHash, dedup linking, verification workflow.
- M5: export (JSON manifest, CSV, bundle), `check` integrity re-verification.

The Project 8 hook (C2PA signing of manifests) is intentionally out of scope
and folds in later. See "Out of scope" below.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # base tool + tests
pip install -e ".[media]"        # optional: Pillow/ImageHash (pHash), PyExifTool
```

The pipeline has only two hard Python dependencies: `typer` and `requests`.
Every external capture tool is optional and detected at runtime. Run `doctor`
to see what is available:

```bash
rescue-archive doctor
```

External tools (install as needed via Homebrew, apt, or pip):

| Tool        | Used for                          | Required? |
|-------------|-----------------------------------|-----------|
| yt-dlp      | video / post download + info JSON | for video URLs |
| gallery-dl  | image-set download                | for image-set URLs |
| ffmpeg      | video keyframe extraction         | for keyframes |
| exiftool    | EXIF extraction (via PyExifTool)  | for EXIF |
| archivebox  | WARC page snapshot                | optional, off by default |

When a tool is missing, the affected step is skipped and the skip is recorded
in the custody log. The core path (ingest, SHA-256, custody, export) needs no
external tools at all.

## Quickstart

```bash
rescue-archive init                      # create the data tree + schema

# Ingest a local file (no network):
rescue-archive add ./clip.mp4 \
    --location "Dahieh, Beirut" \
    --datetime "2026-03-18T14:30:00+02:00" \
    --note "rooftop, plume to the south" \
    --tags "beirut,airstrike,rooftop"

# Ingest a web item (downloads, snapshots to Wayback, hashes):
rescue-archive add "https://example.com/post/123" --tags "beirut"

# Flag graphic content (suppresses auto keyframes/thumbnails):
rescue-archive add ./graphic.mp4 --graphic

# Record a sensitive uploader handle (access-controlled, never default-exported):
rescue-archive add "https://example.com/x" --uploader-handle "@source" \
    --contributor-note "trusted local journalist"

rescue-archive list --status captured
rescue-archive show 1
rescue-archive verify 1 --verdict confirmed --method "geolocation; reverse image" --verifier "analyst-2"
rescue-archive check                     # re-hash everything, report drift
rescue-archive dedup                     # link exact + near duplicates
rescue-archive export --format json      # manifest into exports/
rescue-archive export --format bundle     # manifest + CSV + read-only originals + SHA256SUMS
```

Exit codes: `check` exits non-zero if any file mismatches or is missing, so it
can gate cron jobs and CI-style integrity sweeps.

## CLI reference

| Command | Purpose |
|---|---|
| `init` | Create the data tree and schema (idempotent). |
| `doctor` | Show config paths and external-tool capabilities. |
| `add URL\|PATH` | Ingest one item. Options: `--location --datetime --note --tags --graphic --keyframes --make-thumbnails --uploader-handle --contributor-note --operator --no-wayback`. |
| `list` | List items. Options: `--status --tag --since`. |
| `show ID` | Full detail. Options: `--show-sensitive` (logged), `--log-tail`. |
| `verify ID` | Open a verification record. Options: `--verdict --method --notes --verifier`. |
| `check [ID]` | Recompute SHA-256 and report match / mismatch / missing. |
| `dedup` | Link exact (SHA-256) and near (pHash) duplicates. Option: `--threshold`. |
| `export` | Options: `--format json\|csv\|bundle --since --out --redact-source --include-sensitive --no-media`. |

## Data model (SQLite)

- **items**: id, ingest_ts, ingested_by, source_url, source_kind, platform,
  claimed_location, claimed_datetime, description, status, tags, graphic_flag.
- **files**: id, item_id, path, media_type, role, sha256, phash, bytes,
  original_filename, created_ts.
- **captures**: id, item_id, method, capture_ts, wayback_url, warc_path, tool,
  tool_version, status, detail.
- **verifications**: id, item_id, verifier, verified_ts, verdict, method, notes.
- **custody_log**: id, item_id, ts, actor, action, detail. Append-only,
  enforced by database triggers that abort UPDATE and DELETE. This is the
  chain-of-custody foundation for Project 8.
- **item_sensitive**: item_id, uploader_handle, contributor_note, recorded_ts,
  recorded_by. Isolated, access-controlled, never in the default export.
- **item_links**: dedup links (exact / near) with pHash distance. Matches are
  linked, never discarded.

## Storage layout

```
data/                          # mode 0700, gitignored, access-controlled
  rescue_archive.db            # mode 0600
  originals/
    item_000001/
      <id>.mp4                 # mode 0444, byte-identical to capture
      <id>.info.json           # yt-dlp provenance sidecar
      <id>.mp4.exif.json       # EXIF sidecar (kept out of default export)
      keyframes/               # derived, hashed, role='keyframe'
  snapshots/
  tmp/                         # scratch; never the canonical copy
exports/                       # manifests, CSVs, bundles
```

## Decisions (confirmed 2026-06-01)

The handoff asked that these be settled before M3. They are now recorded as
project decisions; revisit by editing this section and the relevant config.

1. **Storage path, encryption at rest, access control.**
   - Storage path is `./data` (override with `RESCUE_ARCHIVE_DATA`).
   - Access control is POSIX permissions: data tree 0700, database 0600,
     originals 0444. Operator identity for custody attribution comes from
     `RESCUE_ARCHIVE_OPERATOR` (set this to a role label rather than a personal
     name if staff anonymity matters).
   - **Decision: encryption at rest is delegated to the operating system.**
     The `data/` tree must live on a FileVault (macOS) or LUKS (Linux)
     encrypted volume. This keeps key management out of the tool. Application
     level encryption and per-archive encrypted containers were considered and
     deferred; they can be revisited if the threat model changes (for example,
     if the archive must be transported on shared or untrusted hardware).

2. **Independent provenance: ArchiveBox WARC versus Wayback-only.**
   - **Decision: Wayback-only by default.** Every web item requests a Wayback
     Machine snapshot. ArchiveBox WARC capture stays an opt-in
     (`RESCUE_ARCHIVE_ARCHIVEBOX=1`) for operators who want self-hosted,
     full-page preservation and have ArchiveBox installed.

3. **Partner schema mapping (Airwars / CIR / Mnemonic / Syrian Archive).**
   - **Decision: deferred.** Exports use the clean internal manifest, which is
     designed to be mappable. A `--schema` adapter can be added later without
     touching the capture path, once a partner is chosen.

4. **Retention and redaction policy for graphic content and personal data.**
   - **Decision: indefinite retention, manual deletion only.** Nothing is
     auto-deleted; preservation for scholarship and possible accountability is
     the purpose. Deletion is a deliberate operator action, and the custody-log
     record of any deletion survives the deleted item.
   - Redaction tooling is already in place: graphic items suppress auto
     keyframes/thumbnails unless opted in; sensitive identities live only in
     `item_sensitive` and are excluded from default exports; provenance
     sidecars are withheld from bundles unless `--include-sensitive`; and
     `--redact-source` strips source and Wayback URLs for wider circulation.

## Source protection: a note on URLs

A captured `source_url` (for example `x.com/<handle>/status/...`) can embed an
uploader handle. Two acceptance criteria are in tension here: exports must list
capture URLs, and exports must not carry contributor identity. The resolution:

- The "contributor identity" the guardrail protects is the handle an operator
  *deliberately records* via `--uploader-handle`. That lives in
  `item_sensitive` and is never in a default export.
- The `source_url` and Wayback URL are provenance and are included by default,
  because the manifest is local and access-controlled. For exports that leave
  that boundary, use `--redact-source` to strip the URLs while keeping hashes,
  timestamps, claimed context, and verification status.

## Threat model and limitations

What this tool does protect:

- **Integrity of captured bytes.** Originals are frozen read-only and hashed on
  ingest; `check` detects any later drift. The custody log is append-only,
  enforced by database triggers.
- **Source identity.** Deliberately recorded uploader handles and contributor
  notes are isolated in `item_sensitive` and never leave via a default export.
- **Independent provenance.** A third party (the Internet Archive) is asked to
  hold a timestamped copy of every web item.
- **Locality.** No publication or cloud upload; storage is owner-only on an
  encrypted volume.

What it does not do, and where to be careful:

- **It does not verify anything.** Location, time, and authenticity are
  determined by human analysts; the tool only records their verdicts.
- **gallery-dl is pinned to a single item (`--range 1`).** This prevents a
  profile or feed URL from expanding into a bulk scrape, but it can also
  under-capture a single multi-image post (only the first file). Add the
  individual media URLs separately if you need all of them.
- **Provenance sidecars persist on disk.** The yt-dlp `info.json` and EXIF
  `exif.json` carry uploader handle, source URL, GPS, and device data. They are
  stored read-only inside the access-controlled tree and excluded from default
  exports, but they are not deleted. Treat the whole `data/` tree as sensitive.
- **Integrity anchors are not yet cryptographic.** SHA-256 plus the append-only
  log detect accidental or in-app tampering, but they are not signatures. A
  party with direct filesystem or database access could rebuild the store; OS
  permissions and full-disk encryption are the backstop until C2PA signing
  lands (Project 8).
- **Wayback is best-effort.** If archive.org is unreachable or rate-limits, the
  independent copy may be missing. The failure is recorded in the custody log;
  retry is a manual re-run.
- **Trust assumptions.** The tool assumes a trusted operator on a secured,
  encrypted host. It is not hardened against a hostile operator.

## Testing

```bash
pytest -q
```

The suite exercises the network-free path: local-file ingest, SHA-256, the
read-only freeze, append-only custody enforcement, sensitive-field exclusion
from exports, source redaction, dedup linking, and integrity re-checking.

## Out of scope (for now)

Automated geolocation or chronolocation; the public platform (Project 9);
cryptographic signing of manifests (Project 8, folds in later via the
append-only custody log and stable manifest schema); and any capture beyond
operator-supplied items.

## Maintainer and license

Maintained by MIRL (Material / Image Research Lab), Department of History of Art
and Architecture, UC Santa Barbara. Part of the Beirut 2026 / Latent Ground
slate. Contact: mirl@arthistory.ucsb.edu.

Released under the MIT License (see `pyproject.toml`). Note that the license
covers the software only. Captured material is governed by the source
protection, access control, and retention decisions recorded above, and by any
agreements with contributors and partners.
