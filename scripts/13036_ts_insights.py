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

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from datetime import date, timedelta

# =====================================================
# Config
# =====================================================

DW_NAME = "dw_3202"
SCHEMA = "ts_prod"

CLIENTS_TABLE = "clients"
USERS_TABLE = "users"
PROJECTS_TABLE = "projects"
TASKS_TABLE = "tasks"

TASK_STATUSES = ["done", "todo", "in_progress"]

# Matches ts_8h_alerter's convention: a workday is Mon-Fri, 8h expected.
WORKDAY_MINUTES = 480
WORKDAYS = {0, 1, 2, 3, 4}

# Client 43 ("Internal Support") is not a real client - it's a bucket. Projects
# under it are named after the real client they were support work for, so
# those hours belong to that client too (matched by project name == client name).
INTERNAL_SUPPORT_CLIENT_ID = 43

st.set_page_config(page_title="Insights", layout="wide")

# =====================================================
# Data loading
# =====================================================

@st.cache_data(ttl=300)
def load_clients():
    dbconn = pq.dbconnect(DW_NAME)
    return sorted(
        dbconn.fetch(DW_NAME, SCHEMA, CLIENTS_TABLE) or [],
        key=lambda c: (c.get("name") or "").lower()
    )


@st.cache_data(ttl=300)
def load_projects():
    dbconn = pq.dbconnect(DW_NAME)
    return dbconn.fetch(DW_NAME, SCHEMA, PROJECTS_TABLE) or []


@st.cache_data(ttl=300)
def load_tasks():
    dbconn = pq.dbconnect(DW_NAME)
    return dbconn.fetch(DW_NAME, SCHEMA, TASKS_TABLE) or []


def sql_int_list(ids):
    return ",".join(str(int(i)) for i in ids)


def sql_text_list(ids):
    return ",".join(f"'{int(i)}'" for i in ids)


@st.cache_data(ttl=300)
def load_hours_by_user_for_project(project_ids, start_date, end_date):
    if not project_ids:
        return pd.DataFrame(columns=["user_name", "total_minutes"])
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            u.name AS user_name,
            SUM(t.duration) AS total_minutes
        FROM ts_prod.timetable t
        JOIN ts_prod.tasks tk ON tk.id = t.task_id
        JOIN ts_prod.users u ON u.id::text = t.user_id
        WHERE tk.project_id IN ({sql_int_list(project_ids)})
          AND t.date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
        GROUP BY u.name
        ORDER BY total_minutes DESC
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


@st.cache_data(ttl=300)
def load_users():
    dbconn = pq.dbconnect(DW_NAME)
    return sorted(
        dbconn.fetch(DW_NAME, SCHEMA, USERS_TABLE) or [],
        key=lambda u: (u.get("name") or "").lower()
    )


@st.cache_data(ttl=300)
def load_hours_by_project_for_user(user_ids, start_date, end_date):
    if not user_ids:
        return pd.DataFrame(columns=["project_name", "total_minutes"])
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            p.name AS project_name,
            SUM(t.duration) AS total_minutes
        FROM ts_prod.timetable t
        JOIN ts_prod.tasks tk ON tk.id = t.task_id
        JOIN ts_prod.projects p ON p.id = tk.project_id
        WHERE t.user_id::text IN ({sql_text_list(user_ids)})
          AND t.date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
        GROUP BY p.name
        ORDER BY total_minutes DESC
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


@st.cache_data(ttl=300)
def load_hours_by_user_all(start_date, end_date):
    """Logged minutes per user across all clients/projects, including users
    who logged nothing (LEFT JOIN so they still show up at 0%)."""
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            u.name AS user_name,
            COALESCE(SUM(t.duration), 0) AS total_minutes
        FROM ts_prod.users u
        LEFT JOIN ts_prod.timetable t
            ON t.user_id::text = u.id::text
            AND t.date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
        GROUP BY u.name
        ORDER BY total_minutes DESC
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


