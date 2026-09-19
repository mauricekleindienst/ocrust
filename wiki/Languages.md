# Languages

The shipped model covers **26 languages completely** and `ocrust` knows **35**,
which is what lets it refuse the ones it cannot spell.

```console
$ ocrust languages
26 language(s) covered by the installed model (18709 characters):

  han         zh (Chinese (Simplified)), zh-hant (Chinese (Traditional))
  kana        ja (Japanese)
  latin       cs (Czech), da (Danish), de (German), en (English), es (Spanish),
              et (Estonian), fi (Finnish), fr (French), hr (Croatian),
              hu (Hungarian), is (Icelandic), it (Italian), lt (Lithuanian),
              lv (Latvian), nb (Norwegian), nl (Dutch), pl (Polish),
              pt (Portuguese), ro (Romanian), sk (Slovak), sl (Slovenian),
              sv (Swedish), tr (Turkish)

nearly covered (a few characters missing):
  vi (Vietnamese): 98%, missing ạ ả
```

## Declaring a language is a check, not a hint

Most OCR tools take `lang` as a switch between models. `ocrust` ships one
multilingual recognizer, so there is nothing to switch — but there is something
much more useful to do with the information.

A CTC recognizer can only ever emit characters from its own class list. Ask a
model that has no `ü` for German text and it does not fail: it returns
`Grusse` for `Grüße`, `Fuhrungsschiene` for `Führungsschiene`. The output looks
plausible, passes review, and is wrong. That is the worst failure mode a document
pipeline can have.

So `lang` is a contract checked when the engine is built:

```python
ocr = ocrust.Ocr(lang="de,fr")     # fine

ocrust.Ocr(lang="ru")
# OcrustError: invalid configuration: recognition model ppocrv6_rec.onnx does not
# cover the requested language(s): Russian (ru): cannot write А Б В Г Д Е Ж З И Й …
# hint: `ocrust languages` lists what this model covers; install a bundle for the
# cyrillic script, or drop the language requirement to accept partial results
```

```bash
ocrust scan scan.png --lang ru     # same, before a single page is read
```

Each language in the table carries the non-ASCII letters of its alphabet
(cross-checked against PaddleOCR's own dictionaries) or, for non-alphabetic
scripts, a set of probe characters. Coverage is the share of those the loaded
charset can emit. Punctuation is reported separately, because a missing
typographic quote does not corrupt a word.

Accepted forms: `"de"`, `"de,fr"`, `"de fr"`, `["de", "fr"]`, a language name
(`"German"`), and full tags where they mean something distinct — `zh-hant` is
Traditional Chinese, not `zh`.

## What is covered

| Script | Languages |
|---|---|
| Latin | English, German, French, Spanish, Italian, Portuguese, Dutch, Swedish, Danish, Norwegian, Finnish, Icelandic, Polish, Czech, Slovak, Hungarian, Romanian, Turkish, Croatian, Slovenian, Estonian, Latvian, Lithuanian |
| Kana + Han | Japanese |
| Han | Chinese (Simplified), Chinese (Traditional) |

## What is not

| Language | Coverage | Why |
|---|---:|---|
| Vietnamese | 98% | two tone-marked vowels (`ạ`, `ả`) are outside the charset |
| Greek | 70% | the plain letters are there; every accented vowel and the final sigma are not |
| Russian, Ukrainian, Bulgarian, Serbian | 0% | no Cyrillic in the PP-OCRv6 charset |
| Korean | 0% | no Hangul |
| Arabic | 0% | no Arabic script |
| Hindi | 0% | no Devanagari |

"Nearly covered" is still a failure: `lang="vi"` refuses with
`cannot write ạ ả`, because a tone mark silently dropped from a Vietnamese word
is exactly the error the check exists to prevent. Drop the requirement if you
want the partial result anyway.

These are known, named and checked — `ocrust.Ocr(lang="ru")` fails immediately
rather than returning nonsense. Adding a Cyrillic/Arabic/Devanagari bundle is a
matter of shipping a second recognizer, which is on the [Roadmap](Roadmap.md).

```python
ocrust.Ocr().partial_languages(min_ratio=0.5)
# ({'code': 'vi', 'name': 'Vietnamese', 'ratio': 0.976, 'missing': ['ạ', 'ả']},)
```

## Mixed-language documents

Nothing needs to be declared per page. One recognizer handles all 26 languages
at once, so a German invoice with French line items and an English footer is read
in one pass:

```text
RECHNUNG Nr. 2026-04-1187
Industriestraße 14, 85748 Garching
Français: déjà payé — Merci
Terms: net 30 days
```

Declaring `lang="de,fr,en"` does not change the recognition; it asserts that the
model can spell all three, which is what you want in CI.

## Accuracy per language

Measured on the generated corpus ([Evaluation](Evaluation.md)), clean renders at 200 dpi:

| language | mean CER on clean renders |
|---|---:|
| French | 0.000 |
| Polish | 0.000 |
| German | 0.001 |
| English | 0.003 |
| Czech | 0.007 |
| Japanese | 0.049 |

Latin-script European text is essentially solved at this resolution, diacritics
included. Japanese costs more because a single wrong kanji is a larger share of a
shorter line.

Greek is absent from this table on purpose. It used to sit at the bottom of it,
at 0.173, which is what a language looks like when a fifth of its characters
cannot be written at all: `Μηχανουργεία` came back as `Mηχανουργεα`, `Σύνολο` as
`Σúνoλo` with Latin lookalikes standing in for the accented Greek, and every
line still carried a confidence around 0.93. Measuring an accuracy for it implied
a choice a Greek reader does not have, so the claim is gone instead of qualified.
The pages are still in the corpus, scored in their own section of the
[evaluation report](../docs/evaluation-report.md).

## Bringing your own model

Any PP-OCR-compatible recognizer works, and the language check applies to it too:

```python
ocr = ocrust.Ocr(
    recognition_model="models/ppocrv5_rec_cyrillic.onnx",
    dictionary="models/cyrillic_dict.txt",
    lang="ru",           # now this passes
)
```

If the dictionary is embedded in the ONNX metadata, `dictionary` can be omitted.
See [Models](Models.md).
