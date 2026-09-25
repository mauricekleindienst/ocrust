"""Formulas in Word, PowerPoint and LibreOffice documents as Markdown math.

Word and PowerPoint keep an equation as markup of their own, LibreOffice keeps
each formula as a small MathML document inside the file. Both are written as
TeX between dollar signs, the way formulas from web pages are: `$…$` in the
sentence, `$$…$$` for an equation on a line of its own. Before, Word's
equations were flattened into their characters — a fraction 1/36 read as
"136" — and LibreOffice's formulas were left out.
"""

from __future__ import annotations

import pytest

from test_markdown import ODF, W, body, convert, docx, package, pptx

M = 'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'
A14 = 'xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main"'
MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
DRAW = (
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"'
)


# --------------------------------------------------------------------------
# Word


def r(text: str, props: str = "") -> str:
    """A run of an equation's text."""
    return f"<m:r>{props}<m:t>{text}</m:t></m:r>"


def e(inner: str) -> str:
    return f"<m:e>{inner}</m:e>"


def sup(base: str, script: str) -> str:
    return f"<m:sSup>{e(r(base))}<m:sup>{r(script)}</m:sup></m:sSup>"


def frac(numerator: str, denominator: str, kind: str = "") -> str:
    props = f'<m:fPr><m:type m:val="{kind}"/></m:fPr>' if kind else ""
    return f"<m:f>{props}<m:num>{numerator}</m:num><m:den>{denominator}</m:den></m:f>"


def equation(inner: str) -> str:
    """An equation in the line of its paragraph."""
    return f"<m:oMath {M}>{inner}</m:oMath>"


def displayed(*lines: str) -> str:
    """An equation on a line of its own."""
    inner = "".join(f"<m:oMath>{line}</m:oMath>" for line in lines)
    return f"<m:oMathPara {M}>{inner}</m:oMathPara>"


