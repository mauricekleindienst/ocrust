"""HTML as blocks: web pages, e-books' chapters, and the bodies of mails.

HTML in the wild is not well-formed, so the page is first built into a tree
the way a browser would — a new `<p>` closes the open one, a `<td>` the cell
before it — and only then read. Tables that only lay the page out, which is
how most mails are built, are read as the text they hold; tables of data stay
tables.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urljoin

from ._context import Context
from ._ir import (
    Block,
    Break,
    Code,
    Heading,
    Image,
    Inline,
    ListBlock,
    Note,
    Paragraph,
    Quote,
    Rule,
    Run,
    Span,
    Table,
    assemble,
    flatten,
    is_empty,
    strip,
    subscript,
    superscript,
)

_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_CLOSES_P = {
    "address",
    "article",
    "aside",
    "blockquote",
    "details",
    "div",
    "dl",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "ul",
}
# A start tag that closes the open element of these kinds, up to the boundary.
_IMPLIED_END = {
    "li": ({"li"}, {"ul", "ol", "menu"}),
    "dt": ({"dt", "dd"}, {"dl"}),
    "dd": ({"dt", "dd"}, {"dl"}),
    "tr": ({"tr", "td", "th"}, {"table", "thead", "tbody", "tfoot"}),
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "thead": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
    "tbody": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
    "tfoot": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
    "option": ({"option"}, {"select", "datalist"}),
}
_SKIP = {
    "script",
    "style",
    "noscript",
    "template",
    "head",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
    "select",
    "button",
    "nav",
    "map",
    "audio",
    "video",
    "meta",
    "link",
    "title",
}
#: MathML's elements, and those MathJax sets around it. A formula is part of
#: the line it stands in, and so is every piece of it: an `<mi>` is not a
#: paragraph of its own.
_MATHML = {
    "math",
    "annotation",
    "annotation-xml",
    "maction",
    "menclose",
    "merror",
    "mfenced",
    "mfrac",
    "mglyph",
    "mi",
    "mlabeledtr",
    "mlongdiv",
    "mmultiscripts",
    "mn",
    "mo",
    "mover",
    "mpadded",
    "mphantom",
    "mprescripts",
    "mroot",
    "mrow",
    "ms",
    "mscarries",
    "mscarry",
    "msgroup",
    "msline",
    "mspace",
    "msqrt",
    "msrow",
    "mstack",
    "mstyle",
    "msub",
    "msubsup",
    "msup",
    "mtable",
    "mtd",
    "mtext",
    "mtr",
    "munder",
    "munderover",
    "none",
    "semantics",
    # MathJax's own, around the MathML it keeps for screen readers.
    "mjx-assistive-mml",
    "mjx-container",
}
_INLINE = _MATHML | {
    "a",
    "abbr",
    "acronym",
    "b",
    "bdi",
    "bdo",
    "big",
    "br",
    "cite",
    "code",
    "data",
    "del",
    "dfn",
    "em",
    "font",
    "i",
    "img",
    "input",
    "ins",
    "kbd",
    "label",
    "mark",
    "q",
    "rp",
    "rt",
    "ruby",
    "s",
    "samp",
    "small",
    "span",
    "strike",
    "strong",
    "sub",
    "sup",
    "time",
    "tt",
    "u",
    "var",
    "wbr",
}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_STRUCTURE = {"table", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "pre", "dl"}
_LAYOUT_STRUCTURE = {"table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "article"}
_WHITESPACE = re.compile(r"[ \t\n\r\f]+")


@dataclass
class Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Element | str] = field(default_factory=list)

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def find_all(self, tag: str) -> list[Element]:
        found: list[Element] = []
        for c in self.children:
            if isinstance(c, Element):
                if c.tag == tag:
                    found.append(c)
                found.extend(c.find_all(tag))
        return found

    def has_any(self, tags: set[str]) -> bool:
        return any(
            isinstance(c, Element) and (c.tag in tags or c.has_any(tags)) for c in self.children
        )


#: Elements open inside one another at most. A generated page that opens a
#: `<font>` or a `<div>` on every line and never closes one is read as a
#: browser shows it, not refused as nested too deeply: past this depth, text
#: formatting, plain containers and the pieces of a formula are let go and
#: their content stays where it is. Everything that decides what is read — a
#: script, a hidden element, a table — keeps its place.
_MAX_DEPTH = 100
_LET_GO = _MATHML | {
    "a",
    "article",
    "blockquote",
    "center",
    "div",
    "footer",
    "header",
    "main",
    "section",
    "abbr",
    "b",
    "big",
    "cite",
    "em",
    "font",
    "i",
    "ins",
    "mark",
    "nobr",
    "q",
    "s",
    "small",
    "span",
    "strike",
    "strong",
    "sub",
    "sup",
    "tt",
    "u",
}


def _tag(tag: str) -> str:
    """An element's name. MathML in an e-book's XHTML may carry a prefix —
    `<m:math>`, `<mml:mi>` — and is read as the element it is."""
    tag = tag.lower()
    if ":" in tag:
        local = tag.rsplit(":", 1)[1]
        if local in _MATHML:
            return local
    return tag


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#root")
        self.stack: list[Element] = [self.root]
        #: Elements let go past the depth cap, by tag: their end tags close
        #: nothing, rather than an element opened before them.
        self.let_go: dict[str, int] = {}

    def _close(self, tags: set[str], boundary: set[str]) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[index].tag
            if tag in tags:
                del self.stack[index:]
                return
            if tag in boundary:
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = _tag(tag)
        if tag in _CLOSES_P:
            self._close(
                {"p"}, {"div", "td", "th", "li", "blockquote", "section", "article", "body"}
            )
        if tag in _IMPLIED_END:
            closes, boundary = _IMPLIED_END[tag]
            self._close(closes, boundary)
        node = Element(tag, {k.lower(): v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag in _VOID:
            return
        if len(self.stack) < _MAX_DEPTH or tag not in _LET_GO or _invisible(node):
            self.stack.append(node)
        else:
            self.let_go[tag] = self.let_go.get(tag, 0) + 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = _tag(tag)
        node = Element(tag, {k.lower(): v or "" for k, v in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = _tag(tag)
        if self.let_go.get(tag):
            self.let_go[tag] -= 1
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse(text: str) -> Element:
    builder = _Builder()
    builder.feed(text)
    builder.close()
    return builder.root


#: Finds a picture's bytes and name from its `src`, or None.
Resolver = Callable[[str], "tuple[bytes, str] | None"]


def data_uri(src: str) -> tuple[bytes, str] | None:
    match = re.match(r"^data:([\w/+.-]+)?(;[^,]*)?,(.*)$", src, re.S)
    if not match:
        return None
    kind = match.group(1) or "application/octet-stream"
    payload = match.group(3)
    try:
        if match.group(2) and "base64" in match.group(2):
            data = base64.b64decode(payload, validate=False)
        else:
            data = unquote(payload).encode("latin-1", "replace")
    except (binascii.Error, ValueError):
        return None
    suffix = kind.rsplit("/", 1)[-1].replace("jpeg", "jpg").split("+", 1)[0]
    return data, f"image.{suffix}"


class _Converter:
    def __init__(
        self,
        ctx: Context,
        resolve: Resolver | None,
        base: str,
        formulas: _Formulas | None = None,
    ) -> None:
        self.ctx = ctx
        self.resolve = resolve
        self.base = base
        self.formulas = formulas if formulas is not None else _Formulas()

    # -- blocks

    def blocks(self, node: Element) -> list[Block]:
        out: list[Block] = []
        pending: list[Element | str] = []

        def flush() -> None:
            if pending:
                extras: list[Block] = []
                content = self.inlines(pending, extras)
                if not is_empty(content):
                    out.append(Paragraph(content))
                out.extend(extras)
                pending.clear()

        for child in node.children:
            # A `<font>` around a table, the way old pages and mails are set,
            # holds blocks: those are read as blocks, not as one line. So does
            # a formula set apart from the text around it.
            if isinstance(child, str) or (
                child.tag in _INLINE
                and not child.has_any(_STRUCTURE)
                and id(child) not in self.formulas.displayed
            ):
                pending.append(child)
                continue
            flush()
            inner = self.block(child)
            if child.tag == "a" and inner:
                # A link around a heading and its teaser — a card on an index
                # page: the heading carries the link.
                inner = self._linked(inner, child.attrs.get("href", ""))
            out.extend(inner)
        flush()
        return out

    def _linked(self, blocks: list[Block], href: str) -> list[Block]:
        href = href.strip()
        if not href or href.lower().startswith(("javascript:", "#", "data:")):
            return blocks
        link = urljoin(self.base, href) if self.base else href
        for index, block in enumerate(blocks):
            if isinstance(block, (Heading, Paragraph)) and not is_empty(block.content):
                content = [Span("link", strip(block.content), link)]
                blocks[index] = (
                    Heading(block.level, content)
                    if isinstance(block, Heading)
                    else Paragraph(content)
                )
                break
        return blocks

    def block(self, node: Element) -> list[Block]:
        tag = node.tag
        if tag in _SKIP:
            return []
        if _invisible(node):
            copy = self.formulas.copies.get(id(node))
            return self.formula_block(copy) if copy is not None else []
        if tag == "math":
            return self.formula_block(node)
        if tag in _HEADINGS:
            extras: list[Block] = []
            content = self.inlines(node.children, extras)
            return ([Heading(_HEADINGS[tag], content)] if not is_empty(content) else []) + extras
        if tag == "p":
            if node.has_any(_STRUCTURE | {"div", "p"}) or id(node) in self.formulas.displayed:
                return self.blocks(node)
            extras = []
            content = self.inlines(node.children, extras)
            return ([Paragraph(content)] if not is_empty(content) else []) + extras
        if tag in ("ul", "ol", "menu"):
            return self.list(node)
        if tag == "table":
            return self.table(node)
        if tag == "pre":
            return self.pre(node)
        if tag == "blockquote":
            inner = self.blocks(node)
            return [Quote(inner)] if inner else []
        if tag == "hr":
            return [Rule()]
        if tag == "dl":
            return self.definitions(node)
        if tag == "figcaption":
            extras = []
            content = self.inlines(node.children, extras)
            return [Paragraph([Span("emph", strip(content))])] if not is_empty(content) else []
        if tag == "details":
            return self.blocks(node)
        if tag == "summary":
            extras = []
            content = self.inlines(node.children, extras)
            return [Paragraph([Span("strong", strip(content))])] if not is_empty(content) else []
        if tag in ("li", "dd", "dt"):
            return self.blocks(node)
        return self.blocks(node)

    def list(self, node: Element) -> list[Block]:
        items: list[list[Block]] = []
        loose: list[Element | str] = []
        for child in node.children:
            if isinstance(child, Element) and child.tag == "li":
                if loose:
                    items.append(self.blocks(Element("li", children=loose)))
                    loose = []
                items.append(self.blocks(child))
            elif isinstance(child, Element) and child.tag in ("ul", "ol") and items:
                items[-1].extend(self.list(child))
            elif isinstance(child, str) and not child.strip():
                continue
            else:
                loose.append(child)
        if loose:
            items.append(self.blocks(Element("li", children=loose)))
        items = [item for item in items if item]
        if not items:
            return []
        try:
            start = int(node.attrs.get("start", "1"))
        except ValueError:
            start = 1
        return [ListBlock(ordered=node.tag == "ol", items=items, start=start)]

    def definitions(self, node: Element) -> list[Block]:
        out: list[Block] = []
        for child in node.children:
            if not isinstance(child, Element):
                continue
            extras: list[Block] = []
            if child.tag == "dt":
                content = self.inlines(child.children, extras)
                if not is_empty(content):
                    out.append(Paragraph([Span("strong", strip(content))]))
            elif child.tag == "dd":
                out.extend(self.blocks(child))
            out.extend(extras)
        return out

    def pre(self, node: Element) -> list[Block]:
        text = _pre_text(node)
        if text.startswith("\n"):
            text = text[1:]
        language = ""
        for candidate in (node, *node.find_all("code")):
            match = re.search(r"(?:language|lang)-([\w+#.-]+)", candidate.attrs.get("class", ""))
            if match:
                language = match.group(1)
                break
        return [Code(text.rstrip(), language)] if text.strip() else []

    def table(self, node: Element) -> list[Block]:
        rows = _table_rows(node)
        if not rows:
            return []
        cells = [cell for row in rows for cell in row]
        # A table that lays out the page — a mail's frame, a newsletter's
        # columns — holds tables, headings or whole articles in its cells. A
        # table of data may hold a list or two paragraphs in a cell and is
        # still a table.
        text_length = sum(len(cell.text()) for cell in cells)
        layout = (
            node.attrs.get("role") == "presentation"
            or any(cell.has_any(_LAYOUT_STRUCTURE) for cell in cells)
            or (len(cells) <= 2 and text_length > 400)
            or text_length > 600 * max(len(cells), 1)
        )
        if layout:
            out: list[Block] = []
            for cell in cells:
                out.extend(self.blocks(cell))
            return out
        grid: list[list[list[Inline]]] = []
        pending: dict[int, int] = {}
        for row in rows:
            line: list[list[Inline]] = []
            column = 0
            for cell in row:
                while pending.get(column, 0) > 0:
                    pending[column] -= 1
                    line.append([])
                    column += 1
                content = flatten(self.blocks(cell))
                span = _span(cell.attrs.get("colspan"))
                down = _span(cell.attrs.get("rowspan")) - 1
                for offset in range(span):
                    line.append(content if offset == 0 else [])
                    if down > 0:
                        pending[column + offset] = down
                column += span
            while pending.get(column, 0) > 0:
                pending[column] -= 1
                line.append([])
                column += 1
            grid.append(line)
        grid = [line for line in grid if any(not is_empty(c) for c in line)]
        if not grid:
            return []
        width = max(len(line) for line in grid)
        used = [c for c in range(width) if any(c < len(ln) and not is_empty(ln[c]) for ln in grid)]
        if len(used) <= 1:
            return [Paragraph(line[used[0]]) for line in grid if used and used[0] < len(line)]
        caption = next(
            (c for c in node.children if isinstance(c, Element) and c.tag == "caption"), None
        )
        out = []
        if caption is not None:
            extras: list[Block] = []
            content = self.inlines(caption.children, extras)
            if not is_empty(content):
                out.append(Paragraph([Span("emph", strip(content))]))
        out.append(Table([[line[c] if c < len(line) else [] for c in used] for line in grid]))
        return out

    # -- inline content

    def inlines(self, nodes: list[Element | str], extras: list[Block]) -> list[Inline]:
        runs: list[Run] = []
        for node in nodes:
            self._walk(node, runs, frozenset(), "", extras)
        return assemble(runs)

    def _walk(
        self,
        node: Element | str,
        runs: list[Run],
        fmt: frozenset[str],
        link: str,
        extras: list[Block],
        script: str = "",
    ) -> None:
        if isinstance(node, str):
            text = _WHITESPACE.sub(" ", node)
            if script == "sup":
                text = superscript(text)
            elif script == "sub":
                text = subscript(text)
            runs.append((text, fmt, link))
            return
        tag = node.tag
        if tag in _SKIP:
            return
        if _invisible(node):
            copy = self.formulas.copies.get(id(node))
            if copy is not None:
                self.formula(copy, runs, fmt, link)
            return
        if tag == "math":
            self.formula(node, runs, fmt, link)
            return
        if tag == "br":
            runs.append((Break(), frozenset(), link))
            return
        if tag == "img":
            if id(node) in self.formulas.pictures:
                return
            picture = self.image(node)
            if picture is not None:
                extras.append(picture)
            return
        if tag == "input":
            if node.attrs.get("type", "").lower() in ("checkbox", "radio"):
                runs.append(("☒ " if "checked" in node.attrs else "☐ ", fmt, link))
            return
        if tag in ("code", "kbd", "samp", "tt") and not node.has_any({"br"}):
            runs.append((Span("code", [node.text()]), fmt, link))
            return
        inner = set(fmt)
        if tag in ("strong", "b"):
            inner.add("strong")
        elif tag in ("em", "i", "cite", "dfn", "var"):
            inner.add("emph")
        elif tag in ("s", "strike", "del"):
            inner.add("strike")
        if tag == "a":
            href = node.attrs.get("href", "").strip()
            if href and not href.lower().startswith(("javascript:", "#", "data:")):
                link = urljoin(self.base, href) if self.base else href
                if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", link):
                    link = ""
        if tag == "q":
            runs.append(("„" if False else "“", frozenset(inner), link))
        block_like = tag not in _INLINE
        if block_like and runs:
            runs.append((Break(), frozenset(), link))
        child_script = tag if tag in ("sup", "sub") else script
        for child in node.children:
            self._walk(child, runs, frozenset(inner), link, extras, child_script)
        if tag == "q":
            runs.append(("”", frozenset(inner), link))
        if block_like:
            runs.append((Break(), frozenset(), link))

    def formula(self, node: Element, runs: list[Run], fmt: frozenset[str], link: str) -> None:
        """A `<math>` as one formula in TeX, set apart where it says so."""
        tex = _tex(node)
        if not tex:
            return
        if runs and isinstance(runs[-1][0], Span) and runs[-1][0].kind in _FORMULA_KINDS:
            # Two formulas that touch would be joined into one.
            runs.append((" ", fmt, link))
        kind = "displaymath" if _display(node) else "math"
        runs.append((Span(kind, [tex]), fmt, link))

    def formula_block(self, node: Element) -> list[Block]:
        runs: list[Run] = []
        self.formula(node, runs, frozenset(), "")
        return [Paragraph(assemble(runs))] if runs else []

    def image(self, node: Element) -> Image | None:
        alt = _WHITESPACE.sub(" ", node.attrs.get("alt", "")).strip()
        src = node.attrs.get("src", "").strip()
        found = None
        if src:
            found = (
                data_uri(src)
                if src.startswith("data:")
                else (self.resolve(src) if self.resolve else None)
            )
        described = alt if len(alt.split()) >= 2 and not re.search(r"\.\w{2,4}$", alt) else ""
        if found is None:
            return Image(alt=described) if described else None
        data, name = found
        return self.ctx.picture(data, name, described)


def _pre_text(node: Element) -> str:
    """Preformatted text, a `<br>` as the line break it shows."""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == "br":
            parts.append("\n")
        elif child.tag not in _SKIP:
            parts.append(_pre_text(child))
    return "".join(parts)


def _span(value: str | None) -> int:
    try:
        return min(max(int(value or 1), 1), 50)
    except ValueError:
        return 1


def _invisible(node: Element) -> bool:
    style = node.attrs.get("style", "").replace(" ", "").lower()
    return (
        "hidden" in node.attrs
        or "display:none" in style
        or "visibility:hidden" in style
        or node.attrs.get("aria-hidden") == "true"
    )


# -- formulas

#: A formula as inline content: `$…$` in its line, `$$…$$` set apart.
_FORMULA_KINDS = ("math", "displaymath")
#: The encoding of an annotation that holds the TeX a formula was written in.
_TEX_ENCODING = re.compile(r"(?<![a-z])(?:la)?tex(?![a-z])", re.I)
_TEX_STYLE = re.compile(r"\{\\(?:display|text)style(?![A-Za-z])\s*(.*)\}", re.S)
#: What only TeX writes: a command, a script, a group in braces.
_TEX_MARKUP = re.compile(r"\\[A-Za-z]+|[\^_{}]")
#: Words a formula sets as text, `\text{for all}`: TeX may hold those too.
_TEX_WORDS = re.compile(r"\\(?:text[a-z]*|mathrm|operatorname|mbox)\s*\{[^{}]*\}")
#: Three words in a row, none of them a command: said, not written.
_SPOKEN = re.compile(r"(?<![\\\w])[^\W\d_]{2,}(?:\s+[^\W\d_]{2,}){2}(?!\w)")
_TOKENS = {"mi", "mn", "mo", "ms", "mtext"}
#: The functions TeX sets upright under a name of its own: `\sin x`, `\lim`.
_FUNCTIONS = frozenset(
    {
        "arccos",
        "arcsin",
        "arctan",
        "arg",
        "cos",
        "cosh",
        "cot",
        "coth",
        "csc",
        "deg",
        "det",
        "dim",
        "exp",
        "gcd",
        "hom",
        "inf",
        "ker",
        "lg",
        "lim",
        "liminf",
        "limsup",
        "ln",
        "log",
        "max",
        "min",
        "Pr",
        "sec",
        "sin",
        "sinh",
        "sup",
        "tan",
        "tanh",
    }
)
#: The large operators a formula sets its limits on, as TeX.
_LARGE_OPERATORS = {
    "∑": "\\sum",
    "∏": "\\prod",
    "∐": "\\coprod",
    "∫": "\\int",
    "∬": "\\iint",
    "∭": "\\iiint",
    "∮": "\\oint",
    "⋃": "\\bigcup",
    "⋂": "\\bigcap",
    "⋁": "\\bigvee",
    "⋀": "\\bigwedge",
    "⨁": "\\bigoplus",
    "⨂": "\\bigotimes",
    "⨀": "\\bigodot",
}
#: Marks set over a letter, by the character a document keeps for them —
#: combining or spacing — as TeX's accents: `\hat{x}`, `\vec{v}`.
_ACCENTS = {
    "\u0300": "\\grave",
    "`": "\\grave",
    "\u0301": "\\acute",
    "´": "\\acute",
    "\u0302": "\\hat",
    "^": "\\hat",
    "ˆ": "\\hat",
    "\u0303": "\\tilde",
    "~": "\\tilde",
    "˜": "\\tilde",
    "\u0304": "\\bar",
    "\u0305": "\\overline",
    "¯": "\\overline",
    "‾": "\\overline",
    "\u0306": "\\breve",
    "˘": "\\breve",
    "\u0307": "\\dot",
    "˙": "\\dot",
    "\u0308": "\\ddot",
    "¨": "\\ddot",
    "\u030c": "\\check",
    "ˇ": "\\check",
    "\u20d6": "\\overleftarrow",
    "←": "\\overleftarrow",
    "\u20d7": "\\vec",
    "→": "\\vec",
    "\u20db": "\\dddot",
    "\u20e1": "\\overleftrightarrow",
    "↔": "\\overleftrightarrow",
    "⏞": "\\overbrace",
}
#: Marks set under a part: a line, a brace.
_UNDER_ACCENTS = {
    "\u0332": "\\underline",
    "_": "\\underline",
    "‾": "\\underline",
    "¯": "\\underline",
    "⏟": "\\underbrace",
}
#: Brackets TeX writes as a command, not as the character.
_FENCES = {
    "{": "\\{",
    "}": "\\}",
    "⟨": "\\langle",
    "⟩": "\\rangle",
    "〈": "\\langle",
    "〉": "\\rangle",
    "⌊": "\\lfloor",
    "⌋": "\\rfloor",
    "⌈": "\\lceil",
    "⌉": "\\rceil",
    "‖": "\\|",
    "\\": "\\backslash",
}
#: The brackets that open and close a part in MathML's own markup.
_OPENING = set("([{|‖⟨〈⌊⌈")
_CLOSING = set(")]}|‖⟩〉⌋⌉")
#: What stands taller than a line: brackets around it grow with it.
_TALL = re.compile(r"\\(?:frac|atop|begin|sum|prod|coprod|i+nt|oint|big)")
#: A command's name at the end of TeX: a letter right after it would lengthen it.
_COMMAND_END = re.compile(r"\\[A-Za-z]+$")
#: The scripts of each kind, in the order MathML gives them: `x_{i}^{2}`.
_SCRIPTS = {
    "msub": "_",
    "msup": "^",
    "msubsup": "_^",
    "munder": "_",
    "mover": "^",
    "munderover": "_^",
}
#: What TeX reads as markup, as the characters it is; and the invisible
#: operators MathML sets between a function and its argument, which TeX does
#: without.
_TEX_TEXT = str.maketrans(
    {
        "\\": "\\backslash ",
        "{": "\\{",
        "}": "\\}",
        "$": "\\$",
        "%": "\\%",
        "#": "\\#",
        "&": "\\&",
        "_": "\\_",
        "^": "\\hat{}",
        "~": "\\sim ",
        "⁡": "",
        "⁢": "",
        "⁣": "",
        "⁤": "",
    }
)


def _display(math: Element) -> bool:
    """Whether a formula is set apart from the text: `display="block"`, or
    MathML 1's `mode="display"`."""
    return (
        math.attrs.get("display", "").strip().lower() == "block"
        or math.attrs.get("mode", "").strip().lower() == "display"
    )


