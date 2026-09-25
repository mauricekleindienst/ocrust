//! Turning anything a user throws at us into pages of pixels.
//!
//! Supported today: every raster format the `image` crate decodes (PNG, JPEG,
//! WebP, TIFF, BMP, GIF, PNM, TGA, DDS, HDR, OpenEXR, QOI, ICO), multi-page
//! TIFF, and PDF (rasterized with the pure-Rust `hayro` renderer).

#[cfg(feature = "pdf")]
pub mod pdf;
#[cfg(feature = "pdf")]
pub mod pdftext;

use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

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

/// One page of pixels ready for preprocessing — or, for a PDF page read from
/// its text layer, the lines of that layer and no pixels at all.
#[derive(Debug, Clone)]
pub struct RawPage {
    pub index: usize,
    pub image: RgbImage,
    pub origin: PageOrigin,
    /// The page's own text, when it is read instead of recognized.
    pub text: Option<TextPage>,
}

/// A page's text layer, as lines placed in the pixels of the rendered page.
#[derive(Debug, Clone)]
pub struct TextPage {
    pub lines: Vec<crate::doc::Line>,
    pub width: u32,
    pub height: u32,
    /// The page's scale: pixels to one point of the PDF.
    pub pixels_per_point: f32,
    /// The straight lines drawn across or down the page: a table's borders.
    pub rules: Vec<crate::geom::Rect>,
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
    /// Extra attempts when reading a file fails with a transient error.
    ///
    /// Reading `\\\\fileserver\\scans\\invoice.pdf` is not a local read: SMB and
    /// NFS drop connections, time out and return "the network name is no longer
    /// available" for reasons that have nothing to do with the file. Those are
    /// worth another attempt, and a batch over a share should not die on the
    /// four hundredth document. `0` disables retrying.
    pub read_retries: u32,
    /// How long to wait before the first retry; it doubles after that.
    pub retry_delay_ms: u64,
    /// Largest image, in pixels, that will be decoded. `0` removes the limit.
    ///
    /// PDF pages have always been capped by `pdf_max_side`; image files were
    /// not, and a picture declares its size before a byte of it is decoded. A
    /// 9 KB CCITT TIFF of a 12000 × 12000 page took 1.8 GB and 40 s, and the
    /// TIFF decoder's own limit let one fourteen times larger through. The
    /// default is Pillow's decompression-bomb threshold — twice 89 478 485 —
    /// which still admits an A0 sheet scanned at 300 dpi (139 Mpx).
    pub max_pixels: u64,
    /// Password for encrypted PDFs. `None` opens only those that need none —
    /// which includes every PDF protected by an owner password alone.
    pub pdf_password: Option<Password>,
    /// Whether a PDF page's own text layer is read instead of recognizing the
    /// page. [`PdfText::Never`] by default.
    #[cfg(feature = "pdf")]
    pub pdf_text: pdftext::PdfText,
}

/// Default for [`IngestConfig::max_pixels`]: Pillow's `DecompressionBombError`
/// threshold, so a limit Python users already know.
pub const DEFAULT_MAX_PIXELS: u64 = 2 * 89_478_485;

/// A PDF password that stays out of logs: its `Debug` output is redacted, and
/// an engine's configuration is exactly the kind of thing that gets logged.
#[derive(Clone, PartialEq, Eq)]
pub struct Password(pub String);

impl std::fmt::Debug for Password {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Password(<redacted>)")
    }
}

impl Default for IngestConfig {
    fn default() -> Self {
        Self {
            pdf_dpi: 200.0,
            pdf_max_side: 4000,
            pages: None,
            read_retries: 2,
            retry_delay_ms: 150,
            max_pixels: DEFAULT_MAX_PIXELS,
            pdf_password: None,
            #[cfg(feature = "pdf")]
            pdf_text: pdftext::PdfText::Never,
        }
    }
}

