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

# =====================================================
# Config
# =====================================================

DW_NAME = "dw_3202"
SCHEMA = "ts_prod"

CLIENTS_TABLE = "clients"
PROJECTS_TABLE = "projects"
TASKS_TABLE = "tasks"
TASK_TAGS_TABLE = "task_tags"

st.set_page_config(
    page_title="Beheer Clients, Projects, Tasks en Task Tags",
    layout="wide",
)

# =====================================================
# Data helpers (elk 1x gecached)
# =====================================================

@st.cache_data(ttl=30)
def load_clients():
    dbconn = pq.dbconnect(DW_NAME)
    return sorted(
        dbconn.fetch(DW_NAME, SCHEMA, CLIENTS_TABLE) or [],
        key=lambda c: (c.get("name") or "").lower(),
    )

@st.cache_data(ttl=30)
def load_projects():
    dbconn = pq.dbconnect(DW_NAME)
    return sorted(
        dbconn.fetch(DW_NAME, SCHEMA, PROJECTS_TABLE) or [],
        key=lambda p: p.get("id"),
    )

@st.cache_data(ttl=30)
def load_tasks():
    dbconn = pq.dbconnect(DW_NAME)
    return sorted(
        dbconn.fetch(DW_NAME, SCHEMA, TASKS_TABLE) or [],
        key=lambda t: (t.get("name") or "").lower(),
    )

@st.cache_data(ttl=30)
def load_tags():
    dbconn = pq.dbconnect(DW_NAME)
    return sorted(
        dbconn.fetch(DW_NAME, SCHEMA, TASK_TAGS_TABLE) or [],
        key=lambda t: (t.get("name") or "").lower(),
    )

def clear_client_cache():
    load_clients.clear()

def clear_project_cache():
    load_projects.clear()

def clear_task_cache():
    load_tasks.clear()

def clear_tag_cache():
    load_tags.clear()

# =====================================================
# Database acties
# =====================================================

def create_client(name, peliqan_account_id, status):

    dbconn = pq.dbconnect(DW_NAME)

    dbconn.insert(
        DW_NAME,
        SCHEMA,
        CLIENTS_TABLE,
        {
            "name": name.strip(),
            "peliqan_account_id": peliqan_account_id.strip(),
            "status": status.strip()
        }
    )

    clear_client_cache()

def update_client(client_id, name, peliqan_account_id, status):

    dbconn = pq.dbconnect(DW_NAME)

    dbconn.update(
        DW_NAME,
        SCHEMA,
        CLIENTS_TABLE,
        client_id,
        {
            "name": name.strip(),
            "peliqan_account_id": peliqan_account_id.strip(),
            "status": status.strip()
        }
    )

    clear_client_cache()

def create_project(project_name, client_id, status, start_date, end_date):

    dbconn = pq.dbconnect(DW_NAME)

    dbconn.insert(
        DW_NAME,
        SCHEMA,
        PROJECTS_TABLE,
        {
            "name": project_name.strip(),
            "client_id": client_id,
            "status": status.strip(),
            "start_date": str(start_date),
            "end_date": str(end_date)
        }
    )

    clear_project_cache()

def update_project(project_id, values):

    dbconn = pq.dbconnect(DW_NAME)

    dbconn.update(
        DW_NAME,
        SCHEMA,
        PROJECTS_TABLE,
        project_id,
        values
    )

    clear_project_cache()

def create_task(values):

    dbconn = pq.dbconnect(DW_NAME)

    dbconn.insert(
        DW_NAME,
        SCHEMA,
        TASKS_TABLE,
        values
    )

    clear_task_cache()

def update_task(task_id, values):

    dbconn = pq.dbconnect(DW_NAME)

    dbconn.update(
        DW_NAME,
        SCHEMA,
        TASKS_TABLE,
        task_id,
        values
    )

    clear_task_cache()

def create_tag(name):

    dbconn = pq.dbconnect(DW_NAME)

    dbconn.insert(
        DW_NAME,
        SCHEMA,
        TASK_TAGS_TABLE,
        {
            "name": name.strip()
        }
    )

    clear_tag_cache()

def update_tag(tag_id, name):

    dbconn = pq.dbconnect(DW_NAME)

    dbconn.update(
        DW_NAME,
        SCHEMA,
        TASK_TAGS_TABLE,
        tag_id,
        {
            "name": name.strip()
        }
    )

    clear_tag_cache()


# =====================================================
# UI
# =====================================================

st.title("Beheer Clients, Projects, Tasks en Task Tags")

clients = load_clients()
projects = load_projects()
tasks = load_tasks()
tags = load_tags()

tab_clients, tab_projects, tab_tasks, tab_tags = st.tabs(
    [
        "Clients",
        "Projects",
        "Tasks",
        "Task Tags"
    ]
)


# =====================================================
# TAB 1: Clients
# =====================================================