def text(value: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{value}</w:t></w:r>'


def word(document: str, numbering: str = "") -> str:
    return body(convert(docx(document, numbering=numbering), "formeln.docx").markdown)


def test_word_equations_keep_their_structure_in_the_sentence():
    document = (
        "<w:p>"
        + text("Für ein rechtwinkliges Dreieck gilt ")
        + equation(sup("a", "2") + r("+") + sup("b", "2") + r("=") + sup("c", "2"))
        + text(", wobei c die Hypotenuse ist.")
        + "</w:p><w:p>"
        + text("Die Wahrscheinlichkeit ist ")
        + equation(r("P=") + frac(r("1"), r("36")))
        + text(" für einen Pasch.")
        + "</w:p>"
    )
    assert word(document).split("\n\n") == [
        "Für ein rechtwinkliges Dreieck gilt $a^{2}+b^{2}=c^{2}$, wobei c die Hypotenuse ist.",
        # One over thirty-six, not a hundred and thirty-six.
        "Die Wahrscheinlichkeit ist $P=\\frac{1}{36}$ für einen Pasch.",
    ]


def test_a_word_equation_on_a_line_of_its_own_is_set_apart():
    root = (
        '<m:rad><m:radPr><m:degHide m:val="1"/></m:radPr><m:deg/>'
        + e(sup("b", "2") + r("−4ac"))
        + "</m:rad>"
    )
    document = (
        "<w:p>" + text("Die Lösung lautet") + "</w:p>"
        "<w:p>" + displayed(r("x=") + frac(r("−b±") + root, r("2a"))) + "</w:p>"
        # Alone in its paragraph, an equation in the line is on a line of its own too.
        "<w:p>" + equation(r("E=m") + sup("c", "2")) + "</w:p>"
    )
    assert word(document).split("\n\n") == [
        "Die Lösung lautet",
        "$$\nx=\\frac{−b±\\sqrt{b^{2}−4ac}}{2a}\n$$",
        "$$\nE=mc^{2}\n$$",
    ]


def test_lines_of_one_word_equation_and_equations_that_touch_stay_apart():
    lines = displayed(r("a=1"), r("b=2"))
    touching = "<w:p>" + text("A ") + equation(r("a")) + equation(r("b")) + text(" B") + "</w:p>"
    assert word(f"<w:p>{lines}</w:p>" + touching).split("\n\n") == [
        "$$\n\\begin{gathered} a=1 \\\\ b=2 \\end{gathered}\n$$",
        "A $a$ $b$ B",
    ]


UPRIGHT = '<m:rPr><m:sty m:val="p"/></m:rPr>'


@pytest.mark.parametrize(
    ("omml", "tex"),
    [
        pytest.param(
            '<m:nary><m:naryPr><m:chr m:val="∑"/></m:naryPr><m:sub>'
            + r("i=1")
            + "</m:sub><m:sup>"
            + r("n")
            + "</m:sup>"
            + e(r("i"))
            + "</m:nary>",
            "\\sum_{i=1}^{n} i",
            id="sum-with-limits",
        ),
        pytest.param(
            "<m:nary><m:sub>"
            + r("0")
            + "</m:sub><m:sup>"
            + r("1")
            + "</m:sup>"
            + e(r("f"))
            + "</m:nary>",
            "\\int_{0}^{1} f",
            id="integral-where-it-does-not-say",
        ),
        pytest.param(
            '<m:nary><m:naryPr><m:chr m:val="∮"/><m:subHide m:val="1"/><m:supHide m:val="1"/>'
            "</m:naryPr><m:sub/><m:sup/>" + e(r("F")) + "</m:nary>",
            "\\oint F",
            id="integral-without-limits",
        ),
        pytest.param("<m:d>" + e(r("x+1")) + "</m:d>", "(x+1)", id="brackets"),
        pytest.param(
            "<m:d>" + e(frac(r("a"), r("b"))) + "</m:d>",
            "\\left(\\frac{a}{b}\\right)",
            id="brackets-around-a-fraction-grow",
        ),
        pytest.param(
            '<m:d><m:dPr><m:begChr m:val="{"/><m:sepChr m:val=","/><m:endChr m:val="}"/></m:dPr>'
            + e(r("1"))
            + e(r("2"))
            + "</m:d>",
            "\\{1,2\\}",
            id="a-set",
        ),
        pytest.param(
            '<m:d><m:dPr><m:begChr m:val="⟨"/><m:endChr m:val="⟩"/></m:dPr>' + e(r("v")) + "</m:d>",
            "\\langle v\\rangle",
            id="angle-brackets",
        ),
        pytest.param(
            "<m:d>"
            + e(
                "<m:m><m:mr>"
                + e(r("1"))
                + e(r("0"))
                + "</m:mr><m:mr>"
                + e(r("0"))
                + e(r("1"))
                + "</m:mr></m:m>"
            )
            + "</m:d>",
            "\\left(\\begin{matrix} 1 & 0 \\\\ 0 & 1 \\end{matrix}\\right)",
            id="matrix",
        ),
        pytest.param(
            "<m:func><m:fName>" + r("sin", UPRIGHT) + "</m:fName>" + e(r("x")) + "</m:func>",
            "\\sin x",
            id="function",
        ),
        pytest.param(
            "<m:func><m:fName>"
            + r("Var")
            + "</m:fName>"
            + e("<m:d>" + e(r("X")) + "</m:d>")
            + "</m:func>",
            "\\operatorname{Var}(X)",
            id="function-tex-has-no-command-for",
        ),
        pytest.param(
            "<m:func><m:fName><m:limLow>"
            + e(r("lim", UPRIGHT))
            + "<m:lim>"
            + r("n→∞")
            + "</m:lim></m:limLow></m:fName>"
            + e(r("a"))
            + "</m:func>",
            "\\lim_{n→∞}a",
            id="limit",
        ),
        pytest.param("<m:acc>" + e(r("x")) + "</m:acc>", "\\hat{x}", id="accent"),
        pytest.param(
            '<m:acc><m:accPr><m:chr m:val="\u20d7"/></m:accPr>' + e(r("v")) + "</m:acc>",
            "\\vec{v}",
            id="vector",
        ),
        pytest.param(
            '<m:bar><m:barPr><m:pos m:val="top"/></m:barPr>' + e(r("z")) + "</m:bar>",
            "\\overline{z}",
            id="bar",
        ),
        pytest.param(
            "<m:rad><m:deg>" + r("3") + "</m:deg>" + e(r("x")) + "</m:rad>",
            "\\sqrt[3]{x}",
            id="cube-root",
        ),
        pytest.param(
            "<m:sSubSup>"
            + e(r("x"))
            + "<m:sub>"
            + r("i")
            + "</m:sub><m:sup>"
            + r("2")
            + "</m:sup></m:sSubSup>",
            "x_{i}^{2}",
            id="sub-and-superscript",
        ),
        pytest.param(r("α+β≤γ·π"), "α+β≤γ·π", id="greek-and-operators-as-they-are"),
        pytest.param(
            r(" für alle ", "<m:rPr><m:nor/></m:rPr>") + r("x"), "\\text{ für alle }x", id="text"
        ),
        pytest.param(r("{a}_#%$&amp;"), "\\{a\\}\\_\\#\\%\\$\\&", id="tex-markup-as-characters"),
        pytest.param(
            r("R", '<m:rPr><m:scr m:val="double-struck"/></m:rPr>'),
            "\\mathbb{R}",
            id="double-struck",
        ),
        pytest.param(
            "<m:d>" + e(frac(r("n"), r("k"), "noBar")) + "</m:d>",
            "\\left({n \\atop k}\\right)",
            id="binomial",
        ),
        pytest.param(frac(r("a+b"), r("c"), "lin"), "{a+b}/c", id="fraction-on-the-line"),
        pytest.param(
            "<m:eqArr>" + e(r("x&amp;=1")) + e(r("y&amp;=2")) + "</m:eqArr>",
            "\\begin{aligned} x&=1 \\\\ y&=2 \\end{aligned}",
            id="aligned-lines",
        ),
        pytest.param(
            "<m:groupChr>" + e(r("a+b")) + "</m:groupChr>", "\\underbrace{a+b}", id="brace"
        ),
        pytest.param(
            r("a") + f"<w:del {W}>" + r("b") + "</w:del>" + r("c"), "ac", id="tracked-deletion"
        ),
        pytest.param("<m:f><m:num>" + r("1") + "</m:num></m:f>", "\\frac{1}{}", id="missing-part"),
    ],
)
def test_word_equation_pieces_as_tex(omml, tex):
    assert word("<w:p>" + text("Es gilt ") + equation(omml) + text(".") + "</w:p>") == (
        f"Es gilt ${tex}$."
    )


def test_a_dollar_sign_beside_a_word_equation_is_escaped_and_elsewhere_left_alone():
    document = (
        "<w:p>" + text("Preis 5 $ für ") + equation(r("x")) + "</w:p>"
        "<w:p>" + text("Preis 5 $ und $10") + "</w:p>"
    )
    assert word(document).split("\n\n") == ["Preis 5 \\$ für $x$", "Preis 5 $ und $10"]


def test_word_equations_in_list_items_and_table_cells_stay_on_their_line():
    numbering = (
        '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>'
    )
    item = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
    half = frac(r("1"), r("2"))

    def cell(inner: str) -> str:
        return f"<w:tc><w:p>{inner}</w:p></w:tc>"

    document = (
        f"<w:p>{item}{text('eins')}</w:p>"
        f"<w:p>{item}{displayed(half)}</w:p>"
        f"<w:p>{item}{text('drei ')}{equation(r('x'))}</w:p>"
        "<w:tbl><w:tr>" + cell(text("Formel")) + cell(text("Betrag")) + "</w:tr>"
        "<w:tr>"
        + cell(displayed(half))
        + cell(
            equation(
                "<m:d><m:dPr><m:begChr m:val='|'/><m:endChr m:val='|'/></m:dPr>"
                + e(r("x"))
                + "</m:d>"
            )
        )
        + "</w:tr></w:tbl>"
    )
    assert word(document, numbering=numbering).split("\n\n") == [
        "1. eins\n2. $$\\frac{1}{2}$$\n3. drei $x$",
        # A table cell is one line: its formulas are in it, and a bar in one
        # does not end the cell.
        "| Formel | Betrag |\n| --- | --- |\n| $\\frac{1}{2}$ | $\\|x\\|$ |",
    ]


def test_a_word_equation_nested_past_all_sense_still_reads():
    deep = "<m:d><m:e>" * 3000 + r("x") + "</m:e></m:d>" * 3000
    text_ = word("<w:p>" + text("Tief ") + equation(deep) + text(" genug.") + "</w:p>")
    assert text_.startswith("Tief $((((") and text_.endswith("))))$ genug.") and "(x)" in text_


def test_a_powerpoint_equation_is_read_like_words():
    shape = (
        f'<mc:AlternateContent {MC}><mc:Choice {A14} Requires="a14"><p:sp><p:nvSpPr>'
        '<p:cNvPr id="2" name="Formel"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="0" y="100"/></a:xfrm></p:spPr><p:txBody><a:bodyPr/>'
        # PowerPoint keeps letters in Unicode's mathematical italic.
        f"<a:p><a14:m>{displayed(r('𝑎=') + sup('𝑏', '2'))}</a14:m></a:p>"
        f"<a:p><a:r><a:t>Mit </a:t></a:r><a14:m>{equation(r('𝑥'))}</a14:m>"
        "<a:r><a:t> im Satz.</a:t></a:r></a:p>"
        "</p:txBody></p:sp></mc:Choice><mc:Fallback><p:sp><p:nvSpPr>"
        '<p:cNvPr id="2" name="Formel"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody>'
        "<a:bodyPr/><a:p><a:r><a:t>Bild der Formel</a:t></a:r></a:p></p:txBody></p:sp>"
        "</mc:Fallback></mc:AlternateContent>"
    )
    assert body(convert(pptx([shape]), "folie.pptx").markdown).split("\n\n") == [
        "<!-- slide 1 -->",
        "## Slide 1",
        "$$\na=b^{2}\n$$",
        "Mit $x$ im Satz.",
    ]


def test_a_formula_in_a_slide_title_is_math_in_its_heading():
    title = (
        f'<mc:AlternateContent {MC}><mc:Choice {A14} Requires="a14"><p:sp><p:nvSpPr>'
        '<p:cNvPr id="2" name="Titel"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr>'
        "</p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:p><a:r><a:t>Die Formel </a:t></a:r>"
        f"<a14:m>{equation(r('E=m') + sup('c', '2'))}</a14:m>"
        "<a:r><a:t> kostet 5 $</a:t></a:r></a:p></p:txBody></p:sp></mc:Choice>"
        "<mc:Fallback/></mc:AlternateContent>"
    )
    md = body(convert(pptx([title]), "folie.pptx").markdown)
    assert "## Die Formel $E=mc^{2}$ kostet 5 \\$" in md, md


# --------------------------------------------------------------------------
# LibreOffice


def formula(mathml: str, annotation: str = "x") -> str:
    """A formula object's own document, as LibreOffice writes it: MathML,
    with the formula in its own notation beside."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><semantics>'
        f'<mrow>{mathml}</mrow><annotation encoding="StarMath 5.0">{annotation}</annotation>'
        "</semantics></math>"
    )


def frame(number: int, href: str = "") -> str:
    """Where a formula stands in the text: a frame pointing to its object,
    with a picture of it beside."""
    return (
        f'<draw:frame draw:name="Formel{number}" text:anchor-type="as-char">'
        f'<draw:object xlink:href="{href or f"./Object {number}"}" xlink:type="simple"/>'
        f'<draw:image xlink:href="ObjectReplacements/Object {number}.png"/></draw:frame>'
    )


def odf(kind: str, content: str, objects: dict[int, str]) -> bytes:
    parts: dict[str, str | bytes] = {
        "mimetype": f"application/vnd.oasis.opendocument.{kind}",
        "content.xml": (
            f"<office:document-content {ODF} {DRAW}><office:body><office:{kind}>{content}"
            f"</office:{kind}></office:body></office:document-content>"
        ),
    }
    for number, document in objects.items():
        parts[f"Object {number}/content.xml"] = document
        parts[f"ObjectReplacements/Object {number}.png"] = b"\x89PNG\r\n\x1a\n" + bytes(200)
    return package(parts)


PYTHAGORAS = formula(
    "<msup><mi>a</mi><mn>2</mn></msup><mo>+</mo><msup><mi>b</mi><mn>2</mn></msup>"
    "<mo>=</mo><msup><mi>c</mi><mn>2</mn></msup>",
    "a^2 + b^2 = c^2",
)
HALF = formula("<mfrac><mn>1</mn><mn>2</mn></mfrac>")
SINE = formula("<mi>sin</mi><mo>\u2061</mo><mi>x</mi>")


def test_a_libreoffice_formula_is_read_in_its_sentence():
    content = (
        f"<text:p>Für ein rechtwinkliges Dreieck gilt {frame(1)}, wobei c die Hypotenuse ist.</text:p>"
        f"<text:p>{frame(2)}</text:p>"
        f"<text:p>Also {frame(3)}{frame(2)} und 5 $ mehr.</text:p>"
    )
    note = convert(odf("text", content, {1: PYTHAGORAS, 2: HALF, 3: SINE}), "f.odt", assets=True)
    assert body(note.markdown).split("\n\n") == [
        "Für ein rechtwinkliges Dreieck gilt $a^{2}+b^{2}=c^{2}$, wobei c die Hypotenuse ist.",
        # Alone in its paragraph, it is set apart.
        "$$\n\\frac{1}{2}\n$$",
        "Also $\\sin x$ $\\frac{1}{2}$ und 5 \\$ mehr.",
    ]
    # The picture kept beside each formula is not needed.
    assert not note.assets


def test_libreoffice_formulas_in_list_items_table_cells_and_on_slides():
    content = (
        "<text:list><text:list-item><text:p>eins</text:p></text:list-item>"
        f"<text:list-item><text:p>{frame(1)}</text:p></text:list-item></text:list>"
        "<table:table><table:table-row><table:table-cell><text:p>A</text:p></table:table-cell>"
        "<table:table-cell><text:p>B</text:p></table:table-cell></table:table-row>"
        f"<table:table-row><table:table-cell><text:p>{frame(1)}</text:p></table:table-cell>"
        f"<table:table-cell><text:p>x {frame(2)}</text:p></table:table-cell></table:table-row>"
        "</table:table>"
    )
    text_ = body(convert(odf("text", content, {1: HALF, 2: SINE}), "f.odt").markdown)
    assert text_.split("\n\n") == [
        "- eins\n- $$\\frac{1}{2}$$",
        "| A | B |\n| --- | --- |\n| $\\frac{1}{2}$ | x $\\sin x$ |",
    ]
    slide = (
        '<draw:page draw:name="Folie1"><draw:frame presentation:class="title"><draw:text-box>'
        f"<text:p>Pythagoras</text:p></draw:text-box></draw:frame>{frame(1)}</draw:page>"
    )
    assert body(convert(odf("presentation", slide, {1: PYTHAGORAS}), "f.odp").markdown).split(
        "\n\n"
    ) == ["<!-- slide 1 -->", "## Pythagoras", "$$\na^{2}+b^{2}=c^{2}\n$$"]


def test_libreoffice_formulas_read_as_the_same_word_equations_do():
    # As LibreOffice 24.2 writes `sin^2 %alpha + cos^2 %alpha = 1` and
    # `left( matrix{…} right) cdot overline{z} = abs{x} + vec v + nroot{3}{8}`.
    sines = formula(
        "<msup><mi>sin</mi><mn>2</mn></msup><mrow><mi>α</mi><mo stretchy='false'>+</mo>"
        "<msup><mi>cos</mi><mn>2</mn></msup></mrow><mrow><mi>α</mi><mo stretchy='false'>=</mo>"
        "<mn>1</mn></mrow>"
    )
    marks = formula(
        "<mrow><mrow><mo fence='true' form='prefix' stretchy='true'>(</mo><mrow><mtable><mtr>"
        "<mtd><mn>1</mn></mtd><mtd><mn>0</mn></mtd></mtr><mtr><mtd><mn>0</mn></mtd><mtd><mn>1</mn>"
        "</mtd></mtr></mtable></mrow><mo fence='true' form='postfix' stretchy='true'>)</mo></mrow>"
        "<mo stretchy='false'>⋅</mo><mover accent='true'><mi>z</mi><mo>¯</mo></mover></mrow>"
        "<mo stretchy='false'>=</mo><mrow><mrow><mo fence='true' form='prefix' stretchy='true'>|</mo>"
        "<mrow><mi>x</mi></mrow><mo fence='true' form='postfix' stretchy='true'>|</mo></mrow>"
        "<mo stretchy='false'>+</mo><mover accent='true'><mi>v</mi><mo stretchy='false'>⃗</mo>"
        "</mover><mo stretchy='false'>+</mo><mroot><mn>8</mn><mn>3</mn></mroot></mrow>"
    )
    content = f"<text:p>A {frame(1)}, B {frame(2)}.</text:p>"
    assert body(convert(odf("text", content, {1: sines, 2: marks}), "f.odt").markdown) == (
        "A $\\sin^{2}α+\\cos^{2}α=1$, B $\\left(\\begin{matrix} 1 & 0 \\\\ 0 & 1 \\end{matrix}"
        "\\right)⋅\\overline{z}=|x|+\\vec{v}+\\sqrt[3]{8}$."
    )


def test_an_openoffice_formula_with_a_document_type_and_prefixes_is_read():
    old = (
        '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE math:math PUBLIC "-//OpenOffice.org//DTD '
        'Modified W3C MathML 1.01//EN" "math.dtd"><math:math xmlns:math="http://www.w3.org/1998/Math/MathML">'
        "<math:mi>y</math:mi><math:mo>=</math:mo><math:msqrt><math:mi>x</math:mi></math:msqrt></math:math>"
    )
    content = f"<text:p>Es gilt {frame(1, 'Object 1')}.</text:p>"
    assert body(convert(odf("text", content, {1: old}), "f.odt").markdown) == (
        "Es gilt $y=\\sqrt{x}$."
    )


def test_objects_that_are_no_formula_or_cannot_be_read_leave_the_text_as_it_is():
    chart = (
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0">'
        '<office:body><chart:chart xmlns:chart="urn:oasis:names:tc:opendocument:xmlns:chart:1.0"/>'
        "</office:body></office:document-content>"
    )
    content = (
        f"<text:p>Diagramm {frame(1)} und fehlt {frame(5)} und "
        f"{frame(6, 'https://example.org/x')} ende.</text:p>"
        f"<text:p>Tief {frame(2)}.</text:p>"
    )
    deep = formula("<mrow>" * 3000 + "<mi>z</mi>" + "</mrow>" * 3000)
    text_ = body(convert(odf("text", content, {1: chart, 2: deep}), "f.odt").markdown)
    assert text_.split("\n\n") == ["Diagramm und fehlt und ende.", "Tief $z$."]
