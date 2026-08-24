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
ts_weekly_calendar (v3.0)

Weekly calendar for logged timesheet entries, now editable.

Data sources:
  - ts_reporting.fact_timetable  : read-only, joined view used for display
  - ts_prod.timetable            : the real table - insert/update/delete happen here
  - ts_prod.users / tasks / projects / user_roles : lookups for forms + role checks
  - ts_prod.timetable_submissions: NEW table (created on first run if missing)
                                    tracks the weekly submit -> confirm workflow.
                                    Deliberately separate from ts_prod.timetable so
                                    nothing here touches the table the Lucas
                                    Timetracking MCP server owns.
  - _pq_metadata._pq_rl_1339ee9e : the internal junction table backing
                                    ts_prod.clients.user_list (a many-to-many
                                    that never comes through a normal
                                    dbconn.fetch/update). Same table and same
                                    raw-SQL-read pattern as ts_mcp_server's
                                    own _fetch_client_user_links. If this
                                    silently returns nothing, the user_list
                                    field was likely deleted and recreated -
                                    re-check its current relation name.

Identity: there's no real login yet (deliberately deferred - see chat). The
"I am" selector below is a soft, self-declared identity.

Permissions follow ts_mcp_server's documented scope model exactly - role
(ts_prod.user_roles) has NO effect on permissions here, only
ts_prod.users.scope (1/2/3, cumulative):
  - Scope 1 (employee): can only see and edit their OWN entries, and only
    their own calendar at all - there's no "Viewing" of anyone else, mirroring
    that get_my_time_entries is the only read tool available at this level.
    On top of that (section 4.1, scope 1 only): can only add/edit entries
    for tasks whose project's client they're explicitly listed for via
    clients.user_list - mirrors get_available_tasks/_check_entry_client_access.
    This re-checks on every edit, even unrelated fields, same as
    update_time_entry - losing access to a client blocks editing entries
    already logged against it, not just re-assigning to a new one.
  - Scope 2 (manager) / Scope 3 (admin): can view anyone, add/edit/delete
    ANY entry regardless of ownership or client access (section 4.1 is
    skipped entirely from scope 2 up), and can approve a submitted week
    (approve_entry is scope >= 2).
  - Universal, regardless of scope: once an entry is approved (approved=true),
    nobody can edit or delete it through this app - matches the documented
    restriction on update_time_entry/delete_entry (section 4.2).
  - Our own weekly submit/approve workflow (timetable_submissions) is an
    addition on top of ts_mcp_server, not part of it: submitting a week
    locks the owner's own further edits on it (self-service undo via
    "Un-submit" while still just submitted, not yet approved). Scope 2/3
    can bypass this lock the same way they bypass ownership - but never the
    approved-entry lock above, which is absolute.
  - Approval is a true terminal state: once a week is approved, NO new
    entries can be added to it either, even by managers - otherwise the
    approved total would silently drift with no re-approval step. Managers
    CAN still add to a submitted-but-not-yet-approved week (fixing a gap
    before approving is the intended workflow); they cannot once approved.

Layout: .block-container is pinned to a min-width that scales with the
number of days in DAY_NAMES, so the day grid never gets squeezed - narrower
browser windows get a horizontal scrollbar for the whole page instead of
shrinking columns. Timeline has an hour line/label every hour, plus a
1-hour buffer at the bottom so the last hour line has room instead of
sitting exactly on the box's edge. Timeline block labels "auto-fill":
character budget scales with block height. Expander/dialog details show
full task/project names and rely on the wrap CSS override instead of
pre-truncating.

Viewing: there's no "All employees" mode - Viewing is always one specific
person, defaulting to current_user_id on first load (seeded into
st.session_state before the widget is created, since a plain index= only
applies on a widget's very first-ever render).

Section ordering, both directions matter here, per two real Streamlit
constraints hit in practice (not style choices):
  1. A widget's on_change= callback must already be DEFINED (as a name in
     scope) before that widget is instantiated - hence go_to_week/
     handle_jump_date_change are defined before "View filter".
  2. st.session_state[key] can't be WRITTEN after that widget has already
     been instantiated in the SAME script run - hence the nav buttons
     (which write session_state.jump_date via go_to_week) must execute
     BEFORE "View filter" creates the jump_date widget, not after. This is
     why the buttons+label block sits above View filter, not below it.
