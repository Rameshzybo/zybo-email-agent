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
st.set_page_config(page_title="ZYBO Advanced AI Email Agent Dashboard", page_icon="✉️", layout="wide")

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

    dangerous_exts = ['.exe', '.scr', '.vbs', '.js', '.bat', '.cmd', '.msi', '.ps1', '.zip']
    parts = payload.get('parts', [])
    for p in parts:
        fname = p.get('filename', '').lower()
        if any(fname.endswith(ext) for ext in dangerous_exts):
            warnings.append(f"⚠️ Potentially dangerous attachment detected: `{fname}`")

    if any(k in combined for k in ["verify your account", "login urgently", "password expired", "click here immediately", "bank transfer", "wire funds"]):
        warnings.append("⚠️ Phishing indicator: Urgent credential or financial action requested.")

    urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', combined)
    for url in urls:
        if re.search(r'https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', url):
            warnings.append(f"⚠️ Suspicious IP-based URL detected: `{url}`")

    return warnings

# --- Attachment & Document Intelligence Handling ---
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
            return filename, f"[Excel Sheet Data]: {'; '.join(rows[:5])}"

        return filename, f"[Attached File]: {filename}"
    except Exception as e:
        return filename, f"Attachment parse error"

# --- Invoice Processing & Intelligence ---
def process_invoice_data(subject, snippet):
    combined = f"{subject} {snippet}".lower()
    if any(k in combined for k in ["invoice", "bill", "payment due", "receipt", "amount due"]):
        amt_match = re.search(r'(\$|₹|USD|INR)\s?(\d+[\d,]*(\.\d+)?)|(\d+[\d,]*(\.\d+)?)\s?(USD|INR)', snippet)
        inv_num = re.search(r'inv[-_#]?\d+', combined)
        return {
            "is_invoice": True,
            "invoice_num": inv_num.group(0).upper() if inv_num else "INV-DETECTED",
            "amount": amt_match.group(0) if amt_match else "Amount pending review",
            "status": "Pending Payment"
        }
    return {"is_invoice": False}

# --- Relationship & Lead Management ---
def classify_relationship_and_lead(sender, subject, snippet):
    combined = f"{subject} {snippet}".lower()
    
    rel_type = "Vendor / Service"
    if "zybo" in sender.lower() or "employee" in combined:
        rel_type = "Team Member"
    elif any(k in combined for k in ["enquiry", "quote", "proposal", "requirement", "pricing", "cost"]):
        rel_type = "Prospective Lead"
    elif any(k in combined for k in ["project", "website", "client", "revision", "update"]):
        rel_type = "Client"

    lead_info = None
    if rel_type == "Prospective Lead":
        lead_info = {
            "lead_name": sender.split("<")[0].strip(),
            "contact": sender,
            "requirements": snippet[:120] + "...",
            "next_step": "Send Proposal / Customization Details"
        }

    return rel_type, lead_info

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

# Initialize Session State Variables
if "analyzed_emails" not in st.session_state:
    st.session_state.analyzed_emails = []

if "cleanup_history" not in st.session_state:
    st.session_state.cleanup_history = []

if "bulk_selected_ids" not in st.session_state:
    st.session_state.bulk_selected_ids = []

if "drafts_saved_count" not in st.session_state:
    st.session_state.drafts_saved_count = 0

if "ai_replies_generated_count" not in st.session_state:
    st.session_state.ai_replies_generated_count = 0

if "email_memory" not in st.session_state:
    st.session_state.email_memory = {
        "agency_name": "ZYBO Tech Lab",
        "project_head": "Ramesh R.",
        "preferred_language": "English / Multilingual",
        "custom_instructions": "Maintain customization focus over default templates."
    }

if "team_assignments" not in st.session_state:
    st.session_state.team_assignments = {}

if "nl_rules" not in st.session_state:
    st.session_state.nl_rules = []

# --- UI Layout ---
st.title("✉️ ZYBO Advanced AI Email Agent Dashboard")
st.markdown("Automated Mail Management, Conversational Assistant, Relationship & Lead Intelligence, Invoice Processing & Team Workflows.")

