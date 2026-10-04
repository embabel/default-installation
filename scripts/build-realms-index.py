#!/usr/bin/env python3
"""
Build `realms-index.yml` — the curated realm catalogue every appliance can read WITHOUT a token.

WHY THIS FILE EXISTS AT ALL. An appliance discovers realms two ways. It can scan GitHub, which is
live but rate-limited: unauthenticated, GitHub allows ~60 requests an hour, so the scan reads repo
listings only and never opens a realm's manifest. Everything a manifest says — the description its
author wrote, its version, and its `maturity` claim — is therefore invisible to a public browse, and
a surface that wanted to hide unfinished realms had nothing to hide them by.

This index is one document, fetched in one request, carrying exactly those manifest facts. It is
GENERATED, never hand-edited: a hand-maintained catalogue drifts from the repos it describes, and a
stale `maturity` claim is worse than none — it either hides a realm that was finished months ago or
vouches for one that has since been torn up.

Run it with a token (the scan reads every manifest, one request per repo):

    GITHUB_TOKEN=$(gh auth token) python3 scripts/build-realms-index.py

Only PUBLIC repositories are listed. A private realm's clone URL is useless to anybody who cannot
see it, and its name is nobody else's business; a token-bearing scan still finds those directly.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"

# The accounts to describe, mirroring `assistant.directory.realm-sources`.
SOURCES = [("embabel-worlds", "orgs"), ("embabel", "orgs")]

# Names the appliance's own scan refuses to offer, kept out of here for the same reasons: `spec` is
# the specification of what a realm IS (it ships an example manifest, so no inspection excludes it),
# and the rest are forks of the unrelated Realm mobile database's SDKs, which the `realm-` prefix
# collects by accident. The index applies no exclusions of its own, so a name listed here WOULD be
# offered — hence this list, in step with GitHubRealmDirectoryProperties.DEFAULT_EXCLUDED_REALMS.
EXCLUDED = {"spec", "swift", "js", "core", "java", "dotnet", "kotlin", "cocoa", "dart"}

# Manifest keys worth carrying. `maturity` is the one this index was built for; the others make the
# catalogue worth reading on its own, since a public scan has only the repo's one-line description.
CARRIED = ("description", "version", "author", "tags", "maturity")

MATURITIES = {"experimental", "beta", "stable", "deprecated"}


def token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    sys.exit(
        "No token in GITHUB_TOKEN / GH_TOKEN / GITHUB_PERSONAL_ACCESS_TOKEN.\n"
        "Without one GitHub refuses the volume of manifest reads this needs; try\n"
        "  GITHUB_TOKEN=$(gh auth token) python3 scripts/build-realms-index.py"
    )


def get(url: str, accept: str = "application/vnd.github+json") -> bytes | None:
    request = urllib.request.Request(url, headers={
        "Accept": accept,
        "Authorization": f"Bearer {token()}",
        "User-Agent": "embabel-realms-index",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        # A missing manifest is ordinary (a repo may ship neither realm.yml nor pack.yml); anything
        # else is worth seeing, because a silent 403 would quietly shrink the catalogue.
        if e.code != 404:
            print(f"  ! {url} → HTTP {e.code}", file=sys.stderr)
        return None


def repos() -> list[dict]:
    found: list[dict] = []
    for account, kind in SOURCES:
        page = 1
        while True:
            body = get(f"{API}/{kind}/{account}/repos?per_page=100&page={page}")
            if not body:
                break
            batch = json.loads(body)
            found += batch
            if len(batch) < 100:
                break
            page += 1
    return found


def manifest(full_name: str) -> dict | None:
    """The realm's own manifest, realm.yml first and legacy pack.yml second — the host's order."""
    import yaml  # local import so --help works without PyYAML installed

    for name in ("realm.yml", "pack.yml"):
        raw = get(f"{API}/repos/{full_name}/contents/{name}", accept="application/vnd.github.raw+json")
        if raw is None:
            continue
        try:
            loaded = yaml.safe_load(raw.decode())
        except yaml.YAMLError as e:
            print(f"  ! {full_name}/{name} is not readable YAML: {e}", file=sys.stderr)
            return None
        if isinstance(loaded, dict):
            return loaded
    return None


def entry(repo: dict) -> dict | None:
    name = repo["name"]
    for prefix in ("realm-", "pack-"):
        if name.startswith(prefix):
            display = name[len(prefix):]
            break
    else:
        return None
    if display in EXCLUDED or repo.get("private") or repo.get("archived"):
        return None

    out = {
        "name": display,
        # What an install needs, and what a person reads: the appliance requires `source`, and
        # treats `url` as the page to open.
        "source": repo["clone_url"],
        "url": repo["html_url"],
        "description": (repo.get("description") or "").strip(),
    }
    declared = manifest(repo["full_name"]) or {}
    for key in CARRIED:
        value = declared.get(key)
        if key == "maturity":
            claim = str(value).strip().lower() if value is not None else ""
            # An unrecognized claim is dropped rather than published: the spec requires a host to
            # read one as unstated anyway, and a typo passed along here would be a claim nobody made.
            if claim and claim not in MATURITIES:
                print(f"  ! {display} declares maturity '{value}', which is not one of "
                      f"{sorted(MATURITIES)} — dropped", file=sys.stderr)
            elif claim:
                out["maturity"] = claim
        elif isinstance(value, list) and value:
            out[key] = [str(v) for v in value]
        elif isinstance(value, str) and value.strip():
            out[key] = " ".join(value.split()) if key == "description" else value.strip()
        elif value is not None and key == "version":
            out[key] = str(value)
    if not out["description"]:
        out.pop("description")
    return out


def main() -> None:
    import yaml

    entries = sorted(
        (e for e in (entry(r) for r in repos()) if e),
        key=lambda e: e["name"],
    )
    claims = sum(1 for e in entries if "maturity" in e)
    document = {
        "realms": entries,
    }
    target = Path(__file__).resolve().parent.parent / "realms-index.yml"
    header = (
        "# The realm catalogue, as one document an appliance can read with no token.\n"
        "#\n"
        "# GENERATED by scripts/build-realms-index.py — do not edit. Every field below is the\n"
        "# realm's own manifest speaking, so a `maturity` claim here is the author's, not a\n"
        "# curator's opinion. Regenerate when a realm is published, retired, or changes its claim.\n"
        "#\n"
        "# Public repositories only: a private realm's clone URL is no use to anybody who cannot\n"
        "# see it. An appliance with a token still finds those by scanning directly.\n"
    )
    target.write_text(header + yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100))
    print(f"{len(entries)} realms → {target} ({claims} declare a maturity)")


if __name__ == "__main__":
    main()
