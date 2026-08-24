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
approved (approved = true for all of that day's entries), in which case it's
treated as signed off and skipped.

Two notifications per run:
1. Each employee with outstanding shortfall days gets a Slack DM listing
   every one of their non-8h, not-fully-approved days since FIXED_START_DATE.
2. If run on a Monday, every "team lead" (ts_prod.users.scope == TEAM_LEAD_SCOPE)
   additionally gets a Slack DM with the full team-wide overview.

Monitored users / team leads are both derived dynamically from ts_prod.users:
anyone with a non-null name, email AND scope (this excludes placeholder
"role" rows like "Admin (Scope 3)" which have no email).

IMPORTANT CAVEAT: Slack DMs are sent as {"channel": "@<name>"}, using the
exact ts_prod.users.name value as the Slack username. If someone's real
Slack username differs from their displayed name, add an entry to
SLACK_USERNAME_OVERRIDES below - otherwise their DM will fail the same way
a bad channel name does (silently, from this script's point of view; check
the printed Slack send results after each run).

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
# digest. Currently only "Lucas" has scope 2 in this account.
TEAM_LEAD_SCOPE = 2

# Restrict monitoring + all Slack DMs to only these people (matched against
# ts_prod.users.name). Set to None to include everyone with name+email+scope
# set (the previous, unrestricted behavior).
NOTIFY_ONLY = ["Lucas", "Sander", "Piet-Michiel"]


# Maps ts_prod.users.name -> real Slack username, for anyone whose Slack
# handle differs from their displayed name. Add entries as needed, e.g.
# SLACK_USERNAME_OVERRIDES = {"Piet-Michiel": "pm_vdb"}
SLACK_USERNAME_OVERRIDES = {}

# Only shortfall days on/after this date are tracked/notified.
FIXED_START_DATE = date(2026, 8, 17)
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
    return SLACK_USERNAME_OVERRIDES.get(name, name)


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
    SELECT name, scope
    FROM ts_prod.users
    WHERE name IS NOT NULL
      AND email IS NOT NULL
      AND scope IS NOT NULL
    """
    roster_rows = fetch_with_retry(dbconn, roster_query, "Roster query")

    USER_LIST = [row["name"] for row in roster_rows]
    TEAM_LEADS = [row["name"] for row in roster_rows if row["scope"] == TEAM_LEAD_SCOPE]

    if NOTIFY_ONLY is not None:
        USER_LIST = [u for u in USER_LIST if u in NOTIFY_ONLY]
        TEAM_LEADS = [u for u in TEAM_LEADS if u in NOTIFY_ONLY]

    print(f"Monitoring {len(USER_LIST)} user(s): {USER_LIST}")
    print(f"Team leads (scope={TEAM_LEAD_SCOPE}): {TEAM_LEADS}")

    if not USER_LIST:
        print("No users found with name, email and scope set - nothing to check.")
    else:
        # -----------------------------------------------------------------
        # Query timesheet data for the whole window, grouped per user PER
        # DAY, plus whether every entry that day is approved.
        # -----------------------------------------------------------------

        user_list_sql = ", ".join("'" + u.replace("'", "''") + "'" for u in USER_LIST)

        query = f"""
        SELECT
            user_name,
            CAST(entry_date AS DATE) AS entry_day,
            COALESCE(SUM(duration), 0) AS total_minutes,
            BOOL_AND(approved) AS all_approved
        FROM ts_reporting.fact_timetable
        WHERE entry_date >= TIMESTAMP '{window_start_str} 00:00:00'
          AND entry_date < TIMESTAMP '{window_end_exclusive_str} 00:00:00'
          AND user_name IN ({user_list_sql})
        GROUP BY user_name, CAST(entry_date AS DATE)
        """

        rows = fetch_with_retry(dbconn, query, "Timesheet query")

        # day_data[(user, day)] = (total_minutes, all_approved)
        day_data = {}
        for row in rows:
            day = row["entry_day"]
            if hasattr(day, "date"):
                day = day.date()
            elif isinstance(day, str):
                day = date.fromisoformat(day[:10])
            day_data[(row["user_name"], day)] = (
                row["total_minutes"] or 0,
                bool(row["all_approved"]),
            )

        # -----------------------------------------------------------------
        # Build shortfall list: every (user, day) under threshold, UNLESS
        # every entry logged that day is approved. Days with zero entries
        # have nothing to approve, so they're always included if < threshold.
        # -----------------------------------------------------------------

        shortfalls = []  # list of (user, day, minutes)
        for user in USER_LIST:
            for day in days_to_check:
                minutes, all_approved = day_data.get((user, day), (0, False))
                if minutes < DAILY_THRESHOLD_MINUTES and not all_approved:
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
                    f"Missing hours check \u2014 tracking since {window_start_str}\n\n"
                    f"You have {len(by_user[user])} day(s) logged under "
                    f"{DAILY_THRESHOLD_MINUTES // 60}h that still need attention:"
                )
                for day, minutes in sorted(by_user[user]):
                    lines.append(format_shortfall_line(day, minutes))
                lines.append("")
                lines.append(
                    "If any of these are already covered by approved leave or time off, "
                    "check with your team lead so they can be marked approved."
                )
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
