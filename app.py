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

# Automatically generate credentials.json on the fly
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

# Automatically generate token.json on the fly for cloud authentication
if not os.path.exists('token.json') and 'GMAIL_TOKEN' in st.secrets:
    token_dict = dict(st.secrets['GMAIL_TOKEN'])
    with open('token.json', 'w') as f:
        json.dump(token_dict, f)

def get_gmail_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            st.error("Authentication token is missing or invalid. Please check your Streamlit Secrets.")
            return None
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def analyze_email_with_gemini(subject, sender, snippet):
    if not GEMINI_API_KEY:
        return "Error: Gemini API Key is missing in Streamlit Secrets."
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    Analyze this email and categorize it into one of these buckets: 
    [Urgent, Client Project, Security Alert, Spam, General]. 
    Also provide a 1-sentence summary.
    
    Sender: {sender}
    Subject: {subject}
    Snippet: {snippet}
    """
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
    )
    return response.text

# --- UI Layout ---
st.title("✉️ ZYBO AI Email Agent")
st.markdown("Automated Inbox Categorization & Summary Dashboard powered by Gemini.")

col1, col2 = st.columns([1, 3])

with col1:
    st.subheader("Controls")
    max_emails = st.slider("Number of emails to scan", 1, 10, 5)
    run_btn = st.button("🚀 Fetch & Analyze Inbox", type="primary")

with col2:
    st.subheader("Live Analysis Feed")
    if run_btn:
        if not GEMINI_API_KEY:
            st.error("Please configure your GEMINI_API_KEY in Streamlit Secrets.")
        else:
            with st.spinner("Connecting to Gmail and running Gemini AI analysis..."):
                service = get_gmail_service()
                if service:
                    results = service.users().messages().list(userId='me', q='label:unread', maxResults=max_emails).execute()
                    messages = results.get('messages', [])

                    if not messages:
                        st.info("No unread messages found anywhere in your mailbox.")
                    else:
                        st.success(f"Found {len(messages)} unread email(s).")
                        
                        for msg in messages:
                            txt = service.users().messages().get(userId='me', id=msg['id']).execute()
                            headers = txt['payload']['headers']
                            
                            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
                            snippet = txt.get('snippet', '')

                            ai_analysis = analyze_email_with_gemini(subject, sender, snippet)

                            with st.container(border=True):
                                st.markdown(f"**From:** {sender}")
                                st.markdown(f"**Subject:** {subject}")
                                st.markdown(f"**Snippet:** _{snippet}_")
                                st.markdown("---")
                                st.markdown(f"**AI Analysis & Sorting:**\n\n{ai_analysis}")
