//! Adding an OCR text layer to an existing PDF, without touching its pages.
//!
//! This is the archival workflow: a scanned PDF goes in, the same PDF comes out
//! — same pages, same images, same compression — with an invisible text layer
//! placed over the words so the file becomes searchable, selectable and
//! indexable. Pages that already carry text are left alone unless asked
//! otherwise.
//!
//! Unlike [`super::pdf`], which rasterizes everything into a fresh document,
//! nothing here is re-encoded: the original page objects are preserved and only
//! a content stream and a font are added.

use image::RgbImage;
use lopdf::{dictionary, Dictionary, Document as PdfDocument, Object, ObjectId, Stream};

use crate::doc::Page;
use crate::error::{Error, Result};

use super::cidfont;
use super::pdf::winansi_literal;

/// Resource name the added font is registered under. Prefixed to avoid clashing
/// with fonts the document already uses.
const FONT_NAME: &str = "OcrustHelv";

/// What [`super::pdf::show_text`] calls the base-14 font it encodes for.
const FONT_NAME_WINANSI: &str = "F1";

/// Options for [`add_text_layer`].
#[derive(Debug, Clone)]
pub struct OverlayOptions {
    /// Resolution used to rasterize pages for recognition.
    pub dpi: f32,
    /// Leave pages that already contain text untouched. This is what makes the
    /// tool safe to run over a mixed archive.
    pub skip_pages_with_text: bool,
    /// Compress the added content streams.
    pub compress: bool,
    /// Password for an encrypted PDF. The output is written decrypted: the
    /// layer is added to the document as it reads once opened, and a password
    /// cannot be re-applied with the same key.
    pub password: Option<crate::ingest::Password>,
}

impl Default for OverlayOptions {
    fn default() -> Self {
        Self {
            dpi: 200.0,
            skip_pages_with_text: true,
            compress: true,
            password: None,
        }
    }
}

/// What [`add_text_layer`] did.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct OverlayReport {
    /// Pages in the document.
    pub pages: usize,
    /// Pages that received a text layer.
    pub pages_with_layer: usize,
    /// Pages skipped because they already had text.
    pub pages_skipped: usize,
    /// Text lines written.
    pub lines: usize,
    /// Characters replaced because a base-14 font cannot show them.
    pub unmappable_chars: usize,
}

/// Page geometry needed to place text, in PDF points.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PageBox {
    pub x0: f32,
    pub y0: f32,
    pub width: f32,
    pub height: f32,
    /// `/Rotate`, normalized to 0, 90, 180 or 270.
    pub rotate: i64,
}

impl PageBox {
    /// Size the page is displayed at, which is what a renderer produces.
    pub fn display_size(&self) -> (f32, f32) {
        if self.rotate == 90 || self.rotate == 270 {
            (self.height, self.width)
        } else {
            (self.width, self.height)
        }
    }

    /// Matrix mapping display-space points onto page space.
    ///
    /// Text is emitted in the renderer's coordinate system; this `cm` puts it
    /// back where the page expects it, so rotated scans line up.
    pub fn display_to_page_matrix(&self) -> [f32; 6] {
        let (x0, y0, w, h) = (self.x0, self.y0, self.width, self.height);
        match self.rotate {
            90 => [0.0, 1.0, -1.0, 0.0, x0 + w, y0],
            180 => [-1.0, 0.0, 0.0, -1.0, x0 + w, y0 + h],
            270 => [0.0, -1.0, 1.0, 0.0, x0, y0 + h],
            _ => [1.0, 0.0, 0.0, 1.0, x0, y0],
        }
    }

    /// Applies [`Self::display_to_page_matrix`] to a point, for tests and
    /// diagnostics.
    pub fn display_to_page(&self, dx: f32, dy: f32) -> (f32, f32) {
        let m = self.display_to_page_matrix();
        (m[0] * dx + m[2] * dy + m[4], m[1] * dx + m[3] * dy + m[5])
    }
}

