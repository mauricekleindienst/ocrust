//! Writing a searchable PDF: the page image with an invisible text layer on
//! top, so the result looks exactly like the scan but can be selected,
//! searched and indexed.
//!
//! The writer is deliberately dependency-free — it emits the handful of PDF
//! objects this job needs instead of pulling in a full PDF toolkit.

use std::io::Cursor;

use image::RgbImage;

use super::cidfont;
use crate::doc::Document;
use crate::error::{Error, Result};

/// Options for [`build`].
#[derive(Debug, Clone)]
pub struct PdfOptions {
    /// Resolution the page images are assumed to have. Decides the physical
    /// page size: `pixels / dpi` inches.
    pub dpi: f32,
    /// JPEG quality for the page images, 1..=100.
    pub jpeg_quality: u8,
    /// Emit the invisible text layer. Turning this off produces an image-only PDF.
    pub text_layer: bool,
}

impl Default for PdfOptions {
    fn default() -> Self {
        Self {
            dpi: 200.0,
            jpeg_quality: 80,
            text_layer: true,
        }
    }
}

/// Builds a searchable PDF from a scanned document.
///
/// Every page needs its image: scan with `EngineConfig::keep_page_images`
/// enabled, or pass the images explicitly with [`build_with_images`].
pub fn build(doc: &Document, opts: &PdfOptions) -> Result<Vec<u8>> {
    let mut images = Vec::with_capacity(doc.pages.len());
    for page in &doc.pages {
        let img = page.image.as_ref().ok_or_else(|| {
            Error::config(
                "searchable PDF needs the page images: scan with keep_page_images enabled",
            )
        })?;
        images.push(img.as_ref().clone());
    }
    build_with_images(doc, &images, opts)
}

/// Same as [`build`], with the page images supplied separately.
pub fn build_with_images(
    doc: &Document,
    images: &[RgbImage],
    opts: &PdfOptions,
) -> Result<Vec<u8>> {
    if images.len() != doc.pages.len() {
        return Err(Error::config(format!(
            "got {} image(s) for {} page(s)",
            images.len(),
            doc.pages.len()
        )));
    }
    let dpi = if opts.dpi > 1.0 { opts.dpi } else { 200.0 };
    let px_to_pt = 72.0 / dpi;

    let mut pdf = PdfWriter::new();
    // Object 1 is the catalog, object 2 the page tree; page objects follow.
    let catalog = pdf.reserve();
    let page_tree = pdf.reserve();
    let font = pdf.reserve();
    // The Unicode font is written only when some line needs it, so a Western
    // document keeps exactly the objects it had before.
    let unicode_font = doc
        .pages
        .iter()
        .flat_map(|page| page.lines())
        .any(|line| cidfont::needs_unicode(&line.text))
        .then(|| write_unicode_font(&mut pdf));

    let mut page_ids = Vec::with_capacity(doc.pages.len());
    for (page, image) in doc.pages.iter().zip(images) {
        let w_pt = image.width() as f32 * px_to_pt;
        let h_pt = image.height() as f32 * px_to_pt;

        let jpeg = encode_jpeg(image, opts.jpeg_quality)?;
        let image_id = pdf.stream_object(
            &format!(
                "<< /Type /XObject /Subtype /Image /Width {} /Height {} \
                 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {} >>",
                image.width(),
                image.height(),
                jpeg.len()
            ),
            &jpeg,
        );

        let mut content = String::new();
        // Draw the scan so the PDF looks like the original.
        content.push_str(&format!("q\n{w_pt:.2} 0 0 {h_pt:.2} 0 0 cm\n/Im0 Do\nQ\n"));
        if opts.text_layer {
            content.push_str(&text_layer(page, px_to_pt, h_pt, unicode_font.is_some()));
        }
        let content_id = pdf.stream_object(
            &format!("<< /Length {} >>", content.len()),
            content.as_bytes(),
        );

        let fonts = match unicode_font {
            Some(id) => format!("/F1 {font} 0 R /{} {id} 0 R", cidfont::UNICODE_FONT_NAME),
            None => format!("/F1 {font} 0 R"),
        };
        let page_id = pdf.object(&format!(
            "<< /Type /Page /Parent {page_tree} 0 R /MediaBox [0 0 {w_pt:.2} {h_pt:.2}] \
             /Resources << /XObject << /Im0 {image_id} 0 R >> /Font << {fonts} >> >> \
             /Contents {content_id} 0 R >>"
        ));
        page_ids.push(page_id);
    }

    let kids = page_ids
        .iter()
        .map(|id| format!("{id} 0 R"))
        .collect::<Vec<_>>()
        .join(" ");
    pdf.fill(
        page_tree,
        &format!(
            "<< /Type /Pages /Kids [{kids}] /Count {} >>",
            page_ids.len()
        ),
    );
    pdf.fill(
        font,
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    );
    pdf.fill(
        catalog,
        &format!("<< /Type /Catalog /Pages {page_tree} 0 R >>"),
    );

    Ok(pdf.finish(catalog))
}

