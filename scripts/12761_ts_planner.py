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

# Here is some example Python code to get you started.
# Check the Data activation library section for useful code snippets and supported functions.

# Show a title (st = Streamlit module)
st.title("My Table Data")

# Show some text
st.text("Lorem ipsum.")

# connect to the data warehouse
dbconn = pq.dbconnect(pq.DW_NAME)

# fetch records from a table in the data warehouse
rows = dbconn.fetch(pq.DW_NAME, 'ts_prod', 'api_keys')

# Show the results as a dataframe
st.dataframe(rows)
