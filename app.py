import json
import os
import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google import genai

# Page Configuration
st.set_page_config(page_title="ZYBO Email Agent", page_icon="✉️", layout="wide")

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

def analyze_email_with_gemini(subject, sender, snippet):
    if not GEMINI_API_KEY:
        return "Category: General\nPriority: Low\nReason: API key missing\nAction Required: None\nSummary: Gemini API Key missing."
    
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
        Analyze this email and strictly follow this format:
        Category: [Urgent, Client Project, Security Alert, Promotional, Spam, General]
        Priority: [High, Medium, Low]
        Reason: [1 short sentence explaining priority]
        Action Required: [None, Reply Needed, Review Required, Deadlines]
        Summary: [1-sentence summary]
        Suggested Reply: [A professional draft response]

        Sender: {sender}
        Subject: {subject}
        Snippet: {snippet}
        """
        # Updated to gemini-3.8-flash as mandated by the API error response
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return f"Category: General\nPriority: Low\nReason: Error processing\nAction Required: None\nSummary: Error ({e})\nSuggested Reply: N/A"

# Initialize session state for persistent results
if "analyzed_emails" not in st.session_state:
    st.session_state.analyzed_emails = []

# --- UI Layout ---
st.title("✉️ ZYBO AI Email Agent Dashboard")
st.markdown("Advanced 24/7 Mail Categorization, Smart Priority Detection, Automated Cleanup & Reply Generator.")

# Sidebar Controls
with st.sidebar:
    st.subheader("⚙️ Agent Controls")
    max_scan_limit = st.slider("Max emails to scan", 10, 200, 50)
    
    st.markdown("---")
    st.markdown("### 🧹 Automation Cleanup")
    auto_trash_spam = st.checkbox("🗑️ Automatically trash Spam", value=True)
    auto_trash_promo = st.checkbox("🗑️ Automatically trash Promotions", value=True)
    require_confirmation = st.checkbox("🔒 Require manual review before trashing", value=False)
    
    run_btn = st.button("🚀 Fetch, Sort & Clean All Mail", type="primary")

# Execute scan when button is pressed
if run_btn:
    if not GEMINI_API_KEY:
        st.error("Please configure your GEMINI_API_KEY in Streamlit Secrets.")
    else:
        with st.spinner("Scanning Inbox, Spam, & Archives, analyzing with Gemini, and sorting..."):
            service = get_gmail_service()
            if service:
                messages = []
                page_token = None
                
                # Scan across all folders including Spam and Trash explicitly
                while len(messages) < max_scan_limit:
                    results = service.users().messages().list(
                        userId='me', 
                        q='in:anywhere', 
                        includeSpamTrash=True, 
                        maxResults=min(50, max_scan_limit - len(messages)),
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
                    st.info("No messages found in your mailbox.")
                    st.session_state.analyzed_emails = []
                else:
                    processed_items = []
                    for msg in messages:
                        msg_id = msg['id']
                        try:
                            txt = service.users().messages().get(userId='me', id=msg_id).execute()
                            headers = txt.get('payload', {}).get('headers', [])
                            
                            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
                            snippet = txt.get('snippet', '')
                            label_ids = txt.get('labelIds', [])

                            ai_response = analyze_email_with_gemini(subject, sender, snippet)
                            
                            # Default parsing
                            category = "General"
                            priority = "Low"
                            reason = "Routine notification"
                            action_req = "None"
                            summary = snippet
                            suggested_reply = "Thank you for reaching out."

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

                            # Force classification override if Gmail already marks it as SPAM
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
                                "trashed": False
                            }

                            # Perform Trashing if conditions match
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

# Render Dashboard Metrics & Feed if data exists
if st.session_state.analyzed_emails:
    emails = st.session_state.analyzed_emails
    
    # Dashboard Metrics Row
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

    # Tabs for Categorization
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
                            st.error("🗑️ *Automatically moved to Trash.*")
                        
                        # Expandable section for AI Reply & Draft creation
                        with st.expander("✨ AI Reply Generator & Gmail Draft"):
                            reply_text = st.text_area("Edit Draft Reply:", value=item['suggested_reply'], key=f"reply_{item['id']}")
                            if st.button("📥 Save Draft to Gmail", key=f"draft_{item['id']}"):
                                service = get_gmail_service()
                                if service:
                                    try:
                                        from email.mime.text import MIMEText
                                        import base64
                                        
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
    st.info("👈 Click **'🚀 Fetch, Sort & Clean All Mail'** in the sidebar to start analyzing and cleaning your inbox.")