/// Emits invisible (`3 Tr`) text positioned over each recognized line.
///
/// `unicode_font` says whether the Unicode font was added to the page's
/// resources; without it, text outside WinAnsi still degrades to `?`.
fn text_layer(
    page: &crate::doc::Page,
    px_to_pt: f32,
    page_h_pt: f32,
    unicode_font: bool,
) -> String {
    let mut out = String::from("BT\n3 Tr\n");
    for line in page.lines() {
        if line.text.trim().is_empty() {
            continue;
        }
        let size = (line.bbox.height() * px_to_pt).max(1.0);
        let x = line.bbox.x0 * px_to_pt;
        // PDF's origin is the bottom-left corner; the baseline sits a little
        // above the box bottom.
        let y = page_h_pt - line.bbox.y1 * px_to_pt + size * 0.18;
        let target_w = line.bbox.width() * px_to_pt;
        let (font, show, em) = show_text(&line.text, unicode_font);
        let scale = horizontal_scale_em(&line.text, size, target_w, em);

        out.push_str(&format!(
            "/{font} {size:.2} Tf\n{scale:.1} Tz\n1 0 0 1 {x:.2} {y:.2} Tm\n{show} Tj\n"
        ));
    }
    out.push_str("ET\n");
    out
}

/// Picks the font for one line and encodes the text for it.
///
/// Returns the resource name, the operand for `Tj` and how wide a character is
/// in that font, which is what stretches the line onto its box.
pub(crate) fn show_text(text: &str, unicode_font: bool) -> (&'static str, String, f32) {
    if unicode_font && cidfont::needs_unicode(text) {
        (
            cidfont::UNICODE_FONT_NAME,
            format!("<{}>", cidfont::utf16_hex(text)),
            cidfont::UNICODE_EM_PER_CHAR,
        )
    } else {
        ("F1", format!("({})", escape_pdf_text(text)), 0.5)
    }
}

