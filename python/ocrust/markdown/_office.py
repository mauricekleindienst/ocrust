"""Word, PowerPoint and Excel files, read from their XML without Office.

What is written is what a reader of the document sees, in the order they see
it: tracked deletions and hidden text are left out, field codes give way to
their results, a text box's text follows the paragraph it is anchored to, a
chart becomes the table of numbers it was drawn from, and a list numbered 1 to
3, interrupted by a table and continued at 4, still starts at 4.
"""

from __future__ import annotations

import datetime as _dt
import math
import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element

from ._context import Context
from ._ir import (
    Block,
    Break,
    Code,
    Entry,
    Footnote,
    FootnoteRef,
    Heading,
    Inline,
    ListBlock,
    Marker,
    Note,
    Paragraph,
    Quote,
    Run,
    Span,
    Table,
    assemble,
    body_of,
    flatten,
    is_empty,
    nest,
    plain,
    relabel,
    strip,
    subscript,
    superscript,
    verbatim,
)
from ._package import (
    ConversionError,
    Package,
    attr,
    child,
    children,
    descendants,
    local,
    office_properties,
    rel_id,
    relationships,
)

_OFF = {"0", "false", "off", "none"}
# Characters of the Symbol and Wingdings fonts Word stores as private-use code
# points, as the characters they draw.
_SYMBOLS = {
    0xF0B7: "•",
    0xF0A7: "▪",
    0xF076: "❖",
    0xF0D8: "➢",
    0xF0E0: "→",
    0xF0E8: "➔",
    0xF0FC: "✓",
    0xF0FB: "✗",
    0xF0FD: "☒",
    0xF0FE: "☑",
    0xF06F: "☐",
    0xF0A8: "☐",
    0xF06C: "●",
    0xF06E: "■",
}
_CODE_STYLES = {"code", "source code", "html preformatted", "macro text", "plain text"}
_QUOTE_STYLES = {"quote", "intense quote", "zitat", "intensives zitat", "block text"}


def _on(element: Element | None) -> bool:
    """Whether a toggle property is present and not switched off."""
    if element is None:
        return False
    value = attr(element, "val")
    return value is None or value.lower() not in _OFF


def _symbol(element: Element) -> str:
    try:
        code = int(attr(element, "char") or "", 16)
    except ValueError:
        return ""
    if code in _SYMBOLS:
        return _SYMBOLS[code]
    if 0xF020 <= code <= 0xF07E:
        return chr(code - 0xF000)
    return chr(code) if code < 0xE000 else ""


def _roman(number: int) -> str:
    out = ""
    for value, letters in (
        (1000, "m"),
        (900, "cm"),
        (500, "d"),
        (400, "cd"),
        (100, "c"),
        (90, "xc"),
        (50, "l"),
        (40, "xl"),
        (10, "x"),
        (9, "ix"),
        (5, "v"),
        (4, "iv"),
        (1, "i"),
    ):
        while number >= value:
            out += letters
            number -= value
    return out


def _letters(number: int) -> str:
    out = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        out = chr(ord("a") + rest) + out
    return out


def _format_number(number: int, fmt: str) -> str:
    if fmt in ("lowerLetter",):
        return _letters(number)
    if fmt == "upperLetter":
        return _letters(number).upper()
    if fmt == "lowerRoman":
        return _roman(number)
    if fmt == "upperRoman":
        return _roman(number).upper()
    if fmt == "decimalZero":
        return f"{number:02d}"
    if fmt in ("bullet", "none"):
        return ""
    return str(number)


# --------------------------------------------------------------------------
# Word


@dataclass
class _Level:
    fmt: str = "decimal"
    start: int = 1
    text: str = "%1."


@dataclass
class _Para:
    """A Word paragraph before it is known which list it belongs to."""

    content: list[Inline]
    extras: list[Block] = field(default_factory=list)
    heading: int = 0
    list_level: int = -1
    ordered: bool = False
    number: int = 1
    style: str = ""
    list_id: object = None


class _Numbering:
    """Word's list numbering: formats per level, and the counters."""

    def __init__(self, root: Element | None) -> None:
        self.abstract: dict[str, dict[int, _Level]] = {}
        self.nums: dict[str, tuple[str, dict[int, int]]] = {}
        self.counters: dict[str, dict[int, int]] = {}
        self._overridden: set[str] = set()
        if root is None:
            return
        for item in children(root, "abstractNum"):
            levels: dict[int, _Level] = {}
            for lvl in children(item, "lvl"):
                index = _level_number(attr(lvl, "ilvl"))
                fmt = (
                    attr(child(lvl, "numFmt"), "val", "decimal")
                    if child(lvl, "numFmt") is not None
                    else "decimal"
                )
                start = child(lvl, "start")
                text = child(lvl, "lvlText")
                levels[index] = _Level(
                    fmt=fmt or "decimal",
                    start=_count(attr(start, "val"), 1, 10**6) if start is not None else 1,
                    text=(attr(text, "val") or "") if text is not None else "",
                )
            self.abstract[attr(item, "abstractNumId") or ""] = levels
        for num in children(root, "num"):
            abstract = child(num, "abstractNumId")
            overrides: dict[int, int] = {}
            for override in children(num, "lvlOverride"):
                start = child(override, "startOverride")
                if start is not None:
                    overrides[_level_number(attr(override, "ilvl"))] = _count(
                        attr(start, "val"), 1, 10**6
                    )
            self.nums[attr(num, "numId") or ""] = (
                attr(abstract, "val") or "" if abstract is not None else "",
                overrides,
            )

    def level(self, num_id: str, ilvl: int) -> _Level | None:
        entry = self.nums.get(num_id)
        if entry is None:
            return None
        levels = self.abstract.get(entry[0], {})
        return levels.get(ilvl) or levels.get(0)

    def advance(self, num_id: str, ilvl: int) -> tuple[int, str]:
        """Counts one paragraph of a list; returns its number and its label as
        the document would print it (`2.1.`)."""
        abstract, overrides = self.nums.get(num_id, ("", {}))
        counters = self.counters.setdefault(abstract, {})
        levels = self.abstract.get(abstract, {})
        if num_id not in self._overridden and overrides:
            # A numbering instance that restarts the list: the first paragraph
            # that uses it begins again.
            self._overridden.add(num_id)
            for index, start in overrides.items():
                counters[index] = start - 1
        level = levels.get(ilvl, _Level())
        counters[ilvl] = counters.get(ilvl, level.start - 1) + 1
        for deeper in [k for k in counters if k > ilvl]:
            del counters[deeper]
        label = level.text
        for index in range(ilvl + 1):
            lvl = levels.get(index, _Level())
            value = counters.get(index, lvl.start)
            label = label.replace(f"%{index + 1}", _format_number(value, lvl.fmt))
        return counters[ilvl], label


