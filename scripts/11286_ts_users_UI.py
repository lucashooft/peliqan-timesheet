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

print("test")

DW_NAME = "dw_3202"
SCHEMA = "ts_prod"

TEAMS_TABLE = "teams"
USERS_TABLE = "users"
ROLES_TABLE = "user_roles"

st.set_page_config(
    page_title="Beheer Teams, Users en Roles",
    layout="wide"
)

# =====================================================
# Data helpers (gecached zoals API key app)
# =====================================================

@st.cache_data(ttl=30)
def load_teams():
    dbconn = pq.dbconnect(DW_NAME)
    return sorted(
        dbconn.fetch(DW_NAME, SCHEMA, TEAMS_TABLE) or [],
        key=lambda t: (t.get("name") or "").lower()
    )

@st.cache_data(ttl=30)
def load_users():
    dbconn = pq.dbconnect(DW_NAME)
    return sorted(
        dbconn.fetch(DW_NAME, SCHEMA, USERS_TABLE) or [],
        key=lambda u: (u.get("name") or "").lower()
    )

@st.cache_data(ttl=30)
def load_roles():
    dbconn = pq.dbconnect(DW_NAME)
    return sorted(
        dbconn.fetch(DW_NAME, SCHEMA, ROLES_TABLE) or [],
        key=lambda r: (r.get("name") or "").lower()
    )

def clear_team_cache():
    load_teams.clear()

def clear_user_cache():
    load_users.clear()

def clear_role_cache():
    load_roles.clear()


# =====================================================
# Database acties
# =====================================================

def create_team(name: str):
    dbconn = pq.dbconnect(DW_NAME)
    dbconn.insert(
        DW_NAME,
        SCHEMA,
        TEAMS_TABLE,
        {
            "name": name.strip()
        }
    )
    clear_team_cache()

def update_team(team_id: int, name: str):
    dbconn = pq.dbconnect(DW_NAME)
    dbconn.update(
        DW_NAME,
        SCHEMA,
        TEAMS_TABLE,
        team_id,
        {
            "name": name.strip()
        }
    )
    clear_team_cache()

def create_user(name: str, email: str, role_id: int, team_id: int):
    dbconn = pq.dbconnect(DW_NAME)
    dbconn.insert(
        DW_NAME,
        SCHEMA,
        USERS_TABLE,
        {
            "name": name.strip(),
            "email": email.strip(),
            "role_id": role_id,
            "team_id": team_id
        }
    )
    clear_user_cache()

def update_user(
    user_id: int,
    name: str,
    email: str,
    role_id: int,
    team_id: int
):
    dbconn = pq.dbconnect(DW_NAME)
    dbconn.update(
        DW_NAME,
        SCHEMA,
        USERS_TABLE,
        user_id,
        {
            "name": name.strip(),
            "email": email.strip(),
            "role_id": role_id,
            "team_id": team_id
        }
    )
    clear_user_cache()

def create_role(name: str):
    dbconn = pq.dbconnect(DW_NAME)
    dbconn.insert(
        DW_NAME,
        SCHEMA,
        ROLES_TABLE,
        {
            "name": name.strip()
        }
    )
    clear_role_cache()

def update_role(role_id: int, name: str):
    dbconn = pq.dbconnect(DW_NAME)
    dbconn.update(
        DW_NAME,
        SCHEMA,
        ROLES_TABLE,
        role_id,
        {
            "name": name.strip()
        }
    )
    clear_role_cache()

# =====================================================
# UI
# =====================================================

st.title("Beheer Teams, Users en User Roles")

teams = load_teams()
users = load_users()
roles = load_roles()

tab_teams, tab_users, tab_roles = st.tabs(
    [
        "Teams",
        "Users",
        "User Roles"
    ]
)

# =====================================================
# TAB 1: Teams beheren
# =====================================================