with tab_clients:
    st.header("Clients beheren")
    st.dataframe(clients)


    with st.expander("Nieuwe client toevoegen"):
        name = st.text_input("Client naam", key="add_client_name")
        account_id = st.text_input("Peliqan account ID", key="add_client_account_id")
        status = st.text_input("Status", key="add_client_status")
        
        if st.button("Voeg client toe"):
            create_client(
                name,
                account_id,
                status
            )
            st.success("Client toegevoegd.")
            st.rerun()

    st.subheader("Bestaande client aanpassen")

    if clients:
        options = {
            c["name"]: c["id"]
            for c in clients
        }
        selected = st.selectbox(
            "Selecteer client",
            list(options.keys()),
            key="update_client_select"
        )
        client = next(
            c for c in clients
            if c["id"] == options[selected]
        )
        new_name = st.text_input(
            "Nieuwe naam",
            value=client["name"],
            key="update_client_name"
        )
        new_account = st.text_input(
            "Nieuwe Peliqan account ID",
            value=client.get("peliqan_account_id") or "",
            key="update_client_account_id"
        )
        new_status = st.text_input(
            "Nieuwe status",
            value=client.get("status") or "",
            key="update_client_status"
        )

        if st.button("Pas client aan"):
            update_client(
                client["id"],
                new_name,
                new_account,
                new_status
            )
            st.success("Client aangepast.")
            st.rerun()
            

# =====================================================
# TAB 2: Projects
# =====================================================

with tab_projects:
    st.header("Projects beheren")
    st.dataframe(projects)

    client_options = {
        c["name"]: c["id"]
        for c in clients
    }

    with st.expander("Nieuw project toevoegen"):
        project_name = st.text_input("Name", key="add_project_name")
        client_name = st.selectbox(
            "Client",
            list(client_options.keys()),
            key="add_project_client"
        )
        status = st.text_input("Status", key="add_project_status")
        start_date = st.date_input("Start datum", key="add_project_start_date")
        end_date = st.date_input("Eind datum", key="add_project_end_date")

        if st.button("Voeg project toe"):
            create_project(
                project_name,
                client_options[client_name],
                status,
                start_date,
                end_date
            )
            st.success("Project toegevoegd.")
            st.rerun()

    st.subheader("Bestaand project aanpassen")

    if projects:
        project_options = {
            p["id"]: p["id"]
            for p in projects
        }
        project_id = st.selectbox(
            "Selecteer project",
            list(project_options.keys()),
            key="update_project_select"
        )
        project = next(
            p for p in projects
            if p["id"] == project_id
        )
        new_name = st.text_input(
            "Name",
            value=project.get("name") or "",
            key="update_project_name"
        )
        new_status = st.text_input(
            "Status",
            value=project.get("status") or "",
            key="update_project_status"
        )
        
        if st.button("Pas project aan"):
            update_project(
                project_id,
                {
                    "name": new_name,
                    "status": new_status
                }
            )
            st.success("Project aangepast.")
            st.rerun()


# =====================================================
# TAB 3: Tasks
# =====================================================

with tab_tasks:
    st.header("Tasks beheren")
    st.dataframe(tasks)

    project_options = {
        p["name"]: p["id"]
        for p in projects
    }

    tag_options = {
        t["name"]: t["id"]
        for t in tags
    }

    with st.expander("Nieuwe task toevoegen"):
        name = st.text_input("Naam", key="add_task_name")
        description = st.text_area("Beschrijving", key="add_task_description")
        project = st.selectbox(
            "Project",
            list(project_options.keys()),
            key="add_task_project"
        )
        status = st.text_input("Status", key="add_task_status")
        tag = st.selectbox(
            "Tag",
            list(tag_options.keys()),
            key="add_task_tag"
        )

        if st.button("Voeg task toe"):
            create_task(
                {
                    "name": name,
                    "description": description,
                    "project_id": project_options[project],
                    "status": status,
                    "tag_id": tag_options[tag]
                }
            )
            st.success("Task toegevoegd.")
            st.rerun()

    st.subheader("Bestaande task aanpassen")

    if tasks:
        task_options = {
            t["name"]: t["id"]
            for t in tasks
        }
        selected = st.selectbox(
            "Selecteer task",
            list(task_options.keys()),
            key="update_task_select"
        )
        task = next(
            t for t in tasks
            if t["id"] == task_options[selected]
        )
        new_status = st.text_input(
            "Status",
            value=task.get("status") or "",
            key="update_task_status"
        )

        if st.button("Pas task aan"):
            update_task(
                task["id"],
                {
                    "status": new_status
                }
            )
            st.success("Task aangepast.")
            st.rerun()


# =====================================================
# TAB 4: Task Tags
# =====================================================

with tab_tags:
    st.header("Task Tags beheren")
    st.dataframe(tags)

    with st.expander("Nieuwe tag toevoegen"):
        name = st.text_input("Tag naam", key="add_tag_name")
        
        if st.button("Voeg tag toe"):
            create_tag(name)
            st.success("Tag toegevoegd.")
            st.rerun()

    st.subheader("Bestaande tag aanpassen")

    if tags:
        tag_options = {
            t["name"]: t["id"]
            for t in tags
        }
        selected = st.selectbox(
            "Selecteer tag",
            list(tag_options.keys()),
            key="update_tag_select"
        )
        tag = next(
            t for t in tags
            if t["id"] == tag_options[selected]
        )
        new_name = st.text_input(
            "Nieuwe naam",
            value=tag["name"],
            key="update_tag_name"
        )

        if st.button("Pas tag aan"):
            update_tag(
                tag["id"],
                new_name
            )
            st.success("Tag aangepast.")
            st.rerun()
            