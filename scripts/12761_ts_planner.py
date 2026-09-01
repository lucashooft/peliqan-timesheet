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
ts_planner (v2) - team resourcing SCHEDULE, standalone from actuals.

One row per employee, WINDOW_DAYS blocks per row - the last 4 workweeks
(Mon-Fri) by default, navigable in 4-week jumps - each block colored by
the project PLANNED for that employee that day. Click any block to
assign, change or clear its project.

Deliberately NOT derived from ts_prod.timetable (logged hours): this is
its own plan, stored in ts_prod.planned_assignments, so it can later be
compared against what was actually logged instead of just echoing it.
That comparison is a follow-up, not built here yet.

ts_prod.planned_assignments does not exist until the first assignment is
saved - save_assignment() calls dbconn.create_table() before every
upsert, so a brand new install shows an all-empty grid rather than an
error. See the SCHEDULE_FIELDS comment below for why: Peliqan's warehouse
runs on Baserow, and only create_table() registers a new table with
Baserow's own catalog - neither dbconn.write()/write_records nor a raw
CREATE TABLE via dbconn.execute() left anything queryable.

No login, no scope check - same as every Data App in this repo except
12011_ts_my_week and 11383_ts_mcp_server (see CLAUDE.md). Anyone who can
open the app can see and edit every employee's schedule.

