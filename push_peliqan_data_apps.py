#!/usr/bin/env python3
"""
Push locally-edited Data App scripts in scripts/ back up to Peliqan.

Matches each scripts/<id>_<slug>.py file to its Peliqan interface by the
leading numeric id in the filename (the convention fetch_peliqan_data_apps.py
uses), and PATCHes `raw_script` for any file whose content has changed since
the last fetch/push (tracked in scripts/.manifest.json).

To avoid clobbering an edit made directly in the Peliqan UI, each push first
re-fetches the interface's current raw_script and compares it against the
manifest's recorded hash. If it has drifted since the last fetch/push, the
push is skipped with a warning (use --force to override).

This is the script the GitHub Actions workflow
(.github/workflows/sync-peliqan-scripts.yml) runs on every push to main.

Usage:
    $env:PELIQAN_API_TOKEN = "your-token-here"
    python push_peliqan_data_apps.py                # push all changed scripts
    python push_peliqan_data_apps.py --dry-run       # show what would be pushed
    python push_peliqan_data_apps.py --force         # push even if remote drifted
    python push_peliqan_data_apps.py --scripts-dir scripts
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

import peliqan_common as pc

FILENAME_RE = re.compile(r"^(\d+)_.*\.py$")


def find_script_files(scripts_dir: str) -> list[tuple[int, str]]:
    """Return (interface_id, path) for every scripts/<id>_<slug>.py file."""
    found = []
    for path in glob.glob(os.path.join(scripts_dir, "*.py")):
        match = FILENAME_RE.match(os.path.basename(path))
        if match:
            found.append((int(match.group(1)), path))
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scripts-dir", default="scripts", help="Folder containing fetched/edited scripts (default: scripts)")
    parser.add_argument("--base-url", default=os.environ.get("PELIQAN_BASE_URL", pc.DEFAULT_BASE_URL), help="Peliqan instance base URL")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be pushed without making changes")
    parser.add_argument("--force", action="store_true", help="Push even if the remote script has changed since the last fetch/push")
    args = parser.parse_args()

    token = pc.get_token()
    manifest = pc.load_manifest(args.scripts_dir)
    script_files = find_script_files(args.scripts_dir)

    if not script_files:
        print(f"No scripts found in {args.scripts_dir}/ (expected files named like 123_my_app.py).")
        return

    pushed = skipped_unchanged = skipped_conflict = failed = 0

    for interface_id, path in sorted(script_files):
        key = str(interface_id)
        with open(path, "r", encoding="utf-8") as f:
            local_content = f.read()
        # Peliqan's raw_script never carries the local-dev shim - it's stripped
        # back off here so the live script stays exactly as originally authored.
        push_content = pc.without_local_dev_shim(local_content)
        push_hash = pc.content_hash(push_content)
        local_hash = pc.content_hash(local_content)
        entry = manifest.get(key, {})
        name = entry.get("name", os.path.basename(path))

        expected_remote_hash = entry.get("remote_hash")

        if push_hash == expected_remote_hash:
            skipped_unchanged += 1
            continue

        try:
            remote_detail = pc.fetch_interface_detail(args.base_url, token, interface_id)
        except Exception as e:
            print(f"  ! Failed to check remote state for '{name}' (id={interface_id}): {e}")
            failed += 1
            continue

        remote_raw = remote_detail.get("raw_script") or ""
        remote_hash = pc.content_hash(remote_raw)

        if remote_hash == push_hash:
            # What's live already matches what we'd push (e.g. both sides were
            # independently reverted back to the same content) - nothing to
            # do. Refresh the manifest so a stale expected_remote_hash doesn't
            # keep flagging this as drift on future runs.
            manifest[key] = {
                **entry,
                "name": name,
                "filename": os.path.basename(path),
                "local_hash": local_hash,
                "remote_hash": remote_hash,
            }
            skipped_unchanged += 1
            continue

        if expected_remote_hash is None:
            # No sync history for this id - only safe to push without asking
            # if there's genuinely nothing on the remote to clobber.
            if remote_raw and not args.force:
                print(
                    f"  ! Skipped '{name}' (id={interface_id}): no sync history for this script, "
                    "and Peliqan already has script content at this id. Run fetch_peliqan_data_apps.py "
                    "first to establish a baseline, or re-run with --force if you're sure."
                )
                skipped_conflict += 1
                continue
        elif remote_hash != expected_remote_hash and not args.force:
            print(
                f"  ! Skipped '{name}' (id={interface_id}): the script on Peliqan has changed "
                "since your last fetch/push. Re-run fetch_peliqan_data_apps.py to reconcile, "
                "or re-run with --force to overwrite the remote copy anyway."
            )
            skipped_conflict += 1
            continue

        if args.dry_run:
            print(f"  (dry-run) Would push '{name}' (id={interface_id}) <- {path} (shim stripped before push)")
            pushed += 1
            continue

        try:
            pc.update_interface_script(args.base_url, token, interface_id, push_content)
        except Exception as e:
            print(f"  ! Failed to push '{name}' (id={interface_id}): {e}")
            failed += 1
            continue

        manifest[key] = {
            **entry,
            "name": name,
            "filename": os.path.basename(path),
            "local_hash": local_hash,
            "remote_hash": push_hash,
        }
        print(f"  > Pushed '{name}' (id={interface_id})")
        pushed += 1

    if not args.dry_run:
        pc.save_manifest(args.scripts_dir, manifest)

    print(
        f"\nDone: {pushed} pushed, {skipped_unchanged} unchanged, "
        f"{skipped_conflict} conflict(s) skipped, {failed} failed."
    )
    if skipped_conflict or failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
