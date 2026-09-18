//! Turning anything a user throws at us into pages of pixels.
//!
//! Supported today: every raster format the `image` crate decodes (PNG, JPEG,
//! WebP, TIFF, BMP, GIF, PNM, TGA, DDS, HDR, OpenEXR, QOI, ICO), multi-page
//! TIFF, and PDF (rasterized with the pure-Rust `hayro` renderer).

#[cfg(feature = "pdf")]
pub mod pdf;

use std::io::Cursor;
use std::path::{Path, PathBuf};

use image::{DynamicImage, ImageDecoder, RgbImage};

use crate::doc::PageOrigin;
use crate::error::{Error, Result};

/// Where the pixels for a scan come from.
#[derive(Debug, Clone)]
pub enum Source {
    /// A file on disk; the format is sniffed from its content.
    Path(PathBuf),
    /// An encoded document or image in memory.
    Bytes {
        data: Vec<u8>,
        /// Label used in results and error messages.
        name: String,
    },
    /// An already decoded RGB frame.
    Image { image: RgbImage, name: String },
}

impl Source {
    pub fn path(p: impl Into<PathBuf>) -> Self {
        Self::Path(p.into())
    }

    pub fn bytes(data: impl Into<Vec<u8>>, name: impl Into<String>) -> Self {
        Self::Bytes {
            data: data.into(),
            name: name.into(),
        }
    }

    /// Label for results and diagnostics.
    pub fn name(&self) -> String {
        match self {
            Self::Path(p) => p.display().to_string(),
            Self::Bytes { name, .. } | Self::Image { name, .. } => name.clone(),
        }
    }
}

/// One page of pixels ready for preprocessing.
#[derive(Debug, Clone)]
pub struct RawPage {
    pub index: usize,
    pub image: RgbImage,
    pub origin: PageOrigin,
}

/// How to rasterize vector input.
#[derive(Debug, Clone)]
pub struct IngestConfig {
    /// Render PDFs at this DPI. 200 is the sweet spot for OCR quality vs. speed;
    /// 300 helps on small print.
    pub pdf_dpi: f32,
    /// Hard cap on the long side of a rasterized PDF page, in pixels.
    pub pdf_max_side: u32,
    /// Restrict a multi-page source to these zero-based page indices.
    pub pages: Option<Vec<usize>>,
}

impl Default for IngestConfig {
    fn default() -> Self {
        Self {
            pdf_dpi: 200.0,
            pdf_max_side: 4000,
            pages: None,
        }
    }
}

impl IngestConfig {
    fn wants(&self, index: usize) -> bool {
        match &self.pages {
            None => true,
            Some(list) => list.contains(&index),
        }
    }
}

/// Detected container format.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Container {
    Pdf,
    Tiff,
    OtherImage,
}

/// Sniffs the container from magic bytes.
pub fn sniff(data: &[u8]) -> Container {
    if data.starts_with(b"%PDF-") {
        return Container::Pdf;
    }
    // Some PDFs carry leading junk before the header; PDF readers tolerate it.
    if data.len() > 1024
        && data[..1024.min(data.len())]
            .windows(5)
            .any(|w| w == b"%PDF-")
    {
        return Container::Pdf;
    }
    if data.starts_with(b"II*\0") || data.starts_with(b"MM\0*") {
        return Container::Tiff;
    }
    Container::OtherImage
}

/// Loads every page of `source`.
pub fn load(source: &Source, cfg: &IngestConfig) -> Result<Vec<RawPage>> {
    match source {
        Source::Image { image, .. } => Ok(vec![RawPage {
            index: 0,
            image: image.clone(),
            origin: PageOrigin::Image,
        }]),
        Source::Bytes { data, name } => load_bytes(data, name, cfg),
        Source::Path(path) => {
            let data = std::fs::read(path).map_err(|e| Error::io(path, e))?;
            load_bytes(&data, &path.display().to_string(), cfg)
        }
    }
}

