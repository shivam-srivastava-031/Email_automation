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


PROMPT = """You write one short, personal email from the sender to one recipient, in the sender's own voice.

FROM
- {sender_name}{sender_company}
- About the sender: {product}

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


def avoid_rule(campaign):
    phrases = campaign.avoid_list
    return ("\n- Never use these words or phrases: " + "; ".join(f'"{p}"' for p in phrases) + ".") if phrases else ""


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
        instructions=campaign.ai_instructions.strip() or "Introduce yourself and ask for a short call.",
        cta_rule=cta_rule + avoid_rule(campaign),
    )


def parse_output(raw, require_subject=True):
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
    body = strip_signoff(body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not body or (not subject and require_subject):
        raise AIError("the AI returned an empty subject or body")
    return {"subject": subject, "body": body}


SIGNOFF = re.compile(
    r"\n\s*(best( regards| wishes)?|warm( regards)?|kind regards|regards|thanks( again)?|thank you|sincerely|cheers|"
    r"looking forward[^\n]*)[,.!]?\s*(\n[^\n]{0,60}){0,3}\s*$", re.I)


def strip_signoff(body):
    """Drop a trailing sign-off ("Best regards,\\nShivam"): the signature is added automatically."""
    return SIGNOFF.sub("", body).rstrip()


FOLLOWUP_PROMPT = """You write follow-up number {n} to an email that got no reply, from the sender, in their own voice.

FROM: {sender_name}. About the sender: {product}
TO: {name} ({role} at {company})

THE EMAILS ALREADY SENT (oldest first)
{history}

THE SENDER'S INSTRUCTIONS
{instructions}

RULES
- Plain text, 2 to 4 short sentences, 25 to 90 words. Start with "Hi {first_name}," on its own line.
- A polite, friendly nudge that refers back to the earlier email; add one new angle or fact from "About the sender", do not repeat the first email.
- {closing}
- No links, no sign-off, no signature, never mention being an AI. Do not invent facts.

Return JSON only: {{"body": "<the follow-up>"}}
"""

CLASSIFY_PROMPT = """Classify this reply to a job-application / outreach email.

THE EMAIL WE SENT (subject): {subject}
THE REPLY:
\"\"\"{reply}\"\"\"

Categories:
- interested: wants to talk, shortlisted, asks for a call/interview, says they will forward with positive intent
- info: asks a question or for more details (CTC, notice period, location, portfolio...)
- referral: points to another person or email to contact
- not_now: no opening now but maybe later / will keep on file
- not_interested: no, not hiring, stop emailing, unsubscribe
- ooo: automatic out-of-office / on leave / auto-reply
- other: anything else

Return JSON only: {{"category": "<one of: interested, info, referral, not_now, not_interested, ooo, other>", "summary": "<max 15 words, what they said>"}}
"""


def _generate(prompt, temperature=0.6, max_tokens=1024, timeout=60):
    """Gemini JSON call → raw text. Raises AIError."""
    if not settings.GEMINI_API_KEY:
        raise AIError("GEMINI_API_KEY is not set")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"},
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
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, ValueError) as exc:
        raise AIError("Gemini returned no text (the request may have been blocked)") from exc


def write_email(campaign, contact, app_settings, timeout=60):
    """Ask Gemini for this contact's first email. Returns {"subject", "body"}."""
    return parse_output(_generate(build_prompt(campaign, contact, app_settings), timeout=timeout))


def write_followup(campaign, contact, app_settings, history, n, timeout=60):
    """A follow-up body. `history` = [(subject, body), ...] already sent, oldest first. Returns {"body"}."""
    last = n >= campaign.max_followups
    prompt = FOLLOWUP_PROMPT.format(
        n=n, sender_name=app_settings.sender_name or "the sender", product=app_settings.product_description or "not described",
        name=contact.full_name or "the recipient", first_name=contact.first_name or "there",
        role=contact.role or "unknown role", company=contact.company or "their company",
        history="\n\n".join(f"Subject: {s}\n{b}" for s, b in history),
        instructions=(campaign.followup_instructions.strip() or campaign.ai_instructions.strip() or "Keep it short and polite.")
        + avoid_rule(campaign),
        closing=("This is the last follow-up: say you won't email again and leave the door open." if last
                 else "End with one simple question."),
    )
    out = parse_output(_generate(prompt, temperature=0.5, max_tokens=512, timeout=timeout), require_subject=False)
    return {"body": out["body"]}


def classify_reply(subject, reply_text, timeout=45):
    """{"category", "summary"} for a reply. Raises AIError."""
    raw = _generate(CLASSIFY_PROMPT.format(subject=subject[:200], reply=(reply_text or "")[:3000]),
                    temperature=0.0, max_tokens=200, timeout=timeout)
    try:
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
    except json.JSONDecodeError as exc:
        raise AIError(f"the AI did not return JSON: {exc}") from exc
    category = str(data.get("category", "")).strip().lower()
    if category not in {"interested", "info", "referral", "not_now", "not_interested", "ooo", "other"}:
        category = "other"
    return {"category": category, "summary": " ".join(str(data.get("summary", "")).split())[:300]}
