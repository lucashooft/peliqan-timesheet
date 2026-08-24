#!/usr/bin/env python3
"""
Fetch all Data Apps (Peliqan API: "interfaces") on your Peliqan account.

Auth: Peliqan REST API uses JWT bearer auth via the `Authorization: JWT <token>`
header. Get a personal API token from Peliqan under:
    Admin > Security settings > API token
(API-side this is GET/POST https://app.eu.peliqan.io/api/user/api_token/)

Usage:
    # PowerShell
    $env:PELIQAN_API_TOKEN = "your-token-here"
    python fetch_peliqan_data_apps.py

    # optional: point at a different Peliqan instance (default: app.eu.peliqan.io)
    $env:PELIQAN_BASE_URL = "https://app.peliqan.io"

    # optional flags
    python fetch_peliqan_data_apps.py --output data_apps.json --csv data_apps.csv

    # each data app's underlying script/flow is also saved as its own file
    # under scripts/ (created automatically). Customize with --scripts-dir,
    # or skip entirely with --skip-scripts.

Local dev + push-back: each saved .py script gets Peliqan's recommended
local-dev shim prepended (so `pq`, `st`, and RUN_CONTEXT work when you run it
outside Peliqan too - see requirements.txt). Fetch also tracks a content hash
per interface in scripts/.manifest.json; if a local file has been edited but
not pushed yet, re-fetching won't clobber it (the fresh remote copy is saved
alongside as *.remote.py instead) unless you pass --force. Use
push_peliqan_data_apps.py to send local edits back up to Peliqan.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

try:
    import requests
except ImportError:
    sys.exit(
        "Missing dependency 'requests'. Install it with:\n"
        "    pip install requests"
    )

import peliqan_common as pc


def save_interface_script(scripts_dir: str, app: dict, detail: dict, manifest: dict, force: bool) -> str:
    """Write a data app's script (or flow) to its own file under scripts_dir.
    Returns a short status string for the summary print."""
    interface_id = app.get("id")
    key = str(interface_id)
    filename_base = f"{interface_id}_{pc.slugify(app.get('name', ''))}"
    raw_script = detail.get("raw_script")
    flow = detail.get("flow")

    if raw_script:
        content = pc.with_local_dev_shim(raw_script)
        path = os.path.join(scripts_dir, f"{filename_base}.py")
        entry = manifest.get(key)

        if not force and entry and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
            existing_hash = pc.content_hash(existing)
            if existing_hash != entry.get("local_hash") and existing_hash != pc.content_hash(content):
                # Local file has edits that haven't been pushed yet, and they
                # don't already match what's live - don't overwrite them.
                # Save the current remote copy alongside so it can be
                # diffed/merged by hand.
                remote_path = os.path.join(scripts_dir, f"{filename_base}.remote.py")
                with open(remote_path, "w", encoding="utf-8") as f:
                    f.write(content)
                return "conflict"

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        manifest[key] = {
            "name": app.get("name", ""),
            "filename": os.path.basename(path),
            "group": app.get("group"),
            # local_hash: what's on disk here (shim included) - used to
            # detect un-pushed local edits before a future fetch overwrites.
            "local_hash": pc.content_hash(content),
            # remote_hash: what's actually confirmed stored as raw_script on
            # Peliqan right now (no shim, until it's been pushed once) - used
            # by push_peliqan_data_apps.py to detect remote-side drift.
            "remote_hash": pc.content_hash(raw_script or ""),
        }
        return "script"

    if flow:
        if isinstance(flow, str):
            try:
                flow = json.loads(flow)
            except ValueError:
                pass
        path = os.path.join(scripts_dir, f"{filename_base}.flow.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(flow, f, indent=2)
        return "flow"

    return "empty"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", "-o", default="data_apps.json", help="Path to write JSON output (default: data_apps.json)")
    parser.add_argument("--csv", default=None, help="Optional path to also write a CSV summary")
    parser.add_argument("--base-url", default=os.environ.get("PELIQAN_BASE_URL", pc.DEFAULT_BASE_URL), help="Peliqan instance base URL")
    parser.add_argument("--scripts-dir", default="scripts", help="Folder to save each data app's script into (created if missing; default: scripts)")
    parser.add_argument("--skip-scripts", action="store_true", help="Don't fetch/save individual data app scripts, only the summary JSON/CSV")
    parser.add_argument("--force", action="store_true", help="Overwrite local scripts even if they have unpushed edits")
    args = parser.parse_args()

    token = pc.get_token()
    data_apps = pc.fetch_data_apps(args.base_url, token)
    group_names = pc.fetch_group_names(args.base_url, token)

    for app in data_apps:
        app["group_name"] = group_names.get(app.get("group"), "")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data_apps, f, indent=2)
    print(f"Saved {len(data_apps)} data app(s) to {args.output}")

    if args.csv:
        fieldnames = ["id", "name", "type", "group", "group_name", "url", "created_on", "updated_on"]
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data_apps)
        print(f"Saved CSV summary to {args.csv}")

    if data_apps:
        print("\nID    Type        Group                Name")
        print("-" * 70)
        for app in data_apps:
            print(f"{app.get('id', ''):<5} {app.get('type', ''):<11} {app.get('group_name', ''):<20} {app.get('name', '')}")
    else:
        print("No data apps found on this account.")

    if data_apps and not args.skip_scripts:
        os.makedirs(args.scripts_dir, exist_ok=True)
        manifest = pc.load_manifest(args.scripts_dir)
        print(f"\nFetching individual scripts into {args.scripts_dir}/ ...")
        counts = {"script": 0, "flow": 0, "empty": 0, "error": 0, "conflict": 0}
        for app in data_apps:
            interface_id = app.get("id")
            label = app.get("name", f"id={interface_id}")
            if interface_id is None:
                continue
            try:
                detail = pc.fetch_interface_detail(args.base_url, token, interface_id)
                status = save_interface_script(args.scripts_dir, app, detail, manifest, args.force)
            except requests.HTTPError as e:
                print(f"  ! Failed to fetch '{label}' (id={interface_id}): {e}")
                status = "error"
            counts[status] += 1
            if status == "flow":
                print(f"  i '{label}' uses the visual flow editor (no raw_script) - saved its flow definition as JSON instead.")
            elif status == "empty":
                print(f"  i '{label}' has no script or flow content to save.")
            elif status == "conflict":
                print(
                    f"  ! '{label}' (id={interface_id}) has local edits that haven't been pushed yet - "
                    f"left your copy alone and saved the current Peliqan version as "
                    f"{interface_id}_{pc.slugify(label)}.remote.py instead. Review/merge by hand, "
                    "push your local version with push_peliqan_data_apps.py, or re-run with --force "
                    "to overwrite your local copy."
                )

        pc.save_manifest(args.scripts_dir, manifest)
        print(
            f"\nDone: {counts['script']} script(s), {counts['flow']} flow definition(s), "
            f"{counts['empty']} empty, {counts['conflict']} conflict(s), {counts['error']} failed."
        )


if __name__ == "__main__":
    main()