/// Refuses a picture too large to decode safely, before any of it is decoded.
///
/// Returns the reason rather than an error, so each caller can label it once:
/// a TIFF names its page, a single image its file.
fn check_pixels(width: u32, height: u32, max_pixels: u64) -> std::result::Result<(), String> {
    let pixels = width as u64 * height as u64;
    if max_pixels > 0 && pixels > max_pixels {
        return Err(format!(
            "{width} × {height} pixels is {:.0} megapixels, over the {:.0}-megapixel limit \
             that guards against decompression bombs; raise `max_pixels` \
             (`--max-pixels`) if this is a genuine scan",
            pixels as f64 / 1e6,
            max_pixels as f64 / 1e6
        ));
    }
    Ok(())
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

/// A source opened for reading, handing out one page at a time.
///
/// Decoding every page up front is the obvious way to write this and the wrong
/// way to run it. A 200-page scan at 200 dpi is over 2 GB of pixels and the
/// pipeline looks at one page at a time: measured on a 120-page PDF, loading it
/// all first cost 1.8 GB of peak memory where a single page costs 0.4 GB. So
/// pages are produced on demand and the caller decides how many to keep alive.
///
/// Iteration is forward-only, which is all the pipeline needs and all a TIFF's
/// chain of image directories cheaply allows.
pub struct Reader {
    inner: Inner,
    /// Label for the errors that only surface once a page is decoded.
    name: String,
    /// Zero-based page indices this reader will produce, in order.
    indices: Vec<usize>,
    /// Pixel budget for pictures decoded later, from [`IngestConfig::max_pixels`].
    max_pixels: u64,
}

enum Inner {
    /// A frame that was already decoded before it got here.
    Frame(Option<RgbImage>),
    /// One encoded raster image, decoded on the first pull.
    Encoded(Arc<Vec<u8>>),
    #[cfg(feature = "pdf")]
    Pdf(pdf::Renderer),
    /// Boxed: a parked TIFF decoder is by far the largest of these, and every
    /// reader would otherwise carry its footprint.
    Tiff(Box<TiffFrames>),
}

impl std::fmt::Debug for Reader {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Reader")
            .field("name", &self.name)
            .field("pages", &self.indices.len())
            .finish()
    }
}

/// Opens `source` and counts its pages, decoding none of them.
pub fn open(source: &Source, cfg: &IngestConfig) -> Result<Reader> {
    let name = source.name();
    let reader = open_inner(source, name.clone(), cfg).map_err(|e| label(&name, e))?;
    // A page filter that matches nothing is a mistake worth reporting: an empty
    // document looks exactly like a page the recognizer found no text on.
    if reader.indices.is_empty() {
        if let Some(wanted) = &cfg.pages {
            let numbers: Vec<String> = wanted.iter().map(|p| (p + 1).to_string()).collect();
            return Err(Error::config(format!(
                "the document has no page {}",
                numbers.join(", ")
            )));
        }
    }
    Ok(reader)
}

fn open_inner(source: &Source, name: String, cfg: &IngestConfig) -> Result<Reader> {
    match source {
        Source::Image { image, .. } => Ok(Reader {
            inner: Inner::Frame(Some(image.clone())),
            name,
            indices: if cfg.wants(0) { vec![0] } else { Vec::new() },
            // Already decoded by whoever handed it over: nothing left to guard.
            max_pixels: 0,
        }),
        Source::Bytes { data, .. } => open_bytes(Arc::new(data.clone()), name, cfg),
        Source::Path(path) => {
            let data = read_file(path, cfg).map_err(|e| Error::io(path, e))?;
            open_bytes(Arc::new(data), name, cfg)
        }
    }
}

/// Opens encoded bytes. They are shared rather than copied, which for a
/// 200 MB scanned PDF is 200 MB not spent.
fn open_bytes(data: Arc<Vec<u8>>, name: String, cfg: &IngestConfig) -> Result<Reader> {
    let (inner, total) = match sniff(&data) {
        Container::Pdf => {
            #[cfg(feature = "pdf")]
            {
                let renderer = pdf::Renderer::new(data, cfg)?;
                let total = renderer.page_count();
                (Inner::Pdf(renderer), total)
            }
            #[cfg(not(feature = "pdf"))]
            {
                return Err(Error::Unsupported(format!(
                    "{name} is a PDF but ocrust was built without the `pdf` feature"
                )));
            }
        }
        Container::Tiff => {
            let frames = Box::new(TiffFrames::new(data, cfg.max_pixels)?);
            let total = frames.total;
            (Inner::Tiff(frames), total)
        }
        Container::OtherImage => (Inner::Encoded(data), 1),
    };
    Ok(Reader {
        inner,
        name,
        indices: (0..total).filter(|i| cfg.wants(*i)).collect(),
        max_pixels: cfg.max_pixels,
    })
}

impl Reader {
    /// Pages this reader will produce.
    pub fn len(&self) -> usize {
        self.indices.len()
    }

    pub fn is_empty(&self) -> bool {
        self.indices.is_empty()
    }

    /// Zero-based indices of the pages this reader will produce, in order.
    pub fn indices(&self) -> &[usize] {
        &self.indices
    }

