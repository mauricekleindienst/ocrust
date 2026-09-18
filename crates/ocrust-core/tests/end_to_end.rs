//! End-to-end test against real ONNX models.
//!
//! The fixture is generated, not committed: a one-page PDF is written with
//! known text, rasterized through the normal ingest path and run through the
//! full engine. That exercises PDF rendering, detection, orientation, CTC
//! recognition, layout and export in one go.
//!
//! The test needs model files and an ONNX Runtime library, so it skips itself
//! when neither `OCRUST_MODELS_DIR` nor `~/.cache/ocrust/models` has any:
//!
//! ```text
//! OCRUST_MODELS_DIR=/path/to/ppocr ORT_DYLIB_PATH=/path/to/libonnxruntime.so \
//!     cargo test -p ocrust-core --test end_to_end -- --nocapture
//! ```

use ocrust_core::{Engine, EngineConfig, Source};

/// Builds a single-page PDF containing `lines` of Helvetica text.
fn text_pdf(lines: &[(&str, u32)]) -> Vec<u8> {
    let mut content = String::from("BT\n");
    for (text, size) in lines {
        content.push_str(&format!("/F1 {size} Tf 1 0 0 1 60 {{Y}} Tm ({text}) Tj\n"));
    }
    content.push_str("ET\n");
    // Lay the lines out from the top of the page downwards.
    let mut y = 700;
    let mut laid_out = String::new();
    for part in content.split("{Y}") {
        laid_out.push_str(part);
        if laid_out.len() < content.len() {
            laid_out.push_str(&y.to_string());
            y -= 70;
        }
    }

    let objects: Vec<String> = vec![
        "<< /Type /Catalog /Pages 2 0 R >>".into(),
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>".into(),
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] \
         /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
            .into(),
        format!(
            "<< /Length {} >>\nstream\n{}\nendstream",
            laid_out.len(),
            laid_out
        ),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".into(),
    ];

    let mut pdf = String::from("%PDF-1.4\n");
    let mut offsets = Vec::new();
    for (i, body) in objects.iter().enumerate() {
        offsets.push(pdf.len());
        pdf.push_str(&format!("{} 0 obj\n{}\nendobj\n", i + 1, body));
    }
    let xref_at = pdf.len();
    pdf.push_str(&format!(
        "xref\n0 {}\n0000000000 65535 f \n",
        objects.len() + 1
    ));
    for off in &offsets {
        pdf.push_str(&format!("{off:010} 00000 n \n"));
    }
    pdf.push_str(&format!(
        "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF\n",
        objects.len() + 1,
        xref_at
    ));
    pdf.into_bytes()
}

/// Builds an engine, or `None` when no models are installed.
fn engine() -> Option<Engine> {
    let mut config = EngineConfig::new();
    config.keep_page_images = true;
    if let Ok(dir) = std::env::var("OCRUST_MODELS_DIR") {
        config.models.directory = Some(dir.into());
    }
    match Engine::new(config) {
        Ok(engine) => Some(engine),
        Err(e) => {
            eprintln!("skipping end-to-end test: {e}");
            None
        }
    }
}

/// Normalizes OCR output so comparisons ignore spacing and case.
fn squash(s: &str) -> String {
    s.chars()
        .filter(|c| !c.is_whitespace())
        .flat_map(|c| c.to_lowercase())
        .collect()
}

#[test]
fn reads_generated_pdf_text() {
    let Some(engine) = engine() else { return };

    let pdf = text_pdf(&[
        ("INVOICE 2026-0042", 34),
        ("Total: 199.90 EUR", 28),
        ("Thank you for your business", 22),
    ]);
    let doc = engine
        .scan(&Source::bytes(pdf, "invoice.pdf"))
        .expect("scan should succeed");

    let text = doc.text();
    eprintln!("--- recognized ---\n{text}\n------------------");
    let flat = squash(&text);

    assert_eq!(doc.pages.len(), 1, "one page expected");
    for needle in ["invoice", "2026-0042", "199.90", "thankyou"] {
        assert!(
            flat.contains(&squash(needle)),
            "missing {needle:?} in recognized text:\n{text}"
        );
    }

    let confidence = doc.confidence().expect("confidence");
    assert!(confidence > 0.8, "mean confidence too low: {confidence}");
    assert!(
        doc.line_count() >= 3,
        "expected 3+ lines, got {}",
        doc.line_count()
    );
    assert!(
        doc.word_count() >= 8,
        "expected 8+ words, got {}",
        doc.word_count()
    );
}