class _Styles:
    def __init__(self, root: Element | None) -> None:
        self.by_id: dict[str, Element] = {}
        self.default = ""
        if root is None:
            return
        for style in children(root, "style"):
            sid = attr(style, "styleId") or ""
            self.by_id[sid] = style
            if attr(style, "type") == "paragraph" and _on_attr(attr(style, "default")):
                self.default = sid

    def chain(self, sid: str) -> Iterator[Element]:
        seen: set[str] = set()
        while sid and sid not in seen and sid in self.by_id:
            seen.add(sid)
            style = self.by_id[sid]
            yield style
            based = child(style, "basedOn")
            sid = attr(based, "val") or "" if based is not None else ""

    def name(self, sid: str) -> str:
        style = self.by_id.get(sid)
        name = child(style, "name") if style is not None else None
        return (attr(name, "val") or "").lower() if name is not None else ""

    def heading(self, sid: str) -> int:
        """The heading level a style gives its paragraphs, 0 for none."""
        for style in self.chain(sid):
            name = child(style, "name")
            value = (attr(name, "val") or "").lower() if name is not None else ""
            match = re.match(r"^heading\s*([1-9])$", value)
            if match:
                return int(match.group(1))
            if value == "title":
                return -1
            outline = child(child(style, "pPr"), "outlineLvl")
            if outline is not None:
                level = int(attr(outline, "val") or 9)
                return level + 1 if level < 9 else 0
        return 0

    def vanishes(self, sid: str) -> bool | None:
        """Whether a style hides its text; `None` when it says nothing."""
        for style in self.chain(sid):
            rpr = child(style, "rPr")
            for name in ("vanish", "specVanish"):
                flag = child(rpr, name)
                if flag is not None:
                    return _on(flag)
        return None

    def numbering(self, sid: str) -> tuple[str, int] | None:
        for style in self.chain(sid):
            num = child(child(style, "pPr"), "numPr")
            if num is not None:
                num_id = child(num, "numId")
                ilvl = child(num, "ilvl")
                return (
                    attr(num_id, "val") or "" if num_id is not None else "",
                    _level_number(attr(ilvl, "val")) if ilvl is not None else 0,
                )
        return None


def _level_number(value: str | None) -> int:
    """A list level, within the nine Word has."""
    try:
        return min(max(int(value or 0), 0), 8)
    except ValueError:
        return 0


def _count(value: str | None, default: int = 1, most: int = 64) -> int:
    """A span or repeat count from the file, kept within reason: a file may
    claim thirty million columns for one cell."""
    try:
        return min(max(int(value or default), 1), most)
    except ValueError:
        return default


def _on_attr(value: str | None) -> bool:
    return value is not None and value.lower() in ("1", "true", "on")


