//! PDF rasterization with `hayro`, a pure-Rust renderer.
//!
//! No Poppler, no PDFium, no native toolchain: this is what lets the Python
//! wheel stay a single self-contained artifact.

use std::sync::Arc;

use hayro::{RenderCache, RenderSettings};
use image::RgbImage;

use crate::doc::PageOrigin;
use crate::error::{Error, Result};

use super::pdftext::{self, PdfText};
use super::{IngestConfig, Password, RawPage, TextPage};

/// PDF user-space unit: 72 points per inch.
const POINTS_PER_INCH: f32 = 72.0;

/// A parsed PDF that rasterizes pages one at a time.
///
/// Parsing happens once; rendering is what costs memory, and handing out one
/// page at a time rather than all of them is what lets a 500-page document be
/// scanned on a laptop.
pub struct Renderer {
    pdf: hayro::hayro_syntax::Pdf,
    dpi: f32,
    max_side: u32,
    text: PdfText,
}

impl std::fmt::Debug for Renderer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Renderer")
            .field("pages", &self.page_count())
            .field("dpi", &self.dpi)
            .finish()
    }
}

impl Renderer {
    /// Parses `data`, which the renderer then shares rather than copies.
    pub fn new(data: Arc<Vec<u8>>, cfg: &IngestConfig) -> Result<Self> {
        let pdf = open(data, cfg.pdf_password.as_ref())?;
        if pdf.pages().is_empty() {
            return Err(Error::Unsupported("the PDF has no pages".into()));
        }
        Ok(Self {
            pdf,
            dpi: cfg.pdf_dpi,
            max_side: cfg.pdf_max_side,
            text: cfg.pdf_text,
        })
    }

    /// Hands each of `indices` to `sink` as a page to scan: read from its text
    /// layer when that layer can stand for the page (see [`PdfText`]),
    /// rendered to pixels otherwise.
    pub fn read_each(
        &self,
        indices: &[usize],
        sink: &mut dyn FnMut(RawPage) -> Result<()>,
    ) -> Result<()> {
        if self.text == PdfText::Never {
            return self.render_each(indices, &mut |index, image| {
                sink(RawPage {
                    index,
                    image,
                    origin: PageOrigin::PdfPage,
                    text: None,
                })
            });
        }
        let pages = self.pdf.pages();
        let cache = RenderCache::new();
        let fonts = hayro::hayro_interpret::InterpreterCache::new();
        for &index in indices {
            let page = pages
                .get(index)
                .ok_or_else(|| Error::Pdf(format!("the document has no page {}", index + 1)))?;
            let scale = self.page_scale(page, index)?;
            let layer = pdftext::read(page, &fonts, scale);
            if layer.usable(self.text) {
                let (width, height) = layer.size();
                sink(RawPage {
                    index,
                    image: image::RgbImage::new(0, 0),
                    origin: PageOrigin::PdfText,
                    text: Some(TextPage {
                        lines: layer.lines(self.text == PdfText::Always),
                        width,
                        height,
                    }),
                })?;
                continue;
            }
            let image = render_page(page, index, &cache, scale)?;
            sink(RawPage {
                index,
                image,
                origin: PageOrigin::PdfPage,
                text: None,
            })?;
        }
        Ok(())
    }

    /// Pixels per point for `page`, or why it cannot be rendered.
    fn page_scale(&self, page: &hayro::hayro_syntax::page::Page<'_>, index: usize) -> Result<f32> {
        let media = page.media_box();
        let w_pt = (media.x1 - media.x0) as f32;
        let h_pt = (media.y1 - media.y0) as f32;
        if !(w_pt > 0.0 && h_pt > 0.0) {
            return Err(Error::Pdf(format!(
                "page {} has an empty media box",
                index + 1
            )));
        }
        Ok(render_scale(w_pt, h_pt, self.dpi, self.max_side))
    }

    pub fn page_count(&self) -> usize {
        self.pdf.pages().len()
    }

