"""The decision: does this prospect want an email, to which address, and
what does it say. Every email has one shape: a reply that continues the
LinkedIn conversation, the attachments, and a call-to-action button - taken
from the team's content (the admin's) or the agent's own, as sharing.py says.

One model call per decision row. The model proposes; code disposes:
  * the recipient must be a string that a PROSPECT typed (or that Newton's
    own regex captured from one) - never enrichment data, never memory;
  * evidence must be a verbatim quote from the prospect's words, or the
    answer is not trusted;
  * the model writes no URLs: attachments, links and the button are added
    by code, and any URL it writes anyway (other than the company website)
    is removed before a human or a mailbox sees it;
  * the attachments and the call to action are not the model's choice at
    all - every active attachment and the same button go with every email.
"""
import html as html_lib
import json
import logging
import re

import requests
from django.conf import settings
from django.utils import timezone

from agentic_deals.extraction import contact_tz, model_name as agentic_model_name, resolve_links
from common.gemini_shim import GeminiOpenAIShim

from . import gates
from .models import AgentEmailSettings, EmailAsset, EmailDecision
from .readiness import resolve_sender
from .sharing import SENDABLE_KINDS, attachments_for, content_for, team_assets

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
MESSAGE_LIMIT = 15
NOTE_LIMIT = 6


class EngineTransient(Exception):
    """Retry-worthy: network, quota, malformed JSON."""


def model_name():
    return getattr(settings, "EMAIL_ORCHESTRATOR_MODEL", None) or agentic_model_name()


def call_json(prompt, max_tokens=1400, temperature=0.3):
    api_key = getattr(settings, "GEMINI_API_KEY", "") or ""
    if not api_key:
        raise EngineTransient("GEMINI_API_KEY is not configured")
    client = GeminiOpenAIShim(api_key, model=model_name())
    try:
        completion = client.chat.completions.create(
            model=model_name(),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=temperature,
        )
        data = json.loads(completion.choices[0].message.content)
    except requests.RequestException as exc:
        raise EngineTransient(f"gemini request failed: {exc}") from exc
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        raise EngineTransient(f"gemini returned non-JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EngineTransient("gemini returned a non-object JSON value")
    return data


# --------------------------------------------------------------------------
# Context

def parse_state(conversation):
    """Newton's ConversationState, stored as JSON text in Conversation.lead_brief."""
    raw = getattr(conversation, "lead_brief", "") or ""
    raw = raw.strip()
    if not raw.startswith("{"):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _clean(text, cap=600):
    text = " ".join(str(text or "").split())
    return text[:cap]


def linkedin_messages(sdr_lead_id, limit=MESSAGE_LIMIT):
    from ai_sdr.models import ConversationMessage
    items = []
    if not sdr_lead_id:
        return items
    rows = (
        ConversationMessage.objects.filter(conversation__lead_id=sdr_lead_id)
        .order_by("-created_at")[: limit * 2]
    )
    seen = set()
    for m in rows:
        # The same outbound text is sometimes persisted twice within a second
        # (inline persist + recovery task). One copy is enough for the model.
        key = (m.direction, _clean(m.content, 200))
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "at": m.sent_at or m.created_at, "channel": "linkedin",
            "who": "them" if m.direction == "inbound" else "us", "text": m.content or "",
        })
    items = [i for i in items if i["at"] is not None]
    items.sort(key=lambda i: i["at"])
    return items[-limit:]


def load_messages(contact, sdr_lead_id):
    items = []
    if contact is not None:
        try:
            from agentic_deals.drafting import recent_messages
            items = recent_messages(contact, limit=MESSAGE_LIMIT)
        except Exception:
            logger.exception("cross-channel history failed for contact %s", contact.pk)
    if not any(i.get("channel") == "linkedin" for i in items):
        items = items + linkedin_messages(sdr_lead_id)
    items = [i for i in items if i.get("at")]
    items.sort(key=lambda i: i["at"])
    return items[-MESSAGE_LIMIT:]


def load_notes(contact, now=None):
    if contact is None:
        return []
    try:
        from agentic_deals.dossier import rep_notes
        deal, lead = resolve_links(contact)
        return rep_notes(
            contact, [lead.pk] if lead else [], [deal.pk] if deal else [],
            limit=NOTE_LIMIT, now=now or timezone.now(),
        )
    except Exception:
        logger.exception("notes failed for contact %s", contact.pk)
        return []


DEFAULT_CTA_TEXT = "Book a 15-minute call"


