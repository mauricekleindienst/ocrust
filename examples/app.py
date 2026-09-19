#!/usr/bin/env python3
"""A small web application for trying ocrust out, with no dependencies at all.

    python examples/app.py                 # http://127.0.0.1:8765
    python examples/app.py --port 9000 --lang de,fr

Drop a scan, a photo or a PDF into the page: the text comes back with the boxes
drawn over the image, every export format, a search box that highlights hits,
and a download button for the searchable PDF.

Only the standard library is used — `http.server` and a hand-rolled multipart
reader — so the demo installs exactly nothing beyond `ocrust` itself. That is
the point: if this runs, the library runs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import ocrust

#: Exports offered in the result pane.
FORMATS = ("text", "markdown", "json", "hocr", "alto", "csv")

#: Upload ceiling. A 30-page 300 dpi scan is comfortably inside it.
MAX_UPLOAD = 64 * 1024 * 1024


class Engine:
    """The shared OCR engine, built on first use.

    Building it loads 31 MB of models, so it happens once and is guarded by a
    lock: several browser tabs must not race to build several engines.
    """

    def __init__(self, **kwargs: object) -> None:
        self._kwargs = kwargs
        self._lock = threading.Lock()
        self._engine: ocrust.Ocr | None = None

    def get(self) -> ocrust.Ocr:
        with self._lock:
            if self._engine is None:
                self._engine = ocrust.Ocr(**self._kwargs)  # type: ignore[arg-type]
            return self._engine


def parse_multipart(body: bytes, content_type: str) -> tuple[str, bytes]:
    """Returns `(filename, contents)` of the first file part in `body`.

    `cgi.FieldStorage` did this until Python 3.13 removed it, and pulling in a
    web framework for one upload would defeat the purpose of the example. A
    multipart body is a boundary, headers, a blank line and the bytes, so that is
    what this reads.
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type, re.IGNORECASE)
    if not match:
        raise ValueError("not a multipart upload")
    boundary = b"--" + match.group(1).encode()

    for part in body.split(boundary):
        head, _, data = part.partition(b"\r\n\r\n")
        if not data or b"filename=" not in head:
            continue
        name = re.search(rb'filename="([^"]*)"', head)
        filename = name.group(1).decode("utf-8", "replace") if name else "upload"
        # The part ends with the CRLF that precedes the next boundary.
        return filename, data[:-2] if data.endswith(b"\r\n") else data
    raise ValueError("no file in the upload")


def result_payload(doc: ocrust.Document, needle: str | None = None) -> dict[str, object]:
    """Everything the page needs about one scanned document."""
    pages = [
        {
            "index": page.index,
            "width": page.width,
            "height": page.height,
            "elapsed_ms": round(page.elapsed_ms, 1),
            "lines": [
                {
                    "text": line.text,
                    "box": list(line.box.as_tuple()),
                    "confidence": round(line.confidence, 4),
                    "angle": round(line.angle, 2),
                    "words": [{"text": w.text, "box": list(w.box.as_tuple())} for w in line.words],
                }
                for line in page.lines
            ],
        }
        for page in doc.pages
    ]
    payload: dict[str, object] = {
        "source": doc.source,
        "elapsed_ms": round(doc.elapsed_ms, 1),
        "confidence": doc.confidence,
        "pages": pages,
        "exports": {fmt: doc.render(fmt) for fmt in FORMATS},
    }
    if needle:
        payload["matches"] = [
            {"text": hit.text, "page": hit.page, "box": list(hit.box.as_tuple())}
            for hit in doc.search(needle)
        ]
    return payload


