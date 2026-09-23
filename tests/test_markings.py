"""Classification markings: the matcher on hand-built pages, then `ocrust vs`.

The first half needs no model: a document is assembled line by line, which is
what makes it possible to pin down exactly where a line sits on the page. The
second half renders real PDFs and runs the command, the way a user does.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from ocrust import Block, Box, Document, Line, Page, Segment, Table, markings
from ocrust.cli import main

HEIGHT = 2338  # A4 at 200 dpi
HEADER, FOOTER = 90, 2200
BODY = [
    ("Bundesministerium für Beispiele", 400),
    ("Sehr geehrte Damen und Herren,", 500),
    ("anbei übersenden wir die Unterlagen zur Beschaffung.", 560),
]


def _line(text: str, y: float, x: float = 100) -> Line:
    return Line(
        text=text,
        box=Box(x, y, x + 12 * len(text), y + 30),
        confidence=0.98,
        angle=0.0,
    )


def _page(lines, index: int = 0, table: bool = False) -> Page:
    built = [line if isinstance(line, Line) else _line(*line) for line in lines]
    blocks = tuple(
        Block(
            kind="table" if table else "paragraph",
            box=line.box,
            lines=(line,),
            table=Table(rows=1, columns=1, cells=()) if table else None,
        )
        for line in built
    )
    return Page(
        index=index,
        width=1654,
        height=HEIGHT,
        rotation=0.0,
        origin="image",
        blocks=blocks,
        elapsed_ms=0.0,
    )


def _doc(*pages) -> Document:
    return Document(
        source="test",
        pages=tuple(_page(lines, i) for i, lines in enumerate(pages)),
        elapsed_ms=0.0,
    )


def _report(*pages) -> markings.MarkingReport:
    return _doc(*pages).markings()


def _marked(text: str, y: float = HEADER) -> markings.MarkingReport:
    return _report([(text, y), *BODY])


# ------------------------------------------------------------------ the grades


@pytest.mark.parametrize(
    "text",
    [
        "VS-NfD",
        "VS - NfD",
        "VS-NFD",
        "VS – NUR FÜR DEN DIENSTGEBRAUCH",
        "VS-Nur für den Dienstgebrauch",
        "VS-NUR FUER DEN DIENSTGEBRAUCH",
        "Verschlußsache – Nur für den Dienstgebrauch",
        "Verschlusssache - Nur für den Dienstgebrauch",
        "NUR FÜR DEN DIENSTGEBRAUCH",
    ],
)
def test_every_spelling_of_vs_nfd_is_one_grade(text):
    report = _marked(text)
    assert (report.level, report.label) == (1, "VS-NfD"), report.findings


@pytest.mark.parametrize(
    ("text", "level", "label"),
    [
        ("VS-VERTRAULICH", 2, "VS-VERTRAULICH"),
        ("VS – VERTRAULICH", 2, "VS-VERTRAULICH"),
        ("GEHEIM", 3, "GEHEIM"),
        ("STRENG GEHEIM", 4, "STRENG GEHEIM"),
        ("NATO RESTRICTED", 1, "NATO RESTRICTED"),
        ("NATO CONFIDENTIAL", 2, "NATO CONFIDENTIAL"),
        ("NATO SECRET", 3, "NATO SECRET"),
        ("COSMIC TOP SECRET", 4, "COSMIC TOP SECRET"),
        ("RESTREINT UE/EU RESTRICTED", 1, "RESTREINT UE"),
        ("CONFIDENTIEL UE/EU CONFIDENTIAL", 2, "CONFIDENTIEL UE"),
        ("SECRET UE/EU SECRET", 3, "SECRET UE"),
        ("TRÈS SECRET UE/EU TOP SECRET", 4, "TRÈS SECRET UE"),
        ("DIFFUSION RESTREINTE", 1, "FR DIFFUSION RESTREINTE"),
        ("OFFICIAL-SENSITIVE", 1, "UK OFFICIAL-SENSITIVE"),
    ],
)
def test_grades_land_on_one_scale(text, level, label):
    report = _marked(text)
    assert (report.level, report.label) == (level, label), report.findings


def test_a_bilingual_eu_marking_is_one_finding():
    report = _marked("RESTREINT UE/EU RESTRICTED")
    assert len(report.markings) == 1
    assert report.markings[0].match == "RESTREINT UE/EU RESTRICTED"


def test_the_same_marking_in_header_and_footer_is_two_findings():
    # Regression: the merge meant for the two halves of a bilingual marking
    # also swallowed a footer whose text repeats the header.
    report = _report([("VS-NfD", HEADER), *BODY, ("VS-NfD", FOOTER)])
    assert [f.reason for f in report.markings] == ["header", "footer"]


# ------------------------------------------------------------------ how OCR reads them


@pytest.mark.parametrize(
    ("text", "level"),
    [("V5-NFD", 1), ("VS-NtD", 1), ("GEHElM", 3), ("VS-VERTRAUL1CH", 2), ("STRENG GEHEIN", 4)],
)
def test_ocr_confusions_are_undone_and_flagged(text, level):
    report = _marked(text)
    assert report.level == level, report.findings
    assert all(f.fuzzy for f in report.markings)


@pytest.mark.parametrize(
    ("text", "level"),
    [("G E H E I M", 3), ("V S - V E R T R A U L I C H", 2), ("S T R E N G  G E H E I M", 4)],
)
def test_letter_spaced_stamps_are_read(text, level):
    assert _marked(text).level == level


def test_a_stamp_merged_into_the_letterhead_is_still_a_marking():
    # A rotated stamp is a tall box; it bridged two letterhead rows and the
    # layout read all three as one line, which left the stamp in a sentence.
    parts = [
        Segment("Bundesministerium für Beispiele", Box(98, 158, 375, 180), 0.97),
        Segment("Referat 12 Az 12-345/2026", Box(98, 190, 374, 211), 0.94),
        Segment("VS-NfD", Box(452, 140, 588, 211), 0.96),
    ]
    merged = Line(
        text=" ".join(p.text for p in parts),
        box=Box(98, 140, 588, 211),
        confidence=0.95,
        angle=0.0,
        segments=tuple(parts),
    )
    report = _report([merged, *BODY])
    assert report.level == 1
    (stamp,) = report.markings
    assert stamp.match == "VS-NfD"
    assert stamp.box.x0 >= 452, "boxed as the stamp, not the whole merged line"


# ------------------------------------------------------------------ marking or mention


@pytest.mark.parametrize(
    "sentence",
    [
        "gemäß VSA sind Unterlagen des Grades VS-NfD entsprechend zu kennzeichnen.",
        "Dieses Dokument ist nicht als Verschlusssache eingestuft.",
        "Die Einstufung als GEHEIM ist nach Ablauf der Frist aufzuheben.",
        "Das Gerät ist zugelassen für VS-NfD.",
        "Dieses Handbuch ist nur für den Dienstgebrauch bestimmt.",
        "Dokumente mit TLP:AMBER dürfen nur im Haus geteilt werden.",
        "Unterlagen der Stufe NATO SECRET werden im Tresor verwahrt.",
    ],
)
def test_a_sentence_about_a_grade_is_a_mention(sentence):
    report = _report([*BODY, (sentence, 900)])
    assert report.level == 0
    assert report.tlp is None
    assert report.markings == ()
    assert report.mentions, "the mention should still be reported"


@pytest.mark.parametrize(
    "sentence",
    [
        "Die Wahl ist geheim; das Betriebsgeheimnis bleibt gewahrt.",
        "Bitte behandeln Sie diese Angaben vertraulich.",
        "Das Formular liegt im Internet und intern auf dem Laufwerk.",
        "Streng geheim! Wir planen eine Überraschungsparty.",
        "Die NFD GmbH liefert pünktlich.",
        "Bayern vs. Dortmund, VS Wien gegen VfB",
        "Ihre Geheimzahl erhalten Sie getrennt.",
        "This e-mail is confidential and intended solely for the addressee.",
    ],
)
def test_ordinary_words_are_not_grades_at_all(sentence):
    assert _report([*BODY, (sentence, 900)]).findings == ()


def test_a_word_built_on_a_grade_is_a_mention():
    report = _marked("Leitfaden zur VS-NfD-Zulassung")
    assert report.level == 0
    assert report.mentions[0].reason == "compound word"


def test_a_page_listing_the_grades_is_not_marked_with_them():
    report = _report(
        [
            ("Geheimhaltungsgrade nach § 4 SÜG", 300),
            ("STRENG GEHEIM", 400),
            ("GEHEIM", 460),
            ("VS-VERTRAULICH", 520),
            ("VS-NUR FÜR DEN DIENSTGEBRAUCH", 580),
        ]
    )
    assert report.level == 0
    assert {f.reason for f in report.mentions} == {"list of grades"}


def test_a_grade_in_a_table_cell_is_a_mention():
    doc = Document(
        source="t",
        pages=(_page([("VS-VERTRAULICH", 900)], table=True),),
        elapsed_ms=0.0,
    )
    report = doc.markings()
    assert report.level == 0
    assert report.mentions[0].reason == "table"


@pytest.mark.parametrize(
    "line",
    [
        "Geheimhaltungsgrad: VS-NfD",
        "VS-NfD   Seite 2 von 5",
        "Kopie Nr. 3 von 5  VS-NfD",
        "Bundesministerium des Innern                VS-NfD",
    ],
)
def test_page_numbers_field_labels_and_letterheads_keep_a_marking_a_marking(line):
    assert _marked(line).level == 1


# ------------------------------------------------------------------ context


def test_vertraulich_means_what_the_country_says_it_means():
    at = _report([("VERTRAULICH", HEADER), ("Republik Österreich, BMI, Wien", 300), *BODY])
    ch = _report([("VERTRAULICH", HEADER), ("Schweizerische Eidgenossenschaft, Bern", 300)])
    firm = _report([("VERTRAULICH", HEADER), *BODY])
    assert (at.level, at.label) == (2, "AT VERTRAULICH")
    assert (ch.level, ch.label) == (2, "CH VERTRAULICH")
    assert (firm.level, firm.company) == (0, "VERTRAULICH")


def test_intern_is_a_swiss_grade_and_a_company_marking():
    ch = _report([("INTERN", HEADER), ("Schweizerische Eidgenossenschaft, VBS, Bern", 300)])
    firm = _report([("INTERN", HEADER), *BODY])
    assert (ch.level, ch.label) == (1, "CH INTERN")
    assert (firm.level, firm.company) == (0, "INTERN")


def test_a_us_banner_carries_its_caveats():
    report = _report(
        [
            ("TOP SECRET//NOFORN", HEADER),
            ("Classified By: J. Doe  Derived From: Guide 12  Declassify On: 2045", 300),
            *BODY,
        ]
    )
    assert (report.level, report.label) == (4, "US TOP SECRET")
    assert report.caveats == ("NOFORN",)


@pytest.mark.parametrize(
    ("text", "colour"),
    [("TLP:AMBER+STRICT", "AMBER+STRICT"), ("TLP: RED", "RED"), ("TLP:WHITE", "CLEAR")],
)
def test_tlp_is_reported_beside_the_grades(text, colour):
    report = _marked(text)
    assert report.tlp == colour
    assert report.level == 0 and not report.classified


# ------------------------------------------------------------------ what the VSA prescribes


@pytest.mark.parametrize(
    "line",
    [
        "amtlich geheimgehalten",
        "AMTLICH GEHEIM GEHALTEN",
        "auf amtliche Veranlassung geheimgehalten",
    ],
)
def test_amtlich_geheimgehalten_means_at_least_vs_vertraulich(line):
    # VSA 2023 Anlage IV puts it beside VS-VERTRAULICH, GEHEIM and STRENG
    # GEHEIM, so on its own it is a lower bound, not a grade.
    report = _marked(line)
    assert (report.level, report.label) == (2, "VS (amtlich geheimgehalten)")


def test_a_named_grade_beats_the_lower_bound_beside_it():
    report = _report([("VS-VERTRAULICH", HEADER), ("amtlich geheimgehalten", 150), *BODY])
    assert (report.level, report.label) == (2, "VS-VERTRAULICH")


def test_the_classification_term_line_marks_a_document():
    report = _report([*BODY, ("Die VS-Einstufung endet mit Ablauf des Jahres 2055.", 900)])
    assert (report.level, report.label) == (1, "VS")
    policy = "Auf Seite 1 steht, dass die VS-Einstufung endet mit Ablauf des Jahres."
    assert _report([*BODY, (policy, 900)]).level == 0


@pytest.mark.parametrize(
    "line",
    ["Betreff: VS-NfD – Beschaffung von Führungsschienen", "VS-NfD: Beschaffung der Liegenschaft"],
)
def test_a_grade_before_the_subject_is_a_marking(line):
    assert _report([*BODY, (line, 900)]).level == 1


def test_offen_and_unclassified_are_markings_that_classify_nothing():
    report = _marked("OFFEN")
    assert (report.level, report.label, report.classified) == (0, "OFFEN", False)
    assert _marked("UNCLASSIFIED").label == "UNCLASSIFIED"


def test_an_old_geheim_stamp_with_its_exclamation_mark():
    assert _marked("Geheim!").level == 3
    assert _marked("Geheime Kommandosache!").label == "GEHEIME KOMMANDOSACHE"


@pytest.mark.parametrize(
    "heading",
    [
        "GEHEIME WAHL",
        "EINGESCHRÄNKTE HAFTUNG",
        "VERTRAULICHE MITTEILUNG",
        "PERSONNEL ET CONFIDENTIEL",
    ],
)
def test_an_inflected_word_is_not_repaired_into_a_grade(heading):
    # The OCR repair used to read GEHEIME as GEHEIM one edit away.
    report = _marked(heading)
    assert report.level == 0
    assert not any(f.fuzzy for f in report.findings)


def test_official_sensitive_is_found_without_its_hyphen():
    assert _marked("OFFICIAL SENSITIVE").label == "UK OFFICIAL-SENSITIVE"


@pytest.mark.parametrize(
    ("line", "label"),
    [
        ("INTERNE", "CH INTERN"),
        ("AD USO INTERNO", "CH INTERN"),
        ("CONFIDENZIALE", "CH VERTRAULICH"),
    ],
)
def test_the_swiss_grades_in_french_and_italian(line, label):
    report = _report(
        [(line, HEADER), ("Confédération suisse, Schweizerische Eidgenossenschaft", 300)]
    )
    assert report.label == label


def test_the_tlp_explainer_is_not_tlp_red():
    # The BSI's own explainer is TLP:CLEAR and then lists every colour.
    report = _report(
        [
            ("TLP:CLEAR", HEADER),
            ("TLP:RED", 500),
            ("TLP:AMBER+STRICT", 600),
            ("TLP:AMBER", 700),
            ("TLP:GREEN", 800),
        ]
    )
    assert report.tlp == "CLEAR"


def test_a_specimen_page_shows_markings_without_carrying_them():
    report = _report([("GEHEIM", HEADER), ("MUSTER", 1000), *BODY, ("GEHEIM", FOOTER)])
    assert report.level == 0
    assert {f.reason for f in report.mentions} == {"specimen"}


def test_a_release_stamp_flags_the_grade_as_lifted():
    report = _report([("SECRET", HEADER), ("Approved For Release 2005/01/12", 150), *BODY])
    assert report.cancelled


def test_a_unicode_nfd_heading_is_not_vs_nfd():
    assert _marked("NFD").findings == ()
    assert _marked("NfD").level == 1


# ------------------------------------------------------------------ the file itself


def test_a_sensitivity_label_in_the_metadata_is_a_marking(tmp_path):
    # How Acrobat and the MIP SDK leave it in XMP; the page shows nothing.
    guid = "1b2c3d4e-0000-4000-8000-00000000abcd"
    pdf = tmp_path / "bericht.pdf"
    pdf.write_bytes(
        b"%PDF-1.7\n<x:xmpmeta><rdf:Description "
        + f'pdfx:MSIP_Label_{guid}_Enabled="true" pdfx:MSIP_Label_{guid}_Name="VS-NfD"'.encode()
        + b"/></x:xmpmeta>\n%%EOF\n"
    )
    report = markings.inspect(_doc(BODY), file=pdf)
    assert (report.level, report.label) == (1, "VS-NfD")
    (label,) = report.markings
    assert (label.page, label.reason) == (0, "sensitivity label")
    assert report.pages == (0,), "evidence from the file belongs to no page"


def test_a_label_that_names_no_grade_is_still_reported(tmp_path):
    guid = "1b2c3d4e-0000-4000-8000-00000000abcd"
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(f"/MSIP_Label_{guid}_Name (Highly Confidential)".encode())
    assert markings.inspect(_doc(BODY), file=pdf).company == "CONFIDENTIAL"
    pdf.write_bytes(f"/MSIP_Label_{guid}_Name (Public)".encode())
    report = markings.inspect(_doc(BODY), file=pdf)
    assert (report.level, report.label) == (0, "Public")


def test_a_grade_in_the_file_name_is_a_hint_not_a_marking(tmp_path):
    named = tmp_path / "Merkblatt_VS-NfD.pdf"
    named.write_bytes(b"%PDF-1.4\n%%EOF\n")
    report = markings.inspect(_doc(BODY), file=named)
    assert report.level == 0
    assert [(f.kind, f.reason, f.label) for f in report.findings] == [
        ("mention", "file name", "VS-NfD")
    ]


# ------------------------------------------------------------------ the document


def test_a_page_without_the_grade_is_named():
    marked = [("VS-NfD", HEADER), *BODY, ("VS-NfD", FOOTER)]
    report = _report(marked, BODY, marked)
    assert report.pages == (1, 0, 1)
    assert report.unmarked_pages == (2,)


def test_pages_are_numbered_as_in_the_source_when_only_some_were_scanned():
    marked = _page([("GEHEIM", HEADER), *BODY], index=2)
    bare = _page(BODY, index=4)
    report = Document(source="t", pages=(marked, bare), elapsed_ms=0.0).markings()
    assert [f.page for f in report.markings] == [3]
    assert report.page_numbers == (3, 5)
    assert report.unmarked_pages == (5,)


def test_a_lifted_grade_is_flagged_but_still_counts():
    # Conservative on purpose: whether "aufgehoben" applies to this copy is a
    # human's call, and a gate that waved it through would be the worse error.
    report = _marked("VS-NfD aufgehoben")
    assert report.level == 1
    assert report.cancelled


def test_the_highest_grade_wins():
    report = _report([("VS-NfD", HEADER), *BODY, ("GEHEIM", 1200)])
    assert (report.level, report.label) == (3, "GEHEIM")
    assert report.at_least("vs-nfd") and report.at_least("geheim")
    assert not report.at_least("streng-geheim")


def test_a_report_is_plain_json():
    report = _marked("VS-NfD")
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["label"] == "VS-NfD"
    assert payload["findings"][0]["page"] == 1


@pytest.mark.parametrize(
    ("name", "level"),
    [("vs-nfd", 1), ("VS-V", 2), ("geheim", 3), ("Streng Geheim", 4), ("4", 4), ("secret", 3)],
)
def test_grade_names(name, level):
    assert markings.level_for(name) == level


def test_an_unknown_grade_name_says_what_is_known():
    with pytest.raises(ValueError, match="vs-nfd"):
        markings.level_for("topsecretish")


# ------------------------------------------------------------------ ocrust vs


def _pdf(lines, pages: int = 1) -> bytes:
    """A PDF with text placed where a marking really goes: (text, size, y in pt)."""
    from conftest import _pdf_from_stream, _winansi_literal

    body = ["BT"]
    for text, size, y in lines:
        x = 60 if size < 14 else 150
        body.append(f"/F1 {size} Tf 1 0 0 1 {x} {y} Tm ({_winansi_literal(text)}) Tj")
    body.append("ET")
    return _pdf_from_stream("\n".join(body), pages=pages)


LETTER = [
    ("Bundesministerium für Beispiele", 12, 690),
    ("Betreff: Beschaffung von Führungsschienen", 12, 650),
    ("Die Lieferung erfolgt bis zum 17.03.2026.", 12, 610),
]


@pytest.fixture(scope="module")
def vs_files(tmp_path_factory):
    folder = tmp_path_factory.mktemp("vs")
    marking = "VS – NUR FÜR DEN DIENSTGEBRAUCH"
    (folder / "a_marked.pdf").write_bytes(
        _pdf([(marking, 12, 765), *LETTER, (marking, 12, 25)], pages=2)
    )
    (folder / "b_clean.pdf").write_bytes(_pdf(LETTER))
    (folder / "c_geheim.pdf").write_bytes(_pdf([("GEHEIM", 16, 765), *LETTER]))
    return folder


def test_vs_reports_each_file_and_exits_three_when_marked(engine, vs_files, capsys):
    code = main(["vs", str(vs_files), "-q"])
    out = capsys.readouterr().out
    assert code == 3
    rows = [line.split()[0] for line in out.splitlines() if line.strip()]
    # In input order, even though the files were scanned in parallel.
    assert rows == ["VS-NfD", "-", "GEHEIM"]


def test_vs_fail_on_raises_the_bar(engine, vs_files):
    assert main(["vs", str(vs_files / "a_marked.pdf"), "--fail-on", "geheim", "-q"]) == 0
    assert main(["vs", str(vs_files / "c_geheim.pdf"), "--fail-on", "geheim", "-q"]) == 3
    assert main(["vs", str(vs_files), "--fail-on", "none", "-q"]) == 0


def test_vs_clean_files_exit_zero(engine, vs_files):
    assert main(["vs", str(vs_files / "b_clean.pdf"), "-q"]) == 0


def test_vs_json_carries_the_evidence(engine, vs_files, capsys):
    main(["vs", str(vs_files / "a_marked.pdf"), "-f", "json", "-q"])
    payload = json.loads(capsys.readouterr().out)
    (record,) = payload["files"]
    assert record["label"] == "VS-NfD"
    assert record["pages"] == [1, 1]
    assert {f["reason"] for f in record["findings"]} == {"header", "footer"}
    assert payload["summary"]["tripped"] == 1


def test_vs_csv_has_a_row_per_finding_and_per_clean_file(engine, vs_files, tmp_path):
    report = tmp_path / "audit.csv"
    main(["vs", str(vs_files), "-f", "csv", "-o", str(report), "-q"])
    rows = list(csv.DictReader(io.StringIO(report.read_text(encoding="utf-8"))))
    clean = [r for r in rows if r["source"].endswith("b_clean.pdf")]
    assert len(clean) == 1 and clean[0]["kind"] == ""
    assert sum(r["label"] == "VS-NfD" for r in rows) == 4  # two pages, top and bottom


def test_vs_an_unreadable_file_is_not_a_clean_one(engine, vs_files, tmp_path, capsys):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 this is not a pdf")
    assert main(["vs", str(broken), str(vs_files / "b_clean.pdf"), "-q"]) == 1
    assert "unreadable" in capsys.readouterr().out
    # A marked file still decides the verdict: that is the finding that matters.
    assert main(["vs", str(broken), str(vs_files / "a_marked.pdf"), "-q"]) == 3


def test_vs_rejects_an_unknown_grade(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["vs", "whatever.pdf", "--fail-on", "sehr-geheim"])
    assert raised.value.code == 2
    assert "vs-nfd" in capsys.readouterr().err
