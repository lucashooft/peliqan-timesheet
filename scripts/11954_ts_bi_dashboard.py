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
project_bi_dashboard (v3.0)

BI dashboard for approval tracking and project analytics, built on top of
timesheet data.

Two tabs, two different data sources:
  - Approval overview : reads ts_reporting.fact_timetable (read-only, joins
                         timetable entries with tasks, projects, clients and
                         users). Fine for reporting - it lags writes to
                         ts_prod until its own query re-runs, but nothing on
                         this tab writes anything.
  - Project explorer   : reads AND writes ts_prod.timetable/tasks/projects/
                         clients/users directly. It has to - fact_timetable
                         is a materialized query table, so an edit made
                         through it would not show up here until Peliqan
                         re-runs that query, which would look like the edit
                         silently reverted. Hours and the Approved flag are
                         editable inline; everything else (task, employee,
                         date, billable) is read-only.

NOTE: this app has no login and no scope check (see CLAUDE.md - only
ts_my_week and ts_mcp_server enforce anything). That means the Project
explorer's editing is wide open to anyone with the dashboard URL. Accepted
as a known tradeoff for now.
"""

import streamlit as st
import pandas as pd
from datetime import date

st.set_page_config(page_title="Project BI Dashboard", layout="wide")

st.markdown(
    """
    <style>
        .block-container { padding-top: 1.5rem !important; max-width: 100% !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

DW_NAME = pq.DW_NAME
S = "ts_prod"

ALL_MONTHS = "(all months)"
ALL_CLIENTS = "(all clients)"


def is_true(v):
    # Booleans coming back through Peliqan's query layer can be True, or the
    # strings "true"/"1", depending on how the value was written.
    return v in (True, "true", "True", 1, "1")


def prepare_df(raw_df):
    """Shared post-processing for both data sources - they return the same
    column shape (entry_id, entry_date, duration, billable, approved,
    task_name, task_status, project_name, project_status, project_end_date,
    client_name, user_id, user_name)."""
    d = raw_df.copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"])
    d["hours"] = d["duration"].astype(float) / 60.0
    d["approved"] = d["approved"].apply(is_true)
    d["billable"] = d["billable"].apply(is_true)
    d["month"] = d["entry_date"].dt.to_period("M").dt.to_timestamp()
    d["project_name"] = d["project_name"].fillna("(no project)")
    d["client_name"] = d["client_name"].fillna("(no client)")
    d["task_name"] = d["task_name"].fillna("(no task)")
    d["user_name"] = d["user_name"].fillna("(unknown employee)")
    return d


# =====================================================
# Data loading - Approval overview (read-only)
# =====================================================

@st.cache_data(ttl=300)
def load_entries():
    dbconn = pq.dbconnect(DW_NAME)
    sql = """
        SELECT
            e.entry_id,
            e.entry_date,
            e.duration,
            e.billable,
            e.approved,
            e.task_name,
            e.task_status,
            e.project_name,
            e.project_status,
            e.project_end_date,
            e.client_name,
            e.user_id,
            e.user_name
        FROM ts_reporting.fact_timetable e
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


# =====================================================
# Data loading / writing - Project explorer (live, editable)
# =====================================================

@st.cache_data(ttl=60)
def load_live_entries():
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            t.id                       AS entry_id,
            t.date                     AS entry_date,
            COALESCE(t.duration, 0)    AS duration,
            COALESCE(tk.billable, FALSE) AS billable,
            COALESCE(t.approved, FALSE)  AS approved,
            tk.name                    AS task_name,
            tk.status                  AS task_status,
            p.name                     AS project_name,
            p.status                   AS project_status,
            p.end_date                 AS project_end_date,
            c.name                     AS client_name,
            t.user_id                  AS user_id,
            COALESCE(u.name, u.email)  AS user_name
        FROM {S}.timetable t
        LEFT JOIN {S}.tasks    tk ON tk.id = t.task_id
        LEFT JOIN {S}.projects p  ON p.id = tk.project_id
        LEFT JOIN {S}.clients  c  ON c.id = p.client_id
        LEFT JOIN {S}.users    u  ON u.id::text = t.user_id::text
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


def update_entry(entry_id, values):
    """Write straight to ts_prod.timetable. Callers must pass real Python
    bools/ints - the write proxy runs string values through Decimal(), so
    e.g. "true" for a boolean column comes back as a 400."""
    dbconn = pq.dbconnect(DW_NAME)
    dbconn.update(DW_NAME, S, "timetable", int(entry_id), values)


raw_df = load_entries()

if raw_df.empty:
    st.warning("No timesheet data found.")
    st.stop()

df = prepare_df(raw_df)

st.title("Project BI Dashboard")
st.caption("Approval status (billable entries only) and project/task breakdowns, based on logged timesheet entries.")

month_values = sorted(df["month"].dropna().unique(), reverse=True)


def month_label(m):
    return pd.Timestamp(m).strftime("%B %Y")


tab_approval, tab_explorer = st.tabs(["Approval overview", "Project explorer"])

# =====================================================
# Tab 1: Approval overview
# =====================================================

with tab_approval:
    if not month_values:
        st.info("No entries found.")
    else:
        # Survive a full page refresh (not just a rerun) by round-tripping
        # the selected month through the URL's query string - session_state
        # resets on refresh, but st.query_params doesn't. index= only seeds
        # the widget on a truly fresh session; once "approval_month" exists
        # in session_state, later reruns use that instead (same pattern
        # ts_my_week already uses for its own query params).
        month_strs = [pd.Timestamp(m).strftime("%Y-%m") for m in month_values]
        qp_month = st.query_params.get("month")
        default_month_index = month_strs.index(qp_month) if qp_month in month_strs else 0

        f1, f2 = st.columns(2)
        with f1:
            selected_month = st.selectbox(
                "Month", month_values, format_func=month_label, key="approval_month",
                index=default_month_index,
            )
        with f2:
            sort_by = st.radio(
                "Sort by", ["Needs attention first", "Client name"],
                horizontal=True, key="approval_sort",
            )
        st.query_params["month"] = pd.Timestamp(selected_month).strftime("%Y-%m")

        month_df = df[(df["month"] == selected_month) & (df["billable"])]

        if month_df.empty:
            st.info(f"No billable entries for {month_label(selected_month)}.")
        else:
            client_stats = (
                month_df.groupby("client_name")
                .agg(
                    total=("entry_id", "count"),
                    approved=("approved", "sum"),
                    total_hours=("hours", "sum"),
                    approved_hours=("hours", lambda s: s[month_df.loc[s.index, "approved"]].sum()),
                )
                .reset_index()
            )
            client_stats["approved"] = client_stats["approved"].astype(int)
            client_stats["fully_approved"] = client_stats["approved"] == client_stats["total"]
            if sort_by == "Client name":
                client_stats = client_stats.sort_values("client_name")
            else:
                client_stats = client_stats.sort_values(
                    ["fully_approved", "client_name"], ascending=[True, True]
                )

            total_entries = int(client_stats["total"].sum())
            total_approved = int(client_stats["approved"].sum())
            total_hours = client_stats["total_hours"].sum()
            approved_hours = client_stats["approved_hours"].sum()

            m1, m2 = st.columns(2)
            m1.metric(
                f"Billable entries approved for {month_label(selected_month)}",
                f"{total_approved} / {total_entries}",
            )
            m2.metric(
                "Billable hours approved",
                f"{approved_hours:.1f} / {total_hours:.1f}",
            )

            for _, row in client_stats.iterrows():
                client = row["client_name"]
                total = int(row["total"])
                approved = int(row["approved"])
                if row["fully_approved"]:
                    label = f"{client} — APPROVED ({total} entries, {row['total_hours']:.1f}h)"
                else:
                    label = (
                        f"{client} — {approved}/{total} approved "
                        f"({row['approved_hours']:.1f}/{row['total_hours']:.1f}h)"
                    )

                with st.expander(label):
                    unapproved = month_df[
                        (month_df["client_name"] == client) & (~month_df["approved"])
                    ].sort_values(["entry_date", "user_name"])
                    if unapproved.empty:
                        st.success("All entries approved.")
                    else:
                        st.dataframe(
                            unapproved[
                                ["entry_date", "project_name", "task_name", "user_name",
                                 "hours", "task_status"]
                            ].round({"hours": 1}),
                            width='stretch',
                            hide_index=True,
                            column_config={
                                "entry_date": "Date",
                                "project_name": "Project",
                                "task_name": "Task",
                                "user_name": "Employee",
                                "hours": "Hours",
                                "task_status": "Task status",
                            },
                        )

# =====================================================
# Tab 2: Project explorer
# =====================================================

with tab_explorer:
    live_raw = load_live_entries()

    if live_raw.empty:
        st.info("No timesheet entries found.")
    else:
        live_df = prepare_df(live_raw)
        explorer_months = sorted(live_df["month"].dropna().unique(), reverse=True)

        st.caption(
            "Entries here are live from ts_prod, not the reporting table - "
            "edits below take effect immediately. Hours and Approved are "
            "editable; everything else is read-only."
        )

        col1, col2 = st.columns(2)
        with col1:
            explorer_month = st.selectbox(
                "Month", [ALL_MONTHS] + list(explorer_months),
                format_func=lambda m: m if m == ALL_MONTHS else month_label(m),
                key="explorer_month",
            )
        with col2:
            explorer_client = st.selectbox(
                "Client", [ALL_CLIENTS] + sorted(live_df["client_name"].unique().tolist()),
                key="explorer_client",
            )

        scoped = live_df
        if explorer_month != ALL_MONTHS:
            scoped = scoped[scoped["month"] == explorer_month]
        if explorer_client != ALL_CLIENTS:
            scoped = scoped[scoped["client_name"] == explorer_client]

        if scoped.empty:
            st.info("No entries match the selected filters.")
        else:
            client_expanded = explorer_client != ALL_CLIENTS
            for client, client_df in scoped.groupby("client_name", sort=True):
                client_total = len(client_df)
                client_approved = int(client_df["approved"].sum())
                client_label = f"{client} — {client_approved}/{client_total} approved"
                with st.expander(client_label, expanded=client_expanded):
                    for project, project_df in client_df.groupby("project_name", sort=True):
                        proj_total = len(project_df)
                        proj_approved = int(project_df["approved"].sum())
                        st.markdown(f"**{project}** — {proj_approved}/{proj_total} approved")

                        entries = (
                            project_df.sort_values(["task_name", "entry_date"])
                            .reset_index(drop=True)
                        )
                        display_df = entries[
                            ["entry_id", "task_name", "entry_date", "user_name",
                             "hours", "billable", "approved"]
                        ].copy()
                        display_df["entry_date"] = display_df["entry_date"].dt.strftime("%Y-%m-%d")
                        display_df["hours"] = display_df["hours"].round(2)

                        # A plain st.data_editor reruns the whole script on every
                        # cell edit, which was re-evaluating expanded= above and
                        # snapping expanders shut mid-edit. Wrapping it in a form
                        # defers that rerun until Save is actually clicked.
                        editor_key = f"editor_{client}_{project}"
                        with st.form(f"form_{editor_key}", clear_on_submit=False):
                            edited = st.data_editor(
                                display_df,
                                key=editor_key,
                                width='stretch',
                                hide_index=True,
                                num_rows="fixed",
                                disabled=["entry_id", "task_name", "entry_date", "user_name", "billable"],
                                column_config={
                                    "entry_id": "ID",
                                    "task_name": "Task",
                                    "entry_date": "Date",
                                    "user_name": "Employee",
                                    "hours": "Hours",
                                    "billable": "Billable",
                                    "approved": "Approved",
                                },
                            )
                            submitted = st.form_submit_button("Save changes")

                        if submitted:
                            changes = {}
                            for i in range(len(entries)):
                                entry_id = int(entries.loc[i, "entry_id"])
                                orig_hours = round(float(entries.loc[i, "hours"]), 2)
                                orig_approved = bool(entries.loc[i, "approved"])
                                new_hours = round(float(edited.loc[i, "hours"]), 2)
                                new_approved = bool(edited.loc[i, "approved"])

                                updates = {}
                                if new_hours != orig_hours:
                                    if new_hours <= 0:
                                        st.warning(f"Entry {entry_id}: hours must be positive, skipped.")
                                    else:
                                        updates["duration"] = int(round(new_hours * 60))
                                if new_approved != orig_approved:
                                    updates["approved"] = new_approved

                                if updates:
                                    changes[entry_id] = updates

                            if not changes:
                                st.info("No changes to save.")
                            else:
                                for entry_id, updates in changes.items():
                                    update_entry(entry_id, updates)
                                load_live_entries.clear()
                                plural = "y" if len(changes) == 1 else "ies"
                                st.success(f"Saved {len(changes)} entr{plural}.")
                                st.rerun()

                        st.divider()