/// Which pages need OCR, and what geometry they have.
#[derive(Debug, Clone)]
pub struct PagePlan {
    /// Zero-based page index.
    pub index: usize,
    pub page_box: PageBox,
    /// False when the page already has text and should be skipped.
    pub needs_ocr: bool,
}

/// Inspects a PDF and reports what an overlay run would do.
///
/// Useful on its own (`ocrust ocr --dry-run`) and used internally so that only
/// the pages that need OCR are rasterized.
pub fn plan(pdf: &[u8], opts: &OverlayOptions) -> Result<Vec<PagePlan>> {
    let doc = load(pdf, opts.password.as_ref())?;
    let mut plans = Vec::new();
    for (position, (_, page_id)) in doc.get_pages().into_iter().enumerate() {
        let page_box = page_box(&doc, page_id)?;
        let has_text = page_has_text(&doc, page_id);
        plans.push(PagePlan {
            index: position,
            page_box,
            needs_ocr: !(opts.skip_pages_with_text && has_text),
        });
    }
    Ok(plans)
}

/// Adds an invisible text layer to `pdf`.
///
/// `recognize` is called with the zero-based page index and the rasterized page,
/// and returns the OCR result for it. Keeping the callback abstract means this
/// module needs no models and can be tested on its own.
pub fn add_text_layer<F>(
    pdf: &[u8],
    opts: &OverlayOptions,
    mut recognize: F,
) -> Result<(Vec<u8>, OverlayReport)>
where
    F: FnMut(usize, RgbImage) -> Result<Page>,
{
    let plans = plan(pdf, opts)?;
    let wanted: Vec<usize> = plans
        .iter()
        .filter(|p| p.needs_ocr)
        .map(|p| p.index)
        .collect();

    let mut report = OverlayReport {
        pages: plans.len(),
        pages_skipped: plans.len() - wanted.len(),
        ..Default::default()
    };
    // A PDF with no readable pages is not a PDF with nothing left to do. The
    // pass-through below is for documents whose every page already has text;
    // taking it for zero pages is how a locked file used to come back
    // unchanged, "0 of 0 pages layered", with a successful exit.
    if plans.is_empty() {
        return Err(Error::Pdf("no readable pages in this PDF".into()));
    }
    if wanted.is_empty() {
        return Ok((pdf.to_vec(), report));
    }

    // Rasterize only the pages that need it, and only one at a time: an
    // overlay over a 300-page scan should not need 3 GB of pixels to write.
    let renderer = crate::ingest::pdf::Renderer::new(
        std::sync::Arc::new(pdf.to_vec()),
        &crate::ingest::IngestConfig {
            pdf_dpi: opts.dpi,
            pdf_password: opts.password.clone(),
            ..Default::default()
        },
    )?;

    let mut doc = load(pdf, opts.password.as_ref())?;
    let page_ids: Vec<ObjectId> = doc.get_pages().into_values().collect();
    let font_id = doc.add_object(dictionary! {
        "Type" => "Font",
        "Subtype" => "Type1",
        "BaseFont" => "Helvetica",
        "Encoding" => "WinAnsiEncoding",
    });
    let mut font_used = false;
    // Added the first time a page carries text WinAnsi cannot hold, so Western
    // documents keep exactly the objects they had before.
    let mut unicode_font_id: Option<ObjectId> = None;

    renderer.render_each(&wanted, &mut |index, image| {
        let Some(plan) = plans.iter().find(|p| p.index == index) else {
            return Ok(());
        };
        let (image_w, image_h) = image.dimensions();
        let ocr_page = recognize(index, image)?;

        let needs_unicode = ocr_page
            .lines()
            .any(|line| cidfont::needs_unicode(line.text.trim()));
        if needs_unicode && unicode_font_id.is_none() {
            unicode_font_id = Some(add_unicode_font(&mut doc));
        }

        let (stream, lines, unmappable) = text_layer_stream(
            &ocr_page,
            plan.page_box,
            image_w,
            image_h,
            unicode_font_id.is_some(),
        );
        if lines == 0 {
            return Ok(());
        }
        report.lines += lines;
        report.unmappable_chars += unmappable;

        let mut content = Stream::new(dictionary! {}, stream.into_bytes());
        if opts.compress {
            // Failing to compress is not fatal; the uncompressed stream is valid.
            let _ = content.compress();
        }
        let content_id = doc.add_object(content);

        let page_id = *page_ids
            .get(index)
            .ok_or_else(|| Error::Pdf(format!("page {} disappeared", index + 1)))?;
        let mut fonts: Vec<(&str, ObjectId)> = vec![(FONT_NAME, font_id)];
        if let Some(id) = unicode_font_id {
            fonts.push((cidfont::UNICODE_FONT_NAME, id));
        }
        attach_font(&mut doc, page_id, &fonts)?;
        append_content(&mut doc, page_id, content_id)?;
        font_used = true;
        report.pages_with_layer += 1;
        Ok(())
    })?;

    if !font_used {
        // Nothing was written: hand back the original bytes untouched.
        return Ok((pdf.to_vec(), report));
    }

    let mut out = Vec::with_capacity(pdf.len() + 4096);
    doc.save_to(&mut out)
        .map_err(|e| Error::Pdf(format!("could not write PDF: {e}")))?;
    Ok((out, report))
}