class _Word:
    def __init__(self, package: Package, ctx: Context) -> None:
        self.package = package
        self.ctx = ctx
        self.part = _main_part(package, "word/document.xml")
        self.rels = relationships(package, self.part)
        folder = posixpath.dirname(self.part)
        self.styles = _Styles(package.xml(f"{folder}/styles.xml"))
        self.numbering = _Numbering(package.xml(f"{folder}/numbering.xml"))
        self.notes: dict[str, Element] = {}
        for kind, name in (("", "footnotes.xml"), ("e", "endnotes.xml")):
            root = package.xml(f"{folder}/{name}")
            if root is None:
                continue
            for note in root:
                note_id = attr(note, "id")
                if note_id is not None and attr(note, "type") in (None, "normal"):
                    self.notes[f"{kind}{note_id}"] = note
        self.used_notes: list[str] = []
        self.titled = False
        self.paragraph_style = ""
        self.chunks = 0

    def convert(self) -> Note:
        root = self.package.xml(self.part)
        body = child(root, "body") if root is not None else None
        if body is None:
            raise ConversionError("no document body")
        self.titled = any(self._style_level(p) == -1 for p in descendants(body, "p"))
        blocks = self._body(body)
        index = 0
        while index < len(self.used_notes):
            label = self.used_notes[index]
            index += 1
            note = self.notes.get(label)
            if note is not None:
                blocks.append(Footnote(label, self._body(note)))
        meta = office_properties(self.package)
        return Note(blocks=blocks, meta=meta)

    # -- structure

    def _body(self, container: Element) -> list[Block]:
        entries: list[_Para | Block] = []
        self._collect(container, entries)
        return self._assemble(entries)

    def _collect(self, container: Element, entries: list[_Para | Block]) -> None:
        for node in container:
            name = local(node.tag)
            if name == "p":
                entries.extend(self._paragraph(node))
            elif name == "tbl":
                entries.extend(self._table(node))
            elif name == "sdt":
                content = child(node, "sdtContent")
                if content is not None:
                    self._collect(content, entries)
            elif name in ("customXml", "ins", "moveTo", "smartTag", "txbxContent"):
                self._collect(node, entries)
            elif name == "AlternateContent":
                choice = child(node, "Choice")
                if choice is not None:
                    self._collect(choice, entries)
            elif name == "altChunk":
                entries.extend(self._chunk(node))

    def _chunk(self, node: Element) -> list[Block]:
        """Content imported whole from another file (`w:altChunk`): a web
        page, RTF, plain text or another Word document, which Word shows in
        its place. It is read by the reader of its kind; one that cannot be
        read is left out, not the document."""
        target = self.rels.get(rel_id(node))
        if not target or target[2] or self.ctx.convert is None:
            return []
        part = target[1]
        try:
            data = self.package.read(part) or b""
            kind = _chunk_kind(self.package, part, data)
            name = (posixpath.splitext(posixpath.basename(part))[0] or "chunk") + kind
            note = self.ctx.convert(data, name, self.ctx) if kind else None
        except Exception:  # noqa: BLE001 - one chunk must not sink the document
            return []
        if note is None:
            return []
        self.chunks += 1
        # Its footnotes stay its own beside the document's.
        return relabel(body_of(note), f"chunk{self.chunks}-")

    def _assemble(self, entries: list[_Para | Block]) -> list[Block]:
        out: list[Block] = []
        items: list[_Para] = []
        code: list[str] = []

        def flush() -> None:
            if items:
                out.extend(_nest_items(items))
                items.clear()
            if code:
                out.append(Code("\n".join(code)))
                code.clear()

        for entry in entries:
            if not isinstance(entry, _Para):
                flush()
                out.append(entry)
                continue
            if entry.list_level >= 0 and not entry.heading:
                if code:
                    flush()
                items.append(entry)
                continue
            if entry.style in _CODE_STYLES:
                if items:
                    out.extend(_nest_items(items))
                    items.clear()
                code.append(verbatim(entry.content))
                continue
            flush()
            if entry.heading and not is_empty(entry.content):
                out.append(Heading(entry.heading, _unbold(entry.content)))
            elif entry.style in _QUOTE_STYLES and not is_empty(entry.content):
                out.append(Quote([Paragraph(entry.content)]))
            elif entry.style in ("subtitle", "caption") and not is_empty(entry.content):
                out.append(Paragraph([Span("emph", strip(entry.content))]))
            elif not is_empty(entry.content):
                out.append(Paragraph(entry.content))
            out.extend(entry.extras)
        flush()
        return out

    def _style_level(self, p: Element) -> int:
        ppr = child(p, "pPr")
        style = child(ppr, "pStyle")
        sid = attr(style, "val") or "" if style is not None else self.styles.default
        outline = child(ppr, "outlineLvl")
        if outline is not None:
            level = int(attr(outline, "val") or 9)
            if level < 9:
                return level + 1
        return self.styles.heading(sid)

    def _paragraph(self, p: Element) -> list[_Para | Block]:
        ppr = child(p, "pPr")
        style_el = child(ppr, "pStyle")
        sid = attr(style_el, "val") or "" if style_el is not None else self.styles.default
        extras: list[Block] = []
        outer_style, self.paragraph_style = self.paragraph_style, sid
        try:
            content = self._inlines(p, extras)
        finally:
            self.paragraph_style = outer_style
        para = _Para(content=content, extras=extras, style=self.styles.name(sid))

        level = self._style_level(p)
        if level == -1:
            para.heading = 1
        elif level > 0:
            para.heading = min(level + (1 if self.titled else 0), 6)

        numbering = None
        num = child(ppr, "numPr")
        if num is not None:
            num_id = child(num, "numId")
            ilvl = child(num, "ilvl")
            numbering = (
                attr(num_id, "val") or "" if num_id is not None else "",
                _level_number(attr(ilvl, "val")) if ilvl is not None else 0,
            )
            if not numbering[0] and sid:
                inherited = self.styles.numbering(sid)
                if inherited:
                    numbering = (inherited[0], numbering[1])
        elif sid:
            numbering = self.styles.numbering(sid)
        if numbering and numbering[0] not in ("", "0"):
            lvl = self.numbering.level(*numbering)
            if lvl is not None and lvl.fmt != "none" and not is_empty(content):
                number, label = self.numbering.advance(*numbering)
                if para.heading:
                    if lvl.fmt != "bullet" and label.strip():
                        para.content = [label.strip(), " ", *strip(content)]
                else:
                    para.list_level = numbering[1]
                    style_level = re.match(
                        r"^list (?:bullet|number|continue)\s*([2-9])$", para.style
                    )
                    if style_level and numbering[1] == 0:
                        # `List Bullet 2` is indented by its style, not by
                        # its list level.
                        para.list_level = int(style_level.group(1)) - 1
                    para.ordered = lvl.fmt != "bullet"
                    para.number = number
                    para.list_id = numbering[0]
        if is_empty(para.content) and not para.extras:
            return []
        if is_empty(para.content):
            return list(para.extras)
        return [para]

    # -- inline content

    def _inlines(self, element: Element, extras: list[Block]) -> list[Inline]:
        runs: list[Run] = []
        state = _FieldState()
        self._walk(element, runs, extras, "", state)
        return assemble(runs)

    def _walk(
        self,
        element: Element,
        runs: list[Run],
        extras: list[Block],
        link: str,
        state: _FieldState,
    ) -> None:
        for node in element:
            name = local(node.tag)
            if name == "r":
                self._run(node, runs, extras, link, state)
            elif name == "hyperlink":
                rid = rel_id(node)
                url = ""
                if rid and rid in self.rels and self.rels[rid][2]:
                    url = self.rels[rid][1]
                self._walk(node, runs, extras, url or link, state)
            elif name in ("ins", "smartTag", "customXml", "fldSimple", "moveTo", "dir", "bdo"):
                self._walk(node, runs, extras, link, state)
            elif name == "sdt":
                content = child(node, "sdtContent")
                if content is not None:
                    self._walk(content, runs, extras, link, state)
            elif name == "AlternateContent":
                choice = child(node, "Choice")
                if choice is not None:
                    self._walk(choice, runs, extras, link, state)
            elif name in ("oMath", "oMathPara"):
                text = "".join(t.text or "" for t in descendants(node, "t"))
                if text:
                    runs.append((text, frozenset(), link))

    def _run(
        self,
        run: Element,
        runs: list[Run],
        extras: list[Block],
        link: str,
        state: _FieldState,
    ) -> None:
        rpr = child(run, "rPr")
        if self._hidden(rpr):
            return
        fmt: set[str] = set()
        if _on(child(rpr, "b")):
            fmt.add("strong")
        if _on(child(rpr, "i")):
            fmt.add("emph")
        if _on(child(rpr, "strike")) or _on(child(rpr, "dstrike")):
            fmt.add("strike")
        vert = attr(child(rpr, "vertAlign"), "val") if child(rpr, "vertAlign") is not None else None
        frozen = frozenset(fmt)

        def text(value: str) -> None:
            if state.skipping():
                state.result_text(value)
                return
            state.shown(value)
            if vert == "superscript":
                value = superscript(value)
            elif vert == "subscript":
                value = subscript(value)
            runs.append((value, frozen, state.link or link))

        for node in run:
            name = local(node.tag)
            if name == "t":
                text(node.text or "")
            elif name in ("tab", "ptab"):
                text("\t")
            elif name == "br":
                kind = attr(node, "type")
                if kind in ("page", "column"):
                    text(" ")
                elif not state.skipping():
                    runs.append((Break(), frozenset(), link))
            elif name == "cr":
                runs.append((Break(), frozenset(), link))
            elif name == "noBreakHyphen":
                text("-")
            elif name == "sym":
                text(_symbol(node))
            elif name == "fldChar":
                value = state.char(attr(node, "fldCharType") or "", child(node, "ffData"))
                if value:
                    text(value)
            elif name == "instrText":
                state.instruction(node.text or "")
            elif name == "footnoteReference":
                self._note_ref(attr(node, "id") or "", "", runs)
            elif name == "endnoteReference":
                self._note_ref(attr(node, "id") or "", "e", runs)
            elif name in ("drawing", "pict", "object"):
                extras.extend(self._drawing(node))
            elif name == "ruby":
                self._ruby(node, runs, extras, link, state)
            elif name == "AlternateContent":
                choice = child(node, "Choice")
                if choice is not None:
                    for inner in choice:
                        if local(inner.tag) in ("drawing", "pict", "object"):
                            extras.extend(self._drawing(inner))

    def _ruby(
        self,
        ruby: Element,
        runs: list[Run],
        extras: list[Block],
        link: str,
        state: _FieldState,
    ) -> None:
        """Text with its reading set above it (furigana, Word's Phonetic
        Guide): the text, then the reading in parentheses, as a web page
        writes it for a browser without ruby — 東京(とうきょう)."""
        start = len(runs)
        base = child(ruby, "rubyBase")
        if base is not None:
            self._walk(base, runs, extras, link, state)
        above = child(ruby, "rt")
        if above is None:
            return
        reading: list[Run] = []
        self._walk(above, reading, extras, link, state)
        texts = [(item, fmt) for item, fmt, _ in reading if isinstance(item, str)]
        said = "".join(item for item, _ in texts).strip()
        shown = "".join(item for item, _, _ in runs[start:] if isinstance(item, str)).strip()
        # Dots set over a word for emphasis are not a reading.
        if re.search(r"\w", said) and said != shown:
            # The parentheses take the formatting the whole reading has.
            fmt = frozenset.intersection(*(fmt for _, fmt in texts))
            runs.append(("(", fmt, state.link or link))
            runs.extend(reading)
            runs.append((")", fmt, state.link or link))

    def _hidden(self, rpr: Element | None) -> bool:
        """Whether a run is hidden text: by its own formatting, else by its
        character style, else by its paragraph's style."""
        for name in ("vanish", "specVanish"):
            flag = child(rpr, name)
            if flag is not None:
                return _on(flag)
        style = child(rpr, "rStyle")
        if style is not None:
            by_style = self.styles.vanishes(attr(style, "val") or "")
            if by_style is not None:
                return by_style
        return bool(self.styles.vanishes(self.paragraph_style))

    def _note_ref(self, note_id: str, kind: str, runs: list[Run]) -> None:
        label = f"{kind}{note_id}"
        if label not in self.notes:
            return
        if label not in self.used_notes:
            self.used_notes.append(label)
        runs.append((FootnoteRef(label), frozenset(), ""))

    def _drawing(self, node: Element) -> list[Block]:
        """Pictures, text boxes, charts and diagrams inside a run.

        A text box's own pictures and boxes are its paragraphs' business: the
        walk does not go into it a second time.
        """
        out: list[Block] = []
        described = next(descendants(node, "docPr"), None)
        alt = (
            (attr(described, "descr") or attr(described, "title") or "")
            if described is not None
            else ""
        )

        def visit(element: Element) -> None:
            nonlocal alt
            for item in element:
                name = local(item.tag)
                if name == "txbxContent":
                    out.extend(self._body(item))
                    continue
                if name == "Fallback":
                    continue
                if name in ("blip", "imagedata"):
                    rid = rel_id(item, "embed") or rel_id(item, "id") or rel_id(item, "link")
                    target = self.rels.get(rid or "")
                    if target and not target[2]:
                        picture = self.ctx.picture(self.package.read(target[1]), target[1], alt)
                        if picture is not None:
                            out.append(picture)
                            alt = ""
                elif name == "chart":
                    target = self.rels.get(rel_id(item))
                    if target and not target[2]:
                        out.extend(_chart(self.package, target[1]))
                elif name == "relIds":
                    target = self.rels.get(rel_id(item, "dm"))
                    if target and not target[2]:
                        out.extend(_smartart(self.package, target[1]))
                visit(item)

        visit(node)
        return out

    # -- tables

    def _table(self, table: Element) -> list[Block]:
        rows: list[list[list[Block]]] = []
        for tr in _rows(table):
            row: list[list[Block]] = []
            # Cells a row leaves out on its left: its values start further right.
            before = child(child(tr, "trPr"), "gridBefore")
            if before is not None:
                row.extend(
                    []
                    for _ in range(
                        _count(attr(before, "val"), 0) if attr(before, "val") != "0" else 0
                    )
                )
            for tc in _cells(tr):
                tcpr = child(tc, "tcPr")
                span_el = child(tcpr, "gridSpan")
                span = _count(attr(span_el, "val")) if span_el is not None else 1
                merge = child(tcpr, "vMerge")
                continued = merge is not None and (attr(merge, "val") or "continue") == "continue"
                blocks = [] if continued else self._body(tc)
                row.append(blocks)
                row.extend([] for _ in range(max(span, 1) - 1))
            if row:
                rows.append(row)
        return _table_blocks(rows)