def _asset_dict(a):
    return {
        "id": a.pk, "kind": a.kind, "name": a.name, "filename": a.filename,
        "url": a.url or "", "file_size": a.file_size,
    }


def _settings_of(agent, email_settings):
    if email_settings is not None or agent is None:
        return email_settings
    return AgentEmailSettings.objects.filter(agent=agent).first()


def asset_catalogue(agent, email_settings=None):
    """What goes with EVERY email this agent sends: the active files
    (attached) and links (listed) of the content it uses - the team's or its
    own, never a mix."""
    if agent is None:
        return []
    return [_asset_dict(a) for a in attachments_for(agent, _settings_of(agent, email_settings))]


def allowed_asset_ids(agent, ids, email_settings=None):
    """The subset of `ids` this agent may send: its own sendable items and its
    team's. Used when a rep edits a draft, so an id from another agent or
    another business can never ride along."""
    ids = [i for i in (ids or []) if isinstance(i, int)]
    if agent is None or not ids:
        return set()
    own = EmailAsset.objects.filter(agent=agent, pk__in=ids, kind__in=SENDABLE_KINDS)
    team = team_assets(agent.business_record_id).filter(pk__in=ids, kind__in=SENDABLE_KINDS)
    return set(own.values_list("pk", flat=True)) | set(team.values_list("pk", flat=True))


def cta_for(agent, email_settings):
    """-> (label, url) for the button under every email, or ("", "") when the
    button is off or there is nowhere to send people. `email_settings` is the
    content in use (team or own); an empty link means this agent's own
    booking link, so team content still sends each rep's prospects to that
    rep's calendar unless the admin set one link for everybody."""
    if email_settings is None or not getattr(email_settings, "include_booking_link", True):
        return ("", "")
    url = (getattr(email_settings, "cta_url", "") or "").strip() or booking_link(agent)
    if not url:
        return ("", "")
    label = (getattr(email_settings, "cta_text", "") or "").strip() or DEFAULT_CTA_TEXT
    return (label, url)


def cta_html(label, url):
    """The button as email-safe inline HTML: email clients ignore classes and
    stylesheets, so everything is inline."""
    if not url:
        return ""
    return (
        '<p style="margin:22px 0 6px 0;"><a href="' + html_lib.escape(url, quote=True) + '" '
        'style="display:inline-block;padding:11px 20px;background:#2563eb;color:#ffffff;'
        'text-decoration:none;border-radius:6px;font-weight:600;font-family:Arial,sans-serif;">'
        + html_lib.escape(label or DEFAULT_CTA_TEXT) + '</a></p>'
    )


def booking_link(agent):
    link = (getattr(agent, "meeting_link", "") or "").strip()
    if link:
        return link
    events = getattr(agent, "conversion_events", None) or []
    for ev in events:
        if isinstance(ev, dict) and (ev.get("url") or "").strip():
            return ev["url"].strip()
    return ""


def persona_of(agent):
    return {
        "name": agent.agent_name or "",
        "tone": getattr(agent, "comment_tone", "") or "professional",
        "persona": (getattr(agent, "persona_text", "") or "")[:1200],
        "directions": (getattr(agent, "custom_directions", "") or "")[:800],
        "company": getattr(agent, "company_name", "") or "",
        "product": (getattr(agent, "product_description", "") or "")[:900],
        "value_proposition": (getattr(agent, "value_proposition", "") or "")[:600],
        "website": getattr(agent, "company_website", "") or "",
    }


def prospect_of(contact, sdr_lead):
    if contact is not None:
        name = " ".join(p for p in (contact.name, contact.last_name) if p).strip()
        return {
            "name": name or (getattr(sdr_lead, "full_name", "") or "the prospect"),
            "first_name": (contact.name or "").split(" ")[0] if contact.name else "",
            "title": contact.title or getattr(sdr_lead, "title", "") or "",
            "company": contact.company_name or getattr(sdr_lead, "company", "") or "",
        }
    full = getattr(sdr_lead, "full_name", "") or "the prospect"
    return {
        "name": full, "first_name": full.split(" ")[0] if full else "",
        "title": getattr(sdr_lead, "title", "") or "", "company": getattr(sdr_lead, "company", "") or "",
    }


def inbound_corpus(messages, trigger_text=""):
    parts = [m["text"] for m in messages if m.get("who") == "them"]
    if trigger_text:
        parts.append(trigger_text)
    return "\n".join(parts)


