import base64
import json
import os
import io
import time
import re
from datetime import datetime, timedelta
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
st.set_page_config(page_title="ZYBO AI Email Agent Dashboard", page_icon="✉️", layout="wide")

# Standard Scope for Email Actions
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
        saved_scopes = list(token_secret.get("scopes", SCOPES))
        
        token_info = {
            "token": token_secret.get("token"),
            "refresh_token": token_secret.get("refresh_token"),
            "token_uri": token_secret.get("token_uri", "https://oauth2.googleapis.com/token"),
            "client_id": token_secret.get("client_id"),
            "client_secret": token_secret.get("client_secret"),
            "scopes": saved_scopes,
        }
        creds = Credentials.from_authorized_user_info(token_info, saved_scopes)
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

def get_calendar_service():
    service = get_gmail_service()
    if service and hasattr(service, '_http'):
        try:
            return build('calendar', 'v3', credentials=service._http.credentials)
        except Exception:
            return None
    return None

# --- Security & Phishing Inspection ---
def inspect_security_and_links(subject, sender, snippet, payload):
    warnings = []
    combined = f"{subject} {snippet}".lower()

    # 1. Detect Suspicious File Extensions
    dangerous_exts = ['.exe', '.scr', '.vbs', '.js', '.bat', '.cmd', '.msi', '.ps1', '.zip']
    parts = payload.get('parts', [])
    for p in parts:
        fname = p.get('filename', '').lower()
        if any(fname.endswith(ext) for ext in dangerous_exts):
            warnings.append(f"⚠️ Potentially dangerous attachment detected: `{fname}`")

    # 2. Phishing & Suspicious Link Patterns
    if any(k in combined for k in ["verify your account", "login urgently", "password expired", "click here immediately", "bank transfer", "wire funds"]):
        warnings.append("⚠️ Phishing indicator: Urgent credential or financial action requested.")

    # 3. Mismatched domain or raw IP URL patterns
    urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', combined)
    for url in urls:
        if re.search(r'https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', url):
            warnings.append(f"⚠️ Suspicious IP-based URL detected: `{url}`")

    return warnings

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

        if ('pdf' in mime_type or filename.endswith('.pdf')) and pypdf:
            reader = pypdf.PdfReader(io.BytesIO(file_data))
            text = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
            return filename, f"[PDF Text Summary]: {text[:300]}..."

        elif ('word' in mime_type or filename.endswith('.docx')) and docx:
            doc = docx.Document(io.BytesIO(file_data))
            text = "\n".join([p.text for p in doc.paragraphs if p.text])
            return filename, f"[Word Doc Text]: {text[:300]}..."

        elif ('excel' in mime_type or 'spreadsheet' in mime_type or filename.endswith('.xlsx')) and openpyxl:
            wb = openpyxl.load_workbook(io.BytesIO(file_data), data_only=True)
            sheet = wb.active
            rows = []
            for row in sheet.iter_rows(values_only=True):
                filtered_row = [str(cell) for cell in row if cell is not None]
                if filtered_row:
                    rows.append(" | ".join(filtered_row))
            return filename, f"[Excel Sheet Data]: {'; '.join(rows[:5])}..."

        return filename, f"[Attached File]: {filename}"
    except Exception as e:
        return filename, f"Attachment parse error"

# --- Extraction Functions ---
def extract_tasks_and_deadlines(subject, snippet):
    combined = f"{subject} {snippet}".lower()
    tasks = []
    deadlines = []

    if any(k in combined for k in ["deadline", "due date", "by tomorrow", "by Friday", "eod", "asap"]):
        deadlines.append("Urgent timeline detected in email text.")

    if any(k in combined for k in ["please send", "submit", "review", "feedback", "approve", "update"]):
        tasks.append("Action item requested in email body.")

    if "meeting" in combined or "call" in combined or "schedule" in combined or "zoom" in combined:
        tasks.append("Meeting / Call scheduling requested.")

    return tasks, deadlines

