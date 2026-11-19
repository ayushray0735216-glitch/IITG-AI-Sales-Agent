
"""Shared presentation for the authenticated CRM workspace."""

from hashlib import sha256
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import streamlit as st

from crm_storage import lead_cell, _lead_key
from sharngon_brand import render_brand_header

BRAND_VERSION = "2026-11-16-prospect-search-v1"


NAVIGATION = (
    ("Workspace", (
        ("overview", "Overview", "space_dashboard"),
        ("leads", "Leads", "group"),
        ("prospects", "Find prospects", "person_search"),
        ("inbox", "Inbox", "inbox"),
        ("campaigns", "Campaigns", "campaign"),
        ("reports", "Reports", "bar_chart"),
    )),
    ("Communications", (
        ("chat", "Chat history", "forum"),
        ("calls", "Scheduled calls", "call"),
        ("follow-ups", "Follow-up controls", "schedule_send"),
    )),
    ("Administration", (
        ("knowledge", "Knowledge center", "menu_book"),
        ("settings", "Restore CRM data", "settings_backup_restore"),
    )),
)
PAGES = {page for _, links in NAVIGATION for page, _, _ in links}


def workspace_url(page, **params):
    return "?" + urlencode({"mode": "developer", "page": page, **params})


def open_workspace_page(page, **params):
    st.query_params.from_dict({"mode": "developer", "page": page, **params})
    st.rerun()


def go_to_url(url):
    params = dict(parse_qsl(url.removeprefix("?")))
    params.pop("mode", None)
    open_workspace_page(**params)


def lead_link_key(row):
    return sha256(_lead_key(row).encode("utf-8")).hexdigest()[:24]


def render_global_search(leads):
    if st.session_state.pop("crm_clear_global_search", False):
        st.session_state.pop("crm_global_search", None)
    search = st.text_input(
        "Search CRM", placeholder="Search leads by name, email, phone, or company",
        key="crm_global_search",
    ).strip()
    if not search:
        return
    terms = search.casefold().split()
    digits = "".join(char for char in search if char.isdigit())
    found = 0
    with st.container(border=True):
        for lead in leads:
            searchable = " ".join(lead_cell(lead.get(field)) for field in (
                "Name", "Contact Name", "Email", "Phone", "WhatsApp Number", "Company", "Visitor ID",
            )).casefold()
            numbers = ["".join(char for char in lead_cell(lead.get(field)) if char.isdigit())
                       for field in ("Phone", "WhatsApp Number")]
            if not (all(term in searchable for term in terms)
                    or len(digits) >= 3 and not any(char.isalpha() for char in search)
                    and any(digits in number for number in numbers)):
                continue
            name = lead_cell(lead.get("Contact Name")) or lead_cell(lead.get("Name")) or "Lead"
            label = " · ".join(filter(None, (name, lead_cell(lead.get("Company")), lead_cell(lead.get("Email")))))
            if st.button(label, key=f"crm_search_result_{found}", icon=":material/person_search:"):
                st.session_state["crm_clear_global_search"] = True
                open_workspace_page("leads", lead=lead_link_key(lead))
            found += 1
            if found == 10:
                st.caption("Showing the first 10 matches. Refine your search for more specific results.")
                break
        if not found:
            st.caption("No matching leads.")


def render_work_queue(groups):
    st.subheader("Today’s work queue", anchor=False)
    for title, items, empty, browse_url in groups:
        with st.expander(f"{title} · {len(items)}", expanded=bool(items)):
            if not items:
                st.caption(empty)
            page = st.selectbox(f"{title} page", range(1, (len(items) + 7) // 8 + 1),
                                key=f"queue_page_{title}") if len(items) > 8 else 1
            for index, (label, detail, url) in enumerate(items[(page - 1) * 8:page * 8]):
                with st.container(border=True, horizontal=True, vertical_alignment="center"):
                    with st.container():
                        st.text(label)
                        st.caption(detail)
                    if st.button("Open", key=f"queue_{title}_{page}_{index}_{url}", icon=":material/arrow_forward:"):
                        go_to_url(url)
            if browse_url and items:
                if st.button(f"View all {title.lower()}", key=f"queue_browse_{title}"):
                    go_to_url(browse_url)


def _icon(name):
    return f'<span class="crm-symbol" aria-hidden="true">{name}</span>'


def render_workspace_sidebar(active_page):
    st.html(Path(__file__).with_suffix(".css"))
    with st.sidebar, st.container(key="crm_nav_actions"):
        st.html(
            '<div class="crm-brand crm-nav">'
            f'<div class="crm-brand-mark">{_icon("support_agent")}</div>'
            '<div><strong>Sharngon</strong><small>Developer console</small></div></div>'
        )
        for title, links in NAVIGATION:
            st.html(f'<div class="crm-nav-group"><p>{escape(title)}</p></div>')
            for page, label, icon in links:
                if st.button(label, icon=f":material/{icon}:", width="stretch", key=f"crm_nav_{page}",
                             type="primary" if page == active_page else "secondary"):
                    open_workspace_page(page)


def render_workspace_header():
    render_brand_header("overview")


def render_workspace_metrics(total, overview, conversations, activity):
    metrics = (
        ("Total leads", total, workspace_url("leads"), "group", "blue"),
        ("Follow-ups due", overview["follow_ups_due"], workspace_url("leads", view="due"), "event_upcoming", "amber"),
        ("Chat messages", conversations, workspace_url("chat"), "forum", "violet"),
        ("Sales activities", activity, workspace_url("reports"), "monitoring", "teal"),
    )
    for column, (label, value, url, icon, color) in zip(st.columns(4), metrics):
        with column, st.container(key=f"crm_overview_metric_{color}"):
            st.html(
                f'<div class="crm-metric crm-{color}"><span class="crm-metric-icon">{_icon(icon)}</span>'
                f'<span class="crm-metric-value">{value:,}</span></div>'
            )
            if st.button(label, key=f"crm_metric_{color}", width="stretch"):
                go_to_url(url)


def render_section_header(anchor, title, icon, tone="blue"):
    st.html(
        f'<div id="{anchor}" class="crm-section-heading crm-{tone}">'
        f'<span class="crm-section-icon">{_icon(icon)}</span><h2>{escape(title)}</h2></div>'
    )

