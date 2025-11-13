from typing import Any, Dict, List
import os
import base64
import re
import html

from langgraph.prebuilt.tool_node import ToolNode
from utils.logger import get_logger

# Google API
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Sentiment (assumes transformers is installed)
from transformers import pipeline

_LOG = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


_client_id = os.getenv("GMAIL_CLIENT_ID")
_client_secret = os.getenv("GMAIL_CLIENT_SECRET")
_project_id = os.getenv("GMAIL_PROJECT_ID", "project-id-placeholder")

# In-memory client credentials (as requested)
_credentials_Gmail = {
    "installed": {
        "client_id": _client_id,
        "project_id":  _project_id,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": _client_secret,
        "redirect_uris": ["http://localhost"],
    }
}


def _decode_base64_url(data_b64: str) -> str:
    decoded = base64.urlsafe_b64decode(data_b64).decode("utf-8", errors="ignore")
    text = re.sub(r"<[^>]+>", "", decoded)
    return html.unescape(text)


def _extract_plain_text(msg_payload: Dict) -> str:
    """
    Extract text from Gmail
    """
    if not msg_payload:
        return ""
    if "parts" in msg_payload:
        texts = []
        for part in msg_payload["parts"]:
            mime = part.get("mimeType", "")
            if mime == "text/plain" and "data" in part.get("body", {}):
                texts.append(_decode_base64_url(part["body"]["data"]))
            elif mime == "text/html" and "data" in part.get("body", {}):
                texts.append(_decode_base64_url(part["body"]["data"]))
            elif part.get("parts"):
                texts.append(_extract_plain_text(part))
        return "\n".join(t for t in texts if t).strip()
    else:
        body = msg_payload.get("body", {})
        if "data" in body:
            return _decode_base64_url(body["data"])
    return ""


def _get_header(headers: List[Dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _analyze_sentiments(texts: List[str]) -> List[Dict]:
    """
    Analyze sentiment as a list of text.
    """
    sentiment = pipeline("sentiment-analysis")
    outs = sentiment(texts)
    results = []
    for out, txt in zip(outs, texts):
        results.append({"label": out["label"], "score": float(out["score"]), "text": txt})
    return results


def _load_credentials_or_run_flow(token_path: str) -> Credentials:
    """
 Pull Credentials from secrets.
    """
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        return creds
    flow = InstalledAppFlow.from_client_config(_credentials_Gmail, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    return creds


def get_gmail_tool() -> ToolNode:
    """
    Tool that reads Gmail messages and summarizes positive/negative sentiment.
    """
    def gmail_read_tool(inputs: Dict[str, Any]) -> Dict[str, Any]:
        token_path = os.getenv("GMAIL_TOKEN_JSON", "token.json")
        query = inputs.get("query", "in:inbox -category:social")
        max_results = int(inputs.get("max_results", 10))

        creds = _load_credentials_or_run_flow(token_path)
        service = build("gmail", "v1", credentials=creds)

        resp = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        msgs = resp.get("messages", []) or []

        extracted_texts: List[str] = []
        per_msg: List[Dict] = []

        for m in msgs:
            mid = m["id"]
            mfull = service.users().messages().get(userId="me", id=mid, format="full").execute()
            headers = mfull.get("payload", {}).get("headers", [])
            subject = _get_header(headers, "Subject")
            sender = _get_header(headers, "From")
            body = _extract_plain_text(mfull.get("payload", {})) or ""
            text_snippet = (body[:1000] + "...") if len(body) > 1000 else body
            extracted_texts.append(text_snippet if text_snippet else (subject or ""))
            per_msg.append({"id": mid, "subject": subject, "from": sender, "text": text_snippet})

        if not extracted_texts:
            summary = "No messages found for the given query."
            return {"messages": [summary], "results": {"count": 0, "per_message": []}}

        sentiments = _analyze_sentiments(extracted_texts)

        pos = [s for s in sentiments if s["label"].upper().startswith("POS")]
        neg = [s for s in sentiments if s["label"].upper().startswith("NEG")]

        summary_lines = [
            f"Analyzed {len(sentiments)} messages (query={query}).",
            f"Positive: {len(pos)}, Negative: {len(neg)}.",
        ]

        def top_subjects(filter_list: List[Dict], k: int = 3) -> List[str]:
            subjects: List[str] = []
            for item, senti in zip(per_msg, sentiments):
                if senti in filter_list:
                    subj = item.get("subject") or item.get("text", "")[:80]
                    subjects.append(subj)
            return subjects[:k]

        top_pos = top_subjects(pos, 3)
        top_neg = top_subjects(neg, 3)

        if top_pos:
            summary_lines.append("Top positive examples: " + "; ".join(top_pos))
        if top_neg:
            summary_lines.append("Top negative examples: " + "; ".join(top_neg))

        summary = " ".join(summary_lines)

        for i, s in enumerate(sentiments):
            per_msg[i]["sentiment"] = {"label": s["label"], "score": s["score"]}

        return {
            "messages": [summary],
            "results": {
                "count": len(sentiments),
                "positive": len(pos),
                "negative": len(neg),
                "per_message": per_msg,
            },
        }

    return ToolNode(name="gmail_read", fn=gmail_read_tool)