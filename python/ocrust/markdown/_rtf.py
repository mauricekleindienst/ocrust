"""Rich Text Format, read without a word processor.

RTF is a stream of control words inside nested groups. What matters for a
knowledge base is what a reader sees: the text in the document's code page
and its Unicode escapes, paragraphs, headings by style or outline level,
bold and italic, lists, tables row by row, hyperlinks, and footnotes. Font
tables, pictures' hex dumps, revision tables and every other destination a
reader never sees are skipped.
"""

from __future__ import annotations

import codecs
import contextlib
import datetime as _dt
import re
from dataclasses import dataclass, field, replace
from typing import Any

from ._context import Context
from ._ir import (
    Block,
    Break,
    Footnote,
    FootnoteRef,
    Heading,
    Inline,
    ListBlock,
    Note,
    Paragraph,
    Run,
    Table,
    assemble,
    is_empty,
    plain,
    subscript,
    superscript,
)

_SKIPPED = {
    "fonttbl",
    "colortbl",
    "stylesheet",
    "info",
    "pict",
    "object",
    "header",
    "headerl",
    "headerr",
    "headerf",
    "footer",
    "footerl",
    "footerr",
    "footerf",
    "fldinst",
    "themedata",
    "colorschememapping",
    "datastore",
    "latentstyles",
    "listtable",
    "listoverridetable",
    "rsidtbl",
    "generator",
    "xmlnstbl",
    "mmathPr",
    "pgdsctbl",
    "filetbl",
    "revtbl",
    "bkmkstart",
    "bkmkend",
    "protusertbl",
    "docvar",
    "wgrffmtfilter",
    "nonshppict",
    "shprslt",
    "sp",
    "annotation",
    "atnid",
    "atnauthor",
    "atrfstart",
    "atrfend",
    "fchars",
    "lchars",
    "xe",
    "tc",
    "template",
    "userprops",
    "passwordhash",
    "blipuid",
    "listpicture",
    "ftnsep",
    "ftnsepc",
    "aftnsep",
    "aftnsepc",
}
# A control word and its number, a hex byte, a control symbol, a brace, line
# ends (which RTF ignores), and text.
_TOKEN = re.compile(
    r"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?"
    r"|\\'([0-9a-fA-F]{2})"
    r"|\\([^a-zA-Z])"
    r"|([{}])"
    r"|([\r\n]+)"
    r"|([^\\{}\r\n]+)"
)
_BULLETS = "•◦▪·●○■□➢➤–-*"
_PARAGRAPH_WORDS = {
    "pard",
    "s",
    "outlinelevel",
    "intbl",
    "cell",
    "nestcell",
    "row",
    "nestrow",
    "ls",
    "ilvl",
    "sect",
    "page",
}


@dataclass
class _State:
    skip: bool = False
    bold: bool = False
    italic: bool = False
    strike: bool = False
    script: str = ""
    unicode_skip: int = 1
    footnote: bool = False
    link: str = ""
    field: dict[str, str] | None = None
    in_fldinst: bool = False
    #: Hidden text (`\\v`): read past, never written.
    hidden: bool = False
    #: Inside a list item's number (`\\listtext`), whose own paragraph
    #: settings are the number's, not the item's.
    listtext: bool = False
    #: The code page of the current font, when its character set names one.
    codec: str = ""


@dataclass
class _Paragraph:
    runs: list[Run] = field(default_factory=list)
    style: int = 0
    outline: int = -1
    in_table: bool = False
    list_level: int = -1


