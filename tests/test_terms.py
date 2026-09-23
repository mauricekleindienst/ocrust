"""Search profiles: terms found however the scan broke them up, then `ocrust find`.

The first half builds pages by hand, which is what makes it possible to say
exactly how a word was broken; the second half renders PDFs and runs the
command.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from ocrust import Block, Box, Document, Line, Page, terms
from ocrust.cli import main

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


def test_every_hit_has_a_box_per_line():
    found = _doc(["Unterlagen zum Projekt", "Adler anbei."]).find(PROFILE)
    (hit,) = [h for h in found if h.term == "Projekt Adler"]
    assert len(hit.boxes) == 2
    assert hit.box.y0 <= hit.boxes[0].y0 and hit.box.y1 >= hit.boxes[1].y1
    assert json.loads(json.dumps(hit.to_dict()))["how"] == ["split"]


# ------------------------------------------------------------------ profiles


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
        ({"term": [{"name": "x"}]}, "needs `match`"),
        ({"term": [{"match": "x", "fuzzy": 7}]}, "fuzzy 7"),
        ({"term": [{"match": "x", "zones": ["margin"]}]}, "zone 'margin'"),
        ({"term": [{"match": "a"}, {"match": "a"}]}, "two terms are named"),
        ({"term": []}, "no terms"),
        ({"bogus": 1}, "unknown section"),
    ],
)
def test_a_bad_profile_says_what_is_wrong(data, message):
    with pytest.raises(terms.ProfileError, match=message):
        terms.parse(data, source="profil.toml")


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