# Sidebar Controls
with st.sidebar:
    st.subheader("🌐 Account & Scan Controls")
    account_selector = st.selectbox("Select Account:", ["Primary (Ramesh R. - ZYBO)", "Support Account", "Billing Account"])
    search_keyword = st.text_input("🔍 Smart Search (Sender, Subject, Keywords)", "")
    max_scan_limit = st.slider("Max emails to scan", 10, 500, 100)
    
    st.markdown("---")
    st.markdown("### 🧠 Email Agent Memory")
    with st.expander("⚙️ Manage Agent Preferences", expanded=False):
        mem_agency = st.text_input("Agency Name:", value=st.session_state.email_memory["agency_name"])
        mem_head = st.text_input("Project Head:", value=st.session_state.email_memory["project_head"])
        mem_notes = st.text_area("Persistent Rules / Preferences:", value=st.session_state.email_memory["custom_instructions"])
        if st.button("💾 Update Memory"):
            st.session_state.email_memory["agency_name"] = mem_agency
            st.session_state.email_memory["project_head"] = mem_head
            st.session_state.email_memory["custom_instructions"] = mem_notes
            st.success("Agent Memory Updated!")

    st.markdown("---")
    st.markdown("### ⚡ Natural Language Rule Builder")
    with st.expander("🛠️ Build Natural Language Rules", expanded=False):
        nl_input = st.text_input("Type Rule Instruction:", "If email is from wordfence, assign High priority and tag Security Alert.")
        if st.button("➕ Add Rule"):
            st.session_state.nl_rules.append(nl_input)
            st.success("Custom Natural Language Rule Saved!")
        if st.session_state.nl_rules:
            st.caption("Active NL Rules:")
            for r in st.session_state.nl_rules:
                st.caption(f"• {r}")

    st.markdown("---")
    st.markdown("### 🛡️ Whitelist & Exclusion Contacts")
    whitelist_domains = st.text_area("Protected Senders/Domains:", "zybo.in, google.com, hostinger.com")

    st.markdown("---")
    st.markdown("### 🧹 Automation Cleanup")
    auto_trash_spam = st.checkbox("🗑️ Automatically trash Spam", value=True)
    auto_trash_promo = st.checkbox("🗑️ Automatically trash Promotions", value=True)
    require_confirmation = st.checkbox("🔒 Require confirmation before trashing", value=False)
    
    run_btn = st.button("🚀 Fetch, Analyze & Organize Mail", type="primary")
    scan_spam_btn = st.button("🚀 Scan Spam Separately", type="secondary")

# Execute regular scan
if run_btn:
    with st.spinner(f"Scanning up to {max_scan_limit} active emails for {account_selector}..."):
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
                        relationship, lead_details = classify_relationship_and_lead(sender, subject, snippet)
                        invoice_data = process_invoice_data(subject, snippet)

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
                            "relationship": relationship,
                            "lead_details": lead_details,
                            "invoice_data": invoice_data,
                            "is_enquiry": classification.get("is_enquiry", False),
                            "is_whitelisted": classification.get("is_whitelisted", False),
                            "security_warnings": security_warnings,
                            "is_starred": 'STARRED' in label_ids,
                            "is_unread": 'UNREAD' in label_ids,
                            "trashed": False
                        }

                        should_trash = (category == "Spam" and auto_trash_spam) or (category == "Promotional" and auto_trash_promo)
                        if should_trash and not require_confirmation and not email_data["is_whitelisted"]:
                            try:
                                service.users().messages().trash(userId='me', id=msg_id).execute()
                                email_data["trashed"] = True
                                time.sleep(0.3)
                            except Exception:
                                pass

                        processed_items.append(email_data)
                        if idx > 0 and idx % 20 == 0:
                            time.sleep(0.2)
                            
                    except Exception:
                        continue

                st.session_state.analyzed_emails = processed_items
                st.success(f"Successfully processed {len(processed_items)} emails for {account_selector}!")

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
                                "relationship": "Vendor / Service",
                                "lead_details": None,
                                "invoice_data": {"is_invoice": False},
                                "is_enquiry": False,
                                "is_whitelisted": False,
                                "security_warnings": [],
                                "is_starred": 'STARRED' in label_ids,
                                "is_unread": 'UNREAD' in label_ids,
                                "trashed": False
                            }
                            processed_items.append(email_data)
                        except Exception:
                            continue

                    st.session_state.analyzed_emails = processed_items
                    st.success(f"Successfully scanned {len(processed_items)} spam messages!")
            except Exception as err:
                st.error(f"Error scanning spam folder: {err}")

# Main Navigation Tabs
main_tabs = st.tabs([
    "💬 Conversational Assistant", 
    "📊 Analytics & Dashboard", 
    "💼 Relationship & Leads", 
    "🧾 Invoice & Documents", 
    "📥 Active Inbox Feed"
])