def _tex(math: Element) -> str:
    """A formula as TeX. Wikipedia, KaTeX, MathJax and Pandoc keep the TeX it
    was written in as an annotation, and that is read first; otherwise the
    formula is read from the MathML's elements.

    `alttext` is what a screen reader says in the formula's place. An
    accessible e-book puts words there — "a squared plus b squared" — which
    are no formula, while Wikipedia and LaTeXML put the TeX. It is read only
    where the elements give nothing, and only when it is written in TeX.
    """
    parts = [child for child in math.children if isinstance(child, Element)]
    if len(parts) == 1 and parts[0].tag == "semantics":
        for note in parts[0].children:
            if (
                isinstance(note, Element)
                and note.tag == "annotation"
                and _TEX_ENCODING.search(note.attrs.get("encoding", ""))
            ):
                tex = _unwrapped(note.text())
                if tex:
                    return tex
    tex = _WHITESPACE.sub(" ", _linear(math)).strip()
    if tex:
        return tex
    alttext = math.attrs.get("alttext", "")
    return _unwrapped(alttext) if _written_in_tex(alttext) else ""


def _written_in_tex(text: str) -> bool:
    """Whether a formula's `alttext` is TeX — `{\\displaystyle E=mc^{2}}` —
    and not the words a screen reader says for it."""
    return bool(_TEX_MARKUP.search(text)) and not _SPOKEN.search(_TEX_WORDS.sub(" ", text))


