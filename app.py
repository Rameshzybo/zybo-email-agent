import base64
import json
import os
import io
import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google import genai

# Safe Optional Attachment Parsers
try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import docx
except ImportError:
    docx = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

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

# --- Attachment Handling ---
def parse_attachment_content(service, msg_id, part):
    filename = part.get('filename', '')
    mime_type = part.get('mimeType', '')
    body = part.get('body', {})
    attachment_id = body.get('attachmentId')

    if not attachment_id:
        return filename, "No payload data"

    try:
        attachment = service.users().messages().attachments().get(
            userId='me', messageId=msg_id, id=attachment_id
        ).execute()
        file_data = base64.urlsafe_b64decode(attachment['data'])

        # PDF Extraction
        if ('pdf' in mime_type or filename.endswith('.pdf')) and pypdf:
            reader = pypdf.PdfReader(io.BytesIO(file_data))
            text = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
            return filename, f"[PDF Text Summary]: {text[:500]}..."

        # Word Document Extraction
        elif ('word' in mime_type or filename.endswith('.docx')) and docx:
            doc = docx.Document(io.BytesIO(file_data))
            text = "\n".join([p.text for p in doc.paragraphs if p.text])
            return filename, f"[Word Doc Text]: {text[:500]}..."

        # Excel Spreadsheet Extraction
        elif ('excel' in mime_type or 'spreadsheet' in mime_type or filename.endswith('.xlsx')) and openpyxl:
            wb = openpyxl.load_workbook(io.BytesIO(file_data), data_only=True)
            sheet = wb.active
            rows = []
            for row in sheet.iter_rows(values_only=True):
                filtered_row = [str(cell) for cell in row if cell is not None]
                if filtered_row:
                    rows.append(" | ".join(filtered_row))
            return filename, f"[Excel Sheet Data]: {'; '.join(rows[:10])}..."

        return filename, f"[Attached File]: {filename} ({mime_type})"
    except Exception as e:
        return filename, f"Failed to parse {filename}: {e}"

