# rescue-archiving

**Make a safe, verifiable copy of online media before it disappears.**

> **In active development.** This tool is being built and refined in the open. Its features, file formats, and interface may still change, and some parts may be incomplete or rough. Please keep your own copies of anything important, and reports of whatever breaks are welcome.

rescue-archiving is a small, local-first tool. You give it one link or one file
at a time. It downloads the media, makes a tamper-evident copy on your own
computer, asks the Internet Archive to keep an independent dated copy, records
exactly what it did, and can hand you a tidy package of everything later. It is
built to be careful: it never changes your originals, and it protects the people
who provide material.

You do not need to be a programmer to use it. If you can copy and paste a few
lines into a Terminal window, you can run it. This guide walks you through every
step in plain language; the precise technical details are gathered at the end
for those who want them.

---

## Contents

- [What rescue-archiving does](#what-rescue-archiving-does)
- [Who it is for](#who-it-is-for)
- [Before you begin](#before-you-begin)
- [Installing the tool](#installing-the-tool)
- [Key words, in plain language](#key-words-in-plain-language)
- [Your first capture, step by step](#your-first-capture-step-by-step)
- [See it in action](#see-it-in-action)
- [The everyday commands](#the-everyday-commands)
- [Where your files are kept](#where-your-files-are-kept)
- [Getting your archive out](#getting-your-archive-out)
- [Keeping sources safe](#keeping-sources-safe)
- [What the tool protects, and what it does not](#what-the-tool-protects-and-what-it-does-not)
- [Troubleshooting](#troubleshooting)
- [Glossary](#glossary)
- [Technical reference](#technical-reference)
- [Contributing, security, and license](#contributing-security-and-license)

---

## What rescue-archiving does

Imagine you find a video, a photo set, or a social-media post that documents
something important, and you are worried it might be edited or deleted. You want
a copy you can trust later: one that you can prove has not been altered, that
records where and when it came from, and that does not quietly expose whoever
shared it.

That is what this tool is for. For each item you give it, it does six things:

1. **Captures the media.** It downloads the video, images, or post you point it
   at and saves the file on your computer.
2. **Asks for an independent copy.** For web links, it asks the
   [Wayback Machine](https://web.archive.org) to keep its own dated snapshot, so
   a neutral third party also holds a record.
3. **Takes a fingerprint.** It computes a "hash" of every file, a short code
   that changes if even one byte changes, so you can later prove the file is
   untouched.
4. **Reads the details.** It pulls out available metadata (the platform's own
   information, camera EXIF data, still frames from videos).
5. **Writes everything down.** Every action goes into a log that can be added to
   but never edited or erased, like a notarized notebook.
6. **Packages it up.** When you are ready, it exports a clean summary (and,
   optionally, copies of the files) that you can share or hand to an archive.

It works entirely on your own machine. Nothing is published or uploaded to the
cloud unless you choose to do that yourself.

> **What it is not.** rescue-archiving is not a web scraper and not a fact
> checker. It only handles items you hand it, one at a time, and it never judges
> whether the content is true or what it shows. People do that; the tool just
> keeps a faithful, well-documented copy and records the judgements people make.

---

## Who it is for

The tool is general-purpose. It suits any situation where you need a
trustworthy, well-documented copy of media that might change or vanish:

- **Cultural heritage and at-risk sites** - documenting objects, buildings, or
  installations before alteration, demolition, or loss.
- **Field and environmental records** - preserving timestamped observations
  from fieldwork or long-term monitoring.
- **Oral histories and community archives** - capturing contributed recordings
  with consent and a clear record of their origin.
- **Journalism and research** - keeping a fixed, fingerprinted copy of source
  material alongside the page it came from.
- **Ephemeral social media** - saving posts that are routinely edited or taken
  down, with an independent Wayback snapshot.
- **Sensitive documentation** - where the identities of contributors must be
  protected by default and a clear chain of custody matters.

Nothing about the tool is specific to any one field. The strong protections for
sources and for file integrity simply make it dependable for high-stakes work.

---

## Before you begin

**You will need:**

- **A Mac or a Linux computer.** (On Windows, it runs inside the Windows
  Subsystem for Linux. It is not tested on plain Windows.)
- **About 15 minutes** for the one-time setup.
- **The Terminal.** This is the app where you type commands. On a Mac, open it
  from Applications > Utilities > Terminal, or press Cmd+Space, type "Terminal,"
  and press Return. A window with a text prompt appears. That is where every
  command in this guide goes: paste a line, then press Return.
- **Python 3.11 or newer.** This is the programming language the tool is written
  in; most Macs already have it. You will check for it during setup, and the
  guide tells you what to do if it is missing.

**You really cannot break your source material.** When you add a file from your
computer, the tool makes a *copy* and marks that copy read-only; it never edits
or deletes your original. When you add a web link, it only downloads; it changes
nothing at the source.

A note on copying and pasting: throughout this guide, lines shown in boxes like
this are meant to be pasted into the Terminal one block at a time, each followed
by Return. A line that starts with `#` is a human-readable comment, not a
command; you can paste it or ignore it.

---

## Installing the tool

You only do this once. Open the Terminal and work through these steps.

**1. Go to the project folder.** If you were given the project as a folder, move
into it. (If you have access on GitHub, you can instead download it first with
`git clone https://github.com/mirl-ucsb/rescue-archiving.git`.)

```bash
cd path/to/rescue-archiving
```

`cd` means "change directory." Replace `path/to/rescue-archiving` with wherever
your copy actually lives (for example, `cd ~/rescue-archiving`).

**2. Check that Python is present and new enough:**

```bash
python3 --version
```

You should see `Python 3.11.x` or higher. If you see a lower number or an error,
install the latest Python from [python.org](https://www.python.org/downloads/)
and try again.

**3. Create a private workspace for the tool ("a virtual environment").** This
is a sandboxed folder so the tool's parts do not interfere with anything else on
your computer:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

After the second line, your Terminal prompt usually shows `(.venv)` at the
front. That means the workspace is active. You repeat only this `source ...`
line each time you open a new Terminal to use the tool.

**4. Install the tool itself:**

```bash
pip install -e ".[dev]"
```

This pulls in the two small pieces the tool needs and makes the `rescue-archiving`
command available. When it finishes you are ready.

**5. (Optional) Add extra abilities.** The tool works without these, but they
unlock more features (recognizing near-duplicate images, reading photo EXIF
data, downloading image sets):

```bash
pip install -e ".[media]"     # adds image-similarity and EXIF support
```

Some features also use small external programs (`yt-dlp`, `ffmpeg`, `exiftool`).
You do not have to install them to start; the tool simply skips whatever is
missing and notes it. To see what is available at any time:

```bash
rescue-archiving doctor
```

This prints where your files will be stored and a checklist of which optional
programs are installed. Anything marked missing is fine; it just means that one
feature is skipped.

![The doctor command showing config paths and a capability checklist. Items
marked MISS are simply skipped; the core features still work.](docs/img/doctor.svg)

---

## Key words, in plain language

A few terms appear throughout. Here is what they mean, without the jargon. (A
fuller [glossary](#glossary) is at the end.)

- **Capture** - one item you saved, with all its files and records. Each capture
  gets a number (`#1`, `#2`, ...).
- **Hash / SHA-256** - a short fingerprint of a file. If the file changes in any
  way, the fingerprint changes. This is how the tool proves a file is untouched.
- **Custody log** - the can-only-be-added-to record of everything the tool did
  to a capture, with timestamps. Your evidence trail.
- **Manifest** - the summary file the tool exports, listing every capture, its
  files, their fingerprints, and where they came from.
- **Wayback snapshot** - the independent dated copy that the Internet Archive
  keeps when you capture a web link.
- **Read-only** - a file marked so it cannot be changed or deleted by accident.
  Your captured originals are stored this way.

---

## Your first capture, step by step

This walkthrough uses a made-up file so you can see how everything works without
touching anything real. Make sure your workspace is active (your prompt shows
`(.venv)`; if not, run `source .venv/bin/activate` again).

**1. Set up the archive folders and database:**

```bash
rescue-archiving init
```

It prints the locations it created. By default they sit in a `data` folder where
you are working. (To pin a permanent location instead, see
[Where your files are kept](#where-your-files-are-kept).)

**2. Make a small test file to practice on:**

```bash
echo "this is a practice file" > practice.txt
```

**3. Add it to the archive:**

```bash
rescue-archiving add practice.txt --note "my first capture" --tags "practice"
```

You should see something like `Added item #1 (file)` followed by a long
fingerprint and the path where the read-only copy now lives. That is a complete
capture.

**4. See what you have:**

```bash
rescue-archiving list
```

A short table lists your captures. You should see item `#1`.

**5. Look at it in full:**

```bash
rescue-archiving show 1
```

This prints everything the tool knows about capture `#1`: its files and their
fingerprints, and the custody log showing each step it took, with timestamps.

**6. Check that nothing has changed:**

```bash
rescue-archiving check
```

The tool re-computes every fingerprint and compares. You should see
`1 ok, 0 mismatch, 0 missing`. If a file had been altered, it would say so and
tell you which one.

**7. Export a summary:**

```bash
rescue-archiving export --format json
```

It writes a manifest file into your `exports` folder and prints the path. Open
that file in any text editor to see the human-readable record.

That is the whole loop: **add, list, show, check, export.** Everything else is a
variation on these.

When you are done for the day, you can simply close the Terminal. Next time, open
it, `cd` back to the project folder, run `source .venv/bin/activate`, and carry
on.

---

## See it in action

The walkthrough above uses a tiny text file so anyone can follow it. Here is a
slightly richer example: capturing a short video clip offline (the `--no-wayback`
flag skips the internet snapshot), then inspecting the result and confirming
every stored file is intact.

![Adding a video clip to the archive and listing the result: the tool reports
the new item, its fingerprint, and that five keyframes were extracted.](docs/img/capture.svg)

![Showing the full record for the item, then re-hashing every file. The custody
log lists each step, and the check reports six files all intact.](docs/img/inspect.svg)

---

## The everyday commands

Each command is `rescue-archiving` followed by a word. Here is what each one is
for, in order of how often you will reach for it.

### `add` - save something

The main command. Give it one web link or one file path:

```bash
# A file from your computer:
rescue-archiving add ./interview.mp4 --note "elder interview, reel 2" --tags "oral-history"

# A web link (it downloads the media and asks Wayback for a dated copy):
rescue-archiving add "https://example.com/post/123" --tags "fieldwork"
```

Useful options (all optional):

- `--location "..."` - where the content was made or is about.
- `--datetime "..."` - when, ideally as `2026-03-18T14:30:00-07:00`.
- `--note "..."` - a free-text description.
- `--tags "a,b,c"` - comma-separated labels you can filter on later.
- `--graphic` - mark distressing content. The tool then does **not**
  auto-generate still-frame thumbnails from it.
- `--uploader-handle "..."` and `--contributor-note "..."` - record who provided
  the material. This is stored separately and is **never** included in a normal
  export. See [Keeping sources safe](#keeping-sources-safe).
- `--no-wayback` - skip the Internet Archive snapshot (for example, when testing
  offline).

### `list` - see everything

```bash
rescue-archiving list                 # all captures
rescue-archiving list --tag fieldwork # only those tagged "fieldwork"
rescue-archiving list --status captured
```

### `show` - inspect one capture

```bash
rescue-archiving show 3
```

Shows files, fingerprints, snapshot links, any verifications, related items, and
the recent custody log for capture `#3`.

### `verify` - record a human judgement

The tool never decides whether content is genuine; a person does, and records
the verdict here:

```bash
rescue-archiving verify 3 --verdict confirmed --method "cross-referenced location" --verifier "your-name"
```

### `check` - confirm nothing has changed

```bash
rescue-archiving check        # check every file
rescue-archiving check 3      # just capture #3
```

Re-computes fingerprints and reports `ok`, `mismatch`, or `missing` for each
file. Run it whenever you want assurance your archive is intact.

### `dedup` - find duplicates

```bash
rescue-archiving dedup
```

Links captures that contain the same file, or near-identical images. It never
deletes anything; it just notes the connections.

### `export` - package your archive

```bash
rescue-archiving export --format json     # a single summary file
rescue-archiving export --format csv      # a spreadsheet-friendly table
rescue-archiving export --format bundle   # a folder with the summary plus copies of the files
```

See [Getting your archive out](#getting-your-archive-out) for the details and
the privacy options.

### `init` and `doctor` - setup helpers

`init` creates the folders and database (safe to run again anytime). `doctor`
shows your settings and which optional programs are installed.

---

## Where your files are kept

Everything lives in one folder called `data`, created where you ran `init`.
Inside it:

- a small database file that holds the records,
- an `originals` folder with one subfolder per capture, holding the read-only
  copies and any extracted frames,
- working space the tool manages itself.

Your exports go in a separate `exports` folder.

**Pinning a permanent location.** By default the `data` folder is created
wherever you happen to be working, which can be confusing. To always use the
same place, set this once at the start of your Terminal session (put your own
path in the quotes):

```bash
export RESCUE_ARCHIVING_DATA="/Users/you/Archives/rescue/data"
export RESCUE_ARCHIVING_EXPORTS="/Users/you/Archives/rescue/exports"
```

These settings last only for that Terminal window; run them again (or add them
to your shell profile) for a permanent setup.

**Keep this folder safe.** It contains your originals and any sensitive notes,
so store it on an encrypted disk (FileVault on a Mac, LUKS on Linux) and treat
it as confidential. The tool also marks the files read-only and owner-only as a
safeguard, but encryption is your responsibility.

---

## Getting your archive out

`export` produces three shapes, depending on what you need:

- **`--format json`** - one structured summary file listing every capture, its
  files and fingerprints, where they came from, and their verification status.
  Good for handing to another system.
- **`--format csv`** - the same information as a flat table you can open in
  Excel or Google Sheets.
- **`--format bundle`** - a self-contained folder with the summary, the table, a
  checksums file, and read-only copies of the captured files. Good for handing
  to an archive or a partner.

Two privacy controls:

- `--redact-source` - removes the original links from the export, for when you
  are sharing widely and even the URL could identify a source.
- `--include-sensitive` - the opposite: deliberately includes the protected
  contributor details. The tool records in the custody log that you did this.
  Use it only for trusted, access-controlled handoffs.

By default an export carries **no** contributor identities and **no** copies of
the platform's own metadata files, because those can name an uploader. See next.

---

## Keeping sources safe

Protecting the people who provide material is a core design goal, not an
afterthought. In practice:

- The tool does not record who provided something **unless you tell it to**, with
  `--uploader-handle` or `--contributor-note`. When you do, that information is
  kept in a separate, locked-away place and is left out of every normal export.
- Files the platform produces (for example a video's accompanying data file) can
  secretly contain an uploader's name. The tool keeps those for your records but
  **never copies them into an export bundle** unless you explicitly ask.
- If you need to share an export widely, `--redact-source` also strips the
  original links, which can themselves contain a username.

The trade-off worth knowing: a link you capture (say `example.com/some-user/...`)
may contain a username, and normal exports keep links because they are part of
the provenance. If that is too revealing for a particular handoff, use
`--redact-source`.

---

## What the tool protects, and what it does not

**It protects:**

- **Your originals.** They are copied, fingerprinted, and made read-only, and
  `check` will tell you if anything ever drifts.
- **Your record of events.** The custody log can be added to but not edited or
  erased.
- **An independent copy.** The Internet Archive is asked to hold a dated
  snapshot of every web capture.
- **Your sources.** Contributor identities stay out of exports by default.

**Be aware that:**

- **It does not verify content.** Whether something is real, and what it shows,
  is for people to determine.
- **The whole `data` folder is sensitive,** because it keeps the platform's
  metadata files (which can name sources) for your records. Encrypt it.
- **The Internet Archive snapshot is best-effort.** If their service is busy or
  unreachable, the snapshot may not be made; the tool records the failure so you
  can retry.
- **Some sites resist downloading.** Without logging in (which the tool refuses
  to do, for safety), some videos cannot be fetched. The tool records the
  outcome either way.

The precise, technical statement of all of this is in
[Project guardrails](#project-guardrails) and
[Threat model and limitations](#threat-model-and-limitations) below.

---

## Troubleshooting

**"command not found: rescue-archiving"**
Your workspace probably is not active. Run `source .venv/bin/activate` (from the
project folder) and try again. The prompt should show `(.venv)`.

**"command not found: python3"**
Python is not installed. Get it from
[python.org](https://www.python.org/downloads/), then redo the install steps.

**`add` said the capture "failed" or "metadata-only".**
The site likely needs a login to download, which the tool will not do. For web
items it still tries to make a Wayback snapshot, so check `show` for that link.
You can also save the file manually and add it from disk.

**"Permission denied" when I try to open or change a file in `data`.**
That is expected for captured originals; they are read-only on purpose. View them
by copying them out, or use `export --format bundle` to get shareable copies.

**`check` reported a "mismatch."**
A stored file no longer matches its fingerprint, meaning it was changed or
corrupted. The custody log and your backups are the place to investigate.

**I closed the Terminal and the command stopped working.**
That is normal. Open a new Terminal, `cd` to the project folder, and run
`source .venv/bin/activate` again.

**Did it actually work?**
Run `rescue-archiving show <number>` and look at the custody log, then
`rescue-archiving check` to confirm the files are intact.

If something else goes wrong, run `rescue-archiving doctor` and include its
output when you ask for help.

---

## Glossary

- **Capture / item** - one thing you saved, with its files and records. Numbered
  `#1`, `#2`, and so on.
- **Hash** - a fingerprint of a file's exact contents.
- **SHA-256** - the specific, widely trusted hashing method used here. Detects
  any change to a file.
- **pHash (perceptual hash)** - a fuzzy fingerprint of an image that stays
  similar when the image is lightly re-encoded or resized, used to spot
  near-duplicates.
- **Custody log** - an append-only record of every action, with timestamps.
- **Manifest** - the exported summary of your archive.
- **Sidecar** - a small companion file (such as a video's metadata) saved
  alongside the main media.
- **EXIF** - metadata embedded in photos (camera, settings, sometimes GPS).
- **Keyframe** - a still image taken from a video at intervals.
- **Wayback Machine** - the Internet Archive's public service that keeps dated
  snapshots of web pages.
- **WARC** - a standard file format for a full web-page snapshot (optional here).
- **Read-only** - a file that cannot be altered or deleted without first removing
  the protection.
- **Virtual environment (venv)** - the isolated workspace the tool runs in.

---

## Technical reference

The sections below are the precise, implementation-level details for operators
and developers.

### Command summary

| Command | Purpose |
|---|---|
| `init` | Create the data tree and schema (idempotent). |
| `doctor` | Show config paths and external-tool capabilities. |
| `add URL\|PATH` | Ingest one item. Options: `--location --datetime --note --tags --graphic --keyframes --make-thumbnails --uploader-handle --contributor-note --operator --no-wayback`. |
| `list` | List items. Options: `--status --tag --since`. |
| `show ID` | Full detail. Options: `--show-sensitive` (logged), `--log-tail`. |
| `verify ID` | Open a verification record. Options: `--verdict --method --notes --verifier`. |
| `check [ID]` | Recompute SHA-256 and report match / mismatch / missing. Exits non-zero on any mismatch or missing file, so it can gate scheduled integrity sweeps. |
| `dedup` | Link exact (SHA-256) and near (pHash) duplicates. Option: `--threshold`. |
| `export` | Options: `--format json\|csv\|bundle --since --out --redact-source --include-sensitive --no-media`. |

### External tools (all optional, detected at runtime)

| Tool        | Used for                          | Required? |
|-------------|-----------------------------------|-----------|
| yt-dlp      | video / post download + info JSON | for video URLs |
| gallery-dl  | image-set download                | for image-set URLs |
| ffmpeg      | video keyframe extraction         | for keyframes |
| exiftool    | EXIF extraction (via PyExifTool)  | for EXIF |
| archivebox  | WARC page snapshot                | optional, off by default |

When a tool is missing, the affected step is skipped and the skip is recorded in
the custody log. The core path (ingest, SHA-256, custody, export) needs no
external tools at all.

### Status

Milestones M1 through M5 are implemented: repo skeleton and SQLite schema with
`add`/`list`/`show` (M1); capture, SHA-256, read-only storage, and the custody
log (M2); the Wayback Save API, EXIF sidecars, and keyframe extraction (M3);
perceptual hashing, dedup linking, and the verification workflow (M4); and JSON,
CSV, and bundle export plus `check` integrity re-verification (M5). Cryptographic
signing of manifests (C2PA) is intentionally out of scope for now.

### Project guardrails

These are enforced in code, not just documented. See `SECURITY.md` for the
security policy.

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
   snapshot, so a third party holds a timestamped copy. Failures are recorded.
4. **Source protection.** Contributor and uploader identities are never recorded
   or exported by default. A flagged handle is stored in a separate,
   access-controlled table (`item_sensitive`), excluded from every default
   export; disclosure requires `--include-sensitive` and is logged. Provenance
   sidecars (yt-dlp `info.json`, EXIF `exif.json`) are kept read-only inside the
   data tree but never copied into an export bundle unless `--include-sensitive`
   is passed; the manifest still lists their hashes. gallery-dl is pinned to
   identity-free filenames.
5. **Local and private.** No publication and no cloud upload by default. The data
   tree is owner-only (0700); the database is 0600.
6. **Graphic content.** Items can be flagged graphic; keyframe and thumbnail
   generation is skipped for them unless the operator opts in with
   `--make-thumbnails`.

### Data model (SQLite)

- **items**: id, ingest_ts, ingested_by, source_url, source_kind, platform,
  claimed_location, claimed_datetime, description, status, tags, graphic_flag.
- **files**: id, item_id, path, media_type, role, sha256, phash, bytes,
  original_filename, created_ts.
- **captures**: id, item_id, method, capture_ts, wayback_url, warc_path, tool,
  tool_version, status, detail.
- **verifications**: id, item_id, verifier, verified_ts, verdict, method, notes.
- **custody_log**: id, item_id, ts, actor, action, detail. Append-only, enforced
  by database triggers that abort UPDATE and DELETE. The chain-of-custody
  foundation for future cryptographic signing.
- **item_sensitive**: item_id, uploader_handle, contributor_note, recorded_ts,
  recorded_by. Isolated, access-controlled, never in the default export.
- **item_links**: dedup links (exact / near) with pHash distance. Matches are
  linked, never discarded.

### Storage layout

```
data/                          # mode 0700, gitignored, access-controlled
  rescue_archiving.db          # mode 0600
  originals/
    item_000001/
      <id>.mp4                 # mode 0444, byte-identical to capture
      <id>.info.json           # platform provenance sidecar
      <id>.mp4.exif.json       # EXIF sidecar (kept out of default export)
      keyframes/               # derived, hashed, role='keyframe'
  snapshots/
  tmp/                         # scratch; never the canonical copy
exports/                       # manifests, CSVs, bundles
```

### Configuration (environment variables)

- `RESCUE_ARCHIVING_DATA` - data directory (default `./data`).
- `RESCUE_ARCHIVING_EXPORTS` - export directory (default `./exports`).
- `RESCUE_ARCHIVING_OPERATOR` - name recorded as the actor in the custody log
  (default: your OS user; set a role label if staff anonymity matters).
- `RESCUE_ARCHIVING_ARCHIVEBOX=1` - enable optional ArchiveBox WARC capture.
- `RESCUE_ARCHIVING_ROOT` - base directory if you prefer to set one root.

### Decisions (confirmed 2026-06-01)

1. **Encryption at rest is delegated to the operating system.** The `data/` tree
   must live on a FileVault (macOS) or LUKS (Linux) encrypted volume. Access
   control is POSIX permissions (0700 / 0600 / 0444). Application-level
   encryption and per-archive encrypted containers were considered and deferred.
2. **Wayback-only by default.** Every web item requests a Wayback snapshot;
   ArchiveBox WARC capture stays an opt-in (`RESCUE_ARCHIVING_ARCHIVEBOX=1`).
3. **Interchange schema mapping deferred.** Exports use the clean internal
   manifest, designed to be mappable; a `--schema` adapter can be added later
   without touching the capture path.
4. **Indefinite retention, manual deletion only.** Nothing is auto-deleted;
   long-term preservation is the purpose. Deletion is a deliberate operator
   action, and the custody-log record of any deletion survives the item.
   Redaction tooling already exists (graphic flag, `item_sensitive`, sidecar
   exclusion from bundles, `--redact-source`).

### Threat model and limitations

Protects: integrity of captured bytes (read-only, hashed, `check`-verifiable;
append-only custody log); deliberately recorded source identities (isolated and
excluded from default exports); independent provenance (Wayback); and locality
(owner-only storage, no cloud).

Does not, and caveats: it does not verify content (people do); gallery-dl is
pinned to a single item (`--range 1`), which prevents feed expansion but can
under-capture a multi-image post; provenance sidecars persist on disk and carry
identity/GPS, so the whole `data/` tree is sensitive; integrity anchors are
SHA-256 plus the append-only log, not yet cryptographic signatures, so a party
with direct disk/database access could rebuild the store (OS permissions and
full-disk encryption are the backstop until C2PA signing lands); Wayback is
best-effort; and the tool assumes a trusted operator on a secured, encrypted
host (it is not hardened against a hostile operator).

### Testing

```bash
pytest -q
```

The suite exercises the network-free path: local-file ingest, SHA-256, the
read-only freeze, append-only custody enforcement, sensitive-field exclusion
from exports, source redaction, dedup linking, and integrity re-checking.

### Out of scope (for now)

Automated geolocation or chronolocation; a public access layer (planned
separately); cryptographic signing of manifests (C2PA, folds in later via the
append-only custody log and stable manifest schema); and any capture beyond
operator-supplied items.

---

## Contributing, security, and license

- **Contributing:** see `CONTRIBUTING.md`. Contributions are held to the
  guardrails above and to an offline, synthetic-data testing rule.
- **Security:** see `SECURITY.md`. Report vulnerabilities privately; source
  protection and integrity are the highest-severity classes.
- **License:** MIT (see `LICENSE`). The license covers the software only.
  Captured material is governed by the source-protection, access-control, and
  retention decisions above, and by any agreements with contributors and
  partners.

Developed and maintained by MIRL (Material / Image Research Lab), Department of
History of Art and Architecture, UC Santa Barbara.
