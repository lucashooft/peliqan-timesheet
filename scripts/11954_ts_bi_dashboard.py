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

Access: Google login required, same flow and same Google Cloud OAuth
client (client_id/client_secret) as ts_my_week (12011), but gated on
ts_prod.users.scope >= 2 (manager/admin) instead of any user row - this
dashboard has no per-viewer filtering, so an employee-scope login would
see and edit everyone's data. See the OAuth block below for why the flow
is duplicated rather than shared.
"""

import base64
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

import streamlit as st
import pandas as pd
from datetime import date, datetime, timezone

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


# =====================================================
# Google login (OAuth 2.0 authorization code flow)
# =====================================================
#
# Same flow as ts_my_week (12011_ts_my_week.py) - duplicated here because
# Peliqan apps cannot import each other; keep the two in step if the flow
# ever changes. Uses the SAME Google Cloud OAuth client as ts_my_week
# (same client_id / client_secret Secret Store entries), but this app's
# own redirect_uri, which must be registered as an extra "Authorized
# redirect URI" on that same Google Cloud client. A distinct cookie
# prefix keeps this app's session cookie from colliding with ts_my_week's.
#
# Difference from ts_my_week: access requires scope >= 2, not just any
# row in ts_prod.users.

MANAGE_SCOPE = 2

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# Must match an "Authorized redirect URI" on the Google Cloud OAuth client
# byte for byte, trailing slash included - the same client ts_my_week uses.
PUBLISHED_APP_URL = (
    "https://app.eu.peliqan.io/apps/"
    "RVRoRjhXR2FVVUpRMmZDZExrTk1qeXJDd1FmbUR1Q01QNk5QTDd2dzFtc0VYQWNwelM4THVtZzJuSnQyR25lWA==/"
)
# For local `streamlit run` development, point this at http://localhost:8501/
# and register that as another redirect URI on the same Google client.
REDIRECT_URI = PUBLISHED_APP_URL

SECRET_CLIENT_ID = "google_login_client_id"
SECRET_CLIENT_SECRET = "google_login_client_secret"
SECRET_COOKIE_PASSWORD = "ts_cookie_password"

SESSION_HOURS = 12            # how long one login stays valid
STATE_MAX_AGE = 30 * 60       # a started login must be completed within this
COOKIE_PREFIX = "ts_bi_dashboard_"
COOKIE_SESSION = "session"


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


@st.cache_data(ttl=300)
def load_users():
    dbconn = pq.dbconnect(DW_NAME)
    rows = dbconn.fetch(DW_NAME, S, "users") or []
    return [r for r in rows if to_int(r.get("id")) is not None]


class LoginError(Exception):
    """Anything that must send the visitor back to the login page."""


class StaleCodeError(LoginError):
    """The authorization code was already redeemed, or it expired."""


def now_ts():
    return int(datetime.now(timezone.utc).timestamp())


@st.cache_data(ttl=3600)
def oauth_config():
    return {
        "client_id": pq.get_secret(SECRET_CLIENT_ID),
        "client_secret": pq.get_secret(SECRET_CLIENT_SECRET),
        "cookie_password": pq.get_secret(SECRET_COOKIE_PASSWORD),
    }


def b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64url_decode(txt):
    return base64.urlsafe_b64decode(str(txt) + "=" * (-len(str(txt)) % 4))


def sign(payload):
    key = oauth_config()["cookie_password"].encode()
    return b64url_encode(hmac.new(key, payload.encode(), hashlib.sha256).digest())


def make_state():
    rand = secrets.token_urlsafe(16)
    body = b64url_encode(json.dumps({"r": rand, "t": now_ts()}).encode())
    return body + "." + sign(body), rand


def read_state(state):
    try:
        body, mac = str(state).split(".", 1)
    except (ValueError, AttributeError):
        raise LoginError("The login response carried no state - please sign in again.")
    if not hmac.compare_digest(mac, sign(body)):
        raise LoginError("The login response did not belong to a login started here.")
    try:
        data = json.loads(b64url_decode(body))
    except Exception:
        raise LoginError("The login state was unreadable - please sign in again.")
    if now_ts() - int(data.get("t") or 0) > STATE_MAX_AGE:
        raise LoginError("This login took too long to complete - please sign in again.")
    return str(data.get("r") or "")


def nonce_for(state_rand):
    return sign("nonce|" + state_rand)


def build_auth_url(state, state_rand):
    return GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": oauth_config()["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce_for(state_rand),
        "access_type": "online",
        "prompt": "select_account",
    })


def exchange_code(code):
    cfg = oauth_config()
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        GOOGLE_TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        if detail == "invalid_grant":
            raise StaleCodeError("This authorization code was already used or has expired.")
        raise LoginError(
            f"Google refused this login ({detail or exc.code}). Usually the "
            "redirect URI or the client secret does not match the Google Cloud client."
        )
    except Exception:
        raise LoginError("Could not reach Google to finish the login - please try again.")


def read_identity(tokens, expected_nonce):
    """Signature not re-checked: the token arrived straight from Google's
    token endpoint over TLS, in response to a request carrying our client
    secret - the one case Google documents as not requiring it."""
    id_token = tokens.get("id_token")
    if not id_token:
        raise LoginError("Google returned no identity token.")
    try:
        claims = json.loads(b64url_decode(id_token.split(".")[1]))
    except Exception:
        raise LoginError("The identity token from Google was unreadable.")

    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise LoginError("That identity token was not issued by Google.")
    if claims.get("aud") != oauth_config()["client_id"]:
        raise LoginError("That identity token was issued for a different application.")
    if int(claims.get("exp") or 0) <= now_ts():
        raise LoginError("That identity token had already expired.")
    if expected_nonce and claims.get("nonce") != expected_nonce:
        raise LoginError("That identity token does not match this login attempt.")
    if not is_true(claims.get("email_verified")):
        raise LoginError("This Google account has no verified email address.")

    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise LoginError("Google returned no email address for this account.")
    return {"email": email, "name": claims.get("name") or email}


def init_cookies():
    if st.session_state.get("cookies_unavailable"):
        return None
    try:
        st.cache = st.cache_data     # streamlit_cookies_manager still expects st.cache
        from streamlit_cookies_manager import EncryptedCookieManager
        return EncryptedCookieManager(prefix=COOKIE_PREFIX,
                                      password=oauth_config()["cookie_password"])
    except Exception:
        st.session_state.cookies_unavailable = True
        return None


def store_session(cookies, email, name):
    auth = {"email": email, "name": name, "exp": now_ts() + SESSION_HOURS * 3600}
    st.session_state.auth = auth
    if cookies is not None:
        body = b64url_encode(json.dumps(auth).encode())
        try:
            cookies[COOKIE_SESSION] = body + "." + sign(body)
            cookies.save()
        except Exception:
            pass
    return auth


def read_session(cookies):
    auth = st.session_state.get("auth")
    if not auth and cookies is not None:
        raw = cookies.get(COOKIE_SESSION)
        if raw:
            try:
                body, mac = str(raw).split(".", 1)
                if hmac.compare_digest(mac, sign(body)):
                    auth = json.loads(b64url_decode(body))
            except Exception:
                auth = None
    if not auth or int(auth.get("exp") or 0) <= now_ts():
        return None
    st.session_state.auth = auth
    return auth


def end_session(cookies):
    st.session_state.pop("auth", None)
    if cookies is not None:
        try:
            cookies[COOKIE_SESSION] = ""
            cookies.save()
        except Exception:
            pass


def login_page(message=None):
    """Render the sign-in page and stop the script - never returns."""
    stashed = st.session_state.pop("login_message", None)
    message = message or stashed
    state, state_rand = make_state()
    st.title("Project BI Dashboard")
    if message:
        st.error(message)
    st.write("Sign in with your Google timesheet account. This dashboard "
             "is limited to managers and admins.")
    st.markdown(
        f"<a href='{build_auth_url(state, state_rand)}' target='_top' "
        "style='display:inline-block;padding:0.55rem 1.1rem;border-radius:0.5rem;"
        "background:#053763;color:#fff;text-decoration:none;font-weight:600;'>"
        "Sign in with Google</a>",
        unsafe_allow_html=True,
    )
    if st.session_state.get("cookies_unavailable"):
        st.caption("Cookies are unavailable here, so a page refresh will ask you to sign in again.")
    st.stop()


GOOGLE_PARAMS = ("code", "state", "scope", "authuser", "prompt", "hd", "error", "error_subtype")


def clear_login_params():
    for key in GOOGLE_PARAMS:
        if key in st.query_params:
            del st.query_params[key]


def match_login_to_user(email):
    for u in load_users():
        if str(u.get("email") or "").strip().lower() == email:
            return u
    return None

# ---- the gate: nothing below here runs unauthenticated or under scope ----

try:
    oauth_config()
except Exception:
    st.error(
        "Google login is not configured yet. Add these three entries to the "
        f"Peliqan Secret Store: {SECRET_CLIENT_ID}, {SECRET_CLIENT_SECRET} "
        f"and {SECRET_COOKIE_PASSWORD}."
    )
    st.stop()

cookies = init_cookies()
if cookies is not None and not cookies.ready():
    st.stop()                    # cookie component still initialising

auth = read_session(cookies)

if auth is None:
    params = st.query_params
    if params.get("error"):
        denied = f"Google did not complete the login ({str(params.get('error'))})."
        st.session_state.login_message = denied
        clear_login_params()
        login_page(denied)
    code = params.get("code")
    if not code:
        login_page()
    consumed = st.session_state.setdefault("consumed_codes", set())
    if str(code) in consumed:
        clear_login_params()
        login_page()
    consumed.add(str(code))
    try:
        state_rand = read_state(params.get("state"))
        identity = read_identity(exchange_code(code), nonce_for(state_rand))
    except StaleCodeError:
        clear_login_params()
        login_page()
    except LoginError as exc:
        st.session_state.login_message = str(exc)
        clear_login_params()
        login_page(str(exc))
    auth = store_session(cookies, identity["email"], identity["name"])
    clear_login_params()
    st.rerun()

elif "code" in st.query_params:
    clear_login_params()    # already signed in: drop a stale code from the URL

login_user = match_login_to_user(auth["email"])
if login_user is None:
    st.title("Project BI Dashboard")
    st.error(
        f"{auth['email']} is not a timesheet user. Ask an administrator to add this "
        "email address to the users table, or sign in with another account."
    )
    if st.button("Sign in with another account", key="switch_account"):
        end_session(cookies)
        st.rerun()
    st.stop()

if (to_int(login_user.get("scope")) or 1) < MANAGE_SCOPE:
    st.title("Project BI Dashboard")
    st.error("This dashboard is limited to managers and admins. Ask an "
             "administrator for a scope upgrade if you believe you should "
             "have access.")
    if st.button("Sign in with another account", key="switch_account"):
        end_session(cookies)
        st.rerun()
    st.stop()

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