with tab_teams:
    st.header("Teams beheren")

    st.dataframe(teams)

    # -----------------------------
    # Nieuw team
    # -----------------------------

    with st.expander("Nieuw team toevoegen"):
        team_name = st.text_input(
            "Team naam",
            key="new_team_name"
        )

        if st.button("Voeg team toe"):
            if not team_name.strip():
                st.error("Team naam mag niet leeg zijn.")
            else:
                create_team(team_name)
                st.success("Nieuw team toegevoegd.")
                st.rerun()


    # -----------------------------
    # Team aanpassen
    # -----------------------------

    st.subheader("Bestaand team aanpassen")

    if teams:
        team_options = {
            t["name"]: t["id"]
            for t in teams
        }

        selected_team = st.selectbox(
            "Selecteer team",
            list(team_options.keys())
        )

        team_id = team_options[selected_team]

        team = next(
            t for t in teams
            if t["id"] == team_id
        )

        new_name = st.text_input(
            "Nieuwe team naam",
            value=team["name"],
            key="edit_team"
        )

        if st.button("Pas team aan"):
            update_team(team_id, new_name)
            st.success("Team aangepast.")
            st.rerun()


# =====================================================
# TAB 2: Users beheren
# =====================================================

with tab_users:
    st.header("Users beheren")

    st.dataframe(users)

    st.subheader("User aanpassen")

    if users:
        user_options = {
            f"{u['name']} ({u['email']})": u["id"]
            for u in users
        }

        selected_user = st.selectbox(
            "Selecteer user",
            list(user_options.keys())
        )

        user_id = user_options[selected_user]

        user = next(
            u for u in users
            if u["id"] == user_id
        )

        role_options = {
            r["name"]: r["id"]
            for r in roles
        }

        team_options = {
            t["name"]: t["id"]
            for t in teams
        }

        current_role = next(
            (
                name
                for name, rid in role_options.items()
                if rid == user["role_id"]
            ),
            None
        )

        current_team = next(
            (
                name
                for name, tid in team_options.items()
                if tid == user["team_id"]
            ),
            None
        )

        new_name = st.text_input(
            "Naam",
            value=user["name"]
        )

        new_email = st.text_input(
            "Email",
            value=user["email"]
        )

        new_role = st.selectbox(
            "Rol",
            list(role_options.keys()),
            index=list(role_options.keys()).index(current_role)
            if current_role else 0
        )

        new_team = st.selectbox(
            "Team",
            list(team_options.keys()),
            index=list(team_options.keys()).index(current_team)
            if current_team else 0
        )

        if st.button("Pas user aan"):
            update_user(
                user_id,
                new_name,
                new_email,
                role_options[new_role],
                team_options[new_team]
            )
            st.success("User aangepast.")
            st.rerun()



# =====================================================
# TAB 3: User Roles beheren
# =====================================================

with tab_roles:
    st.header("User Roles beheren")
    st.dataframe(roles)

    
    # -----------------------------
    # Nieuwe role
    # -----------------------------

    with st.expander("Nieuwe user role toevoegen"):
        role_name = st.text_input(
            "Role naam",
            key="new_role_name"
        )

        if st.button("Voeg role toe"):
            if not role_name.strip():
                st.error("Role naam mag niet leeg zijn.")
            else:
                create_role(role_name)
                st.success("Nieuwe user role toegevoegd.")
                st.rerun()
                

    # -----------------------------
    # Role aanpassen
    # -----------------------------

    st.subheader("Bestaande role aanpassen")

    if roles:
        role_options = {
            r["name"]: r["id"]
            for r in roles
        }

        selected_role = st.selectbox(
            "Selecteer role",
            list(role_options.keys())
        )

        role_id = role_options[selected_role]

        role = next(
            r for r in roles
            if r["id"] == role_id
        )

        new_name = st.text_input(
            "Nieuwe role naam",
            value=role["name"],
            key="edit_role"
        )

        if st.button("Pas role aan"):
            update_role(role_id, new_name)
            st.success("Role aangepast.")
            st.rerun()