def mathml_tex(data: bytes) -> str:
    """The TeX of a formula kept as a MathML file of its own: LibreOffice
    stores each formula of a document that way, beside its text. The file is
    read as the MathML of a web page is, with the same limits."""
    maths = parse(decode_html(data)).find_all("math")
    return _tex(maths[0]) if maths else ""


def _unwrapped(tex: str) -> str:
    """TeX without the `{\\displaystyle …}` MediaWiki wraps every formula in."""
    tex = _WHITESPACE.sub(" ", tex).strip()
    match = _TEX_STYLE.fullmatch(tex)
    if match and _balanced(match.group(1)):
        return match.group(1).strip()
    return tex


def _balanced(tex: str) -> bool:
    """Whether every brace in `tex` that closes was opened in it, and every
    one that opens is closed."""
    depth = 0
    for char in re.sub(r"\\.", "", tex, flags=re.S):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _linear(node: Element) -> str:
    """MathML read as TeX from its elements: a superscript as `^{}`, a
    subscript as `_{}`, a fraction as `\\frac{}{}`, a root as `\\sqrt{}`, a
    matrix row by row, and the rest as the text it holds. A piece that is
    missing is left out, not guessed: malformed MathML still reads as its text.
    """
    tag = node.tag
    if tag in _TOKENS:
        text = _WHITESPACE.sub(" ", node.text())
        word = text.strip()
        if tag in ("mi", "mo") and word in _FUNCTIONS:
            # `sin` as TeX sets it: upright, and apart from what follows.
            return f"\\{word} "
        if tag == "mo" and word in _LARGE_OPERATORS:
            return f"{_LARGE_OPERATORS[word]} "
        if tag != "mtext":
            return word.translate(_TEX_TEXT)
        # Words in a formula, `if` and `for all`, keep the spaces around them.
        return f"\\text{{{text.translate(_TEX_TEXT)}}}" if text.strip() else ""
    if tag in _SKIP or tag in ("annotation", "annotation-xml", "mphantom", "mprescripts", "none"):
        return ""
    if tag == "mspace":
        return " "
    if tag == "mglyph":
        return node.attrs.get("alt", "").translate(_TEX_TEXT)
    if tag == "mtable":
        rows: list[str] = []
        for row in node.children:
            if not isinstance(row, Element):
                continue
            cells = [c for c in row.children if isinstance(c, Element)]
            if row.tag == "mlabeledtr":
                cells = cells[1:]
            elif row.tag != "mtr":
                cells = [row]
            rows.append(" & ".join(_linear(cell) for cell in cells))
        return "\\begin{matrix} " + " \\\\ ".join(rows) + " \\end{matrix}"
    args = [
        _linear(child)
        if isinstance(child, Element)
        else _WHITESPACE.sub(" ", child).strip().translate(_TEX_TEXT)
        for child in node.children
        if isinstance(child, Element) or child.strip()
    ]
    parts = [child for child in node.children if isinstance(child, Element)]
    if tag in ("mover", "munder") and len(args) == len(parts) == 2:
        # A mark over or under a part is an accent: `\hat{x}`, not `x^{\hat{}}`.
        marks = _ACCENTS if tag == "mover" else _UNDER_ACCENTS
        accent = marks.get(parts[1].text().strip())
        if accent:
            return f"{accent}{{{args[0]}}}"
    if tag in _SCRIPTS and len(args) > 1:
        scripts = zip(_SCRIPTS[tag], args[1:])
        return _group(args[0].rstrip()) + "".join(f"{mark}{{{script}}}" for mark, script in scripts)
    if tag == "mfrac" and len(args) > 1:
        return f"\\frac{{{args[0]}}}{{{args[1]}}}"
    if tag == "msqrt":
        return f"\\sqrt{{{''.join(args)}}}"
    if tag == "mroot" and len(args) > 1:
        return f"\\sqrt[{args[1]}]{{{args[0]}}}"
    if tag == "maction":
        # What it shows until it is clicked.
        return args[0] if args else ""
    if tag == "mfenced":
        separators = "".join(node.attrs.get("separators", ",").split())
        inner = args[:1]
        for index, arg in enumerate(args[1:]):
            if separators:
                inner.append(separators[min(index, len(separators) - 1)].translate(_TEX_TEXT))
            inner.append(arg)
        return _fenced(node.attrs.get("open", "("), "".join(inner), node.attrs.get("close", ")"))
    if (
        tag == "mrow"
        and len(args) == len(parts) > 2
        and parts[0].tag == parts[-1].tag == "mo"
        and parts[0].attrs.get("stretchy") != "false"
    ):
        # Brackets around a part, which grow with it where it is tall.
        opening, closing = parts[0].text().strip(), parts[-1].text().strip()
        inner = "".join(args[1:-1])
        if opening in _OPENING and closing in _CLOSING and _TALL.search(inner):
            return _fenced(opening, inner, closing)
    return "".join(args)


