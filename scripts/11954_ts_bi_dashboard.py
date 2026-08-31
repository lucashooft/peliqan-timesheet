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
project_bi_dashboard (v2.0)

BI dashboard for approval tracking and project analytics, built on top of
timesheet data.

Data source:
  - ts_reporting.fact_timetable      : read-only, already joins timetable
                                        entries with tasks, projects, clients
                                        and users. "approved" here is the
                                        entry-level validation flag (see
                                        ts_prod.timetable.approved).

This app is read-only - it never writes to ts_prod.

Two tabs:
  - Approval overview : pick a month, see every client's approved/total
                         entry count for that month (a client that hits
                         100% shows APPROVED instead of a fraction), and
                         expand a client to see exactly which entries are
                         still unapproved.
  - Project explorer  : pick a month and/or a client, browse every project
                         underneath with its tasks and the individual
                         entries logged against them.
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

ALL_MONTHS = "(all months)"
ALL_CLIENTS = "(all clients)"


def is_true(v):
    # fact_timetable is a query table; booleans can come back as True or as
    # the strings "true"/"1" depending on how the value was written.
    return v in (True, "true", "True", 1, "1")


# =====================================================
# Data loading
# =====================================================

@st.cache_data(ttl=300)
def load_entries():
    dbconn = pq.dbconnect(pq.DW_NAME)
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
    df = dbconn.fetch(pq.DW_NAME, query=sql, df=True)
    return df


df = load_entries()

if df.empty:
    st.warning("No timesheet data found.")
    st.stop()

df["entry_date"] = pd.to_datetime(df["entry_date"])
df["hours"] = df["duration"].astype(float) / 60.0
df["approved"] = df["approved"].apply(is_true)
df["month"] = df["entry_date"].dt.to_period("M").dt.to_timestamp()
df["project_name"] = df["project_name"].fillna("(no project)")
df["client_name"] = df["client_name"].fillna("(no client)")
df["task_name"] = df["task_name"].fillna("(no task)")
df["user_name"] = df["user_name"].fillna("(unknown employee)")

st.title("Project BI Dashboard")
st.caption("Approval status and project/task breakdowns, based on logged timesheet entries.")

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
        selected_month = st.selectbox(
            "Month", month_values, format_func=month_label, key="approval_month"
        )
        month_df = df[df["month"] == selected_month]

        if month_df.empty:
            st.info(f"No entries for {month_label(selected_month)}.")
        else:
            client_stats = (
                month_df.groupby("client_name")
                .agg(total=("entry_id", "count"), approved=("approved", "sum"))
                .reset_index()
            )
            client_stats["approved"] = client_stats["approved"].astype(int)
            client_stats["fully_approved"] = client_stats["approved"] == client_stats["total"]
            client_stats = client_stats.sort_values(
                ["fully_approved", "client_name"], ascending=[True, True]
            )

            total_entries = int(client_stats["total"].sum())
            total_approved = int(client_stats["approved"].sum())
            st.metric(
                f"Approved entries for {month_label(selected_month)}",
                f"{total_approved} / {total_entries}",
            )

            for _, row in client_stats.iterrows():
                client = row["client_name"]
                total = int(row["total"])
                approved = int(row["approved"])
                if row["fully_approved"]:
                    label = f"✅ {client} — APPROVED ({total} entries)"
                else:
                    label = f"{client} — {approved}/{total} approved"

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
                            use_container_width=True,
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
    col1, col2 = st.columns(2)
    with col1:
        explorer_month = st.selectbox(
            "Month", [ALL_MONTHS] + list(month_values), format_func=lambda m: m if m == ALL_MONTHS else month_label(m),
            key="explorer_month",
        )
    with col2:
        explorer_client = st.selectbox(
            "Client", [ALL_CLIENTS] + sorted(df["client_name"].unique().tolist()), key="explorer_client"
        )

    scoped = df
    if explorer_month != ALL_MONTHS:
        scoped = scoped[scoped["month"] == explorer_month]
    if explorer_client != ALL_CLIENTS:
        scoped = scoped[scoped["client_name"] == explorer_client]

    if scoped.empty:
        st.info("No entries match the selected filters.")
    else:
        for client, client_df in scoped.groupby("client_name", sort=True):
            st.subheader(client)
            for project, project_df in client_df.groupby("project_name", sort=True):
                proj_total = len(project_df)
                proj_approved = int(project_df["approved"].sum())
                label = f"{project} — {proj_approved}/{proj_total} approved"
                with st.expander(label):
                    entries = project_df.sort_values(["task_name", "entry_date"])
                    st.dataframe(
                        entries[
                            ["task_name", "entry_date", "user_name", "hours", "billable", "approved"]
                        ].round({"hours": 1}),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "task_name": "Task",
                            "entry_date": "Date",
                            "user_name": "Employee",
                            "hours": "Hours",
                            "billable": "Billable",
                            "approved": "Approved",
                        },
                    )
            st.divider()