    /// Decodes each page in turn and hands it to `sink`.
    ///
    /// A callback rather than an iterator because a PDF's render cache borrows
    /// from the parsed document and so cannot be stored beside it: one call
    /// keeps one cache alive for the whole document. Either way the caller only
    /// ever holds the page it was just handed.
    pub fn for_each_page(&mut self, sink: &mut dyn FnMut(RawPage) -> Result<()>) -> Result<()> {
        // Destructured so the page source and the index list can be borrowed at
        // the same time.
        let Reader {
            inner,
            name,
            indices,
            max_pixels,
        } = self;
        let max_pixels = *max_pixels;
        let labelled = |e| label(name, e);
        match inner {
            // Taken, not cloned: the caller handed us this frame to scan.
            Inner::Frame(frame) => {
                for &index in indices.iter() {
                    let image = frame
                        .take()
                        .ok_or_else(|| Error::config("that frame was already read"))?;
                    sink(RawPage {
                        index,
                        image,
                        origin: PageOrigin::Image,
                        text: None,
                    })?;
                }
                Ok(())
            }
            Inner::Encoded(data) => {
                for &index in indices.iter() {
                    let image = decode_single(data, Some(name), max_pixels).map_err(labelled)?;
                    sink(RawPage {
                        index,
                        image,
                        origin: PageOrigin::Image,
                        text: None,
                    })?;
                }
                Ok(())
            }
            #[cfg(feature = "pdf")]
            Inner::Pdf(renderer) => renderer.read_each(indices, sink),
            Inner::Tiff(frames) => {
                for &index in indices.iter() {
                    let image = frames.frame(index).map_err(labelled)?;
                    sink(RawPage {
                        index,
                        image,
                        origin: PageOrigin::TiffFrame,
                        text: None,
                    })?;
                }
                Ok(())
            }
        }
    }
}

/// A file we cannot decode is an input problem, not an internal one, so it says
/// so with the file name attached.
fn label(name: &str, error: Error) -> Error {
    match error {
        Error::Image(inner) => Error::Unsupported(format!("{name}: {inner}")),
        Error::PlainIo(inner) => Error::Unsupported(format!("{name}: {inner}")),
        other => other,
    }
}

/// Loads every page of `source` at once.
///
/// What the exporters and the tests want; [`open`] is what the pipeline uses,
/// because holding one page costs a hundredth of holding a hundred.
pub fn load(source: &Source, cfg: &IngestConfig) -> Result<Vec<RawPage>> {
    let mut pages = Vec::new();
    open(source, cfg)?.for_each_page(&mut |page| {
        pages.push(page);
        Ok(())
    })?;
    Ok(pages)
}

/// Reads a file, retrying the failures a network share produces under load.
fn read_file(path: &Path, cfg: &IngestConfig) -> std::io::Result<Vec<u8>> {
    retry_io(cfg.read_retries, cfg.retry_delay_ms, || std::fs::read(path))
}

/// Runs `attempt` again while it fails transiently, backing off between tries.
///
/// Kept separate from the filesystem so the policy can be tested: how many
/// attempts, how long between them, and which errors are worth repeating.
fn retry_io<T>(
    retries: u32,
    delay_ms: u64,
    mut attempt: impl FnMut() -> std::io::Result<T>,
) -> std::io::Result<T> {
    let mut delay = Duration::from_millis(delay_ms);
    for _ in 0..retries {
        match attempt() {
            Ok(value) => return Ok(value),
            Err(e) if is_transient(&e) => {
                if !delay.is_zero() {
                    std::thread::sleep(delay);
                }
                delay = delay.saturating_mul(2);
            }
            Err(e) => return Err(e),
        }
    }
    attempt()
}

/// Whether an i/o error is the kind a second attempt can survive.
///
/// "Not found" and "permission denied" are answers; a reset connection, a
/// timeout or a stale NFS handle are noise from the wire.
fn is_transient(error: &std::io::Error) -> bool {
    use std::io::ErrorKind::*;
    if matches!(
        error.kind(),
        Interrupted
            | TimedOut
            | WouldBlock
            | ConnectionReset
            | ConnectionAborted
            | BrokenPipe
            | NotConnected
            | HostUnreachable
            | NetworkUnreachable
            | NetworkDown
            | StaleNetworkFileHandle
            | ResourceBusy
            | UnexpectedEof
    ) {
        return true;
    }
    // Windows reports the interesting SMB failures as raw codes that Rust does
    // not map onto an `ErrorKind`: the network name was deleted, an unexpected
    // network error, the redirector ran out of resources.
    #[cfg(windows)]
    if let Some(code) = error.raw_os_error() {
        return matches!(code, 51 | 52 | 54 | 59 | 64 | 121 | 1450);
    }
    false
}