fn load(pdf: &[u8], password: Option<&crate::ingest::Password>) -> Result<PdfDocument> {
    let loaded = match password {
        Some(p) => PdfDocument::load_mem_with_options(pdf, lopdf::LoadOptions::with_password(&p.0)),
        None => PdfDocument::load_mem(pdf),
    };
    let doc = loaded.map_err(|e| match e {
        lopdf::Error::InvalidPassword => crate::ingest::pdf::password_error(true),
        other => Error::Unsupported(format!("could not parse PDF: {other}")),
    })?;
    // lopdf does not refuse a document it cannot decrypt: it logs a warning and
    // returns it still encrypted, and every page of it then reads as missing.
    // Encrypted in the file but never decrypted is the signature of that.
    if doc.is_encrypted() && !doc.was_encrypted() {
        return Err(crate::ingest::pdf::password_error(password.is_some()));
    }
    Ok(doc)
}

/// Builds the content stream holding the invisible text for one page.
///
/// Returns the stream, the number of lines written and how many characters had
/// to be replaced.
fn text_layer_stream(
    page: &Page,
    page_box: PageBox,
    image_w: u32,
    image_h: u32,
    unicode_font: bool,
) -> (String, usize, usize) {
    let (display_w, display_h) = page_box.display_size();
    // Points per pixel, derived from the rasterization that produced the image.
    let sx = if image_w > 0 {
        display_w / image_w as f32
    } else {
        0.0
    };
    let sy = if image_h > 0 {
        display_h / image_h as f32
    } else {
        0.0
    };

    let m = page_box.display_to_page_matrix();
    let mut out = format!(
        "q\n{:.4} {:.4} {:.4} {:.4} {:.4} {:.4} cm\nBT\n3 Tr\n",
        m[0], m[1], m[2], m[3], m[4], m[5]
    );

    let mut lines = 0usize;
    let mut unmappable = 0usize;
    for line in page.lines() {
        let text = line.text.trim();
        if text.is_empty() {
            continue;
        }
        let (font, show, em) = super::pdf::show_text(text, unicode_font);
        if font == FONT_NAME_WINANSI {
            unmappable += winansi_literal(text).1;
        }

        // Rotated and skewed lines get a rotated baseline, so selecting the text
        // follows the ink instead of cutting across it.
        let angle = baseline_angle(line.angle);
        let radians = -angle.to_radians(); // image y grows downwards, PDF y upwards
        let (cos, sin) = (radians.cos(), radians.sin());

        let quad = line.quad.ordered();
        let baseline = quad.points[3]; // bottom-left corner of the line
        let size = (line.quad.edge_height() * sy).max(1.0);
        let x = baseline.x * sx;
        // Image space counts down from the top; PDF counts up from the bottom.
        let y = display_h - baseline.y * sy + size * 0.18;
        let target_w = line.quad.edge_width() * sx;
        let scale = super::pdf::horizontal_scale_em(text, size, target_w, em);
        // The base-14 font keeps its own resource name; the Unicode font brings
        // its own, and `show_text` has already encoded the string for it.
        let resource = if font == FONT_NAME_WINANSI {
            FONT_NAME
        } else {
            font
        };

        out.push_str(&format!(
            "/{resource} {size:.2} Tf\n{scale:.1} Tz\n\
             {cos:.5} {sin:.5} {:.5} {cos:.5} {x:.2} {y:.2} Tm\n{show} Tj\n",
            -sin
        ));
        lines += 1;
    }
    out.push_str("ET\nQ\n");
    (out, lines, unmappable)
}