class Handler(BaseHTTPRequestHandler):
    """Four endpoints: the page, a scan, a searchable PDF, and the languages."""

    engine: Engine
    server_version = f"ocrust-demo/{ocrust.__version__}"

    def do_GET(self) -> None:  # noqa: N802  (http.server's spelling)
        if self.path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", PAGE.encode())
        elif self.path == "/languages":
            languages = self.engine.get().languages
            self._json({"languages": list(languages), "charset": self.engine.get().charset_size})
        elif self.path == "/health":
            self._json({"ok": True, "ocrust": ocrust.__version__})
        else:
            self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found\n")

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?")[0]
        if route not in ("/scan", "/pdf"):
            self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found\n")
            return
        try:
            filename, data = self._upload()
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        needle = None
        if "?" in self.path:
            needle = re.search(r"[?&]q=([^&]*)", self.path)
            needle = unquote(needle.group(1).replace("+", " ")) if needle else None

        try:
            if route == "/scan":
                started = time.perf_counter()
                doc = self.engine.get().scan(data, name=filename)
                payload = result_payload(doc, needle)
                payload["server_ms"] = round((time.perf_counter() - started) * 1000, 1)
                self._json(payload)
            else:
                self._searchable_pdf(filename, data)
        except (OSError, ValueError) as exc:
            # A broken upload is the user's problem, not a server error.
            self._json({"error": f"{filename}: {exc}"}, HTTPStatus.UNPROCESSABLE_ENTITY)
        except ocrust.OcrustError as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _searchable_pdf(self, filename: str, data: bytes) -> None:
        """A PDF keeps its own pages and gains a text layer; an image becomes one."""
        engine = self.engine.get()
        report: dict[str, int] | None = None
        if data[:5] == b"%PDF-":
            pdf, report = engine.ocr_pdf(data)
            self.log_message("text layer: %s", report)
        else:
            # `searchable_pdf` takes a path, so the upload gets a temporary one.
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / (Path(filename).name or "upload")
                path.write_bytes(data)
                pdf = engine.searchable_pdf(path)
        name = re.sub(r"[^\w.-]", "_", Path(filename).stem or "scan")
        extra = {"Content-Disposition": f'attachment; filename="{name}.ocr.pdf"'}
        if report is not None:
            # Pages that already carry text are skipped, and the page says so
            # rather than handing back a file that looks unchanged for no reason.
            extra["X-Ocrust-Report"] = json.dumps(report)
        self._send(HTTPStatus.OK, "application/pdf", pdf, extra=extra)

    def _upload(self) -> tuple[str, bytes]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty upload")
        if length > MAX_UPLOAD:
            raise ValueError(f"upload larger than {MAX_UPLOAD // 1024 // 1024} MB")
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/"):
            return parse_multipart(body, content_type)
        # A raw body is allowed too, which makes `curl --data-binary` work.
        return self.headers.get("X-Filename", "upload"), body

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._send(status, "application/json; charset=utf-8", body)

    def _send(
        self,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("  %s\n" % (format % args))


PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>ocrust</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #121214; --panel: #1c1c1f; --line: #252528;
    --ink: #e2e2e8; --muted: #888890; --accent: #ff5c00;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
           padding: 18px 20px; border-bottom: 1px solid var(--line); }
  h1 { font-size: 22px; margin: 0; letter-spacing: -0.5px; }
  h1 b { color: var(--accent); }
  .muted { color: var(--muted); font-size: 13px; }
  main { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 16px;
         align-items: start; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
           padding: 14px; min-width: 0; }
  #drop { border: 1.5px dashed #3a3a40; border-radius: 12px; padding: 28px 16px;
          text-align: center; cursor: pointer; }
  #drop.over { border-color: var(--accent); background: #17171a; }
  button { background: var(--accent); color: #fff; border: 0; border-radius: 8px;
           padding: 8px 14px; font-weight: 600; cursor: pointer; }
  button.ghost { background: #2a2a2f; color: var(--ink); }
  button:disabled { opacity: .5; cursor: default; }
  input[type=search] { background: #17171a; border: 1px solid var(--line); color: var(--ink);
                       border-radius: 8px; padding: 8px 10px; width: 100%; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
  .stage { position: relative; display: inline-block; max-width: 100%; }
  .stage img { display: block; max-width: 100%; height: auto; border-radius: 8px; }
  .stage svg { position: absolute; inset: 0; width: 100%; height: 100%; }
  rect.line { fill: rgba(255,92,0,.10); stroke: var(--accent); stroke-width: 1.2; }
  rect.hit { fill: rgba(255,214,0,.28); stroke: #ffd600; stroke-width: 1.6; }
  embed { width: 100%; height: 70vh; border-radius: 8px; background: #fff; }
  pre { white-space: pre-wrap; word-break: break-word; margin: 0; max-height: 62vh;
        overflow: auto; font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }
  .tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
  .tabs button { background: #2a2a2f; color: var(--muted); font-weight: 500; padding: 5px 10px; }
  .tabs button[aria-selected=true] { background: var(--accent); color: #fff; }
  table { border-collapse: collapse; font-size: 13px; width: 100%; }
  td { padding: 2px 8px 2px 0; }
  td.n { text-align: right; font-variant-numeric: tabular-nums; }
</style>

<header>
  <h1>ocr<b>ust</b></h1>
  <span class="muted" id="about">document OCR — drop a scan, a photo or a PDF</span>
  <span class="muted" style="margin-left:auto" id="langs"></span>
</header>

<main>
  <section class="panel">
    <div id="drop">
      <p><strong>Drop a file here</strong> or
         <button class="ghost" id="pick">choose one</button></p>
      <p class="muted">PNG, JPEG, WebP, BMP, GIF, TIFF (multi-page), PDF — up to 64 MB</p>
      <input type="file" id="file" hidden
             accept="image/*,.pdf,.tif,.tiff,.webp,.bmp,.gif,.ppm,.pgm,.pbm">
    </div>
    <div class="row" style="margin-top:12px">
      <input type="search" id="q" placeholder="Search the result (highlights the boxes)">
    </div>
    <div class="row">
      <button id="pdfbtn" disabled>Download searchable PDF</button>
      <label class="muted"><input type="checkbox" id="boxes" checked> show boxes</label>
    </div>
    <div id="stage"></div>
  </section>

  <section class="panel">
    <div class="tabs" id="tabs"></div>
    <div id="stats" class="muted" style="margin-bottom:10px"></div>
    <pre id="out">Nothing scanned yet.</pre>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
let current = null, upload = null, format = "text";

fetch("/languages").then(r => r.json()).then(d => {
  $("langs").textContent = `${d.languages.length} languages · ${d.charset} characters`;
}).catch(() => {});

for (const fmt of ["text", "markdown", "json", "hocr", "alto", "csv"]) {
  const b = document.createElement("button");
  b.textContent = fmt;
  b.setAttribute("aria-selected", String(fmt === format));
  b.onclick = () => { format = fmt; render(); };
  $("tabs").append(b);
}

$("pick").onclick = () => $("file").click();
$("drop").onclick = (e) => { if (e.target.id === "drop") $("file").click(); };
$("file").onchange = () => $("file").files[0] && scan($("file").files[0]);
$("boxes").onchange = render;
$("q").oninput = debounce(() => upload && scan(upload, true), 250);
$("pdfbtn").onclick = downloadPdf;

for (const type of ["dragenter", "dragover"]) {
  $("drop").addEventListener(type, (e) => { e.preventDefault(); $("drop").classList.add("over"); });
}
for (const type of ["dragleave", "drop"]) {
  $("drop").addEventListener(type, (e) => {
    e.preventDefault(); $("drop").classList.remove("over");
  });
}
$("drop").addEventListener("drop", (e) => {
  const file = e.dataTransfer.files[0];
  if (file) scan(file);
});

function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

async function scan(file, quiet) {
  upload = file;
  if (!quiet) { $("out").textContent = `Scanning ${file.name} …`; $("stats").textContent = ""; }
  const body = new FormData();
  body.append("file", file, file.name);
  const q = $("q").value.trim();
  const started = performance.now();
  const res = await fetch("/scan" + (q ? "?q=" + encodeURIComponent(q) : ""), {
    method: "POST", body,
  });
  const data = await res.json();
  if (data.error) { $("out").textContent = "Error: " + data.error; return; }
  current = data;
  current.round_trip = Math.round(performance.now() - started);
  $("pdfbtn").disabled = false;
  render();
}

function render() {
  if (!current) return;
  for (const b of $("tabs").children) {
    b.setAttribute("aria-selected", String(b.textContent === format));
  }
  $("out").textContent = current.exports[format] || "(empty)";

  const pages = current.pages.length;
  const lines = current.pages.reduce((n, p) => n + p.lines.length, 0);
  const conf = current.confidence == null ? "—" : (current.confidence * 100).toFixed(1) + "%";
  const hits = current.matches ? current.matches.length : null;
  $("stats").innerHTML = `<table><tr>
      <td>pages</td><td class="n">${pages}</td>
      <td>lines</td><td class="n">${lines}</td>
      <td>confidence</td><td class="n">${conf}</td>
      <td>engine</td><td class="n">${current.elapsed_ms} ms</td>
      <td>round trip</td><td class="n">${current.round_trip} ms</td>
      ${hits === null ? "" : `<td>hits</td><td class="n">${hits}</td>`}
    </tr></table>`;

  drawStage();
}

function drawStage() {
  const stage = $("stage");
  stage.innerHTML = "";
  if (!upload) return;

  if (upload.type === "application/pdf" || /\\.pdf$/i.test(upload.name)) {
    // The browser renders PDFs itself, and the searchable copy is selectable.
    const embed = document.createElement("embed");
    embed.type = "application/pdf";
    embed.src = URL.createObjectURL(upload);
    stage.append(embed);
    return;
  }

  const page = current && current.pages[0];
  const wrap = document.createElement("div");
  wrap.className = "stage";
  const img = document.createElement("img");
  img.src = URL.createObjectURL(upload);
  wrap.append(img);

  if (page && $("boxes").checked) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${page.width} ${page.height}`);
    svg.setAttribute("preserveAspectRatio", "none");
    const hits = new Set((current.matches || [])
      .filter(m => m.page === page.index).map(m => m.box.join(",")));
    for (const line of page.lines) {
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      const [x0, y0, x1, y1] = line.box;
      r.setAttribute("x", x0); r.setAttribute("y", y0);
      r.setAttribute("width", Math.max(1, x1 - x0));
      r.setAttribute("height", Math.max(1, y1 - y0));
      r.setAttribute("class", "line");
      svg.append(r);
    }
    for (const m of current.matches || []) {
      if (m.page !== page.index) continue;
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      const [x0, y0, x1, y1] = m.box;
      r.setAttribute("x", x0); r.setAttribute("y", y0);
      r.setAttribute("width", Math.max(1, x1 - x0));
      r.setAttribute("height", Math.max(1, y1 - y0));
      r.setAttribute("class", "hit");
      svg.append(r);
    }
    wrap.append(svg);
  }
  stage.append(wrap);
}

async function downloadPdf() {
  if (!upload) return;
  $("pdfbtn").disabled = true;
  $("pdfbtn").textContent = "Building …";
  const body = new FormData();
  body.append("file", upload, upload.name);
  const res = await fetch("/pdf", { method: "POST", body });
  if (res.ok) {
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = upload.name.replace(/\\.[^.]+$/, "") + ".ocr.pdf";
    a.click();
    const report = res.headers.get("X-Ocrust-Report");
    if (report) {
      const r = JSON.parse(report);
      if (!r.pages_with_layer) {
        alert(`Nothing to add: all ${r.pages} page(s) already contain text.`);
      }
    }
  } else {
    const data = await res.json().catch(() => ({ error: res.statusText }));
    alert("Could not build the PDF: " + data.error);
  }
  $("pdfbtn").textContent = "Download searchable PDF";
  $("pdfbtn").disabled = false;
}
</script>
</html>
"""


def serve(host: str, port: int, **engine_kwargs: object) -> ThreadingHTTPServer:
    """Starts the server and returns it, without blocking."""
    handler = type("BoundHandler", (Handler,), {"engine": Engine(**engine_kwargs)})
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--lang", help="languages to require, e.g. de,fr")
    parser.add_argument("--dpi", type=float, help="PDF rasterization DPI (default 200)")
    parser.add_argument("--models", help="directory holding the ONNX models")
    parser.add_argument("--workers", type=int, help="pages scanned in parallel")
    args = parser.parse_args(argv)

    try:
        server = serve(
            args.host,
            args.port,
            lang=args.lang,
            pdf_dpi=args.dpi,
            models_dir=args.models,
            page_workers=args.workers,
        )
    except OSError as exc:
        print(f"cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1

    print(f"ocrust {ocrust.__version__} — http://{args.host}:{server.server_port}")
    print("Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