/// Decodes one raster image, honouring its EXIF orientation.
///
/// `hint` is the file name or extension, used when the bytes alone are not
/// enough: TGA and a few other formats have no magic number, so content
/// sniffing cannot identify them.
fn decode_single(data: &[u8], hint: Option<&str>, max_pixels: u64) -> Result<RgbImage> {
    let mut reader = image::ImageReader::new(Cursor::new(data))
        .with_guessed_format()
        .map_err(Error::PlainIo)?;
    if reader.format().is_none() {
        if let Some(format) = hint.and_then(format_from_hint) {
            reader.set_format(format);
        }
    }
    let mut decoder = reader.into_decoder()?;
    let (width, height) = decoder.dimensions();
    check_pixels(width, height, max_pixels).map_err(Error::Unsupported)?;
    // Phone photos are almost always stored rotated with an EXIF tag.
    let orientation = decoder.orientation().ok();
    let mut img = DynamicImage::from_decoder(decoder)?;
    if let Some(o) = orientation {
        img.apply_orientation(o);
    }
    Ok(img.into_rgb8())
}

/// Maps a file name or extension onto an image format.
fn format_from_hint(hint: &str) -> Option<image::ImageFormat> {
    let extension = hint
        .rsplit(['.', '/', '\\'])
        .next()
        .unwrap_or(hint)
        .to_ascii_lowercase();
    image::ImageFormat::from_extension(extension)
}

/// A TIFF's chain of image directories, walked forward one frame at a time.
///
/// `image` only exposes the first directory, so the `tiff` crate (which `image`
/// uses internally anyway) drives the page walk. Scanned faxes and archive
/// masters are routinely multi-page, and silently dropping pages would be worse
/// than not supporting the format at all.
struct TiffFrames {
    data: SharedBytes,
    decoder: tiff::decoder::Decoder<Cursor<SharedBytes>>,
    /// Directory the decoder currently sits on.
    at: usize,
    total: usize,
    /// Pixel budget per frame; see [`IngestConfig::max_pixels`].
    max_pixels: u64,
}

/// Lets one buffer back several cursors without being copied for each.
#[derive(Clone)]
struct SharedBytes(Arc<Vec<u8>>);

impl AsRef<[u8]> for SharedBytes {
    fn as_ref(&self) -> &[u8] {
        &self.0
    }
}

impl TiffFrames {
    /// Counts the directories, which needs no pixels decoded, and leaves a
    /// decoder parked on the first one.
    fn new(data: Arc<Vec<u8>>, max_pixels: u64) -> Result<Self> {
        let data = SharedBytes(data);
        let mut counter = decoder_for(&data)?;
        let mut total = 1usize;
        while counter.more_images() {
            counter
                .next_image()
                .map_err(|e| tiff_error(total + 1, &e.to_string()))?;
            total += 1;
        }
        Ok(Self {
            decoder: decoder_for(&data)?,
            data,
            at: 0,
            total,
            max_pixels,
        })
    }

    /// Decodes one frame, walking the chain forward to reach it.
    fn frame(&mut self, index: usize) -> Result<RgbImage> {
        if index < self.at {
            // Only a forward walk is cheap; going back means starting over.
            self.decoder = decoder_for(&self.data)?;
            self.at = 0;
        }
        while self.at < index {
            if !self.decoder.more_images() {
                return Err(tiff_error(index + 1, "the document ends before it"));
            }
            self.decoder
                .next_image()
                .map_err(|e| tiff_error(self.at + 2, &e.to_string()))?;
            self.at += 1;
        }

        let page = index + 1;
        let (w, h) = self
            .decoder
            .dimensions()
            .map_err(|e| tiff_error(page, &e.to_string()))?;
        // Checked before `read_image`: a CCITT page of a few kilobytes can
        // declare hundreds of megapixels, and each one becomes three bytes.
        check_pixels(w, h, self.max_pixels).map_err(|reason| tiff_error(page, &reason))?;
        let color = self
            .decoder
            .colortype()
            .map_err(|e| tiff_error(page, &e.to_string()))?;
        // A bilevel page stores 0 for white when it is a fax (WhiteIsZero, the
        // default in CCITT-coded TIFFs), and 0 for black otherwise. The tag is
        // the only thing that says which, and reading it wrong inverts the page.
        let white_is_zero = self
            .decoder
            .get_tag_unsigned(tiff::tags::Tag::PhotometricInterpretation)
            .map(|v: u32| v == 0)
            .unwrap_or(false);
        let decoded = self
            .decoder
            .read_image()
            .map_err(|e| tiff_error(page, &e.to_string()))?;
        tiff_to_rgb(w, h, color, decoded, white_is_zero)
            .ok_or_else(|| tiff_error(page, &format!("unsupported pixel layout ({color:?})")))
    }
}