def expected_workday_minutes(start_date, end_date):
    """Expected minutes = one workday (Mon-Fri, 8h) per weekday in the period,
    capped at yesterday since today's entries aren't final yet - mirrors
    ts_8h_alerter's tracking window."""
    window_end = min(end_date, date.today() - timedelta(days=1))
    if window_end < start_date:
        return 0
    num_days = (window_end - start_date).days + 1
    workdays = sum(
        1 for i in range(num_days)
        if (start_date + timedelta(days=i)).weekday() in WORKDAYS
    )
    return workdays * WORKDAY_MINUTES


@st.cache_data(ttl=300)
def load_task_ids_for_user(user_ids, start_date, end_date):
    if not user_ids:
        return set()
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT DISTINCT task_id
        FROM ts_prod.timetable
        WHERE user_id::text IN ({sql_text_list(user_ids)})
          AND date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
    """
    df = dbconn.fetch(DW_NAME, query=sql, df=True)
    return set(df["task_id"].tolist()) if not df.empty else set()


@st.cache_data(ttl=300)
def load_support_hours_by_client(start_date, end_date):
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            p.name AS client_name,
            SUM(t.duration) AS total_minutes
        FROM ts_prod.timetable t
        JOIN ts_prod.tasks tk ON tk.id = t.task_id
        JOIN ts_prod.projects p ON p.id = tk.project_id
        WHERE p.client_id = {INTERNAL_SUPPORT_CLIENT_ID}
          AND t.date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
        GROUP BY p.name
        ORDER BY total_minutes DESC
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


@st.cache_data(ttl=300)
def load_support_hours_over_time(start_date, end_date, granularity):
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            date_trunc('{granularity}', t.date::date) AS period,
            SUM(t.duration) AS total_minutes
        FROM ts_prod.timetable t
        JOIN ts_prod.tasks tk ON tk.id = t.task_id
        JOIN ts_prod.projects p ON p.id = tk.project_id
        WHERE p.client_id = {INTERNAL_SUPPORT_CLIENT_ID}
          AND t.date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
        GROUP BY period
        ORDER BY period
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


@st.cache_data(ttl=300)
def load_overview_totals(start_date, end_date):
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            SUM(t.duration) AS total_minutes,
            COUNT(DISTINCT t.user_id) AS user_count,
            COUNT(DISTINCT p.id) AS project_count,
            COUNT(DISTINCT p.client_id) AS client_count
        FROM ts_prod.timetable t
        JOIN ts_prod.tasks tk ON tk.id = t.task_id
        JOIN ts_prod.projects p ON p.id = tk.project_id
        WHERE t.date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


@st.cache_data(ttl=300)
def load_overview_hours_over_time(start_date, end_date, granularity):
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            date_trunc('{granularity}', date::date) AS period,
            SUM(duration) AS total_minutes
        FROM ts_prod.timetable
        WHERE date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
        GROUP BY period
        ORDER BY period
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


@st.cache_data(ttl=300)
def load_top_clients_by_hours(start_date, end_date, limit=5):
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            c.name AS client_name,
            SUM(t.duration) AS total_minutes
        FROM ts_prod.timetable t
        JOIN ts_prod.tasks tk ON tk.id = t.task_id
        JOIN ts_prod.projects p ON p.id = tk.project_id
        LEFT JOIN ts_prod.clients c ON c.id = p.client_id
        WHERE t.date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
        GROUP BY c.name
        ORDER BY total_minutes DESC
        LIMIT {int(limit)}
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


@st.cache_data(ttl=300)
def load_top_users_by_hours(start_date, end_date, limit=5):
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            u.name AS user_name,
            SUM(t.duration) AS total_minutes
        FROM ts_prod.timetable t
        JOIN ts_prod.users u ON u.id::text = t.user_id
        WHERE t.date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
        GROUP BY u.name
        ORDER BY total_minutes DESC
        LIMIT {int(limit)}
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


def hours_granularity(start_date, end_date):
    return "week" if (end_date - start_date).days > 60 else "day"


@st.cache_data(ttl=300)
def load_hours_over_time_for_user(user_ids, start_date, end_date, granularity):
    if not user_ids:
        return pd.DataFrame(columns=["period", "total_minutes"])
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            date_trunc('{granularity}', date::date) AS period,
            SUM(duration) AS total_minutes
        FROM ts_prod.timetable
        WHERE user_id::text IN ({sql_text_list(user_ids)})
          AND date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
        GROUP BY period
        ORDER BY period
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


@st.cache_data(ttl=300)
def load_raw_entries(project_ids, user_ids, start_date, end_date):
    if not project_ids or not user_ids:
        return pd.DataFrame(columns=[
            "id", "date", "duration", "internal_description", "external_description",
            "approved", "user_name", "task_name", "project_name", "client_name",
        ])
    dbconn = pq.dbconnect(DW_NAME)
    sql = f"""
        SELECT
            t.id,
            t.date,
            t.duration,
            t.internal_description,
            t.external_description,
            t.approved,
            u.name AS user_name,
            tk.name AS task_name,
            p.name AS project_name,
            c.name AS client_name
        FROM ts_prod.timetable t
        JOIN ts_prod.tasks tk ON tk.id = t.task_id
        JOIN ts_prod.projects p ON p.id = tk.project_id
        LEFT JOIN ts_prod.clients c ON c.id = p.client_id
        JOIN ts_prod.users u ON u.id::text = t.user_id
        WHERE tk.project_id IN ({sql_int_list(project_ids)})
          AND t.user_id::text IN ({sql_text_list(user_ids)})
          AND t.date::date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
        ORDER BY t.date DESC
    """
    return dbconn.fetch(DW_NAME, query=sql, df=True)


def multiselect_with_controls(label, key, all_ids, id_to_label, searchable=False, on_commit=None):
    """A closed dropdown (popover) with a checkbox per option to click on to
    select/deselect it, plus Select all / Clear filters buttons. Checkbox
    state is the source of truth (each checkbox keeps its own session_state
    entry, defaulting to checked), so stale options after an upstream filter
    change are simply ignored instead of crashing. An empty selection is
    treated as "no filter" (i.e. show everything). If searchable, a live
    search box narrows which checkboxes are shown, without touching their
    checked state. If given, on_commit(new_effective_ids, old_effective_ids)
    fires whenever the effective (empty-means-all) selection actually
    changes, so a caller can cascade the change into a dependent dropdown."""
    all_ids = list(all_ids)

    def checkbox_key(i):
        return f"{key}_chk_{i}"

    def select_all():
        for i in all_ids:
            st.session_state[checkbox_key(i)] = True

    def clear_filters():
        for i in all_ids:
            st.session_state[checkbox_key(i)] = False

    before = {i for i in all_ids if st.session_state.get(checkbox_key(i), True)}

    if not before:
        summary = f"{label}: All (cleared)"
    elif len(before) == len(all_ids):
        summary = f"{label}: All"
    else:
        summary = f"{label}: {len(before)}/{len(all_ids)}"

    with st.popover(summary, use_container_width=True):
        visible_ids = all_ids
        if searchable:
            query = st.text_input(
                "Search", key=f"{key}_search", placeholder="Search...", label_visibility="collapsed"
            )
            if query.strip():
                q = query.strip().lower()
                visible_ids = [i for i in all_ids if q in id_to_label[i].lower()]
                if not visible_ids:
                    st.caption("No matches.")

        b1, b2 = st.columns(2)
        b1.button(
            "Select all", key=f"{key}_select_all_btn", on_click=select_all, use_container_width=True
        )
        b2.button(
            "Clear filters", key=f"{key}_clear_btn", on_click=clear_filters, use_container_width=True
        )
        st.divider()

        checkbox_cols = st.columns(2)
        for idx, i in enumerate(visible_ids):
            with checkbox_cols[idx % 2]:
                st.checkbox(id_to_label[i], value=True, key=checkbox_key(i))

    after = {i for i in all_ids if st.session_state.get(checkbox_key(i), True)}

    if on_commit and after != before:
        effective_before = before if before else set(all_ids)
        effective_after = after if after else set(all_ids)
        on_commit(effective_after, effective_before)

    return list(after) if after else list(all_ids)


def is_truthy(value):
    """dbconn.fetch(schema, table) returns booleans as the strings "true"/"false",
    not Python bools, so a plain truthiness check on the raw value is always True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def pie_chart(labels, values, title):
    fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.4))
    fig.update_layout(title=title, margin=dict(t=40, b=0, l=0, r=0), height=320)
    return fig