def allowed_recipients(messages, state, trigger_text=""):
    """Addresses a prospect actually typed, plus Newton's own regex capture."""
    found = []
    for addr in EMAIL_RE.findall(inbound_corpus(messages, trigger_text)):
        a = addr.strip().strip(".").lower()
        if a not in found:
            found.append(a)
    captured = (state or {}).get("captured_email") if isinstance(state, dict) else None
    if captured:
        c = str(captured).strip().lower()
        if EMAIL_RE.fullmatch(c) and c not in found:
            found.append(c)
    return found


def build_context(decision):
    agent = decision.agent
    email_settings = getattr(agent, "email_settings", None) if agent else None
    contact = decision.contact
    sdr_lead = None
    conversation = None
    try:
        from ai_sdr.models import Conversation, SDRLead
        if decision.sdr_lead_id:
            sdr_lead = SDRLead.objects.filter(pk=decision.sdr_lead_id).first()
        if decision.conversation_id:
            conversation = Conversation.objects.filter(pk=decision.conversation_id).first()
        elif sdr_lead is not None:
            conversation = sdr_lead.conversations.order_by("-id").first()
    except Exception:
        logger.exception("lead/conversation load failed for decision %s", decision.pk)
    state = parse_state(conversation) if conversation is not None else {}
    messages = load_messages(contact, decision.sdr_lead_id)
    return {
        "agent": agent,
        "settings": email_settings,
        "contact": contact,
        "sdr_lead": sdr_lead,
        "conversation": conversation,
        "state": state,
        "messages": messages,
        "notes": load_notes(contact),
        "assets": asset_catalogue(agent, email_settings) if agent else [],
        "persona": persona_of(agent) if agent else {},
        "prospect": prospect_of(contact, sdr_lead),
        "booking_link": booking_link(agent) if agent else "",
        "allowed_recipients": allowed_recipients(messages, state, decision.trigger_text),
        "tz": contact_tz(contact),
    }


# --------------------------------------------------------------------------
# Prompt

PROMPT = """You are {name}, a sales development rep at {company}. You have been chatting with a prospect on LinkedIn. Decide whether they want an email from you right now, and if so write it.

WHO YOU ARE
- Tone: {tone}
{persona_block}{directions_block}
What you sell: {product}
{value_block}
PROSPECT
- {prospect_name}{prospect_title}{prospect_company}
- Today (their local time): {today}

ATTACHED TO THE EMAIL AUTOMATICALLY
{attachments}

BUTTON ADDED UNDER YOUR TEXT AUTOMATICALLY
{cta_line}

INSTRUCTIONS FROM YOUR MANAGER
{playbook}
{mailbox_block}
CONVERSATION (oldest first; 'them' is the prospect, 'us' is you on LinkedIn)
{history}
{notes_block}
DECIDE
An email is wanted when the prospect asked for something to be sent, shared or mailed, or gave an email address for that purpose (details, a deck, a video, a proposal, more information). It is NOT wanted when "send me an email" is a brush-off next to a clear no, when they asked you to stop, or when nothing they wrote asks for anything by email.

The recipient address must be copied character for character from something THEY wrote. If they never gave one, set recipient_email to null.

If they asked for something specific that is neither attached nor something you can honestly answer in the email (exact prices, a contract, a portfolio you do not have), still write the email, say in hold_reason what is missing, and set hold_for_human true.

WRITE
- Continue the conversation: open by picking up what they said, in your own words, then give them what they asked for.
- 60 to 140 words, plain paragraphs, no bullet lists unless your instructions ask for them.
- If something is attached, mention it by name in one natural phrase. Never claim an attachment that is not listed above.
- Do not write any URL yourself: attachments, links and the button are added automatically.
- Do not state prices, dates, numbers, customer names or claims that are not in the conversation or your instructions.
- No sign-off and no signature: those are added automatically. Never mention being an AI or an assistant.
- Subject: short and specific to what they asked for.

Return ONLY a JSON object with exactly these keys:
{{
  "should_email": true or false,
  "confidence": number 0.0-1.0,
  "ask": "one line: what they want",
  "recipient_email": "address copied verbatim from their messages, or null",
  "subject": "...",
  "body_markdown": "the email body, paragraphs separated by blank lines",
  "reasons": ["short reasons for your decision"],
  "evidence": ["verbatim quote(s) from the PROSPECT's messages that justify should_email"],
  "hold_for_human": true or false,
  "hold_reason": "why a person should look before this goes out, or empty"
}}
"""


