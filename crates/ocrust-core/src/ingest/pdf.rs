//! PDF rasterization with `hayro`, a pure-Rust renderer.
//!
//! No Poppler, no PDFium, no native toolchain: this is what lets the Python
//! wheel stay a single self-contained artifact.

use std::sync::Arc;

use hayro::{RenderCache, RenderSettings};
use image::RgbImage;

use crate::doc::PageOrigin;
use crate::error::{Error, Result};

use super::{IngestConfig, RawPage};

/// PDF user-space unit: 72 points per inch.
const POINTS_PER_INCH: f32 = 72.0;

/// Renders the requested pages of a PDF document.
pub fn load(data: &[u8], cfg: &IngestConfig) -> Result<Vec<RawPage>> {
    let pdf = hayro::hayro_syntax::Pdf::new(Arc::new(data.to_vec()))
        .map_err(|e| Error::Pdf(format!("could not parse PDF: {e:?}")))?;
    let cache = RenderCache::new();
    let pages = pdf.pages();

    let mut out = Vec::new();
    for (index, page) in pages.iter().enumerate() {
        if !cfg.wants(index) {
            continue;
        }
        let media = page.media_box();
        let w_pt = (media.x1 - media.x0) as f32;
        let h_pt = (media.y1 - media.y0) as f32;
        if !(w_pt > 0.0 && h_pt > 0.0) {
            return Err(Error::Pdf(format!(
                "page {} has an empty media box",
                index + 1
            )));
        }

        let scale = render_scale(w_pt, h_pt, cfg);
        let settings = RenderSettings {
            x_scale: scale,
            y_scale: scale,
            // hayro defaults to a transparent background; documents need white
            // so that dropping the alpha channel does not turn the page black.
            bg_color: hayro::vello_cpu::color::palette::css::WHITE,
            ..Default::default()
        };
        let pixmap = hayro::render(
            page,
            &cache,
            &hayro::hayro_interpret::InterpreterSettings::default(),
            &settings,
        );

        let rgba = pixmap.data_as_u8_slice();
        let mut rgb = Vec::with_capacity(rgba.len() / 4 * 3);
        for px in rgba.chunks_exact(4) {
            rgb.extend_from_slice(&px[..3]);
        }
        let image = RgbImage::from_raw(u32::from(pixmap.width()), u32::from(pixmap.height()), rgb)
            .ok_or_else(|| Error::Pdf(format!("page {} produced an invalid pixmap", index + 1)))?;

        out.push(RawPage {
            index,
            image,
            origin: PageOrigin::PdfPage,
        });
    }

    if out.is_empty() && cfg.pages.is_none() {
        return Err(Error::Pdf("document has no pages".into()));
    }
    Ok(out)
}

/// Number of pages without rendering anything.
pub fn page_count(data: &[u8]) -> Result<usize> {
    let pdf = hayro::hayro_syntax::Pdf::new(Arc::new(data.to_vec()))
        .map_err(|e| Error::Pdf(format!("could not parse PDF: {e:?}")))?;
    Ok(pdf.pages().len())
}

/// DPI-based scale, capped so huge pages cannot exhaust memory.
fn render_scale(w_pt: f32, h_pt: f32, cfg: &IngestConfig) -> f32 {
    let dpi = cfg.pdf_dpi.clamp(36.0, 1200.0);
    let mut scale = dpi / POINTS_PER_INCH;
    if cfg.pdf_max_side > 0 {
        let longest_pt = w_pt.max(h_pt);
        let max_scale = cfg.pdf_max_side as f32 / longest_pt;
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
        assert_eq!(page_count(&tiny_pdf()).unwrap(), 1);
    }

    #[test]
    fn scale_is_capped_by_max_side() {
        let cfg = IngestConfig {
            pdf_dpi: 1200.0,
            pdf_max_side: 1000,
            ..Default::default()
        };
        let scale = render_scale(612.0, 792.0, &cfg);
        assert!((scale * 792.0) <= 1000.5, "scale {scale}");
    }

    #[test]
    fn broken_pdf_is_an_error() {
        assert!(load(b"%PDF-1.4\nnope", &IngestConfig::default()).is_err());
    }
}
