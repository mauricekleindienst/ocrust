"""Text files: plain text, Markdown, CSV, source code, data, subtitles."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import re
from typing import Any

from ._context import Context
from ._ir import Block, Break, Code, Heading, Inline, ListBlock, Note, Paragraph, Quote, Span, Table

#: Source code and data files, by suffix, with the language their fence names.
CODE_LANGUAGES = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".vb": "vbnet",
    ".fs": "fsharp",
    ".swift": "swift",
    ".m": "objectivec",
    ".rb": "ruby",
    ".php": "php",
    ".pl": "perl",
    ".lua": "lua",
    ".r": "r",
    ".jl": "julia",
    ".dart": "dart",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
    ".fish": "fish",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".bat": "batch",
    ".cmd": "batch",
    ".sql": "sql",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".vue": "vue",
    ".svelte": "svelte",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
    ".geojson": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".properties": "properties",
    ".env": "dotenv",
    ".xml": "xml",
    ".xsd": "xml",
    ".xsl": "xml",
    ".xslt": "xml",
    ".svg": "xml",
    ".plist": "xml",
    ".gradle": "groovy",
    ".groovy": "groovy",
    ".tf": "hcl",
    ".hcl": "hcl",
    ".proto": "protobuf",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".tex": "latex",
    ".bib": "bibtex",
    ".dockerfile": "dockerfile",
    ".makefile": "makefile",
    ".mk": "makefile",
    ".cmake": "cmake",
    ".asm": "asm",
    ".s": "asm",
    ".diff": "diff",
    ".patch": "diff",
    ".log": "text",
    ".ics": "text",
    ".vcf": "text",
}

_BULLET = re.compile(r"^\s{0,3}([-*+•–·])\s+(.*)$")
_ENUMERATOR = re.compile(r"^\s{0,3}(\d{1,3})[.)]\s+(.*)$")
_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?\r?\n)?---[ \t]*(\r?\n|\Z)", re.S)


def decode_text(data: bytes) -> str:
    """Bytes of a text file as text: by its byte order mark, as UTF-8 if it
    is valid UTF-8, else as Windows-1252 — the other encoding text files
    written in Western Europe come in."""
    for bom, encoding in (
        (b"\xef\xbb\xbf", "utf-8"),
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if data.startswith(bom):
            return data[len(bom) :].decode(encoding, "replace")
    sample = data[:4096]
    if sample and sample.count(b"\x00") > len(sample) // 4:
        # UTF-16 without a byte order mark: every other byte of ASCII is zero.
        odd = sample[1::2].count(b"\x00")
        return data.decode(
            "utf-16-le" if odd >= sample[0::2].count(b"\x00") else "utf-16-be", "replace"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _paragraph(lines: list[str], width: int) -> list[Inline]:
    """Lines of one paragraph: joined where the text was wrapped to `width`,
    broken where the next line's first word would still have fit — an
    address, a signature, a list of names."""
    content: list[Inline] = []
    for index, line in enumerate(lines):
        text = line.strip()
        if index:
            previous = lines[index - 1].rstrip()
            first_word = len(text.split(" ", 1)[0])
            short = width <= 0 or len(previous.strip()) + 1 + first_word <= width
            if lines[index - 1].endswith("  ") or short:
                content.append(Break())
            elif (
                previous.endswith("-")
                and len(previous) > 1
                and previous[-2].isalpha()
                and text[:1].islower()
            ):
                content[-1] = content[-1][:-1] if isinstance(content[-1], str) else content[-1]
            else:
                content.append(" ")
        content.append(text)
    return content


def text_blocks(text: str, *, wrapped: bool = True) -> list[Block]:
    """Plain text as blocks: paragraphs at blank lines, and the lists, quotes,
    headings and tab-separated tables plain text is written with.

    `wrapped` text was broken into lines at a width — the longest line's —
    and its paragraphs are joined up again; otherwise every line break stays.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n")
    lengths = sorted(len(line.strip()) for line in text.split("\n") if line.strip())
    width = (
        lengths[int(len(lengths) * 0.95) - 1 if len(lengths) > 20 else -1]
        if lengths and wrapped
        else 0
    )
    out: list[Block] = []
    for chunk in re.split(r"\n[ \t]*\n+", text.strip("\n")):
        lines = [line.rstrip() for line in chunk.split("\n")]
        lines = [line for line in lines if line.strip()] if len(lines) > 1 else lines
        if not lines or not any(line.strip() for line in lines):
            continue
        out.extend(_chunk(lines, width))
    return out