def _render_history(messages):
    lines = []
    for m in messages:
        who = "them" if m.get("who") == "them" else "us"
        at = m.get("at")
        stamp = at.strftime("%Y-%m-%d %H:%M") if at else "?"
        lines.append(f"- [{stamp}] {who} via {m.get('channel', '?')}: {_clean(m.get('text'))}")
    return "\n".join(lines) or "- (no messages)"


def _render_notes(notes):
    if not notes:
        return ""
    lines = ["\nREP NOTES (written by our side)"]
    for n in notes[:NOTE_LIMIT]:
        at = n.get("at")
        stamp = at.strftime("%Y-%m-%d") if at else "?"
        lines.append(f"- [{stamp}] {_clean(n.get('text') or n.get('body') or '', 300)}")
    return "\n".join(lines) + "\n"


def _render_attachments(assets):
    if not assets:
        return "- nothing is attached. Do not mention attachments."
    lines = []
    for a in assets:
        what = a["filename"] if a["kind"] == EmailAsset.KIND_FILE else "a link"
        lines.append(f"- {a['name']} ({what})")
    return "\n".join(lines)


def _render_cta(label, url):
    if not url:
        return "- none. There is no booking link; do not invent one."
    return (f'- a "{label}" button that opens {url}. You may point to it in a few '
            "words (\"the button below\"); do not write the link yourself.")


def _render_mailbox(sender):
    """Only when the team's (admin's) mailbox carries the email. The prospect
    chatted with the rep and will see a colleague's address; one honest line
    keeps that from reading like a stranger or a phish."""
    if not sender or not sender.get("shared"):
        return ""
    name = (sender.get("from_name") or "").strip() or "a colleague"
    first = name.split(" ")[0]
    return (
        f"\nSENDING FROM: this email goes out from your colleague {name}'s mailbox "
        f"({sender.get('from_email')}), which your team sends from. Say so in a few "
        f"natural words (for example \"I'm sending this from my colleague {first}'s inbox\") "
        "so the address does not surprise them. Write everything else in your own voice.\n"
    )


def build_prompt(ctx):
    p = ctx["persona"]
    pr = ctx["prospect"]
    content = ctx.get("content")
    if content is None:
        content = content_for(ctx.get("agent"), ctx.get("settings"))[1]
    playbook = (getattr(content, "playbook", "") or "").strip() or "(none given)"
    cta_label, cta_url = cta_for(ctx.get("agent"), content)
    return PROMPT.format(
        name=p.get("name") or "the rep",
        company=p.get("company") or "our company",
        tone=p.get("tone") or "professional",
        persona_block=(f"- Persona: {p['persona']}\n" if p.get("persona") else ""),
        directions_block=(f"- Hard rules: {p['directions']}\n" if p.get("directions") else ""),
        product=p.get("product") or "(not described)",
        value_block=(f"Value proposition: {p['value_proposition']}\n" if p.get("value_proposition") else ""),
        prospect_name=pr.get("name") or "the prospect",
        prospect_title=(f", {pr['title']}" if pr.get("title") else ""),
        prospect_company=(f" at {pr['company']}" if pr.get("company") else ""),
        today=timezone.now().astimezone(ctx["tz"]).strftime("%A %d %B %Y, %H:%M"),
        attachments=_render_attachments(ctx["assets"]),
        cta_line=_render_cta(cta_label, cta_url),
        playbook=playbook,
        mailbox_block=_render_mailbox(ctx.get("sender")),
        history=_render_history(ctx["messages"]),
        notes_block=_render_notes(ctx["notes"]),
    )


# --------------------------------------------------------------------------
# Validation

def _norm(text):
    return " ".join(str(text or "").split()).lower()


def grounded(evidence, corpus):
    body = _norm(corpus)
    keep = []
    for q in evidence or []:
        qn = _norm(q)
        if len(qn) >= 4 and qn in body:
            keep.append(" ".join(str(q).split())[:300])
    return keep


MERGE_TAGS = [
    "first_name", "full_name", "company_name", "role",
    "sender_name", "sender_company", "booking_link",
]


def merge_context(ctx):
    """Values for the {{tags}} a fixed template may use."""
    pr = ctx.get("prospect") or {}
    p = ctx.get("persona") or {}
    return {
        "first_name": pr.get("first_name") or pr.get("name") or "there",
        "full_name": pr.get("name") or "",
        "company_name": pr.get("company") or "",
        "role": pr.get("title") or "",
        "sender_name": p.get("name") or "",
        "sender_company": p.get("company") or "",
        "booking_link": ctx.get("booking_link") or "",
    }