/// Folds a line angle into the half turn a baseline can express.
///
/// `Line::angle` carries the 180-degree flip the line classifier applied, and a
/// flipped line sits on the same baseline as an unflipped one — so 180 means
/// zero here. Clamping instead would tip such a line onto its side.
fn baseline_angle(angle: f32) -> f32 {
    let mut folded = angle % 180.0;
    if folded > 90.0 {
        folded -= 180.0;
    } else if folded < -90.0 {
        folded += 180.0;
    }
    folded
}

/// Reads a page's media box and rotation, following `/Parent` for inherited
/// attributes as the PDF specification requires.
fn page_box(doc: &PdfDocument, page_id: ObjectId) -> Result<PageBox> {
    let media = inherited(doc, page_id, b"MediaBox")
        .and_then(|o| o.as_array().ok().cloned())
        .unwrap_or_else(|| {
            // US Letter, the same default renderers fall back to.
            vec![0.into(), 0.into(), 612.into(), 792.into()]
        });
    if media.len() != 4 {
        return Err(Error::Pdf("media box does not have four entries".into()));
    }
    let number = |o: &Object| -> f32 {
        o.as_f32()
            .or_else(|_| o.as_i64().map(|v| v as f32))
            .unwrap_or(0.0)
    };
    let (x0, y0, x1, y1) = (
        number(&media[0]),
        number(&media[1]),
        number(&media[2]),
        number(&media[3]),
    );
    let rotate = normalize_rotation(
        inherited(doc, page_id, b"Rotate")
            .and_then(|o| o.as_i64().ok())
            .unwrap_or(0),
    );

    Ok(PageBox {
        x0: x0.min(x1),
        y0: y0.min(y1),
        width: (x1 - x0).abs().max(1.0),
        height: (y1 - y0).abs().max(1.0),
        rotate,
    })
}

/// Normalizes `/Rotate` to 0, 90, 180 or 270.
///
/// The specification allows any multiple of 90, including negative values and
/// values beyond a full turn, and real documents use all of them.
fn normalize_rotation(raw: i64) -> i64 {
    let wrapped = raw.rem_euclid(360);
    (wrapped / 90) * 90
}

/// Looks up `key` on the page, walking up the page tree when it is inherited.
fn inherited<'a>(doc: &'a PdfDocument, page_id: ObjectId, key: &[u8]) -> Option<&'a Object> {
    let mut current = page_id;
    for _ in 0..32 {
        let dict = doc.get_dictionary(current).ok()?;
        if let Ok(value) = dict.get(key) {
            // Resolve one level of indirection if needed.
            return match value {
                Object::Reference(id) => doc.get_object(*id).ok(),
                other => Some(other),
            };
        }
        current = match dict.get(b"Parent") {
            Ok(Object::Reference(id)) => *id,
            _ => return None,
        };
    }
    None
}