# --- Tab 1: Conversational Email Assistant ---
with main_tabs[0]:
    st.subheader("💬 Conversational Email Assistant")
    st.markdown("Ask natural language questions about your scanned inbox (e.g., *'What did the client say about the project deadline?'* or *'Show me all pending invoices'*).")
    
    user_query = st.text_input("Ask your Inbox Assistant:", key="agent_chat_input")
    if st.button("🔍 Query Assistant", type="primary"):
        if user_query and st.session_state.analyzed_emails:
            inbox_context = "\n".join([f"- From: {e['sender']} | Subject: {e['subject']} | Snippet: {e['snippet']}" for e in st.session_state.analyzed_emails[:25]])
            prompt = f"Answer this question based on the user's inbox content below:\nQuestion: {user_query}\n\nInbox Content:\n{inbox_context}"
            answer = generate_ai_text(prompt)
            st.info(f"**Assistant Response:**\n\n{answer}")
        else:
            st.warning("Please scan your inbox first or enter a query.")

# --- Tab 2: Dashboard & Analytics ---
with main_tabs[1]:
    st.subheader("📊 Comprehensive Dashboard & Analytics Suite")
    if st.session_state.analyzed_emails:
        emails = st.session_state.analyzed_emails
        col_r1, col_r2 = st.columns(2)
        with col_r1:
            st.metric("Total Scanned", len(emails))
            st.metric("Unread Messages", sum(1 for e in emails if e['is_unread']))
        with col_r2:
            st.metric("High Priority", sum(1 for e in emails if e['priority'] == 'High'))
            st.metric("AI Replies Generated", st.session_state.ai_replies_generated_count)
    else:
        st.info("Run a scan from the sidebar to view analytics.")

# --- Tab 3: Relationship & Lead Management ---
with main_tabs[2]:
    st.subheader("💼 Relationship & Prospective Lead Intelligence")
    if st.session_state.analyzed_emails:
        leads = [e for e in st.session_state.analyzed_emails if e.get("lead_details")]
        if leads:
            for l in leads:
                with st.container(border=True):
                    ld = l["lead_details"]
                    st.markdown(f"**Lead Name:** {ld['lead_name']}")
                    st.markdown(f"**Contact:** `{ld['contact']}`")
                    st.markdown(f"**Requirements:** {ld['requirements']}")
                    st.info(f"**Suggested Next Step:** {ld['next_step']}")
        else:
            st.info("No prospective leads extracted from current scan.")
    else:
        st.info("Run a scan from the sidebar to view leads.")

# --- Tab 4: Invoice & Document Intelligence ---
with main_tabs[3]:
    st.subheader("🧾 Invoice Processing & Attachment Intelligence")
    if st.session_state.analyzed_emails:
        invoices = [e for e in st.session_state.analyzed_emails if e.get("invoice_data", {}).get("is_invoice")]
        if invoices:
            for inv in invoices:
                with st.container(border=True):
                    idata = inv["invoice_data"]
                    st.markdown(f"**Vendor:** {inv['sender']}")
                    st.markdown(f"**Invoice Number:** `{idata['invoice_num']}`")
                    st.markdown(f"**Detected Amount:** `{idata['amount']}`")
                    st.caption(f"Status: {idata['status']}")
        else:
            st.info("No pending invoices detected in current scan.")
    else:
        st.info("Run a scan from the sidebar to view invoices.")

# --- Tab 5: Active Inbox Feed ---
with main_tabs[4]:
    if st.session_state.analyzed_emails:
        for item in st.session_state.analyzed_emails:
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                with c1:
                    st.markdown(f"**From:** {item['sender']} (`{item['relationship']}`)")
                    st.markdown(f"**Subject:** {item['subject']}")
                with c2:
                    st.markdown(f"**Priority:** `{item['priority']}`")
                
                st.markdown(f"**AI Summary:** {item['summary']}")
                
                assignee = st.selectbox(
                    "Assign Team Member:", 
                    ["Unassigned", "Ramesh R. (Project Head)", "Rajeev", "Technical Team"], 
                    key=f"team_{item['id']}"
                )
                st.session_state.team_assignments[item['id']] = assignee
    else:
        st.info("👈 Click **'🚀 Fetch, Analyze & Organize Mail'** in the sidebar to begin.")