def detect_meeting_details(subject, snippet):
    combined = f"{subject} {snippet}".lower()
    if any(k in combined for k in ["meeting", "schedule a call", "zoom", "google meet", "discussion", "appointment"]):
        return {
            "is_meeting": True,
            "title": f"Meeting: {subject}",
            "suggested_time": "Tomorrow at 10:00 AM IST"
        }
    return {"is_meeting": False, "title": "", "suggested_time": ""}

# --- Smart Local Rule Classifier with Custom Rules & Whitelist Support ---
def classify_email_locally(subject, sender, snippet, headers, label_ids, whitelist_str):
    sub_lower = subject.lower()
    snip_lower = snippet.lower()
    send_lower = sender.lower()

    # Check Whitelist Protection
    whitelisted_domains = [d.strip().lower() for d in whitelist_str.split(",") if d.strip()]
    is_whitelisted = any(domain in send_lower for domain in whitelisted_domains)

    if 'SPAM' in label_ids:
        return {
            "category": "Spam",
            "priority": "Low",
            "reason": "Marked as Spam by Gmail filter.",
            "action_required": "None",
            "summary": f"Spam message from {sender}",
            "suggested_reply": "N/A",
            "unsubscribe": "",
            "is_enquiry": False,
            "is_whitelisted": is_whitelisted
        }

    unsubscribe_link = ""
    for h in headers:
        if h['name'].lower() == 'list-unsubscribe':
            unsubscribe_link = h['value'].strip('<>')

    # Custom Automation Rule: Client Enquiry Detection
    is_enquiry = any(k in sub_lower or k in snip_lower for k in ["enquiry", "inquiry", "quote", "pricing", "proposal", "requirement", "cost", "hire"])
    if is_enquiry:
        return {
            "category": "Client Project",
            "priority": "High",
            "reason": "Custom Rule: Client Enquiry detected. Applied 'Client Enquiry' label.",
            "action_required": "Reply Needed",
            "summary": f"Client enquiry from {sender}: {snippet[:100]}...",
            "suggested_reply": f"Hello,\n\nThank you for reaching out to ZYBO Tech Lab regarding your enquiry. Ramesh R. and our project team have received your request and will provide a formal response shortly.\n\nBest regards,\nRamesh R.\nProject Head | ZYBO Tech Lab",
            "unsubscribe": unsubscribe_link,
            "is_enquiry": True,
            "is_whitelisted": is_whitelisted
        }

    # 1. Security Alerts
    if "wordfence" in sub_lower or "security" in sub_lower or "login alert" in sub_lower or "vulnerability" in sub_lower:
        return {
            "category": "Security Alert",
            "priority": "High",
            "reason": "Automated website security or firewall notification.",
            "action_required": "Review Required",
            "summary": f"Security warning regarding website activity from {sender}.",
            "suggested_reply": "Thank you. The technical team will review this security alert.",
            "unsubscribe": unsubscribe_link,
            "is_enquiry": False,
            "is_whitelisted": is_whitelisted
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
            "unsubscribe": unsubscribe_link,
            "is_enquiry": False,
            "is_whitelisted": is_whitelisted
        }

    # 3. Promotional, Newsletters & Digests
    if "e27" in send_lower or "reddit" in send_lower or "digest" in sub_lower or "newsletter" in sub_lower or "unsubscribe" in snip_lower or "offer" in sub_lower or "discount" in sub_lower or "promo" in sub_lower or "weekly" in sub_lower:
        return {
            "category": "Promotional",
            "priority": "Low",
            "reason": "Marketing digest, newsletter or promotional communication.",
            "action_required": "None",
            "summary": f"Promotional publication or digest: {subject}",
            "suggested_reply": "Offers noted.",
            "unsubscribe": unsubscribe_link,
            "is_enquiry": False,
            "is_whitelisted": is_whitelisted
        }

    # 4. Client Projects
    if "project" in sub_lower or "design" in sub_lower or "website" in sub_lower or "zybo" in sub_lower or "client" in sub_lower:
        return {
            "category": "Client Project",
            "priority": "High",
            "reason": "Client website or agency project correspondence.",
            "action_required": "Reply Needed",
            "summary": f"Client project communication: {snippet[:100]}...",
            "suggested_reply": "Hello,\n\nThank you for your update. Ramesh R. and the ZYBO team are reviewing your requirements and will get back to you shortly.\n\nBest regards,\nRamesh R.\nProject Head | ZYBO Tech Lab",
            "unsubscribe": unsubscribe_link,
            "is_enquiry": False,
            "is_whitelisted": is_whitelisted
        }

    # Default General
    return {
        "category": "General",
        "priority": "Medium",
        "reason": "Standard correspondence or notification.",
        "action_required": "None",
        "summary": snippet[:150] if snippet else "No summary available.",
        "suggested_reply": "Hello,\n\nThank you for reaching out to ZYBO Tech Lab. We have received your message and will respond shortly.\n\nBest regards,\nRamesh R.\nProject Head | ZYBO Tech Lab",
        "unsubscribe": unsubscribe_link,
        "is_enquiry": False,
        "is_whitelisted": is_whitelisted
    }