@dataclass
class _Field:
    """One complex field being read: its instruction, whether its result has
    begun, and what a form field shows without a result of its own."""

    instruction: str = ""
    in_result: bool = False
    value: str = ""
    shown: bool = False


class _FieldState:
    """Word's complex fields: `{ HYPERLINK "…" }` and its displayed result.

    The instruction is never text; the result is, and a hyperlink field's
    result becomes a link. A legacy form field keeps its answer in its field
    data instead: a check box's state and a drop-down's choice stand where
    the field ends, when it has no result that says them.
    """

    def __init__(self) -> None:
        self.stack: list[_Field] = []
        self.link = ""

    def char(self, kind: str, data: Element | None = None) -> str:
        """Follows a field character; returns what a form field that ends
        here shows, if anything."""
        if kind == "begin":
            self.stack.append(_Field(value=_form_value(data)))
        elif kind == "separate" and self.stack:
            self.stack[-1].in_result = True
            match = re.match(r'\s*HYPERLINK\s+"([^"]+)"', self.stack[-1].instruction)
            if match and len(self.stack) == 1:
                self.link = match.group(1)
        elif kind == "end" and self.stack:
            ended = self.stack.pop()
            if not self.stack:
                self.link = ""
            if not ended.shown:
                return ended.value
        return ""

    def instruction(self, text: str) -> None:
        if self.stack and not self.stack[-1].in_result:
            self.stack[-1].instruction += text

    def skipping(self) -> bool:
        return any(not frame.in_result for frame in self.stack)

    def result_text(self, _text: str) -> None:
        """Text inside an instruction: never shown."""

    def shown(self, text: str) -> None:
        """Text of the fields' results: what they show, their data aside."""
        if text.strip():
            for frame in self.stack:
                frame.shown = True


def _form_value(data: Element | None) -> str:
    """What a legacy form field's data (`w:ffData`) says it shows: a check
    box as ☒ or ☐, a drop-down as the entry chosen. A text field's answer is
    its result, as any field's."""
    if data is None:
        return ""
    box = child(data, "checkBox")
    if box is not None:
        # Checked as the user left it, else as the form was made.
        state = child(box, "checked")
        if state is None:
            state = child(box, "default")
        return "☒" if _on(state) else "☐"
    choices = child(data, "ddList")
    if choices is not None:
        entries = [attr(entry, "val") or "" for entry in children(choices, "listEntry")]
        chosen = child(choices, "result")
        if chosen is None:
            chosen = child(choices, "default")
        try:
            index = int(attr(chosen, "val") or 0) if chosen is not None else 0
        except ValueError:
            index = 0
        return entries[index] if 0 <= index < len(entries) else ""
    return ""


def _rows(table: Element) -> Iterator[Element]:
    for node in table:
        name = local(node.tag)
        if name == "tr":
            yield node
        elif name in ("sdt", "customXml"):
            inner = child(node, "sdtContent") if name == "sdt" else node
            if inner is not None:
                yield from _rows(inner)


def _cells(row: Element) -> Iterator[Element]:
    for node in row:
        name = local(node.tag)
        if name == "tc":
            yield node
        elif name in ("sdt", "customXml"):
            inner = child(node, "sdtContent") if name == "sdt" else node
            if inner is not None:
                yield from _cells(inner)


def _table_blocks(rows: list[list[list[Block]]]) -> list[Block]:
    """A table's cells, as a table — or as the text it holds, when the table is
    only there to lay the page out: one column, or a single cell."""
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    used = [c for c in range(width) if any(c < len(row) and row[c] for row in rows)]
    if len(used) <= 1:
        return [block for row in rows for cell in row for block in cell]
    return [Table([[flatten(row[c]) if c < len(row) else [] for c in used] for row in rows])]


def _unbold(content: list[Inline]) -> list[Inline]:
    """A heading's text without the bold its style already implies."""
    out: list[Inline] = []
    for item in content:
        if isinstance(item, Span) and item.kind == "strong":
            out.extend(_unbold(item.children))
        elif isinstance(item, Span):
            out.append(Span(item.kind, _unbold(item.children), item.url))
        elif isinstance(item, Break):
            out.append(" ")
        else:
            out.append(item)
    return out


def _nest_items(items: list[_Para]) -> list[Block]:
    """List paragraphs as nested lists, by their level; a paragraph of another
    list, or of another kind, at the same level starts a list of its own."""
    return nest(
        [
            Entry(
                level=item.list_level,
                ordered=item.ordered,
                blocks=[Paragraph(item.content), *item.extras],
                number=item.number,
                list_id=item.list_id,
            )
            for item in items
        ]
    )