fn load_bytes(data: &[u8], name: &str, cfg: &IngestConfig) -> Result<Vec<RawPage>> {
    // The decode runs in a closure so that `?` inside it cannot skip the
    // error mapping below.
    let decoded = (|| -> Result<Vec<RawPage>> {
        match sniff(data) {
            Container::Pdf => {
                #[cfg(feature = "pdf")]
                {
                    pdf::load(data, cfg)
                }
                #[cfg(not(feature = "pdf"))]
                {
                    Err(Error::Unsupported(format!(
                        "{name} is a PDF but ocrust was built without the `pdf` feature"
                    )))
                }
            }
            Container::Tiff => load_tiff(data, cfg),
            Container::OtherImage => {
                if !cfg.wants(0) {
                    return Ok(Vec::new());
                }
                Ok(vec![RawPage {
                    index: 0,
                    image: decode_single(data)?,
                    origin: PageOrigin::Image,
                }])
            }
        }
    })();

    decoded.map_err(|e| match e {
        // A file we cannot decode is an input problem, not an internal one, so
        // say so with the file name attached.
        Error::Image(inner) => Error::Unsupported(format!("{name}: {inner}")),
        Error::PlainIo(inner) => Error::Unsupported(format!("{name}: {inner}")),
        other => other,
    })
}

/// Decodes one raster image, honouring its EXIF orientation.
fn decode_single(data: &[u8]) -> Result<RgbImage> {
    let reader = image::ImageReader::new(Cursor::new(data))
        .with_guessed_format()
        .map_err(Error::PlainIo)?;
    let mut decoder = reader.into_decoder()?;
    // Phone photos are almost always stored rotated with an EXIF tag.
    let orientation = decoder.orientation().ok();
    let mut img = DynamicImage::from_decoder(decoder)?;
    if let Some(o) = orientation {
        img.apply_orientation(o);
    }
    Ok(img.into_rgb8())
}

/// Decodes every page of a (possibly multi-page) TIFF.
///
/// `image` only exposes the first IFD, so the `tiff` crate (which `image` uses
/// internally anyway) drives the page walk. Scanned faxes and archive masters
/// are routinely multi-page, and silently dropping pages would be worse than
/// not supporting the format at all.
fn load_tiff(data: &[u8], cfg: &IngestConfig) -> Result<Vec<RawPage>> {
    use tiff::decoder::Decoder;

    let mut decoder = Decoder::new(Cursor::new(data))
        .map_err(|e| Error::Unsupported(format!("not a readable TIFF: {e}")))?;
    let mut pages = Vec::new();
    let mut index = 0usize;

    loop {
        if cfg.wants(index) {
            let (w, h) = decoder
                .dimensions()
                .map_err(|e| Error::Unsupported(format!("TIFF page {}: {e}", index + 1)))?;
            let color = decoder
                .colortype()
                .map_err(|e| Error::Unsupported(format!("TIFF page {}: {e}", index + 1)))?;
            let decoded = decoder
                .read_image()
                .map_err(|e| Error::Unsupported(format!("TIFF page {}: {e}", index + 1)))?;
            let image = tiff_to_rgb(w, h, color, decoded).ok_or_else(|| {
                Error::Unsupported(format!(
                    "TIFF page {} uses an unsupported pixel layout ({color:?})",
                    index + 1
                ))
            })?;
            pages.push(RawPage {
                index,
                image,
                origin: PageOrigin::TiffFrame,
            });
        }
        index += 1;
        if !decoder.more_images() {
            break;
        }
        decoder
            .next_image()
            .map_err(|e| Error::Unsupported(format!("TIFF page {}: {e}", index + 1)))?;
    }
    Ok(pages)
}

/// Converts one decoded TIFF page into RGB8.
fn tiff_to_rgb(
    w: u32,
    h: u32,
    color: tiff::ColorType,
    decoded: tiff::decoder::DecodingResult,
) -> Option<RgbImage> {
    use tiff::decoder::DecodingResult;
    use tiff::ColorType;

    let pixels = (w as usize).checked_mul(h as usize)?;
    // 16-bit samples are scaled down; OCR does not benefit from the extra depth.
    let bytes: Vec<u8> = match decoded {
        DecodingResult::U8(v) => v,
        DecodingResult::U16(v) => v.into_iter().map(|s| (s >> 8) as u8).collect(),
        _ => return None,
    };

    let mut out = Vec::with_capacity(pixels * 3);
    match color {
        ColorType::Gray(_) => {
            if bytes.len() < pixels {
                return None;
            }
            for &g in &bytes[..pixels] {
                out.extend_from_slice(&[g, g, g]);
            }
        }
        ColorType::GrayA(_) => {
            if bytes.len() < pixels * 2 {
                return None;
            }
            for px in bytes[..pixels * 2].chunks_exact(2) {
                out.extend_from_slice(&[px[0], px[0], px[0]]);
            }
        }
        ColorType::RGB(_) => {
            if bytes.len() < pixels * 3 {
                return None;
            }
            out.extend_from_slice(&bytes[..pixels * 3]);
        }
        ColorType::RGBA(_) => {
            if bytes.len() < pixels * 4 {
                return None;
            }
            for px in bytes[..pixels * 4].chunks_exact(4) {
                out.extend_from_slice(&px[..3]);
            }
        }
        ColorType::CMYK(_) => {
            if bytes.len() < pixels * 4 {
                return None;
            }
            for px in bytes[..pixels * 4].chunks_exact(4) {
                let k = px[3] as u32;
                let conv = |c: u8| ((255 - c) as u32 * k / 255) as u8;
                out.extend_from_slice(&[conv(px[0]), conv(px[1]), conv(px[2])]);
            }
        }
        _ => return None,
    }
    RgbImage::from_raw(w, h, out)
}

