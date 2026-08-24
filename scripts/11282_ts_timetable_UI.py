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
import pandas as pd
from datetime import date, datetime

# Peliqan settings
DW_NAME = "dw_3202"
SCHEMA = "ts_prod"

USERS_TABLE = "users"
TASKS_TABLE = "tasks"
TIMETABLE_TABLE = "timetable"

USER_NAME_FIELD = "name"

st.set_page_config(
    page_title="Timetable Entry",
    layout="wide"
)


# =====================================================
# Helpers
# =====================================================

@st.cache_data(ttl=60)
def load_users():
    dbconn = pq.dbconnect(DW_NAME)
    return dbconn.fetch(DW_NAME, SCHEMA, USERS_TABLE) or []


@st.cache_data(ttl=60)
def load_tasks():
    dbconn = pq.dbconnect(DW_NAME)
    return dbconn.fetch(DW_NAME, SCHEMA, TASKS_TABLE) or []


@st.cache_data(ttl=60)
def load_timetable():
    dbconn = pq.dbconnect(DW_NAME)
    return dbconn.fetch(DW_NAME, SCHEMA, TIMETABLE_TABLE) or []


def clear_cache():
    load_users.clear()
    load_tasks.clear()
    load_timetable.clear()


def user_label(user):
    name = (
        user.get(USER_NAME_FIELD)
        or user.get("email")
        or f"user #{user.get('id')}"
    )
    return f"{name} (id {user.get('id')})"


def task_label(task):
    return f"{task.get('name', 'task')} (id {task.get('id')})"


def create_timetable_dataframe():

    timetable = load_timetable()
    users = load_users()
    tasks = load_tasks()

    if not timetable:
        return pd.DataFrame()

    timetable_df = pd.DataFrame(timetable)
    users_df = pd.DataFrame(users)
    tasks_df = pd.DataFrame(tasks)

    # Zorg dat de join keys hetzelfde datatype hebben
    if "user_id" in timetable_df.columns:
        timetable_df["user_id"] = pd.to_numeric(
            timetable_df["user_id"],
            errors="coerce"
        ).astype("Int64")

    if "task_id" in timetable_df.columns:
        timetable_df["task_id"] = pd.to_numeric(
            timetable_df["task_id"],
            errors="coerce"
        ).astype("Int64")

    if not users_df.empty:
        users_df["id"] = pd.to_numeric(
            users_df["id"],
            errors="coerce"
        ).astype("Int64")

        users_df = users_df.rename(
            columns={
                "id": "user_id",
                "email": "user_email",
                "name": "user_name",
            }
        )

        timetable_df = timetable_df.merge(
            users_df[["user_id", "user_email", "user_name"]],
            on="user_id",
            how="left",
        )

    if not tasks_df.empty:
        tasks_df["id"] = pd.to_numeric(
            tasks_df["id"],
            errors="coerce"
        ).astype("Int64")

        tasks_df = tasks_df.rename(
            columns={
                "id": "task_id",
                "name": "task_name",
            }
        )

        timetable_df = timetable_df.merge(
            tasks_df[["task_id", "task_name"]],
            on="task_id",
            how="left",
        )

    return timetable_df


# =====================================================
# UI
# =====================================================

st.title("Timesheet Management")

tab_entry, tab_overview = st.tabs(
    [
        "Nieuwe entry",
        "Overzicht"
    ]
)


# =====================================================
# TAB 1 - Nieuwe entry
# =====================================================

