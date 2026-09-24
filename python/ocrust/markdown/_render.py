"""Writes a :class:`~ocrust.markdown._ir.Note` as Markdown.

The dialect is CommonMark with the two GitHub extensions every Markdown tool
reads — pipe tables and footnotes — and nothing else: Obsidian, GitHub, Pandoc,
MkDocs and the Markdown loaders of LangChain and LlamaIndex all read it the same
way. Front matter is flat YAML, which is what Obsidian shows as properties and
what a retrieval pipeline turns into metadata.

Text is escaped so that it reads back as the text it was and not as markup —
a `*` in a formula, a line that starts with `1.` in the middle of a sentence, an
`<` before a word — but no further: every escape is a backslash in the text a
language model is later given, so characters that cannot start markup where
they stand are left alone.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from ._ir import (
    Block,
    Break,
    Code,
    Footnote,
    FootnoteRef,
    Heading,
    Image,
    Inline,
    ListBlock,
    Marker,
    Note,
    Paragraph,
    Quote,
    Rule,
    Table,
    clean,
    nfc,
    normalize,
    plain,
    strip,
)

_PUNCTUATION = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_ENTITY = re.compile(r"&(#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
# What a line may not start with unless it is meant as markup.
_LINE_START = (
    (re.compile(r"^(\s+)"), ""),
    (re.compile(r"^(#{1,6})(?=\s|$)"), r"\\\1"),
    (re.compile(r"^>"), r"\\>"),
    (re.compile(r"^([-+])(?=\s|$)"), r"\\\1"),
    (re.compile(r"^(\d{1,9})([.)])(?=\s|$)"), r"\1\\\2"),
    (re.compile(r"^(=+|-+)(?=\s*$)"), r"\\\1"),
    (re.compile(r"^\[(?=[^\]]*\]:)"), r"\\["),
)
_NUMERIC = re.compile(r"^[-+−–(]?[€$£¥]?\s?\d[\d.,'\s]*(%|€|\$|[A-Za-z]{0,3}\)?)?$")


def escape(text: str) -> str:
    """Plain text as Markdown inline content that reads back as the same text."""
    out: list[str] = []
    length = len(text)
    for index, char in enumerate(text):
        before = text[index - 1] if index else ""
        after = text[index + 1] if index + 1 < length else ""
        if char == "\\":
            # A backslash escapes only punctuation; before a letter it is text.
            out.append("\\\\" if after in _PUNCTUATION or not after else "\\")
        elif char in "*`~":
            out.append("\\" + char)
        elif char == "_":
            # Inside a word an underscore never opens emphasis: snake_case and
            # j_doe@example.com stay as they are.
            out.append("_" if before.isalnum() and after.isalnum() else "\\_")
        elif char == "<":
            out.append("\\<" if after.isalpha() or (after and after in "/!?") else "<")
        elif char == "&":
            out.append("\\&" if _ENTITY.match(text, index) else "&")
        elif char == "[":
            # `[[` is a wiki link in Obsidian, `[^` a footnote reference.
            out.append("\\[" if after and after in "[^" else "[")
        elif char == "]":
            # `](` and `][` are the second half of a link; alone, a bracket is text.
            out.append("\\]" if after and after in "([" else "]")
        elif char == "=" and (after == "=" or before == "="):
            # `==` marks highlighted text in Obsidian.
            out.append("\\=")
        elif char == "#" and after.isalpha() and not before.isalnum():
            # `#word` is a tag in Obsidian; `C#` and `Nr. #12` are not.
            out.append("\\#")
        else:
            out.append(char)
    return "".join(out)


def inline(content: Iterable[Inline], *, breaks: str = "  \n") -> str:
    """Inline content as Markdown; `breaks` is what a line break becomes."""
    return "".join(_inline(item, breaks) for item in normalize(list(content)))


def _inline(item: Inline, breaks: str) -> str:
    if isinstance(item, str):
        return escape(clean(item))
    if isinstance(item, Break):
        return breaks
    if isinstance(item, FootnoteRef):
        return f"[^{_label(item.label)}]"
    if item.kind == "code":
        return _code_span(plain(item.children) if item.children else "")
    inner = inline(item.children, breaks=breaks)
    if item.kind == "link":
        # Like emphasis: the white space around a link's text is outside it.
        core = inner.strip()
        lead = inner[: len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()) :]
        return f"{lead}{_link(core, item.url)}{trail}" if core else inner
    marker = {"strong": "**", "emph": "*", "strike": "~~"}.get(item.kind)
    if marker is None:
        return inner
    # A delimiter next to white space does not open or close emphasis, so the
    # white space goes outside it: `**Vertrag **` would stay literal.
    core = inner.strip()
    if not core:
        return inner
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()) :]
    return f"{lead}{marker}{core}{marker}{trail}"


def _code_span(text: str) -> str:
    text = clean(text)
    if not text:
        return ""
    ticks = max((len(run) for run in re.findall(r"`+", text)), default=0) + 1
    fence = "`" * ticks
    pad = " " if text.startswith(("`", " ")) or text.endswith(("`", " ")) else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _link(text: str, url: str) -> str:
    url = url.strip()
    if not url:
        return text
    target = _destination(url)
    if not text.strip() or text == escape(url):
        return f"<{url}>" if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]{1,31}:[^\s<>]*$", url) else target
    return f"[{text}]({target})"


def _destination(url: str) -> str:
    """A link target that survives white space and parentheses."""
    return quote(url, safe="/:?#[]@!$&'*+,;=%~-._")


def _label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", label) or "note"


def _line_start(text: str) -> str:
    """Escapes what would make a line of text into a heading, list or quote."""
    for pattern, replacement in _LINE_START:
        text = pattern.sub(replacement, text, count=1)
    return text


def _paragraph(content: list[Inline]) -> str:
    text = inline(strip(content))
    return "\n".join(_line_start(line) for line in text.split("\n"))


def blocks(items: Iterable[Block]) -> str:
    """Blocks as Markdown, a blank line between each two."""
    parts = [text for text in (_block(item) for item in items) if text.strip()]
    return "\n\n".join(parts)


def _block(item: Block) -> str:
    if isinstance(item, Paragraph):
        return _paragraph(item.content)
    if isinstance(item, Heading):
        text = inline(strip(item.content), breaks=" ").replace("\n", " ").strip()
        if not text:
            return ""
        # A trailing run of `#` would be read as the closing sequence.
        text = re.sub(r"(\s)(#+)$", r"\1\\\2", text)
        return f"{'#' * min(max(item.level, 1), 6)} {text}"
    if isinstance(item, ListBlock):
        return _list(item)
    if isinstance(item, Table):
        return _table(item)
    if isinstance(item, Code):
        return _fenced(item.text, item.language)
    if isinstance(item, Quote):
        inner = blocks(item.blocks)
        return (
            "\n".join(f"> {line}" if line else ">" for line in inner.split("\n")) if inner else ""
        )
    if isinstance(item, Rule):
        return "---"
    if isinstance(item, Marker):
        text = clean(item.text).replace("--", "- -").strip()
        return f"<!-- {text} -->" if text else ""
    if isinstance(item, Image):
        return _image(item)
    if isinstance(item, Footnote):
        inner = blocks(item.blocks)
        if not inner:
            return ""
        first, *rest = inner.split("\n")
        body = "\n".join(f"    {line}" if line else "" for line in rest)
        return f"[^{_label(item.label)}]: {first}" + (f"\n{body}" if rest else "")
    raise TypeError(f"not a block: {item!r}")  # pragma: no cover - a converter bug


def _list(item: ListBlock) -> str:
    entries: list[str] = []
    tight = all(
        len(entry) <= 2
        and isinstance(entry[0], Paragraph)
        and all(isinstance(rest, ListBlock) for rest in entry[1:])
        for entry in item.items
        if entry
    )
    number = max(item.start, 0)
    for entry in item.items:
        marker = f"{number}." if item.ordered else "-"
        number += 1
        indent = " " * (len(marker) + 1)
        if tight:
            parts = [_block(part) for part in entry]
            body = "\n".join(part for part in parts if part.strip())
        else:
            body = blocks(entry)
        if not body.strip():
            body = ""
        lines = body.split("\n")
        rendered = [f"{marker} {lines[0]}".rstrip()]
        rendered.extend(f"{indent}{line}" if line else "" for line in lines[1:])
        entries.append("\n".join(rendered))
    return ("\n" if tight else "\n\n").join(entries)


def _cell(content: list[Inline]) -> str:
    text = inline(strip(content), breaks="<br>").replace("\n", " ")
    return text.replace("|", "\\|")


def _table(item: Table) -> str:
    rows = [[_cell(cell) for cell in row] for row in item.rows]
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    # Columns nothing is in are dropped: a spreadsheet's formatting reaches
    # further than its values do.
    keep = [c for c in range(width) if any(row[c].strip() for row in rows)]
    rows = [[row[c] for c in keep] for row in rows]
    if not keep:
        return ""
    header, body = rows[0], rows[1:]
    rule = []
    for column in range(len(keep)):
        values = [row[column].strip() for row in body if row[column].strip()]
        # Two figures at least: one is not yet a column of them.
        figures = sum(bool(_NUMERIC.match(v)) for v in values)
        numeric = len(values) >= 2 and figures >= 0.8 * len(values)
        rule.append("---:" if numeric else "---")
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(rule) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _fenced(text: str, language: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    longest = max((len(run) for run in re.findall(r"^\s*(`{3,}|~{3,})", text, re.M)), default=0)
    fence = "`" * max(3, longest + 1)
    language = re.sub(r"[^A-Za-z0-9_+#.-]", "", language)
    return f"{fence}{language}\n{text}\n{fence}"


def _image(item: Image) -> str:
    parts: list[str] = []
    alt = escape(clean(item.alt)).replace("\n", " ").strip()
    if item.target:
        parts.append(f"![{alt}]({_destination(item.target)})")
    text = blocks(item.blocks)
    if text:
        quoted = "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))
        parts.append(quoted)
    elif alt and not item.target:
        parts.append(f"*{alt}*")
    return "\n\n".join(parts)


def front_matter(meta: dict[str, Any]) -> str:
    """Flat YAML: one scalar or one list of scalars per key.

    Strings are written as double-quoted scalars in JSON's escaping, which YAML
    reads exactly — no key, colon or leading `-` in a title can change what the
    file means. Dates are written unquoted, which is how Obsidian recognizes a
    date property.
    """
    lines = ["---"]
    for key, value in meta.items():
        written = _yaml(value)
        if written is not None:
            lines.append(f"{key}: {written}")
    lines.append("---")
    return "\n".join(lines) if len(lines) > 2 else ""


def _yaml(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return repr(round(value, 6))
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            return value.isoformat(timespec="seconds") + "Z"
        return value.isoformat(timespec="seconds")
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        items = [_yaml(v) for v in value]
        items = [i for i in items if i is not None]
        return "[" + ", ".join(items) + "]" if items else None
    text = clean(str(value)).strip()
    if not text:
        return None
    return json.dumps(nfc(text), ensure_ascii=False)


def render(note: Note, meta: dict[str, Any] | None = None) -> str:
    """The whole note: front matter, then the body, in NFC with LF endings."""
    head = front_matter(meta if meta is not None else note.meta)
    body = blocks(note.blocks)
    text = f"{head}\n\n{body}" if head and body else head or body
    text = "\n".join(
        line.rstrip() if not line.endswith("  ") else line for line in text.split("\n")
    )
    return nfc(text).rstrip("\n") + "\n"