"""

import streamlit as st
import pandas as pd
from datetime import date, datetime, time, timedelta

# =====================================================
# Config
# =====================================================

DW_NAME = "dw_3202"

REPORTING_SCHEMA = "ts_reporting"
ENTRIES_TABLE = "fact_timetable"

PROD_SCHEMA = "ts_prod"
TIMETABLE_TABLE = "timetable"
USERS_TABLE = "users"
TASKS_TABLE = "tasks"
PROJECTS_TABLE = "projects"
ROLES_TABLE = "user_roles"
SUBMISSIONS_TABLE = "timetable_submissions"

MANAGE_SCOPE_THRESHOLD = 2  # scope >= 2 = manager-level (approve_entry, edit/delete anyone's entries)
SCOPE_LABELS = {1: "employee", 2: "manager", 3: "admin"}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"] #, "Saturday", "Sunday"]
UNKNOWN_USER_LABEL = "Unknown user"
TIMELINE_HEIGHT_PX = 600
MIN_COLUMN_WIDTH_PX = 200

st.set_page_config(page_title="Weekly Timesheet Calendar", layout="wide")

# =====================================================
# Data loading (cached)
# =====================================================

@st.cache_data(ttl=60)
def load_entries():
    dbconn = pq.dbconnect(DW_NAME)
    rows = dbconn.fetch(DW_NAME, REPORTING_SCHEMA, ENTRIES_TABLE) or []
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def load_users():
    dbconn = pq.dbconnect(DW_NAME)
    return dbconn.fetch(DW_NAME, PROD_SCHEMA, USERS_TABLE) or []


@st.cache_data(ttl=60)
def load_tasks():
    dbconn = pq.dbconnect(DW_NAME)
    return dbconn.fetch(DW_NAME, PROD_SCHEMA, TASKS_TABLE) or []


@st.cache_data(ttl=60)
def load_projects():
    dbconn = pq.dbconnect(DW_NAME)
    return dbconn.fetch(DW_NAME, PROD_SCHEMA, PROJECTS_TABLE) or []


@st.cache_data(ttl=60)
def load_roles():
    dbconn = pq.dbconnect(DW_NAME)
    return dbconn.fetch(DW_NAME, PROD_SCHEMA, ROLES_TABLE) or []


@st.cache_data(ttl=60)
def load_timetable_task_map():
    # fact_timetable exposes task_name but not task_id - needed so the edit
    # form can pre-select the entry's current task. Pulled separately here
    # rather than added to the shared reporting query table.
    dbconn = pq.dbconnect(DW_NAME)
    rows = dbconn.fetch(DW_NAME, PROD_SCHEMA, TIMETABLE_TABLE) or []
    return {
        to_int_or_none(r.get("id")): to_int_or_none(r.get("task_id"))
        for r in rows
    }


@st.cache_data(ttl=60)
def load_timetable_approval_map():
    # fact_timetable is a materialized query table, not a live view - it
    # doesn't reflect confirm_week()'s writes to ts_prod.timetable.approved
    # until someone re-runs its underlying query. Pulled live here instead,
    # same reasoning as load_timetable_task_map above.
    dbconn = pq.dbconnect(DW_NAME)
    rows = dbconn.fetch(DW_NAME, PROD_SCHEMA, TIMETABLE_TABLE) or []
    return {
        to_int_or_none(r.get("id")): {
            "approved": bool(r.get("approved")),
            "approved_by": to_int_or_none(r.get("approved_by")),
        }
        for r in rows
    }


@st.cache_data(ttl=60)
def load_client_user_links():
    # ts_prod.clients.user_list is a many-to-many field that never comes
    # through a normal dbconn.fetch/update, per ts_mcp_server's own
    # comments (confirmed empirically there). The only way to read it is
    # raw SQL against Peliqan's internal junction table for this specific
    # relation - same table ts_mcp_server's _fetch_client_user_links uses.
    # NOTE (from that script): this internal table name changes if the
    # user_list field is ever deleted and recreated - if this silently
    # starts returning nothing, re-check the relation's current name.
    dbconn = pq.dbconnect(DW_NAME)
    rows = dbconn.fetch(DW_NAME, query=(
        "SELECT source_table_id, target_table_id "
        "FROM _pq_metadata._pq_rl_1339ee9e"
    )) or []
    links = {}
    for row in rows:
        links.setdefault(row.get("source_table_id"), set()).add(row.get("target_table_id"))
    return links


@st.cache_data(ttl=60)
def load_submissions():
    dbconn = pq.dbconnect(DW_NAME)
    try:
        return dbconn.fetch(DW_NAME, PROD_SCHEMA, SUBMISSIONS_TABLE) or []
    except Exception:
        return []

def refresh_entries():
    load_entries.clear()
    load_timetable_task_map.clear()
    load_timetable_approval_map.clear()

def refresh_submissions():
    load_submissions.clear()

def refresh_all():
    load_entries.clear()
    load_users.clear()
    load_tasks.clear()
    load_projects.clear()
    load_roles.clear()
    load_submissions.clear()
    load_timetable_task_map.clear()
    load_timetable_approval_map.clear()


def ensure_submissions_table():
    dbconn = pq.dbconnect(DW_NAME)
    try:
        dbconn.fetch(DW_NAME, PROD_SCHEMA, SUBMISSIONS_TABLE)
    except Exception:
        fields = {
            "id": "text",
            "user_id": "integer",
            "week_start_date": "text",
            "status": "text",
            "submitted_at": "text",
            "confirmed_by": "integer",
            "confirmed_at": "text",
        }
        dbconn.create_table(
            db_name=DW_NAME,
            schema_name=PROD_SCHEMA,
            table_name=SUBMISSIONS_TABLE,
            fields=fields,
            pk="id",
        )
        load_submissions.clear()


# =====================================================
# Small helpers
# =====================================================

def format_duration(minutes):
    try:
        minutes = int(round(float(minutes)))
    except (TypeError, ValueError):
        return "-"
    if minutes <= 0:
        return "0m"
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def week_monday(any_day):
    return any_day - timedelta(days=any_day.weekday())


def to_int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def truncate(text, length):
    text = text or ""
    if len(text) <= length:
        return text
    cut = text[: max(length - 1, 1)]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "…"


# =====================================================
# Lookups: users, tasks, projects, roles
# =====================================================

def build_lookups():
    users = load_users()
    tasks = load_tasks()
    projects = load_projects()
    roles = load_roles()

    user_by_id = {to_int_or_none(u.get("id")): u for u in users if to_int_or_none(u.get("id")) is not None}
    project_by_id = {
        to_int_or_none(p.get("id")): p
        for p in projects
        if to_int_or_none(p.get("id")) is not None
    }
    role_name_by_id = {
        to_int_or_none(r.get("id")): (r.get("name") or "").lower()
        for r in roles
        if to_int_or_none(r.get("id")) is not None
    }

    tasks_with_labels = []
    for t in tasks:
        project = project_by_id.get(to_int_or_none(t.get("project_id")))
        project_name = project.get("name") if project else None
        client_id = project.get("client_id") if project else None
        label = t.get("name") or "Untitled task"
        if project_name:
            label = f"{label} ({project_name})"
        tasks_with_labels.append({**t, "label": label, "client_id": to_int_or_none(client_id)})

    return users, tasks_with_labels, user_by_id, role_name_by_id


def role_name_for_user(user_id, user_by_id, role_name_by_id):
    user = user_by_id.get(to_int_or_none(user_id))
    if not user:
        return "employee"
    role_id = to_int_or_none(user.get("role_id"))
    return role_name_by_id.get(role_id, "employee")


def user_scope(user_id, user_by_id):
    user = user_by_id.get(to_int_or_none(user_id))
    return user.get("scope") if user else None


def user_authorized_for_client(client_id, user_id, client_user_links):
    return to_int_or_none(user_id) in client_user_links.get(to_int_or_none(client_id), set())


def user_display_name(user):
    return user.get("name") or user.get("email") or f"user #{user.get('id')}"

def resolve_approved_by_name(entry_id, approval_map, user_by_id):
    info = approval_map.get(to_int_or_none(entry_id))
    if not info:
        return None
    approver = user_by_id.get(info.get("approved_by"))
    return user_display_name(approver) if approver else None


# =====================================================
# Weekly submission workflow
# =====================================================

def submission_key(user_id, week_start):
    return f"{user_id}_{week_start.isoformat()}"


def build_submissions_index(submissions):
    index = {}
    for s in submissions:
        uid = to_int_or_none(s.get("user_id"))
        wk = s.get("week_start_date")
        if uid is not None and wk:
            index[(uid, wk)] = s
    return index


def get_submission(user_id, week_start, submissions_index):
    return submissions_index.get((to_int_or_none(user_id), week_start.isoformat()))


def is_locked(user_id, week_start, submissions_index):
    sub = get_submission(user_id, week_start, submissions_index)
    return bool(sub and sub.get("status") in ("submitted", "confirmed"))


def is_approved_week(user_id, week_start, submissions_index):
    sub = get_submission(user_id, week_start, submissions_index)
    return bool(sub and sub.get("status") == "confirmed")


def submit_week(user_id, week_start):
    dbconn = pq.dbconnect(DW_NAME)
    key = submission_key(user_id, week_start)
    dbconn.upsert(
        DW_NAME, PROD_SCHEMA, SUBMISSIONS_TABLE, key,
        {
            "id": key,
            "user_id": int(user_id),
            "week_start_date": week_start.isoformat(),
            "status": "submitted",
            "submitted_at": datetime.utcnow().isoformat(),
            "confirmed_by": None,
            "confirmed_at": None,
        },
    )
    refresh_submissions()


def unsubmit_week(user_id, week_start):
    dbconn = pq.dbconnect(DW_NAME)
    key = submission_key(user_id, week_start)
    dbconn.execute(
        DW_NAME,
        query=f"DELETE FROM {PROD_SCHEMA}.{SUBMISSIONS_TABLE} WHERE id = '{key}'",
    )
    refresh_submissions()


def confirm_week(user_id, week_start, confirmed_by_id, existing_submission):
    dbconn = pq.dbconnect(DW_NAME)
    key = submission_key(user_id, week_start)
    week_end = week_start + timedelta(days=len(DAY_NAMES) - 1)

    # Keep the existing per-entry approved/approved_by fields on
    # ts_prod.timetable in sync with the weekly confirmation - otherwise
    # a "confirmed" week still shows every entry as "Pending approval"
    # elsewhere (timeline block color, entry detail view, fact_timetable).
    raw_rows = dbconn.fetch(DW_NAME, PROD_SCHEMA, TIMETABLE_TABLE) or []
    for row in raw_rows:
        if to_int_or_none(row.get("user_id")) != to_int_or_none(user_id):
            continue
        row_date = pd.to_datetime(row.get("date"), errors="coerce")
        if pd.isna(row_date) or not (week_start <= row_date.date() <= week_end):
            continue
        dbconn.update(
            DW_NAME, PROD_SCHEMA, TIMETABLE_TABLE, to_int_or_none(row.get("id")),
            {"approved": "true", "approved_by": int(confirmed_by_id)},
        )

    dbconn.upsert(
        DW_NAME, PROD_SCHEMA, SUBMISSIONS_TABLE, key,
        {
            "id": key,
            "user_id": int(user_id),
            "week_start_date": week_start.isoformat(),
            "status": "confirmed",
            "submitted_at": (existing_submission or {}).get("submitted_at"),
            "confirmed_by": int(confirmed_by_id),
            "confirmed_at": datetime.utcnow().isoformat(),
        },
    )
    refresh_entries()
    refresh_submissions()


# =====================================================
# Writes to ts_prod.timetable
# =====================================================

def insert_entry(user_id, task_id, entry_date, duration_minutes, internal_description, external_description):
    dbconn = pq.dbconnect(DW_NAME)
    new_entry = {
        "user_id": int(user_id),
        "task_id": int(task_id),
        "date": entry_date.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": int(duration_minutes),
        "internal_description": (internal_description or "").strip(),
        "external_description": (external_description or "").strip(),
        "date_inserted": datetime.utcnow().strftime("%Y-%m-%d"),
        "approved": None,
        "approved_by": None,
    }
    dbconn.insert(DW_NAME, PROD_SCHEMA, TIMETABLE_TABLE, new_entry)
    refresh_entries()


def update_entry(entry_id, task_id, entry_date, duration_minutes, internal_description, external_description):
    dbconn = pq.dbconnect(DW_NAME)
    updated = {
        "task_id": int(task_id),
        "date": entry_date.strftime("%Y-%m-%d"),
        "duration": int(duration_minutes),
        "internal_description": (internal_description or "").strip(),
        "external_description": (external_description or "").strip(),
    }
    dbconn.update(DW_NAME, PROD_SCHEMA, TIMETABLE_TABLE, int(entry_id), updated)
    refresh_entries()


def delete_entry(entry_id):
    dbconn = pq.dbconnect(DW_NAME)
    dbconn.execute(
        DW_NAME,
        query=f"DELETE FROM {PROD_SCHEMA}.{TIMETABLE_TABLE} WHERE id = {int(entry_id)}",
    )
    refresh_entries()


# =====================================================
# Timeline visual (per day) - proportional HTML/CSS block
# =====================================================

def render_day_timeline(day_entries, axis_start_hour, axis_end_hour, is_today):
    # +1 hour of buffer so the last hour line/label has room below it
    # instead of landing exactly on the box's bottom edge (where it was
    # getting clipped/pushed outside the visible area).
    total_minutes = max((axis_end_hour - axis_start_hour + 1) * 60, 1)
    px_per_minute = TIMELINE_HEIGHT_PX / total_minutes

    border_color = "#e2e2e2"
    muted_text = "#8a8a8a"
    accent_bg = "#dbeafe"
    accent_text = "#1d4ed8"
    accent_border = "#93c5fd"
    success_bg = "#dcfce7"
    success_text = "#15803d"
    success_border = "#86efac"
    today_border = "#2563eb"
    warning_bg = "#fef3c7"
    warning_text = "#92400e"
    warning_border = "#fcd34d"

    hour_lines = []
    for h in range(axis_start_hour, axis_end_hour + 1):
        top = (h - axis_start_hour) * 60 * px_per_minute
        hour_lines.append(
            f'<div style="position:absolute; top:{top:.0f}px; left:0; right:0; '
            f'height:1px; background:{border_color};"></div>'
        )
        hour_lines.append(
            f'<div style="position:absolute; top:{top:.0f}px; left:4px; '
            f'font-size:10px; color:{muted_text};">{h:02d}:00</div>'
        )

    blocks = []
    for _, entry in day_entries.sort_values("entry_date").iterrows():
        start_dt = entry.get("entry_date")
        if pd.isna(start_dt):
            continue
        duration = float(entry.get("duration") or 0)
        start_minutes = (start_dt.hour - axis_start_hour) * 60 + start_dt.minute
        top = max(start_minutes, 0) * px_per_minute
        height = max(duration * px_per_minute, 5)
        status = entry.get("entry_status", "draft")
        if status == "approved":
            color_bg, color_text, color_border = success_bg, success_text, success_border
        elif status == "submitted":
            color_bg, color_text, color_border = warning_bg, warning_text, warning_border
        else:
            color_bg, color_text, color_border = accent_bg, accent_text, accent_border

        # "Auto-fill": estimate how many ~11px lines fit in this block's
        # height and give the label roughly that much text, so short
        # entries show a short label and taller entries show more of the
        # task name instead of a fixed one-line truncation every time.
        max_lines = max(1, int(height // 13))
        char_budget = min(34 * max_lines, 280)
        label = truncate(entry.get("task_name") or "Untitled", char_budget) if height >= 15 else ""

        blocks.append(
            f'<div style="position:absolute; top:{top:.0f}px; left:34px; right:4px; '
            f'height:{height:.0f}px; background:{color_bg}; color:{color_text}; '
            f'border:1px solid {color_border}; border-radius:4px; font-size:11px; '
            f'padding:2px 5px; overflow:hidden; white-space:normal; '
            f'word-break:break-word; line-height:1.25;">{label}</div>'
        )

    border = f"border:2px solid {today_border};" if is_today else f"border:1px solid {border_color};"
    html = (
        f'<div style="position:relative; height:{TIMELINE_HEIGHT_PX}px; {border} '
        f'border-radius:6px; background:#fafafa; margin-bottom:8px;">'
        + "".join(hour_lines) + "".join(blocks) +
        "</div>"
    )
    return html


# =====================================================
# Entry add / edit forms
# =====================================================

@st.dialog("Add entry")
def show_add_entry_dialog(day, tasks, current_user_id, form_key):
    with st.form(form_key, clear_on_submit=True):
        task_choice = st.selectbox(
            "Task",
            options=[t.get("id") for t in tasks],
            format_func=lambda tid: next((t["label"] for t in tasks if t.get("id") == tid), str(tid)),
            key=f"{form_key}_task",
        )
        start_time = st.time_input(
            "Start time", value=time(9, 0), step=timedelta(minutes=15), key=f"{form_key}_time"
        )
        duration = st.number_input(
            "Duration (minutes)", min_value=1, step=15, value=60, key=f"{form_key}_duration"
        )
        internal_description = st.text_area("Internal description", key=f"{form_key}_int")
        external_description = st.text_area("External description", key=f"{form_key}_ext")
        submitted = st.form_submit_button("Save")

        if submitted:
            if not tasks:
                st.error("No tasks available.")
            elif duration <= 0:
                st.error("Duration must be greater than 0.")
            else:
                entry_datetime = datetime.combine(day, start_time)
                insert_entry(
                    current_user_id, task_choice, entry_datetime, duration,
                    internal_description, external_description,
                )
                st.success(
                    f"Entry added for {day.strftime('%d %b')} at "
                    f"{start_time.strftime('%H:%M')} ({int(duration)} min)."
                )
                st.rerun()


def render_add_entry_form(day, tasks, current_user_id, form_key):
    if st.button("+ Add entry", key=f"open_add_{day.isoformat()}", use_container_width=True):
        show_add_entry_dialog(day, tasks, current_user_id, form_key)


def render_entry_edit_form(entry, tasks, form_key):
    current_task_id = to_int_or_none(entry.get("task_id"))
    task_ids = [t.get("id") for t in tasks]
    default_index = task_ids.index(current_task_id) if current_task_id in task_ids else 0

    with st.form(form_key):
        task_choice = st.selectbox(
            "Task", options=task_ids,
            format_func=lambda tid: next((t["label"] for t in tasks if t.get("id") == tid), str(tid)),
            index=default_index if task_ids else 0,
            key=f"{form_key}_task",
        )
        entry_date_val = entry.get("entry_date")
        default_date = entry_date_val.date() if pd.notna(entry_date_val) else date.today()
        new_date = st.date_input("Date", value=default_date, key=f"{form_key}_date")
        new_duration = st.number_input(
            "Duration (minutes)", min_value=1, step=15,
            value=int(entry.get("duration") or 1), key=f"{form_key}_duration",
        )
        new_internal = st.text_area(
            "Internal description", value=entry.get("internal_description") or "", key=f"{form_key}_int"
        )
        new_external = st.text_area(
            "External description", value=entry.get("external_description") or "",
            key=f"{form_key}_ext",
        )
        col_save, col_delete = st.columns(2)
        save_clicked = col_save.form_submit_button("Save changes", use_container_width=True)
        delete_clicked = col_delete.form_submit_button("Delete entry", use_container_width=True)

        if save_clicked:
            update_entry(entry.get("entry_id"), task_choice, new_date, new_duration, new_internal, new_external)
            st.success("Entry updated.")
            st.rerun()

        if delete_clicked:
            delete_entry(entry.get("entry_id"))
            st.success("Entry deleted.")
            st.rerun()


def render_entry_details_readonly(entry):
    task_status = (entry.get("task_status") or "-").replace("_", " ").title()
    project_status = (entry.get("project_status") or "-").replace("_", " ").title()

    st.markdown(f"**Task:** {entry.get('task_name') or '-'}  ·  {task_status}")
    st.markdown(f"**Project:** {entry.get('project_name') or '-'}  ·  {project_status}")
    if entry.get("project_end_date"):
        st.caption(f"Project ends {entry.get('project_end_date')}")
    st.markdown(f"**Client:** {entry.get('client_name') or '-'}")
    st.markdown(f"**Employee:** {entry.get('user_name') or '-'}")

    entry_dt = entry.get("entry_date")
    when = entry_dt.strftime("%A %d %B %Y, %H:%M") if pd.notna(entry_dt) else "-"
    st.markdown(f"**Date & time:** {when}")
    st.markdown(f"**Duration:** {format_duration(entry.get('duration'))}")
    st.markdown(f"**Billable:** {'Yes' if entry.get('billable') else 'No'}")

    if bool(entry.get("approved")):
        approval_line = "Approved"
        if entry.get("approved_by_name"):
            approval_line += f" by {entry.get('approved_by_name')}"
    else:
        approval_line = "Pending approval"
    st.markdown(f"**Approval:** {approval_line}")

    st.markdown("**Internal description**")
    st.caption(entry.get("internal_description") or "-")

    st.markdown("**External description**")
    st.caption(entry.get("external_description") or "-")

    st.caption(f"Entry ID: {entry.get('entry_id')}")

@st.dialog("Entry details")
def show_entry_dialog(entry, tasks, can_edit, owner_locked):
    render_entry_details_readonly(entry)
    if can_edit:
        st.markdown("---")
        render_entry_edit_form(entry, tasks, form_key=f"edit_{entry.get('entry_id')}")
    elif owner_locked:
        st.caption("Locked - this week has been submitted.")

# =====================================================
# Load everything
# =====================================================

ensure_submissions_table()

users, tasks, user_by_id, role_name_by_id = build_lookups()
submissions_index = build_submissions_index(load_submissions())

st.title("Weekly Timesheet Calendar")

st.markdown(
    f"""
    <style>
        .block-container {{
            min-width: {len(DAY_NAMES) * MIN_COLUMN_WIDTH_PX}px;
        }}
        [data-testid="stExpander"] summary,
        [data-testid="stExpander"] summary p {{
            white-space: normal !important;
            overflow-wrap: anywhere !important;
        }}
        [data-testid="stPopover"] button,
        [data-testid="stPopover"] button p {{
            white-space: normal !important;
            overflow-wrap: anywhere !important;
            height: auto !important;
        }}
        [data-testid="stButton"] button,
        [data-testid="stButton"] button p {{
            white-space: normal !important;
            overflow-wrap: anywhere !important;
            height: auto !important;
        }}
    </style>
    """,
    unsafe_allow_html=True,
)

if not users:
    st.warning("No users found - check ts_prod.users.")
    st.stop()


# =====================================================
# Identity ("I am") - soft self-declared, no real login yet
# =====================================================

identity_col, viewing_col = st.columns(2)

user_options = [u.get("id") for u in users]
with identity_col:
    current_user_id = st.selectbox(
        "I am",
        options=user_options,
        format_func=lambda uid: user_display_name(user_by_id.get(to_int_or_none(uid), {"id": uid})),
        key="current_identity",
    )

current_scope = user_scope(current_user_id, user_by_id)
can_manage = current_scope is not None and current_scope >= MANAGE_SCOPE_THRESHOLD
scope_label = SCOPE_LABELS.get(current_scope, "unknown")
st.caption(f"Scope: {current_scope} ({scope_label})" + (" (manager override enabled)" if can_manage else ""))

# Section 4.1 of the docs: this client-access restriction applies ONLY to
# scope 1, and is independent of (on top of) the cumulative scope rule
# above - from scope 2 onward it's skipped entirely, same as ts_mcp_server.
client_user_links = load_client_user_links()
task_client_by_id = {to_int_or_none(t.get("id")): t.get("client_id") for t in tasks}
if can_manage:
    visible_tasks = tasks
else:
    visible_tasks = [
        t for t in tasks
        if user_authorized_for_client(t.get("client_id"), current_user_id, client_user_links)
    ]


# =====================================================
# Week navigation - definitions/state, then buttons + label. This
# ENTIRE block must run before "View filter" below - see the two
# ordering constraints explained in the module docstring.
# =====================================================

if "week_start" not in st.session_state:
    st.session_state.week_start = week_monday(date.today())
if "jump_date" not in st.session_state:
    st.session_state.jump_date = st.session_state.week_start


def go_to_week(new_monday):
    st.session_state.week_start = new_monday
    st.session_state.jump_date = new_monday


def handle_jump_date_change():
    st.session_state.week_start = week_monday(st.session_state.jump_date)


nav_prev, nav_label, nav_next, nav_today = st.columns([1, 3, 1, 1])

with nav_prev:
    if st.button("Previous week", use_container_width=True):
        go_to_week(st.session_state.week_start - timedelta(days=7))

with nav_next:
    if st.button("Next week", use_container_width=True):
        go_to_week(st.session_state.week_start + timedelta(days=7))

with nav_today:
    if st.button("Today", use_container_width=True):
        go_to_week(week_monday(date.today()))

week_start = st.session_state.week_start
week_end = week_start + timedelta(days=len(DAY_NAMES) - 1)

with nav_label:
    if week_start.month == week_end.month:
        label = f"{week_start.strftime('%d')} - {week_end.strftime('%d %b %Y')}"
    else:
        label = f"{week_start.strftime('%d %b')} - {week_end.strftime('%d %b %Y')}"
    st.markdown(f"#### {label}")
    st.caption(f"Week {week_start.isocalendar()[1]}")


# =====================================================
# View filter (separate from identity)
# =====================================================

filter_col, jump_col, refresh_col = st.columns([2, 2, 1])

with filter_col:
    if can_manage:
        if "viewing_filter" not in st.session_state:
            st.session_state["viewing_filter"] = current_user_id
        viewing_options = [u.get("id") for u in users]
        viewing_choice = st.selectbox(
            "Viewing",
            options=viewing_options,
            format_func=lambda v: user_display_name(user_by_id.get(to_int_or_none(v), {"id": v})),
            key="viewing_filter",
        )
    else:
        # Scope 1 = employee level: only get_my_time_entries is available,
        # there's no tool to see anyone else's time. Lock the view to self.
        viewing_choice = current_user_id
        st.selectbox(
            "Viewing", options=[current_user_id],
            format_func=lambda v: user_display_name(user_by_id.get(to_int_or_none(v), {"id": v})),
            disabled=True, key="viewing_filter_locked",
        )

with jump_col:
    st.date_input("Jump to a date", key="jump_date", on_change=handle_jump_date_change)

with refresh_col:
    st.write("")
    if st.button("Refresh data", use_container_width=True):
        refresh_all()
        st.rerun()


# =====================================================
# Weekly submission status panel
# =====================================================

st.divider()

viewing_user_id = to_int_or_none(viewing_choice)

status_col, action_col = st.columns([3, 2])


def status_label(sub):
    if not sub:
        return "Draft"
    if sub.get("status") == "confirmed":
        confirmed_by_user = user_by_id.get(to_int_or_none(sub.get("confirmed_by")))
        name = user_display_name(confirmed_by_user) if confirmed_by_user else "a manager"
        return f"Approved by {name}"
    return "Submitted - waiting approval"


is_own_week = viewing_user_id == to_int_or_none(current_user_id)
sub = get_submission(viewing_user_id, week_start, submissions_index)
who = user_display_name(user_by_id.get(viewing_user_id, {"id": viewing_user_id}))
with status_col:
    label = "Your week" if is_own_week else f"{who}'s week"
    st.markdown(f"**{label}:** {status_label(sub)}")
with action_col:
    if is_own_week:
        if sub and sub.get("status") == "submitted":
            if st.button("Un-submit your week"):
                unsubmit_week(viewing_user_id, week_start)
                st.rerun()
            if can_manage:
                if st.button("Approve your week"):
                    confirm_week(viewing_user_id, week_start, current_user_id, sub)
                    st.rerun()
        elif not sub:
            if st.button("Submit your week"):
                submit_week(viewing_user_id, week_start)
                st.rerun()
    elif can_manage and sub and sub.get("status") == "submitted":
        if st.button(f"Approve {who}'s week"):
            confirm_week(viewing_user_id, week_start, current_user_id, sub)
            st.rerun()

st.divider()


# =====================================================
# Load + prep entries
# =====================================================

raw_df = load_entries()

if raw_df.empty:
    st.info("No time entries have been logged yet.")
    st.stop()

df = raw_df.copy()
df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
df["entry_day"] = df["entry_date"].dt.date
df["duration"] = pd.to_numeric(df["duration"], errors="coerce").fillna(0)
df["user_name"] = df["user_name"].fillna(UNKNOWN_USER_LABEL)
df["user_id"] = df["user_id"].apply(to_int_or_none)

task_id_map = load_timetable_task_map()
df["task_id"] = df["entry_id"].apply(lambda eid: task_id_map.get(to_int_or_none(eid)))

approval_map = load_timetable_approval_map()
df["approved"] = df["entry_id"].apply(
    lambda eid: approval_map.get(to_int_or_none(eid), {}).get("approved", False)
)
df["approved_by_name"] = df["entry_id"].apply(
    lambda eid: resolve_approved_by_name(eid, approval_map, user_by_id)
)

week_df = df[(df["entry_day"] >= week_start) & (df["entry_day"] <= week_end)]
week_df = week_df[week_df["user_id"] == viewing_user_id]
week_df = week_df.copy()
week_df["entry_status"] = week_df.apply(
    lambda r: "approved" if bool(r.get("approved"))
    else ("submitted" if is_locked(to_int_or_none(r.get("user_id")), week_start, submissions_index) else "draft"),
    axis=1,
)


# =====================================================
# Weekly grid: timeline + clickable list, per day
# =====================================================

if week_df.empty:
    axis_start_hour, axis_end_hour = 7, 19
else:
    min_hour = int(week_df["entry_date"].dt.hour.min())
    max_hour = int(week_df["entry_date"].dt.hour.max()) + 1
    axis_start_hour = min(7, min_hour)
    axis_end_hour = max(19, max_hour)

day_cols = st.columns(len(DAY_NAMES))

for i, col in enumerate(day_cols):
    day = week_start + timedelta(days=i)
    day_entries = week_df[week_df["entry_day"] == day]
    day_total = day_entries["duration"].sum()
    is_today = day == date.today()

    with col:
        weekday_label = f"**{DAY_NAMES[i][:3]}** - {day.strftime('%d %b')}"
        st.markdown(f":blue[{weekday_label}]" if is_today else weekday_label)
        st.caption(f"{format_duration(day_total)}" if day_total else "-")

        st.markdown(
            render_day_timeline(day_entries, axis_start_hour, axis_end_hour, is_today),
            unsafe_allow_html=True,
        )

        if day_entries.empty:
            st.caption(":gray[No entries]")
        else:
            for _, entry in day_entries.sort_values("entry_date").iterrows():
                status = entry.get("entry_status", "draft")
                entry_dt = entry.get("entry_date")
                time_label = entry_dt.strftime("%H:%M") if pd.notna(entry_dt) else ""

                summary = f"{time_label}  -  {entry.get('task_name') or 'Untitled task'}"
                if entry.get("project_name"):
                    summary += f" ({entry.get('project_name')})"
                summary += f"  -  {format_duration(entry.get('duration'))}"

                entry_owner_id = to_int_or_none(entry.get("user_id"))
                owner_locked = is_locked(entry_owner_id, week_start, submissions_index)
                entry_approved = bool(entry.get("approved"))
                entry_client_id = task_client_by_id.get(to_int_or_none(entry.get("task_id")))
                client_access_ok = can_manage or user_authorized_for_client(
                    entry_client_id, current_user_id, client_user_links
                )
                # Per ts_mcp_server docs (4.2): an already-approved entry can
                # never be edited/deleted through these actions, by anyone,
                # regardless of scope. Below that: scope 1 needs ownership
                # (and respects our own week-submission lock) AND current
                # client access for this entry's task - update_time_entry
                # re-checks that on every edit, even unrelated fields, so a
                # scope-1 user who's lost access to this client is fully
                # blocked here too, not just from re-assigning the task.
                # Scope 2/3 can touch any entry regardless of client access
                # (section 4.1 only restricts scope 1).
                if entry_approved:
                    can_edit = False
                elif can_manage:
                    can_edit = True
                else:
                    can_edit = (
                        entry_owner_id == to_int_or_none(current_user_id)
                        and not owner_locked
                        and client_access_ok
                    )

                if st.button(summary, key=f"open_entry_{entry.get('entry_id')}", use_container_width=True):
                    show_entry_dialog(entry, visible_tasks, can_edit, owner_locked)

        day_owner_id = to_int_or_none(current_user_id)
        day_owner_locked = is_locked(day_owner_id, week_start, submissions_index)
        day_owner_approved = is_approved_week(day_owner_id, week_start, submissions_index)
        if day_owner_approved:
            # Approval is meant to be a stable, signed-off total - adding
            # entries after the fact would silently change that total with
            # no re-approval step, for anyone including managers. Compare
            # to a submitted-but-not-yet-approved week just below: a
            # manager fixing something there before approving is exactly
            # the intended workflow; doing it after approval is not.
            st.caption("This week has been approved - no new entries can be added.")
        elif can_manage or not day_owner_locked:
            render_add_entry_form(day, visible_tasks, current_user_id, form_key=f"add_{day.isoformat()}")
        else:
            st.caption("Your week is locked for new entries.")