def _group(tex: str) -> str:
    """`tex` as one piece for a script to stand on: `c^{2}`, but `{ab}^{2}`."""
    return tex if len(tex) <= 1 or re.fullmatch(r"\\(?:[A-Za-z]+ ?|.)", tex) else f"{{{tex}}}"


def _fenced(opening: str, inner: str, closing: str) -> str:
    """`inner` in brackets, which grow with it where it is tall — a fraction,
    a sum, a matrix — as a formula editor draws them: `\\left(…\\right)`."""
    if _TALL.search(inner):
        return _joined(
            [f"\\left{_fence(opening) or '.'}", inner, f"\\right{_fence(closing) or '.'}"]
        )
    return _joined([_fence(opening), inner, _fence(closing)])


def _fence(char: str) -> str:
    """A bracket as TeX; none, where a side has none, is nothing."""
    return _FENCES.get(char, char.translate(_TEX_TEXT))


def _joined(pieces: Iterable[str]) -> str:
    """Pieces of TeX one after the other, a space between a command and a
    letter after it: `\\sin x`, not `\\sinx`."""
    out: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        if out and piece[0].isalpha() and _COMMAND_END.search(out[-1][-32:]):
            out.append(" ")
        out.append(piece)
    return "".join(out)


@dataclass
class _Formulas:
    """Where a page's formulas are, found before it is read.

    Wikipedia shows a formula as a picture, hidden from screen readers, and
    keeps its MathML beside it for them, hidden from the eye. Read as it is,
    the formula would be lost twice over; its MathML is read instead, once,
    and the picture is not.
    """

    #: The hidden elements that hold such a formula's MathML, by id: its `<math>`.
    copies: dict[int, Element] = field(default_factory=dict)
    #: The pictures shown in their place, by id.
    pictures: set[int] = field(default_factory=set)
    #: The elements that show a formula set apart from the text, by id: the
    #: paragraph around one is split there.
    displayed: set[int] = field(default_factory=set)