def _main_part(package: Package, fallback: str) -> str:
    """The package's main document, from its root relationships."""
    rels = relationships(package, "")
    for kind, target, external in rels.values():
        if kind == "officeDocument" and not external and package.find(target):
            return package.find(target) or target
    found = package.find(fallback)
    if found is None:
        raise ConversionError(f"no {fallback} in the package")
    return found


#: What an imported chunk is read as, by its content type or else its name.
_CHUNK_TYPES = {
    "text/html": ".html",
    "application/xhtml+xml": ".xhtml",
    "message/rfc822": ".mht",
    "multipart/related": ".mht",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "text/plain": ".txt",
}
_CHUNK_SUFFIXES = {
    ".htm": ".html",
    ".html": ".html",
    ".xhtml": ".xhtml",
    ".mht": ".mht",
    ".mhtml": ".mht",
    ".rtf": ".rtf",
    ".txt": ".txt",
}


def _chunk_kind(package: Package, part: str, data: bytes) -> str:
    """The suffix of the reader an imported chunk is read with, or "" for a
    kind none reads. A Word document is a package and RTF starts as RTF,
    whatever their content type says."""
    if data.startswith(b"PK\x03\x04"):
        return ".docx"
    if data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{\\rtf"):
        return ".rtf"
    by_type = _CHUNK_TYPES.get(_content_type(package, part))
    return by_type or _CHUNK_SUFFIXES.get(posixpath.splitext(part.lower())[1], "")


def _content_type(package: Package, part: str) -> str:
    """A part's content type, from `[Content_Types].xml`: its own, else the
    one of its extension."""
    root = package.xml("[Content_Types].xml")
    if root is None:
        return ""
    name = "/" + part.lstrip("/").lower()
    extension = posixpath.splitext(name)[1].lstrip(".")
    found = ""
    for item in root:
        kind = local(item.tag)
        if kind == "Override" and (attr(item, "PartName") or "").lower() == name:
            found = attr(item, "ContentType") or ""
            break
        if kind == "Default" and (attr(item, "Extension") or "").lower() == extension:
            found = found or attr(item, "ContentType") or ""
    return found.split(";", 1)[0].strip().lower()


def docx(data: bytes, ctx: Context) -> Note:
    return _Word(Package(data), ctx).convert()


# --------------------------------------------------------------------------
# Charts and diagrams, shared by Word and PowerPoint


def _points(element: Element | None) -> dict[int, str]:
    """The cached values of a chart reference, by index."""
    out: dict[int, str] = {}
    if element is None:
        return out
    for pt in descendants(element, "pt"):
        value = child(pt, "v")
        try:
            index = int(attr(pt, "idx") or 0)
        except ValueError:
            continue
        if value is not None and value.text is not None:
            out[index] = value.text.strip()
    return out


def _number(text: str) -> str:
    """A number as Excel shows it: to fifteen significant digits, which is
    all a sheet keeps, without the noise of binary fractions (0.1 + 0.2), and
    a whole number as long as it is exact."""
    try:
        value = float(text)
    except ValueError:
        return text
    if math.isfinite(value) and value == int(value) and abs(value) <= 2**53:
        return str(int(value))
    return format(value, ".15g")


_XSTRING = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _xstring(text: str) -> str:
    """Text of a sheet with its escapes undone: `_x000D_` is a carriage
    return, `_x005F_` the underscore that keeps a literal `_x...` apart."""
    if "_x" not in text:
        return text
    return _XSTRING.sub(lambda m: chr(int(m.group(1), 16)), text)


def _either(element: Element, first: str, second: str) -> Element | None:
    found = child(element, first)
    return found if found is not None else child(element, second)


def _chart(package: Package, part: str) -> list[Block]:
    """A chart as the table of numbers it draws: categories down, series across."""
    root = package.xml(part)
    if root is None:
        return []
    title_el = next(descendants(root, "title"), None)
    title = (
        " ".join(t.text or "" for t in descendants(title_el, "t")).strip()
        if title_el is not None
        else ""
    )
    series: list[tuple[str, dict[int, str], dict[int, str]]] = []
    for ser in descendants(root, "ser"):
        name = ""
        tx = child(ser, "tx")
        if tx is not None:
            name = " ".join(v.text or "" for v in descendants(tx, "v")).strip()
        cats = _points(_either(ser, "cat", "xVal"))
        vals = _points(_either(ser, "val", "yVal"))
        series.append((name, cats, vals))
    if not series:
        return [Paragraph([Span("emph", [title])])] if title else []
    categories: dict[int, str] = {}
    for _, cats, _ in series:
        for index, value in cats.items():
            categories.setdefault(index, value)
    indices = sorted(set(categories) | {i for _, _, vals in series for i in vals})
    header: list[list[Inline]] = [[""]] + [
        [name or f"Series {n}"] for n, (name, _, _) in enumerate(series, 1)
    ]
    rows: list[list[list[Inline]]] = [header]
    for index in indices:
        row: list[list[Inline]] = [[categories.get(index, str(index + 1))]]
        row.extend([[_number(vals.get(index, ""))] for _, _, vals in series])
        rows.append(row)
    out: list[Block] = []
    if title:
        out.append(Paragraph([Span("emph", [title])]))
    out.append(Table(rows))
    return out


def _smartart(package: Package, part: str) -> list[Block]:
    """A SmartArt diagram's text, one item per shape."""
    root = package.xml(part)
    if root is None:
        return []
    items: list[list[Block]] = []
    for pt in descendants(root, "pt"):
        if attr(pt, "type") not in (None, "node"):
            continue
        body = child(pt, "t")
        if body is None:
            continue
        text = " ".join(
            "".join(t.text or "" for t in descendants(p, "t")) for p in children(body, "p")
        ).strip()
        if text:
            items.append([Paragraph([text])])
    return [ListBlock(ordered=False, items=items)] if items else []


# --------------------------------------------------------------------------
# PowerPoint

_SKIPPED_PLACEHOLDERS = {"dt", "ftr", "sldNum", "hdr"}
_TITLE_PLACEHOLDERS = {"title", "ctrTitle"}


@dataclass
class _Shape:
    y: float
    x: float
    element: Element
    kind: str
    placeholder: str | None
    index: str | None


def _offset(element: Element) -> tuple[float, float] | None:
    for xfrm in (
        child(child(element, "spPr"), "xfrm"),
        child(element, "xfrm"),
        child(child(element, "grpSpPr"), "xfrm"),
    ):
        off = child(xfrm, "off")
        if off is not None:
            try:
                return float(attr(off, "y") or 0), float(attr(off, "x") or 0)
            except ValueError:
                return None
    return None


def _placeholder(element: Element) -> tuple[str | None, str | None]:
    for nv in element:
        if local(nv.tag).startswith("nv"):
            ph = next(descendants(nv, "ph"), None)
            if ph is not None:
                return attr(ph, "type") or "body", attr(ph, "idx")
    return None, None


class _Slides:
    def __init__(self, package: Package, ctx: Context) -> None:
        self.package = package
        self.ctx = ctx
        self.part = _main_part(package, "ppt/presentation.xml")
        self.rels = relationships(package, self.part)

    def convert(self) -> Note:
        root = self.package.xml(self.part)
        if root is None:
            raise ConversionError("no presentation part")
        blocks: list[Block] = []
        slide_list = child(root, "sldIdLst")
        slides = []
        if slide_list is not None:
            for entry in children(slide_list, "sldId"):
                target = self.rels.get(rel_id(entry))
                if target and not target[2]:
                    slides.append(target[1])
        shown = 0
        for number, part in enumerate(slides, 1):
            converted = self._slide(number, part)
            if converted:
                shown += 1
            blocks.extend(converted)
        meta = office_properties(self.package)
        meta["slides"] = shown
        return Note(blocks=blocks, meta=meta)

    def _slide(self, number: int, part: str) -> list[Block]:
        root = self.package.xml(part)
        if root is None or root.get("show") in ("0", "false"):
            # A hidden slide is not part of the talk.
            return []
        rels = relationships(self.package, part)
        layout_positions = self._layout_positions(rels)
        tree = next(descendants(root, "spTree"), None)
        shapes = self._shapes(tree, layout_positions) if tree is not None else []
        title = ""
        body: list[Block] = []
        for shape in shapes:
            if shape.placeholder in _TITLE_PLACEHOLDERS and not title:
                title = " ".join(
                    plain(p) for p in self._text_blocks(shape.element, rels, False)
                ).strip()
                if title:
                    continue
            body.extend(self._shape_blocks(shape, rels))
        out: list[Block] = [Marker(f"slide {number}"), Heading(2, [title or f"Slide {number}"])]
        out.extend(body)
        notes = self._notes(rels)
        if notes:
            out.append(Quote([Paragraph([Span("strong", ["Notes:"])]), *notes]))
        return out

    def _layout_positions(
        self, rels: dict[str, tuple[str, str, bool]]
    ) -> dict[str, tuple[float, float]]:
        """Where the slide's layout puts each placeholder, for shapes that
        inherit their position instead of stating it."""
        out: dict[str, tuple[float, float]] = {}
        for kind, target, external in rels.values():
            if kind != "slideLayout" or external:
                continue
            layout = self.package.xml(target)
            if layout is None:
                continue
            for sp in descendants(layout, "sp"):
                ph_type, ph_idx = _placeholder(sp)
                position = _offset(sp)
                if ph_type and position:
                    out.setdefault(f"idx:{ph_idx}", position)
                    out.setdefault(f"type:{ph_type}", position)
        return out

    def _shapes(self, tree: Element, layout: dict[str, tuple[float, float]]) -> list[_Shape]:
        shapes: list[_Shape] = []
        for node in tree:
            name = local(node.tag)
            if name == "AlternateContent":
                choice = child(node, "Choice")
                node = next(iter(choice), None) if choice is not None else None
                if node is None:
                    continue
                name = local(node.tag)
            if name not in ("sp", "pic", "graphicFrame", "grpSp"):
                continue
            ph_type, ph_idx = _placeholder(node)
            if ph_type in _SKIPPED_PLACEHOLDERS:
                continue
            own = next(descendants(node, "cNvPr"), None)
            if own is not None and _on_attr(attr(own, "hidden")):
                continue
            position = _offset(node) or layout.get(f"idx:{ph_idx}") or layout.get(f"type:{ph_type}")
            if position is None:
                position = (-1.0, -1.0) if ph_type in _TITLE_PLACEHOLDERS else (math.inf, math.inf)
            shapes.append(_Shape(position[0], position[1], node, name, ph_type, ph_idx))
        # Top to bottom, left to right; shapes on nearly one line read across.
        shapes.sort(key=lambda s: (round(s.y / 190500) if math.isfinite(s.y) else math.inf, s.x))
        titles = [s for s in shapes if s.placeholder in _TITLE_PLACEHOLDERS]
        return titles + [s for s in shapes if s.placeholder not in _TITLE_PLACEHOLDERS]

    def _shape_blocks(self, shape: _Shape, rels: dict[str, tuple[str, str, bool]]) -> list[Block]:
        node = shape.element
        if shape.kind == "grpSp":
            inner = self._shapes(node, {})
            return [block for s in inner for block in self._shape_blocks(s, rels)]
        if shape.kind == "pic":
            return self._picture(node, rels)
        if shape.kind == "graphicFrame":
            out: list[Block] = []
            for table in descendants(node, "tbl"):
                out.extend(self._table(table, rels))
            for chart in descendants(node, "chart"):
                target = rels.get(rel_id(chart))
                if target and not target[2]:
                    out.extend(_chart(self.package, target[1]))
            for diagram in descendants(node, "relIds"):
                target = rels.get(rel_id(diagram, "dm"))
                if target and not target[2]:
                    out.extend(_smartart(self.package, target[1]))
            return out
        bulleted = shape.placeholder in ("body", "obj")
        blocks = self._text_blocks(node, rels, bulleted)
        blip = next(descendants(node, "blip"), None)
        if blip is not None:
            blocks.extend(self._picture(node, rels))
        return blocks

    def _picture(self, node: Element, rels: dict[str, tuple[str, str, bool]]) -> list[Block]:
        described = next(descendants(node, "cNvPr"), None)
        alt = attr(described, "descr") or "" if described is not None else ""
        out: list[Block] = []
        for blip in descendants(node, "blip"):
            target = rels.get(rel_id(blip, "embed"))
            if target and not target[2]:
                picture = self.ctx.picture(self.package.read(target[1]), target[1], alt)
                if picture is not None:
                    out.append(picture)
        return out

    def _text_blocks(
        self, node: Element, rels: dict[str, tuple[str, str, bool]], bulleted: bool
    ) -> list[Block]:
        body = child(node, "txBody")
        if body is None:
            return []
        return _drawing_text(body, rels, bulleted)

    def _table(self, table: Element, rels: dict[str, tuple[str, str, bool]]) -> list[Block]:
        rows: list[list[list[Block]]] = []
        for tr in children(table, "tr"):
            row: list[list[Block]] = []
            for tc in children(tr, "tc"):
                if _on_attr(attr(tc, "hMerge")) or _on_attr(attr(tc, "vMerge")):
                    row.append([])
                    continue
                body = child(tc, "txBody")
                row.append(_drawing_text(body, rels, False) if body is not None else [])
            rows.append(row)
        return _table_blocks(rows)

    def _notes(self, rels: dict[str, tuple[str, str, bool]]) -> list[Block]:
        for kind, target, external in rels.values():
            if kind != "notesSlide" or external:
                continue
            root = self.package.xml(target)
            if root is None:
                return []
            note_rels = relationships(self.package, target)
            out: list[Block] = []
            for sp in descendants(root, "sp"):
                ph_type, _ = _placeholder(sp)
                if ph_type == "body":
                    body = child(sp, "txBody")
                    if body is not None:
                        out.extend(_drawing_text(body, note_rels, False))
            return out
        return []


def _drawing_text(
    body: Element, rels: dict[str, tuple[str, str, bool]], bulleted: bool
) -> list[Block]:
    """A DrawingML text body: paragraphs, and bullets where the shape or the
    paragraph asks for them."""
    paragraphs: list[tuple[int, str, list[Inline], int]] = []
    for p in children(body, "p"):
        ppr = child(p, "pPr")
        level = _level_number(attr(ppr, "lvl")) if ppr is not None else 0
        start = 1
        auto = child(ppr, "buAutoNum")
        if child(ppr, "buNone") is not None:
            kind = "none"
        elif auto is not None:
            kind = "ordered"
            start = _count(attr(auto, "startAt"), 1, 10**6)
        elif child(ppr, "buChar") is not None or child(ppr, "buBlip") is not None:
            kind = "bullet"
        else:
            kind = "inherited" if bulleted else "none"
        content = _drawing_runs(p, rels)
        if not is_empty(content):
            paragraphs.append((level, kind, content, start))
    if len(paragraphs) == 1 and paragraphs[0][1] == "inherited" and paragraphs[0][0] == 0:
        # One statement in a text placeholder is a sentence, not a list.
        paragraphs = [(0, "none", paragraphs[0][2], 1)]
    out: list[Block] = []
    items: list[_Para] = []
    counters: dict[int, int] = {}
    for level, kind, content, start in paragraphs:
        if kind == "none":
            if items:
                out.extend(_nest_items(items))
                items = []
            counters.clear()
            out.append(Paragraph(content))
            continue
        counters = {k: v for k, v in counters.items() if k <= level}
        number = counters.get(level, start - 1) + 1
        counters[level] = number
        items.append(
            _Para(content=content, list_level=level, ordered=kind == "ordered", number=number)
        )
    if items:
        out.extend(_nest_items(items))
    return out


def _drawing_runs(p: Element, rels: dict[str, tuple[str, str, bool]]) -> list[Inline]:
    runs: list[Run] = []
    for node in p:
        name = local(node.tag)
        if name in ("r", "fld"):
            if name == "fld" and (attr(node, "type") or "").startswith(("slidenum", "datetime")):
                continue
            rpr = child(node, "rPr")
            fmt: set[str] = set()
            if rpr is not None:
                if _on_attr(attr(rpr, "b")):
                    fmt.add("strong")
                if _on_attr(attr(rpr, "i")):
                    fmt.add("emph")
                if (attr(rpr, "strike") or "noStrike") != "noStrike":
                    fmt.add("strike")
            link = ""
            click = child(rpr, "hlinkClick")
            if click is not None:
                target = rels.get(rel_id(click))
                if target and target[2]:
                    link = target[1]
            text = "".join(t.text or "" for t in children(node, "t"))
            baseline = attr(rpr, "baseline") if rpr is not None else None
            if baseline and baseline.lstrip("-").isdigit():
                text = superscript(text) if int(baseline) > 0 else subscript(text)
            runs.append((text, frozenset(fmt), link))
        elif name == "br":
            runs.append((Break(), frozenset(), ""))
    return assemble(runs)


def pptx(data: bytes, ctx: Context) -> Note:
    return _Slides(Package(data), ctx).convert()


# --------------------------------------------------------------------------
# Excel

_DATE_FORMATS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 27, 30, 36, 45, 47, 50, 57}
_DURATION_FORMATS = {46}
#: Columns beyond this are a sheet's formatting run to its edge, not data.
_MAX_SHEET_COLUMNS = 16384
#: How far into a sheet its column settings are looked for: they come before
#: the first row, within its first few kilobytes.
_SHEET_HEAD = 1024 * 1024
_PERCENT_FORMATS = {9, 10}


