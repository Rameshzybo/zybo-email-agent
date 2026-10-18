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
        return "Category: General\nSummary: Gemini API Key missing."
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    Analyze this email and strictly follow this output format:
    Category: [Urgent, Client Project, Security Alert, Spam, General]
    Summary: [1-sentence summary]
    
    Sender: {sender}
    Subject: {subject}
    Snippet: {snippet}
    """
    # Using the universal stable latest flash endpoint
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=prompt,
    )
    return response.text

# --- UI Layout ---
st.title("✉️ ZYBO AI Email Agent")
st.markdown("Automated Inbox Categorization, Cleanup & Summary Dashboard powered by Gemini.")

col1, col2 = st.columns([1, 3])

with col1:
    st.subheader("Controls")
    max_emails = st.slider("Number of emails to scan", 1, 15, 5)
    auto_trash_spam = st.checkbox("🗑️ Automatically trash Spam emails", value=False)
    run_btn = st.button("🚀 Fetch, Sort & Clean Inbox", type="primary")

with col2:
    st.subheader("Live Categorized Feed")
    if run_btn:
        if not GEMINI_API_KEY:
            st.error("Please configure your GEMINI_API_KEY in Streamlit Secrets.")
        else:
            with st.spinner("Connecting to Gmail, analyzing via Gemini, and sorting..."):
                service = get_gmail_service()
                if service:
                    results = service.users().messages().list(userId='me', q='label:unread', maxResults=max_emails).execute()
                    messages = results.get('messages', [])

                    if not messages:
                        st.info("No unread messages found in your mailbox.")
                    else:
                        st.success(f"Successfully processed {len(messages)} unread email(s).")
                        
                        categorized_bins = {
                            "Urgent": [],
                            "Client Project": [],
                            "Security Alert": [],
                            "General": [],
                            "Spam": []
                        }

                        for msg in messages:
                            msg_id = msg['id']
                            txt = service.users().messages().get(userId='me', id=msg_id).execute()
                            headers = txt['payload']['headers']
                            
                            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
                            snippet = txt.get('snippet', '')

                            ai_response = analyze_email_with_gemini(subject, sender, snippet)
                            
                            category = "General"
                            summary = ai_response
                            for line in ai_response.split('\n'):
                                if "Category:" in line:
                                    clean_cat = line.replace("Category:", "").strip().replace("[", "").replace("]", "")
                                    if clean_cat in categorized_bins:
                                        category = clean_cat
                                elif "Summary:" in line:
                                    summary = line.replace("Summary:", "").strip()

                            email_data = {
                                "id": msg_id,
                                "sender": sender,
                                "subject": subject,
                                "snippet": snippet,
                                "summary": summary
                            }

                            if category == "Spam" and auto_trash_spam:
                                try:
                                    service.users().messages().trash(userId='me', id=msg_id).execute()
                                    email_data["trashed"] = True
                                except Exception:
                                    email_data["trashed"] = False
                            else:
                                email_data["trashed"] = False

                            categorized_bins[category].append(email_data)

                        tabs = st.tabs(["🚀 Urgent", "💼 Client Project", "🛡️ Security Alert", "📂 General", "🗑️ Spam"])
                        
                        bin_keys = ["Urgent", "Client Project", "Security Alert", "General", "Spam"]
                        for i, tab in enumerate(tabs):
                            current_cat = bin_keys[i]
                            items = categorized_bins[current_cat]
                            
                            with tab:
                                if not items:
                                    st.info(f"No emails in this category.")
                                else:
                                    for item in items:
                                        with st.container(border=True):
                                            st.markdown(f"**From:** {item['sender']}")
                                            st.markdown(f"**Subject:** {item['subject']}")
                                            st.markdown(f"**Snippet:** _{item['snippet']}_")
                                            st.markdown("---")
                                            st.markdown(f"**AI Summary:** {item['summary']}")
                                            if item['trashed']:
                                                st.error("🗑️ *Automatically moved to Trash (identified as Spam).*")