def hours_line_chart(x, y, x_title, y_title):
    fig = go.Figure(go.Scatter(x=x, y=y, mode="lines+markers"))
    fig.update_layout(
        xaxis_title=x_title,
        yaxis_title=y_title,
        xaxis=dict(tickformat="%d-%m-%Y", tickfont=dict(size=16)),
        yaxis=dict(tickfont=dict(size=16)),
        font=dict(size=16),
        margin=dict(t=20, b=0, l=0, r=0),
        height=350,
    )
    return fig


st.title("Insights")

period_col, _ = st.columns([1, 3])
with period_col:
    date_range = st.date_input(
        "Period",
        value=(date.today() - timedelta(days=90), date.today()),
        key="global_date_range",
    )

if isinstance(date_range, tuple) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date, end_date = date.today() - timedelta(days=90), date.today()

tab_clients, tab_users, tab_managers, tab_raw = st.tabs(["Clients", "Users", "Managers", "Raw data"])
# tab_support disabled - see commented-out "with tab_support:" block below.
# tab_overview disabled - see commented-out "with tab_overview:" block below.

# with tab_overview:
#     st.header("Overview")
#
#     totals = load_overview_totals(start_date, end_date)
#     total_minutes = totals["total_minutes"].iloc[0] if not totals.empty else None
#
#     if not total_minutes:
#         st.info("No hours logged in the selected period.")
#     else:
#         row = totals.iloc[0]
#         k1, k2, k3, k4 = st.columns(4)
#         k1.metric("Total hours", f"{total_minutes / 60.0:,.1f}")
#         k2.metric("Active clients", int(row["client_count"] or 0))
#         k3.metric("Active users", int(row["user_count"] or 0))
#         k4.metric("Active projects", int(row["project_count"] or 0))
#
#         st.subheader("Hours over time")
#         granularity = hours_granularity(start_date, end_date)
#         overview_trend = load_overview_hours_over_time(start_date, end_date, granularity)
#
#         if not overview_trend.empty:
#             overview_trend["hours"] = overview_trend["total_minutes"] / 60.0
#             st.plotly_chart(
#                 hours_line_chart(
#                     overview_trend["period"],
#                     overview_trend["hours"],
#                     granularity.capitalize(),
#                     "Hours",
#                 ),
#                 use_container_width=True,
#                 key="overview_hours_over_time_chart",
#             )
#
#         top_col1, top_col2 = st.columns(2)
#
#         with top_col1:
#             st.subheader("Top clients")
#             top_clients = load_top_clients_by_hours(start_date, end_date)
#             top_clients["hours"] = (top_clients["total_minutes"] / 60.0).round(1)
#             st.dataframe(
#                 top_clients[["client_name", "hours"]],
#                 use_container_width=True,
#                 hide_index=True,
#                 column_config={
#                     "client_name": "Client",
#                     "hours": "Hours",
#                 },
#             )
#
#         with top_col2:
#             st.subheader("Top users")
#             top_users = load_top_users_by_hours(start_date, end_date)
#             top_users["hours"] = (top_users["total_minutes"] / 60.0).round(1)
#             st.dataframe(
#                 top_users[["user_name", "hours"]],
#                 use_container_width=True,
#                 hide_index=True,
#                 column_config={
#                     "user_name": "User",
#                     "hours": "Hours",
#                 },
#             )