def _is_date_format(code: str) -> bool:
    code = re.sub(r'"[^"]*"', "", code)
    code = re.sub(r"\\.", "", code)
    code = re.sub(r"\[(?![hms]+\])[^\]]*\]", "", code, flags=re.IGNORECASE)
    code = code.split(";", 1)[0].lower()
    return "general" not in code and bool(re.search(r"[dmyhs]", code))


def _cell_ref(ref: str) -> tuple[int, int] | None:
    match = re.match(r"^\$?([A-Za-z]{1,3})\$?(\d{1,7})$", ref or "")
    if not match:
        return None
    column = 0
    for letter in match.group(1).upper():
        column = column * 26 + (ord(letter) - 64)
    return int(match.group(2)) - 1, column - 1


def _excel_date(serial: float, base_1904: bool) -> _dt.date | _dt.datetime | _dt.time | None:
    if not math.isfinite(serial) or serial < 0 or serial > 2958465:
        return None
    if base_1904:
        base = _dt.datetime(1904, 1, 1)
    elif serial < 61:
        # The days before Excel's imaginary 29 February 1900.
        base = _dt.datetime(1899, 12, 31)
    else:
        base = _dt.datetime(1899, 12, 30)
    stamp = base + _dt.timedelta(days=serial)
    stamp = stamp.replace(microsecond=0) + (
        _dt.timedelta(seconds=1) if stamp.microsecond >= 500000 else _dt.timedelta()
    )
    if serial < 1:
        # A time of day with no date: 0.5 is noon, in either date system.
        return stamp.time()
    if stamp.time() == _dt.time(0, 0):
        return stamp.date()
    return stamp


