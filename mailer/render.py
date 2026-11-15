"""Turning a message into the email that is sent (and previewed).

One function builds the HTML for both the preview and the real send, so
what you approve is exactly what goes out.
"""
import html
import re

MERGE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")

BASE_FIELDS = ["first_name", "last_name", "full_name", "company", "role", "email"]


def fill(text, values):
    """Replace {{field}} with the contact's value; unknown fields become empty."""
    return MERGE_RE.sub(lambda m: values.get(m.group(1), ""), text or "")


def unknown_fields(text, known):
    return sorted({m.group(1) for m in MERGE_RE.finditer(text or "")} - set(known))


def paragraphs_html(text):
    """Plain text → <p> paragraphs; links become clickable; everything escaped."""
    out = []
    for para in re.split(r"\n\s*\n", (text or "").strip()):
        if not para.strip():
            continue
        # quote=False: text nodes need no quote escaping, and keeping quotes raw
        # lets URL_RE stop at them, so an href can never contain one.
        safe = html.escape(para.strip(), quote=False)
        safe = URL_RE.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', safe)
        out.append(f"<p style=\"margin:0 0 14px\">{safe.replace(chr(10), '<br>')}</p>")
    return "\n".join(out)


def button_html(text, url):
    if not (text and url):
        return ""
    return (
        f'<p style="margin:18px 0"><a href="{html.escape(url, quote=True)}" '
        'style="display:inline-block;padding:11px 20px;background:#2563eb;color:#ffffff;'
        'border-radius:8px;text-decoration:none;font-weight:600">'
        f"{html.escape(text)}</a></p>"
    )


def build_email(body, cta_text="", cta_url="", signature=""):
    """(html, text) for a message body plus the campaign button and signature."""
    parts = [paragraphs_html(body), button_html(cta_text, cta_url)]
    if signature.strip():
        parts.append(f'<div style="margin-top:18px;color:#475569">{paragraphs_html(signature)}</div>')
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#1e293b">'
        + "\n".join(p for p in parts if p) + "</div>"
    )
    text_parts = [body.strip()]
    if cta_text and cta_url:
        text_parts.append(f"{cta_text}: {cta_url}")
    if signature.strip():
        text_parts.append(signature.strip())
    return html_body, "\n\n".join(text_parts)