/// True when the page's content stream shows any text.
///
/// Text-showing operators are what matters here, not fonts: a page can declare
/// fonts it never uses, and a born-digital page always has `Tj`/`TJ`.
fn page_has_text(doc: &PdfDocument, page_id: ObjectId) -> bool {
    let content = doc.get_page_content(page_id);
    let Ok(decoded) = lopdf::content::Content::decode(&content) else {
        return false;
    };
    decoded.operations.iter().any(|op| {
        matches!(op.operator.as_str(), "Tj" | "TJ" | "'" | "\"")
            && op.operands.iter().any(|operand| match operand {
                Object::String(bytes, _) => !bytes.is_empty(),
                Object::Array(items) => items.iter().any(|i| match i {
                    Object::String(bytes, _) => !bytes.is_empty(),
                    _ => false,
                }),
                _ => false,
            })
    })
}

/// Adds the Unicode font, its descendant and the `ToUnicode` map to the file.
fn add_unicode_font(doc: &mut PdfDocument) -> ObjectId {
    let cmap = cidfont::to_unicode_cmap();
    let to_unicode = doc.add_object(Stream::new(dictionary! {}, cmap.into_bytes()));
    let descriptor = doc.add_object(dictionary! {
        "Type" => "FontDescriptor",
        "FontName" => Object::Name(cidfont::UNICODE_BASE_FONT.into()),
        "Flags" => cidfont::DESCRIPTOR_FLAGS,
        "FontBBox" => Object::Array(
            cidfont::DESCRIPTOR_BBOX.iter().map(|v| Object::Integer(*v)).collect(),
        ),
        "ItalicAngle" => 0,
        "Ascent" => cidfont::DESCRIPTOR_ASCENT,
        "Descent" => cidfont::DESCRIPTOR_DESCENT,
        "CapHeight" => cidfont::DESCRIPTOR_CAP_HEIGHT,
        "StemV" => cidfont::DESCRIPTOR_STEM_V,
    });
    let descendant = doc.add_object(dictionary! {
        "Type" => "Font",
        "Subtype" => "CIDFontType2",
        "BaseFont" => Object::Name(cidfont::UNICODE_BASE_FONT.into()),
        "CIDSystemInfo" => dictionary! {
            "Registry" => Object::string_literal("Adobe"),
            "Ordering" => Object::string_literal("Identity"),
            "Supplement" => 0,
        },
        "FontDescriptor" => Object::Reference(descriptor),
        "DW" => 1000,
        "CIDToGIDMap" => "Identity",
    });
    doc.add_object(dictionary! {
        "Type" => "Font",
        "Subtype" => "Type0",
        "BaseFont" => Object::Name(cidfont::UNICODE_BASE_FONT.into()),
        "Encoding" => "Identity-H",
        "DescendantFonts" => Object::Array(vec![Object::Reference(descendant)]),
        "ToUnicode" => Object::Reference(to_unicode),
    })
}

/// Registers the overlay font in the page's resources.
///
/// Inherited resources are copied onto the page first, because setting
/// `/Resources` here would otherwise shadow them and break existing content.
fn attach_font(
    doc: &mut PdfDocument,
    page_id: ObjectId,
    fonts_to_add: &[(&str, ObjectId)],
) -> Result<()> {
    let existing: Option<Dictionary> = match inherited(doc, page_id, b"Resources") {
        Some(Object::Dictionary(dict)) => Some(dict.clone()),
        _ => None,
    };
    let mut resources = existing.unwrap_or_default();

    let mut fonts = match resources.get(b"Font") {
        Ok(Object::Dictionary(dict)) => dict.clone(),
        Ok(Object::Reference(id)) => doc.get_dictionary(*id).cloned().unwrap_or_default(),
        _ => Dictionary::new(),
    };
    for (name, id) in fonts_to_add {
        fonts.set(*name, Object::Reference(*id));
    }
    resources.set("Font", Object::Dictionary(fonts));

    let page = doc
        .get_dictionary_mut(page_id)
        .map_err(|e| Error::Pdf(format!("page dictionary unavailable: {e}")))?;
    page.set("Resources", Object::Dictionary(resources));
    Ok(())
}