#[test]
fn boxes_are_ordered_and_inside_the_page() {
    let Some(engine) = engine() else { return };

    let pdf = text_pdf(&[
        ("First line here", 30),
        ("Second line here", 30),
        ("Third line", 30),
    ]);
    let doc = engine.scan(&Source::bytes(pdf, "lines.pdf")).expect("scan");
    let page = &doc.pages[0];
    let lines: Vec<_> = page.lines().collect();
    assert!(lines.len() >= 3, "got {} lines", lines.len());

    // Reading order must run down the page.
    for pair in lines.windows(2) {
        assert!(
            pair[0].bbox.y0 <= pair[1].bbox.y0 + 5.0,
            "lines out of order: {:?} then {:?}",
            pair[0].text,
            pair[1].text
        );
    }
    // Every box must lie within the page, and word boxes within their line.
    for line in &lines {
        assert!(
            line.bbox.x0 >= 0.0 && line.bbox.y0 >= 0.0,
            "{:?}",
            line.bbox
        );
        assert!(line.bbox.x1 <= page.width as f32 + 1.0, "{:?}", line.bbox);
        assert!(line.bbox.y1 <= page.height as f32 + 1.0, "{:?}", line.bbox);
        for word in &line.words {
            assert!(
                word.bbox.x0 >= line.bbox.x0 - 2.0 && word.bbox.x1 <= line.bbox.x1 + 2.0,
                "word {:?} escapes line {:?}",
                word.bbox,
                line.bbox
            );
        }
    }
}

#[test]
fn recovers_from_rotation_and_skew() {
    let Some(engine) = engine() else { return };

    let pdf = text_pdf(&[("Rotated document test", 32), ("second line of text", 28)]);
    let pages = ocrust_core::ingest::load(
        &Source::bytes(pdf, "rot.pdf"),
        &ocrust_core::IngestConfig::default(),
    )
    .expect("render");
    let upright = pages[0].image.clone();

    // Rotate the page by 3 degrees: preprocessing must straighten it again.
    let skewed = imageproc::geometric_transformations::rotate_about_center_no_crop(
        &upright,
        3f32.to_radians(),
        imageproc::geometric_transformations::Interpolation::Bilinear,
        imageproc::geometric_transformations::Border::Constant(image::Rgb([255u8, 255, 255])),
    );

    let doc = engine.scan_image(skewed, "skewed").expect("scan");
    let flat = squash(&doc.text());
    assert!(
        flat.contains(&squash("Rotated document")),
        "skewed page not recognized: {}",
        doc.text()
    );
    assert!(
        doc.pages[0].rotation.abs() > 1.0,
        "deskew should report a correction, got {}",
        doc.pages[0].rotation
    );
}

#[test]
fn writes_a_searchable_pdf() {
    let Some(engine) = engine() else { return };
    use ocrust_core::export::pdf::{build, PdfOptions};

    let pdf = text_pdf(&[("Searchable output", 30)]);
    let doc = engine
        .scan(&Source::bytes(pdf, "searchable.pdf"))
        .expect("scan");
    let out = build(&doc, &PdfOptions::default()).expect("build pdf");

    let parsed = hayro::hayro_syntax::Pdf::new(std::sync::Arc::new(out.clone()))
        .expect("searchable PDF must parse");
    assert_eq!(parsed.pages().len(), 1);
    assert!(
        String::from_utf8_lossy(&out).contains("Searchable"),
        "text layer missing"
    );
}