# --- AI Text Generator ---
def generate_ai_text(prompt_text):
    if GEMINI_API_KEY:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt_text,
            )
            if response and response.text:
                return response.text
        except Exception:
            pass

    return f"""Hello,

Regarding your request:

{prompt_text}

Here is the draft content prepared for review. Please let us know if any further adjustments are needed.

Best regards,
Ramesh R.
Project Head | ZYBO Tech Lab"""

# Initialize Session State
if "analyzed_emails" not in st.session_state:
    st.session_state.analyzed_emails = []

if "cleanup_history" not in st.session_state:
    st.session_state.cleanup_history = []

if "bulk_selected_ids" not in st.session_state:
    st.session_state.bulk_selected_ids = []

# --- UI Layout ---
st.title("✉️ ZYBO AI Email Agent Dashboard")
st.markdown("Automated Mail Categorization, Phishing & Spam Cleanup, Whitelisted Contacts, Task Extraction & Action History.")

# Sidebar Controls
with st.sidebar:
    st.subheader("⚙️ Scan & Search Controls")
    search_keyword = st.text_input("🔍 Smart Search (Sender, Subject, Keywords)", "")
    max_scan_limit = st.slider("Max emails to scan", 10, 500, 100)
    
    st.markdown("---")
    st.markdown("### 🛡️ Whitelist & Exclusion Contacts")
    whitelist_domains = st.text_area("Protected Senders/Domains (Comma separated):", "zybo.in, google.com, hostinger.com")

    st.markdown("---")
    st.markdown("### ⚙️ Custom Automation Rules")
    enquiry_auto_tag = st.checkbox("🏷️ Auto-assign 'Client Enquiry' label", value=True)
    enquiry_auto_reply = st.checkbox("⚡ Auto-generate enquiry reply drafts", value=True)
    
    st.markdown("---")
    st.markdown("### 🧹 Automation Cleanup")
    auto_trash_spam = st.checkbox("🗑️ Automatically trash Spam", value=True)
    auto_trash_promo = st.checkbox("🗑️ Automatically trash Promotions", value=True)
    require_confirmation = st.checkbox("🔒 Require confirmation before trashing", value=False)
    
    run_btn = st.button("🚀 Fetch, Analyze & Organize Mail", type="primary")
    scan_spam_btn = st.button("🚀 Scan Spam Separately", type="secondary")

    st.markdown("---")
    st.markdown("### ✍️ AI Email Composer")
    with st.expander("✨ Compose New Email", expanded=False):
        comp_to = st.text_input("Recipient Email:", key="comp_to")
        comp_subject = st.text_input("Subject:", key="comp_sub")
        comp_instructions = st.text_area("Instructions / Key Points:", key="comp_inst")
        comp_tone = st.selectbox("Tone:", ["Professional", "Friendly", "Formal", "Concise", "Customized"], key="comp_tone")
        
        if "composed_text" not in st.session_state:
            st.session_state.composed_text = ""

        if st.button("🤖 Generate Draft Content"):
            if comp_instructions:
                comp_prompt = f"Compose a complete email body in a {comp_tone} tone based on these instructions: {comp_instructions}. Recipient: {comp_to}, Subject: {comp_subject}."
                st.session_state.composed_text = generate_ai_text(comp_prompt)
            else:
                st.warning("Please enter instructions before generating.")

        if st.session_state.composed_text:
            edited_email = st.text_area("Review & Edit Draft Content:", value=st.session_state.composed_text, height=200, key="edited_comp")
            
            if st.button("📥 Save Draft to Gmail"):
                if comp_to:
                    service = get_gmail_service()
                    if service:
                        try:
                            from email.mime.text import MIMEText
                            msg = MIMEText(edited_email)
                            msg['Subject'] = comp_subject or "New Message from ZYBO Tech Lab"
                            msg['To'] = comp_to
                            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
                            
                            draft_body = {'message': {'raw': raw}}
                            service.users().drafts().create(userId='me', body=draft_body).execute()
                            st.success("Draft saved to Gmail successfully!")
                            
                            # Audit log
                            st.session_state.cleanup_history.append({
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "action": "Saved Draft",
                                "target": comp_to
                            })
                            st.session_state.composed_text = ""
                        except Exception as err:
                            st.error(f"Failed to save draft: {err}")
                else:
                    st.warning("Please provide a recipient email.")