class _Parser:
    def __init__(self, text: str, styles: dict[int, str]) -> None:
        self.text = text
        self.styles = styles
        self.codec = "cp1252"
        self.blocks: list[Block] = []
        self.paragraph = _Paragraph()
        self.table: list[list[list[Inline]]] = []
        self.row: list[list[Inline]] = []
        self.cell: list[Inline] = []
        self.items: list[tuple[int, bool, list[Inline]]] = []
        self.hex: bytearray = bytearray()
        self.notes: list[Footnote] = []
        self.note_runs: list[Run] | None = None
        self.pending_skip = 0
        self.high = 0
        self.fonts: dict[int, str] = {}

    # -- output

    def _emit_text(self, text: str, state: _State) -> None:
        if state.skip or not text:
            return
        if self.pending_skip:
            # The fallback a `\\u` escape is followed by, for readers that
            # do not know it: skipped, hidden or not.
            dropped = min(self.pending_skip, len(text))
            text = text[dropped:]
            self.pending_skip -= dropped
            if not text:
                return
        if state.hidden:
            return
        if state.in_fldinst and state.field is not None:
            state.field["instr"] = state.field.get("instr", "") + text
            return
        if state.script == "super":
            text = superscript(text)
        elif state.script == "sub":
            text = subscript(text)
        fmt = frozenset(
            kind
            for kind, on in (
                ("strong", state.bold),
                ("emph", state.italic),
                ("strike", state.strike),
            )
            if on
        )
        target = (
            self.note_runs if state.footnote and self.note_runs is not None else self.paragraph.runs
        )
        target.append((text, fmt, state.link))

    def _flush_items(self) -> None:
        if not self.items:
            return
        base = min(level for level, _, _ in self.items)
        root = ListBlock(ordered=self.items[0][1], items=[])
        stack: list[tuple[int, ListBlock]] = [(base, root)]
        for level, numbered, content in self.items:
            while len(stack) > 1 and level < stack[-1][0]:
                stack.pop()
            if level > stack[-1][0] and stack[-1][1].items:
                nested = ListBlock(ordered=numbered, items=[])
                stack[-1][1].items[-1].append(nested)
                stack.append((level, nested))
            stack[-1][1].items.append([Paragraph(content)])
        self.blocks.append(root)
        self.items = []

    def _flush_table(self) -> None:
        if self.cell and not is_empty(self.cell):
            self.row.append(self.cell)
        if self.row:
            self.table.append(self.row)
        self.cell, self.row = [], []
        if self.table:
            width = max(len(row) for row in self.table)
            if width >= 2:
                self.blocks.append(Table(self.table))
            else:
                self.blocks.extend(
                    Paragraph(row[0]) for row in self.table if row and not is_empty(row[0])
                )
        self.table = []

    def end_paragraph(self) -> None:
        paragraph = self.paragraph
        content = assemble(paragraph.runs)
        self.paragraph = replace(paragraph, runs=[])
        if paragraph.in_table:
            self._flush_items()
            if not is_empty(content):
                if self.cell:
                    self.cell.append(Break())
                self.cell.extend(content)
            return
        if self.table or self.row or self.cell:
            self._flush_table()
        if is_empty(content):
            return
        text = plain(content)
        bullet = text[:1] in _BULLETS and len(text) > 1 and text[1] in " \t"
        numbered = re.match(r"^\d{1,3}[.)]\s", text)
        if paragraph.list_level >= 0 or bullet:
            content = _drop_marker(content) if (bullet or numbered) else content
            self.items.append((max(paragraph.list_level, 0), bool(numbered), content))
            return
        self._flush_items()
        name = self.styles.get(paragraph.style, "")
        match = re.match(r"^heading\s*([1-9])$", name)
        if match:
            self.blocks.append(Heading(int(match.group(1)), content))
        elif name == "title":
            self.blocks.append(Heading(1, content))
        elif 0 <= paragraph.outline < 9:
            self.blocks.append(Heading(paragraph.outline + 1, content))
        else:
            self.blocks.append(Paragraph(content))

    def end_cell(self) -> None:
        self.paragraph.in_table = True
        self.end_paragraph()
        self.row.append(self.cell)
        self.cell = []

    def end_row(self) -> None:
        if self.cell:
            self.row.append(self.cell)
            self.cell = []
        if self.row:
            self.table.append(self.row)
        self.row = []

    # -- parsing

    def parse(self) -> list[Block]:
        state = _State()
        stack: list[_State] = []
        ignorable = False
        destination_start = False
        for match in _TOKEN.finditer(self.text):
            word, param, hexa, symbol, brace, newline, text = match.groups()
            if hexa is not None and not state.skip:
                # Bytes in the document's code page, which may take two of
                # them for one character: decoded together.
                if self.pending_skip:
                    self.pending_skip -= 1
                else:
                    self.hex.append(int(hexa, 16))
                continue
            if self.hex and newline is None:
                codec = state.codec or self.codec
                self._emit_text(bytes(self.hex).decode(codec, "replace"), state)
                self.hex.clear()
            if brace == "{":
                stack.append(state)
                state = replace(state)
                destination_start = True
                continue
            if brace == "}":
                if (
                    state.footnote
                    and stack
                    and not stack[-1].footnote
                    and self.note_runs is not None
                ):
                    content = assemble(self.note_runs)
                    label = str(len(self.notes) + 1)
                    self.notes.append(Footnote(label, [Paragraph(content)]))
                    self.paragraph.runs.append((FootnoteRef(label), frozenset(), ""))
                    self.note_runs = None
                if state.in_fldinst and state.field is not None:
                    found = re.search(r'HYPERLINK\s+"([^"]+)"', state.field.get("instr", ""))
                    if found:
                        state.field["url"] = found.group(1)
                state = stack.pop() if stack else _State()
                destination_start = False
                continue
            first = destination_start
            destination_start = False
            if newline is not None:
                continue
            if text is not None:
                if not state.skip:
                    self._emit_text(
                        text.encode("latin-1", "replace").decode(
                            state.codec or self.codec, "replace"
                        ),
                        state,
                    )
                continue
            if hexa is not None:
                continue
            if symbol is not None:
                if symbol == "*":
                    ignorable = True
                    destination_start = first
                    continue
                if symbol in "\\{}":
                    self._emit_text(symbol, state)
                elif symbol == "~":
                    self._emit_text(" ", state)
                elif symbol == "_":
                    self._emit_text("-", state)
                elif symbol in "\n\r" and not state.skip:
                    self.end_paragraph()
                continue
            assert word is not None
            number = int(param) if param is not None else None
            if first or ignorable:
                if word in ("listtext", "pntext"):
                    state.listtext = True
                if word in _SKIPPED or (
                    ignorable and word not in ("footnote", "fldrslt", "shptxt", "shpinst")
                ):
                    if word == "fldinst":
                        state.in_fldinst = True
                    else:
                        state.skip = True
                    ignorable = False
                    continue
                ignorable = False
            if state.skip:
                continue
            self._control(word, number, state)
        self.end_paragraph()
        self._flush_items()
        if self.table or self.row or self.cell:
            self._flush_table()
        return self.blocks + list(self.notes)

    def _control(self, word: str, number: int | None, state: _State) -> None:
        on = number != 0
        if state.footnote and self.note_runs is not None:
            # A footnote's paragraphs belong to it, not to the paragraph that
            # refers to it.
            if word in ("par", "line"):
                self.note_runs.append((Break(), frozenset(), ""))
                return
            if word in _PARAGRAPH_WORDS:
                return
        if state.listtext and word in _PARAGRAPH_WORDS:
            return
        if word == "ansicpg" and number:
            try:
                codecs.lookup(f"cp{number}")
                self.codec = f"cp{number}"
            except LookupError:
                pass
        elif word in ("mac",):
            self.codec = "mac_roman"
        elif word == "u" and number is not None:
            code = number + 65536 if number < 0 else number
            self.pending_skip = 0
            if 0xD800 <= code <= 0xDBFF:
                # The first half of a character outside the basic plane — an
                # emoji, as Word writes it: the second half follows.
                self.high = code
            elif 0xDC00 <= code <= 0xDFFF:
                if self.high:
                    combined = 0x10000 + ((self.high - 0xD800) << 10) + (code - 0xDC00)
                    self._emit_text(chr(combined), state)
                self.high = 0
            else:
                self.high = 0
                self._emit_text(chr(code), state)
            self.pending_skip = state.unicode_skip
        elif word == "f" and number is not None:
            state.codec = self.fonts.get(number, "")
        elif word == "uc" and number is not None:
            state.unicode_skip = max(number, 0)
        elif word in ("par", "sect") or word == "page":
            self.end_paragraph()
        elif word == "line":
            self.paragraph.runs.append((Break(), frozenset(), state.link))
        elif word == "tab":
            self._emit_text("\t", state)
        elif word in ("emdash", "endash"):
            self._emit_text("—" if word == "emdash" else "–", state)
        elif word in ("lquote", "rquote"):
            self._emit_text("‘" if word == "lquote" else "’", state)
        elif word in ("ldblquote", "rdblquote"):
            self._emit_text("“" if word == "ldblquote" else "”", state)
        elif word == "bullet":
            self._emit_text("•", state)
        elif word == "pard":
            self.paragraph.style = 0
            self.paragraph.outline = -1
            self.paragraph.in_table = False
            self.paragraph.list_level = -1
        elif word == "s" and number is not None:
            self.paragraph.style = number
        elif word == "outlinelevel" and number is not None:
            self.paragraph.outline = number
        elif word == "intbl":
            self.paragraph.in_table = True
        elif word in ("cell", "nestcell"):
            self.end_cell()
        elif word in ("row", "nestrow"):
            self.end_row()
        elif word == "ls":
            self.paragraph.list_level = max(self.paragraph.list_level, 0)
        elif word == "ilvl" and number is not None:
            self.paragraph.list_level = number
        elif word == "plain":
            state.bold = state.italic = state.strike = False
            state.script = ""
        elif word == "b":
            state.bold = on
        elif word == "i":
            state.italic = on
        elif word in ("strike", "striked"):
            state.strike = on
        elif word == "super":
            state.script = "super"
        elif word == "sub":
            state.script = "sub"
        elif word == "nosupersub":
            state.script = ""
        elif word == "v":
            # Hidden text: its control words still count — the `\\v0` that
            # ends it among them.
            state.hidden = on
        elif word == "footnote":
            state.footnote = True
            self.note_runs = []
        elif word == "field":
            state.field = {}
        elif word == "fldrslt" and state.field is not None:
            state.link = state.field.get("url", "")


