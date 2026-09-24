"""Search profiles: terms found however the scan broke them up, then `ocrust find`.

The first half builds pages by hand, which is what makes it possible to say
exactly how a word was broken; the second half renders PDFs and runs the
command.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import pytest

from ocrust import Block, Box, Document, Line, Page, Segment, terms
from ocrust.cli import _CSV_FIELDS, main

PROFILE = terms.parse(
    {
        "term": [
            {"name": "Projekt Adler", "match": ["Projekt Adler", "Operation Adler"]},
            {"name": "Vereinbarung", "match": "Geheimhaltungsvereinbarung"},
            {"name": "Weißmüller", "match": "Jürgen Weißmüller"},
            {"name": "Kunde", "regex": r"KD-\d{6}", "severity": "low"},
            {"name": "Teil", "match": "FS-220", "fuzzy": 0},
            {"name": "Adler", "match": "Adler", "fuzzy": 0, "not_near": ["Radsport"]},
        ]
    }
)


def _doc(*blocks: list[str]) -> Document:
    built, y = [], 300
    for lines in blocks:
        made = []
        for text in lines:
            box = Box(100, y, 100 + 12 * len(text), y + 30)
            made.append(Line(text=text, box=box, confidence=0.98, angle=0.0))
            y += 40
        built.append(Block(kind="paragraph", box=made[0].box, lines=tuple(made)))
        y += 30
    page = Page(
        index=0,
        width=1654,
        height=2338,
        rotation=0.0,
        origin="image",
        blocks=tuple(built),
        elapsed_ms=0.0,
    )
    return Document(source="t", pages=(page,), elapsed_ms=0.0)


def _found(*blocks: list[str], profile: terms.Profile = PROFILE) -> list[tuple[str, str, tuple]]:
    return [(h.term, h.text, h.how) for h in _doc(*blocks).find(profile)]


# ------------------------------------------------------------------ broken up


@pytest.mark.parametrize(
    ("lines", "text", "how"),
    [
        (["Wir starten das Projekt Adler."], "Projekt Adler", ("exact",)),
        (["P r o j e k t  A d l e r"], "P r o j e k t  A d l e r", ("spaced",)),
        (["Das ProjektAdler läuft."], "ProjektAdler", ("glued",)),
        (["Das Projekt-Adler läuft."], "Projekt-Adler", ("exact",)),
        (["Unterlagen zum Projekt", "Adler anbei."], "Projekt\nAdler", ("split",)),
        (["Das Pr0jekt AdIer startet."], "Pr0jekt AdIer", ("ocr",)),
        (["Die Operation Adler beginnt."], "Operation Adler", ("exact",)),
    ],
)
def test_a_phrase_is_found_however_it_was_broken(lines, text, how):
    hits = [h for h in _found(lines) if h[0] == "Projekt Adler"]
    assert hits == [("Projekt Adler", text, how)]


def test_a_word_hyphenated_at_the_line_end_is_one_word():
    hits = _found(["Die Geheimhaltungs-", "vereinbarung liegt bei."])
    assert hits == [("Vereinbarung", "Geheimhaltungs\nvereinbarung", ("hyphenated",))]


def test_a_stray_space_inside_a_word_is_forgiven():
    assert _found(["Die Geheimhaltungs vereinbarung"])[0][2] == ("broken",)


def test_umlauts_written_out_are_a_spelling_not_an_error():
    hits = _found(["Herr Juergen Weissmueller", "Jürgen Weißrnüller"])
    assert [(h[1], h[2]) for h in hits] == [
        ("Juergen Weissmueller", ("spelling",)),
        ("Jürgen Weißrnüller", ("ocr",)),  # rn read for m
    ]


def test_a_typo_is_a_fuzzy_hit_within_the_budget():
    assert _found(["Geheimhaltungsvereinbrung fehlt"])[0][2] == ("fuzzy",)
    # A long phrase forgives a letter; a term with fuzzy off does not.
    assert [h for h in _found(["Projekt Abler"]) if h[0] == "Projekt Adler"]
    assert not [h for h in _found(["Der Abler kreist"]) if h[0] == "Adler"]


def test_a_term_inside_a_longer_word_is_not_a_hit():
    assert _found(["Der Radler fuhr zum Adlerhorst."]) == []


def test_fuzzy_off_means_exactly_that():
    hits = _found(["Teil FS-220, FS 220 und FS-2200 und FS-221"])
    assert [h[1] for h in hits if h[0] == "Teil"] == ["FS-220", "FS 220"]


def test_not_near_suppresses_a_hit():
    assert not [h for h in _found(["Adler Radsport Verein"]) if h[0] == "Adler"]
    assert [h for h in _found(["Der Adler kreist."]) if h[0] == "Adler"]


def test_a_regex_finds_what_the_layout_broke():
    hits = _found(["Kunde KD-123456 und KD-12345"], ["Kunde KD-12", "3456 bitte"])
    assert [h[1] for h in hits if h[0] == "Kunde"] == ["KD-123456", "KD-12\n3456"]


def test_zones_restrict_where_a_term_counts():
    profile = terms.parse({"term": [{"match": "Entwurf", "zones": ["header"]}]})
    header = Line(text="Entwurf", box=Box(100, 60, 300, 90), confidence=0.99, angle=0.0)
    body = Line(text="Entwurf", box=Box(100, 900, 300, 930), confidence=0.99, angle=0.0)
    page = Page(
        index=0,
        width=1654,
        height=2338,
        rotation=0.0,
        origin="image",
        blocks=(Block(kind="paragraph", box=header.box, lines=(header, body)),),
        elapsed_ms=0.0,
    )
    hits = Document(source="t", pages=(page,), elapsed_ms=0.0).find(profile)
    assert [h.zone for h in hits] == ["header"]


def test_case_sensitive_terms():
    profile = terms.parse({"term": [{"match": "BAföG", "case": True}]})
    assert [h.text for h in _doc(["BAföG und Bafög"]).find(profile)] == ["BAföG"]


def test_an_initial_or_a_house_number_is_not_spent_on_fuzziness():
    profile = terms.parse({"term": [{"match": "M. Schöllhorn"}, {"match": "Hafenstraße 12"}]})

    def texts(line: str) -> list[str]:
        return [h.text for h in _doc([line]).find(profile)]

    assert texts("Gez. M. Schöllhorn") == ["M. Schöllhorn"]
    assert texts("Gez. MSchöllhorn") == ["MSchöllhorn"]
    assert texts("Gez. M. Schöllhom") == ["M. Schöllhom"]  # the long word forgives
    assert texts("Frau Schöllhorn schreibt") == []
    assert texts("Ms Schöllhorn wrote") == []
    assert texts("Hafenstraße 12 und Hafenstraße12") == ["Hafenstraße 12", "Hafenstraße12"]
    assert texts("Hafenstraße 1, Hafenstraße 120") == []


def test_comb_fields_read_as_one_number():
    # A form prints one character a box; the regex sees "KD-438300".
    hits = _found(["Kundennummer: K D - 4 3 8 3 0 0 bitte angeben", "Teil F S - 2 2 0"])
    assert [(h[0], h[1]) for h in hits] == [
        ("Kunde", "K D - 4 3 8 3 0 0"),
        ("Teil", "F S - 2 2 0"),
    ]


def test_a_lost_space_still_shows_as_a_capital():
    profile = terms.parse({"term": [{"match": "Weißmüller", "fuzzy": 0}]})
    assert [h.text for h in _doc(["Sehr geehrter HerrWeißmüller,"]).find(profile)] == ["Weißmüller"]
    # Without the capital nothing marks the boundary: a limit, not a guess.
    assert not _doc(["Sehr geehrter Herrweißmüller,"]).find(profile).hits


def test_a_misspelling_is_read_from_the_start_of_the_word():
    # "Opperation" is "Operation" with a P too many, or, one letter in,
    # "pperation" with a P for the O: the same cost, but inside the word.
    profile = terms.parse({"term": [{"match": "Operation Silberfuchs", "fuzzy": 1}]})
    hits = _doc(["Die Opperation Silberfuchs läuft"]).find(profile)
    assert [(h.text, h.how) for h in hits] == [("Opperation Silberfuchs", ("fuzzy",))]


def test_a_short_code_after_a_word_ending_in_r():
    # The prefilter reads "rn" as "m"; "Server NOX" must not become "ServeMOX".
    profile = terms.parse({"term": [{"match": "NOX", "fuzzy": "off"}]})
    for line in ("Server NOX neu gestartet", "SERVER NOX"):
        assert [h.text for h in _doc([line]).find(profile)] == ["NOX"]


def test_a_stamp_read_twice_is_one_hit():
    main = Line(text="Projekt Adler", box=Box(100, 300, 400, 330), confidence=0.9, angle=0.0)
    stamp = Line(text="PROJEKT ADLER", box=Box(104, 297, 396, 333), confidence=0.9, angle=0.0)
    page = Page(
        index=0,
        width=1654,
        height=2338,
        rotation=0.0,
        origin="image",
        blocks=(
            Block(kind="paragraph", box=main.box, lines=(main,)),
            Block(kind="stamp", box=stamp.box, lines=(stamp,)),
        ),
        elapsed_ms=0.0,
    )
    hits = Document(source="t", pages=(page,), elapsed_ms=0.0).find(PROFILE)
    assert [h.term for h in hits] == ["Projekt Adler", "Adler"]


def _two_columns(rows: list[tuple[str, str]], y0: int = 300) -> Page:
    """A page whose layout took two columns' lines for one, as a scan of a
    newsletter often does: each line is a left and a right piece."""
    lines = []
    for n, (left, right) in enumerate(rows):
        y = y0 + 40 * n
        pieces = []
        if left:
            pieces.append(Segment(left, Box(100, y, 100 + 12 * len(left), y + 30), 0.98))
        if right:
            pieces.append(Segment(right, Box(850, y, 850 + 12 * len(right), y + 30), 0.98))
        text = " ".join(p.text for p in pieces)
        box = Box(pieces[0].box.x0, y, pieces[-1].box.x1, y + 30)
        segments = tuple(pieces) if len(pieces) > 1 else ()
        lines.append(Line(text=text, box=box, confidence=0.98, angle=0.0, segments=segments))
    block = Block(kind="paragraph", box=lines[0].box, lines=tuple(lines))
    return Page(
        index=0,
        width=1654,
        height=2338,
        rotation=0.0,
        origin="image",
        blocks=(block,),
        elapsed_ms=0.0,
    )


def test_a_phrase_broken_over_two_columns_is_found():
    page = _two_columns(
        [
            ("Die Kollegen beginnen", "Adler wird bis zum Sommer"),
            ("im Mai mit dem Projekt", "alle Tore erneuern."),
        ]
    )
    # Read a row at a time, "Projekt" ends the left column and "Adler" heads
    # the right one; read column by column, they meet.
    hits = Document(source="t", pages=(page,), elapsed_ms=0.0).find(PROFILE)
    assert [(h.term, h.text, h.how) for h in hits if h.term == "Projekt Adler"] == [
        ("Projekt Adler", "Projekt\nAdler", ("split",))
    ]


def test_a_cell_that_wraps_is_read_down_the_column():
    page = _two_columns([("Geheimhaltungs", "offen"), ("vereinbarung", "")])
    hits = Document(source="t", pages=(page,), elapsed_ms=0.0).find(PROFILE)
    assert [h.text for h in hits if h.term == "Vereinbarung"] == ["Geheimhaltungs\nvereinbarung"]


def test_a_longer_word_is_not_the_term_misread():
    # One edit is within the budget of "Adler", but a letter too many at the
    # edge makes another word, not a misreading of this one.
    profile = terms.parse({"term": [{"match": "Adler"}, {"match": "Operation Silberfuchs"}]})
    for line in ("Der Radler zahlt bar.", "Die Sadler GmbH", "des Adlers Horst"):
        assert [h.text for h in _doc([line]).find(profile)] == [], line
    # An extra letter that can be read inside the word is a misreading: the
    # doubled r of "Adlerr", the doubled p of "Opperation".
    for line, text in (
        ("Der Adlerr", "Adlerr"),
        ("Die Opperation Silberfuchs", "Opperation Silberfuchs"),
    ):
        assert [h.text for h in _doc([line]).find(profile)] == [text]
    # And a letter or digit missing at an edge makes another word or number.
    shorter = terms.parse({"term": [{"match": "Radler"}, {"match": "Hafenstraße 120"}]})
    for line in ("Der Adler kreist", "Hafenstraße 12", "Hafenstraße 1200"):
        assert not _doc([line]).find(shorter).hits, line
    assert [h.text for h in _doc(["Gez. M. Schöllhom"]).find(terms.load(["M. Schöllhorn"]))] == [
        "M. Schöllhom"
    ]


def test_case_is_checked_letter_by_letter_along_the_reading():
    profile = terms.parse({"term": [{"match": "Müller", "case": True}]})
    found = {
        line: [h.text for h in _doc([line]).find(profile)]
        for line in ("Herr Müller", "Herr Mueller", "Herr MÜLLER", "Herr MUELLER", "Herr mueller")
    }
    assert found == {
        "Herr Müller": ["Müller"],
        "Herr Mueller": ["Mueller"],
        "Herr MÜLLER": [],
        "Herr MUELLER": [],
        "Herr mueller": [],
    }
    fuzzy = terms.parse({"term": [{"match": "Adler", "case": True, "fuzzy": 1}]})
    assert [h.text for h in _doc(["Das Adlxr Team"]).find(fuzzy)] == ["Adlxr"]
    assert not _doc(["Das ADLLER Team"]).find(fuzzy).hits


def test_ss_for_sharp_s_is_a_spelling():
    profile = terms.parse({"term": [{"match": "Hafenstraße"}, {"match": "Strasse 5"}]})
    hits = _doc(["Die Hafenstrasse und die Straße 5"]).find(profile)
    assert [(h.text, h.how) for h in hits] == [
        ("Hafenstrasse", ("spelling",)),
        ("Straße 5", ("spelling",)),
    ]


def test_fuzzy_allows_up_to_a_third_of_the_term():
    for word, typo, fuzzy in (("Abteil", "Abxeiy", 2), ("Fax", "Fux", 1)):
        profile = terms.parse({"term": [{"match": word, "fuzzy": fuzzy}]})
        assert [h.text for h in _doc([f"Das {typo} dort"]).find(profile)] == [typo]
        profile = terms.parse({"term": [{"match": word, "fuzzy": fuzzy - 1}]})
        assert not _doc([f"Das {typo} dort"]).find(profile).hits


def test_not_near_sees_the_word_the_hit_is_part_of():
    profile = terms.parse(
        {"term": [{"match": "Adler", "whole_words": False, "not_near": ["Adlerhorst"]}]}
    )
    assert not _doc(["Der Adlerhorst liegt oben"]).find(profile).hits
    assert [h.text for h in _doc(["Der Adler kreist"]).find(profile)] == ["Adler"]


def test_a_regex_sees_a_number_broken_after_its_dash():
    hits = _found(["Kundennummer KD-", "123456 bitte angeben"])
    assert [(h[0], h[1]) for h in hits] == [("Kunde", "KD-\n123456")]


def test_a_word_the_layout_joined_across_a_line_end():
    # A real scan: the layout reads "Brand-" / "meldezentrale" as one line
    # "Brandmeldezentrale", and the words keep their own boxes and hyphen.
    from ocrust import Word

    words = (
        Word("Die", Box(700, 100, 760, 130), 0.99),
        Word("Brand-", Box(850, 100, 920, 130), 0.99),
        Word("meldezentrale", Box(206, 140, 362, 170), 0.99),
        Word("piept", Box(370, 140, 440, 170), 0.99),
    )
    line = Line(
        text="Die Brandmeldezentrale piept",
        box=Box(206, 100, 920, 170),
        confidence=0.99,
        angle=0.0,
        words=words,
    )
    page = Page(
        index=0,
        width=1654,
        height=2338,
        rotation=0.0,
        origin="image",
        blocks=(Block(kind="paragraph", box=line.box, lines=(line,)),),
        elapsed_ms=0.0,
    )
    document = Document(source="t", pages=(page,), elapsed_ms=0.0)
    (hit,) = document.find(terms.load(["Brandmeldezentrale"]))
    assert hit.how == ("hyphenated",)
    assert [b.as_tuple() for b in hit.boxes] == [(850, 100, 920, 130), (206, 140, 362, 170)]


def test_near_is_judged_in_the_order_that_puts_the_words_together():
    page = _two_columns(
        [
            ("Der Codename", "Die Kantine bleibt am Freitag wegen Umbau geschlossen"),
            ("Falke gilt ab Mai.", "und Anmeldungen nimmt das Sekretariat entgegen"),
        ]
    )
    profile = terms.parse(
        {"term": [{"match": "Falke", "fuzzy": 0, "near": ["Codename"], "window": 20}]}
    )
    hits = Document(source="t", pages=(page,), elapsed_ms=0.0).find(profile)
    assert [h.text for h in hits] == ["Falke"]


def test_a_join_across_a_column_gutter_is_a_split_not_exact():
    page = _two_columns(
        [
            ("Die Kantine startet das Projekt", "Adler-Gehege im Zoo wird neu"),
            ("Salat am Freitag mit allen.", "gebaut, sagt der Direktor."),
        ]
    )
    hits = Document(source="t", pages=(page,), elapsed_ms=0.0).find(PROFILE)
    assert [h.how for h in hits if h.term == "Projekt Adler"] == [("split",)]


def test_a_box_drawn_backwards_does_not_hang_the_column_order():
    first = Line(text="Adler eins", box=Box(0, 0, 10, 10), confidence=1.0, angle=0.0)
    backwards = Line(text="zwei", box=Box(50, 20, 5, 30), confidence=1.0, angle=0.0)
    page = Page(
        index=0,
        width=100,
        height=100,
        rotation=0.0,
        origin="image",
        blocks=(Block(kind="paragraph", box=first.box, lines=(first, backwards)),),
        elapsed_ms=0.0,
    )
    report = Document(source="t", pages=(page,), elapsed_ms=0.0).find(terms.load(["Adler"]))
    assert report.terms == {"Adler": 1}


def test_a_footnote_or_trademark_beside_a_term_is_not_part_of_it():
    profile = terms.parse({"term": [{"match": "Projekt Adler"}, {"match": "ORKA"}]})
    for line, text in (
        ("siehe Projekt Adler¹ unten", "Projekt Adler"),
        ("Wir nutzen ORKA™ seit 2020", "ORKA"),
    ):
        assert [h.text for h in _doc([line]).find(profile)] == [text], line


def test_a_raised_digit_is_a_digit_set_apart():
    profile = terms.parse({"term": [{"match": "120 m2"}, {"match": "CO₂"}]})
    hits = _doc(["Fläche 120 m², Ausstoß CO2"]).find(profile)
    assert [(h.text, h.how) for h in hits] == [("120 m²", ("exact",)), ("CO2", ("exact",))]
    # CO₂ is not the Co. of a company name.
    assert not _doc(["Müller & Co. KG"]).find(profile).hits


def test_letters_and_digits_written_together_are_one_word():
    profile = terms.parse(
        {
            "term": [
                {"match": "Hafenstraße 12"},
                {"match": "FS-220", "fuzzy": 0},
                {"match": "VEGA", "fuzzy": 0},
                {"match": "Adler"},
            ]
        }
    )
    for line in ("Hafenstraße 12a", "FS-220B", "VEGA2", "Adler1"):
        assert not _doc([line]).find(profile).hits, line


def test_a_long_term_that_lost_an_edge_letter_is_still_found():
    profile = terms.parse({"term": [{"match": "Geheimhaltungsvereinbarung"}]})
    assert len(_doc(["Die eheimhaltungsvereinbarung liegt bei"]).find(profile).hits) == 1


def test_not_near_does_not_look_inside_the_hit():
    profile = terms.parse({"term": [{"match": "Kranich", "fuzzy": 0, "not_near": ["Kran"]}]})
    assert [h.text for h in _doc(["Das Projekt Kranich startet"]).find(profile)] == ["Kranich"]
    assert not _doc(["Der Baukran neben Kranich"]).find(profile).hits


def test_not_near_is_not_spelled_across_a_gap_at_the_hit():
    # "der Adler" reads DERADLER, which holds RADLER; the space says otherwise.
    profile = terms.parse({"term": [{"match": "Adler", "not_near": ["Radler"]}]})
    for line in ("Der Adler fliegt", "Herr Adler kam"):
        assert [h.text for h in _doc([line]).find(profile)] == ["Adler"], line
    inside = terms.parse(
        {"term": [{"match": "Adler", "whole_words": False, "not_near": ["Adlerhorst"]}]}
    )
    assert [h.text for h in _doc(["Adler horstet"]).find(inside)] == ["Adler"]
    assert not _doc(["Der Adlerhorst"]).find(inside).hits


def test_not_near_vetoes_in_either_reading_order():
    page = _two_columns(
        [
            ("Der Falke", "Die Kantine bleibt am Freitag wegen Umbau geschlossen"),
            ("Vogelschutz im Park.", "und Anmeldungen nimmt das Sekretariat entgegen"),
        ]
    )
    profile = terms.parse(
        {"term": [{"match": "Falke", "fuzzy": 0, "not_near": ["Vogelschutz"], "window": 20}]}
    )
    assert not Document(source="t", pages=(page,), elapsed_ms=0.0).find(profile).hits


def test_a_regex_respects_whole_words():
    hits = _found(["KD-1234567 und XKD-123456 und KD-123456."])
    assert [h[1] for h in hits if h[0] == "Kunde"] == ["KD-123456"]
    loose = terms.parse({"term": [{"regex": r"KD-\d{6}", "whole_words": False}]})
    assert len(_doc(["KD-1234567"]).find(loose).hits) == 1


def test_a_regex_over_a_hyphenated_word_and_a_broken_number():
    profile = terms.parse({"term": [{"name": "K", "regex": r"Kundennummer KD-\d{6}"}]})
    hits = _doc(["Ihre Kunden-", "nummer KD-12", "3456 bitte"]).find(profile)
    assert [h.text for h in hits] == ["Kunden\nnummer KD-12\n3456"]


def test_symbols_in_a_phrase_do_not_shift_the_case_check():
    profile = terms.parse({"term": [{"match": "Temperatur ℃ Max", "case": True}]})
    assert len(_doc(["Temperatur ℃ Max"]).find(profile).hits) == 1
    rooms = terms.parse({"term": [{"match": "3½ Zimmer"}]})
    assert [h.text for h in _doc(["Die 3½ Zimmer Wohnung"]).find(rooms)] == ["3½ Zimmer"]


def test_an_umlaut_written_decomposed():
    profile = terms.parse({"term": [{"match": "Müller", "case": True}]})
    assert len(_doc(["Herr Mu\u0308ller kam"]).find(profile).hits) == 1


def test_search_gives_a_box_per_row_for_a_joined_word():
    from ocrust import Word

    words = (
        Word("Brand-", Box(850, 100, 920, 130), 0.99),
        Word("meldezentrale", Box(206, 140, 362, 170), 0.99),
    )
    line = Line(
        text="Brandmeldezentrale",
        box=Box(206, 100, 920, 170),
        confidence=0.99,
        angle=0.0,
        words=words,
    )
    page = Page(
        index=0,
        width=1654,
        height=2338,
        rotation=0.0,
        origin="image",
        blocks=(Block(kind="paragraph", box=line.box, lines=(line,)),),
        elapsed_ms=0.0,
    )
    (match,) = Document(source="t", pages=(page,), elapsed_ms=0.0).search("Brandmeldezentrale")
    assert [b.as_tuple() for b in match.boxes] == [(850, 100, 920, 130), (206, 140, 362, 170)]


def test_every_hit_has_a_box_per_line():
    found = _doc(["Unterlagen zum Projekt", "Adler anbei."]).find(PROFILE)
    (hit,) = [h for h in found if h.term == "Projekt Adler"]
    assert len(hit.boxes) == 2
    assert hit.box.y0 <= hit.boxes[0].y0 and hit.box.y1 >= hit.boxes[1].y1
    assert json.loads(json.dumps(hit.to_dict()))["how"] == ["split"]


# ------------------------------------------------------------------ profiles


def test_a_term_with_only_a_name_is_looked_for_by_its_name():
    assert terms.parse({"term": [{"name": "ORKA"}]}).terms[0].match == ("ORKA",)


def test_profiles_load_from_toml_json_text_and_lists(tmp_path):
    toml = tmp_path / "p.toml"
    toml.write_text('[[term]]\nmatch = "Adler"\nseverity = "high"\n', encoding="utf-8")
    jsn = tmp_path / "p.json"
    jsn.write_text(json.dumps({"term": [{"match": "Adler"}]}), encoding="utf-8")
    txt = tmp_path / "p.txt"
    txt.write_text("# Namen\nAdler\n\nProjekt Adler\n", encoding="utf-8")
    try:
        import tomllib  # noqa: F401
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        pytest.importorskip("tomli")
    assert terms.load(toml).terms[0].severity == "high"
    assert terms.load(jsn).terms[0].name == "Adler"
    assert [t.name for t in terms.load(txt).terms] == ["Adler", "Projekt Adler"]
    assert [t.name for t in terms.load(["A", "B"]).terms] == ["A", "B"]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"term": [{"match": "x", "sevrity": "high"}]}, "unknown .*sevrity"),
        ({"term": [{"match": "x", "severity": "urgent"}]}, "severity 'urgent'"),
        ({"term": [{"regex": "KD-(\\d"}]}, "regex"),
        ({"term": [{"category": "x"}]}, "needs `match`"),
        ({"term": [{"match": "x", "fuzzy": 7}]}, "fuzzy 7"),
        ({"term": [{"match": "x", "zones": ["margin"]}]}, "zone 'margin'"),
        ({"term": [{"match": "a"}, {"match": "a"}]}, "two terms are named"),
        ({"term": []}, "no terms"),
        ({"bogus": 1}, "unknown section"),
        ({"term": {"match": "Adler"}}, "double brackets"),
        ({"term": ["Adler", "Falke"]}, "is not a table"),
        ({"term": None}, "must be a list"),
        ({"settings": [], "term": [{"match": "x"}]}, "must be a table"),
        ({"term": [{"match": "x", "markings": True}]}, "markings is a .settings. key"),
        ({"term": [{"match": "x", "category": ["a"]}]}, "category must be a string"),
        ({"term": [{"match": "x", "name": {"a": 1}}]}, "name must be a string"),
        ({"term": [{"match": "x", "not_near": ["€"]}]}, "not_near '€' has no letters"),
        ({"term": [{"match": "x", "near": ["§"]}]}, "near '§' has no letters"),
    ],
)
def test_a_bad_profile_says_what_is_wrong(data, message):
    with pytest.raises(terms.ProfileError, match=message):
        terms.parse(data, source="profil.toml")


def test_a_profile_file_that_is_not_a_profile(tmp_path):
    listed = tmp_path / "list.json"
    listed.write_text('["Adler"]', encoding="utf-8")
    with pytest.raises(terms.ProfileError, match="not a list"):
        terms.load(listed)
    latin = tmp_path / "namen.txt"
    latin.write_bytes("Müller\n".encode("latin-1"))
    with pytest.raises(terms.ProfileError, match="not UTF-8"):
        terms.load(latin)


def test_a_comment_after_a_phrase(tmp_path):
    listed = tmp_path / "namen.txt"
    listed.write_text("# Namen\nAdler   # der Vogel\nC#\n", encoding="utf-8")
    assert [t.match for t in terms.load(listed).terms] == [("Adler",), ("C#",)]


def test_two_profiles_may_not_name_one_term_twice():
    a = terms.parse({"term": [{"match": "Adler"}]}, source="a.toml")
    with pytest.raises(terms.ProfileError, match="two terms are named 'Adler'"):
        a + a


# ------------------------------------------------------------------ ocrust find


def _letter_pdf(lines: list[str]) -> bytes:
    from conftest import _pdf_from_stream, _winansi_literal

    body = ["BT"]
    for i, text in enumerate(lines):
        body.append(f"/F1 12 Tf 1 0 0 1 60 {700 - 30 * i} Tm ({_winansi_literal(text)}) Tj")
    body.append("ET")
    return _pdf_from_stream("\n".join(body))


@pytest.fixture(scope="module")
def letters(tmp_path_factory):
    folder = tmp_path_factory.mktemp("find")
    (folder / "a_brief.pdf").write_bytes(
        _letter_pdf(
            [
                "Sehr geehrter Herr Juergen Weissmueller,",
                "anbei Unterlagen zum ProjektAdler (Kunde KD-123456).",
                "P r o j e k t   A d l e r   startet im Mai.",
            ]
        )
    )
    (folder / "b_rechnung.pdf").write_bytes(
        _letter_pdf(["RECHNUNG 2026-0042", "Der Radler vom Adlerhorst zahlt bar."])
    )
    profile = folder / "profil.json"
    profile.write_text(
        json.dumps(
            {
                "term": [
                    {"name": "Projekt Adler", "match": "Projekt Adler", "severity": "high"},
                    {"name": "Weißmüller", "match": "Jürgen Weißmüller"},
                    {"name": "Kunde", "regex": "KD-\\d{6}", "severity": "low"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_find_reports_hits_and_exits_three(engine, letters, capsys):
    code = main(["find", str(letters), "--terms", str(letters / "profil.json"), "-q"])
    out = capsys.readouterr().out.splitlines()
    assert code == 3
    assert out[0].split()[:2] == ["4", "hits"]
    assert out[1].split()[0] == "-"


def test_find_fail_on_severity(engine, letters):
    profile = str(letters / "profil.json")
    rechnung = str(letters / "b_rechnung.pdf")
    brief = str(letters / "a_brief.pdf")
    assert main(["find", rechnung, "--terms", profile, "-q"]) == 0
    assert main(["find", brief, "--terms", profile, "--fail-on", "critical", "-q"]) == 0
    assert main(["find", brief, "--terms", profile, "--fail-on", "high", "-q"]) == 3


def test_find_json_and_csv_carry_every_hit(engine, letters, tmp_path, capsys):
    profile = str(letters / "profil.json")
    main(["find", str(letters / "a_brief.pdf"), "--terms", profile, "-f", "json", "-q"])
    record = json.loads(capsys.readouterr().out)["files"][0]
    hows = {tuple(h["how"]) for h in record["terms"]["hits"]}
    assert ("glued",) in hows and ("spaced",) in hows and ("spelling",) in hows
    report = tmp_path / "hits.csv"
    main(["find", str(letters), "--terms", profile, "-f", "csv", "-o", str(report), "-q"])
    rows = list(csv.DictReader(io.StringIO(report.read_text(encoding="utf-8"))))
    assert sum(r["kind"] == "term" for r in rows) == 4
    assert any(r["severity"] == "high" for r in rows)


def test_find_with_a_phrase_on_the_command_line(engine, letters):
    assert main(["find", str(letters), "--term", "Adlerhorst", "-q"]) == 3
    assert main(["find", str(letters), "--term", "Falkenhorst", "-q"]) == 0


def test_find_needs_something_to_find(letters, capsys):
    assert main(["find", str(letters), "-q"]) == 2
    assert "--terms" in capsys.readouterr().err


def test_vs_takes_terms_too(engine, letters, capsys):
    code = main(["vs", str(letters / "a_brief.pdf"), "--term", "Projekt Adler", "-q"])
    assert code == 3  # a term found trips the default gate when terms are given
    assert "Projekt Adler" in capsys.readouterr().out


def test_shards_cover_every_file_exactly_once(engine, letters, tmp_path):
    seen: list[str] = []
    for k in (1, 2, 3):
        report = tmp_path / f"shard{k}.jsonl"
        args = ["find", str(letters), "--term", "Adler", "--shard", f"{k}/3"]
        main([*args, "-f", "jsonl", "-o", str(report), "-q"])
        seen += [json.loads(line)["source"] for line in report.read_text().splitlines()]
    pdfs = sorted(str(p) for p in letters.glob("*.pdf"))
    assert sorted(seen) == pdfs


def test_resume_skips_what_the_report_already_has(engine, letters, tmp_path, capsys):
    report = tmp_path / "hits.jsonl"
    brief = str(letters / "a_brief.pdf")
    args = ["find", "--term", "Adler", "-f", "jsonl", "-o", str(report), "-q"]
    main([*args[:1], brief, *args[1:]])
    main([*args[:1], str(letters), *args[1:], "--resume"])
    sources = [json.loads(line)["source"] for line in report.read_text().splitlines()]
    assert sources.count(brief) == 1
    assert len(sources) == 2


def test_resume_needs_a_jsonl_report(letters, capsys):
    assert main(["find", str(letters), "--term", "Adler", "--resume", "-q"]) == 2


def test_a_bad_shard_is_refused(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["find", "x.pdf", "--term", "Adler", "--shard", "4/3"])
    assert raised.value.code == 2


def _unreadable_pdf() -> bytes:
    """A PDF whose page has no area: the engine cannot read it."""
    good = _letter_pdf(["Adler"])
    return good.replace(b"/MediaBox [0 0 612 792]", b"/MediaBox [0 0 000 000]")


def test_an_unreadable_pdf_does_not_end_the_batch(engine, letters, tmp_path, capsys):
    bad = tmp_path / "leer.pdf"
    bad.write_bytes(_unreadable_pdf())
    brief = str(letters / "a_brief.pdf")
    # --pages takes the one-file-at-a-time path, which let the error escape.
    code = main(["find", str(bad), brief, "--term", "Projekt Adler", "--pages", "1"])
    out = capsys.readouterr().out
    assert code == 3
    assert "unreadable" in out and "a_brief.pdf" in out
    import ocrust

    results = [result for _, result in engine.scan_each([str(bad), brief], pages=[0])]
    assert isinstance(results[0], ocrust.OcrustError)
    assert not isinstance(results[1], Exception)


def test_resume_repairs_a_half_written_last_line(engine, letters, tmp_path):
    report = tmp_path / "hits.jsonl"
    args = ["find", str(letters), "--term", "Adler", "-f", "jsonl", "-o", str(report), "-q"]
    main(args)
    lines = report.read_text(encoding="utf-8").splitlines(keepends=True)
    report.write_text(lines[0] + lines[1][:25], encoding="utf-8")  # killed mid-record
    main([*args, "--resume"])
    records = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
    assert sorted(r["source"] for r in records) == sorted(str(p) for p in letters.glob("*.pdf"))


def test_a_resumed_run_answers_for_the_whole_report(engine, letters, tmp_path):
    report = tmp_path / "hits.jsonl"
    brief = str(letters / "a_brief.pdf")
    args = ["--term", "Projekt Adler", "-f", "jsonl", "-o", str(report), "-q"]
    assert main(["find", brief, *args]) == 3
    # Nothing left to read, and the report still holds a hit.
    assert main(["find", brief, *args, "--resume"]) == 3


def test_resume_leaves_a_file_that_is_not_a_report_alone(letters, tmp_path):
    other = tmp_path / "notizen.jsonl"
    other.write_text("Einkaufsliste: Milch", encoding="utf-8")
    args = ["find", str(letters), "--term", "Adler", "-f", "jsonl", "-o", str(other)]
    with pytest.raises(SystemExit) as raised:
        main([*args, "--resume", "-q"])
    assert raised.value.code == 2
    assert other.read_text(encoding="utf-8") == "Einkaufsliste: Milch"


def test_a_report_into_a_folder_is_refused_before_scanning(letters, tmp_path, capsys):
    code = main(["find", str(letters), "--term", "Adler", "-o", str(tmp_path), "-q"])
    assert code == 2
    assert "is a folder" in capsys.readouterr().err


def test_an_empty_shard_still_leaves_its_report(letters, tmp_path):
    only = tmp_path / "one"
    only.mkdir()
    (only / "a.pdf").write_bytes((letters / "a_brief.pdf").read_bytes())
    for fmt in ("json", "csv"):
        for k in (1, 2):
            report = tmp_path / f"s{k}.{fmt}"
            args = ["find", str(only), "--term", "Adler", "--shard", f"{k}/2"]
            main([*args, "-f", fmt, "-o", str(report), "-q"])
            assert report.exists(), (fmt, k)
    empty = [p for p in tmp_path.glob("s*.json") if json.loads(p.read_text())["files"] == []]
    assert len(empty) == 1
    assert any(p.read_text().strip() == ",".join(_CSV_FIELDS) for p in tmp_path.glob("s*.csv"))


def test_a_grade_gate_turns_markings_on(engine, tmp_path):
    marked = tmp_path / "geheim.pdf"
    marked.write_bytes(_letter_pdf(["GEHEIM", "", "Sehr geehrte Damen und Herren,"]))
    assert main(["find", str(marked), "--fail-on", "geheim", "-q"]) == 3


def test_find_trips_on_anything_by_default():
    from argparse import Namespace

    from ocrust.cli import _gates

    assert [g.spec for g in _gates(Namespace(fail_on=None), True, None, "find")] == ["any"]
    assert [g.spec for g in _gates(Namespace(fail_on=None), True, None, "vs")] == ["vs-nfd"]


@pytest.mark.parametrize(
    "option",
    [
        ["--workers", "-1"],
        ["--threads", "100000"],
        ["--max-pixels", "-1"],
        ["--dpi", "0"],
        ["--pages", ","],
    ],
)
def test_a_bad_number_is_a_bad_argument(letters, option):
    with pytest.raises(SystemExit) as raised:
        main(["find", str(letters), "--term", "Adler", *option, "-q"])
    assert raised.value.code == 2


def test_scan_each_takes_one_path_as_one_source(engine, letters):
    results = list(engine.scan_each(str(letters)))
    assert sorted(str(p) for p, _ in results) == sorted(str(p) for p in letters.glob("*.pdf"))


def test_scan_each_does_not_read_a_generator_ahead(engine, letters):
    def paths():
        yield letters / "a_brief.pdf"
        raise RuntimeError("the generator was read past the first chunk")

    first = next(engine.scan_each(paths(), chunk=1))
    assert first[0] == letters / "a_brief.pdf"


def test_a_severity_gate_needs_terms(letters, capsys):
    code = main(["vs", str(letters / "a_brief.pdf"), "--fail-on", "high", "-q"])
    assert code == 2
    assert "term severity" in capsys.readouterr().err


def test_the_report_may_not_be_an_input(letters, tmp_path, capsys):
    copy = tmp_path / "a.pdf"
    copy.write_bytes((letters / "a_brief.pdf").read_bytes())
    before = copy.read_bytes()
    assert main(["find", str(tmp_path), "--term", "Adler", "-f", "jsonl", "-o", str(copy)]) == 2
    assert main(["scan", str(copy), "-o", str(copy)]) == 2
    assert copy.read_bytes() == before


def test_resume_keeps_a_last_record_that_only_lacks_its_line_end(engine, letters, tmp_path):
    report = tmp_path / "hits.jsonl"
    brief = str(letters / "a_brief.pdf")
    args = ["--term", "Adler", "-f", "jsonl", "-o", str(report), "-q"]
    main(["find", brief, *args])
    report.write_text(report.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    main(["find", str(letters), *args, "--resume"])
    sources = [json.loads(line)["source"] for line in report.read_text().splitlines()]
    assert sources.count(brief) == 1 and len(sources) == 2


def test_a_report_is_written_through_a_link(tmp_path):
    from ocrust.cli import _write

    real = tmp_path / "real.txt"
    real.write_text("alt", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    _write(link, "neu")
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "neu"


def test_scan_each_takes_one_image_as_one_source(engine):
    np = pytest.importorskip("numpy")
    image = np.full((40, 60, 3), 255, dtype=np.uint8)
    assert len(list(engine.scan_each(image))) == 1


def test_scan_each_gives_each_source_back_as_given(engine, letters):
    given = [str(letters / "a_brief.pdf"), str(letters / "b_rechnung.pdf")]
    assert [source for source, _ in engine.scan_each(given)] == given


@pytest.mark.skipif(os.name != "posix", reason="/dev/stdout is POSIX")
def test_a_report_to_standard_output(engine, letters, capfd):
    brief = str(letters / "a_brief.pdf")
    assert main(["scan", brief, "-o", "/dev/stdout", "-q"]) == 0
    assert "Weissmueller" in capfd.readouterr().out
    main(["find", brief, "--term", "Adler", "-f", "jsonl", "-o", "/dev/stdout", "-q"])
    assert json.loads(capfd.readouterr().out.splitlines()[0])["source"] == brief


def test_a_hard_link_to_an_input_is_the_input(letters, tmp_path):
    copy = tmp_path / "in" / "a.pdf"
    copy.parent.mkdir()
    copy.write_bytes((letters / "a_brief.pdf").read_bytes())
    link = tmp_path / "report.jsonl"
    try:
        os.link(copy, link)
    except OSError:  # pragma: no cover - a file system without hard links
        pytest.skip("no hard links here")
    before = copy.read_bytes()
    args = ["find", str(copy.parent), "--term", "Adler", "-f", "jsonl", "-o", str(link), "-q"]
    assert main(args) == 2
    assert copy.read_bytes() == before


def test_an_array_of_paths_is_not_an_image():
    np = pytest.importorskip("numpy")
    from ocrust import _is_image

    assert _is_image(np.zeros((4, 4, 3), dtype=np.uint8))
    assert not _is_image(np.array([["a.pdf"], ["b.pdf"]]))


def test_a_name_that_lost_its_last_letter_is_another_name():
    for term, text in (("Frau Heinrichs", "Frau Heinrich"), ("Jürgen Peters", "Jürgen Peter")):
        assert not _doc([text]).find(terms.load([term])).hits, text


def test_a_regex_match_may_carry_a_footnote():
    profile = terms.parse({"term": [{"name": "K", "regex": r"KD-\d{6}"}]})
    assert [h.text for h in _doc(["Kunde KD-438300² bitte"]).find(profile)] == ["KD-438300"]


def test_only_real_streams_are_streams(tmp_path):
    from ocrust.cli import _special_file

    assert _special_file(Path("/dev/stdout"))
    assert not _special_file(tmp_path / "bericht.json")
    assert not _special_file(tmp_path)
    shm = Path("/dev/shm")
    if shm.is_dir():  # a folder under /dev is still a folder, its files files
        assert not _special_file(shm / "bericht.json")


def test_a_reader_that_leaves_does_not_change_the_verdict(engine, letters, monkeypatch):
    class Gone(io.StringIO):
        def write(self, text):
            raise BrokenPipeError

        def fileno(self):
            raise io.UnsupportedOperation

    monkeypatch.setattr("sys.stdout", Gone())
    brief = str(letters / "a_brief.pdf")
    for fmt in ("text", "json", "jsonl"):
        assert main(["find", brief, "--term", "Projekt Adler", "-f", fmt, "-q"]) == 3, fmt


def test_several_files_into_one_plain_file_is_refused(letters, tmp_path):
    target = tmp_path / "ausgabe"
    target.write_text("", encoding="utf-8")
    assert main(["scan", str(letters), "-o", str(target), "-q"]) == 2