/// True when `path` looks like something [`load`] can read.
pub fn is_supported_path(path: &Path) -> bool {
    const EXTS: &[&str] = &[
        "pdf", "png", "jpg", "jpeg", "jpe", "jfif", "webp", "tif", "tiff", "bmp", "gif", "pnm",
        "pbm", "pgm", "ppm", "tga", "dds", "hdr", "exr", "qoi", "ico", "avif",
    ];
    path.extension()
        .and_then(|e| e.to_str())
        .map(|e| e.to_ascii_lowercase())
        .is_some_and(|e| EXTS.contains(&e.as_str()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn png_bytes(w: u32, h: u32) -> Vec<u8> {
        let img = RgbImage::from_pixel(w, h, image::Rgb([200, 100, 50]));
        let mut out = Vec::new();
        DynamicImage::ImageRgb8(img)
            .write_to(&mut Cursor::new(&mut out), image::ImageFormat::Png)
            .unwrap();
        out
    }

    #[test]
    fn sniffs_containers() {
        assert_eq!(sniff(b"%PDF-1.7\n..."), Container::Pdf);
        assert_eq!(sniff(b"II*\0rest"), Container::Tiff);
        assert_eq!(sniff(&png_bytes(2, 2)), Container::OtherImage);
    }

    #[test]
    fn loads_a_png_from_bytes() {
        let src = Source::bytes(png_bytes(8, 4), "test.png");
        let pages = load(&src, &IngestConfig::default()).unwrap();
        assert_eq!(pages.len(), 1);
        assert_eq!(pages[0].image.dimensions(), (8, 4));
        assert_eq!(pages[0].origin, PageOrigin::Image);
    }

    #[test]
    fn page_filter_selects_pages() {
        let cfg = IngestConfig {
            pages: Some(vec![1, 2]),
            ..Default::default()
        };
        assert!(!cfg.wants(0));
        assert!(cfg.wants(2));
        let src = Source::bytes(png_bytes(4, 4), "x.png");
        assert!(load(&src, &cfg).unwrap().is_empty());
    }

    #[test]
    fn garbage_input_is_reported_as_unsupported() {
        let src = Source::bytes(vec![0u8; 64], "junk.bin");
        let err = load(&src, &IngestConfig::default()).unwrap_err();
        assert!(matches!(err, Error::Unsupported(_)), "{err}");
    }

    #[test]
    fn extension_filter_covers_common_formats() {
        assert!(is_supported_path(Path::new("a/b/c.PDF")));
        assert!(is_supported_path(Path::new("x.jpeg")));
        assert!(!is_supported_path(Path::new("x.docx")));
    }

    #[test]
    fn multipage_tiff_yields_every_frame() {
        // Build a two-frame TIFF with the `image` encoder.
        let mut buf = Vec::new();
        {
            let enc = image::codecs::tiff::TiffEncoder::new(Cursor::new(&mut buf));
            use image::ImageEncoder;
            enc.write_image(
                RgbImage::from_pixel(6, 6, image::Rgb([10, 10, 10])).as_raw(),
                6,
                6,
                image::ExtendedColorType::Rgb8,
            )
            .unwrap();
        }
        let pages = load(&Source::bytes(buf, "t.tiff"), &IngestConfig::default()).unwrap();
        assert!(!pages.is_empty());
        assert_eq!(pages[0].image.dimensions(), (6, 6));
    }
}