/// Appends a content stream to the page, keeping the original ones in order.
fn append_content(doc: &mut PdfDocument, page_id: ObjectId, content_id: ObjectId) -> Result<()> {
    let page = doc
        .get_dictionary_mut(page_id)
        .map_err(|e| Error::Pdf(format!("page dictionary unavailable: {e}")))?;
    let contents = match page.get(b"Contents") {
        Ok(Object::Array(items)) => {
            let mut items = items.clone();
            items.push(Object::Reference(content_id));
            items
        }
        Ok(Object::Reference(id)) => vec![Object::Reference(*id), Object::Reference(content_id)],
        _ => vec![Object::Reference(content_id)],
    };
    page.set("Contents", Object::Array(contents));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::doc::{Block, BlockKind, Line, PageOrigin};
    use crate::geom::{Quad, Rect};

    /// A PDF with no page that can be read is an error. The pass-through for a
    /// document whose every page already has text used to catch this case as
    /// well, which is how a password-protected file came back byte for byte
    /// unchanged — "0 of 0 pages layered", and a successful exit.
    #[test]
    fn a_pdf_without_readable_pages_is_an_error_not_a_pass_through() {
        let mut doc = PdfDocument::with_version("1.5");
        let pages = doc.add_object(lopdf::dictionary! {
            "Type" => "Pages",
            "Kids" => Vec::<lopdf::Object>::new(),
            "Count" => 0,
        });
        let catalog = doc.add_object(lopdf::dictionary! {
            "Type" => "Catalog",
            "Pages" => pages,
        });
        doc.trailer.set("Root", catalog);
        let mut pdf = Vec::new();
        doc.save_to(&mut pdf).unwrap();

        let outcome = add_text_layer(
            &pdf,
            &OverlayOptions::default(),
            |_: usize, _: RgbImage| -> Result<Page> { unreachable!("nothing to recognize") },
        );
        let err = outcome.unwrap_err().to_string();
        assert!(err.contains("no readable pages"), "{err}");
    }

    fn page_with_line(text: &str, bbox: Rect, width: u32, height: u32) -> Page {
        Page {
            index: 0,
            width,
            height,
            rotation: 0.0,
            origin: PageOrigin::PdfPage,
            blocks: vec![Block {
                kind: BlockKind::Paragraph,
                table: None,
                bbox,
                lines: vec![Line {
                    text: text.into(),
                    confidence: 0.9,
                    quad: Quad::from_rect(bbox),
                    bbox,
                    det_score: 0.9,
                    ..Default::default()
                }],
            }],
            elapsed_ms: 1.0,
            quality: None,
            image: None,
        }
    }

    fn letter(rotate: i64) -> PageBox {
        PageBox {
            x0: 0.0,
            y0: 0.0,
            width: 612.0,
            height: 792.0,
            rotate,
        }
    }

    #[test]
    fn display_size_swaps_on_quarter_turns() {
        assert_eq!(letter(0).display_size(), (612.0, 792.0));
        assert_eq!(letter(90).display_size(), (792.0, 612.0));
        assert_eq!(letter(180).display_size(), (612.0, 792.0));
        assert_eq!(letter(270).display_size(), (792.0, 612.0));
    }

    #[test]
    fn unrotated_pages_map_one_to_one() {
        let page = letter(0);
        assert_eq!(page.display_to_page(0.0, 0.0), (0.0, 0.0));
        assert_eq!(page.display_to_page(100.0, 200.0), (100.0, 200.0));
    }

    #[test]
    fn media_box_origin_is_honoured() {
        let page = PageBox {
            x0: 20.0,
            y0: 30.0,
            width: 600.0,
            height: 800.0,
            rotate: 0,
        };
        assert_eq!(page.display_to_page(0.0, 0.0), (20.0, 30.0));
    }

    #[test]
    fn quarter_turns_map_display_corners_onto_page_corners() {
        // /Rotate 90: the display is the page turned clockwise, so the display's
        // bottom-left corner is the page's bottom-right corner.
        let page = letter(90);
        let (dw, dh) = page.display_size();
        assert_eq!(page.display_to_page(0.0, 0.0), (612.0, 0.0));
        assert_eq!(page.display_to_page(0.0, dh), (0.0, 0.0));
        assert_eq!(page.display_to_page(dw, dh), (0.0, 792.0));

        let page = letter(270);
        assert_eq!(page.display_to_page(0.0, 0.0), (0.0, 792.0));
        assert_eq!(page.display_to_page(dw, 0.0), (0.0, 0.0));

        let page = letter(180);
        assert_eq!(page.display_to_page(0.0, 0.0), (612.0, 792.0));
        assert_eq!(page.display_to_page(612.0, 792.0), (0.0, 0.0));
    }

    #[test]
    fn every_rotation_keeps_points_inside_the_page() {
        for rotate in [0, 90, 180, 270] {
            let page = letter(rotate);
            let (dw, dh) = page.display_size();
            for (dx, dy) in [
                (0.0, 0.0),
                (dw, 0.0),
                (0.0, dh),
                (dw, dh),
                (dw / 3.0, dh / 7.0),
            ] {
                let (x, y) = page.display_to_page(dx, dy);
                assert!(
                    (-0.01..=612.01).contains(&x) && (-0.01..=792.01).contains(&y),
                    "rotate {rotate}: ({dx},{dy}) -> ({x},{y}) left the page"
                );
            }
        }
    }

    #[test]
    fn rotations_are_normalized_to_quarter_turns() {
        assert_eq!(normalize_rotation(0), 0);
        assert_eq!(normalize_rotation(90), 90);
        assert_eq!(normalize_rotation(-90), 270);
        assert_eq!(normalize_rotation(-270), 90);
        assert_eq!(normalize_rotation(450), 90);
        assert_eq!(normalize_rotation(720), 0);
        // Values that are not multiples of 90 round down to one.
        assert_eq!(normalize_rotation(100), 90);
        assert_eq!(normalize_rotation(-45), 270);
    }

    #[test]
    fn text_stream_is_invisible_and_uses_the_overlay_font() {
        let page = page_with_line(
            "Rechnung",
            Rect::new(100.0, 100.0, 400.0, 140.0),
            1200,
            1600,
        );
        let (stream, lines, unmappable) = text_layer_stream(&page, letter(0), 1200, 1600, false);
        assert_eq!(lines, 1);
        assert_eq!(unmappable, 0);
        assert!(stream.contains("3 Tr"), "{stream}");
        assert!(stream.contains(&format!("/{FONT_NAME}")), "{stream}");
        assert!(
            stream.starts_with("q\n1.0000 0.0000 0.0000 1.0000"),
            "{stream}"
        );
        assert!(stream.ends_with("ET\nQ\n"), "{stream}");
    }

    #[test]
    fn japanese_goes_into_the_unicode_font_as_utf16() {
        let page = page_with_line("請求書", Rect::new(100.0, 100.0, 400.0, 140.0), 1200, 1600);

        // Without the Unicode font the text still degrades to `?`.
        let (plain, _, unmappable) = text_layer_stream(&page, letter(0), 1200, 1600, false);
        assert_eq!(unmappable, 3);
        assert!(plain.contains("(???)"), "{plain}");

        // With it, the line is written as UTF-16 hex and nothing is lost.
        let (stream, lines, unmappable) = text_layer_stream(&page, letter(0), 1200, 1600, true);
        assert_eq!((lines, unmappable), (1, 0));
        assert!(
            stream.contains(&format!("/{}", cidfont::UNICODE_FONT_NAME)),
            "{stream}"
        );
        assert!(stream.contains("<8ACB6C4266F8> Tj"), "{stream}");
    }

    #[test]
    fn western_lines_keep_the_base14_font_even_when_unicode_is_available() {
        let page = page_with_line("Grüße", Rect::new(100.0, 100.0, 400.0, 140.0), 1200, 1600);
        let (stream, _, unmappable) = text_layer_stream(&page, letter(0), 1200, 1600, true);
        assert_eq!(unmappable, 0);
        assert!(stream.contains(&format!("/{FONT_NAME}")), "{stream}");
        assert!(!stream.contains(cidfont::UNICODE_FONT_NAME), "{stream}");
    }

    #[test]
    fn text_position_follows_the_rasterization_scale() {
        // 1200px wide image for a 612pt page: 0.51 pt per pixel.
        let page = page_with_line("x", Rect::new(600.0, 800.0, 700.0, 840.0), 1200, 1600);
        let (stream, _, _) = text_layer_stream(&page, letter(0), 1200, 1600, false);
        let tm = stream
            .lines()
            .find(|l| l.contains(" Tm"))
            .expect("text matrix");
        // x = 600 * 612/1200 = 306
        assert!(tm.contains("306.00"), "{tm}");
    }

    #[test]
    fn baseline_angles_fold_into_a_half_turn() {
        assert_eq!(baseline_angle(0.0), 0.0);
        assert_eq!(baseline_angle(180.0), 0.0);
        assert_eq!(baseline_angle(-180.0), 0.0);
        assert!((baseline_angle(185.0) - 5.0).abs() < 1e-4);
        assert!((baseline_angle(-4.0) + 4.0).abs() < 1e-4);
        // A genuinely sideways line keeps its quarter turn. 90 and -90 describe
        // the same baseline, so the fold settles on 90.
        assert!((baseline_angle(90.0) - 90.0).abs() < 1e-4);
        assert!((baseline_angle(270.0) - 90.0).abs() < 1e-4);
        assert!((baseline_angle(-90.0) + 90.0).abs() < 1e-4);
    }

    #[test]
    fn skewed_lines_get_a_rotated_text_matrix() {
        let mut page = page_with_line("schief", Rect::new(50.0, 50.0, 350.0, 90.0), 1200, 1600);
        page.blocks[0].lines[0].angle = 30.0;
        let (stream, _, _) = text_layer_stream(&page, letter(0), 1200, 1600, false);
        let tm = stream
            .lines()
            .find(|l| l.contains(" Tm"))
            .expect("text matrix");
        // cos(30 deg) = 0.866, and the sign convention flips with PDF's y axis.
        assert!(tm.starts_with("0.86603 -0.50000 0.50000 0.86603"), "{tm}");
    }

    #[test]
    fn unmappable_characters_are_counted() {
        let page = page_with_line("日本語", Rect::new(0.0, 0.0, 100.0, 40.0), 200, 200);
        let (_, lines, unmappable) = text_layer_stream(&page, letter(0), 200, 200, false);
        assert_eq!(lines, 1);
        assert_eq!(unmappable, 3);
    }

    #[test]
    fn empty_pages_produce_no_lines() {
        let mut page = page_with_line("   ", Rect::new(0.0, 0.0, 10.0, 10.0), 100, 100);
        let (_, lines, _) = text_layer_stream(&page, letter(0), 100, 100, false);
        assert_eq!(lines, 0);
        page.blocks.clear();
        let (_, lines, _) = text_layer_stream(&page, letter(0), 100, 100, false);
        assert_eq!(lines, 0);
    }
}
