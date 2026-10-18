import base64
import json
import os
from datetime import datetime
from email.mime.text import MIMEText

import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google import genai

# ---------------------------------------------------------
# ZYBO AI EMAIL AGENT
# ---------------------------------------------------------
st.set_page_config(page_title="ZYBO AI Email Agent", page_icon="✉️", layout="wide")

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
GEMINI_MODEL = "gemini-2.5-flash"

GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

st.title("✉️ ZYBO AI Email Agent")
st.caption("AI inbox intelligence, email summaries, reply drafting, follow-up detection, and controlled cleanup.")

# ---------------------------------------------------------
# AUTHENTICATION
# ---------------------------------------------------------
def get_gmail_service():
    """Build Gmail service from a token stored in Streamlit secrets."""
    if "GMAIL_TOKEN" not in st.secrets:
        st.error("Missing [GMAIL_TOKEN] in Streamlit Secrets.")
        return None

    try:
        token_secret = st.secrets["GMAIL_TOKEN"]
        token_info = {
            "token": token_secret.get("token"),
            "refresh_token": token_secret.get("refresh_token"),
            "token_uri": token_secret.get(
                "token_uri", "https://oauth2.googleapis.com/token"
            ),
            "client_id": token_secret.get("client_id"),
            "client_secret": token_secret.get("client_secret"),
            "scopes": list(token_secret.get("scopes", SCOPES)),
        }
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                # Note: the refreshed token is held in memory only. For long-term
                # use, persist token updates securely per connected user.
            else:
                st.error("Gmail token is invalid or expired. Re-authorize Gmail.")
                return None

        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        st.error(f"Gmail authentication failed: {exc}")
        return None


def get_gemini_client():
    if not GEMINI_API_KEY:
        return None
    return genai.Client(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------
# EMAIL PARSING
# ---------------------------------------------------------
def decode_part(data):
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return ""


def collect_body(payload):
    """Extract readable plain-text body from Gmail's nested MIME payload."""
    plain_parts = []
    html_parts = []

    def walk(part):
        mime_type = part.get("mimeType", "")
        body_data = (part.get("body") or {}).get("data")
        if body_data:
            content = decode_part(body_data)
            if mime_type == "text/plain":
                plain_parts.append(content)