#: What an element holds, for :func:`_find_formulas`: hidden MathML (the
#: hidden element and its `<math>`), pictures, whether it shows anything else —
#: text, or a formula of its own — and whether it shows a formula set apart.
_Held = tuple[list[tuple[Element, Element]], list[Element], bool, bool]


def _find_formulas(root: Element) -> _Formulas:
    """The formulas of a page (see :class:`_Formulas`). A hidden `<math>` is
    read when an element holds it and pictures and nothing else — a formula,
    and the picture of it. MathML hidden where no picture stands for it is
    hidden content like any other."""
    found = _Formulas()

    def visit(node: Element) -> _Held:
        if node.tag in _SKIP:
            return [], [], False, False
        if _invisible(node):
            maths = [node] if node.tag == "math" else node.find_all("math")
            copies = [(node, maths[0])] if len(maths) == 1 else []
            return copies, [node] if node.tag == "img" else [], False, False
        if node.tag == "math":
            if _display(node):
                found.displayed.add(id(node))
            return [], [], True, _display(node)
        if node.tag == "img":
            return [], [node], False, False
        copies: list[tuple[Element, Element]] = []
        pictures: list[Element] = []
        shown = display = False
        for child in node.children:
            if isinstance(child, str):
                shown = shown or bool(child.strip())
                continue
            held = visit(child)
            copies += held[0]
            pictures += held[1]
            shown = shown or held[2]
            display = display or held[3]
        if len(copies) == 1 and pictures and not shown:
            hidden, math = copies[0]
            found.copies[id(hidden)] = math
            found.pictures.update(id(picture) for picture in pictures)
            display = _display(math)
            if display:
                found.displayed.add(id(hidden))
            shown = True
        if display:
            found.displayed.add(id(node))
        # What shows anything else is never one formula, and nor is what holds it.
        return ([], [], True, display) if shown else (copies, pictures, False, display)

    visit(root)
    return found