# Execute regular scan
if run_btn:
    with st.spinner(f"Scanning up to {max_scan_limit} active emails (excluding Trash)..."):
        service = get_gmail_service()
        if service:
            messages = []
            page_token = None
            base_query = search_keyword.strip()
            q_query = f"-in:trash {base_query}".strip()

            try:
                while len(messages) < max_scan_limit:
                    fetch_size = min(50, max_scan_limit - len(messages))
                    results = service.users().messages().list(
                        userId='me', 
                        q=q_query, 
                        includeSpamTrash=False, 
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
                        thread_id = txt.get('threadId')
                        payload = txt.get('payload', {})
                        headers = payload.get('headers', [])
                        label_ids = txt.get('labelIds', [])
                        
                        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                        sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
                        snippet = txt.get('snippet', '')

                        classification = classify_email_locally(subject, sender, snippet, headers, label_ids, whitelist_domains)
                        security_warnings = inspect_security_and_links(subject, sender, snippet, payload)

                        category = classification["category"]
                        tasks, deadlines = extract_tasks_and_deadlines(subject, snippet)
                        meeting_info = detect_meeting_details(subject, snippet)

                        needs_followup = classification["action_required"] == "Reply Needed"

                        email_data = {
                            "id": msg_id,
                            "threadId": thread_id,
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
                            "is_enquiry": classification.get("is_enquiry", False),
                            "is_whitelisted": classification.get("is_whitelisted", False),
                            "security_warnings": security_warnings,
                            "tasks": tasks,
                            "deadlines": deadlines,
                            "meeting": meeting_info,
                            "needs_followup": needs_followup,
                            "is_starred": 'STARRED' in label_ids,
                            "is_unread": 'UNREAD' in label_ids,
                            "trashed": False
                        }

                        # Protect Whitelisted senders from auto-trashing
                        should_trash = (category == "Spam" and auto_trash_spam) or (category == "Promotional" and auto_trash_promo)
                        if should_trash and not require_confirmation and not email_data["is_whitelisted"]:
                            try:
                                service.users().messages().trash(userId='me', id=msg_id).execute()
                                email_data["trashed"] = True
                                st.session_state.cleanup_history.append({
                                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "action": "Auto-Trashed",
                                    "target": f"{subject} ({sender})"
                                })
                                time.sleep(0.3)
                            except Exception:
                                pass

                        processed_items.append(email_data)
                        
                        if idx > 0 and idx % 20 == 0:
                            time.sleep(0.2)
                            
                    except Exception:
                        continue

                st.session_state.analyzed_emails = processed_items
                st.success(f"Successfully processed {len(processed_items)} emails across your account!")

# Execute standalone spam scan
if scan_spam_btn:
    with st.spinner("Scanning Spam folder separately..."):
        service = get_gmail_service()
        if service:
            try:
                results = service.users().messages().list(
                    userId='me', 
                    q='in:spam', 
                    includeSpamTrash=True, 
                    maxResults=100
                ).execute()
                
                messages = results.get('messages', [])
                if not messages:
                    st.info("No spam messages found.")
                    st.session_state.analyzed_emails = []
                else:
                    processed_items = []
                    for msg in messages:
                        msg_id = msg['id']
                        try:
                            txt = service.users().messages().get(userId='me', id=msg_id).execute()
                            thread_id = txt.get('threadId')
                            payload = txt.get('payload', {})
                            headers = payload.get('headers', [])
                            label_ids = txt.get('labelIds', [])
                            
                            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
                            snippet = txt.get('snippet', '')

                            email_data = {
                                "id": msg_id,
                                "threadId": thread_id,
                                "sender": sender,
                                "subject": subject,
                                "snippet": snippet,
                                "category": "Spam",
                                "priority": "Low",
                                "reason": "Retrieved from Spam folder scan.",
                                "action_required": "None",
                                "summary": snippet[:150],
                                "suggested_reply": "N/A",
                                "unsubscribe": "",
                                "is_enquiry": False,
                                "is_whitelisted": False,
                                "security_warnings": [],
                                "tasks": [],
                                "deadlines": [],
                                "meeting": {"is_meeting": False},
                                "needs_followup": False,
                                "is_starred": 'STARRED' in label_ids,
                                "is_unread": 'UNREAD' in label_ids,
                                "trashed": False
                            }

                            if auto_trash_spam and not require_confirmation:
                                try:
                                    service.users().messages().trash(userId='me', id=msg_id).execute()
                                    email_data["trashed"] = True
                                    time.sleep(0.3)
                                except Exception:
                                    pass

                            processed_items.append(email_data)
                        except Exception:
                            continue

                    st.session_state.analyzed_emails = processed_items
                    st.success(f"Successfully scanned {len(processed_items)} spam messages!")
            except Exception as err:
                st.error(f"Error scanning spam folder: {err}")

# Scheduled Briefing Banner & Notifications
if st.session_state.analyzed_emails:
    emails = st.session_state.analyzed_emails
    
    with st.expander("🔔 Scheduled Briefing & Notifications Summary", expanded=True):
        col_b1, col_b2, col_b3 = st.columns(3)
        high_prio_items = [e for e in emails if e['priority'] == 'High']
        pending_followups = [e for e in emails if e['needs_followup']]
        task_items = [e for e in emails if e['tasks'] or e['deadlines']]

        with col_b1:
            st.markdown(f"**🔥 Urgent Alerts ({len(high_prio_items)}):**")
            for h_item in high_prio_items[:3]:
                st.caption(f"• {h_item['subject']} ({h_item['sender']})")

        with col_b2:
            st.markdown(f"**⏳ Overdue Follow-ups ({len(pending_followups)}):**")
            for f_item in pending_followups[:3]:
                st.caption(f"• {f_item['subject']}")

        with col_b3:
            st.markdown(f"**📌 Tasks & Deadlines ({len(task_items)}):**")
            for t_item in task_items[:3]:
                st.caption(f"• {t_item['subject'][:40]}...")

    # Action History & Bulk Options Bar
    with st.expander("📜 Cleanup History & Bulk Preview Actions", expanded=False):
        col_h1, col_h2 = st.columns([2, 1])
        with col_h1:
            st.markdown("### Action History Log")
            if st.session_state.cleanup_history:
                for entry in list(reversed(st.session_state.cleanup_history))[:10]:
                    st.text(f"[{entry['time']}] {entry['action']}: {entry['target']}")
            else:
                st.caption("No cleanup actions recorded in this session.")
        with col_h2:
            st.markdown("### Bulk Actions")
            if st.button("🗑️ Trash Selected Preview Emails"):
                service = get_gmail_service()
                if service and st.session_state.bulk_selected_ids:
                    count = 0
                    for m_id in st.session_state.bulk_selected_ids:
                        try:
                            service.users().messages().trash(userId='me', id=m_id).execute()
                            count += 1
                        except Exception:
                            pass
                    st.success(f"Trashed {count} selected emails!")
                    st.session_state.analyzed_emails = [e for e in st.session_state.analyzed_emails if e['id'] not in st.session_state.bulk_selected_ids]
                    st.session_state.bulk_selected_ids = []
                    st.rerun()

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
                st.info(f"No emails found in this category.")
            else:
                for item in items:
                    with st.container(border=True):
                        c1, c2, c3 = st.columns([0.2, 2.8, 1])
                        
                        with c1:
                            # Bulk select checkbox
                            is_selected = st.checkbox("", key=f"chk_{item['id']}")
                            if is_selected and item['id'] not in st.session_state.bulk_selected_ids:
                                st.session_state.bulk_selected_ids.append(item['id'])
                            elif not is_selected and item['id'] in st.session_state.bulk_selected_ids:
                                st.session_state.bulk_selected_ids.remove(item['id'])

                        with c2:
                            st.markdown(f"**From:** {item['sender']}")
                            st.markdown(f"**Subject:** {item['subject']}")
                            if item.get("is_enquiry", False):
                                st.markdown("🏷️ `Client Enquiry`")
                            if item.get("is_whitelisted", False):
                                st.markdown("🛡️ `Whitelisted Contact (Protected)`")

                        with c3:
                            prio_color = "red" if item['priority'] == 'High' else ("orange" if item['priority'] == 'Medium' else "green")
                            st.markdown(f"**Priority:** :{prio_color}[{item['priority']}]")
                            st.markdown(f"**Action:** `{item['action_required']}`")
                        
                        st.markdown(f"**AI Summary:** {item['summary']}")
                        st.caption(f"Reason: {item['reason']}")

                        # Security and Phishing Warning Box
                        if item.get("security_warnings"):
                            st.markdown("---")
                            for warn in item["security_warnings"]:
                                st.error(warn)

                        # Task & Deadline Extraction Display
                        if item['tasks'] or item['deadlines']:
                            st.markdown("---")
                            st.markdown("**📋 Extracted Tasks & Deadlines:**")
                            for t in item['tasks']:
                                st.markdown(f"- 📌 Task: {t}")
                            for d in item['deadlines']:
                                st.markdown(f"- ⏰ Deadline: {d}")

                        # Meeting Automation Display
                        if item['meeting']['is_meeting']:
                            st.markdown("---")
                            st.markdown("**📅 Meeting Automation:**")
                            st.info(f"Detected Meeting Request: {item['meeting']['title']} (Suggested: {item['meeting']['suggested_time']})")
                            if st.button("📅 Add Event to Google Calendar", key=f"cal_{item['id']}"):
                                cal_service = get_calendar_service()
                                if cal_service:
                                    try:
                                        start_time = (datetime.utcnow() + timedelta(days=1)).isoformat() + 'Z'
                                        end_time = (datetime.utcnow() + timedelta(days=1, hours=1)).isoformat() + 'Z'
                                        event = {
                                            'summary': item['meeting']['title'],
                                            'description': f"Automated meeting invite created from email snippet: {item['snippet']}",
                                            'start': {'dateTime': start_time},
                                            'end': {'dateTime': end_time},
                                        }
                                        cal_service.events().insert(calendarId='primary', body=event).execute()
                                        st.success("Meeting event added to Google Calendar!")
                                    except Exception as cal_err:
                                        st.error(f"Calendar error: {cal_err}")

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
                                    st.session_state.cleanup_history.append({
                                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        "action": "Archived",
                                        "target": f"{item['subject']}"
                                    })
                                    st.success("Archived!")

                        with btn_col4:
                            unsub_link = item.get('unsubscribe', '')
                            if unsub_link:
                                if st.button("🔕 Unsubscribe", key=f"unsub_{item['id']}"):
                                    st.session_state.cleanup_history.append({
                                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        "action": "Unsubscribed",
                                        "target": f"{item['sender']}"
                                    })
                                    st.markdown(f"[Click here to Unsubscribe]({unsub_link})", unsafe_allow_html=True)
                            else:
                                st.button("🔕 Unsubscribe", key=f"unsub_{item['id']}", disabled=True)

                        with btn_col5:
                            if st.button("🗑️ Delete", key=f"del_{item['id']}"):
                                if service:
                                    try:
                                        service.users().messages().trash(userId='me', id=item['id']).execute()
                                        item['trashed'] = True
                                        st.session_state.cleanup_history.append({
                                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                            "action": "Trashed",
                                            "target": f"{item['subject']}"
                                        })
                                        st.session_state.analyzed_emails = [e for e in st.session_state.analyzed_emails if e['id'] != item['id']]
                                        st.success("Deleted from Gmail successfully!")
                                        st.rerun()
                                    except Exception as err:
                                        st.error(f"Delete Error: {err}")

                        # Advanced AI Reply, Tone & Grammar Expander
                        with st.expander("✨ AI Reply Generator, Follow-up Automation & Tone Controls"):
                            selected_tone = st.selectbox(
                                "Select Tone:", 
                                ["Professional", "Friendly", "Formal", "Concise", "Customized"], 
                                key=f"tone_{item['id']}"
                            )
                            
                            custom_instructions = st.text_input(
                                "Customization / Specific instruction (optional):", 
                                key=f"inst_{item['id']}"
                            )
                            
                            current_reply = item.get('suggested_reply', '')
                            
                            col_ai1, col_ai2, col_ai3, col_ai4 = st.columns(4)
                            with col_ai1:
                                if st.button("🤖 Generate AI Reply", key=f"gen_{item['id']}"):
                                    prompt_ai = f"Write a reply to this email from {item['sender']} with subject '{item['subject']}' and snippet '{item['snippet']}'. Use a {selected_tone} tone. Additional instruction: {custom_instructions}."
                                    current_reply = generate_ai_text(prompt_ai)
                                    item['suggested_reply'] = current_reply
                                    st.rerun()
                            with col_ai2:
                                if st.button("✨ Improve Grammar", key=f"gram_{item['id']}"):
                                    prompt_ai = f"Improve the grammar, clarity, and phrasing of this draft reply in a {selected_tone} tone: '{current_reply}'"
                                    current_reply = generate_ai_text(prompt_ai)
                                    item['suggested_reply'] = current_reply
                                    st.rerun()
                            with col_ai3:
                                if st.button("📉 Shorten", key=f"short_{item['id']}"):
                                    prompt_ai = f"Make this draft reply more concise and shorter while keeping its meaning: '{current_reply}'"
                                    current_reply = generate_ai_text(prompt_ai)
                                    item['suggested_reply'] = current_reply
                                    st.rerun()
                            with col_ai4:
                                if st.button("🔄 Follow-up Draft", key=f"fol_{item['id']}"):
                                    prompt_ai = f"Compose a polite follow-up draft checking in on the status of this email: '{item['subject']}' sent to {item['sender']}."
                                    current_reply = generate_ai_text(prompt_ai)
                                    item['suggested_reply'] = current_reply
                                    st.rerun()

                            reply_text = st.text_area("Edit Draft Reply:", value=item.get('suggested_reply', ''), key=f"reply_{item['id']}")
                            
                            if st.button("📥 Save Draft to Gmail (In-Thread)", key=f"draft_{item['id']}"):
                                if service:
                                    try:
                                        from email.mime.text import MIMEText
                                        msg = MIMEText(reply_text)
                                        msg['Subject'] = f"Re: {item['subject']}"
                                        msg['To'] = item['sender']
                                        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
                                        
                                        draft_body = {
                                            'message': {
                                                'threadId': item['threadId'],
                                                'raw': raw
                                            }
                                        }
                                        service.users().drafts().create(userId='me', body=draft_body).execute()
                                        st.session_state.cleanup_history.append({
                                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                            "action": "Saved Reply Draft",
                                            "target": f"{item['subject']}"
                                        })
                                        st.success("Successfully saved as an in-thread draft in your Gmail account!")
                                    except Exception as err:
                                        st.error(f"Failed to create draft: {err}")
else:
    st.info("👈 Click **'🚀 Fetch, Analyze & Organize Mail'** or **'🚀 Scan Spam Separately'** in the sidebar to begin.")
