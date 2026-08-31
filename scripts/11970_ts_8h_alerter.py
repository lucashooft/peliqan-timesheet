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
Timesheet daily-shortfall alert ("ts_8h_alerter")
--------------------------------------------------
Tracks every weekday, from FIXED_START_DATE onward, where a monitored user
logged less than DAILY_THRESHOLD_MINUTES (default: 8h = 480 min) in
ts_reporting.fact_timetable - UNLESS every entry logged that day is already
validated (the approved column is true for all of that day's entries), in which case it's
treated as signed off and skipped.

Two notifications per run:
1. Each employee with outstanding shortfall days gets a Slack DM listing
   every one of their non-8h, not-fully-validated days since FIXED_START_DATE.
2. If run on a Monday, every "team lead" (ts_prod.users.scope == TEAM_LEAD_SCOPE)
   additionally gets a Slack DM with the full team-wide overview.

Monitored users / team leads are both derived dynamically from ts_prod.users:
anyone with a non-null name, email AND scope (this excludes placeholder
"role" rows like "Admin (Scope 3)" which have no email). NOTIFY_ONLY narrows
that to a named few; EXCLUDE drops individuals and is applied last.

Slack DMs are sent as {"channel": "@<handle>"}, where the handle is the
local part of the person's ts_prod.users.email - lucas@peliqan.io becomes
@lucas. That holds for everyone, so there is no override table. A DM to a
handle Slack does not know fails the way a bad channel name does, which is
silently from this script's point of view; the run prints every resolved
handle at the start and each send result after, so check those.

NOTE: ts_prod.users.name is the join key against fact_timetable.user_name and
the key of every per-user dict here, so two people sharing a name would be
merged into one. Nothing enforces that; the email is the unique column.

Deploy note: this script checks "is today Monday" itself for the team-lead
digest, but the actual run frequency (daily vs weekly) is controlled by the
schedule configured for this data-app in Peliqan - that's unchanged by this
script edit.
"""

import time
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Configuration - edit these for your setup
# ---------------------------------------------------------------------------

# ts_prod.users.scope value that identifies a "team lead" for the Monday
# digest.
TEAM_LEAD_SCOPE = 2

# Restrict monitoring + all Slack DMs to only these people (matched against
# ts_prod.users.name). None means the whole roster, which is the point of
# the roster query below - set a list here only to pilot a change on a few
# people first.
NOTIFY_ONLY = None

# Never monitored and never DM'd, whatever the roster or NOTIFY_ONLY say -
# applied last, so it always wins. A name or an email address both work,
# matched case-insensitively: "Niko" and "niko@peliqan.io" exclude the same
# person. Use the email where two people could share a first name.
EXCLUDE = ["arthur@peliqan.io"]


# name -> email, filled from the roster query. Read by slack_handle_for.
EMAIL_BY_NAME = {}

# Only shortfall days on/after this date are tracked/notified.
FIXED_START_DATE = date(2026, 9, 1)
SLACK_CONNECTION_NAME = "Slack"
WORKDAY_MINUTES = 480
DAILY_THRESHOLD_MINUTES = WORKDAY_MINUTES
WORKDAYS_TO_CHECK = [0, 1, 2, 3, 4]
MAX_QUERY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5


def fetch_with_retry(dbconn, query, label):
    """Run a DB query with retry-with-backoff on transient connection errors."""
    for attempt in range(1, MAX_QUERY_ATTEMPTS + 1):
        try:
            return dbconn.fetch(pq.DW_NAME, query=query)
        except Exception as exc:
            print(f"{label} attempt {attempt}/{MAX_QUERY_ATTEMPTS} failed: {exc}")
            if attempt == MAX_QUERY_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS)


def slack_handle_for(name):
    """
    The Slack username to DM this person.

    The local part of their email - lucas@peliqan.io gives "lucas" - which
    is how every handle here is set. Unlike the display name it survives
    capitals, spaces and hyphens; "Piet-Michiel" was never going to resolve.

    The roster query requires a non-null email, so anyone reaching this has
    one. A blank result would send to "@", which the startup line prints as
    an empty handle rather than hiding it.
    """
    return str(EMAIL_BY_NAME.get(name) or "").split("@")[0].strip()


def format_shortfall_line(day, minutes):
    """e.g. 'Mon 2026-08-10: 8.0 hours short (need 8.0 more hours)'
    or, for small gaps, 'Mon 2026-08-10: 12 minutes short'."""
    deficit = DAILY_THRESHOLD_MINUTES - minutes
    if deficit > 60:
        hours = round(deficit / 60, 1)
        detail = f"{hours} hours short"
    else:
        detail = f"{deficit} minutes short"
    return f"\u2022 {day.strftime('%a')} {day.isoformat()}: {detail}"


def daterange(start, end_exclusive):
    d = start
    while d < end_exclusive:
        yield d
        d += timedelta(days=1)


# ---------------------------------------------------------------------------
# Compute tracking window: FIXED_START_DATE -> yesterday (last fully-
# completed day), filtered to WORKDAYS_TO_CHECK.
# ---------------------------------------------------------------------------

today = date.today()
window_end_exclusive = today  # entries for "today" aren't final yet

days_to_check = [
    d for d in daterange(FIXED_START_DATE, window_end_exclusive)
    if d.weekday() in WORKDAYS_TO_CHECK
]

window_start_str = FIXED_START_DATE.isoformat()
window_end_exclusive_str = window_end_exclusive.isoformat()

print(f"Tracking window: {window_start_str} through {(window_end_exclusive - timedelta(days=1)).isoformat()}")
print(f"Weekdays checked in window: {len(days_to_check)}")

if not days_to_check:
    print("Tracking window contains no checkable days yet - nothing to do.")
else:
    dbconn = pq.dbconnect(pq.DW_NAME)

    # -------------------------------------------------------------------
    # Roster: anyone with name + email + scope set in ts_prod.users.
    # Excludes placeholder "role" rows which have no email.
    # -------------------------------------------------------------------

    roster_query = """
    SELECT name, email, scope
    FROM ts_prod.users
    WHERE name IS NOT NULL
      AND email IS NOT NULL
      AND scope IS NOT NULL
    """
    roster_rows = fetch_with_retry(dbconn, roster_query, "Roster query")

    EMAIL_BY_NAME.update({row["name"]: row["email"] for row in roster_rows})

    USER_LIST = [row["name"] for row in roster_rows]
    TEAM_LEADS = [row["name"] for row in roster_rows if row["scope"] >= TEAM_LEAD_SCOPE]

    if NOTIFY_ONLY is not None:
        USER_LIST = [u for u in USER_LIST if u in NOTIFY_ONLY]
        TEAM_LEADS = [u for u in TEAM_LEADS if u in NOTIFY_ONLY]

    if EXCLUDE:
        excluded = {str(x).strip().lower() for x in EXCLUDE if str(x).strip()}

        def is_excluded(user_name):
            """Match on either the name or the email, so a list of addresses
            and a list of names both behave the way someone would expect."""
            return (user_name.strip().lower() in excluded
                    or str(EMAIL_BY_NAME.get(user_name) or "").strip().lower()
                    in excluded)

        skipped = [u for u in USER_LIST if is_excluded(u)]
        USER_LIST = [u for u in USER_LIST if not is_excluded(u)]
        TEAM_LEADS = [u for u in TEAM_LEADS if not is_excluded(u)]
        print(f"Excluded {len(skipped)} user(s): {skipped}")
        # An entry matching nobody is nearly always a typo or a departed
        # colleague, and silently doing nothing is how it stays wrong.
        unmatched = excluded - {u.strip().lower() for u in skipped} - {
            str(EMAIL_BY_NAME.get(u) or "").strip().lower() for u in skipped}
        if unmatched:
            print(f"WARNING: EXCLUDE entries matching no user: {sorted(unmatched)}")

    print(f"Monitoring {len(USER_LIST)} user(s): {USER_LIST}")
    print("Slack handles: "
          + ", ".join(f"{u} -> @{slack_handle_for(u)}" for u in USER_LIST))
    print(f"Team leads (scope={TEAM_LEAD_SCOPE}): {TEAM_LEADS}")

    if not USER_LIST:
        print("No users found with name, email and scope set - nothing to check.")
    else:
        # -----------------------------------------------------------------
        # Query timesheet data for the whole window, grouped per user PER
        # DAY, plus whether every entry that day is validated.
        # -----------------------------------------------------------------

        user_list_sql = ", ".join("'" + u.replace("'", "''") + "'" for u in USER_LIST)

        query = f"""
        SELECT
            user_name,
            CAST(entry_date AS DATE) AS entry_day,
            COALESCE(SUM(duration), 0) AS total_minutes,
            BOOL_AND(approved) AS all_validated
        FROM ts_reporting.fact_timetable
        WHERE entry_date >= TIMESTAMP '{window_start_str} 00:00:00'
          AND entry_date < TIMESTAMP '{window_end_exclusive_str} 00:00:00'
          AND user_name IN ({user_list_sql})
        GROUP BY user_name, CAST(entry_date AS DATE)
        """

        rows = fetch_with_retry(dbconn, query, "Timesheet query")

        # day_data[(user, day)] = (total_minutes, all_validated)
        day_data = {}
        for row in rows:
            day = row["entry_day"]
            if hasattr(day, "date"):
                day = day.date()
            elif isinstance(day, str):
                day = date.fromisoformat(day[:10])
            day_data[(row["user_name"], day)] = (
                row["total_minutes"] or 0,
                bool(row["all_validated"]),
            )

        # -----------------------------------------------------------------
        # Build shortfall list: every (user, day) under threshold, UNLESS
        # every entry logged that day is validated. Days with zero entries
        # have nothing to validate, so they're always included if < threshold.
        # -----------------------------------------------------------------

        shortfalls = []  # list of (user, day, minutes)
        for user in USER_LIST:
            for day in days_to_check:
                minutes, all_validated = day_data.get((user, day), (0, False))
                if minutes < DAILY_THRESHOLD_MINUTES and not all_validated:
                    shortfalls.append((user, day, minutes))

        shortfalls.sort(key=lambda x: (x[1], x[0]))

        print(f"Found {len(shortfalls)} outstanding shortfall day(s)")
        for user, day, minutes in shortfalls:
            print(f"  {day.isoformat()} - {user}: {minutes} min")

        by_user = {}
        for user, day, minutes in shortfalls:
            by_user.setdefault(user, []).append((day, minutes))

        # -------------------------------------------------------------
        # 1. Per-employee Slack DM with their own outstanding days
        # -------------------------------------------------------------
        
        if by_user:
            slack = pq.connect(SLACK_CONNECTION_NAME)
            for user in sorted(by_user):
                lines = []
                if today.weekday() == 4:
                    lines.append(
                        "*Reminder: please submit your timesheets of this week*\n"
                    )
                lines.append(
                    f"Missing hours check\n\n"
                    f"You have {len(by_user[user])} day(s) logged under "
                    f"{DAILY_THRESHOLD_MINUTES // 60}h that still need attention:"
                )
                for day, minutes in sorted(by_user[user]):
                    lines.append(format_shortfall_line(day, minutes))
                lines.append("\nAdd/Edit your entries: <https://app.eu.peliqan.io/apps/dkV4ZE1JMW5obnhsblFJemM5anhKZEQ5UTZYWVp6TTNLZmhPRDJEcXZxeDljcnBndTBWcndnaWpIVmRoYjJwaw==/|Timesheet Calendar>")
                text = "\n".join(lines)

                result = slack.add("message", {
                    "text": text,
                    "channel": f"@{slack_handle_for(user)}",
                })
                print(f"Slack DM to {user}: {result}")
        else:
            print("No outstanding shortfalls - no employee DMs sent.")
        
        # -------------------------------------------------------------
        # 2. Monday-only: full team overview DM to each team lead
        # -------------------------------------------------------------

        if today.weekday() == 0:  # Monday
            if not TEAM_LEADS:
                print("Today is Monday but no team leads found (scope="
                      f"{TEAM_LEAD_SCOPE}) - no digest sent.")
            elif not by_user:
                print("Today is Monday but there are no outstanding shortfalls - no digest sent.")
            else:
                lines = [
                    f"Weekly missing-hours overview - as of {today.isoformat()} (Mon)",
                    f"Tracking since {window_start_str}.",
                    "",
                    f"{len(shortfalls)} day(s) still outstanding across {len(by_user)} "
                    "employee(s):",
                    "",
                ]
                for user in sorted(by_user):
                    lines.append(f"*{user}*")
                    for day, minutes in sorted(by_user[user]):
                        lines.append(format_shortfall_line(day, minutes))
                    lines.append("")
                digest_text = "\n".join(lines).rstrip()

                slack = pq.connect(SLACK_CONNECTION_NAME)
                for lead in TEAM_LEADS:
                    result = slack.add("message", {
                        "text": digest_text,
                        "channel": f"@{slack_handle_for(lead)}",
                    })
                    print(f"Slack digest DM to team lead {lead}: {result}")
        else:
            print(f"Today ({today.isoformat()}) is not Monday - no team-lead digest sent.")
