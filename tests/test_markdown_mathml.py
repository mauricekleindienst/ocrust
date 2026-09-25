"""Formulas in web pages and e-books: MathML as Markdown math.

A formula is read as TeX between dollar signs, `$…$` in its sentence and
`$$…$$` as a block of its own, which is what Obsidian and the math extensions
of other Markdown tools read. Every case here was lost or torn apart before:
Wikipedia's formulas were dropped whole, and MathML took each of its pieces
for a paragraph.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from ocrust import markdown


def body(html: str, name: str = "p.html") -> str:
    return markdown.convert(html.encode(), name=name).body.strip("\n")


def wikipedia(tex: str, mathml: str, *, display: bool = False, shown: bool = False) -> str:
    """A formula the way Wikipedia sets it: the MathML hidden for the eye,
    the picture shown in its place hidden from screen readers."""
    kind = "display" if display else "inline"
    tag = "div" if display else "span"
    hidden = "" if shown else ' aria-hidden="true"'
    return (
        f'<{tag} class="mwe-math-element"><{tag} class="mwe-math-mathml-{kind} '
        f'mwe-math-mathml-a11y" style="display: none;"><math xmlns="http://www.w3.org/1998/Math/MathML"'
        f'{" display=block" if display else ""} alttext="{{\\displaystyle {tex}}}">'
        f"<semantics><mrow>{mathml}</mrow>"
        f'<annotation encoding="application/x-tex">{{\\displaystyle {tex}}}</annotation>'
        f"</semantics></math></{tag}>"
        f'<img src="https://wikimedia.org/api/rest_v1/media/math/render/svg/abc" '
        f'class="mwe-math-fallback-image-{kind}"{hidden} alt="{{\\displaystyle {tex}}}"></{tag}>'
    )


EMC2 = "<mi>E</mi><mo>=</mo><mi>m</mi><msup><mi>c</mi><mn>2</mn></msup>"


def test_a_wikipedia_formula_is_read_once_from_its_tex():
    page = f"<p>Die Energie ist {wikipedia('E=mc^{2}', EMC2)} nach Einstein.</p>"
    assert body(page) == "Die Energie ist $E=mc^{2}$ nach Einstein."
    # Older pages show the picture to screen readers too: still one formula,
    # and no picture of it beside.
    shown = f"<p>Die Energie ist {wikipedia('E=mc^{2}', EMC2, shown=True)} nach Einstein.</p>"
    assert body(shown) == "Die Energie ist $E=mc^{2}$ nach Einstein."
    # The TeX it was written in, not the MathML read back.
    written = wikipedia("E=m\\cdot c^{2}", EMC2)
    assert body(f"<p>Also {written}.</p>") == "Also $E=m\\cdot c^{2}$."


def test_a_display_formula_is_a_block_of_its_own():
    fraction = "<mfrac><mi>a</mi><mi>b</mi></mfrac><mo>=</mo><msqrt><mi>x</mi></msqrt>"
    page = f'<p>Es gilt <math display="block">{fraction}</math> für alle x.</p>'
    assert body(page).split("\n\n") == ["Es gilt", "$$\n\\frac{a}{b}=\\sqrt{x}\n$$", "für alle x."]
    tex = "\\sum _{i=1}^{n}i={\\frac {n(n+1)}{2}}"
    wiki = f"<p>Die Summe:</p><dl><dd>{wikipedia(tex, '<mi>s</mi>', display=True)}</dd></dl><p>Danach.</p>"
    assert body(wiki).split("\n\n") == ["Die Summe:", f"$$\n{tex}\n$$", "Danach."]


def test_mathml_without_tex_is_read_from_its_elements():
    formula = (
        "<math><msub><mi>x</mi><mi>i</mi></msub><mo>=</mo>"
        "<mfrac><mn>1</mn><msqrt><mi>n</mi></msqrt></mfrac>"
        "<mo>+</mo><msup><mrow><mo>(</mo><mi>a</mi><mo>+</mo><mi>b</mi><mo>)</mo></mrow><mn>2</mn></msup>"
        "</math>"
    )
    assert body(f"<p>Mittel {formula} hier.</p>") == (
        "Mittel $x_{i}=\\frac{1}{\\sqrt{n}}+{(a+b)}^{2}$ hier."
    )
    # A function's name is TeX's own.
    sine = "<math><mi>sin</mi><mo>\u2061</mo><mi>x</mi><mo>+</mo><mi>log</mi><mi>y</mi></math>"
    assert body(f"<p>Also {sine}.</p>") == "Also $\\sin x+\\log y$."


@pytest.mark.parametrize(
    ("mathml", "tex"),
    [
        pytest.param("<mover accent='true'><mi>x</mi><mo>^</mo></mover>", "\\hat{x}", id="hat"),
        pytest.param("<mover><mi>v</mi><mo>⃗</mo></mover>", "\\vec{v}", id="vector"),
        pytest.param("<mover><mi>z</mi><mo>¯</mo></mover>", "\\overline{z}", id="overline"),
        pytest.param("<munder><mi>a</mi><mo>⏟</mo></munder>", "\\underbrace{a}", id="brace"),
        pytest.param("<mover><mi>x</mi><mi>n</mi></mover>", "x^{n}", id="over-is-no-accent"),
        pytest.param(
            "<munderover><mo>∑</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mi>n</mi>"
            "</munderover><mi>i</mi>",
            "\\sum_{i=1}^{n}i",
            id="sum",
        ),
        pytest.param(
            "<mrow><mo>(</mo><mfrac><mi>a</mi><mi>b</mi></mfrac><mo>)</mo></mrow>",
            "\\left(\\frac{a}{b}\\right)",
            id="brackets-grow-around-a-fraction",
        ),
        pytest.param(
            "<mrow><mo stretchy='false'>(</mo><mfrac><mi>a</mi><mi>b</mi></mfrac>"
            "<mo stretchy='false'>)</mo></mrow>",
            "(\\frac{a}{b})",
            id="brackets-that-say-they-do-not-grow",
        ),
        pytest.param(
            "<mfenced open='⟨' close='⟩'><mi>u</mi><mi>v</mi></mfenced>",
            "\\langle u,v\\rangle",
            id="fenced",
        ),
    ],
)
def test_mathml_accents_large_operators_and_brackets_as_tex(mathml, tex):
    assert body(f"<p>F <math>{mathml}</math>.</p>") == f"F ${tex}$."


def test_alttext_in_words_is_what_a_screen_reader_says_not_the_formula():
    pythagoras = (
        '<math alttext="a squared plus b squared equals c squared"><msup><mi>a</mi><mn>2</mn></msup>'
        "<mo>+</mo><msup><mi>b</mi><mn>2</mn></msup><mo>=</mo><msup><mi>c</mi><mn>2</mn></msup></math>"
    )
    assert body(f"<p>Es gilt {pythagoras}.</p>") == "Es gilt $a^{2}+b^{2}=c^{2}$."
    root = (
        '<math display="block" alttext="x equals the square root of y"><mi>x</mi><mo>=</mo>'
        "<msqrt><mi>y</mi></msqrt></math>"
    )
    assert body(f"<p>Lösung</p>{root}") == "Lösung\n\n$$\nx=\\sqrt{y}\n$$"
    # The MathML is read before an `alttext` in TeX, too; that is read only
    # where the elements give nothing, and words there never are.
    assert body('<p>A <math alttext="{\\displaystyle a+b}"><mi>q</mi></math>.</p>') == "A $q$."
    assert body('<p>A <math alttext="{\\displaystyle a+b}"></math>.</p>') == "A $a+b$."
    assert body('<p>A <math alttext="x_i squared plus one"><mspace/></math>.</p>') == "A ."
    # A TeX annotation is the TeX it was written in, whatever `alttext` says.
    annotated = (
        '<math alttext="E equals m c squared"><semantics><mi>E</mi>'
        '<annotation encoding="application/x-tex">E=mc^2</annotation></semantics></math>'
    )
    assert body(f"<p>A {annotated}.</p>") == "A $E=mc^2$."


def test_a_formula_set_apart_in_a_list_item_or_a_table_cell_stays_on_its_line():
    half = '<math display="block"><mfrac><mn>1</mn><mn>2</mn></mfrac></math>'
    page = (
        f"<ul><li>eins</li><li>{half}</li><li>drei</li></ul>"
        f"<table><tr><th>Formel</th><th>Wert</th></tr><tr><td>{half}</td><td>0,5 $</td></tr></table>"
    )
    assert body(page).split("\n\n") == [
        "- eins\n- $$\\frac{1}{2}$$\n- drei",
        "| Formel | Wert |\n| --- | --- |\n| $\\frac{1}{2}$ | 0,5 $ |",
    ]


def test_a_sentence_with_inline_math_stays_one_paragraph():
    page = f"<p>MathML direkt: <math>{EMC2}</math> im Satz.</p>"
    assert body(page) == "MathML direkt: $E=mc^{2}$ im Satz."
    # Two formulas that touch stay two.
    assert body("<p>A <math><mi>a</mi></math><math><mi>b</mi></math> B</p>") == "A $a$ $b$ B"
    # KaTeX shows glyphs hidden from screen readers; MathJax wraps its MathML
    # in elements of its own.
    katex = (
        '<p>Formel <span class="katex"><span class="katex-mathml"><math><semantics><mrow><mi>x</mi>'
        '</mrow><annotation encoding="application/x-tex">x^2</annotation></semantics></math></span>'
        '<span class="katex-html" aria-hidden="true"><span class="base">x2</span></span></span> hier.</p>'
    )
    assert body(katex) == "Formel $x^2$ hier."
    mathjax = (
        '<p>Formel <mjx-container class="MathJax" jax="CHTML"><mjx-math aria-hidden="true">'
        "<mjx-mi><mjx-c></mjx-c></mjx-mi></mjx-math><mjx-assistive-mml display=inline><math>"
        "<mi>x</mi><mo>+</mo><mn>1</mn></math></mjx-assistive-mml></mjx-container> hier.</p>"
    )
    assert body(mathjax) == "Formel $x+1$ hier."


def test_a_dollar_amount_is_not_math():
    assert body("<p>Das kostet 5 $ und 10 $.</p>") == "Das kostet 5 $ und 10 $."
    # Beside a formula a dollar sign would open or close one: it is escaped.
    mixed = "<p>Das kostet 5 $ und <math><mi>x</mi></math>, <b>$5</b> netto.</p>"
    assert body(mixed) == "Das kostet 5 \\$ und $x$, **\\$5** netto."


def test_hidden_mathml_without_a_picture_stays_hidden():
    page = '<p>Text <span style="display:none"><math><mi>z</mi></math></span> weiter.</p>'
    assert body(page) == "Text weiter."


def test_an_epub_keeps_its_formulas():
    chapter = (
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:m="http://www.w3.org/1998/Math/MathML"><body><h1>Kapitel</h1>'
        '<p>Die Fläche ist <math xmlns="http://www.w3.org/1998/Math/MathML"><mi>π</mi>'
        "<msup><mi>r</mi><mn>2</mn></msup></math> groß.</p>"
        '<p>Und <m:math display="block"><m:mfrac><m:mn>1</m:mn><m:mn>2</m:mn></m:mfrac></m:math></p>'
        "</body></html>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/book.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/book.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata/>'
            '<manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="a"/></spine></package>',
        )
        archive.writestr("OEBPS/a.xhtml", chapter)
    note = markdown.convert(buffer.getvalue(), name="buch.epub")
    assert note.body.strip("\n").split("\n\n") == [
        "<!-- chapter 1 -->",
        "# Kapitel",
        "Die Fläche ist $πr^{2}$ groß.",
        "Und",
        "$$\n\\frac{1}{2}\n$$",
    ]


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        pytest.param(
            "<msup><mi>x</mi></msup><mfrac><mn>1</mn></mfrac><mroot><mi>y</mi></mroot>",
            "$x1y$",
            id="scripts-short-of-a-piece",
        ),
        pytest.param(
            "<mo>{</mo><mi>a</mi><mo>}</mo><mo>\\</mo><mi>b</mi>",
            "$\\{a\\}\\backslash b$",
            id="tex-markup-as-characters",
        ),
        pytest.param("<mtext>für $5</mtext>", "$\\text{für \\$5}$", id="dollar-in-text"),
        pytest.param("<mrow>" * 3000 + "<mi>x</mi>", "$x$", id="nested-3000-deep"),
        pytest.param("", "", id="empty"),
    ],
)
def test_malformed_mathml_still_reads(formula, expected):
    text = body(f"<p>Vor <math>{formula}</math> nach.</p><p>Weiter.</p>")
    assert text == f"Vor {expected} nach.\n\nWeiter.".replace("  ", " ")


def test_a_formula_ending_in_a_backslash_or_holding_a_dollar_stays_one_formula():
    page = (
        "<p>t <math><semantics><mi>q</mi><annotation encoding='application/x-tex'>"
        "\\text{5 $} x\\</annotation></semantics></math> u</p>"
    )
    assert body(page) == "t $\\text{5 \\$} x$ u"
    # So does one whose `alttext` is read.
    assert body('<p>t <math alttext="\\text{5 $} x\\"></math> u</p>') == "t $\\text{5 \\$} x$ u"