def _drop_marker(content: list[Inline]) -> list[Inline]:
    """Content without the bullet or number a list paragraph starts with."""
    if content and isinstance(content[0], str):
        stripped = re.sub(r"^\s*(?:[•◦▪·●○■□➢➤–*-]|\d{1,3}[.)])[\s\t]+", "", content[0], count=1)
        return [stripped, *content[1:]]
    return content


def _group_at(text: str, start: int) -> str:
    """The group that opens at `start`, braces balanced."""
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
        index += 1
    return text[start:]


#: Code pages by RTF character set.
_CHARSETS = {
    0: "cp1252",
    128: "cp932",
    129: "cp949",
    134: "cp936",
    136: "cp950",
    161: "cp1253",
    162: "cp1254",
    163: "cp1258",
    177: "cp1255",
    178: "cp1256",
    186: "cp1257",
    204: "cp1251",
    222: "cp874",
    238: "cp1250",
}


def _fonts(text: str) -> dict[int, str]:
    """The code page of every font whose character set names one: Cyrillic
    text in a WordPad file is in its font's code page, not the document's."""
    found = text.find("{\\fonttbl")
    if found < 0:
        return {}
    table = _group_at(text, found)
    out: dict[int, str] = {}
    for match in re.finditer(r"\\f(\d+)[^;{}]*?\\fcharset(\d+)", table):
        codec = _CHARSETS.get(int(match.group(2)))
        if codec:
            out[int(match.group(1))] = codec
    return out


