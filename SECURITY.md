# Security Policy

## Scope and intent

rescue-archiving is local-first, human-in-the-loop software for preserving
operator-supplied documentation with verifiable provenance. Because it is used
to protect ephemeral records and the people who contribute them, two classes of
defect are treated as the highest severity:

1. **Source-protection failures.** Any path by which a contributor or uploader
   identity (a recorded `--uploader-handle`, a `contributor_note`, or an
   identity-bearing sidecar such as a yt-dlp `info.json` or an EXIF
   `exif.json`) reaches a default export, a log, or any output that leaves the
   access-controlled boundary.
2. **Integrity failures.** Any path that modifies, re-encodes, or fails to
   freeze a stored original read-only; that lets a SHA-256 be bypassed or
   misreported; or that allows the append-only custody log to be altered.

Custody-log tampering, credential leakage (a capture using ambient cookies or
stored auth), and unbounded capture (feed or profile expansion beyond the single
operator-supplied item) are also high priority.

## Reporting a vulnerability

Please report privately. Do not open a public issue for a security defect,
especially a source-protection or integrity issue, since the report itself may
reveal a way to expose protected data.

- Email **mirl@arthistory.ucsb.edu** with the subject line
  "rescue-archiving security".
- If the repository is public and GitHub private vulnerability reporting is
  enabled, you may instead use "Report a vulnerability" under the Security tab.

Please include:

- the version or commit hash,
- the affected command or module,
- a minimal reproduction using **synthetic data only** (never a real captured
  file, a real source URL, or a real contributor handle),
- the impact, and
- a suggested fix if you have one.

We aim to acknowledge reports within a few working days. As pre-release research
software maintained by a small team we cannot promise a fixed remediation
timeline, but source-protection and integrity reports are prioritized.

## Supported versions

This is pre-release software (0.x). Only the latest `main` is supported.
Security fixes land on `main`; there are no backports.

## In scope

- Identity or source leakage into any default export (JSON, CSV, or bundle) or
  into any log.
- Modification, re-encoding, or non-read-only storage of captured originals.
- Hash computation or verification that can be bypassed or spoofed.
- Custody-log entries that can be updated or deleted.
- A capture that expands beyond the single operator-supplied item, or that loads
  ambient credentials or cookies.

## Not a vulnerability

- Absence of cryptographic signing of manifests. This is planned (the C2PA hook,
  Project 8) and tracked, not a defect.
- The documented gallery-dl single-item trade-off (`--range 1` may under-capture
  a multi-image post).
- Graceful degradation when an optional external tool is missing.
- Reliance on operating-system full-disk encryption for encryption at rest, and
  on a trusted operator and a secured host. These are stated assumptions.

See the README "Threat model and limitations" section for the full model.
