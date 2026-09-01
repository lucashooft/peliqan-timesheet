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
ts_planner (v1) - team resourcing overview.

Read-only, styled to feel like ts_my_week's calendar. One row per
employee, WINDOW_DAYS blocks per row - the last 4 workweeks (Mon-Fri) by
default, navigable in 4-week jumps - each block colored by whichever
project that day's logged hours went to. A day split between two
projects splits its block the same proportion, sorted biggest project
first; a day with nothing logged stays a blank cell.

No login, no scope check - same as every Data App in this repo except
12011_ts_my_week and 11383_ts_mcp_server (see CLAUDE.md). Anyone who can
open the app sees every employee's window.

Data (all read-only):
  - ts_prod.users                  employee list
  - ts_prod.timetable              logged entries in the visible window
  - ts_prod.tasks/projects/clients lookups (project name, color)

NOTE: st.set_page_config must stay a literal string - the Peliqan runtime
lifts that call into a system prepend that runs before this script body.
"""

import plotly.graph_objects as go
import pandas as pd
import streamlit as st
from datetime import date, timedelta

st.set_page_config(page_title="Team planner", layout="wide")

st.markdown("<style>.block-container{padding-top:2.2rem;}</style>",
            unsafe_allow_html=True)

# =====================================================
# Config
# =====================================================

DW_NAME = "dw_3202"
S = "ts_prod"

WORKDAYS_PER_WEEK = 5
WEEKS_IN_WINDOW = 4
WINDOW_DAYS = WORKDAYS_PER_WEEK * WEEKS_IN_WINDOW   # 20

DAY_ABBREV = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ROW_PX = 34

# Cycled by project_id, the same way ts_my_week cycles CLIENT_COLORS by
# client_id - two employees on the same project always land on the same
# color without a shared color table to keep in sync.
PROJECT_COLORS = ["#4c78a8", "#54a24b", "#b279a2", "#f58518", "#e45756",
                  "#eeca3b", "#9d755d", "#72b7b2", "#ff9da6", "#1b9e77",
                  "#7570b3", "#bab0ac"]

EMPTY_CELL_COLOR = "#f5f6f8"
LOOKUP_DEFAULT = {"project": "?", "project_id": 0, "client": "-",
                  "color": "#9d9da6"}

# =====================================================
# Helpers
# =====================================================

def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def fmt_dur(minutes):
    minutes = int(minutes or 0)
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h{m:02d}"
    if h:
        return f"{h}h"
    return f"{m}m"


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
def load_task_lookup():
    """{task_id: {project, project_id, client, color}}."""
    dbconn = pq.dbconnect(DW_NAME)
    rows = dbconn.fetch(DW_NAME, query=f"""
        SELECT t.id AS task_id, t.project_id,
               COALESCE(p.name, '-') AS project,
               COALESCE(c.name, '-') AS client
        FROM {S}.tasks t
        LEFT JOIN {S}.projects p ON p.id = t.project_id
        LEFT JOIN {S}.clients  c ON c.id = p.client_id
    """) or []
    lookup = {}
    for r in rows:
        tid = to_int(r.get("task_id"))
        if tid is None:
            continue
        pid = to_int(r.get("project_id")) or 0
        lookup[tid] = {
            "project": r.get("project") or f"Project {pid}",
            "project_id": pid,
            "client": r.get("client") or "-",
            "color": PROJECT_COLORS[pid % len(PROJECT_COLORS)],
        }
    return lookup


@st.cache_data(ttl=60)
def load_window_entries(start_date, end_date):
    """Every logged entry across ALL employees in [start_date, end_date]."""
    dbconn = pq.dbconnect(DW_NAME)
    rows = dbconn.fetch(DW_NAME, query=f"""
        SELECT user_id, task_id, date, duration
        FROM {S}.timetable
        WHERE date >= '{start_date.isoformat()}'
          AND date <  '{(end_date + timedelta(days=1)).isoformat()}'
    """) or []
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["duration"] = pd.to_numeric(df["duration"], errors="coerce").fillna(0).astype(int)
    df["user_id"] = df["user_id"].apply(to_int)
    df["task_id"] = df["task_id"].apply(to_int)
    return df.dropna(subset=["day"])

# =====================================================
# Grid model (pure function -> unit-testable)
# =====================================================

def build_cells(user_ids, days, entries_df, lookup):
    """
    [{user_id, day_i, y0, y1, color, project, client, minutes}, ...]

    y0/y1 are fractions of ONE row (0..1): a day worked on a single
    project fills the whole block for that day, a day split between two
    projects splits the block the same proportion - sorted biggest first
    so the dominant project always starts at the top.
    """
    if entries_df.empty:
        return []
    day_index = {d: i for i, d in enumerate(days)}
    scoped = entries_df[entries_df["user_id"].isin(user_ids)
                        & entries_df["day"].isin(day_index)]
    cells = []
    for (uid, day), day_rows in scoped.groupby(["user_id", "day"]):
        agg = {}
        for _, r in day_rows.iterrows():
            info = lookup.get(r["task_id"], LOOKUP_DEFAULT)
            key = (info["project"], info["client"], info["color"])
            agg[key] = agg.get(key, 0) + int(r["duration"])
        day_total = sum(agg.values())
        if day_total <= 0:
            continue
        y0 = 0.0
        for (project, client, color), minutes in sorted(agg.items(), key=lambda kv: -kv[1]):
            y1 = y0 + minutes / day_total
            cells.append({"user_id": uid, "day_i": day_index[day], "y0": y0, "y1": y1,
                         "color": color, "project": project, "client": client,
                         "minutes": minutes})
            y0 = y1
    return cells

# =====================================================
# State
# =====================================================

DEFAULT_START = monday_of(date.today()) - timedelta(weeks=WEEKS_IN_WINDOW - 1)

if "planner_start" not in st.session_state:
    st.session_state.planner_start = DEFAULT_START

st.title("Team planner")
st.caption("One row per employee, one block per workday - colored by whichever "
          "project the logged hours that day went to.")

users = load_users()
if not users:
    st.warning("No users found - check ts_prod.users.")
    st.stop()

lookup = load_task_lookup()

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

entries_df = load_window_entries(days[0], days[-1])
cells = build_cells(set(user_ids), days, entries_df, lookup)
row_of = {uid: i for i, uid in enumerate(user_ids)}

# =====================================================
# Legend
# =====================================================

seen = {}
for c in cells:
    seen.setdefault((c["client"], c["project"]), c["color"])

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
    st.caption("No entries logged in this window yet.")

# =====================================================
# The grid (Plotly, styled to match ts_my_week's calendar)
# =====================================================

n_rows = len(display_users)
fig = go.Figure()

# blank placeholder for every cell -> a visible grid even where nobody
# logged anything, drawn first so the colored segments sit on top of it
fig.add_trace(go.Bar(
    x=[i for i in range(len(days))] * n_rows,
    width=0.94,
    base=[r for r in range(n_rows) for _ in days],
    y=[1] * (n_rows * len(days)),
    marker=dict(color=EMPTY_CELL_COLOR, line=dict(color="white", width=1)),
    hoverinfo="skip",
    showlegend=False,
    name="grid",
))

if cells:
    fig.add_trace(go.Bar(
        x=[c["day_i"] for c in cells],
        width=0.94,
        base=[row_of[c["user_id"]] + c["y0"] for c in cells],
        y=[c["y1"] - c["y0"] for c in cells],
        marker=dict(color=[c["color"] for c in cells], line=dict(color="white", width=1)),
        text=[c["project"] if (c["y1"] - c["y0"]) >= 0.22 else "" for c in cells],
        textposition="inside",
        insidetextanchor="middle",
        textfont=dict(color="white", size=9,
                      family="Source Sans Pro, Helvetica Neue, sans-serif"),
        hovertext=[f"<b>{c['client']}</b> - {c['project']}<br>{fmt_dur(c['minutes'])}"
                  for c in cells],
        hovertemplate="%{hovertext}<extra></extra>",
        showlegend=False,
        name="entries",
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

st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})