/// Writes the Type0 font, its descendant and the `ToUnicode` map.
fn write_unicode_font(pdf: &mut PdfWriter) -> usize {
    let cmap = cidfont::to_unicode_cmap();
    let to_unicode = pdf.stream_object(&format!("<< /Length {} >>", cmap.len()), cmap.as_bytes());
    let descriptor = pdf.object(&format!(
        "<< /Type /FontDescriptor /FontName /{} /Flags {} \
         /FontBBox [{} {} {} {}] /ItalicAngle 0 /Ascent {} /Descent {} /CapHeight {} /StemV {} >>",
        cidfont::UNICODE_BASE_FONT,
        cidfont::DESCRIPTOR_FLAGS,
        cidfont::DESCRIPTOR_BBOX[0],
        cidfont::DESCRIPTOR_BBOX[1],
        cidfont::DESCRIPTOR_BBOX[2],
        cidfont::DESCRIPTOR_BBOX[3],
        cidfont::DESCRIPTOR_ASCENT,
        cidfont::DESCRIPTOR_DESCENT,
        cidfont::DESCRIPTOR_CAP_HEIGHT,
        cidfont::DESCRIPTOR_STEM_V,
    ));
    let descendant = pdf.object(&format!(
        "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{} \
         /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> \
         /FontDescriptor {descriptor} 0 R /DW 1000 /CIDToGIDMap /Identity >>",
        cidfont::UNICODE_BASE_FONT
    ));
    pdf.object(&format!(
        "<< /Type /Font /Subtype /Type0 /BaseFont /{} /Encoding /Identity-H \
         /DescendantFonts [{descendant} 0 R] /ToUnicode {to_unicode} 0 R >>",
        cidfont::UNICODE_BASE_FONT
    ))
}

/// Horizontal scaling (`Tz`) that stretches the text onto the detected box.
///
/// Helvetica averages roughly `0.5 em` per character and the Unicode font
/// declares a full em, which is close enough for a layer nobody sees but
/// everybody searches.
pub(crate) fn horizontal_scale_em(text: &str, size: f32, target_w: f32, em: f32) -> f32 {
    let chars = text.chars().count().max(1) as f32;
    let natural = chars * size * em;
    if natural <= 0.0 || target_w <= 0.0 {
        return 100.0;
    }
    (target_w / natural * 100.0).clamp(10.0, 400.0)
}

/// Characters WinAnsi (CP1252) places in `0x80..=0x9F`, where it differs from
/// Latin-1. Everything else in `0xA0..=0xFF` is identical to Latin-1.
const WINANSI_HIGH: [(u8, char); 27] = [
    (0x80, '\u{20AC}'), // €
    (0x82, '\u{201A}'),
    (0x83, '\u{0192}'),
    (0x84, '\u{201E}'), // „
    (0x85, '\u{2026}'), // …
    (0x86, '\u{2020}'),
    (0x87, '\u{2021}'),
    (0x88, '\u{02C6}'),
    (0x89, '\u{2030}'),
    (0x8A, '\u{0160}'),
    (0x8B, '\u{2039}'),
    (0x8C, '\u{0152}'), // Œ
    (0x8E, '\u{017D}'),
    (0x91, '\u{2018}'),
    (0x92, '\u{2019}'),
    (0x93, '\u{201C}'), // “
    (0x94, '\u{201D}'), // ”
    (0x95, '\u{2022}'),
    (0x96, '\u{2013}'), // –
    (0x97, '\u{2014}'), // —
    (0x98, '\u{02DC}'),
    (0x99, '\u{2122}'),
    (0x9A, '\u{0161}'),
    (0x9B, '\u{203A}'),
    (0x9C, '\u{0153}'), // œ
    (0x9E, '\u{017E}'),
    (0x9F, '\u{0178}'),
];

/// Encodes `text` as a WinAnsi (CP1252) PDF string literal.
///
/// Returns the escaped literal and how many characters had to be replaced.
/// WinAnsi covers Western European text — German umlauts, French accents, the
/// euro sign — which is what a base-14 font can show; scripts beyond it need an
/// embedded font and are reported through the replacement count.
pub fn winansi_literal(text: &str) -> (String, usize) {
    let mut out = String::with_capacity(text.len() + 8);
    let mut replaced = 0usize;
    for c in text.chars() {
        let byte = match c {
            '(' => {
                out.push_str("\\(");
                continue;
            }
            ')' => {
                out.push_str("\\)");
                continue;
            }
            '\\' => {
                out.push_str("\\\\");
                continue;
            }
            c => match winansi_byte(c) {
                Some(byte) => byte,
                None => {
                    replaced += 1;
                    b'?'
                }
            },
        };
        // Bytes above 0x7F are written as octal escapes so the literal stays
        // 7-bit clean and cannot be mangled by tooling.
        if byte < 0x80 {
            out.push(byte as char);
        } else {
            out.push_str(&format!("\\{byte:03o}"));
        }
    }
    (out, replaced)
}

