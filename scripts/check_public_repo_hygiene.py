#!/usr/bin/env python3
"""Fail CI if the tree contains markers that only make sense in an internal
(private) context — this repo is public.

This is a lightweight regression guard, not a secrets scanner: it looks for
categories of internal-only references (ticket-style IDs, internal
collaboration-tool links, and repo names outside the known-public set), not
credentials. It intentionally uses small, explicit allow/deny lists instead
of a single clever regex, so a false positive is a one-line list edit instead
of a mystery.

Run: python3 scripts/check_public_repo_hygiene.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files this script itself doesn't need to scan (its own source, lockfiles,
# and generated/vendored output that isn't hand-authored).
EXCLUDE_PATHS = {
    Path("scripts/check_public_repo_hygiene.py"),
}
EXCLUDE_DIR_PREFIXES = (
    "src/comfy_low/models/",  # generated from the vendored spec
)

# --- Category 1: ticket-shaped identifiers (TEAM-1234) -------------------
# Generic shape rather than a guessed list of real internal team keys, so we
# don't need to encode (and thus disclose) an internal naming scheme here.
# Catches false positives on common tech acronyms via an explicit allowlist
# below -- extend that list, not the regex, when a legitimate term trips it.
TICKET_RE = re.compile(r"\b[A-Z]{2,6}-\d{2,6}\b")
TICKET_ALLOWLIST = {
    "UTF-8",
    "ISO-8601",
    "SHA-256",
    "SHA-384",
    "SHA-512",
    "AES-128",
    "AES-256",
    "RFC-2119",
    "RFC-7231",
    "RFC-3339",
    "OAUTH-2",
    "IPV-4",
    "IPV-6",
    "X-25519",
    "WIN-32",
    "WIN-64",
}

# --- Category 2: internal collaboration-tool links/markers ----------------
INTERNAL_MARKER_RES = [
    re.compile(r"notion\.(so|site)/", re.IGNORECASE),
    re.compile(r"slack\.com/(archives|client)/", re.IGNORECASE),
    re.compile(r"\bapp\.slack\.com\b", re.IGNORECASE),
    re.compile(r"docs\.google\.com/", re.IGNORECASE),
    re.compile(r"drive\.google\.com/", re.IGNORECASE),
    re.compile(r"app\.datadoghq\.com/", re.IGNORECASE),
    re.compile(r"\bposthog\.com/project/", re.IGNORECASE),
    re.compile(r"\blinear\.app/", re.IGNORECASE),
    re.compile(r"\bincident-\d+\b", re.IGNORECASE),
]

# --- Category 3: references to Comfy-Org repos outside the known-public set
# Default-deny: only these are known to be public. Anything else under
# `Comfy-Org/<repo>` gets flagged so a maintainer can either scrub it or add
# it here once confirmed public. (No private repo names are listed here on
# purpose -- the point of default-deny is that we never need to.)
PUBLIC_COMFY_ORG_REPOS = {
    "comfy-api-proxy",
    "comfy-cla",
    "comfy-cli",
    "comfy-cloud-mcp-server",
    "Comfy-Desktop",
    "comfy-python-sdk",
    "comfy-swift-sdk",
    "comfy-typescript-sdk",
    # This repo's pre-rename name (v0.1.5 moved it to comfy-python-sdk).
    # Public, and GitHub still redirects it, so historical references -- the
    # CHANGELOG's rename note, old release-notes compare links -- stay valid.
    "ComfyPythonSDK",
    "ComfyUI_frontend",
    "ComfyUI",
}
# CODEOWNERS team handles (`@Comfy-Org/<team>`) are inherently public on a
# public repo -- GitHub renders the CODEOWNERS owners to anyone who can see the
# repo, so listing them here is not a leak. These mirror the sibling repos'
# CODEOWNERS (e.g. comfy-api-proxy). An `@Comfy-Org/<team>` handle NOT in this
# set is still flagged, so a genuinely-internal team reference surfaces.
PUBLIC_COMFY_ORG_TEAMS = {
    "comfy-cloud-team",
    "core-engine-team",
}
REPO_REF_RE = re.compile(r"Comfy-Org/([A-Za-z0-9_.-]+)")


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return [Path(p) for p in out.splitlines() if p]


def _is_excluded(rel: Path) -> bool:
    if rel in EXCLUDE_PATHS:
        return True
    return any(str(rel).startswith(prefix) for prefix in EXCLUDE_DIR_PREFIXES)


def _check_file(rel: Path) -> list[str]:
    findings: list[str] = []
    abs_path = ROOT / rel
    try:
        text = abs_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return findings  # binary or unreadable; not in scope

    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in TICKET_RE.finditer(line):
            if match.group(0).upper() not in TICKET_ALLOWLIST:
                findings.append(f"{rel}:{lineno}: possible internal ticket ID: {match.group(0)!r}")

        for pattern in INTERNAL_MARKER_RES:
            if pattern.search(line):
                findings.append(
                    f"{rel}:{lineno}: internal collaboration-tool marker: {line.strip()!r}"
                )

        for match in REPO_REF_RE.finditer(line):
            name = match.group(1)
            # A leading `@` makes this a CODEOWNERS team handle, not a repo ref.
            if match.start() > 0 and line[match.start() - 1] == "@":
                if name not in PUBLIC_COMFY_ORG_TEAMS:
                    findings.append(
                        f"{rel}:{lineno}: reference to @Comfy-Org/{name}, a team not in the "
                        "known-public allowlist (scripts/check_public_repo_hygiene.py) -- "
                        "confirm it's public and add it, or remove the reference"
                    )
                continue
            if name not in PUBLIC_COMFY_ORG_REPOS:
                findings.append(
                    f"{rel}:{lineno}: reference to Comfy-Org/{name}, which is not in the "
                    "known-public allowlist (scripts/check_public_repo_hygiene.py) -- "
                    "confirm it's public and add it, or remove the reference"
                )

    return findings


def main() -> int:
    all_findings: list[str] = []
    for rel in _tracked_files():
        if _is_excluded(rel):
            continue
        all_findings.extend(_check_file(rel))

    if all_findings:
        print(
            "ERROR: possible internal-only references found in this public repo:\n", file=sys.stderr
        )
        for finding in all_findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            "\nIf this is a genuine false positive, extend the allowlist in "
            "scripts/check_public_repo_hygiene.py with a comment explaining why.",
            file=sys.stderr,
        )
        return 1

    print("OK: no internal-only references found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