def _table_rows(table: Element) -> list[list[Element]]:
    rows: list[list[Element]] = []
    for child in table.children:
        if not isinstance(child, Element):
            continue
        if child.tag == "tr":
            rows.append(
                [c for c in child.children if isinstance(c, Element) and c.tag in ("td", "th")]
            )
        elif child.tag in ("thead", "tbody", "tfoot"):
            rows.extend(_table_rows(child))
    return [row for row in rows if row]


def _content_root(root: Element) -> Element:
    """The part of a page that is the document: its one `<main>`, or its one
    `<article>` when that holds most of the page's text, else its body. A
    teaser in a sidebar is an article too, and not the page."""
    bodies = root.find_all("body")
    body = bodies[0] if bodies else root
    mains = root.find_all("main")
    if len(mains) == 1:
        return mains[0]
    articles = root.find_all("article")
    if len(articles) == 1:
        page = len(_WHITESPACE.sub("", body.text()))
        if len(_WHITESPACE.sub("", articles[0].text())) >= 0.5 * page:
            return articles[0]
    return body


def metadata(root: Element) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    titles = root.find_all("title")
    if titles:
        meta["title"] = _WHITESPACE.sub(" ", titles[0].text()).strip() or None
    for tag in root.find_all("meta"):
        name = (tag.attrs.get("name") or tag.attrs.get("property") or "").lower()
        content = tag.attrs.get("content", "").strip()
        if not content:
            continue
        if name in ("author", "dc.creator"):
            meta.setdefault("author", content)
        elif name in ("description", "og:description"):
            meta.setdefault("description", content)
        elif name == "keywords":
            meta.setdefault("keywords", [k.strip() for k in content.split(",") if k.strip()])
        elif name in ("article:published_time", "dc.date", "date"):
            from ._package import parse_date

            meta.setdefault("created", parse_date(content))
        elif name == "og:title" and "title" not in meta:
            meta["title"] = content
    for html in root.find_all("html"):
        lang = html.attrs.get("lang") or html.attrs.get("xml:lang")
        if lang:
            meta["language"] = lang.split("-")[0].lower()
    return meta


