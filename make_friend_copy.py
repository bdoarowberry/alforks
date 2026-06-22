#!/usr/bin/env python3
"""Produce a clean shareable AlForks copy, or a code-only update bundle.

Owner tool (run by you, not the recipient). Written in Python rather than
PowerShell because this machine enforces an AllSigned execution policy via
group policy, which blocks unsigned .ps1 even with -ExecutionPolicy Bypass.

Usage (from the repo root):
    python make_friend_copy.py --destination ../AlForks-for-bob   # full clean copy
    python make_friend_copy.py --package                          # code-only update zip

NON-DESTRUCTIVE: only ever reads the repo and writes to a new destination / zip;
it never deletes or modifies anything in the source.
"""

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

# Every local module app.py imports (directly or transitively) + launcher/docs.
# Omitting any .py makes the copy crash on startup with ImportError.
CODE_FILES = [
    "app.py", "detection.py", "cache_utils.py", "geo.py", "osm_breaker.py",
    "trail_match.py", "route_builder.py", "route_attempts.py",
    "route_suggestions.py", "sidebar_cache.py",
    "requirements.txt", "start.bat", "update.bat",
    "config.example.json", "VERSION", "CHANGELOG.md", "README.md",
]
CODE_FOLDERS = ["sync", "templates", "static"]

# Starter settings — shared config (not personal data). Seeded into a NEW copy
# only; never in update bundles, so a recipient's edits survive every update.
SEED_FILES = ["regions.json", "types.json"]

EXCLUDE_DIR_NAMES = {"__pycache__", ".tokens", ".alforks"}


def _iter_folder_files(src_dir: Path):
    """Yield (abs_path, rel_path_within_folder), skipping excluded dir names."""
    for p in src_dir.rglob("*"):
        if any(part in EXCLUDE_DIR_NAMES for part in p.relative_to(src_dir).parts):
            continue
        if p.is_file():
            yield p, p.relative_to(src_dir)


def make_copy(repo: Path, dest: Path) -> int:
    print(f"Source repo : {repo}\nDestination : {dest}\n")
    if dest.exists():
        print(f"ERROR: destination already exists: {dest}\n"
              f"Delete it first, or pass a different --destination.", file=sys.stderr)
        return 1
    try:
        dest.relative_to(repo)
        print(f"ERROR: destination must be OUTSIDE the source repo. Chosen: {dest}",
              file=sys.stderr)
        return 1
    except ValueError:
        pass  # good — outside the repo

    dest.mkdir(parents=True)
    copied = []
    for rel in CODE_FILES + SEED_FILES:
        src = repo / rel
        if src.is_file():
            shutil.copy2(src, dest / rel)
            copied.append(rel)
        elif rel in CODE_FILES:
            print(f"  ! missing (skipped): {rel}")
    for folder in CODE_FOLDERS:
        src_dir = repo / folder
        if not src_dir.is_dir():
            print(f"  ! missing folder (skipped): {folder}/")
            continue
        for src, rel in _iter_folder_files(src_dir):
            target = dest / folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        copied.append(folder + "/")

    total_mb = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file()) / (1024 * 1024)
    print("\nCopied into the clean copy:")
    for c in copied:
        print(f"    {c}")
    print(f"\nTotal size: {total_mb:.2f} MB\n")
    print(f"Clean copy ready at {dest}.")
    print("Zip this folder and send it. It contains no rides, no cache, and no secrets.")
    return 0


def make_package(repo: Path) -> int:
    vfile = repo / "VERSION"
    version = vfile.read_text(encoding="utf-8").strip() if vfile.exists() else "dev"
    out = repo / f"alforks-update-{version}.zip"
    print(f"Building code-only update bundle: {out.name}\n")
    if out.exists():
        print(f"ERROR: {out.name} already exists — delete it or bump VERSION.",
              file=sys.stderr)
        return 1
    added = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in CODE_FILES:
            src = repo / rel
            if src.is_file():
                z.write(src, rel)
                added.append(rel)
            else:
                print(f"  ! missing (skipped): {rel}")
        for folder in CODE_FOLDERS:
            src_dir = repo / folder
            if not src_dir.is_dir():
                print(f"  ! missing folder (skipped): {folder}/")
                continue
            for src, rel in _iter_folder_files(src_dir):
                z.write(src, (Path(folder) / rel).as_posix())
            added.append(folder + "/")
    size_mb = out.stat().st_size / (1024 * 1024)
    print("Included (code only — NO regions.json / types.json / data):")
    for a in added:
        print(f"    {a}")
    print(f"\nBundle: {out}  ({size_mb:.2f} MB)")
    print("Send this .zip to recipients; they apply it by double-clicking update.bat.")
    return 0


def main() -> int:
    repo = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Make a clean AlForks copy or an update bundle.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--destination", "-d", help="Make a full clean copy at this NEW directory.")
    g.add_argument("--package", action="store_true", help="Build a code-only update .zip.")
    args = ap.parse_args()

    if args.package:
        return make_package(repo)
    dest = Path(args.destination)
    dest = (repo / dest).resolve() if not dest.is_absolute() else dest.resolve()
    return make_copy(repo, dest)


if __name__ == "__main__":
    raise SystemExit(main())