/// The WinAnsi (CP1252) byte for a character, if it has one.
///
/// Control characters become a space: they carry no text and would otherwise be
/// counted as losses. Everything this returns `None` for needs the Unicode font
/// in [`super::cidfont`].
pub(crate) fn winansi_byte(c: char) -> Option<u8> {
    let code = c as u32;
    if code < 0x20 {
        return Some(b' ');
    }
    if code < 0x80 {
        return Some(code as u8);
    }
    if let Some((byte, _)) = WINANSI_HIGH.iter().find(|(_, mapped)| *mapped == c) {
        return Some(*byte);
    }
    (0xA0..=0xFF).contains(&code).then_some(code as u8)
}

/// Escapes a string for a PDF literal, dropping characters WinAnsi cannot hold.
fn escape_pdf_text(text: &str) -> String {
    winansi_literal(text).0
}

fn encode_jpeg(image: &RgbImage, quality: u8) -> Result<Vec<u8>> {
    let mut buf = Vec::new();
    let encoder = image::codecs::jpeg::JpegEncoder::new_with_quality(
        Cursor::new(&mut buf),
        quality.clamp(1, 100),
    );
    use image::ImageEncoder;
    encoder.write_image(
        image.as_raw(),
        image.width(),
        image.height(),
        image::ExtendedColorType::Rgb8,
    )?;
    Ok(buf)
}

/// Minimal PDF object writer with a cross-reference table.
struct PdfWriter {
    body: Vec<u8>,
    /// Byte offset of each object, indexed by `id - 1`.
    offsets: Vec<Option<usize>>,
    /// Objects reserved but not yet written, kept so `fill` can append them.
    pending: Vec<(usize, Option<Vec<u8>>)>,
}

impl PdfWriter {
    fn new() -> Self {
        Self {
            body: b"%PDF-1.7\n%\xE2\xE3\xCF\xD3\n".to_vec(),
            offsets: Vec::new(),
            pending: Vec::new(),
        }
    }

    /// Allocates an object id to be filled in later.
    fn reserve(&mut self) -> usize {
        self.offsets.push(None);
        let id = self.offsets.len();
        self.pending.push((id, None));
        id
    }

    /// Writes a dictionary object and returns its id.
    fn object(&mut self, dict: &str) -> usize {
        self.offsets.push(None);
        let id = self.offsets.len();
        self.write_object(id, dict.as_bytes(), None);
        id
    }

    /// Writes a stream object and returns its id.
    fn stream_object(&mut self, dict: &str, data: &[u8]) -> usize {
        self.offsets.push(None);
        let id = self.offsets.len();
        self.write_object(id, dict.as_bytes(), Some(data));
        id
    }

    /// Writes a previously reserved object.
    fn fill(&mut self, id: usize, dict: &str) {
        self.write_object(id, dict.as_bytes(), None);
        self.pending.retain(|(pending_id, _)| *pending_id != id);
    }

    fn write_object(&mut self, id: usize, dict: &[u8], stream: Option<&[u8]>) {
        let offset = self.body.len();
        self.offsets[id - 1] = Some(offset);
        self.body
            .extend_from_slice(format!("{id} 0 obj\n").as_bytes());
        self.body.extend_from_slice(dict);
        if let Some(data) = stream {
            self.body.extend_from_slice(b"\nstream\n");
            self.body.extend_from_slice(data);
            self.body.extend_from_slice(b"\nendstream");
        }
        self.body.extend_from_slice(b"\nendobj\n");
    }