    /// Rasterizes each of `indices` in turn and hands it to `sink`.
    ///
    /// One call, one render cache: hayro asks for a cache per document, because
    /// it holds the parsed embedded fonts, and re-parsing a CJK font for every
    /// page of a report is not free. The cache borrows from the parsed PDF, so
    /// it cannot be stored next to it — which is why this renders through a
    /// callback instead of returning pages. The caller still only ever holds the
    /// page it was just given.
    pub fn render_each(
        &self,
        indices: &[usize],
        sink: &mut dyn FnMut(usize, RgbImage) -> Result<()>,
    ) -> Result<()> {
        // `Pages` derefs to a slice, so a page is a lookup rather than a walk.
        let pages = self.pdf.pages();
        let cache = RenderCache::new();
        for &index in indices {
            let page = pages
                .get(index)
                .ok_or_else(|| Error::Pdf(format!("the document has no page {}", index + 1)))?;
            let scale = self.page_scale(page, index)?;
            let image = render_page(page, index, &cache, scale)?;
            sink(index, image)?;
        }
        Ok(())
    }
}

/// Rasterizes one page at `scale` pixels per point, on white.
fn render_page<'a>(
    page: &'a hayro::hayro_syntax::page::Page<'a>,
    index: usize,
    cache: &RenderCache<'a>,
    scale: f32,
) -> Result<RgbImage> {
    let settings = RenderSettings {
        x_scale: scale,
        y_scale: scale,
        // hayro defaults to a transparent background; documents need
        // white so that dropping the alpha channel does not turn the
        // page black.
        bg_color: hayro::vello_cpu::color::palette::css::WHITE,
        ..Default::default()
    };
    let pixmap = hayro::render(
        page,
        cache,
        &hayro::hayro_interpret::InterpreterSettings::default(),
        &settings,
    );

    let (width, height) = (u32::from(pixmap.width()), u32::from(pixmap.height()));
    let rgb = drop_alpha(pixmap.data_as_u8_slice());
    // The pixmap is four bytes a pixel and the image three; dropping it
    // here keeps the page's transient cost to seven rather than ten.
    drop(pixmap);
    RgbImage::from_raw(width, height, rgb)
        .ok_or_else(|| Error::Pdf(format!("page {} produced an invalid pixmap", index + 1)))
}

/// Copies RGBA pixels into a tight RGB buffer.
///
/// Written as a row of `copy_from_slice`s rather than a byte-at-a-time push:
/// this runs over every pixel of every page, and a full-page pixmap is a
/// four-megapixel buffer.
fn drop_alpha(rgba: &[u8]) -> Vec<u8> {
    let pixels = rgba.len() / 4;
    let mut rgb = vec![0u8; pixels * 3];
    for (dst, src) in rgb.chunks_exact_mut(3).zip(rgba.chunks_exact(4)) {
        dst.copy_from_slice(&src[..3]);
    }
    rgb
}

/// Renders the requested pages of a PDF document, all of them at once.
pub fn load(data: &[u8], cfg: &IngestConfig) -> Result<Vec<RawPage>> {
    let renderer = Renderer::new(Arc::new(data.to_vec()), cfg)?;
    let wanted: Vec<usize> = (0..renderer.page_count())
        .filter(|i| cfg.wants(*i))
        .collect();
    let mut out = Vec::new();
    renderer.render_each(&wanted, &mut |index, image| {
        out.push(RawPage {
            index,
            image,
            origin: PageOrigin::PdfPage,
            text: None,
        });
        Ok(())
    })?;
    Ok(out)
}

/// Number of pages without rendering anything.
pub fn page_count(data: &[u8], password: Option<&Password>) -> Result<usize> {
    Ok(open(Arc::new(data.to_vec()), password)?.pages().len())
}

/// Parses a PDF, with its password when it has one.
///
/// A locked document is an error that says what to do, not the parser's
/// `Decryption(PasswordProtected)`: the person reading it has a password to
/// find, not a parser to debug.
fn open(data: Arc<Vec<u8>>, password: Option<&Password>) -> Result<hayro::hayro_syntax::Pdf> {
    use hayro::hayro_syntax::{DecryptionError, LoadPdfError};
    let key = password.map(|p| p.0.as_str()).unwrap_or("");
    hayro::hayro_syntax::Pdf::new_with_password(data, key).map_err(|e| match e {
        LoadPdfError::Decryption(DecryptionError::PasswordProtected) => {
            password_error(password.is_some())
        }
        // A damaged file is an unreadable input like any other: `Unsupported`,
        // which Python sees as the ValueError the documented batch loop
        // `except (IOError, ValueError)` skips. It was a bare RuntimeError,
        // which that loop let through to crash the batch.
        other => Error::Unsupported(format!("could not parse PDF: {other:?}")),
    })
}