with tab_clients:
    clients = load_clients()

    if not clients:
        st.info("No clients found.")
    else:
        client_options = {c["id"]: c["name"] for c in clients}
        client_by_id = {c["id"]: c for c in clients}
        all_projects = load_projects()

        def projects_for_client_ids(client_ids):
            names = {client_by_id[cid]["name"] for cid in client_ids if cid in client_by_id}
            return {
                p["id"] for p in all_projects
                if p.get("client_id") in client_ids
                or (p.get("client_id") == INTERNAL_SUPPORT_CLIENT_ID and p.get("name") in names)
            }

        def on_clients_committed(new_effective, old_effective):
            added = new_effective - old_effective
            removed = old_effective - new_effective
            if not added and not removed:
                return
            proj_committed_key = "clients_tab_selected_project_ids_committed"
            current = st.session_state.get(proj_committed_key)
            if current is None:
                return
            current = set(current)
            current |= projects_for_client_ids(added)
            current -= projects_for_client_ids(removed)
            st.session_state[proj_committed_key] = current

        filter_col1, filter_col2 = st.columns(2)

        with filter_col1:
            selected_client_ids = multiselect_with_controls(
                "Client", "clients_tab_selected_client_ids", client_options.keys(), client_options,
                searchable=True, on_commit=on_clients_committed,
            )
        selected_clients = [c for c in clients if c["id"] in selected_client_ids]
        selected_client_names = {c["name"] for c in selected_clients}

        client_projects = sorted(
            (
                p for p in all_projects
                if p.get("client_id") in selected_client_ids
                or (p.get("client_id") == INTERNAL_SUPPORT_CLIENT_ID and p.get("name") in selected_client_names)
            ),
            key=lambda p: (p.get("name") or "").lower()
        )

        if not client_projects:
            with filter_col2:
                st.info("No projects found for the selected clients.")
        else:
            project_options = {p["id"]: p["name"] for p in client_projects}
            with filter_col2:
                selected_project_ids = multiselect_with_controls(
                    "Project", "clients_tab_selected_project_ids", project_options.keys(), project_options,
                    searchable=True,
                )

            st.subheader("Most hours logged - by user")
            hours_by_user = load_hours_by_user_for_project(selected_project_ids, start_date, end_date)

            if hours_by_user.empty:
                st.info("No hours logged for the selected projects yet.")
            else:
                hours_by_user["hours"] = (hours_by_user["total_minutes"] / 60.0).round(1)
                st.dataframe(
                    hours_by_user[["user_name", "hours"]],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "user_name": "User",
                        "hours": "Hours",
                    },
                )

            project_tasks = [t for t in load_tasks() if t.get("project_id") in selected_project_ids]

            if not project_tasks:
                st.info("No tasks found for the selected projects.")
            else:
                pie_col1, pie_col2 = st.columns(2)

                with pie_col1:
                    billable_count = sum(1 for t in project_tasks if is_truthy(t.get("billable")))
                    non_billable_count = len(project_tasks) - billable_count
                    st.plotly_chart(
                        pie_chart(
                            ["Billable", "Non-billable"],
                            [billable_count, non_billable_count],
                            "Tasks by billable",
                        ),
                        use_container_width=True,
                        key="clients_tab_billable_pie",
                    )

                with pie_col2:
                    status_counts = {
                        status: sum(1 for t in project_tasks if t.get("status") == status)
                        for status in TASK_STATUSES
                    }
                    st.plotly_chart(
                        pie_chart(
                            list(status_counts.keys()),
                            list(status_counts.values()),
                            "Tasks by status",
                        ),
                        use_container_width=True,
                        key="clients_tab_status_pie",
                    )