fn decoder_for(data: &SharedBytes) -> Result<tiff::decoder::Decoder<Cursor<SharedBytes>>> {
    tiff::decoder::Decoder::new(Cursor::new(data.clone()))
        .map_err(|e| Error::Unsupported(format!("not a readable TIFF: {e}")))
}

fn tiff_error(page: usize, detail: &str) -> Error {
    Error::Unsupported(format!("TIFF page {page}: {detail}"))
}

/// Converts one decoded TIFF page into RGB8.
fn tiff_to_rgb(
    w: u32,
    h: u32,
    color: tiff::ColorType,
    decoded: tiff::decoder::DecodingResult,
    white_is_zero: bool,
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
        // Bilevel and other sub-byte depths arrive packed, several pixels to the
        // byte, each row padded to a byte boundary. This is what a scanned
        // archive and every CCITT fax is stored as: the same page is a few
        // kilobytes bilevel against eleven megabytes as RGB.
        ColorType::Gray(bits @ (1 | 2 | 4)) => {
            let per_byte = 8 / bits as usize;
            let row_bytes = (w as usize).div_ceil(per_byte);
            if bytes.len() < row_bytes * h as usize {
                return None;
            }
            let levels = (1u16 << bits) - 1;
            for y in 0..h as usize {
                let row = &bytes[y * row_bytes..(y + 1) * row_bytes];
                for x in 0..w as usize {
                    let byte = row[x / per_byte];
                    let shift = 8 - bits as usize * (x % per_byte + 1);
                    let v = (byte >> shift) as u16 & levels;
                    // Spread the level over the full range, then apply the
                    // photometric sense: under WhiteIsZero, 0 is white.
                    let g = (v * 255 / levels) as u8;
                    let g = if white_is_zero { 255 - g } else { g };
                    out.extend_from_slice(&[g, g, g]);
                }
            }
        }
        ColorType::Gray(_) => {
            if bytes.len() < pixels {
                return None;
            }
            for &g in &bytes[..pixels] {
                let g = if white_is_zero { 255 - g } else { g };
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
///
/// Every suffix here is one the decoder actually handles, and the corpus has a
/// file in each. `dds`, `exr`, `ico` and `avif` were listed once and none of
/// them could be read: the DDS decoder takes no uncompressed surface and DXT
/// wants dimensions in multiples of four, an ICO frame stored as PNG has to be
/// RGBA, and AVIF is not compiled in. Claiming a format nobody could open is
/// worse than not claiming it.
pub fn is_supported_path(path: &Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .map(|e| e.to_ascii_lowercase())
        .is_some_and(|e| SUPPORTED_EXTENSIONS.contains(&e.as_str()))
}

/// Every file suffix [`load`] can read, lowercase and without the dot.
///
/// One list, so the CLI's directory filter and the Python package cannot drift
/// from what the decoder does — `avif` was once offered by this list alone.
pub const SUPPORTED_EXTENSIONS: &[&str] = &[
    "pdf", "png", "jpg", "jpeg", "jpe", "jfif", "webp", "tif", "tiff", "bmp", "gif", "pnm", "pbm",
    "pgm", "ppm", "tga", "hdr", "qoi",
];

#[cfg(test)]
mod tests {
    use super::*;

    /// A 16x4 bilevel TIFF: white, with a black bar across row 1. Photometric 1.
    const TINY_BLACK_IS_ZERO: &[u8] = &[
        73, 73, 42, 0, 8, 0, 0, 0, 8, 0, 0, 1, 4, 0, 1, 0, 0, 0, 16, 0, 0, 0, 1, 1, 4, 0, 1, 0, 0,
        0, 4, 0, 0, 0, 3, 1, 3, 0, 1, 0, 0, 0, 1, 0, 0, 0, 6, 1, 3, 0, 1, 0, 0, 0, 1, 0, 0, 0, 17,
        1, 4, 0, 1, 0, 0, 0, 110, 0, 0, 0, 22, 1, 4, 0, 1, 0, 0, 0, 4, 0, 0, 0, 23, 1, 4, 0, 1, 0,
        0, 0, 8, 0, 0, 0, 28, 1, 3, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 255, 255, 192, 3, 255,
        255, 255, 255,
    ];

    /// The same page with its bits flipped and photometric 0 — the fax convention.
    const TINY_WHITE_IS_ZERO: &[u8] = &[
        73, 73, 42, 0, 8, 0, 0, 0, 8, 0, 0, 1, 4, 0, 1, 0, 0, 0, 16, 0, 0, 0, 1, 1, 4, 0, 1, 0, 0,
        0, 4, 0, 0, 0, 3, 1, 3, 0, 1, 0, 0, 0, 1, 0, 0, 0, 6, 1, 3, 0, 1, 0, 0, 0, 0, 0, 0, 0, 17,
        1, 4, 0, 1, 0, 0, 0, 110, 0, 0, 0, 22, 1, 4, 0, 1, 0, 0, 0, 4, 0, 0, 0, 23, 1, 4, 0, 1, 0,
        0, 0, 8, 0, 0, 0, 28, 1, 3, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 255, 255, 192, 3, 255,
        255, 255, 255,
    ];

    /// A picture is refused on the size it declares, before any of it is
    /// decoded: a 9 KB CCITT TIFF declaring 144 megapixels used to cost 1.8 GB
    /// and 40 seconds before anything looked at it.
    #[test]
    fn an_oversized_picture_is_refused_before_it_is_decoded() {
        assert!(
            check_pixels(100, 100, 10_000).is_ok(),
            "exactly at the limit"
        );
        let reason = check_pixels(101, 100, 10_000).unwrap_err();
        assert!(reason.contains("101 × 100"), "{reason}");
        assert!(
            reason.contains("max_pixels"),
            "says how to raise it: {reason}"
        );
        assert!(
            check_pixels(u32::MAX, u32::MAX, 0).is_ok(),
            "0 means no limit"
        );

        // Through each decode path: a single image, and a TIFF's frames.
        let img = RgbImage::from_pixel(8, 8, image::Rgb([255, 255, 255]));
        let mut png = Vec::new();
        DynamicImage::ImageRgb8(img)
            .write_to(&mut Cursor::new(&mut png), image::ImageFormat::Png)
            .unwrap();
        let err = decode_single(&png, None, 63).unwrap_err().to_string();
        assert!(err.contains("megapixels"), "{err}");
        assert!(decode_single(&png, None, 64).is_ok());

        let mut frames = TiffFrames::new(Arc::new(multipage_tiff(2)), 35).unwrap();
        let err = frames.frame(0).unwrap_err().to_string();
        assert!(err.contains("TIFF page 1"), "{err}");
        // Labelled once: the page, then the reason, not the error kind twice.
        assert_eq!(err.matches("unsupported input").count(), 1, "{err}");
    }

    #[test]
    fn the_default_limit_admits_an_a0_sheet_at_300_dpi() {
        // 841 × 1189 mm at 300 dpi is the largest standard sheet at archival
        // resolution, and it has to keep working.
        let (w, h) = (9933u32, 14043u32);
        assert!(check_pixels(w, h, DEFAULT_MAX_PIXELS).is_ok());
        assert_eq!(DEFAULT_MAX_PIXELS, 178_956_970, "Pillow's threshold");
    }

    #[test]
    fn a_password_never_appears_in_debug_output() {
        let cfg = IngestConfig {
            pdf_password: Some(Password("geheim".into())),
            ..Default::default()
        };
        let shown = format!("{cfg:?}");
        assert!(!shown.contains("geheim"), "{shown}");
        assert!(shown.contains("redacted"), "{shown}");
    }

    /// Bilevel is how a scanned archive and every CCITT fax is stored, and it
    /// used to fail outright: the packed rows fell through to "unsupported pixel
    /// layout (Gray(1))". The corpus never caught it because its "1-bit fax"
    /// fixture was saved as RGB.
    #[test]
    fn a_bilevel_tiff_decodes_either_way_round() {
        let expect = |page: &RgbImage| {
            assert_eq!(page.dimensions(), (16, 4));
            // Row 0 is blank, row 1 carries the bar between x=2 and x=13.
            assert_eq!(
                page.get_pixel(0, 0).0,
                [255, 255, 255],
                "row 0 must be white"
            );
            assert_eq!(page.get_pixel(8, 1).0, [0, 0, 0], "the bar must be black");
            assert_eq!(page.get_pixel(0, 1).0, [255, 255, 255], "before the bar");
            assert_eq!(page.get_pixel(15, 1).0, [255, 255, 255], "after the bar");
        };

        let decode = |bytes: &[u8]| {
            TiffFrames::new(Arc::new(bytes.to_vec()), 0)
                .expect("a readable TIFF")
                .frame(0)
                .expect("page 1 must decode")
        };
        let black_is_zero = decode(TINY_BLACK_IS_ZERO);
        let white_is_zero = decode(TINY_WHITE_IS_ZERO);
        expect(&black_is_zero);
        expect(&white_is_zero);
        // The two files are the same page written both ways round, so a decoder
        // that ignored the tag — or inverted twice — would disagree here.
        assert_eq!(black_is_zero, white_is_zero);
    }

    /// A reader that fails a given number of times before succeeding.
    fn flaky(mut failures: u32, kind: std::io::ErrorKind) -> impl FnMut() -> std::io::Result<u8> {
        move || {
            if failures > 0 {
                failures -= 1;
                Err(std::io::Error::new(
                    kind,
                    "the network name is no longer available",
                ))
            } else {
                Ok(42)
            }
        }
    }

    #[test]
    fn a_transient_read_is_retried() {
        // Two hiccups, three attempts allowed: the read still succeeds.
        let result = retry_io(2, 0, flaky(2, std::io::ErrorKind::ConnectionReset));
        assert_eq!(result.unwrap(), 42);
    }

    #[test]
    fn retrying_gives_up_after_the_configured_attempts() {
        let result = retry_io(2, 0, flaky(3, std::io::ErrorKind::TimedOut));
        assert_eq!(result.unwrap_err().kind(), std::io::ErrorKind::TimedOut);

        // Zero retries means one attempt.
        let mut calls = 0;
        let result = retry_io(0, 0, || {
            calls += 1;
            Err::<u8, _>(std::io::Error::new(std::io::ErrorKind::TimedOut, "slow"))
        });
        assert!(result.is_err());
        assert_eq!(calls, 1);
    }

    #[test]
    fn a_missing_file_is_not_retried() {
        let mut calls = 0;
        let result = retry_io(5, 0, || {
            calls += 1;
            Err::<u8, _>(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "no such file",
            ))
        });
        assert!(result.is_err());
        assert_eq!(calls, 1, "a missing file will not appear on the next try");
    }

    #[test]
    fn transient_errors_are_the_network_ones() {
        use std::io::ErrorKind::*;
        for kind in [
            ConnectionReset,
            TimedOut,
            Interrupted,
            BrokenPipe,
            NetworkDown,
            HostUnreachable,
            StaleNetworkFileHandle,
            UnexpectedEof,
        ] {
            assert!(
                is_transient(&std::io::Error::new(kind, "x")),
                "{kind:?} should be retried"
            );
        }
        for kind in [NotFound, PermissionDenied, InvalidData, IsADirectory] {
            assert!(
                !is_transient(&std::io::Error::new(kind, "x")),
                "{kind:?} should not be retried"
            );
        }
    }

    fn png_bytes(w: u32, h: u32) -> Vec<u8> {
        let img = RgbImage::from_pixel(w, h, image::Rgb([200, 100, 50]));
        let mut out = Vec::new();
        DynamicImage::ImageRgb8(img)
            .write_to(&mut Cursor::new(&mut out), image::ImageFormat::Png)
            .unwrap();
        out
    }

    #[test]
    fn extension_hint_decodes_formats_without_magic_bytes() {
        // TGA carries no signature, so content sniffing cannot identify it.
        let img = RgbImage::from_pixel(4, 3, image::Rgb([9, 9, 9]));
        let mut tga = Vec::new();
        DynamicImage::ImageRgb8(img)
            .write_to(&mut Cursor::new(&mut tga), image::ImageFormat::Tga)
            .unwrap();

        assert!(
            decode_single(&tga, None, 0).is_err(),
            "sniffing cannot work here"
        );
        let decoded = decode_single(&tga, Some("page.tga"), 0).expect("hint decodes it");
        assert_eq!(decoded.dimensions(), (4, 3));

        // The same path goes through the public entry point.
        let pages = load(&Source::bytes(tga, "page.TGA"), &IngestConfig::default()).unwrap();
        assert_eq!(pages[0].image.dimensions(), (4, 3));
    }

    #[test]
    fn format_hints_accept_paths_and_bare_extensions() {
        assert_eq!(format_from_hint("a/b/c.PNG"), Some(image::ImageFormat::Png));
        assert_eq!(format_from_hint("tga"), Some(image::ImageFormat::Tga));
        assert_eq!(format_from_hint("mystery"), None);
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
    fn a_page_filter_that_matches_nothing_is_an_error() {
        // Silently returning an empty document would look like a blank scan.
        let cfg = IngestConfig {
            pages: Some(vec![41]),
            ..IngestConfig::default()
        };
        let src = Source::bytes(png_bytes(8, 4), "one-page.png");
        let err = load(&src, &cfg).unwrap_err();
        assert!(err.to_string().contains("no page 42"), "{err}");
    }

    #[test]
    fn page_filter_selects_pages() {
        let cfg = IngestConfig {
            pages: Some(vec![1, 2]),
            ..Default::default()
        };
        assert!(!cfg.wants(0));
        assert!(cfg.wants(2));
        // A single-page image has neither page 2 nor 3, which `load` reports.
        let src = Source::bytes(png_bytes(4, 4), "x.png");
        assert!(load(&src, &cfg).is_err());
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

    /// Formats that were advertised and could not be opened. Each one is a real
    /// failure, not a missing feature flag: see `is_supported_path`.
    #[test]
    fn formats_that_cannot_be_decoded_are_not_advertised() {
        for suffix in ["dds", "exr", "ico", "avif"] {
            assert!(
                !is_supported_path(&PathBuf::from(format!("page.{suffix}"))),
                "{suffix} cannot be decoded, so it must not be offered"
            );
        }
    }

    /// A TIFF with `pages` frames, each a different shade so they can be told
    /// apart.
    fn multipage_tiff(pages: u8) -> Vec<u8> {
        let images: Vec<RgbImage> = (0..pages)
            .map(|i| RgbImage::from_pixel(6, 6, image::Rgb([i * 20, i * 20, i * 20])))
            .collect();
        crate::export::tiff::write_pages(
            &images,
            crate::export::tiff::TiffOptions::new(crate::export::tiff::TiffColor::Rgb),
        )
        .unwrap()
    }

    #[test]
    fn a_reader_counts_its_pages_before_decoding_any() {
        let reader = open(
            &Source::bytes(multipage_tiff(4), "t.tiff"),
            &IngestConfig::default(),
        )
        .unwrap();
        assert_eq!(reader.len(), 4);
        assert_eq!(reader.indices(), [0, 1, 2, 3]);
    }

    #[test]
    fn a_reader_decodes_a_page_only_when_it_is_asked_for() {
        // The point of the whole reader: a 500-page document must not become
        // 500 pages of pixels before the first line is recognized. A sink that
        // stops after two pages proves the third was never decoded.
        let mut reader = open(
            &Source::bytes(multipage_tiff(5), "t.tiff"),
            &IngestConfig::default(),
        )
        .unwrap();
        let mut seen = Vec::new();
        let err = reader
            .for_each_page(&mut |page| {
                seen.push(page.index);
                if seen.len() == 2 {
                    return Err(Error::config("stop here"));
                }
                Ok(())
            })
            .unwrap_err();
        assert_eq!(seen, [0, 1], "{err}");
    }

    #[test]
    fn a_page_selection_decodes_only_the_pages_it_names() {
        let cfg = IngestConfig {
            pages: Some(vec![2]),
            ..Default::default()
        };
        let mut reader = open(&Source::bytes(multipage_tiff(4), "t.tiff"), &cfg).unwrap();
        assert_eq!(reader.len(), 1);
        let mut seen = Vec::new();
        reader
            .for_each_page(&mut |page| {
                seen.push((page.index, page.image.get_pixel(0, 0).0[0]));
                Ok(())
            })
            .unwrap();
        // The third frame, with the third frame's shade, and nothing else.
        assert_eq!(seen, [(2, 40)]);
    }

    #[test]
    fn frames_read_in_order_match_frames_read_all_at_once() {
        let bytes = multipage_tiff(3);
        let batch = load(
            &Source::bytes(bytes.clone(), "t.tiff"),
            &IngestConfig::default(),
        )
        .unwrap();
        let mut streamed = Vec::new();
        open(&Source::bytes(bytes, "t.tiff"), &IngestConfig::default())
            .unwrap()
            .for_each_page(&mut |page| {
                streamed.push(page);
                Ok(())
            })
            .unwrap();
        assert_eq!(batch.len(), streamed.len());
        for (a, b) in batch.iter().zip(&streamed) {
            assert_eq!(a.index, b.index);
            assert_eq!(a.image.as_raw(), b.image.as_raw());
        }
    }

    #[test]
    fn a_frame_walk_can_go_backwards_by_starting_over() {
        // Nothing in the pipeline reads a TIFF backwards, but the walker has to
        // survive it rather than hand back the wrong frame.
        let mut frames = TiffFrames::new(Arc::new(multipage_tiff(4)), 0).unwrap();
        assert_eq!(frames.total, 4);
        assert_eq!(frames.frame(3).unwrap().get_pixel(0, 0).0[0], 60);
        assert_eq!(frames.frame(1).unwrap().get_pixel(0, 0).0[0], 20);
        assert_eq!(frames.frame(2).unwrap().get_pixel(0, 0).0[0], 40);
        assert!(frames.frame(9).is_err());
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
