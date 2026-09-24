"""Mails (`.eml`) and saved web archives (`.mht`), attachments included.

A mail's sender, recipients, date and subject go into the front matter, where
a knowledge base can filter on them. The body is the HTML part when there is
one — it carries the tables a plain-text part flattens — and every attachment
that can be converted follows as a section of its own, a forwarded mail
included.
"""

from __future__ import annotations

import email
import email.policy
import re
from email.message import EmailMessage, Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from ._context import Context
from ._html import decode_html, html_blocks
from ._ir import Block, Heading, Marker, Note, Paragraph, Span
from ._text import text_blocks

#: How deep mails inside mails are followed.
_MAX_DEPTH = 4


def _addresses(message: Message, header: str) -> list[str]:
    values = message.get_all(header) or []
    out: list[str] = []
    for name, address in getaddresses([str(v) for v in values]):
        if address and name:
            out.append(f"{name} <{address}>")
        elif address or name:
            out.append(address or name)
    return out


def _meta(message: Message) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    subject = message.get("subject")
    meta["title"] = str(subject).strip() if subject else None
    sender = _addresses(message, "from")
    meta["from"] = sender[0] if sender else None
    meta["to"] = _addresses(message, "to") or None
    meta["cc"] = _addresses(message, "cc") or None
    date = message.get("date")
    if date:
        try:
            meta["date"] = parsedate_to_datetime(str(date))
        except (TypeError, ValueError, IndexError):
            meta["date"] = None
    message_id = message.get("message-id")
    meta["message_id"] = str(message_id).strip().strip("<>") if message_id else None
    return meta


def _decoded(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return str(part.get_payload() or "")
    charset = part.get_content_charset() or ""
    if part.get_content_type() == "text/html" and not charset:
        return decode_html(payload)
    for encoding in (charset, "utf-8", "cp1252"):
        if not encoding:
            continue
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("latin-1", "replace")


def _body_parts(message: Message) -> tuple[Message | None, Message | None]:
    """The HTML and the plain-text body, whichever there are."""
    html_part = plain_part = None
    if isinstance(message, EmailMessage):
        html_part = message.get_body(preferencelist=("html",))
        plain_part = message.get_body(preferencelist=("plain",))
    else:  # pragma: no cover - the default policy always makes EmailMessage
        for part in message.walk():
            if part.get_content_type() == "text/html" and html_part is None:
                html_part = part
            elif part.get_content_type() == "text/plain" and plain_part is None:
                plain_part = part
    return html_part, plain_part


def _related(message: Message) -> dict[str, tuple[bytes, str]]:
    """Pictures a HTML body refers to by `cid:` or by location."""
    out: dict[str, tuple[bytes, str]] = {}
    for part in message.walk():
        if part.get_content_maintype() != "image":
            continue
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            continue
        name = part.get_filename() or f"image.{part.get_content_subtype()}"
        cid = part.get("content-id")
        if cid:
            out[f"cid:{str(cid).strip().strip('<>')}"] = (data, name)
        location = part.get("content-location")
        if location:
            out[str(location).strip()] = (data, name)
    return out


def _attachments(message: Message) -> list[Message]:
    if isinstance(message, EmailMessage):
        return list(message.iter_attachments())
    return []  # pragma: no cover


def _message_blocks(message: Message, ctx: Context, depth: int) -> list[Block]:
    html_part, plain_part = _body_parts(message)
    related = _related(message)
    blocks: list[Block] = []
    if html_part is not None:
        base = str(message.get("content-location") or "")
        body, _ = html_blocks(_decoded(html_part), ctx, related.get, base)
        blocks.extend(body)
    elif plain_part is not None:
        blocks.extend(text_blocks(_decoded(plain_part), wrapped=True))
    for part in _attachments(message):
        name = part.get_filename() or ""
        if part.get_content_type() == "message/rfc822" and depth < _MAX_DEPTH:
            payload = part.get_payload()
            inner = payload[0] if isinstance(payload, list) and payload else None
            if isinstance(inner, Message):
                meta = _meta(inner)
                title = meta.get("title") or name or "Attached message"
                blocks.append(Heading(2, [f"Attachment: {title}"]))
                facts = [
                    f"{key}: {value}"
                    for key, value in (("From", meta.get("from")), ("Date", meta.get("date")))
                    if value
                ]
                if facts:
                    blocks.append(Paragraph([Span("emph", ["; ".join(str(f) for f in facts)])]))
                blocks.extend(_demote(_message_blocks(inner, ctx, depth + 1)))
            continue
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes) or not name:
            continue
        if ctx.convert is None:
            continue
        cid = part.get("content-id")
        if cid and part.get_content_maintype() == "image" and html_part is not None:
            # Shown inside the body already.
            continue
        try:
            note = ctx.convert(data, name, ctx)
        except Exception as exc:  # noqa: BLE001 - one attachment must not sink the mail
            blocks.append(Heading(2, [f"Attachment: {name}"]))
            blocks.append(Paragraph([Span("emph", [f"not converted: {exc}"])]))
            continue
        if note is None:
            continue
        blocks.append(Marker(f"attachment {name}"))
        blocks.append(Heading(2, [f"Attachment: {name}"]))
        blocks.extend(_demote(note.blocks))
    return blocks


def _demote(blocks: list[Block]) -> list[Block]:
    """An attachment's headings, one level below the heading that names it."""
    out: list[Block] = []
    for block in blocks:
        if isinstance(block, Heading):
            out.append(Heading(min(block.level + 2, 6), block.content))
        else:
            out.append(block)
    return out


def eml(data: bytes, ctx: Context) -> Note:
    message = email.message_from_bytes(data, policy=email.policy.default)
    meta = _meta(message)
    blocks = _message_blocks(message, ctx, 0)
    attachments = [p.get_filename() for p in _attachments(message) if p.get_filename()]
    if attachments:
        meta["attachments"] = attachments
    return Note(blocks=blocks, meta=meta)


def mht(data: bytes, ctx: Context) -> Note:
    """A web page saved as one file: a MIME message whose first part is the
    page and whose other parts are its pictures."""
    message = email.message_from_bytes(data, policy=email.policy.default)
    html_part, plain_part = _body_parts(message)
    related = _related(message)
    if html_part is not None:
        base = str(html_part.get("content-location") or message.get("content-location") or "")
        blocks, meta = html_blocks(_decoded(html_part), ctx, related.get, base)
    elif plain_part is not None:
        blocks, meta = text_blocks(_decoded(plain_part), wrapped=True), {}
    else:
        blocks, meta = [], {}
    if not meta.get("title"):
        subject = message.get("subject")
        meta["title"] = str(subject).strip() if subject else None
    source = message.get("snapshot-content-location") or message.get("content-location")
    if source and re.match(r"^https?://", str(source)):
        meta["url"] = str(source)
    return Note(blocks=blocks, meta=meta)