def _chunk(lines: list[str], width: int) -> list[Block]:
    if len(lines) == 2 and re.match(r"^\s*(=+|-+)\s*$", lines[1]) and lines[0].strip():
        return [Heading(1 if "=" in lines[1] else 2, [lines[0].strip()])]
    if all(line.lstrip().startswith(">") for line in lines):
        inner = "\n".join(re.sub(r"^\s*>\s?", "", line) for line in lines)
        return [Quote(text_blocks(inner, wrapped=width > 0))]
    if len(lines) >= 2 and all("\t" in line for line in lines):
        widths = {len(line.split("\t")) for line in lines}
        if len(widths) == 1:
            return [Table([[[cell.strip()] for cell in line.split("\t")] for line in lines])]
    if all(line.startswith(("    ", "\t")) for line in lines):
        return [
            Code("\n".join(line[4:] if line.startswith("    ") else line[1:] for line in lines))
        ]
    items = _list_items(lines)
    if items is not None:
        return items
    # A line that leads into a list — `Offene Punkte:` — then the list.
    for index in range(1, len(lines)):
        if _BULLET.match(lines[index]) or _ENUMERATOR.match(lines[index]):
            rest = _list_items(lines[index:])
            lead_in = lines[index - 1].rstrip().endswith(":")
            if rest is not None and (len(lines) - index >= 2 or lead_in):
                return [Paragraph(_paragraph(lines[:index], width)), *rest]
            break
    return [Paragraph(_paragraph(lines, width))]


def _list_items(lines: list[str]) -> list[Block] | None:
    first = lines[0]
    bullet, number = _BULLET.match(first), _ENUMERATOR.match(first)
    if not bullet and not number:
        return None
    pattern = _BULLET if bullet else _ENUMERATOR
    groups: list[list[str]] = []
    for line in lines:
        match = pattern.match(line)
        if match:
            groups.append([match.group(2)])
        elif line.startswith((" ", "\t")) and groups:
            groups[-1].append(line.strip())
        else:
            return None
    if len(groups) < 1 or (len(groups) == 1 and len(lines) == 1 and number):
        # "1. Mai ist Feiertag" alone on a line is a sentence, not a list.
        return None if number else [ListBlock(False, [[Paragraph([groups[0][0]])]])]
    start = int(number.group(1)) if number else 1
    return [
        ListBlock(
            ordered=number is not None,
            # An item's indented lines carry it on: wrapped, whatever their length.
            items=[[Paragraph([" ".join(group)])] for group in groups],
            start=start,
        )
    ]


def txt(data: bytes, _ctx: Context) -> Note:
    return Note(blocks=text_blocks(decode_text(data)))


def front_matter(text: str) -> tuple[str | None, str, dict[str, Any]]:
    """A Markdown file's front matter, verbatim, its body, and the flat keys of
    it this can read."""
    match = _FRONT_MATTER.match(text)
    if not match:
        return None, text, {}
    raw = (match.group(1) or "").rstrip("\n")
    flat: dict[str, Any] = {}
    for line in raw.split("\n"):
        pair = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if pair and pair.group(2).strip():
            value = pair.group(2).strip()
            if value[:1] in "\"'" and value[-1:] == value[:1]:
                value = value[1:-1]
            flat[pair.group(1)] = value
    return raw, text[match.end() :], flat


def md(data: bytes, _ctx: Context) -> Note:
    """Markdown stays as it was written; only its front matter is completed."""
    text = decode_text(data)
    raw, body, flat = front_matter(text)
    note = Note(blocks=[], meta={})
    note.raw_body = body.strip("\n")
    note.raw_front = raw
    title = flat.get("title")
    if not title:
        heading = re.search(r"^#\s+(.+?)\s*#*\s*$", body, re.M)
        title = heading.group(1) if heading else None
    note.meta["title"] = title
    return note


def delimited(data: bytes, ctx: Context, delimiter: str | None = None) -> Note:
    text = decode_text(data)
    sample = text[:65536]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
            delimiter = max(counts, key=lambda d: counts[d])
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    blocks: list[Block] = []
    limit = ctx.options.max_rows
    note_meta: dict[str, Any] = {"rows": max(len(rows) - 1, 0)}
    if limit is not None and len(rows) > limit + 1:
        dropped = len(rows) - limit - 1
        rows = rows[: limit + 1]
        note_meta["truncated"] = True
        blocks.append(Table([[[cell] for cell in row] for row in rows]))
        blocks.append(Paragraph([Span("emph", [f"{dropped} more rows not shown"])]))
    elif rows:
        blocks.append(Table([[[cell] for cell in row] for row in rows]))
    return Note(blocks=blocks, meta=note_meta)


def code(data: bytes, _ctx: Context, suffix: str) -> Note:
    text = decode_text(data)
    language = CODE_LANGUAGES.get(suffix, "")
    if language == "json" and "\n" not in text.strip() and len(text) > 120:
        with contextlib.suppress(ValueError):
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    return Note(blocks=[Code(text, language)])


def subtitles(data: bytes, _ctx: Context) -> Note:
    """Subtitles and transcripts (`.srt`, `.vtt`): the spoken text, without
    cue numbers and timings, in paragraphs of a readable length."""
    text = decode_text(data)
    spoken: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if (
            not stripped
            or stripped.isdigit()
            or "-->" in stripped
            or stripped.startswith(("WEBVTT", "NOTE", "STYLE", "REGION", "Kind:", "Language:"))
        ):
            continue
        stripped = re.sub(r"<[^>]+>", "", stripped)
        if not spoken or spoken[-1] != stripped:
            spoken.append(stripped)
    blocks: list[Block] = []
    current = ""
    for piece in spoken:
        current = f"{current} {piece}".strip()
        if len(current) > 600 and re.search(r"[.!?…]$", current):
            blocks.append(Paragraph([current]))
            current = ""
    if current:
        blocks.append(Paragraph([current]))
    return Note(blocks=blocks)