def _format_serial(value: _dt.date | _dt.datetime | _dt.time) -> str:
    if isinstance(value, _dt.datetime):
        return value.isoformat(sep=" ", timespec="minutes" if value.second == 0 else "seconds")
    if isinstance(value, _dt.time):
        return value.isoformat(timespec="minutes" if value.second == 0 else "seconds")
    return value.isoformat()


class _Workbook:
    def __init__(self, package: Package, ctx: Context) -> None:
        self.package = package
        self.ctx = ctx
        self.part = _main_part(package, "xl/workbook.xml")
        self.rels = relationships(package, self.part)
        self.shared: list[str] = []
        self.formats: list[str] = []
        folder = posixpath.dirname(self.part)
        for kind, target, external in self.rels.values():
            if external:
                continue
            if kind == "sharedStrings":
                self._read_shared(target)
            elif kind == "styles":
                self._read_styles(target)
        if not self.shared and package.find(f"{folder}/sharedStrings.xml"):
            self._read_shared(f"{folder}/sharedStrings.xml")

    def _read_shared(self, part: str) -> None:
        # Plain text, or rich text runs; phonetic readings (`rPh`) are a
        # reading aid, not part of the value. Streamed: a workbook's strings
        # can run to hundreds of megabytes.
        for si in self.package.stream_elements(part, "si"):
            texts = [t.text or "" for t in children(si, "t")]
            for run in children(si, "r"):
                texts.extend(t.text or "" for t in children(run, "t"))
            self.shared.append(_xstring("".join(texts)))

    def _read_styles(self, part: str) -> None:
        root = self.package.xml(part)
        if root is None:
            return
        custom: dict[int, str] = {}
        for fmt in descendants(root, "numFmt"):
            try:
                custom[int(attr(fmt, "numFmtId") or -1)] = attr(fmt, "formatCode") or ""
            except ValueError:
                continue
        xfs = child(root, "cellXfs")
        if xfs is None:
            return
        for xf in children(xfs, "xf"):
            try:
                number = int(attr(xf, "numFmtId") or 0)
            except ValueError:
                number = 0
            if number in _DATE_FORMATS:
                self.formats.append("date")
            elif number in _DURATION_FORMATS:
                self.formats.append("duration")
            elif number in _PERCENT_FORMATS:
                self.formats.append("percent")
            elif number in custom:
                code = re.sub(r'"[^"]*"', "", custom[number])
                if re.search(r"\[(h+|m+|s+)\]", code, re.IGNORECASE):
                    self.formats.append("duration")
                elif "%" in code:
                    self.formats.append("percent")
                elif _is_date_format(code):
                    self.formats.append("date")
                else:
                    self.formats.append("")
            else:
                self.formats.append("")

    def convert(self) -> Note:
        root = self.package.xml(self.part)
        if root is None:
            raise ConversionError("no workbook part")
        props = child(root, "workbookPr")
        base_1904 = _on_attr(attr(props, "date1904")) if props is not None else False
        sheets = child(root, "sheets")
        blocks: list[Block] = []
        names: list[str] = []
        truncated = False
        for sheet in children(sheets, "sheet") if sheets is not None else []:
            if (attr(sheet, "state") or "visible") != "visible":
                # Hidden on purpose: helper tables, and sometimes secrets.
                continue
            name = attr(sheet, "name") or f"Sheet {len(names) + 1}"
            target = self.rels.get(rel_id(sheet))
            if not target or target[2] or target[0] != "worksheet":
                continue
            names.append(name)
            rows, dropped = self._rows(target[1], base_1904, self.ctx.options.max_rows)
            truncated |= dropped > 0
            blocks.append(Marker(f"sheet {name}"))
            blocks.append(Heading(2, [name]))
            blocks.extend(_sheet_blocks(rows, dropped))
        meta = office_properties(self.package)
        meta["sheets"] = len(names)
        if truncated:
            meta["truncated"] = True
        return Note(blocks=blocks, meta=meta)

    def _rows(
        self, part: str, base_1904: bool, limit: int | None
    ) -> tuple[dict[int, dict[int, str]], int]:
        """A sheet's non-empty rows, row → column → text, read as a stream,
        and how many rows beyond `limit` there were.

        Hidden rows and columns are left out, as hidden sheets are; the rows
        around a hidden one follow each other, as they do on screen.
        """
        rows: dict[int, dict[int, str]] = {}
        dropped = 0
        next_row = 0
        hidden_rows = 0
        hidden_columns = self._hidden_columns(part)
        for row in self.package.stream_elements(part, "row"):
            try:
                index = int(attr(row, "r") or 0) - 1
            except ValueError:
                index = -1
            if index < 0:
                index = next_row
            next_row = index + 1
            if _on_attr(attr(row, "hidden")):
                hidden_rows += 1
                continue
            index -= hidden_rows
            values: dict[int, str] = {}
            next_column = 0
            for cell in children(row, "c"):
                ref = _cell_ref(attr(cell, "r") or "")
                column = ref[1] if ref else next_column
                next_column = column + 1
                if column > _MAX_SHEET_COLUMNS or hidden_columns[column]:
                    continue
                text = self._value(cell, base_1904)
                if text:
                    values[column] = text
            if not values:
                continue
            if (limit is not None and len(rows) >= limit) or self.ctx.cells_left < len(values):
                dropped += 1
                continue
            self.ctx.cells_left -= len(values)
            rows[index] = values
        return rows, dropped

    def _hidden_columns(self, part: str) -> bytearray:
        """Which columns a sheet hides, a flag per column, from its column
        settings (`<cols>`). They come before its rows, if at all: the sheet
        is looked into that far, not read through twice."""
        hidden = bytearray(_MAX_SHEET_COLUMNS + 1)
        found = self.package.find(part)
        if found is None:
            return hidden
        with self.package.zip.open(found) as raw:
            head = raw.read(_SHEET_HEAD)
        opening = re.search(rb"<(?:[\w.-]+:)?(cols|sheetData)[\s/>]", head)
        if opening is None or opening.group(1) != b"cols":
            return hidden
        settings = self.package.stream_elements(found, "cols", _SHEET_HEAD)
        try:
            cols = next(settings, None)
        except ConversionError:
            cols = None
        finally:
            settings.close()  # type: ignore[attr-defined]
        for col in children(cols, "col") if cols is not None else []:
            if not _on_attr(attr(col, "hidden")):
                continue
            try:
                first = max(int(attr(col, "min") or 0), 1) - 1
                last = min(int(attr(col, "max") or attr(col, "min") or 0), len(hidden))
            except ValueError:
                continue
            if first < last:
                hidden[first:last] = b"\x01" * (last - first)
        return hidden

    def _value(self, cell: Element, base_1904: bool) -> str:
        kind = attr(cell, "t") or "n"
        value_el = child(cell, "v")
        raw = value_el.text if value_el is not None and value_el.text is not None else ""
        if kind == "s":
            try:
                return self.shared[int(raw)]
            except (ValueError, IndexError):
                return ""
        if kind == "inlineStr":
            inline_el = child(cell, "is")
            return (
                _xstring("".join(t.text or "" for t in descendants(inline_el, "t")))
                if inline_el is not None
                else ""
            )
        if kind == "str":
            return _xstring(raw)
        if kind in ("e", "d"):
            return raw
        if kind == "b":
            return "TRUE" if raw.strip() in ("1", "true") else "FALSE"
        if not raw:
            return ""
        try:
            number = float(raw)
        except ValueError:
            return raw
        try:
            style = int(attr(cell, "s") or 0)
        except ValueError:
            style = 0
        fmt = self.formats[style] if 0 <= style < len(self.formats) else ""
        if fmt == "date":
            converted = _excel_date(number, base_1904)
            if converted is not None:
                return _format_serial(converted)
        if fmt == "duration" and math.isfinite(number):
            return _duration(number)
        if fmt == "percent":
            return f"{_number(repr(round(number * 100, 10)))} %"
        return _number(raw)


