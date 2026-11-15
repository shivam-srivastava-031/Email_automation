"""CSV → contacts.

Accepts the usual exports (Google Sheets, Excel "CSV UTF-8", LinkedIn Sales
Navigator, Apollo, ...): the header row is matched loosely, an `email`
column is required, and every other column is kept on the contact so it can
be used as a {{column}} field in emails.
"""
import csv
import io
import re
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from .models import Contact

HEADER_ALIASES = {
    "email": {"email", "e-mail", "email address", "emailaddress", "mail", "work email", "business email"},
    "first_name": {"first name", "firstname", "first", "given name"},
    "last_name": {"last name", "lastname", "last", "surname", "family name"},
    "full_name": {"name", "full name", "fullname", "contact name"},
    "company": {"company", "company name", "organization", "organisation", "account", "account name"},
    "role": {"role", "title", "job title", "position", "designation"},
}


def _norm_header(h):
    return re.sub(r"[_\s]+", " ", str(h or "").strip().lower())


def _extra_key(h):
    """A CSV header as a merge field name: `Phone Number` → `phone_number`."""
    return re.sub(r"[^a-z0-9]+", "_", str(h or "").strip().lower()).strip("_")


def map_headers(headers):
    """{column index: field name} for known fields, plus the extras."""
    known, extras = {}, {}
    taken = set()
    for i, h in enumerate(headers):
        n = _norm_header(h)
        for fld, aliases in HEADER_ALIASES.items():
            if n in aliases and fld not in taken:
                known[i] = fld
                taken.add(fld)
                break
        else:
            key = _extra_key(h)
            if key:
                extras[i] = key
    return known, extras


@dataclass
class ImportResult:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list = field(default_factory=list)  # first few problems, for the page

    @property
    def total(self):
        return self.added + self.updated


def decode(raw):
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def import_csv(raw_bytes, contact_list=None, max_errors=5):
    """Import contacts from CSV bytes; existing emails are updated, never duplicated."""
    text = decode(raw_bytes)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    result = ImportResult()
    if not rows:
        result.errors.append("The file is empty.")
        return result

    known, extras = map_headers(rows[0])
    if "email" not in known.values():
        result.errors.append("No email column found. Add a column called \"email\".")
        return result

    with transaction.atomic():
        for line_no, row in enumerate(rows[1:], start=2):
            if not any(cell.strip() for cell in row):
                continue
            data = {fld: (row[i].strip() if i < len(row) else "") for i, fld in known.items()}
            email = data.get("email", "").lower()
            try:
                validate_email(email)
            except ValidationError:
                result.skipped += 1
                if len(result.errors) < max_errors:
                    result.errors.append(f"Row {line_no}: \"{email or '(empty)'}\" is not a valid email.")
                continue

            first, last = data.get("first_name", ""), data.get("last_name", "")
            if not first and data.get("full_name"):
                parts = data["full_name"].split()
                first, last = parts[0], " ".join(parts[1:])
            extra = {key: row[i].strip() for i, key in extras.items() if i < len(row) and row[i].strip()}

            contact = Contact.objects.filter(email=email).first()
            if contact is None:
                contact = Contact(email=email)
                result.added += 1
            else:
                result.updated += 1
            # Only overwrite with values the file actually has.
            for fld, val in (("first_name", first), ("last_name", last),
                             ("company", data.get("company", "")), ("role", data.get("role", ""))):
                if val:
                    setattr(contact, fld, val[:200])
            contact.extra = {**(contact.extra or {}), **extra}
            contact.save()
            if contact_list is not None:
                contact.lists.add(contact_list)
    return result
