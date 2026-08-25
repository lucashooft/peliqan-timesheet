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
ts_mcp_server (v3.0 - refactored)

MCP server exposing the ts_prod timesheet/CRM tables (warehouse dw_3202)
as tools for AI agents.

Add a new tool: write a function and decorate it with @tool(min_scope=...).

NOTE on auth: authenticate_user() introspects the Bearer token against
Google's tokeninfo endpoint and checks aud/azp against our own
google_oauth_client_id secret, confirming the token was actually issued
for THIS app - not just any valid Google login for any Google app. This
was the gap discussed at length: without this check, any valid Google
token whose email happened to match a ts_prod.users row would have been
accepted, regardless of which app issued it. Resolved.

Changes from v2.2:
  - Auth reads the x-api-key HEADER (matches ts_api_get_handler /
    ts_api_post_handler) instead of a query-string param, and nothing
    that could contain a secret is logged.
  - Removed list_connections / list_tables / list_columns / execute_query,
    which exposed the whole Peliqan account rather than just this app.
    Replaced with a scoped, SELECT-only run_report_query for admins.
  - Removed the unused OpenAI/RAG connection and create_embedding() -
    nothing called them.
  - tools/call now only dispatches to functions registered via @tool,
    instead of any function name in the module's globals(). Previously
    a scope-1 key could call an internal helper like create_row directly
    and bypass the higher scope required by the tool that wraps it.
  - Resource-specific special cases (default status, timetable's
    server-managed fields, the project date-range check) moved out of
    create_row and into RESOURCE_CONFIG as declarative hooks, so adding
    a resource no longer means editing shared code.
  - Dropped the `mcp` class wrapper around the tool decorator - it had
    no instance state, so it's now a plain function.

Changes from v3.1:
  - Added add_user_to_client / remove_user_from_client. clients.user_list
    is a many-to-many field that never comes through dbconn.fetch or
    dbconn.update at all (confirmed empirically). The working write path
    is a raw SQL INSERT/DELETE directly against the internal junction
    table Peliqan itself uses for this relation. client_id/user_id are
    validated as plain positive integers before ever reaching the SQL
    string, so this is not injectable despite being raw SQL. NOTE: the
    junction table's internal name changes if the field is ever deleted
    and recreated - confirmed this happened once already; if these two
    tools start failing, re-check clients' user_list field metadata for
    its current relation_id.

Changes from v3.2 (per the confirmed scope table):
  - create_task moved from scope-2-only to scope 1+, gated by a new
    _check_task_client_access (same client user_list restriction as
    logging time, but keyed off project_id directly since there's no
    task_id yet at creation time).
  - approve_entry and run_report_query moved from scope-3-only to
    scope 2+ (managers can now approve entries and run report queries,
    not just admins).
  - Added update_time_entry: edit any field of one of YOUR OWN entries,
    only while the entry isn't approved yet.

Changes from v3.3:
  - get_my_time_entries now reads from ts_reporting.fact_timetable (a
    query table joining timetable -> tasks -> projects -> clients ->
    users) instead of raw ts_prod.timetable - richer context in one call,
    and a clean int user_id (unlike timetable.user_id itself, which is
    text). Every write path that touches ts_prod.timetable also
    invalidates the fact_timetable cache entry, since that's a separate
    cache key that doesn't clear automatically.