def html_blocks(
    text: str, ctx: Context, resolve: Resolver | None = None, base: str = ""
) -> tuple[list[Block], dict[str, Any]]:
    root = parse(text)
    converter = _Converter(ctx, resolve, base, _find_formulas(root))
    return converter.blocks(_content_root(root)), metadata(root)


def web_codec(name: str) -> str:
    """The codec a browser uses for a declared charset: Latin-1 and ASCII
    labels mean Windows-1252 on the web, and a page that says ISO-8859-1 but
    has typographic quotes in it is the rule, not the exception."""
    label = name.strip().lower().replace("_", "-")
    if label in (
        "iso-8859-1",
        "iso8859-1",
        "latin1",
        "latin-1",
        "l1",
        "us-ascii",
        "ascii",
        "cp819",
        "ibm819",
        "iso-ir-100",
        "windows-1252",
        "x-cp1252",
    ):
        return "cp1252"
    return label


def decode_html(data: bytes) -> str:
    """HTML bytes as text, in the encoding the page declares."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    head = data[:4096].decode("ascii", "replace")
    match = re.search(r"""charset\s*=\s*["']?([\w-]+)""", head, re.I) or re.search(
        r"""encoding\s*=\s*["']([\w-]+)""", head, re.I
    )
    if match:
        try:
            return data.decode(web_codec(match.group(1)), "replace")
        except LookupError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def html(data: bytes, ctx: Context) -> Note:
    folder = ctx.path.resolve().parent if ctx.path is not None and ctx.depth <= 1 else None

    def resolve(src: str) -> tuple[bytes, str] | None:
        # Pictures in the page's own folder or below it, and nowhere else: a
        # page is not allowed to pull in any other file of the machine.
        if folder is None or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", src) or src.startswith("/"):
            return None
        try:
            path = (folder / unquote(src.split("#", 1)[0].split("?", 1)[0])).resolve()
            if folder not in path.parents or not path.is_file():
                return None
            if path.stat().st_size > 50 * 2**20:
                return None
            return path.read_bytes(), path.name
        except OSError:
            return None

    blocks, meta = html_blocks(decode_html(data), ctx, resolve)
    return Note(blocks=blocks, meta=meta)