with tab_users:
    users = load_users()

    if not users:
        st.info("No users found.")
    else:
        user_options = {u["id"]: u["name"] for u in users}
        filter_col, _ = st.columns([1, 3])
        with filter_col:
            selected_user_ids = multiselect_with_controls(
                "User", "users_tab_selected_user_ids", user_options.keys(), user_options, searchable=True
            )

        st.subheader("Most hours logged - by project")
        hours_by_project = load_hours_by_project_for_user(selected_user_ids, start_date, end_date)

        if hours_by_project.empty:
            st.info("No hours logged by the selected users yet.")
        else:
            hours_by_project["hours"] = (hours_by_project["total_minutes"] / 60.0).round(1)
            st.dataframe(
                hours_by_project[["project_name", "hours"]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "project_name": "Project",
                    "hours": "Hours",
                },
            )

        st.subheader("Hours over time")
        granularity = hours_granularity(start_date, end_date)
        hours_over_time = load_hours_over_time_for_user(selected_user_ids, start_date, end_date, granularity)

        if hours_over_time.empty:
            st.info("No hours logged by the selected users in the selected period.")
        else:
            hours_over_time["hours"] = hours_over_time["total_minutes"] / 60.0
            st.plotly_chart(
                hours_line_chart(
                    hours_over_time["period"],
                    hours_over_time["hours"],
                    granularity.capitalize(),
                    "Hours",
                ),
                use_container_width=True,
                key="users_tab_hours_over_time_chart",
            )

        user_task_ids = load_task_ids_for_user(selected_user_ids, start_date, end_date)
        user_tasks = [t for t in load_tasks() if t.get("id") in user_task_ids]

        if not user_tasks:
            st.info("No tasks found for the selected users.")
        else:
            pie_col, _ = st.columns(2)

            with pie_col:
                billable_count = sum(1 for t in user_tasks if is_truthy(t.get("billable")))
                non_billable_count = len(user_tasks) - billable_count
                st.plotly_chart(
                    pie_chart(
                        ["Billable", "Non-billable"],
                        [billable_count, non_billable_count],
                        "Tasks by billable",
                    ),
                    use_container_width=True,
                    key="users_tab_billable_pie",
                )