Changes from v3.4:
  - Added get_available_projects, so scope 1 has a way to discover a
    valid project_id for create_task even for a brand-new project with
    zero tasks yet (get_available_tasks alone wouldn't surface it).

Changes from v3.5 (intent detection, per the Nimbl MCP spec):
  - Every tool's exposed schema includes an optional tool_intent field,
    injected centrally in the tool() decorator rather than added to each
    tool function individually. handle_tool_call pops it out before
    calling the real function and logs it best-effort to
    ts_prod.tool_intent_log (a logging failure never blocks the actual
    tool call).

Changes from v3.6 (removed the dependency on a separate ts_auth_server):
  - Auth no longer validates a self-issued JWT from ts_auth_server.
    authenticate_user() now checks the incoming Bearer token directly
    against Google's tokeninfo endpoint (see NOTE above).
  - /.well-known/openid-configuration is served directly by this script
    (self-referential issuer), returning Google's real authorize/token
    endpoints. An MCP client talks to Google directly for login and code
    exchange; this script only ever sees the resulting access_token.
  - ts_auth_server (app_id 11577) is NOT deleted - kept as a rollback
    path. The MCP client's own connector settings need to be re-pointed
    at the real Google Cloud OAuth client_id/secret (Google Console),
    not the old placeholder "ts-mcp-claude" client_id.

Changes from v3.7:
  - Added create_tasks: bulk variant of create_task, same JSON-array
    pattern as log_time_entries. Each task goes through create_row
    individually, so per-task validation and the scope-1 client-access
    restriction both still apply exactly as they do for a single
    create_task call.

Changes from v3.8:
  - CURRENT_USER reverted from a contextvars-backed proxy back to a
    plain global dict, reassigned via `global CURRENT_USER` inside
    handler(). The contextvars approach was for per-request isolation
    under concurrent execution, but was reverted per explicit request -
    back to the simpler, original pattern.

Changes from v3.9:
  - authenticate_user() switched from /userinfo to /tokeninfo and gained
    the aud/azp check described in the NOTE above (this is what the NOTE
    used to flag as unresolved).
  - Removed an unconditional print(tokeninfo) left in authenticate_user()
    that logged the full tokeninfo response, including the user's email,
    on every single authenticated request regardless of DEBUG - the same
    kind of leak log_request/log_response are deliberately built to avoid.
  - Confirmed the registered API endpoint (/timesheet/mcp/*/*, two
    wildcards) correctly matches WELLKNOWN_PROTECTED_RESOURCE_PATH's two
    path segments (.well-known + oauth-protected-resource) - this had
    been flagged as unverified, now confirmed consistent.

Changes from v3.10 (cleanup):
  - validate_and_convert_field's foreign_keys branch no longer calls
    validate_positive_int(value) explicitly before calling
    validate_foreign_key(ref, value) - the latter already performs that
    exact check internally (it has to, since it's also called standalone
    e.g. in add_user_to_client, without that explicit pre-check). The
    explicit call was a genuine duplicate, doing the same regex match
    twice on every foreign-key field. One user-visible side effect: a
    non-numeric foreign-key value now gets the generic "No '{ref}' exists
    with id {value}." message instead of the more specific "must be a
    positive integer." - a deliberate, accepted trade-off for removing
    the duplicate check, not an oversight.

Changes from v3.11:
  - Translated every remaining Dutch string in the script (validation
    error messages, a handful of docstrings, a few comments) to English.
    No logic changed - this is a language-only pass. Everything a tool
    caller sees back (error messages included) is now English.

Changes from v3.12:
  - Added delete_entry: scope 1 can only delete their own entries, scope
    2/3 can delete any entry, but nobody - regardless of scope - can
    delete an entry that's already approved.

Changes from v3.13:
  - update_time_entry's ownership check now matches delete_entry's:
    ownership only applies below scope 2. Scope 2/3 can now update any
    entry (not just their own), same as delete_entry. This reverses the
    v3.2 behavior, which required ownership regardless of scope -
    explicitly changed on request, not a bug fix.

Changes from v3.14:
  - Submitted and validated weeks are now locked against writes, closing
    a hole where the MCP could still add, edit, move or delete entries in
    a week the UI had already frozen. ts_prod.timetable_submissions is
    consulted on every timetable write (_week_lock_error): status
    'submitted' or 'confirmed' refuses the call. Creates hook in through
    RESOURCE_CONFIG["timetable"]["cross_field_check"], so log_time_entry
    and log_time_entries are both covered; update_time_entry checks the
    entry's current week AND, when the date changes, the week it would
    move into; delete_entry checks the entry's week. Binds every scope,
    like the per-entry approved lock. approve_entry is deliberately NOT
    locked - approving a submitted week is the point of submitting it.
"""

import json
import inspect
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
from typing import get_type_hints, List, Dict, Any, Optional

DEBUG = False  # set True only for local troubleshooting

DW_NAME = pq.DW_NAME
dbconn = pq.dbconnect(DW_NAME)

# =====================================================================
# Resource / discovery identifiers. AUTHORIZATION_SERVER_URL is
# deliberately the same as RESOURCE_URL - this script is both, per
# Peliqan's own "MCP Server with Google oAuth" reference pattern, since
# it never issues anything itself, only forwards to Google's real
# endpoints and checks what comes back.
# =====================================================================

RESOURCE_URL = "https://api.eu.peliqan.io/3202/timesheet/mcp"
AUTHORIZATION_SERVER_URL = RESOURCE_URL

WELLKNOWN_PROTECTED_RESOURCE_PATH = "/3202/timesheet/mcp/.well-known/oauth-protected-resource"

TOOL_INTENT_DESCRIPTION = (
    "Explain the original intent of the user, taking into account previous instructions from the user, "
    "so that the full intent from the user and reason to invoke this tool is clear. Never include credentials"
)

# =====================================================================
# MCP tool registry + decorator
# =====================================================================

MCP_TOOLS = []          # tool metadata sent to the client on tools/list
TOOL_PERMISSIONS = {}    # tool_name -> minimum scope required
TOOL_FUNCTIONS = {}      # tool_name -> function (only these are callable)
CURRENT_USER = {}        # set per request by authenticate_user(), plain global dict


def tool(min_scope: int = 1):
    """Decorator: register a function as an MCP tool, gated by min_scope."""
    def decorator(func):
        sig = inspect.signature(func)
        type_hints = get_type_hints(func)

        raw_doc = func.__doc__ or ""
        param_docs = {}
        for line in raw_doc.splitlines():
            line = line.strip()
            if line.startswith(":param"):
                try:
                    _, rest = line.split("param", 1)
                    name, desc = rest.split(":", 1)
                    param_docs[name.strip()] = desc.strip()
                except ValueError:
                    pass

        type_map = {"str": "string", "int": "number", "float": "number", "bool": "boolean"}
        properties = {}
        required = []

        for name, param in sig.parameters.items():
            hint = type_hints.get(name, "any")
            type_str = hint.__name__ if isinstance(hint, type) else str(hint)
            prop = {
                "type": type_map.get(type_str, "string"),
                "description": param_docs.get(name, ""),
            }
            if param.default is not inspect._empty:
                prop["default"] = param.default
            else:
                required.append(name)
            properties[name] = prop

        properties["tool_intent"] = {
            "type": "string",
            "description": TOOL_INTENT_DESCRIPTION,
        }

        MCP_TOOLS.append({
            "name": func.__name__,
            "description": raw_doc.strip().split("\n")[0] if raw_doc else "",
            "inputSchema": {"type": "object", "properties": properties, "required": required},
        })
        TOOL_PERMISSIONS[func.__name__] = min_scope
        TOOL_FUNCTIONS[func.__name__] = func
        return func
    return decorator


def get_tool_response_format(tool_name):
    func = TOOL_FUNCTIONS.get(tool_name)
    annotation = inspect.signature(func).return_annotation
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    return str(annotation).replace("typing.", "").replace("class '", "").replace("'>", "")


def require_scope(min_scope: int) -> bool:
    return CURRENT_USER.get("scope", 0) >= min_scope


def log_tool_intent(tool_name, tool_intent):
    """Best-effort: a logging failure must never block the actual tool call."""
    if not tool_intent:
        return
    try:
        dbconn.insert(DW_NAME, "ts_prod", "tool_intent_log", {
            "logged_at": str(int(time.time())),
            "tool_name": tool_name,
            "user_id": CURRENT_USER.get("user_id"),
            "scope": CURRENT_USER.get("scope"),
            "tool_intent": tool_intent,
        })
    except Exception:
        pass


# =====================================================================
# Auth - Google token check, no separate authorization server
# =====================================================================

def authenticate_user(request):
    """
    Authenticate by introspecting the Bearer token against Google's
    tokeninfo endpoint, which (unlike /userinfo) returns aud/azp - the
    check that confirms this token was actually issued for OUR Google
    Cloud app, not just any valid Google login.
    """
    headers = {str(k).lower(): v for k, v in (request.get("headers", {}) or {}).items()}
    auth_header = headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    access_token = auth_header[len("Bearer "):].strip()

    try:
        tokeninfo_url = f"https://oauth2.googleapis.com/tokeninfo?access_token={urllib.parse.quote(access_token)}"
        with urllib.request.urlopen(tokeninfo_url, timeout=10) as resp:
            tokeninfo = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None  # expired, revoked, or invalid token

    google_client_id = pq.get_secret("google_oauth_client_id")
    if google_client_id not in (tokeninfo.get("aud"), tokeninfo.get("azp")):
        return None  # token was not issued for our own Google Cloud app

    email = tokeninfo.get("email", "")
    if not email:
        return None

    user = next(
        (u for u in fetch_cached("ts_prod", "users") if str(u.get("email", "")).lower() == email.lower()),
        None,
    )
    if not user:
        return None

    return {
        "user_id": user.get("id"),
        "user_name": user.get("name"),
        "email": email,
        "scope": user.get("scope") or 0,
    }


def mcp_response_oauth_protected_resource():
    # response for call to <mcp_server>/.well-known/oauth-protected-resource
    return {
        "resource": RESOURCE_URL,
        "authorization_servers": [AUTHORIZATION_SERVER_URL],
        "scopes_supported": ["openid", "email", "profile"],
        "bearer_methods_supported": ["header"],
    }


def mcp_response_authorization_server_openid_config():
    # response for call to <authorization_server>/.well-known/openid-configuration
    return {
        "issuer": AUTHORIZATION_SERVER_URL,
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
    }


# =====================================================================
# Resource config: schema/table, validation rules, defaults, hooks
# =====================================================================

CACHE_TTL = 30
MAX_DESCRIPTION_LEN = 1000

FULL_NAME_REGEX = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 &'\-]{1,99}$")
POSITIVE_INT_REGEX = re.compile(r"^[1-9][0-9]*$")

CLIENT_STATUS = {"prospect", "active", "churned"}
PROJECT_STATUS = {"planned", "active", "on_hold", "done"}
TASK_STATUS = {"todo", "in_progress", "done", "blocked"}


def _timetable_server_managed():
    """Server-controlled fields for a new timetable row - never trust client input for these."""
    return {
        "user_id": CURRENT_USER["user_id"],
        "date_inserted": datetime.utcnow().date().isoformat(),
        "approved": False,
        "approved_by": None,
    }

def _invalidate_timetable_caches():
    """
    Anything that writes to ts_prod.timetable must also invalidate
    fact_timetable's cache entry, since it's a separate cache key that
    derives from the same underlying data but isn't cleared automatically.
    """
    clear_cache("ts_prod", "timetable")
    clear_cache("ts_reporting", "fact_timetable")

def _fetch_client_user_links():
    """
    Many-to-many fields (like clients.user_list) never come through
    dbconn.fetch(schema, table) at all - confirmed empirically. The only
    way to read it is the internal junction table Peliqan itself queries
    for this relation. That internal name changes if the field is ever
    deleted and recreated - confirmed this happened once already.
    """
    key = ("ts_prod", "_client_user_links")
    now = time.time()
    if key in CACHE:
        cached_at, links = CACHE[key]
        if now - cached_at < CACHE_TTL:
            return links

    rows = dbconn.fetch(DW_NAME, query=(
        "SELECT source_table_id, target_table_id "
        "FROM _pq_metadata._pq_rl_1339ee9e"
    ))
    links = {}
    for row in rows:
        links.setdefault(row["source_table_id"], set()).add(row["target_table_id"])

    CACHE[key] = (now, links)
    return links
    
def _user_authorized_for_client(client_id, user_id):
    links = _fetch_client_user_links()
    return user_id in links.get(client_id, set())

def _check_entry_client_access(converted):
    """Used by create_row("timetable", ...) - task_id is present, resolve through it."""
    if CURRENT_USER.get("scope", 0) >= 2:
        return None  # only scope 1 is restricted, per what you confirmed
    task = find_by_id(fetch_cached("ts_prod", "tasks"), converted["task_id"])
    project = find_by_id(fetch_cached("ts_prod", "projects"), task.get("project_id")) if task else None
    if not project or not _user_authorized_for_client(project.get("client_id"), CURRENT_USER["user_id"]):
        return "You are not authorized to log time for this client."
    return None

def _check_task_client_access(converted):
    """Used by create_row("tasks", ...) - only project_id exists yet, no task_id."""
    if CURRENT_USER.get("scope", 0) >= 2:
        return None  # only scope 1 is restricted
    project = find_by_id(fetch_cached("ts_prod", "projects"), converted["project_id"])
    if not project or not _user_authorized_for_client(project.get("client_id"), CURRENT_USER["user_id"]):
        return "You are not authorized to create tasks for this client."
    return None

def _check_project_dates(converted):
    if converted["end_date"] < converted["start_date"]:
        return "'end_date' cannot be before 'start_date'."
    return None


# ---- week locks (ts_prod.timetable_submissions) ----------------------
#
# Submitting a week freezes it. timetable_submissions holds one row per
# (user, week); while its status is 'submitted' or 'confirmed' no entry
# in that week may be added, changed, moved or deleted - the same lock
# ts_my_week / ts_weekly_calendar apply to their grids. Without this the
# MCP was a way around those grids: an agent could still log, edit or
# delete inside a week the user had already handed in.
#
# Absolute, like the per-entry approved lock: it binds every scope, so a
# manager who needs to fix a submitted week unsubmits it first. A
# validated week cannot be reopened at all - validating also sets
# approved=true on every entry, which the approved checks already refuse.

SUBMISSIONS_TABLE = "timetable_submissions"
LOCKED_WEEK_STATUSES = {"submitted": "submitted for approval",
                        "confirmed": "validated"}


def _week_start_of(value):
    """Monday of the week a timetable 'date' falls in, or None if unreadable."""
    # [:19] keeps 'YYYY-MM-DDTHH:MM:SS' and drops any fractional seconds or
    # 'Z' suffix - Python 3.10's fromisoformat (the runtime) rejects those,
    # and an unread date here would fail OPEN, letting a locked week through.
    try:
        parsed = datetime.fromisoformat(str(value).strip()[:19])
    except (ValueError, TypeError, AttributeError):
        return None
    return parsed.date() - timedelta(days=parsed.weekday())


def _week_lock_error(user_id, entry_date, action):
    """The reason this week is closed, or None when it is open."""
    week_start = _week_start_of(entry_date)
    if week_start is None:
        return None      # unreadable date: the field-level checks report that
    for row in fetch_cached("ts_prod", SUBMISSIONS_TABLE):
        if str(row.get("user_id")) != str(user_id):
            continue
        if str(row.get("week_start_date") or "")[:10] != week_start.isoformat():
            continue
        status = str(row.get("status") or "").strip().lower()
        state = LOCKED_WEEK_STATUSES.get(status)
        if state:
            hint = " Unsubmit that week first." if status == "submitted" else ""
            return (f"The week of {week_start.isoformat()} has been {state} "
                    f"and is locked: {action}.{hint}")
    return None


def _check_entry_week_open(converted):
    """
    create_row("timetable", ...) - refuse to log into a locked week.

    Rides on the cross_field_check hook rather than access_check, which
    timetable already uses for its client restriction. Both run before
    anything is written, which is all this needs.
    """
    return _week_lock_error(CURRENT_USER.get("user_id"), converted.get("date"),
                            "no new entries can be added to it")


RESOURCE_CONFIG = {
    "teams": {
        "schema": "ts_prod", "table": "teams", "min_scope_write": 3,
        "required": {"name"}, "unique": {"name"},
    },
    "user_roles": {
        "schema": "ts_prod", "table": "user_roles", "min_scope_write": 3,
        "required": {"name"}, "unique": {"name"},
    },
    "clients": {
        "schema": "ts_prod", "table": "clients", "min_scope_write": 2,
        "required": {"name", "peliqan_account_id"}, "optional": {"status"},
        "unique": {"name"}, "enums": {"status": CLIENT_STATUS},
        "defaults": {"status": "prospect"},
    },
    "projects": {
        "schema": "ts_prod", "table": "projects", "min_scope_write": 2,
        "required": {"name", "client_id", "status", "start_date", "end_date"},
        "foreign_keys": {"client_id": "clients"},
        "enums": {"status": PROJECT_STATUS},
        "date_fields": {"start_date", "end_date"},
        "cross_field_check": _check_project_dates,
    },
    "tasks": {
        "schema": "ts_prod", "table": "tasks", "min_scope_write": 1,
        "required": {"name", "project_id", "status", "billable"}, "optional": {"description"},
        "unique_together": ("project_id", "name"),
        "foreign_keys": {"project_id": "projects"},
        "enums": {"status": TASK_STATUS},
        "access_check": _check_task_client_access,
    },
    "timetable": {
        "schema": "ts_prod", "table": "timetable", "min_scope_write": 1,
        "required": {
            "task_id", "date", "duration",
            "internal_description", "external_description",
        },
        "foreign_keys": {"task_id": "tasks"},
        "datetime_fields": {"date"},
        "server_managed": _timetable_server_managed,
        "cross_field_check": _check_entry_week_open,
        "access_check": _check_entry_client_access,
    },
}

def _parse_entry_date(row: dict) -> Optional[date]:
    """Parse row['entry_date'] into a date, or None if missing/invalid."""
    try:
        return datetime.fromisoformat(row.get("entry_date", "")).date()
    except (ValueError, TypeError):
        return None
 
 
def _my_rows() -> list:
    """All fact_timetable rows belonging to the current user."""
    user_id = CURRENT_USER.get("user_id")
    if user_id is None:
        return []
    return [
        r for r in fetch_cached("ts_reporting", "fact_timetable")
        if r.get("user_id") == user_id
    ]

# =====================================================================
# Cache
# =====================================================================

CACHE = {}


def fetch_cached(schema: str, table: str):
    key = (schema, table)
    now = time.time()
    if key in CACHE:
        cached_at, rows = CACHE[key]
        if now - cached_at < CACHE_TTL:
            return rows
    rows = dbconn.fetch(DW_NAME, schema, table) or []
    CACHE[key] = (now, rows)
    return rows


def clear_cache(schema: str, table: str):
    CACHE.pop((schema, table), None)


def find_by_id(rows, row_id):
    return next((r for r in rows if r.get("id") == row_id), None)


# =====================================================================
# Field-level validation
# =====================================================================

def validate_positive_int(value) -> bool:
    return bool(POSITIVE_INT_REGEX.match(str(value)))


def validate_name(name) -> bool:
    return isinstance(name, str) and bool(FULL_NAME_REGEX.match(name.strip())) and 2 <= len(name.strip()) <= 100


def validate_required(data: dict, required_fields: set):
    for field in required_fields:
        value = data.get(field)
        if value is None:
            return f"'{field}' is required."
        if isinstance(value, str) and not value.strip():
            return f"'{field}' cannot be empty."
    return None


def validate_date(date_string: str):
    """Expects DD-MM-YYYY. Returns a date object or None."""
    try:
        return datetime.strptime(date_string.strip(), "%d-%m-%Y").date()
    except (ValueError, AttributeError):
        return None


def validate_datetime(datetime_string: str):
    """Expects DD-MM-YYYY HH:MM. Returns a datetime object or None."""
    try:
        return datetime.strptime(datetime_string.strip(), "%d-%m-%Y %H:%M")
    except (ValueError, AttributeError):
        return None


def validate_foreign_key(resource: str, record_id) -> bool:
    if not validate_positive_int(record_id):
        return False
    config = RESOURCE_CONFIG[resource]
    rows = fetch_cached(config["schema"], config["table"])
    return any(row.get("id") == int(record_id) for row in rows)


def validate_unique(rows, field: str, value) -> bool:
    value = str(value).strip().lower()
    return not any(str(row.get(field, "")).strip().lower() == value for row in rows)


def validate_unique_together(rows, fields: tuple, data: dict) -> bool:
    return not any(all(row.get(f) == data.get(f) for f in fields) for row in rows)


def validate_and_convert_field(config: dict, field: str, value):
    """Validate + convert one field. Returns (value, None) or (None, error)."""
    foreign_keys = config.get("foreign_keys", {})
    enums = config.get("enums", {})
    date_fields = config.get("date_fields", set())
    datetime_fields = config.get("datetime_fields", set())

    if field in foreign_keys:
        ref = foreign_keys[field]
        if not validate_foreign_key(ref, value):
            return None, f"No '{ref}' exists with id {value}."
        return int(value), None

    if field in enums:
        if not isinstance(value, str):
            return None, f"'{field}' must be a string."
        normalized = value.strip().lower()
        if normalized not in enums[field]:
            return None, f"'{field}' must be one of {sorted(enums[field])}."
        return normalized, None

    if field in datetime_fields:
        parsed = validate_datetime(str(value))
        if not parsed:
            return None, f"'{field}' must be in the format DD-MM-YYYY HH:MM."
        return parsed.isoformat(sep=" "), None

    if field in date_fields:
        parsed = validate_date(str(value))
        if not parsed:
            return None, f"'{field}' must be in the format DD-MM-YYYY."
        return parsed.isoformat(), None

    if field == "duration":
        if not (isinstance(value, (int, float)) and value > 0):
            return None, "'duration' must be a number greater than 0."
        return value, None

    if field == "billable":
        if not isinstance(value, bool):
            return None, "'billable' must be a boolean."
        return value, None

    if field == "name":
        if not validate_name(value):
            return None, (
                f"'{field}' must be 2-100 characters and may only contain letters, digits, "
                f"spaces, &, ' and -."
            )
        return value.strip(), None

    if field in {"description", "internal_description", "external_description"}:
        if not isinstance(value, str) or not value.strip():
            return None, f"'{field}' cannot be empty."
        cleaned = value.strip()
        if len(cleaned) > MAX_DESCRIPTION_LEN:
            return None, f"'{field}' must be at most {MAX_DESCRIPTION_LEN} characters."
        return cleaned, None

    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None, f"'{field}' cannot be empty."
        return cleaned, None

    return value, None


def get_created_row(config: dict, converted: dict):
    """Look up the row we just inserted (dbconn.insert doesn't return an id)."""
    rows = fetch_cached(config["schema"], config["table"])

    unique_fields = config.get("unique")
    if unique_fields:
        field = next(iter(unique_fields))
        value = str(converted.get(field, "")).strip().lower()
        return next((r for r in rows if str(r.get(field, "")).strip().lower() == value), None)

    unique_together = config.get("unique_together")
    if unique_together:
        return next((r for r in rows if all(r.get(f) == converted.get(f) for f in unique_together)), None)

    # No natural unique key (e.g. timetable) - exact field-value matching is
    # fragile whenever a stored type differs from what was inserted. The
    # row we just created reliably has the highest id in the table.
    if not rows:
        return None
    return max(rows, key=lambda r: r.get("id") or 0)


def create_row(resource: str, data: dict) -> dict:
    """Generic, config-driven create for any resource in RESOURCE_CONFIG.

    Not itself an MCP tool - only reachable through the @tool-wrapped
    create_* functions below, each of which enforces its own min_scope.
    """
    config = RESOURCE_CONFIG[resource]
    schema, table = config["schema"], config["table"]

    required_fields = set(config.get("required", set()))
    optional_fields = set(config.get("optional", set()))
    allowed_fields = required_fields | optional_fields

    unknown = set(data.keys()) - allowed_fields
    if unknown:
        return {"success": False, "error": f"Unknown field(s) for '{resource}': {', '.join(sorted(unknown))}."}

    error = validate_required(data, required_fields)
    if error:
        return {"success": False, "error": error}

    converted = {}
    for field in allowed_fields:
        if field not in data:
            continue
        value, error = validate_and_convert_field(config, field, data[field])
        if error:
            return {"success": False, "error": error}
        converted[field] = value

    for field, default in config.get("defaults", {}).items():
        converted.setdefault(field, default)

    cross_field_check = config.get("cross_field_check")
    if cross_field_check:
        error = cross_field_check(converted)
        if error:
            return {"success": False, "error": error}

    access_check = config.get("access_check")
    if access_check:
        error = access_check(converted)
        if error:
            return {"success": False, "error": error}

    rows = fetch_cached(schema, table)

    for field in config.get("unique", set()):
        if field in converted and not validate_unique(rows, field, converted[field]):
            return {"success": False, "error": f"'{field}' with value '{converted[field]}' already exists."}

    unique_together = config.get("unique_together")
    if unique_together and not validate_unique_together(rows, unique_together, converted):
        combo = ", ".join(f"{f}={converted.get(f)}" for f in unique_together)
        return {"success": False, "error": f"A row with {combo} already exists."}

    server_managed = config.get("server_managed")
    if server_managed:
        converted.update(server_managed())

    dbconn.insert(DW_NAME, schema, table, converted)
    if (schema, table) == ("ts_prod", "timetable"):
        _invalidate_timetable_caches()
    else:
        clear_cache(schema, table)

    created_row = get_created_row(config, converted)
    if not created_row:
        return {"success": False, "error": f"Row created in '{table}' but could not be found again."}
    return {"success": True, "data": created_row}


###########################################################################
############################# MCP TOOLS: READ #############################
###########################################################################

@tool(min_scope=2)
def get_teams() -> list:
    """Get all teams."""
    return fetch_cached("ts_prod", "teams")


@tool(min_scope=2)
def get_user_roles() -> list:
    """Get all user roles."""
    return fetch_cached("ts_prod", "user_roles")

@tool(min_scope=3)
def get_users(name: str = "") -> list:
    """
    Get all users, optionally filtered by (part of) the name.
    :param name: part of the user's name to filter on, empty = all users
    """
    rows = fetch_cached("ts_prod", "users")
    if not name:
        return rows
    q = name.strip().lower()
    return [r for r in rows if q in (r.get("name") or "").lower()]
    
@tool(min_scope=2)
def get_clients(name: str = "") -> list:
    """
    Get clients, optionally filtered by (part of) the name.
    :param name: part of the client name to filter on, empty = all clients
    """
    rows = fetch_cached("ts_prod", "clients")
    if not name:
        return rows
    q = name.strip().lower()
    return [r for r in rows if q in (r.get("name") or "").lower()]

@tool()
def get_available_projects(client_id: int = 0, status: str = "") -> list:
    """
    Get projects with client context included in one call. Needed to find
    a valid project_id for create_task, including for a project that
    doesn't have any tasks yet.
    :param client_id: filter by client, 0 = all clients
    :param status: filter by project status (planned, active, on_hold, done), empty = all statuses
    """
    clients = {c["id"]: c for c in fetch_cached("ts_prod", "clients")}

    rows = []
    for project in fetch_cached("ts_prod", "projects"):
        client = clients.get(project.get("client_id"), {})

        if client_id and project.get("client_id") != client_id:
            continue
        if status and project.get("status") != status:
            continue
        if CURRENT_USER.get("scope", 0) < 2 and not _user_authorized_for_client(client.get("id"), CURRENT_USER["user_id"]):
            continue

        rows.append({
            "project_id": project.get("id"),
            "project_name": project.get("name"),
            "project_status": project.get("status"),
            "project_start_date": project.get("start_date"),
            "project_end_date": project.get("end_date"),
            "client_id": project.get("client_id"),
            "client_name": client.get("name"),
            "client_status": client.get("status"),
        })
    return rows


@tool()
def get_available_tasks(client_id: int = 0, project_id: int = 0, status: str = "") -> list:
    """
    Get all tasks with project and client context included in one call.
    :param client_id: filter by client, 0 = all clients
    :param project_id: filter by project, 0 = all projects
    :param status: filter by task status (todo, in_progress, done, blocked), empty = all statuses
    """
    projects = {p["id"]: p for p in fetch_cached("ts_prod", "projects")}
    clients = {c["id"]: c for c in fetch_cached("ts_prod", "clients")}
    
    rows = []
    for task in fetch_cached("ts_prod", "tasks"):
        project = projects.get(task.get("project_id"), {})
        client = clients.get(project.get("client_id"), {})

        if project_id and task.get("project_id") != project_id:
            continue
        if client_id and project.get("client_id") != client_id:
            continue
        if status and task.get("status") != status:
            continue

        if CURRENT_USER.get("scope", 0) < 2 and not _user_authorized_for_client(client.get("id"), CURRENT_USER["user_id"]):
            continue

        rows.append({
            "task_id": task.get("id"),
            "task_name": task.get("name"),
            "task_status": task.get("status"),
            "task_description": task.get("description"),
            "billable": task.get("billable"),
            "project_id": task.get("project_id"),
            "project_name": project.get("name"),
            "project_status": project.get("status"),
            "client_id": project.get("client_id"),
            "client_name": client.get("name"),
            "client_status": client.get("status"),
        })
    return rows


@tool()
def get_date_time_entries(date_str: str = "") -> list:
    """
    Get my own time entries, with task/project/client context already
    included, filtered to a single date. Defaults to today when no
    date is given.
    :param date_str: date DD-MM-YYYY, empty = today
    """
    rows = _my_rows()
 
    if date_str:
        parsed_date = validate_date(date_str)
        if parsed_date is None:
            raise ValueError(f"Invalid date: {date_str!r} (expected DD-MM-YYYY)")
    else:
        parsed_date = date.today()
 
    return [r for r in rows if _parse_entry_date(r) == parsed_date]
 
 
@tool()
def get_my_time_entries(from_date: str = "", to_date: str = "") -> list:
    """
    Get my own time entries, with task/project/client context already
    included, filtered by period. Defaults to the last 7 days
    (today - 7 days through today) when no interval is given at all.
    :param from_date: from date DD-MM-YYYY, empty = today - 7 days (if to_date is also empty)
    :param to_date: to date DD-MM-YYYY, empty = today (if from_date is also empty)
    """
    rows = _my_rows()

    if not from_date and not to_date:
        parsed_from = date.today() - timedelta(days=7)
        parsed_to = date.today()
    else:
        parsed_from = None
        if from_date:
            parsed_from = validate_date(from_date)
            if parsed_from is None:
                raise ValueError(f"Invalid date: {from_date!r} (expected DD-MM-YYYY)")

        parsed_to = None
        if to_date:
            parsed_to = validate_date(to_date)
            if parsed_to is None:
                raise ValueError(f"Invalid date: {to_date!r} (expected DD-MM-YYYY)")

    def in_range(row: dict) -> bool:
        d = _parse_entry_date(row)
        if d is None:
            return False
        if parsed_from and d < parsed_from:
            return False
        if parsed_to and d > parsed_to:
            return False
        return True

    return [r for r in rows if in_range(r)]

###########################################################################
########################## MCP TOOLS: CREATE ##############################
###########################################################################

@tool(min_scope=RESOURCE_CONFIG["timetable"]["min_scope_write"])
def log_time_entry(
    task_id: int,
    date: str,
    duration: int,
    internal_description: str,
    external_description: str,
) -> dict:
    """
    Log a single time entry for the logged-in user.

    Nothing can be logged into a week the user has already submitted or
    that has been validated - that week is locked until it is unsubmitted.
    :param task_id: id of the task the time is logged against
    :param date: date and time in format DD-MM-YYYY HH:MM
    :param duration: duration in minutes
    :param internal_description: internal description of the work
    :param external_description: client-facing description of the work
    """
    return create_row("timetable", {
        "task_id": task_id,
        "date": date,
        "duration": duration,
        "internal_description": internal_description,
        "external_description": external_description,
    })


@tool(min_scope=RESOURCE_CONFIG["timetable"]["min_scope_write"])
def log_time_entries(entries_json: str) -> dict:
    """
    Log multiple time entries at once for the logged-in user, in a single
    API call instead of one log_time_entry call per line. Use this tool
    only for 2 or more entries at once. Entries falling in a submitted or
    validated week are refused individually, like any other invalid entry -
    the rest of the batch still goes through.
    :param entries_json: JSON array of objects, each with task_id, date
        (DD-MM-YYYY HH:MM), duration, internal_description,
        external_description
    """
    try:
        entries = json.loads(entries_json)
    except json.JSONDecodeError:
        return {"success": False, "error": "entries_json is not valid JSON."}

    if not isinstance(entries, list) or not entries:
        return {"success": False, "error": "entries_json must be a non-empty JSON array."}

    results = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            results.append({"index": index, "success": False, "error": "Entry is not a JSON object."})
            continue
        result = create_row("timetable", entry)
        results.append({"index": index, **result})

    success_count = sum(1 for r in results if r.get("success"))
    return {
        "success": success_count == len(results),
        "created": success_count,
        "failed": len(results) - success_count,
        "results": results,
    }


@tool(min_scope=RESOURCE_CONFIG["timetable"]["min_scope_write"])
def update_time_entry(
    entry_id: int,
    task_id: int = 0,
    date: str = "",
    duration: int = 0,
    internal_description: str = "",
    external_description: str = "",
) -> dict:
    """
    Update a time entry. Scope 1 can only update their own entries; scope
    2 and 3 can update any entry (same ownership rule as delete_entry).
    Only fields you provide are changed; entries that are already
    approved can no longer be edited, regardless of scope. The same goes
    for any entry in a submitted or validated week, and a new date may not
    move an entry into such a week - unsubmit it first.
    :param entry_id: id of the timetable row to update
    :param task_id: new task_id, 0 = leave unchanged
    :param date: new date and time in format DD-MM-YYYY HH:MM, empty = leave unchanged
    :param duration: new duration in minutes, 0 = leave unchanged
    :param internal_description: new internal description, empty = leave unchanged
    :param external_description: new external description, empty = leave unchanged
    """
    if not validate_positive_int(entry_id):
        return {"success": False, "error": "entry_id must be a positive integer."}
    entry_id = int(entry_id)

    entry = find_by_id(fetch_cached("ts_prod", "timetable"), entry_id)
    if not entry:
        return {"success": False, "error": "No entry found with this id."}
    if CURRENT_USER.get("scope", 0) < 2 and str(entry.get("user_id")) != str(CURRENT_USER.get("user_id")):
        return {"success": False, "error": "You can only update your own entries."}
    if entry.get("approved"):
        return {"success": False, "error": "This entry is already approved and can no longer be updated."}
    lock_error = _week_lock_error(entry.get("user_id"), entry.get("date"),
                                 "this entry can no longer be changed")
    if lock_error:
        return {"success": False, "error": lock_error}

    config = RESOURCE_CONFIG["timetable"]
    raw_updates = {}
    if task_id:
        raw_updates["task_id"] = task_id
    if date:
        raw_updates["date"] = date
    if duration:
        raw_updates["duration"] = duration
    if internal_description:
        raw_updates["internal_description"] = internal_description
    if external_description:
        raw_updates["external_description"] = external_description

    if not raw_updates:
        return {"success": False, "error": "Provide at least one field to update."}

    converted = {}
    for field, value in raw_updates.items():
        value, error = validate_and_convert_field(config, field, value)
        if error:
            return {"success": False, "error": error}
        converted[field] = value

    # Moving an entry is a write to BOTH weeks, so the target must be open too.
    if "date" in converted:
        lock_error = _week_lock_error(entry.get("user_id"), converted["date"],
                                     "an entry cannot be moved into it")
        if lock_error:
            return {"success": False, "error": lock_error}

    check_task_id = converted.get("task_id", entry.get("task_id"))
    access_error = _check_entry_client_access({"task_id": check_task_id})
    if access_error:
        return {"success": False, "error": access_error}

    dbconn.update(DW_NAME, "ts_prod", "timetable", entry_id, converted)
    _invalidate_timetable_caches()

    updated_entry = find_by_id(fetch_cached("ts_prod", "timetable"), entry_id)
    if not updated_entry:
        return {"success": False, "error": "Entry updated but could not be found again."}
    return {"success": True, "data": updated_entry}


@tool()
def delete_entry(entry_id: int) -> dict:
    """
    Delete a time entry. Scope 1 can only delete their own entries; scope
    2 and 3 can delete any entry. An entry that is already approved
    cannot be deleted by anyone, regardless of scope - same rule as
    update_time_entry. Nor can an entry in a submitted or validated week,
    which is locked until that week is unsubmitted.
    :param entry_id: id of the timetable row to delete
    """
    if not validate_positive_int(entry_id):
        return {"success": False, "error": "entry_id must be a positive integer."}
    entry_id = int(entry_id)

    entry = find_by_id(fetch_cached("ts_prod", "timetable"), entry_id)
    if not entry:
        return {"success": False, "error": "No entry found with this id."}

    if CURRENT_USER.get("scope", 0) < 2 and str(entry.get("user_id")) != str(CURRENT_USER.get("user_id")):
        return {"success": False, "error": "You can only delete your own entries."}

    if entry.get("approved"):
        return {"success": False, "error": "This entry is already approved and can no longer be deleted."}

    lock_error = _week_lock_error(entry.get("user_id"), entry.get("date"),
                                 "this entry can no longer be deleted")
    if lock_error:
        return {"success": False, "error": lock_error}

    dbconn.execute(DW_NAME, query=f"DELETE FROM ts_prod.timetable WHERE id = {entry_id}")
    _invalidate_timetable_caches()

    return {"success": True, "data": {"entry_id": entry_id}}


@tool(min_scope=RESOURCE_CONFIG["clients"]["min_scope_write"])
def create_client(name: str, peliqan_account_id: str, status: str = "prospect") -> dict:
    """
    Create a new client.
    :param name: name of the client
    :param peliqan_account_id: Peliqan account id
    :param status: prospect, active, or churned
    """
    return create_row("clients", {
        "name": name, "peliqan_account_id": peliqan_account_id, "status": status,
    })


@tool(min_scope=RESOURCE_CONFIG["clients"]["min_scope_write"])
def add_user_to_client(client_id: int, user_id: int) -> dict:
    """
    Give a user access to log time against a client's tasks.
    :param client_id: id of the client
    :param user_id: id of the user (ts_prod.users.id) to grant access to
    """
    if not validate_foreign_key("clients", client_id):
        return {"success": False, "error": f"No client exists with id {client_id}."}
    if not validate_positive_int(user_id) or not find_by_id(fetch_cached("ts_prod", "users"), int(user_id)):
        return {"success": False, "error": f"No user exists with id {user_id}."}

    client_id, user_id = int(client_id), int(user_id)
    if user_id in _fetch_client_user_links().get(client_id, set()):
        return {"success": False, "error": "This user already has access to this client."}

    dbconn.execute(DW_NAME, query=(
        f"INSERT INTO _pq_metadata._pq_rl_1339ee9e (source_table_id, target_table_id) "
        f"VALUES ({client_id}, {user_id})"
    ))
    clear_cache("ts_prod", "_client_user_links")
    return {"success": True, "data": {"client_id": client_id, "user_id": user_id}}


@tool(min_scope=RESOURCE_CONFIG["clients"]["min_scope_write"])
def remove_user_from_client(client_id: int, user_id: int) -> dict:
    """
    Remove a user's access to log time against a client's tasks.
    :param client_id: id of the client
    :param user_id: id of the user (ts_prod.users.id) to remove access from
    """
    if not validate_positive_int(client_id) or not validate_positive_int(user_id):
        return {"success": False, "error": "client_id and user_id must be positive integers."}

    client_id, user_id = int(client_id), int(user_id)
    dbconn.execute(DW_NAME, query=(
        f"DELETE FROM _pq_metadata._pq_rl_1339ee9e "
        f"WHERE source_table_id = {client_id} AND target_table_id = {user_id}"
    ))
    clear_cache("ts_prod", "_client_user_links")
    return {"success": True, "data": {"client_id": client_id, "user_id": user_id}}


@tool(min_scope=RESOURCE_CONFIG["projects"]["min_scope_write"])
def create_project(name: str, client_id: int, status: str, start_date: str, end_date: str) -> dict:
    """
    Create a new project.
    :param name: name of the project
    :param client_id: id of the client this project belongs to
    :param status: planned, active, on_hold, or done
    :param start_date: start date in format DD-MM-YYYY
    :param end_date: end date in format DD-MM-YYYY
    """
    return create_row("projects", {
        "name": name, "client_id": client_id, "status": status,
        "start_date": start_date, "end_date": end_date,
    })


@tool(min_scope=RESOURCE_CONFIG["tasks"]["min_scope_write"])
def create_task(name: str, project_id: int, status: str, billable: bool = False, description: str = "") -> dict:
    """
    Create a new task within a project. Scope 1 is restricted to projects
    under clients they're explicitly listed for.
    :param name: name of the task
    :param project_id: id of the project this task belongs to
    :param status: todo, in_progress, done, or blocked
    :param billable: whether this task is billable
    :param description: optional description of the task
    """
    data = {"name": name, "project_id": project_id, "status": status, "billable": billable}
    if description:
        data["description"] = description
    return create_row("tasks", data)


@tool(min_scope=RESOURCE_CONFIG["tasks"]["min_scope_write"])
def create_tasks(tasks_json: str) -> dict:
    """
    Create multiple tasks at once, in a single API call instead of one
    create_task call per task. Use this tool only for 2 or more tasks at
    once. For a single task, create_task is faster and less error-prone.
    :param tasks_json: JSON array of objects, each with name, project_id,
        status, and optionally billable, description
    """
    try:
        tasks = json.loads(tasks_json)
    except json.JSONDecodeError:
        return {"success": False, "error": "tasks_json is not valid JSON."}

    if not isinstance(tasks, list) or not tasks:
        return {"success": False, "error": "tasks_json must be a non-empty JSON array."}

    results = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            results.append({"index": index, "success": False, "error": "Task is not a JSON object."})
            continue
        task = dict(task)
        task.setdefault("billable", False)  # same default as create_task itself
        result = create_row("tasks", task)
        results.append({"index": index, **result})

    success_count = sum(1 for r in results if r.get("success"))
    return {
        "success": success_count == len(results),
        "created": success_count,
        "failed": len(results) - success_count,
        "results": results,
    }


@tool(min_scope=RESOURCE_CONFIG["teams"]["min_scope_write"])
def create_team(name: str) -> dict:
    """
    Create a new team.
    :param name: name of the team
    """
    return create_row("teams", {"name": name})


@tool(min_scope=RESOURCE_CONFIG["user_roles"]["min_scope_write"])
def create_user_role(name: str) -> dict:
    """
    Create a new user role.
    :param name: name of the role
    """
    return create_row("user_roles", {"name": name})

###########################################################################
############################# MCP TOOLS: ADMIN #############################
###########################################################################

@tool(min_scope=2)
def approve_entry(entry_id: int) -> dict:
    """
    Approve a time entry: sets 'approved' to true and records who approved
    it (approved_by = user_id of the approving manager/admin).
    :param entry_id: id of the timetable row being approved
    """
    if not validate_positive_int(entry_id):
        return {"success": False, "error": "entry_id must be a positive integer."}
    entry_id = int(entry_id)

    entry = find_by_id(fetch_cached("ts_prod", "timetable"), entry_id)
    if entry is None:
        return {"success": False, "error": "No entry found with this id."}
    if entry.get("approved"):
        return {"success": False, "error": "This entry is already approved."}

    dbconn.update(DW_NAME, "ts_prod", "timetable", entry_id, {
        "approved": True,
        "approved_by": CURRENT_USER["user_id"],
    })
    _invalidate_timetable_caches()

    updated_entry = find_by_id(fetch_cached("ts_prod", "timetable"), entry_id)
    if not updated_entry:
        return {"success": False, "error": "Entry updated but could not be found again."}
    return {"success": True, "data": updated_entry}


_BLOCKED_SQL_KEYWORDS = re.compile(r"\b(insert|update|delete|drop|alter|grant|truncate|create)\b", re.IGNORECASE)


@tool(min_scope=2)
def run_report_query(select_query: str) -> List[Dict[str, Any]]:
    """
    Runs a read-only SELECT query on the ts_prod schema, for reporting.
    Managers and admins (scope 2+).
    :param select_query: a SELECT query on ts_prod tables, without a
        schema prefix (e.g. 'SELECT * FROM timetable WHERE approved = false')
    """
    normalized = select_query.strip()
    if not normalized.lower().startswith("select"):
        return [{"error": "Only SELECT queries are allowed."}]
    if ";" in normalized.rstrip(";"):
        return [{"error": "Multiple statements are not allowed."}]
    if _BLOCKED_SQL_KEYWORDS.search(normalized):
        return [{"error": "Only read actions are allowed."}]

    rows = dbconn.fetch(DW_NAME, "ts_prod", query=normalized)
    return rows or []


###########################################################################
######################### MCP PROTOCOL (JSON-RPC) ########################
###########################################################################

def log_request(request):
    if not DEBUG:
        return
    print("request method:", request.get("method"))
    print("request path:", request.get("path"))
    # Deliberately not logging query_string / headers / body - that's
    # exactly where an access token or other secret would show up.


def log_response(response):
    if not DEBUG:
        return
    print("response:", json.dumps(response, default=str)[:500])


def mcp_response_initialize(request_id):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"callTool": True, "listTools": True, "tools": {"listChanged": False}},
            "serverInfo": {"name": "peliqan-mcp", "version": "3.0.0"},
        },
    }


def mcp_response_tools_list(request_id):
    """Only advertise tools the caller's scope is allowed to use."""
    user_scope = CURRENT_USER.get("scope", 0)
    visible_tools = [t for t in MCP_TOOLS if TOOL_PERMISSIONS.get(t["name"], 1) <= user_scope]
    return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": visible_tools}}


def mcp_response_tools_call(request_id, response_type):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": response_type, response_type: ""}], "isError": False},
    }


def handle_tool_call(mcp_req, request_id):
    tool_name = mcp_req["params"]["name"]
    args = dict(mcp_req["params"].get("arguments", {}))  # copy: about to pop tool_intent out
    tool_intent = args.pop("tool_intent", "")
    log_tool_intent(tool_name, tool_intent)

    required_scope = TOOL_PERMISSIONS.get(tool_name, 1)
    func = TOOL_FUNCTIONS.get(tool_name)

    is_error = False
    if func is None:
        tool_response = f"Unknown tool: '{tool_name}'."
        is_error = True
    elif not require_scope(required_scope):
        tool_response = (
            f"Unauthorized: tool '{tool_name}' requires scope {required_scope}, "
            f"your key has scope {CURRENT_USER.get('scope', 0)}."
        )
        is_error = True
    else:
        try:
            tool_response = func(**args)
        except Exception as e:
            tool_response = str(e)
            is_error = True

    response_type = "text"
    response = mcp_response_tools_call(request_id, response_type)
    if is_error:
        response["result"]["isError"] = True

    response_format = get_tool_response_format(tool_name) if func else "str"
    if response_format == "str":
        response["result"]["content"][0][response_type] = tool_response
    else:
        response["result"]["content"][0][response_type] = json.dumps(tool_response)
    return response


def handler(request):
    global CURRENT_USER

    if "/.well-known/oauth-protected-resource" in request['url']:
        return mcp_response_oauth_protected_resource()

    elif "/.well-known/openid-configuration" in request['url']:
        return mcp_response_authorization_server_openid_config()

    user = authenticate_user(request)
    if not user:
        metadata_url = f"https://api.eu.peliqan.io{WELLKNOWN_PROTECTED_RESOURCE_PATH}"
        return "Unauthorized", 401, {
            "WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}"'
        }

    CURRENT_USER = user

    log_request(request)

    try:
        mcp_req = json.loads(request.get("data") or "{}")
    except json.JSONDecodeError:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}, 400

    request_id = mcp_req.get("id", 0)
    method = mcp_req.get("method")

    if method == "initialize":
        response = mcp_response_initialize(request_id)
    elif method == "notifications/initialized":
        response = ""
    elif method == "tools/list":
        response = mcp_response_tools_list(request_id)
    elif method == "tools/call":
        response = handle_tool_call(mcp_req, request_id)
    else:
        response = mcp_response_initialize(request_id)

    log_response(response)
    return response
