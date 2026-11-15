"""The AI writer: one personal email per contact, via the Gemini REST API."""
import json
import logging
import re

import requests
from django.conf import settings

from .render import URL_RE

log = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class AIError(Exception):
    """The AI could not write this email (bad key, quota, bad output)."""


PROMPT = """You write one short, personal cold email for a salesperson.

FROM
- {sender_name}{sender_company}
- What they offer: {product}

TO
- Name: {name}
- Role: {role}
- Company: {company}
- Other details: {extra}

THE SENDER'S INSTRUCTIONS
{instructions}

RULES
- Plain text only. No markdown, no bullet symbols, no links or URLs.
- 60 to 140 words, 2 to 4 short paragraphs separated by a blank line.
- Start with "Hi {first_name}," on its own line.
- Only use facts given above. Never invent details about the person or their company.
- No sign-off and no signature: they are added automatically.{cta_rule}
- Never mention being an AI.

Return JSON only: {{"subject": "<under 70 characters>", "body": "<the email>"}}
"""


def build_prompt(campaign, contact, app_settings):
    extra = ", ".join(f"{k}: {v}" for k, v in (contact.extra or {}).items() if v) or "none"
    cta_rule = (f"\n- A button labelled \"{campaign.cta_text}\" is added under the email; you may refer to it."
                if campaign.cta_text and campaign.cta_url else "")
    return PROMPT.format(
        sender_name=app_settings.sender_name or "the sender",
        sender_company=f" at {app_settings.company_name}" if app_settings.company_name else "",
        product=app_settings.product_description or "not described",
        name=contact.full_name or "unknown",
        first_name=contact.first_name or "there",
        role=contact.role or "unknown",
        company=contact.company or "unknown",
        extra=extra,
        instructions=campaign.ai_instructions.strip() or "Introduce the offer and ask for a short call.",
        cta_rule=cta_rule,
    )


def parse_output(raw):
    """Gemini's text → {"subject", "body"}, cleaned. Raises AIError if unusable."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIError(f"the AI did not return JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AIError("the AI returned an unexpected shape")
    subject = " ".join(str(data.get("subject") or "").split())[:150]
    body = str(data.get("body") or "").replace("\r\n", "\n").strip()
    body = URL_RE.sub("", body)                      # links only via the button
    body = re.sub(r"\*\*(.+?)\*\*", r"\1", body)     # stray markdown bold
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not subject or not body:
        raise AIError("the AI returned an empty subject or body")
    return {"subject": subject, "body": body}


def write_email(campaign, contact, app_settings, timeout=60):
    """Ask Gemini for this contact's email. Returns {"subject", "body"}."""
    if not settings.GEMINI_API_KEY:
        raise AIError("GEMINI_API_KEY is not set")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": build_prompt(campaign, contact, app_settings)}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 1024, "responseMimeType": "application/json"},
    }
    try:
        resp = requests.post(
            API_URL.format(model=settings.GEMINI_MODEL), json=payload, timeout=timeout,
            headers={"x-goog-api-key": settings.GEMINI_API_KEY},
        )
    except requests.RequestException as exc:
        raise AIError(f"could not reach Gemini: {exc}") from exc
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except ValueError:
            detail = resp.text[:200]
        raise AIError(f"Gemini error {resp.status_code}: {detail}")
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
        raw = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, ValueError) as exc:
        raise AIError("Gemini returned no text (the request may have been blocked)") from exc
    return parse_output(raw)
