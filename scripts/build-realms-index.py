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

CATEGORIES. A realm says what it is FOR with `category`, one id from `realm-categories.yml`, the
authoritative list. The index carries the claim only when it is on that list, and carries the list
itself, so one fetch tells an appliance both what the categories are and which realm is in which.
A realm is checked against the list BEFORE it is published, with no token and no network:

    python3 scripts/build-realms-index.py --check path/to/realm.yml

That exits non-zero on a missing or unlisted category, naming the ids it could have been. The
build itself never fails on one realm's category: it drops the claim and says so, because one
misspelled line in one repository must not stop the catalogue everybody reads.
"""

from __future__ import annotations

import difflib
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

CATEGORIES_FILE = Path(__file__).resolve().parent.parent / "realm-categories.yml"


def categories() -> list[dict]:
    """The authoritative list, refused outright if it is malformed: every realm is judged against it."""
    import yaml

    listed = yaml.safe_load(CATEGORIES_FILE.read_text()) or []
    ids = [c.get("id") for c in listed if isinstance(c, dict)]
    if len(ids) != len(listed) or not all(isinstance(i, str) and i and c.get("label") for i, c in zip(ids, listed)):
        sys.exit(f"{CATEGORIES_FILE.name}: every category needs an `id` and a `label`")
    if len(set(ids)) != len(ids):
        sys.exit(f"{CATEGORIES_FILE.name}: an id is listed twice")
    return listed


def checked_category(value: object, known: list[str]) -> tuple[str | None, str | None]:
    """The category a manifest claims if it is on the list, else why not. No claim is neither."""
    if value is None or not str(value).strip():
        return None, None
    claim = str(value).strip().lower()
    if claim in known:
        return claim, None
    near = difflib.get_close_matches(claim, known, n=1)
    hint = f" — did you mean '{near[0]}'?" if near else ""
    return None, f"declares category '{value}', which is not one of {known}{hint}"


def check(path: str) -> None:
    """Judge ONE manifest against the list, for a realm's author or its CI, before it is published."""
    import yaml

    listed = categories()
    known = [c["id"] for c in listed]
    declared = yaml.safe_load(Path(path).read_text())
    if not isinstance(declared, dict):
        sys.exit(f"{path} is not a realm manifest")
    category, problem = checked_category(declared.get("category"), known)
    if problem:
        sys.exit(f"{path} {problem}")
    if not category:
        sys.exit(f"{path} declares no category. Add `category:` with one of {known}")
    label = next(c["label"] for c in listed if c["id"] == category)
    print(f"{path}: category {category} ({label})")


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


def entry(repo: dict, known: list[str]) -> dict | None:
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
    # A claim off the list is dropped rather than published, as an unrecognized maturity is: the
    # realm is still offered, uncategorised, and its author is told which ids it could have been.
    category, problem = checked_category(declared.get("category"), known)
    if category:
        out["category"] = category
    elif problem:
        print(f"  ! {display} {problem} — dropped", file=sys.stderr)
    return out


def main() -> None:
    import yaml

    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        return check(sys.argv[2])

    listed = categories()
    known = [c["id"] for c in listed]
    entries = sorted(
        (e for e in (entry(r, known) for r in repos()) if e),
        key=lambda e: e["name"],
    )
    claims = sum(1 for e in entries if "maturity" in e)
    uncategorised = [e["name"] for e in entries if "category" not in e]
    # The list travels with the realms it judges, ahead of them: a reader that wants only the
    # realms takes the `realms:` key, as it always has.
    document = {
        "categories": listed,
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
        "# `categories` is realm-categories.yml, the authoritative list; a realm's `category` is on it\n"
        "# or is not carried.\n"
        "#\n"
        "# Public repositories only: a private realm's clone URL is no use to anybody who cannot\n"
        "# see it. An appliance with a token still finds those by scanning directly.\n"
    )
    target.write_text(header + yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100))
    print(f"{len(entries)} realms → {target} ({claims} declare a maturity, "
          f"{len(entries) - len(uncategorised)} a category)")
    if uncategorised:
        print(f"  ! no category: {', '.join(uncategorised)}", file=sys.stderr)


if __name__ == "__main__":
    main()
