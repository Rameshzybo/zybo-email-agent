    category_options = ["All"] + sorted(
        {e.get("analysis", {}).get("category", "General") for e in emails}
    )
    filter_col1, filter_col2 = st.columns(2)
    with filter_col1:
        category_filter = st.selectbox("Filter by category", category_options)
    with filter_col2:
        priority_filter = st.selectbox("Filter by priority", ["All", "High", "Medium", "Low"])

    filtered = []
    for email in emails:
        analysis = email.get("analysis", {})
        if category_filter != "All" and analysis.get("category") != category_filter:
            continue
        if priority_filter != "All" and analysis.get("priority") != priority_filter:
            continue
        filtered.append(email)

    if not filtered:
        st.info("No emails match these filters.")

    for email in filtered:
        analysis = email.get("analysis", {})
        title = email.get("subject", "(No subject)")
        category = analysis.get("category", "General")
        priority = analysis.get("priority", "Medium")
        with st.container(border=True):
            top1, top2 = st.columns([5, 1])
            with top1:
                st.markdown(f"**{title}**")
                st.caption(f"From: {email.get('sender', 'Unknown')} · {email.get('date', '')}")
            with top2:
                st.markdown(f"**{priority} priority**")
                st.caption(category)

            st.write(analysis.get("summary", email.get("snippet", "")))
            if analysis.get("action_required"):
                st.markdown("**Action needed:** " + ", ".join(analysis.get("action_items", [])))
            if analysis.get("deadline"):
                st.markdown(f"**Deadline:** {analysis['deadline']}")
            st.caption(
                f"AI confidence: {analysis.get('confidence', 'Low')} · "
                f"Next step: {analysis.get('suggested_next_step', 'Review manually')}"
            )
            if email.get("trashed"):
                st.warning("Moved to Trash by the enabled cleanup rule.")

            b1, b2, b3 = st.columns([1, 1, 2])
            with b1:
                if st.button("Read full email", key=f"read_{email['id']}"):
                    st.session_state.selected_email_id = email["id"]
            with b2:
                if st.button("Draft a reply", key=f"reply_{email['id']}"):
                    st.session_state.selected_email_id = email["id"]
                    st.session_state[f"make_reply_{email['id']}"] = True
            with b3:
                if email.get("id") and st.button(
                    "Open in Gmail",
                    key=f"gmail_{email['id']}",
                    help="Opens Gmail in a new browser tab.",
                ):
                    st.link_button(
                        "Open Gmail",
                        f"https://mail.google.com/mail/u/0/#all/{email['id']}",
                        use_container_width=False,
                    )

# ---------------------------------------------------------
# SELECTED EMAIL DETAIL AND DRAFT
# ---------------------------------------------------------
selected_id = st.session_state.selected_email_id
selected = next((e for e in emails if e.get("id") == selected_id), None)

if selected:
    st.divider()
    st.subheader("✍️ Email detail & reply workspace")
    st.markdown(f"**Subject:** {selected.get('subject', '')}")
    st.markdown(f"**From:** {selected.get('sender', '')}")
    with st.expander("Read full email", expanded=True):
        st.text(selected.get("body") or selected.get("snippet") or "(No readable body found.)")

    draft_key = f"reply_text_{selected['id']}"
    if st.button("✨ Generate reply with Gemini", key=f"generate_{selected['id']}"):
        try:
            with st.spinner("Drafting a contextual reply..."):
                reply = generate_reply(
                    selected, reply_tone, extra_reply_instructions
                )
            st.session_state[draft_key] = reply
        except Exception as exc:
            st.error(f"Reply generation failed: {exc}")

    reply_text = st.text_area(
        "Edit your reply before saving it to Gmail",
        key=draft_key,
        height=220,
        placeholder="Generate a reply or write your own...",
    )
    st.caption("This creates a Gmail draft only. It does not send the email.")

    save_col, close_col = st.columns([1, 1])
    with save_col:
        if st.button("💾 Save as Gmail draft", type="primary", key=f"save_draft_{selected['id']}"):
            if not reply_text.strip():
                st.warning("Write or generate a reply first.")
            else:
                service = get_gmail_service()
                if service:
                    try:
                        draft = create_gmail_draft(service, selected, reply_text.strip())
                        st.session_state.drafts_created.add(draft.get("id", selected["id"]))
                        st.success("Draft saved to Gmail. Open your Drafts folder to review it.")
                    except Exception as exc:
                        st.error(f"Could not create Gmail draft: {exc}")
    with close_col:
        if st.button("Close email workspace", key=f"close_{selected['id']}"):
            st.session_state.selected_email_id = None
            st.rerun()

st.divider()
st.caption(
    "Safety note: AI classifications can be wrong. Automatic cleanup is disabled by default; "
    "test with a small batch before enabling it. This app creates drafts and does not auto-send replies."
)