/// The error for a PDF that its password (or the lack of one) does not open.
///
/// An unreadable input, not a refusal by the engine: Python sees a
/// `ValueError`, which is what the documented batch loop
/// `except (IOError, ValueError)` skips a broken file on.
pub(crate) fn password_error(password_given: bool) -> Error {
    Error::Unsupported(if password_given {
        "the password given does not open this PDF".into()
    } else {
        "this PDF is protected by a password; pass it with `--password` \
         (or the OCRUST_PASSWORD environment variable), or `password=` in Python"
            .into()
    })
}

/// DPI-based scale, capped so huge pages cannot exhaust memory.
fn render_scale(w_pt: f32, h_pt: f32, dpi: f32, max_side: u32) -> f32 {
    let dpi = dpi.clamp(36.0, 1200.0);
    let mut scale = dpi / POINTS_PER_INCH;
    if max_side > 0 {
        let longest_pt = w_pt.max(h_pt);
        let max_scale = max_side as f32 / longest_pt;
        if max_scale > 0.0 {
            scale = scale.min(max_scale);
        }
    }
    scale.max(0.1)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A minimal one-page PDF with a line of Helvetica text.
    fn tiny_pdf() -> Vec<u8> {
        let content = b"BT /F1 36 Tf 72 700 Td (Hallo OCR) Tj ET";
        let mut objects: Vec<String> = Vec::new();
        objects.push("<< /Type /Catalog /Pages 2 0 R >>".into());
        objects.push("<< /Type /Pages /Kids [3 0 R] /Count 1 >>".into());
        objects.push(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] \
             /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
                .into(),
        );
        objects.push(format!(
            "<< /Length {} >>\nstream\n{}\nendstream",
            content.len(),
            String::from_utf8_lossy(content)
        ));
        objects.push("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".into());

        let mut pdf = String::from("%PDF-1.4\n");
        let mut offsets = Vec::new();
        for (i, body) in objects.iter().enumerate() {
            offsets.push(pdf.len());
            pdf.push_str(&format!("{} 0 obj\n{}\nendobj\n", i + 1, body));
        }
        let xref_at = pdf.len();
        pdf.push_str(&format!("xref\n0 {}\n", objects.len() + 1));
        pdf.push_str("0000000000 65535 f \n");
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

    #[test]
    fn renders_a_page_at_the_requested_dpi() {
        let cfg = IngestConfig {
            pdf_dpi: 144.0,
            ..Default::default()
        };
        let pages = load(&tiny_pdf(), &cfg).expect("render");
        assert_eq!(pages.len(), 1);
        // 612pt x 792pt at 2x scale.
        assert_eq!(pages[0].image.dimensions(), (1224, 1584));
        assert_eq!(pages[0].origin, PageOrigin::PdfPage);
    }

    #[test]
    fn rendered_page_has_white_background_and_dark_glyphs() {
        let pages = load(&tiny_pdf(), &IngestConfig::default()).expect("render");
        let img = &pages[0].image;
        assert_eq!(img.get_pixel(5, 5), &image::Rgb([255, 255, 255]));
        let dark = img.pixels().filter(|p| p.0[0] < 100).count();
        assert!(
            dark > 200,
            "expected rendered text, found {dark} dark pixels"
        );
    }

    #[test]
    fn counts_pages_without_rendering() {
        assert_eq!(page_count(&tiny_pdf(), None).unwrap(), 1);
    }

    #[test]
    fn scale_is_capped_by_max_side() {
        let scale = render_scale(612.0, 792.0, 1200.0, 1000);
        assert!((scale * 792.0) <= 1000.5, "scale {scale}");
    }

    #[test]
    fn broken_pdf_is_an_error() {
        assert!(load(b"%PDF-1.4\nnope", &IngestConfig::default()).is_err());
    }
}