    fn finish(mut self, root: usize) -> Vec<u8> {
        // Any object still unwritten would break the xref table: emit a stub.
        let unwritten: Vec<usize> = self
            .offsets
            .iter()
            .enumerate()
            .filter(|(_, o)| o.is_none())
            .map(|(i, _)| i + 1)
            .collect();
        for id in unwritten {
            self.write_object(id, b"<< >>", None);
        }

        let xref_at = self.body.len();
        let count = self.offsets.len() + 1;
        self.body
            .extend_from_slice(format!("xref\n0 {count}\n").as_bytes());
        self.body.extend_from_slice(b"0000000000 65535 f \n");
        for offset in &self.offsets {
            let off = offset.unwrap_or(0);
            self.body
                .extend_from_slice(format!("{off:010} 00000 n \n").as_bytes());
        }
        self.body.extend_from_slice(
            format!("trailer\n<< /Size {count} /Root {root} 0 R >>\nstartxref\n{xref_at}\n%%EOF\n")
                .as_bytes(),
        );
        self.body
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::doc::{Block, BlockKind, Line, Page, PageOrigin};
    use crate::geom::{Quad, Rect};

    fn line(text: &str, r: Rect) -> Line {
        Line {
            text: text.into(),
            confidence: 0.9,
            quad: Quad::from_rect(r),
            bbox: r,
            angle: 0.0,
            det_score: 0.9,
            words: Vec::new(),
        }
    }

    fn scanned_doc(pages: usize) -> (Document, Vec<RgbImage>) {
        let mut doc = Document::new("scan.png");
        let mut images = Vec::new();
        for i in 0..pages {
            doc.pages.push(Page {
                index: i,
                width: 400,
                height: 200,
                rotation: 0.0,
                origin: PageOrigin::Image,
                blocks: vec![Block {
                    kind: BlockKind::Paragraph,
                    bbox: Rect::new(20.0, 20.0, 380.0, 60.0),
                    lines: vec![
                        line("Rechnung (Nr. 42)", Rect::new(20.0, 20.0, 380.0, 44.0)),
                        line("Betrag 19,90 EUR", Rect::new(20.0, 48.0, 300.0, 70.0)),
                    ],
                }],
                elapsed_ms: 1.0,
                image: None,
            });
            images.push(RgbImage::from_pixel(400, 200, image::Rgb([250, 250, 250])));
        }
        (doc, images)
    }

    #[cfg(feature = "pdf")]
    #[test]
    fn produces_a_pdf_hayro_can_parse() {
        let (doc, images) = scanned_doc(2);
        let bytes = build_with_images(&doc, &images, &PdfOptions::default()).unwrap();
        assert!(bytes.starts_with(b"%PDF-1.7"));

        let pdf = hayro::hayro_syntax::Pdf::new(std::sync::Arc::new(bytes.clone()))
            .expect("generated PDF must parse");
        assert_eq!(pdf.pages().len(), 2);
    }

    #[test]
    fn page_size_follows_dpi() {
        let (doc, images) = scanned_doc(1);
        let bytes = build_with_images(
            &doc,
            &images,
            &PdfOptions {
                dpi: 100.0,
                ..Default::default()
            },
        )
        .unwrap();
        // 400px at 100 dpi = 4in = 288pt, 200px = 144pt.
        let text = String::from_utf8_lossy(&bytes);
        assert!(
            text.contains("/MediaBox [0 0 288.00 144.00]"),
            "{text:.400}"
        );
    }

    #[test]
    fn text_layer_is_invisible_and_present() {
        let (doc, images) = scanned_doc(1);
        let bytes = build_with_images(&doc, &images, &PdfOptions::default()).unwrap();
        let text = String::from_utf8_lossy(&bytes);
        assert!(text.contains("3 Tr"), "text render mode 3 = invisible");
        assert!(text.contains("Rechnung \\(Nr. 42\\)"), "escaped parens");
    }

    #[test]
    fn a_page_with_other_scripts_gets_the_unicode_font() {
        let (mut doc, images) = scanned_doc(1);
        doc.pages[0].blocks[0].lines[0].text = "請求書 2026".to_string();
        let pdf = build_with_images(&doc, &images, &PdfOptions::default()).unwrap();
        let text = String::from_utf8_lossy(&pdf);

        assert!(text.contains("/Subtype /Type0"), "a Type0 font is written");
        assert!(text.contains("/Encoding /Identity-H"));
        assert!(text.contains("/CIDFontType2"));
        assert!(text.contains("/ToUnicode"));
        assert!(
            text.contains(&format!("/{}", cidfont::UNICODE_FONT_NAME)),
            "the page references it"
        );
        assert!(
            text.contains("<8ACB6C4266F8"),
            "UTF-16 hex, not question marks"
        );
        assert!(
            hayro::hayro_syntax::Pdf::new(std::sync::Arc::new(pdf.clone())).is_ok(),
            "still a readable PDF"
        );
    }

    #[test]
    fn a_western_page_carries_no_unicode_font() {
        let (doc, images) = scanned_doc(1);
        let pdf = build_with_images(&doc, &images, &PdfOptions::default()).unwrap();
        let text = String::from_utf8_lossy(&pdf);
        assert!(!text.contains("/Type0"), "nothing extra for WinAnsi text");
        assert!(!text.contains(cidfont::UNICODE_FONT_NAME));
    }

    #[test]
    fn text_layer_can_be_disabled() {
        let (doc, images) = scanned_doc(1);
        let bytes = build_with_images(
            &doc,
            &images,
            &PdfOptions {
                text_layer: false,
                ..Default::default()
            },
        )
        .unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains("3 Tr"));
    }