with tab_managers:
    st.header("Managers")
    st.caption(
        "Percentage of expected hours (Mon-Fri, 8h/day, up to yesterday) "
        "logged by each user in the selected period."
    )

    hours_by_user_all = load_hours_by_user_all(start_date, end_date)
    expected_minutes = expected_workday_minutes(start_date, end_date)

    if hours_by_user_all.empty:
        st.info("No users found.")
    elif expected_minutes == 0:
        st.info("The selected period has no completed workdays yet.")
    else:
        hours_by_user_all["hours"] = (hours_by_user_all["total_minutes"] / 60.0).round(1)
        hours_by_user_all["expected_hours"] = round(expected_minutes / 60.0, 1)
        hours_by_user_all["percent_logged"] = (
            hours_by_user_all["total_minutes"] / expected_minutes * 100
        ).round(1)
        hours_by_user_all = hours_by_user_all.sort_values("percent_logged")

        st.dataframe(
            hours_by_user_all[["user_name", "hours", "expected_hours", "percent_logged"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "user_name": "User",
                "hours": "Hours logged",
                "expected_hours": "Expected hours",
                "percent_logged": st.column_config.ProgressColumn(
                    "% logged", format="%.1f%%", min_value=0, max_value=100,
                ),
            },
        )

# with tab_support:
#     st.header("Internal Support")
#     st.caption(
#         "Support work is logged under the Internal Support bucket, with one project "
#         "per client. This gives an overview across all clients' support hours."
#     )
#
#     hours_by_client = load_support_hours_by_client(start_date, end_date)
#
#     if hours_by_client.empty:
#         st.info("No support hours logged in the selected period.")
#     else:
#         hours_by_client["hours"] = (hours_by_client["total_minutes"] / 60.0).round(1)
#
#         kpi1, kpi2 = st.columns(2)
#         kpi1.metric("Total support hours", f"{hours_by_client['hours'].sum():,.1f}")
#         kpi2.metric("Clients with support hours", len(hours_by_client))
#
#         st.subheader("Hours by client")
#         st.dataframe(
#             hours_by_client[["client_name", "hours"]],
#             use_container_width=True,
#             hide_index=True,
#             column_config={
#                 "client_name": "Client",
#                 "hours": "Hours",
#             },
#         )
#
#         st.subheader("Hours over time")
#         granularity = hours_granularity(start_date, end_date)
#         support_hours_over_time = load_support_hours_over_time(start_date, end_date, granularity)
#
#         if support_hours_over_time.empty:
#             st.info("No support hours logged in the selected period.")
#         else:
#             support_hours_over_time["hours"] = support_hours_over_time["total_minutes"] / 60.0
#             st.plotly_chart(
#                 hours_line_chart(
#                     support_hours_over_time["period"],
#                     support_hours_over_time["hours"],
#                     granularity.capitalize(),
#                     "Hours",
#                 ),
#                 use_container_width=True,
#             )
#
#     support_project_ids = {
#         p["id"] for p in load_projects()
#         if p.get("client_id") == INTERNAL_SUPPORT_CLIENT_ID
#     }
#     support_tasks = [t for t in load_tasks() if t.get("project_id") in support_project_ids]
#
#     if support_tasks:
#         st.subheader("Support tasks")
#         pie_col, _ = st.columns(2)
#
#         with pie_col:
#             billable_count = sum(1 for t in support_tasks if is_truthy(t.get("billable")))
#             non_billable_count = len(support_tasks) - billable_count
#             st.plotly_chart(
#                 pie_chart(
#                     ["Billable", "Non-billable"],
#                     [billable_count, non_billable_count],
#                     "Support tasks by billable",
#                 ),
#                 use_container_width=True,
#             )

with tab_raw:
    st.header("Raw data")
    st.caption("Every logged timetable entry, filterable by client, project and user (on top of the period above).")

    raw_clients = load_clients()
    raw_users = load_users()

    if not raw_clients or not raw_users:
        st.info("No data found.")
    else:
        raw_client_options = {c["id"]: c["name"] for c in raw_clients}
        raw_client_by_id = {c["id"]: c for c in raw_clients}
        raw_all_projects = load_projects()

        def raw_projects_for_client_ids(client_ids):
            names = {raw_client_by_id[cid]["name"] for cid in client_ids if cid in raw_client_by_id}
            return {
                p["id"] for p in raw_all_projects
                if p.get("client_id") in client_ids
                or (p.get("client_id") == INTERNAL_SUPPORT_CLIENT_ID and p.get("name") in names)
            }

        def on_raw_clients_committed(new_effective, old_effective):
            added = new_effective - old_effective
            removed = old_effective - new_effective
            if not added and not removed:
                return
            proj_committed_key = "raw_tab_selected_project_ids_committed"
            current = st.session_state.get(proj_committed_key)
            if current is None:
                return
            current = set(current)
            current |= raw_projects_for_client_ids(added)
            current -= raw_projects_for_client_ids(removed)
            st.session_state[proj_committed_key] = current

        filter_col1, filter_col2, filter_col3 = st.columns(3)

        with filter_col1:
            raw_selected_client_ids = multiselect_with_controls(
                "Client", "raw_tab_selected_client_ids", raw_client_options.keys(), raw_client_options,
                searchable=True, on_commit=on_raw_clients_committed,
            )
        raw_selected_clients = [c for c in raw_clients if c["id"] in raw_selected_client_ids]
        raw_selected_client_names = {c["name"] for c in raw_selected_clients}

        raw_projects = sorted(
            (
                p for p in raw_all_projects
                if p.get("client_id") in raw_selected_client_ids
                or (p.get("client_id") == INTERNAL_SUPPORT_CLIENT_ID and p.get("name") in raw_selected_client_names)
            ),
            key=lambda p: (p.get("name") or "").lower()
        )

        if not raw_projects:
            with filter_col2:
                st.info("No projects found for the selected clients.")
        else:
            raw_project_options = {p["id"]: p["name"] for p in raw_projects}
            with filter_col2:
                raw_selected_project_ids = multiselect_with_controls(
                    "Project", "raw_tab_selected_project_ids", raw_project_options.keys(), raw_project_options,
                    searchable=True,
                )

            raw_user_options = {u["id"]: u["name"] for u in raw_users}
            with filter_col3:
                raw_selected_user_ids = multiselect_with_controls(
                    "User", "raw_tab_selected_user_ids", raw_user_options.keys(), raw_user_options,
                    searchable=True,
                )

            raw_entries = load_raw_entries(
                raw_selected_project_ids, raw_selected_user_ids, start_date, end_date
            )

            if raw_entries.empty:
                st.info("No entries match the selected filters.")
            else:
                raw_entries["hours"] = (raw_entries["duration"] / 60.0).round(2)
                raw_entries["approved"] = raw_entries["approved"].apply(is_truthy)

                st.caption(f"{len(raw_entries)} entries")
                st.dataframe(
                    raw_entries[[
                        "date", "client_name", "project_name", "task_name", "user_name",
                        "hours", "approved", "internal_description", "external_description",
                    ]],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "date": "Date",
                        "client_name": "Client",
                        "project_name": "Project",
                        "task_name": "Task",
                        "user_name": "User",
                        "hours": "Hours",
                        "approved": "Approved",
                        "internal_description": "Internal description",
                        "external_description": "External description",
                    },
                )