def _styles(text: str) -> dict[int, str]:
    """Paragraph style numbers and their names, from the style sheet."""
    found = text.find("{\\stylesheet")
    if found < 0:
        return {}
    sheet = _group_at(text, found)
    out: dict[int, str] = {}
    for entry in re.finditer(r"\{\\s(\d+)\b", sheet):
        group = _group_at(sheet, entry.start())
        inner = group[1:-1]
        inner = re.sub(r"\{[^{}]*\}", "", inner)
        inner = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", inner)
        inner = re.sub(r"\\'[0-9a-fA-F]{2}", "", inner)
        name = inner.split(";", 1)[0].strip().lower()
        if name:
            out[int(entry.group(1))] = name
    return out


def _info(text: str) -> dict[str, Any]:
    found = text.find("{\\info")
    if found < 0:
        return {}
    info = _group_at(text, found)
    meta: dict[str, Any] = {}

    def field_text(name: str) -> str | None:
        match = re.search(r"\{\\" + name + r"\s([^{}]*)\}", info)
        if not match:
            return None
        value = re.sub(
            r"\\'([0-9a-fA-F]{2})",
            lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"),
            match.group(1),
        )
        value = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), value)
        value = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", value).strip()
        return value or None

    meta["title"] = field_text("title")
    meta["subject"] = field_text("subject")
    meta["author"] = field_text("author")
    keywords = field_text("keywords")
    meta["keywords"] = [k for k in re.split(r"[;,]\s*|\s+", keywords or "") if k] or None
    created = re.search(r"\{\\creatim((?:\\[a-z]+\d+)+)\}", info)
    if created:
        parts = dict(re.findall(r"\\([a-z]+)(\d+)", created.group(1)))
        with contextlib.suppress(ValueError):
            meta["created"] = _dt.datetime(
                int(parts.get("yr", 0)),
                int(parts.get("mo", 1)),
                int(parts.get("dy", 1)),
                int(parts.get("hr", 0)),
                int(parts.get("min", 0)),
            )
    return meta


def rtf(data: bytes, _ctx: Context) -> Note:
    text = data.decode("latin-1")
    if not text.lstrip().startswith("{\\rtf"):
        from ._package import ConversionError

        raise ConversionError("not an RTF document")
    parser = _Parser(text, _styles(text))
    parser.fonts = _fonts(text)
    blocks = parser.parse()
    return Note(blocks=blocks, meta=_info(text))