    #[test]
    fn mismatched_image_count_is_rejected() {
        let (doc, _) = scanned_doc(2);
        let err = build_with_images(&doc, &[], &PdfOptions::default()).unwrap_err();
        assert!(
            err.to_string().contains("0 image(s) for 2 page(s)"),
            "{err}"
        );
    }

    #[test]
    fn build_requires_page_images() {
        let (doc, _) = scanned_doc(1);
        let err = build(&doc, &PdfOptions::default()).unwrap_err();
        assert!(err.to_string().contains("keep_page_images"), "{err}");
    }

    #[test]
    fn winansi_encodes_western_european_text() {
        // ü = 0xFC, ß = 0xDF, é = 0xE9 — written as octal escapes.
        let (literal, replaced) = winansi_literal("Grüße");
        assert_eq!(literal, "Gr\\374\\337e");
        assert_eq!(replaced, 0);
        assert_eq!(winansi_literal("déjà").0, "d\\351j\\340");
    }

    #[test]
    fn winansi_maps_the_cp1252_range() {
        // The euro sign is 0x80 in WinAnsi but absent from Latin-1.
        assert_eq!(winansi_literal("€").0, "\\200");
        assert_eq!(winansi_literal("„quoted“").0, "\\204quoted\\223");
        assert_eq!(winansi_literal("Œuvre").0, "\\214uvre");
    }

    #[test]
    fn characters_outside_winansi_are_counted_not_smuggled() {
        let (literal, replaced) = winansi_literal("日本語");
        assert_eq!(literal, "???");
        assert_eq!(replaced, 3);
    }

    #[test]
    fn pdf_syntax_characters_are_escaped() {
        assert_eq!(escape_pdf_text("a(b)c\\"), "a\\(b\\)c\\\\");
        assert_eq!(escape_pdf_text("tab\there"), "tab here");
    }

    #[test]
    fn horizontal_scale_stretches_to_the_box() {
        // 10 chars at size 10 ≈ 50pt natural width; target 100pt → 200%.
        let s = horizontal_scale_em("0123456789", 10.0, 100.0, 0.5);
        assert!((s - 200.0).abs() < 1.0, "{s}");
        assert_eq!(horizontal_scale_em("x", 10.0, 0.0, 0.5), 100.0);
    }
}