def _duration(days: float) -> str:
    """Elapsed time as Excel's `[h]:mm` shows it: 1.5 days is 36:00."""
    seconds = round(abs(days) * 86400)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    sign = "-" if days < 0 else ""
    text = f"{sign}{hours}:{minutes:02d}"
    return f"{text}:{seconds:02d}" if seconds else text


def _sheet_blocks(rows: dict[int, dict[int, str]], dropped: int = 0) -> list[Block]:
    """A sheet's cells as tables, split where whole rows are empty.

    A sheet often holds a title, a few lines of notes and then the table; a
    run of rows with a single value each is text, not a one-column table.
    """
    out: list[Block] = []
    group: list[int] = []

    def flush() -> None:
        if not group:
            return
        columns = sorted({c for r in group for c in rows[r]})
        lines = [[rows[r].get(c, "") for c in columns] for r in group]
        if all(len(rows[r]) <= 1 for r in group):
            out.extend(Paragraph([next(iter(rows[r].values()))]) for r in group)
        else:
            out.append(Table([[[v] for v in line] for line in lines]))
        group.clear()

    for index in sorted(rows):
        if group and index != group[-1] + 1:
            flush()
        group.append(index)
    flush()
    if dropped:
        out.append(Paragraph([Span("emph", [f"{dropped} more rows not shown"])]))
    return out


def xlsx(data: bytes, ctx: Context) -> Note:
    return _Workbook(Package(data), ctx).convert()
