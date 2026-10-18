import base64
import json
import os
import io
import time
import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Page Configuration
st.set_page_config(page_title="ZYBO Email Agent Dashboard", page_icon="✉️", layout="wide")

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

# Securely load Gemini API Key
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

# Automatically generate credentials.json on the fly for cloud OAuth client setup
if not os.path.exists('credentials.json') and 'GOOGLE_CREDENTIALS' in st.secrets:
    creds_dict = dict(st.secrets['GOOGLE_CREDENTIALS'])
    full_creds = {
        "installed": {
            "client_id": creds_dict.get("client_id"),
            "project_id": creds_dict.get("project_id"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": creds_dict.get("client_secret"),
            "redirect_uris": ["http://localhost"]
        }
    }
    with open('credentials.json', 'w') as f:
        json.dump(full_creds, f)

def get_gmail_service():
    if 'GMAIL_TOKEN' not in st.secrets:
        st.error("The [GMAIL_TOKEN] section is missing from your Streamlit Cloud Secrets.")
        return None

    creds = None
    try:
        token_secret = st.secrets['GMAIL_TOKEN']
        token_info = {
            "token": token_secret.get("token"),
            "refresh_token": token_secret.get("refresh_token"),
            "token_uri": token_secret.get("token_uri", "https://oauth2.googleapis.com/token"),
            "client_id": token_secret.get("client_id"),
            "client_secret": token_secret.get("client_secret"),
            "scopes": list(token_secret.get("scopes", SCOPES)),
        }
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    except Exception as e:
        st.error(f"Error reading token secrets: {e}")
        return None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                st.error(f"Token refresh failed: {e}")
                return None
        else:
            st.error("Authentication token is invalid or expired.")
            return None
            
    return build('gmail', 'v1', credentials=creds)

# --- Smart Local Rule Classifier with Expanded Digest & Promo Matching ---
def classify_email_locally(subject, sender, snippet, headers, label_ids):
    sub_lower = subject.lower()
    snip_lower = snippet.lower()
    send_lower = sender.lower()

    # If Gmail natively marks it as SPAM, respect it immediately
    if 'SPAM' in label_ids:
        return {
            "category": "Spam",
            "priority": "Low",
            "reason": "Marked as Spam by Gmail filter.",
            "action_required": "None",
            "summary": f"Spam message from {sender}",
            "suggested_reply": "N/A",
            "unsubscribe": ""
        }

    # Extract List-Unsubscribe header if present
    unsubscribe_link = ""
    for h in headers:
        if h['name'].lower() == 'list-unsubscribe':
            unsubscribe_link = h['value'].strip('<>')

    # 1. Security Alerts
    if "wordfence" in sub_lower or "security" in sub_lower or "login alert" in sub_lower or "vulnerability" in sub_lower:
        return {
            "category": "Security Alert",
            "priority": "High",
            "reason": "Automated website security or firewall notification.",
            "action_required": "Review Required",
            "summary": f"Security warning regarding website activity from {sender}.",
            "suggested_reply": "Thank you. The technical team will review this security alert.",
            "unsubscribe": unsubscribe_link
        }

    # 2. Spam & Comment Moderation
    if "please moderate" in sub_lower or "new comment" in sub_lower or "comment on" in sub_lower:
        return {
            "category": "Spam",
            "priority": "Low",
            "reason": "Unsolicited WordPress comment moderation notification.",
            "action_required": "None",
            "summary": "WordPress comment moderation request.",
            "suggested_reply": "Automated notification processed.",
            "unsubscribe": unsubscribe_link
        }

    # 3. Promotional, Newsletters & Digests (e27, Reddit, etc.)
    if "e27" in send_lower or "reddit" in send_lower or "digest" in sub_lower or "newsletter" in sub_lower or "unsubscribe" in snip_lower or "offer" in sub_lower or "discount" in sub_lower or "promo" in sub_lower or "weekly" in sub_lower:
        return {
            "category": "Promotional",
            "priority": "Low",
            "reason": "Marketing digest, newsletter or promotional communication.",
            "action_required": "None",
            "summary": f"Promotional publication or digest: {subject}",
            "suggested_reply": "Offers noted.",
            "unsubscribe": unsubscribe_link
        }

    # 4. Client Projects
    if "project" in sub_lower or "design" in sub_lower or "website" in sub_lower or "zybo" in sub_lower or "client" in sub_lower:
        return {
            "category": "Client Project",
            "priority": "High",
            "reason": "Client website or agency project correspondence.",
            "action_required": "Reply Needed",
            "summary": f"Client project communication: {snippet[:100]}...",
            "suggested_reply": "Hello,\n\nThank you for your update. Ramesh and the ZYBO team are reviewing your requirements and will get back to you shortly.\n\nBest regards,\nZYBO Tech Lab",
            "unsubscribe": unsubscribe_link
        }

    # Default General
    return {
        "category": "General",
        "priority": "Medium",
        "reason": "Standard correspondence or notification.",
        "action_required": "None",
        "summary": snippet[:150] if snippet else "No summary available.",
        "suggested_reply": "Hello,\n\nThank you for reaching out to ZYBO Tech Lab. We have received your message and will respond shortly.\n\nBest regards,\nZYBO Tech Lab",
        "unsubscribe": unsubscribe_link
    }

# Initialize session state for persistent results
if "analyzed_emails" not in st.session_state:
    st.session_state.analyzed_emails = []

# --- UI Layout ---
st.title("✉️ ZYBO AI Email Agent Dashboard")
st.markdown("Automated Mail Categorization, Thread Summarization, Priority Detection, Unsubscribe & Cleanup.")

# Sidebar Controls
with st.sidebar:
    st.subheader("⚙️ Scan & Search Controls")
    search_keyword = st.text_input("🔍 Smart Search (Sender, Subject, Keywords)", "")
    max_scan_limit = st.slider("Max emails to scan", 50, 1000, 100)
    
    st.markdown("---")
    st.markdown("### 🧹 Automation Cleanup")
    auto_trash_spam = st.checkbox("🗑️ Automatically trash Spam", value=True)
    auto_trash_promo = st.checkbox("🗑️ Automatically trash Promotions", value=True)
    require_confirmation = st.checkbox("🔒 Require confirmation before trashing", value=False)
    
    run_btn = st.button("🚀 Fetch, Analyze & Organize Mail", type="primary")

# Execute scan
if run_btn:
    with st.spinner(f"Scanning up to {max_scan_limit} emails across all folders..."):
        service = get_gmail_service()
        if service:
            messages = []
            page_token = None
            q_query = f"in:anywhere {search_keyword}".strip()

            try:
                while len(messages) < max_scan_limit:
                    fetch_size = min(100, max_scan_limit - len(messages))
                    results = service.users().messages().list(
                        userId='me', 
                        q=q_query, 
                        includeSpamTrash=True, 
                        maxResults=fetch_size,
                        pageToken=page_token
                    ).execute()
                    
                    batch = results.get('messages', [])
                    if not batch:
                        break
                    messages.extend(batch)
                    
                    page_token = results.get('nextPageToken')
                    if not page_token:
                        break
            except Exception as e:
                st.error(f"API Scan Error: {e}")

            if not messages:
                st.info("No matching messages found in your account.")
                st.session_state.analyzed_emails = []
            else:
                processed_items = []
                for idx, msg in enumerate(messages):
                    msg_id = msg['id']
                    try:
                        txt = service.users().messages().get(userId='me', id=msg_id).execute()
                        headers = txt.get('payload', {}).get('headers', [])
                        label_ids = txt.get('labelIds', [])
                        
                        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                        sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
                        snippet = txt.get('snippet', '')

                        classification = classify_email_locally(subject, sender, snippet, headers, label_ids)
                        
                        category = classification["category"]
                        is_trashed = 'TRASH' in label_ids

                        email_data = {
                            "id": msg_id,
                            "sender": sender,
                            "subject": subject,
                            "snippet": snippet,
                            "category": category,
                            "priority": classification["priority"],
                            "reason": classification["reason"],
                            "action_required": classification["action_required"],
                            "summary": classification["summary"],
                            "suggested_reply": classification["suggested_reply"],
                            "unsubscribe": classification.get("unsubscribe", ""),
                            "is_starred": 'STARRED' in label_ids,
                            "is_unread": 'UNREAD' in label_ids,
                            "trashed": is_trashed
                        }

                        # Auto-Cleanup Execution (Sends directly to Gmail Trash via API)
                        should_trash = (category == "Spam" and auto_trash_spam) or (category == "Promotional" and auto_trash_promo)
                        if should_trash and not require_confirmation and not is_trashed:
                            try:
                                service.users().messages().trash(userId='me', id=msg_id).execute()
                                email_data["trashed"] = True
                            except Exception as trash_err:
                                st.warning(f"Could not auto-trash message {msg_id}: {trash_err}")

                        processed_items.append(email_data)
                        
                        if idx > 0 and idx % 20 == 0:
                            time.sleep(0.3)
                            
                    except Exception:
                        continue

                st.session_state.analyzed_emails = processed_items
                st.success(f"Successfully processed {len(processed_items)} emails across your account!")

# Render Dashboard Metrics & Feed
if st.session_state.analyzed_emails:
    emails = st.session_state.analyzed_emails
    
    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    with col_m1:
        st.metric("Total Scanned", len(emails))
    with col_m2:
        high_prio = sum(1 for e in emails if e['priority'] == 'High')
        st.metric("High Priority", high_prio)
    with col_m3:
        action_needed = sum(1 for e in emails if e['action_required'] != 'None')
        st.metric("Actions Pending", action_needed)
    with col_m4:
        trashed_count = sum(1 for e in emails if e['trashed'])
        st.metric("Cleaned / Trashed", trashed_count)

    st.markdown("---")

    tabs = st.tabs(["🚀 Urgent", "💼 Client Project", "🛡️ Security Alert", "📂 General", "🏷️ Promotional", "🗑️ Spam"])
    bin_keys = ["Urgent", "Client Project", "Security Alert", "General", "Promotional", "Spam"]
    
    for i, tab in enumerate(tabs):
        current_cat = bin_keys[i]
        items = [e for e in emails if e['category'] == current_cat]
        
        with tab:
            if not items:
                st.info(f"No emails found in {current_cat}.")
            else:
                for item in items:
                    with st.container(border=True):
                        c1, c2 = st.columns([3, 1])
                        with c1:
                            st.markdown(f"**From:** {item['sender']}")
                            st.markdown(f"**Subject:** {item['subject']}")
                        with c2:
                            prio_color = "red" if item['priority'] == 'High' else ("orange" if item['priority'] == 'Medium' else "green")
                            st.markdown(f"**Priority:** :{prio_color}[{item['priority']}]")
                            st.markdown(f"**Action:** `{item['action_required']}`")
                        
                        st.markdown(f"**AI Summary:** {item['summary']}")
                        st.caption(f"Reason: {item['reason']}")
                        
                        if item['trashed']:
                            st.error("🗑️ *Moved to Trash (Synced with Gmail).*")

                        # Action Buttons (Star, Mark Read, Archive, Unsubscribe, Delete)
                        btn_col1, btn_col2, btn_col3, btn_col4, btn_col5 = st.columns(5)
                        service = get_gmail_service()
                        
                        with btn_col1:
                            star_label = "⭐ Unstar" if item.get('is_starred', False) else "⭐ Star"
                            if st.button(star_label, key=f"star_{item['id']}"):
                                if service:
                                    body = {'removeLabelIds': ['STARRED']} if item.get('is_starred', False) else {'addLabelIds': ['STARRED']}
                                    service.users().messages().modify(userId='me', id=item['id'], body=body).execute()
                                    item['is_starred'] = not item.get('is_starred', False)
                                    st.rerun()

                        with btn_col2:
                            read_label = "✉️ Unread" if not item.get('is_unread', False) else "📩 Read"
                            if st.button(read_label, key=f"read_{item['id']}"):
                                if service:
                                    body = {'addLabelIds': ['UNREAD']} if not item.get('is_unread', False) else {'removeLabelIds': ['UNREAD']}
                                    service.users().messages().modify(userId='me', id=item['id'], body=body).execute()
                                    item['is_unread'] = not item.get('is_unread', False)
                                    st.rerun()

                        with btn_col3:
                            if st.button("📥 Archive", key=f"arch_{item['id']}"):
                                if service:
                                    service.users().messages().modify(userId='me', id=item['id'], body={'removeLabelIds': ['INBOX']}).execute()
                                    st.success("Archived!")

                        with btn_col4:
                            unsub_link = item.get('unsubscribe', '')
                            if unsub_link:
                                if st.button("🔕 Unsubscribe", key=f"unsub_{item['id']}"):
                                    st.markdown(f"[Click here to Unsubscribe]({unsub_link})", unsafe_allow_html=True)
                            else:
                                st.button("🔕 Unsubscribe", key=f"unsub_{item['id']}", disabled=True)

                        with btn_col5:
                            if st.button("🗑️ Delete", key=f"del_{item['id']}"):
                                if service:
                                    try:
                                        # Forces true Gmail API trash sync
                                        service.users().messages().trash(userId='me', id=item['id']).execute()
                                        item['trashed'] = True
                                        st.success("Deleted from Gmail successfully!")
                                        st.rerun()
                                    except Exception as err:
                                        st.error(f"Delete Error: {err}")

                        with st.expander("✨ AI Reply Generator & Gmail Draft"):
                            reply_text = st.text_area("Edit Draft Reply:", value=item.get('suggested_reply', ''), key=f"reply_{item['id']}")
                            if st.button("📥 Save Draft to Gmail", key=f"draft_{item['id']}"):
                                if service:
                                    try:
                                        from email.mime.text import MIMEText
                                        msg = MIMEText(reply_text)
                                        msg['Subject'] = f"Re: {item['subject']}"
                                        msg['To'] = item['sender']
                                        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
                                        
                                        draft_body = {'message': {'raw': raw}}
                                        service.users().drafts().create(userId='me', body=draft_body).execute()
                                        st.success("Successfully saved as a draft in your Gmail account!")
                                    except Exception as err:
                                        st.error(f"Failed to create draft: {err}")
else:
    st.info("👈 Click **'🚀 Fetch, Analyze & Organize Mail'** in the sidebar to run the scan.")
