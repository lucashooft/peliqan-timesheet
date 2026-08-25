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
project_bi_dashboard (v1.1)

BI dashboard for project analytics, built on top of timesheet data.

Data sources:
  - ts_reporting.fact_timetable      : read-only, already joins timetable
                                        entries with tasks, projects, clients
                                        and users.
  - ts_prod.timetable_submissions    : weekly submit -> validate workflow per
                                        user. Joined here on user_id + the
                                        Monday of the entry's week, so every
                                        logged hour can be classified as
                                        Draft / Submitted / Validated.

This app is read-only - it never writes to ts_prod.timetable or
ts_prod.timetable_submissions, it only reports on them.
"""

import streamlit as st
import pandas as pd
from datetime import date, timedelta

st.set_page_config(page_title="Project BI Dashboard", layout="wide")

st.markdown(
    """
    <style>
        .block-container { padding-top: 1.5rem !important; max-width: 100% !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

STATUS_LABELS = {
    None: "Draft",
    "submitted": "Submitted",
    "confirmed": "Validated",
}

# =====================================================
# Data loading
# =====================================================

@st.cache_data(ttl=300)
def load_entries():
    dbconn = pq.dbconnect(pq.DW_NAME)
    sql = """
        WITH entries AS (
            SELECT
                e.entry_id,
                e.entry_date,
                CAST(date_trunc('week', e.entry_date) AS date) AS week_start,
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
        ),
        subs AS (
            SELECT
                user_id,
                CAST(week_start_date AS date) AS week_start,
                status
            FROM ts_prod.timetable_submissions
        )
        SELECT
            entries.*,
            subs.status AS submission_status
        FROM entries
        LEFT JOIN subs
            ON subs.user_id = entries.user_id
           AND subs.week_start = entries.week_start
    """
    df = dbconn.fetch(pq.DW_NAME, query=sql, df=True)
    return df


df = load_entries()

if df.empty:
    st.warning("No timesheet data found.")
    st.stop()

df["entry_date"] = pd.to_datetime(df["entry_date"])
df["hours"] = df["duration"].astype(float) / 60.0
df["status_label"] = df["submission_status"].map(STATUS_LABELS).fillna("Draft")
df["month"] = df["entry_date"].dt.to_period("M").dt.to_timestamp()
df["project_name"] = df["project_name"].fillna("(no project)")
df["client_name"] = df["client_name"].fillna("(no client)")
df["task_name"] = df["task_name"].fillna("(no task)")
df["user_name"] = df["user_name"].fillna("(unknown employee)")

# =====================================================
# Filters
# =====================================================

st.title("Project BI Dashboard")
st.caption("Hours, billability and validation status across all projects, based on logged timesheet entries.")

with st.sidebar:
    st.header("Filters")

    min_date = df["entry_date"].min().date()
    max_date = df["entry_date"].max().date()
    date_range = st.date_input(
        "Entry date range",
        value=(max(min_date, max_date - timedelta(days=365)), max_date),
        min_value=min_date,
        max_value=max_date,
    )

    clients = sorted(df["client_name"].unique().tolist())
    selected_clients = st.multiselect("Client", clients, default=clients)

    statuses = sorted(df["project_status"].dropna().unique().tolist())
    selected_statuses = st.multiselect("Project status", statuses, default=statuses)

    validation_options = ["Draft", "Submitted", "Validated"]
    selected_validation = st.multiselect("Validation status", validation_options, default=validation_options)

if isinstance(date_range, tuple) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date, end_date = min_date, max_date

mask = (
    (df["entry_date"].dt.date >= start_date)
    & (df["entry_date"].dt.date <= end_date)
    & (df["client_name"].isin(selected_clients))
    & (df["status_label"].isin(selected_validation))
)
if selected_statuses:
    mask &= df["project_status"].isin(selected_statuses)

fdf = df[mask].copy()

if fdf.empty:
    st.warning("No entries match the selected filters.")
    st.stop()

# =====================================================
# KPI row
# =====================================================

total_hours = fdf["hours"].sum()
billable_hours = fdf.loc[fdf["billable"] == True, "hours"].sum()
validated_hours = fdf.loc[fdf["status_label"] == "Validated", "hours"].sum()
active_projects = fdf["project_name"].nunique()
active_clients = fdf["client_name"].nunique()

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total hours", f"{total_hours:,.1f}")
k2.metric("Billable hours", f"{billable_hours:,.1f}", f"{(billable_hours / total_hours * 100):.0f}% of total" if total_hours else None)
k3.metric("Validated hours", f"{validated_hours:,.1f}", f"{(validated_hours / total_hours * 100):.0f}% of total" if total_hours else None)
k4.metric("Active projects", active_projects)
k5.metric("Active clients", active_clients)

st.divider()

# =====================================================
# Project summary table
# =====================================================

st.subheader("Hours by project")

project_summary = (
    fdf.pivot_table(
        index=["project_name", "client_name", "project_status", "project_end_date"],
        columns="status_label",
        values="hours",
        aggfunc="sum",
        fill_value=0,
    )
    .reset_index()
)
for col in validation_options:
    if col not in project_summary.columns:
        project_summary[col] = 0.0

billable_by_project = fdf.groupby("project_name")["hours"].apply(lambda s: s[fdf.loc[s.index, "billable"] == True].sum())
users_by_project = fdf.groupby("project_name")["user_name"].nunique()

project_summary["Total hours"] = project_summary[validation_options].sum(axis=1)
project_summary["Billable hours"] = project_summary["project_name"].map(billable_by_project).fillna(0)
project_summary["Distinct users"] = project_summary["project_name"].map(users_by_project).fillna(0).astype(int)
project_summary = project_summary.sort_values("Total hours", ascending=False)

display_cols = ["project_name", "client_name", "project_status", "project_end_date",
                 "Total hours", "Billable hours", "Draft", "Submitted", "Validated", "Distinct users"]
st.dataframe(
    project_summary[display_cols].round(1),
    use_container_width=True,
    hide_index=True,
    column_config={
        "project_name": "Project",
        "client_name": "Client",
        "project_status": "Status",
        "project_end_date": "End date",
    },
)

st.divider()

# =====================================================
# Projects nearing / past their end date with unvalidated hours
# =====================================================

st.subheader("Attention needed: projects past end date with unvalidated hours")
today = pd.Timestamp(date.today())
project_summary["project_end_date"] = pd.to_datetime(project_summary["project_end_date"], errors="coerce")
flagged = project_summary[
    (project_summary["project_end_date"].notna())
    & (project_summary["project_end_date"] < today)
    & ((project_summary["Draft"] + project_summary["Submitted"]) > 0)
]
if flagged.empty:
    st.success("No overdue projects with unvalidated hours.")
else:
    st.dataframe(
        flagged[["project_name", "client_name", "project_end_date", "Draft", "Submitted", "Validated"]].round(1),
        use_container_width=True,
        hide_index=True,
    )

st.divider()

# =====================================================
# Employee time breakdown: client -> project -> employee -> task
# =====================================================

st.header("Employee time breakdown")
st.caption("Who worked how long on which project for which client, and on what tasks.")

drill_col1, drill_col2, drill_col3 = st.columns(3)

with drill_col1:
    drill_clients = sorted(fdf["client_name"].unique().tolist())
    drill_client = st.selectbox("1. Select client", ["(all)"] + drill_clients, key="drill_client")

client_scoped = fdf if drill_client == "(all)" else fdf[fdf["client_name"] == drill_client]

with drill_col2:
    drill_projects = sorted(client_scoped["project_name"].unique().tolist())
    drill_project = st.selectbox("2. Select project", ["(all)"] + drill_projects, key="drill_project")

project_scoped = client_scoped if drill_project == "(all)" else client_scoped[client_scoped["project_name"] == drill_project]

with drill_col3:
    drill_employees = sorted(project_scoped["user_name"].unique().tolist())
    drill_employee = st.selectbox("3. Select employee", ["(all)"] + drill_employees, key="drill_employee")

employee_scoped = project_scoped if drill_employee == "(all)" else project_scoped[project_scoped["user_name"] == drill_employee]

if employee_scoped.empty:
    st.info("No hours logged for this combination.")
else:
    drill_total = employee_scoped["hours"].sum()
    st.metric("Hours for current selection", f"{drill_total:,.1f}")

    task_breakdown = (
        employee_scoped.groupby(["client_name", "project_name", "user_name", "task_name"])["hours"]
        .sum()
        .reset_index()
        .sort_values("hours", ascending=False)
    )
    task_breakdown["hours"] = task_breakdown["hours"].round(1)
    st.dataframe(
        task_breakdown,
        use_container_width=True,
        hide_index=True,
        column_config={
            "client_name": "Client",
            "project_name": "Project",
            "user_name": "Employee",
            "task_name": "Task",
            "hours": "Hours",
        },
    )

    if drill_employee == "(all)" and employee_scoped["user_name"].nunique() > 1:
        st.subheader("Hours by employee (current selection)")
        emp_hours = employee_scoped.groupby("user_name")["hours"].sum().sort_values(ascending=False)
        st.bar_chart(emp_hours)

st.divider()

# =====================================================
# Full summary: employee x project x task, filterable/sortable
# =====================================================

st.subheader("Employee x Project x Task summary (all filtered data)")

summary_col1, summary_col2, summary_col3 = st.columns(3)
with summary_col1:
    f_clients = st.multiselect("Filter client(s)", sorted(fdf["client_name"].unique().tolist()), key="summary_clients")
with summary_col2:
    f_projects = st.multiselect("Filter project(s)", sorted(fdf["project_name"].unique().tolist()), key="summary_projects")
with summary_col3:
    f_employees = st.multiselect("Filter employee(s)", sorted(fdf["user_name"].unique().tolist()), key="summary_employees")

summary_df = fdf.copy()
if f_clients:
    summary_df = summary_df[summary_df["client_name"].isin(f_clients)]
if f_projects:
    summary_df = summary_df[summary_df["project_name"].isin(f_projects)]
if f_employees:
    summary_df = summary_df[summary_df["user_name"].isin(f_employees)]

full_summary = (
    summary_df.groupby(["client_name", "project_name", "user_name", "task_name"])
    .agg(
        hours=("hours", "sum"),
        billable_hours=("hours", lambda s: s[summary_df.loc[s.index, "billable"] == True].sum()),
        entries=("entry_id", "count"),
    )
    .reset_index()
    .sort_values("hours", ascending=False)
)
full_summary["hours"] = full_summary["hours"].round(1)
full_summary["billable_hours"] = full_summary["billable_hours"].round(1)

st.dataframe(
    full_summary,
    use_container_width=True,
    hide_index=True,
    column_config={
        "client_name": "Client",
        "project_name": "Project",
        "user_name": "Employee",
        "task_name": "Task",
        "hours": "Total hours",
        "billable_hours": "Billable hours",
        "entries": "# entries",
    },
)
st.caption(f"{len(full_summary)} rows. Use the column headers in the table to sort; use the filters above to narrow down.")

with st.expander("Show raw filtered entries"):
    st.dataframe(
        fdf[["entry_date", "project_name", "client_name", "task_name", "user_name",
             "hours", "billable", "status_label"]].sort_values("entry_date", ascending=False),
        use_container_width=True,
        hide_index=True,
    )
