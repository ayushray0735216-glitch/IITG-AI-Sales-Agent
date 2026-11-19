"""Shared, local-first presentation of the Sharngon brand."""

from pathlib import Path

import streamlit as st


NAME = "Sharngon"
LOGO = Path(__file__).resolve().parent / "assets" / "sharngon-logo.png"
STYLE = Path(__file__).with_suffix(".css")


def render_brand_header(surface: str):
    """Show the supplied logo and brand name without changing chat or CRM state."""
    st.html(STYLE)
    with st.container(key=f"sharngon_{surface}_brand", horizontal=True, vertical_alignment="center"):
        st.image(str(LOGO), width=104)
        with st.container():
            st.title(NAME, anchor=False)
            if surface == "customer":
                st.caption("Your intelligent sales assistant · Chat with us")
            elif surface == "login":
                st.caption("Developer console · Private CRM")
            else:
                st.caption("Sales workspace · Leads, conversations, and analytics")