def render_tags(text, values):
    """Fill {{tags}}. An unknown tag is left alone rather than blanked, so a
    typo is visible in the preview instead of silently deleting a sentence."""
    return re.sub(
        r"\{\{(\w+)\}\}",
        lambda m: str(values.get(m.group(1), m.group(0))),
        str(text or ""),
    )


def markdown_to_html(text):
    """Deliberately tiny: paragraphs, line breaks, links. No raw HTML from the model."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", str(text or "").strip()) if p.strip()]
    out = []
    for para in paragraphs:
        escaped = html_lib.escape(para)
        escaped = URL_RE.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        out.append("<p>" + escaped.replace("\n", "<br>") + "</p>")
    return "\n".join(out)


def strip_unknown_links(text, allowed_urls):
    """Remove every URL the model wrote that is not in the allow-list."""
    allowed = {u.rstrip("/").lower() for u in allowed_urls if u}
    removed = []

    def _sub(m):
        url = m.group(0)
        if url.rstrip("/").rstrip(".,;:").lower() in allowed:
            return url
        removed.append(url)
        return ""

    return URL_RE.sub(_sub, str(text or "")), removed


def validate(data, ctx):
    """-> dict with the cleaned decision, plus 'problems' (names of guards that fired)."""
    problems = []
    should = bool(data.get("should_email"))
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0

    corpus = inbound_corpus(ctx["messages"], "")
    evidence = data.get("evidence")
    if isinstance(evidence, str):
        evidence = [evidence]
    evidence = grounded(evidence if isinstance(evidence, list) else [], corpus)
    if should and not evidence:
        problems.append("no_grounded_evidence")

    recipient = _norm(data.get("recipient_email") or "").strip()
    if recipient and recipient not in ctx["allowed_recipients"]:
        problems.append("recipient_not_typed_by_prospect")
        recipient = ""
    if not recipient and ctx["allowed_recipients"]:
        # The model may have returned null while an address exists: prefer the
        # most recent one the prospect typed.
        recipient = ctx["allowed_recipients"][-1]

    # The model is told to write no URLs: attachments, links and the button are
    # added by code. Anything it writes anyway is removed, except the
    # company's own website, which is harmless and often natural to mention.
    allowed_urls = []
    if ctx["persona"].get("website"):
        allowed_urls.append(ctx["persona"]["website"])
    body_md, removed = strip_unknown_links(data.get("body_markdown") or "", allowed_urls)
    if removed:
        problems.append("unknown_links_removed")
    subject = " ".join(str(data.get("subject") or "").split())[:300] or "The details you asked for"

    reasons = data.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    reasons = [" ".join(str(r).split())[:200] for r in reasons if str(r).strip()][:6]

    return {
        "should_email": should,
        "confidence": confidence,
        "ask": " ".join(str(data.get("ask") or "").split())[:300],
        "recipient_email": recipient,
        "subject": subject,
        "body_markdown": body_md.strip(),
        "body_html": markdown_to_html(body_md),
        "reasons": reasons,
        "evidence": evidence[:5],
        "hold_for_human": bool(data.get("hold_for_human")),
        "hold_reason": " ".join(str(data.get("hold_reason") or "").split())[:300],
        "removed_links": removed,
        "problems": problems,
    }


# --------------------------------------------------------------------------
# The decision

def _finish(decision, status, hold_reason="", extra_reasons=()):
    decision.status = status
    if hold_reason:
        decision.hold_reason = hold_reason
    if extra_reasons:
        decision.reasons = list(decision.reasons or []) + list(extra_reasons)
    decision.save()
    return decision


def decide(decision_id, now=None):
    """Run the whole thing for one row. Idempotent: only an 'evaluating' row is judged."""
    decision = EmailDecision.objects.select_related("agent", "contact", "business").filter(pk=decision_id).first()
    if decision is None or decision.status != EmailDecision.STATUS_EVALUATING:
        return decision
    agent = decision.agent
    if agent is None:
        return _finish(decision, EmailDecision.STATUS_BLOCKED, "agent no longer exists")
    email_settings = AgentEmailSettings.objects.filter(agent=agent).select_related("sender_config").first()

    ctx = build_context(decision)
    ctx["settings"] = email_settings
    # Team content or the agent's own - decided once, used for the prompt,
    # the template, the attachments and the button alike.
    source, content = content_for(agent, email_settings)
    ctx["content_source"], ctx["content"] = source, content
    ctx["assets"] = asset_catalogue(agent, email_settings)
    hit = gates.run_pre_draft_gates(
        agent, email_settings, decision.contact, ctx["sdr_lead"], ctx["state"], exclude_pk=decision.pk,
    )
    if hit:
        return _finish(decision, hit[0], hit[1], extra_reasons=[f"gate: {hit[1]}"])

    # Resolved once after the gates passed: it decides the prompt's mailbox
    # line now and the From address recorded below.
    ctx["sender"] = resolve_sender(agent, email_settings)
    prompt = build_prompt(ctx)
    data = call_json(prompt)
    clean = validate(data, ctx)

    decision.model = model_name()
    decision.raw = {"model_output": data, "problems": clean["problems"], "removed_links": clean["removed_links"],
                    "allowed_recipients": ctx["allowed_recipients"], "content_source": source}
    decision.should_email = clean["should_email"]
    decision.confidence = clean["confidence"]
    decision.ask_summary = clean["ask"]
    decision.recipient_email = clean["recipient_email"]
    # Every active attachment goes with every email. Nothing to pick.
    decision.asset_ids = [a["id"] for a in ctx["assets"]]
    decision.subject = clean["subject"]
    decision.body_html = clean["body_html"]
    decision.reasons = clean["reasons"]
    decision.evidence = clean["evidence"]

    # Fixed mode: the rep wrote the email, so the model's prose is discarded
    # and the template is rendered instead. Its judgement about WHETHER to
    # email and to whom is still used - that is the part that needs to read
    # the conversation. Links are NOT stripped here: the rep wrote them.
    if content.body_mode == AgentEmailSettings.BODY_FIXED:
        tpl_body = (content.fixed_body or "").strip()
        if not tpl_body:
            return _finish(decision, EmailDecision.STATUS_HELD,
                           "the fixed email template is empty; write one or switch to AI")
        merged = merge_context(ctx)
        decision.subject = (render_tags(content.fixed_subject, merged).strip()
                            or clean["subject"])[:300]
        decision.body_html = markdown_to_html(render_tags(tpl_body, merged))
        decision.raw["body_mode"] = AgentEmailSettings.BODY_FIXED

    # Measured BEFORE the button goes on: a reply that is only a button is an
    # empty draft, not an email.
    has_body = bool((decision.body_html or "").strip())

    # The call to action is code, never prose: same button, every email, in
    # both modes, and visible in the draft a rep reviews.
    cta_label, cta_url = cta_for(agent, content)
    if cta_url:
        decision.body_html = (decision.body_html or "") + cta_html(cta_label, cta_url)
    decision.raw["cta"] = {"label": cta_label, "url": cta_url}
    decision.raw["attachments"] = [a["name"] for a in ctx["assets"]]

    if not clean["should_email"]:
        return _finish(decision, EmailDecision.STATUS_SKIPPED, clean["hold_reason"] or "no email wanted")
    if "no_grounded_evidence" in clean["problems"]:
        return _finish(decision, EmailDecision.STATUS_HELD, "the model gave no quote from the prospect to justify emailing")
    if not clean["recipient_email"]:
        return _finish(decision, EmailDecision.STATUS_HELD, "no email address typed by the prospect yet")
    # No attachments is fine: the reply and the button still go. Only the
    # model's own "a person should look" stops it here.
    if clean["hold_for_human"]:
        return _finish(decision, EmailDecision.STATUS_HELD,
                       clean["hold_reason"] or "the model asked for a human to look")
    if not has_body:
        return _finish(decision, EmailDecision.STATUS_HELD, "empty draft")

    resolved = ctx["sender"]
    decision.raw["from_email"] = resolved.get("from_email", "")
    decision.raw["mailbox_fallback"] = bool(resolved.get("shared"))
    if email_settings.mode == AgentEmailSettings.MODE_REVIEW:
        return _finish(decision, EmailDecision.STATUS_AWAITING_REVIEW)

    # Auto: send now. Somebody who just asked for something by email should
    # get it while they are still reading the chat, so there is no
    # time-of-day window. `cancel_window_minutes` defaults to 0 and only
    # delays the send if a rep deliberately asked for a grace period.
    from datetime import timedelta
    from .sending import dispatch_send
    now = now or timezone.now()
    when = now + timedelta(minutes=int(email_settings.cancel_window_minutes or 0))
    decision.save()
    dispatch_send(decision, eta=when)
    decision.refresh_from_db()
    return decision
