"""Developer Console panel for local Scout prospect review."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

import pandas as pd
import streamlit as st

from scout_import import load

SCOUT_URL = "https://scout-qlnu.onrender.com/"


def render_scout_prospects():
    with st.expander("Scout prospects"):
        search, upload = st.columns(2)
        with search:
            st.subheader("Find new leads")
            st.link_button("Open Scout search", SCOUT_URL)
            st.caption("Run your search in Scout, then download its Excel export.")
        with upload:
            st.subheader("Upload a sheet")
            workbook = st.file_uploader("Scout Excel export", type=["xlsx"], key="scout_workbook")
            import_clicked = st.button("Import to review queue", disabled=workbook is None)

        db = Path(__file__).with_name("shared-integration-scout-review.sqlite3")
        if import_clicked:
            try:
                counts, _ = load(workbook, db)
            except (OSError, ValueError, KeyError, IndexError, ParseError, BadZipFile, sqlite3.Error):
                st.error("Could not import this workbook. Check that it is an original Scout Excel export.")
            else:
                st.success(
                    f"Added {counts['added']}. Already in queue: {counts['existing']}. "
                    f"Missing email: {counts['missing_email']}. "
                    f"Skipped without a stable identity: {counts['missing_identity']}."
                )

        query = st.text_input("Search saved prospects", key="scout_review_query").strip().casefold()
        if not db.exists():
            st.caption("Import a Scout export to start the local review queue.")
            return
        try:
            with closing(sqlite3.connect(db)) as conn:
                records = [
                    json.loads(data)
                    for (data,) in conn.execute("SELECT data FROM scout_review ORDER BY source_key")
                ]
        except sqlite3.Error:
            st.error("The local review queue could not be read.")
            return
        matches = [
            row for row in records
            if any(query in row.get(field, "").casefold() for field in ("Name", "Job Title", "Company"))
        ]
        if matches:
            st.dataframe(pd.DataFrame(matches), hide_index=True)
        else:
            st.info("No matching prospects.")
        st.caption("Local review only. Imports do not trigger outreach or create CRM contacts.")
