"""Automatic quality check: decides whether an email may go out without you.

Problems are returned in plain words; any problem holds the email for review
(it shows under "To review" with the reason) instead of sending it.
"""
import re

from .render import MERGE_RE, URL_RE

PLACEHOLDER = re.compile(
    r"\[(your|company|name|first|recipient|insert|role|position|hr|link)[^\]]{0,30}\]|<(name|company|first_name)>|\bXXX\b", re.I)
AI_SLIP = re.compile(r"\b(as an ai|i am an ai|i'm an ai|language model|chatgpt|gemini)\b", re.I)
LIMITS = {"first": (40, 230), "followup": (12, 130)}


def check(subject, body, contact, followup=False):
    problems = []
    subject, body = (subject or "").strip(), (body or "").strip()
    words = len(body.split())
    low, high = LIMITS["followup" if followup else "first"]
    if not subject and not followup:
        problems.append("no subject")
    if len(subject) > 120:
        problems.append("subject is too long")
    if words < low:
        problems.append(f"too short ({words} words)")
    if words > high:
        problems.append(f"too long ({words} words)")
    if MERGE_RE.search(subject + body) or PLACEHOLDER.search(subject + " " + body):
        problems.append("contains a placeholder like [Company] or {{field}}")
    if URL_RE.search(body) or "www." in body.lower():
        problems.append("contains a link (links belong in the button)")
    if AI_SLIP.search(subject + " " + body):
        problems.append("mentions AI")
    first = (contact.first_name or "").strip()
    if first and first.lower() not in body[:120].lower():
        problems.append(f"does not greet {first} by name")
    return problems
