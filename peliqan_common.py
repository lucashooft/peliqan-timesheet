"""
Shared helpers for fetch_peliqan_data_apps.py and push_peliqan_data_apps.py.

Not meant to be run directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import Any

try:
    import requests
except ImportError:
    sys.exit(
        "Missing dependency 'requests'. Install it with:\n"
        "    pip install requests"
    )

DEFAULT_BASE_URL = "https://app.eu.peliqan.io"
MANIFEST_FILENAME = ".manifest.json"

# The dual-environment boilerplate Peliqan's own docs recommend prepending to
# a Data App script (see https://help.peliqan.io/low-code-python-data-apps/reference).
# Safe to leave in permanently: on Peliqan, RUN_CONTEXT already exists in
# globals() *before* the script body runs, so the "local" branch below never
# executes there - it only kicks in when you run the file locally.
LOCAL_DEV_SHIM = """\
if 'RUN_CONTEXT' in globals():  # Running on Peliqan
    RUN_ENV = 'peliqan'
else:  # Running outside of Peliqan
    RUN_ENV = 'local'
    from peliqan import Peliqan
    import streamlit as st
    import os
    api_key = os.getenv("PELIQAN_API_KEY")
    if not api_key:
        st.error("PELIQAN_API_KEY environment variable is not set.")
        st.stop()
    interface_id = os.getenv("PELIQAN_INTERFACE_ID", 0)
    pq = Peliqan(api_key)
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        if get_script_run_ctx() is not None:
            RUN_CONTEXT = "interactive"
    except Exception:
        RUN_CONTEXT = "background"
"""

# Used to detect whether a fetched raw_script already contains the shim
# (e.g. it was added directly in the Peliqan UI) so we don't double it up.
SHIM_MARKER = "RUN_CONTEXT' in globals()"


def get_token() -> str:
    token = os.environ.get("PELIQAN_API_TOKEN")
    if not token:
        sys.exit(
            "PELIQAN_API_TOKEN environment variable is not set.\n"
            "Get a personal API token in Peliqan under "
            "Admin > Security settings > API token, then set it, e.g.\n"
            '    $env:PELIQAN_API_TOKEN = "your-token-here"   (PowerShell)\n'
            "    export PELIQAN_API_TOKEN=your-token-here      (bash)"
        )
    return token


def _request(method: str, base_url: str, token: str, path: str, **kwargs) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    resp = requests.request(method, url, headers={"Authorization": f"JWT {token}"}, timeout=30, **kwargs)
    if resp.status_code == 401:
        sys.exit(f"401 Unauthorized calling {url} - check your PELIQAN_API_TOKEN.")
    resp.raise_for_status()
    return resp.json() if resp.content else None


def api_get(base_url: str, token: str, path: str) -> Any:
    return _request("GET", base_url, token, path)


def api_patch(base_url: str, token: str, path: str, payload: dict) -> Any:
    return _request("PATCH", base_url, token, path, json=payload)


def _as_list(data: Any) -> list:
    """Peliqan endpoints are documented as returning a bare list, but some
    accounts/instances wrap results (e.g. {"results": [...]}). Normalize
    either shape into a plain list."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "groups", "interfaces", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def fetch_data_apps(base_url: str, token: str) -> list[dict]:
    """GET /api/interfaces/ - list all interfaces (data apps) the user has access to."""
    return _as_list(api_get(base_url, token, "/api/interfaces/"))


def fetch_group_names(base_url: str, token: str) -> dict[int, str]:
    """GET /api/groups/ - used to resolve each data app's group id to a group name."""
    try:
        data = api_get(base_url, token, "/api/groups/")
    except requests.HTTPError:
        return {}
    names = {}
    for g in _as_list(data):
        if isinstance(g, dict) and "id" in g:
            names[g["id"]] = g.get("name", "")
    return names


def fetch_interface_detail(base_url: str, token: str, interface_id: int) -> dict:
    """GET /api/interfaces/{id}/ - the list endpoint doesn't include the script
    body, so each data app's full content (raw_script or flow) needs its own
    request."""
    return api_get(base_url, token, f"/api/interfaces/{interface_id}/")


def update_interface_script(base_url: str, token: str, interface_id: int, raw_script: str) -> Any:
    """PATCH /api/interfaces/{id}/ - push a new raw_script up to Peliqan."""
    return api_patch(base_url, token, f"/api/interfaces/{interface_id}/", {"raw_script": raw_script})


def slugify(name: str) -> str:
    slug = re.sub(r"[^\w\-]+", "_", (name or "").strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "unnamed"


def with_local_dev_shim(raw_script: str) -> str:
    """Prepend the Peliqan-recommended local-dev shim, unless it's already there."""
    body = raw_script or ""
    if SHIM_MARKER in body:
        return body
    return LOCAL_DEV_SHIM + "\n" + body


def without_local_dev_shim(content: str) -> str:
    """Inverse of with_local_dev_shim - strip the shim back off before pushing,
    so the live Peliqan-hosted raw_script stays exactly as originally authored."""
    text = content or ""
    prefix = LOCAL_DEV_SHIM + "\n"
    if text.startswith(prefix):
        return text[len(prefix):]
    return text


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def load_manifest(scripts_dir: str) -> dict:
    """The manifest (scripts/.manifest.json) records, per interface id, the
    content hash that was last known to match *both* the local file and the
    Peliqan-hosted script. fetch/push use it to detect local edits that
    haven't been pushed yet, and remote edits that haven't been pulled yet."""
    path = os.path.join(scripts_dir, MANIFEST_FILENAME)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(scripts_dir: str, manifest: dict) -> None:
    path = os.path.join(scripts_dir, MANIFEST_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
