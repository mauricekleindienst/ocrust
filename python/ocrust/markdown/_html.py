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
from collections.abc import Callable
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
_INLINE = {
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


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#root")
        self.stack: list[Element] = [self.root]

    def _close(self, tags: set[str], boundary: set[str]) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[index].tag
            if tag in tags:
                del self.stack[index:]
                return
            if tag in boundary:
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _CLOSES_P:
            self._close(
                {"p"}, {"div", "td", "th", "li", "blockquote", "section", "article", "body"}
            )
        if tag in _IMPLIED_END:
            closes, boundary = _IMPLIED_END[tag]
            self._close(closes, boundary)
        node = Element(tag, {k.lower(): v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        node = Element(tag, {k.lower(): v or "" for k, v in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
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
    def __init__(self, ctx: Context, resolve: Resolver | None, base: str) -> None:
        self.ctx = ctx
        self.resolve = resolve
        self.base = base

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
            if isinstance(child, str) or child.tag in _INLINE:
                pending.append(child)
                continue
            flush()
            out.extend(self.block(child))
        flush()
        return out

    def block(self, node: Element) -> list[Block]:
        tag = node.tag
        if tag in _SKIP or node.attrs.get("hidden") is not None or _invisible(node):
            return []
        if tag in _HEADINGS:
            extras: list[Block] = []
            content = self.inlines(node.children, extras)
            return ([Heading(_HEADINGS[tag], content)] if not is_empty(content) else []) + extras
        if tag == "p":
            if node.has_any(_STRUCTURE | {"div", "p"}):
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
        if tag in _SKIP or _invisible(node):
            return
        if tag == "br":
            runs.append((Break(), frozenset(), link))
            return
        if tag == "img":
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
    converter = _Converter(ctx, resolve, base)
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