Data:
  - ts_prod.users                read-only (employee list)
  - ts_prod.projects/clients     read-only lookups (names, legend)
  - ts_prod.planned_assignments  read + write (this app's own table)

NOTE: st.set_page_config must stay a literal string - the Peliqan runtime
lifts that call into a system prepend that runs before this script body.
"""

import plotly.graph_objects as go
import pandas as pd
import streamlit as st
from datetime import date, datetime, timedelta

st.set_page_config(page_title="Team planner", layout="wide")

st.markdown("<style>.block-container{padding-top:2.2rem;}</style>",
            unsafe_allow_html=True)

# =====================================================
# Config
# =====================================================

DW_NAME = "dw_3202"
S = "ts_prod"
SCHEDULE_TABLE = "planned_assignments"

WORKDAYS_PER_WEEK = 5
WEEKS_IN_WINDOW = 4
WINDOW_DAYS = WORKDAYS_PER_WEEK * WEEKS_IN_WINDOW   # 20

DAY_ABBREV = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ROW_PX = 34

# Cycled by project_id, the same way ts_my_week cycles CLIENT_COLORS by
# client_id - two employees planned on the same project always land on
# the same color without a shared color table to keep in sync.
PROJECT_COLORS = ["#4c78a8", "#54a24b", "#b279a2", "#f58518", "#e45756",
                  "#eeca3b", "#9d755d", "#72b7b2", "#ff9da6", "#1b9e77",
                  "#7570b3", "#bab0ac"]

EMPTY_CELL_COLOR = "#f5f6f8"
# Stand-in for a plan pointing at a project that no longer exists.
DEFAULT_PROJECT = {"project": "?", "client": "-", "color": "#9d9da6"}

# =====================================================
# Helpers
# =====================================================

def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def monday_of(d):
    return d - timedelta(days=d.weekday())


def user_display_name(user):
    return (user or {}).get("name") or (user or {}).get("email") or f"user #{(user or {}).get('id')}"


def workdays_window(start_monday):
    return [start_monday + timedelta(weeks=w, days=d)
            for w in range(WEEKS_IN_WINDOW) for d in range(WORKDAYS_PER_WEEK)]

# =====================================================
# Data loading (cached)
# =====================================================

@st.cache_data(ttl=300)
def load_users():
    dbconn = pq.dbconnect(DW_NAME)
    rows = dbconn.fetch(DW_NAME, S, "users") or []
    users = [r for r in rows if to_int(r.get("id")) is not None]
    return sorted(users, key=lambda u: str(user_display_name(u)).lower())


@st.cache_data(ttl=300)
def load_projects():
    """{project_id: {project, client, color}} - the choices in the assign
    dialog and the labels/colors on the grid."""
    dbconn = pq.dbconnect(DW_NAME)
    rows = dbconn.fetch(DW_NAME, query=f"""
        SELECT p.id AS project_id, p.name AS project,
               COALESCE(c.name, '-') AS client
        FROM {S}.projects p
        LEFT JOIN {S}.clients c ON c.id = p.client_id
    """) or []
    out = {}
    for r in rows:
        pid = to_int(r.get("project_id"))
        if pid is None:
            continue
        out[pid] = {
            "project": r.get("project") or f"Project {pid}",
            "client": r.get("client") or "-",
            "color": PROJECT_COLORS[pid % len(PROJECT_COLORS)],
        }
    return out


@st.cache_data(ttl=60)
def load_schedule(start_date, end_date):
    """{(user_id, date): {project_id, note}} for every planned assignment
    in [start_date, end_date]. planned_assignments does not exist until
    the first save_assignment() call, so a missing table reads as
    "nothing planned yet" rather than an error."""
    dbconn = pq.dbconnect(DW_NAME)
    try:
        rows = dbconn.fetch(DW_NAME, query=f"""
            SELECT user_id, date, project_id, note
            FROM {S}.{SCHEDULE_TABLE}
            WHERE date >= '{start_date.isoformat()}' AND date <= '{end_date.isoformat()}'
        """) or []
    except Exception:
        return {}
    out = {}
    for r in rows:
        uid = to_int(r.get("user_id"))
        try:
            d = date.fromisoformat(str(r.get("date"))[:10])
        except ValueError:
            continue
        if uid is None:
            continue
        out[(uid, d)] = {"project_id": to_int(r.get("project_id")),
                         "note": r.get("note") or ""}
    return out

# =====================================================
# Writes
# =====================================================

# Same shape as ts_my_week's timetable_submissions: a synthetic string id
# built from the natural key, upserted with dbconn.upsert.
#
# Peliqan's warehouse runs on Baserow under the hood (a real error message
# leaked its own stack trace: /baserow/backend/src/baserow/...), and a raw
# `CREATE TABLE ... ` through dbconn.execute() only reaches the underlying
# Postgres - Baserow's own catalog (which insert/update/upsert/fetch all
# resolve table names through) never learns the table exists, so it still
# 404s as ERROR_TABLE_DOES_NOT_EXIST. dbconn.write()/write_records didn't
# create anything queryable either. create_table() is Baserow's own table
# API and is what actually registers a table both places at once.
SCHEDULE_FIELDS = [
    {"name": "id", "type": "text"},
    {"name": "user_id", "type": "text"},
    {"name": "date", "type": "text"},
    {"name": "project_id", "type": "text"},
    {"name": "note", "type": "text"},
    {"name": "updated_at", "type": "text"},
]


def assignment_id(user_id, day):
    return f"{int(user_id)}_{day.isoformat()}"


def ensure_schedule_table():
    """Create ts_prod.planned_assignments once. Idempotent: a second
    create_table call against a table that already exists is expected to
    fail, and that specific failure is swallowed; anything else re-raises
    so it still reaches the Save button's error message."""
    dbconn = pq.dbconnect(DW_NAME)
    try:
        dbconn.create_table(DW_NAME, S, SCHEDULE_TABLE, fields=SCHEDULE_FIELDS, pk=["id"])
    except Exception as exc:
        msg = str(exc).lower()
        if "already exist" not in msg and "duplicate" not in msg:
            raise


def save_assignment(user_id, day, project_id, note):
    """Plan `user_id` onto `project_id` for `day`, or move an existing
    plan to a different project - the same call either way, upserted on
    the (user_id, date) pair's synthetic id."""
    ensure_schedule_table()
    dbconn = pq.dbconnect(DW_NAME)
    dbconn.upsert(DW_NAME, S, SCHEDULE_TABLE, assignment_id(user_id, day), {
        "user_id": int(user_id),
        "date": day.isoformat(),
        "project_id": int(project_id),
        "note": (note or "").strip(),
        "updated_at": datetime.utcnow().isoformat(),
    })
    load_schedule.clear()


def clear_assignment(user_id, day):
    dbconn = pq.dbconnect(DW_NAME)
    dbconn.execute(DW_NAME, query=(
        f"DELETE FROM {S}.{SCHEDULE_TABLE} WHERE id = '{assignment_id(user_id, day)}'"))
    load_schedule.clear()

# =====================================================
# Grid model (pure function -> unit-testable)
# =====================================================

def build_cells(user_ids, days, schedule, projects):
    """
    One record per (user, day) cell, in row/day order - a plotted project
    block where one is planned, an empty/clickable block otherwise.
    """
    cells = []
    for uid in user_ids:
        for day_i, d in enumerate(days):
            rec = schedule.get((uid, d))
            pid = rec.get("project_id") if rec else None
            if pid is not None:
                info = projects.get(pid, DEFAULT_PROJECT)
                color, text = info["color"], info["project"]
                hover = f"<b>{info['client']}</b> - {info['project']}"
                if rec.get("note"):
                    hover += f"<br><i>{rec['note']}</i>"
                hover += "<br>Click to change"
            else:
                color, text = EMPTY_CELL_COLOR, ""
                hover = "Click to plan a project"
            cells.append({"user_id": uid, "day_i": day_i, "project_id": pid,
                         "color": color, "text": text, "hover": hover})
    return cells

# =====================================================
# State
# =====================================================

DEFAULT_START = monday_of(date.today()) - timedelta(weeks=WEEKS_IN_WINDOW - 1)

if "planner_start" not in st.session_state:
    st.session_state.planner_start = DEFAULT_START
if "grid_nonce" not in st.session_state:
    st.session_state.grid_nonce = 0
if "dialog_token" not in st.session_state:
    st.session_state.dialog_token = 0
if "pending" not in st.session_state:
    st.session_state.pending = None

# =====================================================
# Dialog helpers
#
# DUPLICATED from ts_my_week.py's dialog_key/open_dialog (Data Apps
# cannot import each other - see CLAUDE.md). Keep the two in step.
# =====================================================

def dialog_key(name):
    return f"{name}_{st.session_state.dialog_token}"


def open_dialog():
    st.session_state.dialog_token += 1


@st.dialog("Plan a project")
def assign_dialog(user_id, day, employee_name, projects, current_project_id, current_note):
    st.caption(f"{employee_name} - {day.strftime('%A %d %B %Y')}")
    ids = sorted(projects, key=lambda i: (projects[i]["client"], projects[i]["project"]))
    if not ids:
        st.warning("There are no projects to plan against yet.")
        return
    index = ids.index(current_project_id) if current_project_id in ids else None
    project_id = st.selectbox(
        "Project", ids, index=index, key=dialog_key("plan_project"),
        placeholder="Choose a project...",
        format_func=lambda i: f"{projects[i]['client']} - {projects[i]['project']}")
    note = st.text_input("Note", value=current_note, key=dialog_key("plan_note"))

    c1, c2 = st.columns(2)
    if c1.button("Save", type="primary", width='stretch',
                 disabled=project_id is None, key="plan_save"):
        try:
            save_assignment(user_id, day, project_id, note)
        except Exception as exc:
            st.error(f"Could not save: {exc}")
        else:
            st.rerun()
    if current_project_id is not None and c2.button("Clear", width='stretch', key="plan_clear"):
        try:
            clear_assignment(user_id, day)
        except Exception as exc:
            st.error(f"Could not clear: {exc}")
        else:
            st.rerun()

# =====================================================
# Page
# =====================================================

st.title("Team planner")
st.caption("One row per employee, one block per workday - click a block to plan "
          "which project they're on. This is a standalone schedule, not a copy "
          "of logged hours.")

users = load_users()
if not users:
    st.warning("No users found - check ts_prod.users.")
    st.stop()
user_by_id = {to_int(u["id"]): u for u in users}

projects = load_projects()

# ---- navigation ----
nav_prev, nav_date, nav_next, nav_reset, search_col = st.columns(
    [1.1, 1.8, 1.1, 1.3, 2.7], vertical_alignment="bottom")

if nav_prev.button("◀ 4 weeks", width='stretch'):
    st.session_state.planner_start -= timedelta(weeks=WEEKS_IN_WINDOW)
    st.rerun()

picked = nav_date.date_input("Week of", value=st.session_state.planner_start,
                             key="planner_date_pick")
if monday_of(picked) != st.session_state.planner_start:
    st.session_state.planner_start = monday_of(picked)
    st.rerun()

if nav_next.button("4 weeks ▶", width='stretch'):
    st.session_state.planner_start += timedelta(weeks=WEEKS_IN_WINDOW)
    st.rerun()

if nav_reset.button("Current", width='stretch',
                    disabled=st.session_state.planner_start == DEFAULT_START):
    st.session_state.planner_start = DEFAULT_START
    st.rerun()

search = search_col.text_input("Filter employee", placeholder="Type a name...",
                               label_visibility="collapsed")

days = workdays_window(st.session_state.planner_start)
st.caption(f"{days[0].strftime('%d %b')} - {days[-1].strftime('%d %b %Y')}")

display_users = [u for u in users
                 if not search or search.lower() in str(user_display_name(u)).lower()]
if not display_users:
    st.info("No employee matches that filter.")
    st.stop()
user_ids = [to_int(u["id"]) for u in display_users]

schedule = load_schedule(days[0], days[-1])

# ---- open a dialog queued by a previous click ----
pending, st.session_state.pending = st.session_state.pending, None
if pending:
    puid, pday = pending
    puser = user_by_id.get(puid)
    if puser is not None and pday in days:
        rec = schedule.get((puid, pday)) or {}
        open_dialog()
        assign_dialog(puid, pday, user_display_name(puser), projects,
                     rec.get("project_id"), rec.get("note", ""))

# =====================================================
# Legend
# =====================================================

seen = {}
for (uid, d), rec in schedule.items():
    pid = rec.get("project_id")
    if uid in user_ids and pid is not None and d in days:
        info = projects.get(pid, DEFAULT_PROJECT)
        seen.setdefault((info["client"], info["project"]), info["color"])

if seen:
    chips = "".join(
        f"<span style='display:inline-flex;align-items:center;margin:0 0.8rem 0.4rem 0;'>"
        f"<span style='width:0.8rem;height:0.8rem;border-radius:2px;background:{color};"
        f"display:inline-block;margin-right:0.35rem;'></span>"
        f"<span style='font-size:0.82rem;color:#3c4043;'>{client} - {project}</span></span>"
        for (client, project), color in sorted(seen.items())
    )
    st.markdown(f"<div style='margin-bottom:0.4rem;'>{chips}</div>", unsafe_allow_html=True)
else:
    st.caption("Nothing planned in this window yet - click any block to start.")

# =====================================================
# The grid (Plotly, styled to match ts_my_week's calendar)
# =====================================================

n_rows = len(display_users)
row_of = {uid: i for i, uid in enumerate(user_ids)}
cells = build_cells(user_ids, days, schedule, projects)

fig = go.Figure()
fig.add_trace(go.Bar(
    x=[c["day_i"] for c in cells],
    width=0.94,
    base=[row_of[c["user_id"]] for c in cells],
    y=[1] * len(cells),
    marker=dict(color=[c["color"] for c in cells], line=dict(color="white", width=1)),
    text=[c["text"] for c in cells],
    textposition="inside",
    insidetextanchor="middle",
    textfont=dict(color="white", size=9,
                  family="Source Sans Pro, Helvetica Neue, sans-serif"),
    customdata=[[str(c["user_id"]), days[c["day_i"]].isoformat()] for c in cells],
    hovertext=[c["hover"] for c in cells],
    hovertemplate="%{hovertext}<extra></extra>",
    showlegend=False,
    name="cells",
))

for i, d in enumerate(days):
    if d == date.today():
        fig.add_vrect(x0=i - 0.5, x1=i + 0.5, fillcolor="#eaf1fb",
                     opacity=0.55, layer="below", line_width=0)
for w in range(1, WEEKS_IN_WINDOW):
    fig.add_vline(x=w * WORKDAYS_PER_WEEK - 0.5, line=dict(color="#dadce0", width=1, dash="dot"))

fig.update_layout(
    height=max(n_rows * ROW_PX, ROW_PX * 3) + 60,
    margin=dict(l=8, r=8, t=30, b=8),
    plot_bgcolor="white",
    paper_bgcolor="rgba(0,0,0,0)",
    barmode="overlay",
    barcornerradius=4,
    dragmode=False,
    clickmode="event+select",
    font=dict(family="Source Sans Pro, Helvetica Neue, sans-serif"),
    xaxis=dict(
        range=[-0.5, len(days) - 0.5],
        tickvals=list(range(len(days))),
        ticktext=[f"{DAY_ABBREV[d.weekday()]} {d.day}" for d in days],
        side="top", fixedrange=True, showgrid=False, zeroline=False,
        showline=False, ticks="",
        tickfont=dict(size=10, color="#6e6e78"),
    ),
    yaxis=dict(
        range=[n_rows, 0],
        tickvals=[i + 0.5 for i in range(n_rows)],
        ticktext=[user_display_name(u) for u in display_users],
        fixedrange=True, zeroline=False, showline=False, ticks="",
        gridcolor="#eeeeef", gridwidth=1, dtick=1,
        tickfont=dict(size=11, color="#3c4043"),
    ),
)

event = st.plotly_chart(
    fig, width='stretch', on_select="rerun", selection_mode="points",
    key=f"grid_{st.session_state.grid_nonce}", config={"displayModeBar": False},
)

# ---- handle a click: queue the dialog, reset the chart selection ----
points = (event.get("selection") or {}).get("points") or []
if points:
    cd = points[0].get("customdata")
    if cd:
        try:
            clicked_uid = int(cd[0])
            clicked_day = date.fromisoformat(str(cd[1]))
        except (TypeError, ValueError, IndexError):
            clicked_uid = None
            clicked_day = None
        if clicked_uid is not None and clicked_day is not None:
            st.session_state.pending = (clicked_uid, clicked_day)
            st.session_state.grid_nonce += 1
            st.rerun()