with tab_entry:

    st.header("Nieuwe Timetable Entry")

    users = load_users()
    tasks = load_tasks()

    if not users:
        st.warning(
            "Geen users gevonden. Controleer de tabel en kolomnamen."
        )

    if not tasks:
        st.warning(
            "Geen tasks gevonden."
        )

    user_map = {
        u.get("id"): u
        for u in users
    }

    task_map = {
        t.get("id"): t
        for t in tasks
    }


    with st.form(
        "new_timetable_entry",
        clear_on_submit=True
    ):

        col1, col2 = st.columns(2)


        with col1:

            user_choice = (
                st.selectbox(
                    "Gebruiker",
                    options=[
                        u.get("id")
                        for u in users
                    ],
                    format_func=lambda uid:
                        user_label(
                            user_map.get(
                                uid,
                                {"id": uid}
                            )
                        )
                )
                if users
                else None
            )


            task_choice = (
                st.selectbox(
                    "Task",
                    options=[
                        t.get("id")
                        for t in tasks
                    ],
                    format_func=lambda tid:
                        task_label(
                            task_map.get(
                                tid,
                                {"id": tid}
                            )
                        )
                )
                if tasks
                else None
            )


            entry_date = st.date_input(
                "Datum",
                value=date.today()
            )


        with col2:

            duration = st.number_input(
                "Duur (minuten)",
                min_value=1,
                step=15,
                value=60
            )


            billable = st.checkbox(
                "Billable",
                value=True
            )


        internal_description = st.text_area(
            "Interne beschrijving"
        )


        external_description = st.text_area(
            "Externe beschrijving (klant)"
        )


        submitted = st.form_submit_button(
            "Opslaan"
        )


        if submitted:

            errors = []

            if not users:
                errors.append(
                    "Geen gebruikers beschikbaar."
                )

            if not tasks:
                errors.append(
                    "Geen taken beschikbaar."
                )

            if duration <= 0:
                errors.append(
                    "Duur moet groter zijn dan 0."
                )


            if errors:

                for error in errors:
                    st.error(error)

            else:

                dbconn = pq.dbconnect(
                    DW_NAME
                )


                new_entry = {

                    "user_id": user_choice,

                    "task_id": task_choice,

                    "date":
                        entry_date.strftime(
                            "%Y-%m-%d"
                        ),

                    "duration":
                        int(duration),

                    "billable":
                        bool(billable),

                    "internal_description":
                        internal_description.strip(),

                    "external_description":
                        external_description.strip(),

                    "date_inserted":
                        datetime.utcnow().strftime(
                            "%Y-%m-%d"
                        ),

                    "approved":
                        None,

                    "approved_by":
                        None
                }


                dbconn.insert(
                    DW_NAME,
                    SCHEMA,
                    TIMETABLE_TABLE,
                    new_entry
                )


                clear_cache()


                st.success(
                    f"Entry toegevoegd voor "
                    f"{user_label(user_map[user_choice])} "
                    f"op {entry_date.strftime('%d-%m-%Y')} "
                    f"({duration} min)."
                )



# =====================================================
# TAB 2 - Overzicht
# =====================================================

with tab_overview:

    st.header("Timetable overzicht")


    df = create_timetable_dataframe()


    if df.empty:

        st.info(
            "Nog geen timetable entries gevonden."
        )

    else:


        col1, col2 = st.columns(2)


        with col1:

            users_filter = [
                "Alle"
            ] + sorted(
                df["user_email"]
                .dropna()
                .unique()
                .tolist()
            )


            selected_user = st.selectbox(
                "Filter op gebruiker",
                users_filter
            )


        with col2:

            tasks_filter = [
                "Alle"
            ] + sorted(
                df["task_name"]
                .dropna()
                .unique()
                .tolist()
            )


            selected_task = st.selectbox(
                "Filter op task",
                tasks_filter
            )


        filtered_df = df.copy()


        if selected_user != "Alle":

            filtered_df = filtered_df[
                filtered_df["user_email"]
                == selected_user
            ]


        if selected_task != "Alle":

            filtered_df = filtered_df[
                filtered_df["task_name"]
                == selected_task
            ]


        st.write(
            f"{len(filtered_df)} entries gevonden"
        )


        st.dataframe(
            filtered_df,
            use_container_width=True,
            hide_index=True
        )