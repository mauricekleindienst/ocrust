# Models

`ocrust` ships one bundle: **PP-OCRv6 mobile**, three ONNX files, 31 MB total.

| File | Role | Size |
|---|---|---:|
| `ppocrv6_det.onnx` | text detection (DB) | 9.5 MB |
| `ppocrv6_rec.onnx` | text recognition (CTC), 18 708 classes | 21 MB |
| `ppocr_cls.onnx` | 180° text-line orientation | 0.6 MB |

The recognizer's character list is embedded in the ONNX metadata, so there is no
dictionary file to keep in sync — and `ocrust languages` can report exactly what
the loaded model is able to spell.

## Provenance and license

The models are PP-OCR releases by the PaddleOCR project, **Apache-2.0** — the
same license as this repository. They were taken from the PyPI distribution
`rapidocr` 3.9.2, which redistributes the PaddleOCR ONNX exports unmodified:

| File here | Upstream name |
|---|---|
| `ppocrv6_det.onnx` | `PP-OCRv6_det_small.onnx` |
| `ppocrv6_rec.onnx` | `PP-OCRv6_rec_small.onnx` |
| `ppocr_cls.onnx` | `ch_ppocr_mobile_v2.0_cls_mobile.onnx` |

The files are byte-identical to that distribution; every SHA-256 is recorded in
`models/ppocrv6.json`.

Why v6 and not v5: the v5 charset (18 383 classes) is missing characters several
European languages need, so v5 cannot spell them. v6 (18 708) covers 26
languages completely — Greek and Vietnamese are not among them, for the reason
given in [Languages](Languages.md).

## One source for everything

This is a design rule, not a coincidence:

- Models live **in the repository**, under `models/ppocrv6/`.
- `pip install "ocrust[models]"` installs them as the `ocrust-models` wheel, so a
  fresh install downloads nothing at run time.
- Without that extra, `ocrust install-models` fetches them from
  `raw.githubusercontent.com` using the manifest — same repository, same files,
  checksum-verified.
- Nothing ever contacts Hugging Face, ModelScope or `bcebos.com`.

A proxy that allows PyPI and GitHub is sufficient. That is the whole point: the
usual OCR stack fails in corporate networks because its models live on hosts the
proxy has never heard of.

### The manifest

```json
{
  "name": "ppocrv6",
  "files": [
    { "name": "ppocrv6_det.onnx",
      "urls": ["https://raw.githubusercontent.com/mauricekleindienst/ocrust/{ref}/models/ppocrv6/ppocrv6_det.onnx"],
      "sha256": "090f04ab…", "size": 9929594 }
  ]
}
```

`{ref}` is substituted with `OCRUST_MODEL_REF` (default `main`), so an
installation can be pinned to a tag. Several URLs per file are tried in order,
which is how an internal mirror is added.

```bash
OCRUST_MODEL_REF=v0.1.0 ocrust install-models
```

Downloads are written to a `.part` file and renamed, so an interrupted run can
never leave half a model behind, and every file is checksum-verified before it is
accepted. Re-running `install-models` over an intact cache is a no-op.

### Private repositories and Enterprise

While the repository is private, anonymous `raw.githubusercontent.com` requests
return 404. Set a token and the downloader authenticates:

```bash
export OCRUST_GITHUB_TOKEN=ghp_…      # or GITHUB_TOKEN, which CI already sets
ocrust install-models
```

The token is sent **only** to `github.com`, `raw.githubusercontent.com`,
`api.github.com` and `codeload.github.com` over HTTPS — never to a mirror URL
from a manifest, which is unit-tested. The `ocrust-models` wheel needs no token
at all, so `pip install "ocrust[models]"` stays the simplest path.

## Where models are looked for

In order:

1. `Ocr(models_dir=…)`, or the individual `detection_model=` / `recognition_model=`
   / `orientation_model=` / `dictionary=` overrides
2. `OCRUST_MODELS_DIR`
3. the `ocrust-models` wheel, if installed
4. the per-user cache: `OCRUST_HOME`, else the OS cache directory
   (`~/.cache/ocrust/models` on Linux)

```bash
ocrust models     # what would be used, right now
```

```console
detection    …/ocrust_models/models/ppocrv6_det.onnx
recognition  …/ocrust_models/models/ppocrv6_rec.onnx
orientation  …/ocrust_models/models/ppocr_cls.onnx
dictionary   -
```

File names are recognized in both `ocrust` and PaddleOCR/RapidOCR spellings
(`PP-OCRv5_mobile_det_infer.onnx`, `ch_ppocr_mobile_v2.0_cls_infer.onnx`, …), so
a bundle downloaded from upstream works unrenamed.

## Bringing your own

Any PP-OCR-compatible export works — detection is a DB map, recognition is CTC
logits over a class list:

```python
ocr = ocrust.Ocr(
    detection_model="models/PP-OCRv5_server_det.onnx",
    recognition_model="models/ppocrv5_rec_cyrillic.onnx",
    dictionary="models/cyrillic_dict.txt",      # only if not embedded
    lang="ru",                                  # checked against the new charset
)
```

```bash
ocrust scan page.png --models /opt/my-bundle
```

A server-class detection model is larger and slower than the mobile default, and
usually better on small text. How much better on *your* documents is a question
for `scripts/evaluate_corpus.py --models`, not for a table in a wiki.

If the recognizer has no embedded charset, `dictionary` is required and `ocrust`
says so instead of guessing. The number of classes is probed from the model, so a
dictionary of the wrong length is rejected immediately rather than producing
shifted characters.

## Building the models wheel

```bash
python scripts/build_models_wheel.py --models-dir models/ppocrv6 -o dist
pip install --find-links dist ocrust-models
```

Useful for an internal index: publish `ocrust-models` once and every machine
installs offline.

## What is not included

- Cyrillic, Korean, Arabic and Devanagari recognizers (see [Roadmap](Roadmap.md))
- a layout/table-structure model — reading order is geometric, not learned
- a document-orientation model beyond the 180° line classifier; page rotation is
  decided from box geometry
