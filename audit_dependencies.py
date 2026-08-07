#!/usr/bin/env python
"""Audit the installed virtualenv against the OSV vulnerability database.

Why this exists rather than relying on GitHub: Dependabot only reads
requirements.txt, so it sees the handful of packages declared there and nothing
else. Most of what is actually installed arrives transitively (pillow via
pdfplumber, urllib3 via requests) or as orphans left behind by other work. On
2026-08-07 an OSV sweep found vulnerabilities in seven packages while GitHub was
reporting one. This audits what is really on disk.

Two extras beyond a plain vulnerability list:

  * Orphan detection. Packages that nothing requires and no project code imports
    are pure liability -- pypdf alone carried eight advisories while being
    entirely unused. Uninstalling orphans is zero-risk and was the single biggest
    reduction available.
  * Installability. A "fix available in 50.0.0" is useless on Windows ARM64 if
    upstream published no wheel for it and there is no Rust toolchain to build
    one. Each finding is marked INSTALLABLE or BLOCKED so the report distinguishes
    "you should act" from "upstream has not shipped anything you can use yet".

Usage:
    python audit_dependencies.py            # human-readable report
    python audit_dependencies.py --quiet    # print only if something is actionable

Exit codes: 0 = nothing actionable, 1 = actionable findings, 2 = audit failed.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import pathlib
import re
import subprocess
import sys
import sysconfig
import urllib.error
import urllib.request

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
PYPI_JSON = "https://pypi.org/pypi/{}/json"
TIMEOUT = 60

REPO = pathlib.Path(__file__).resolve().parent

# Packages that are expected to be present without being imported by project
# code -- build tooling and the like. Keeps them out of the orphan report.
ORPHAN_ALLOWLIST = {"pip", "setuptools", "wheel", "pkg_resources"}


def _get_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as fh:
        return json.load(fh)


def installed_packages() -> dict[str, str]:
    """Map distribution name -> version for everything in this interpreter."""
    out: dict[str, str] = {}
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if name:
            out[name] = dist.version
    return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))


def platform_wheel_tag() -> str:
    """Wheel platform tag for this interpreter, e.g. 'win_arm64'."""
    return sysconfig.get_platform().replace("-", "_").replace(".", "_")


def query_osv(pkgs: dict[str, str]) -> dict[str, list[dict]]:
    """Batch-query OSV. Returns name -> list of advisory stubs."""
    names = list(pkgs)
    queries = [
        {"package": {"name": n, "ecosystem": "PyPI"}, "version": pkgs[n]} for n in names
    ]
    result = _get_json(OSV_BATCH, {"queries": queries})
    findings: dict[str, list[dict]] = {}
    for name, res in zip(names, result.get("results", [])):
        vulns = res.get("vulns", [])
        if vulns:
            findings[name] = vulns
    return findings


def advisory_detail(vuln_id: str) -> dict:
    try:
        return _get_json(f"https://api.osv.dev/v1/vulns/{vuln_id}")
    except urllib.error.URLError:
        return {}


def min_fix_version(name: str, vuln_ids: list[str]) -> str | None:
    """Highest 'fixed' version across the advisories -- what you must reach."""
    fixes: list[tuple, ] = []
    for vid in vuln_ids:
        detail = advisory_detail(vid)
        for aff in detail.get("affected", []):
            if aff.get("package", {}).get("name", "").lower() != name.lower():
                continue
            for rng in aff.get("ranges", []):
                for event in rng.get("events", []):
                    if "fixed" in event:
                        fixes.append(event["fixed"])
    if not fixes:
        return None
    return max(fixes, key=_version_key)


def _version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+]", v)[:4])


def fix_is_installable(name: str, fix_version: str, tag: str) -> tuple[bool, str]:
    """Can a version >= fix_version actually be installed on this platform?

    Returns (installable, explanation). A pure-python wheel works anywhere; a
    compiled package needs a wheel matching this platform tag, otherwise pip
    falls back to an sdist and needs a full build toolchain.
    """
    try:
        data = _get_json(PYPI_JSON.format(name))
    except urllib.error.URLError as exc:
        return True, f"could not check wheels ({exc.reason})"

    candidates = [
        v for v in data.get("releases", {}) if _version_key(v) >= _version_key(fix_version)
    ]
    if not candidates:
        return False, f"no release >= {fix_version} on PyPI"

    for version in sorted(candidates, key=_version_key):
        for f in data["releases"][version]:
            fn = f.get("filename", "")
            if not fn.endswith(".whl"):
                continue
            if "py3-none-any" in fn or "py2.py3-none-any" in fn:
                return True, f"{version} is pure-python"
            if tag in fn:
                return True, f"{version} has a {tag} wheel"
    return False, f"no {tag} wheel for any version >= {fix_version} (source build required)"


def project_imports() -> set[str]:
    """Top-level modules imported anywhere in the project's own .py files."""
    pattern = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
    mods: set[str] = set()
    for path in REPO.rglob("*.py"):
        if ".venv" in path.parts or path.name == pathlib.Path(__file__).name:
            continue
        try:
            mods.update(pattern.findall(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return mods


def find_orphans(pkgs: dict[str, str]) -> list[str]:
    """Installed packages that nothing requires and no project code imports."""
    required_by: set[str] = set()
    for dist in md.distributions():
        for req in dist.requires or []:
            # Skip optional extras -- they are not real edges unless installed for it.
            if ";" in req and "extra ==" in req.split(";", 1)[1]:
                continue
            dep = re.split(r"[<>=!~\[\s;]", req.split(";")[0].strip())[0]
            required_by.add(_norm(dep))

    imported = project_imports()
    declared = declared_requirements()

    orphans = []
    for name in pkgs:
        n = _norm(name)
        if n in ORPHAN_ALLOWLIST or n in required_by or n in declared:
            continue
        # A package supplies one or more importable top-level modules; if any of
        # them is imported by project code it is in use.
        if _top_levels(name) & imported:
            continue
        orphans.append(name)
    return sorted(orphans)


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def _top_levels(dist_name: str) -> set[str]:
    """Top-level importable module names a distribution provides."""
    try:
        dist = md.distribution(dist_name)
    except md.PackageNotFoundError:
        return {_norm(dist_name).replace("-", "_")}
    names = set()
    text = dist.read_text("top_level.txt")
    if text:
        names.update(t.strip() for t in text.splitlines() if t.strip())
    for f in dist.files or []:
        parts = pathlib.PurePosixPath(str(f)).parts
        if parts and not parts[0].endswith((".dist-info", ".data")):
            names.add(parts[0].removesuffix(".py"))
    names.add(dist_name.replace("-", "_"))
    return names


def declared_requirements() -> set[str]:
    """Names appearing in requirements.txt, so declared deps are never orphans."""
    req = REPO / "requirements.txt"
    if not req.exists():
        return set()
    out = set()
    for line in req.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.add(_norm(re.split(r"[<>=!~\[\s;]", line)[0]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true",
                    help="print only when there is something actionable")
    args = ap.parse_args()

    pkgs = installed_packages()
    tag = platform_wheel_tag()

    try:
        findings = query_osv(pkgs)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"AUDIT FAILED: could not reach OSV ({exc})", file=sys.stderr)
        return 2

    actionable: list[str] = []
    blocked: list[str] = []

    for name, vulns in sorted(findings.items()):
        ids = sorted({v["id"] for v in vulns if v["id"].startswith("GHSA")})
        if not ids:
            continue
        fix = min_fix_version(name, ids)
        if not fix:
            actionable.append(f"{name}=={pkgs[name]}: {len(ids)} advisories, no fix published")
            continue
        ok, why = fix_is_installable(name, fix, tag)
        line = f"{name}=={pkgs[name]} -> {fix} ({len(ids)} advisories) [{why}]"
        (actionable if ok else blocked).append(line)

    orphans = find_orphans(pkgs)

    if args.quiet and not actionable and not orphans:
        return 0

    print(f"Dependency audit -- {len(pkgs)} packages, platform tag '{tag}'")
    print("=" * 72)

    if actionable:
        print("\nACTIONABLE -- a patched version is installable on this platform:")
        for line in actionable:
            print(f"  {line}")
        print("\n  Fix with: pip install --upgrade <package>")
    else:
        print("\nACTIONABLE: none.")

    if blocked:
        print("\nBLOCKED -- fix exists upstream but cannot be installed here:")
        for line in blocked:
            print(f"  {line}")
        print("\n  These are accepted risk, not neglect. See constraints-win-arm64.txt.")
        print("  Re-check periodically: if upstream resumes wheels this becomes actionable.")

    if orphans:
        print("\nORPHANS -- nothing requires them, no project code imports them:")
        for name in orphans:
            n_adv = len({v["id"] for v in findings.get(name, [])})
            suffix = f"  ({n_adv} advisories)" if n_adv else ""
            print(f"  {name}=={pkgs[name]}{suffix}")
        print(f"\n  Remove with: pip uninstall -y {' '.join(orphans)}")

    if not actionable and not blocked and not orphans:
        print("\nCLEAN -- no known vulnerabilities, no orphans.")

    print()
    return 1 if (actionable or orphans) else 0


if __name__ == "__main__":
    sys.exit(main())