# --- AI & Rule Analysis ---
def analyze_email_with_gemini(subject, sender, snippet, thread_text, attachment_summary):
    sub_lower = subject.lower()
    snip_lower = snippet.lower()

    # Rule-Based Fast Paths (Saves Gemini Daily Quota)
    if "wordfence" in sub_lower or "security" in sub_lower or "login alert" in sub_lower:
        return """Category: Security Alert
Priority: High
Reason: Automated website security or firewall notification.
Action Required: Review Required
Summary: Security warning requiring monitoring.
Suggested Reply: Notified technical support for review."""

    if "please moderate" in sub_lower or "new comment" in sub_lower:
        return """Category: Spam
Priority: Low
Reason: Unsolicited WordPress comment moderation notification.
Action Required: None
Summary: WordPress comment moderation request.
Suggested Reply: Automated notification processed."""

    if "unsubscribe" in snip_lower or "offer" in sub_lower or "discount" in sub_lower:
        return """Category: Promotional
Priority: Low
Reason: Marketing communication or promotional offer.
Action Required: None
Summary: Promotional newsletter or discount campaign.
Suggested Reply: Offers noted."""

    if not GEMINI_API_KEY:
        return "Category: General\nPriority: Low\nReason: API Key missing\nAction Required: None\nSummary: Gemini API Key missing.\nSuggested Reply: Thank you."

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
        Analyze this email and full thread context strictly in this format:
        Category: [Urgent, Client Project, Security Alert, Promotional, Spam, General]
        Priority: [High, Medium, Low]
        Reason: [1 short sentence explaining priority]
        Action Required: [None, Reply Needed, Review Required, Deadlines]
        Summary: [Concise summary covering the main points and thread history]
        Suggested Reply: [A professional draft response]

        Sender: {sender}
        Subject: {subject}
        Snippet: {snippet}
        Thread Context: {thread_text[:1000]}
        Attachment Context: {attachment_summary}
        """
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return f"""Category: General
Priority: Low
Reason: Processed via fallback engine due to rate limit ({e}).
Action Required: None
Summary: {snippet[:120]}
Suggested Reply: Hello,\n\nThank you for reaching out to ZYBO Tech Lab. We have received your email and will respond shortly.\n\nBest regards,\nZYBO Tech Lab"""

# Initialize session state for persistent results
if "analyzed_emails" not in st.session_state:
    st.session_state.analyzed_emails = []

# --- UI Layout ---
st.title("✉️ ZYBO AI Email Agent Dashboard")
st.markdown("Automated Mail Categorization, Thread Summarization, Priority Detection, Attachment Extraction & Cleanup.")

# Sidebar Controls
with st.sidebar:
    st.subheader("⚙️ Scan & Search Controls")
    search_keyword = st.text_input("🔍 Smart Search (Sender, Subject, Keywords)", "")
    max_scan_limit = st.slider("Max emails to scan", 5, 100, 20)
    
    st.markdown("---")
    st.markdown("### 🧹 Automation Cleanup")
    auto_trash_spam = st.checkbox("🗑️ Automatically trash Spam", value=True)
    auto_trash_promo = st.checkbox("🗑️ Automatically trash Promotions", value=True)
    require_confirmation = st.checkbox("🔒 Require confirmation before trashing", value=False)
    
    run_btn = st.button("🚀 Fetch, Analyze & Organize Mail", type="primary")

# Execute scan
if run_btn:
    if not GEMINI_API_KEY:
        st.error("Please configure your GEMINI_API_KEY in Streamlit Secrets.")
    else:
        with st.spinner("Scanning Gmail account, parsing threads & attachments, and running AI analysis..."):
            service = get_gmail_service()
            if service:
                messages = []
                page_token = None
                
                q_query = f"in:anywhere {search_keyword}".strip()

                while len(messages) < max_scan_limit:
                    results = service.users().messages().list(
                        userId='me', 
                        q=q_query, 
                        includeSpamTrash=True, 
                        maxResults=min(20, max_scan_limit - len(messages)),
                        pageToken=page_token
                    ).execute()
                    
                    batch = results.get('messages', [])
                    if not batch:
                        break
                    messages.extend(batch)
                    
                    page_token = results.get('nextPageToken')
                    if not page_token:
                        break

                if not messages:
                    st.info("No matching messages found in your account.")
                    st.session_state.analyzed_emails = []
                else:
                    processed_items = []
                    for msg in messages:
                        msg_id = msg['id']
                        try:
                            txt = service.users().messages().get(userId='me', id=msg_id).execute()
                            thread_id = txt.get('threadId')
                            headers = txt.get('payload', {}).get('headers', [])
                            label_ids = txt.get('labelIds', [])
                            
                            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
                            snippet = txt.get('snippet', '')

                            thread_text = ""
                            if thread_id:
                                thread_resp = service.users().threads().get(userId='me', id=thread_id).execute()
                                thread_msgs = thread_resp.get('messages', [])
                                thread_text = "\n".join([m.get('snippet', '') for m in thread_msgs])

                            attachments_info = []
                            payload = txt.get('payload', {})
                            parts = payload.get('parts', [])
                            for part in parts:
                                if part.get('filename'):
                                    fname, ftext = parse_attachment_content(service, msg_id, part)
                                    attachments_info.append(f"{fname}: {ftext}")

                            att_summary = " | ".join(attachments_info) if attachments_info else "None"

                            ai_response = analyze_email_with_gemini(subject, sender, snippet, thread_text, att_summary)
                            
                            category = "General"
                            priority = "Low"
                            reason = "Routine notification"
                            action_req = "None"
                            summary = snippet
                            suggested_reply = f"Hello,\n\nThank you for reaching out regarding '{subject}'. We will get back to you shortly.\n\nBest regards,\nZYBO Tech Lab"

                            for line in ai_response.split('\n'):
                                if "Category:" in line:
                                    clean_cat = line.replace("Category:", "").strip().replace("[", "").replace("]", "")
                                    if clean_cat in ["Urgent", "Client Project", "Security Alert", "General", "Promotional", "Spam"]:
                                        category = clean_cat
                                elif "Priority:" in line:
                                    priority = line.replace("Priority:", "").strip().replace("[", "").replace("]", "")
                                elif "Reason:" in line:
                                    reason = line.replace("Reason:", "").strip()
                                elif "Action Required:" in line:
                                    action_req = line.replace("Action Required:", "").strip()
                                elif "Summary:" in line:
                                    summary = line.replace("Summary:", "").strip()
                                elif "Suggested Reply:" in line:
                                    suggested_reply = line.replace("Suggested Reply:", "").strip()

                            if 'SPAM' in label_ids:
                                category = 'Spam'

                            email_data = {
                                "id": msg_id,
                                "sender": sender,
                                "subject": subject,
                                "snippet": snippet,
                                "category": category,
                                "priority": priority,
                                "reason": reason,
                                "action_required": action_req,
                                "summary": summary,
                                "suggested_reply": suggested_reply,
                                "attachments": [a.split(':')[0] for a in attachments_info],
                                "is_starred": 'STARRED' in label_ids,
                                "is_unread": 'UNREAD' in label_ids,
                                "trashed": False
                            }

                            should_trash = (category == "Spam" and auto_trash_spam) or (category == "Promotional" and auto_trash_promo)
                            if should_trash and not require_confirmation and 'TRASH' not in label_ids:
                                try:
                                    service.users().messages().trash(userId='me', id=msg_id).execute()
                                    email_data["trashed"] = True
                                except Exception:
                                    pass

                            processed_items.append(email_data)
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
                            if item['attachments']:
                                st.markdown(f"📎 **Attachments:** `{', '.join(item['attachments'])}`")
                        with c2:
                            prio_color = "red" if item['priority'] == 'High' else ("orange" if item['priority'] == 'Medium' else "green")
                            st.markdown(f"**Priority:** :{prio_color}[{item['priority']}]")
                            st.markdown(f"**Action:** `{item['action_required']}`")
                        
                        st.markdown(f"**AI Summary:** {item['summary']}")
                        st.caption(f"Reason: {item['reason']}")
                        
                        if item['trashed']:
                            st.error("🗑️ *Automatically moved to Trash.*")

                        btn_col1, btn_col2, btn_col3 = st.columns(3)
                        service = get_gmail_service()
                        
                        with btn_col1:
                            star_label = "⭐ Unstar" if item['is_starred'] else "⭐ Star"
                            if st.button(star_label, key=f"star_{item['id']}"):
                                if service:
                                    body = {'removeLabelIds': ['STARRED']} if item['is_starred'] else {'addLabelIds': ['STARRED']}
                                    service.users().messages().modify(userId='me', id=item['id'], body=body).execute()
                                    item['is_starred'] = not item['is_starred']
                                    st.rerun()

                        with btn_col2:
                            read_label = "✉️ Mark Unread" if not item['is_unread'] else "📩 Mark Read"
                            if st.button(read_label, key=f"read_{item['id']}"):
                                if service:
                                    body = {'addLabelIds': ['UNREAD']} if not item['is_unread'] else {'removeLabelIds': ['UNREAD']}
                                    service.users().messages().modify(userId='me', id=item['id'], body=body).execute()
                                    item['is_unread'] = not item['is_unread']
                                    st.rerun()

                        with btn_col3:
                            if st.button("📥 Archive Email", key=f"arch_{item['id']}"):
                                if service:
                                    service.users().messages().modify(userId='me', id=item['id'], body={'removeLabelIds': ['INBOX']}).execute()
                                    st.success("Archived from Inbox!")

                        with st.expander("✨ AI Reply Generator & Gmail Draft"):
                            reply_text = st.text_area("Edit Draft Reply:", value=item['suggested_reply'], key=f"reply_{item['id']}")
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
