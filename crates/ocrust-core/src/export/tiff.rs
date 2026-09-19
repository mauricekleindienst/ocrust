//! Multi-page TIFF output.
//!
//! TIFF is still the lingua franca of document archives and fax workflows, so
//! `ocrust` reads it and writes it: a scanned PDF or a pile of JPEGs can be
//! normalized into one deskewed, ready-to-archive TIFF.

use std::cell::RefCell;
use std::io::{Cursor, Seek, SeekFrom, Write};
use std::rc::Rc;

use image::RgbImage;
use tiff::encoder::{colortype, Compression, TiffEncoder};

use crate::error::{Error, Result};

/// How to store the pages.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum TiffColor {
    /// Full colour, 8 bit per channel.
    #[default]
    Rgb,
    /// Greyscale, which is what most document scanners produce anyway and
    /// roughly a third of the size.
    Gray,
}

/// How the pixels are packed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum TiffCompression {
    /// Lempel-Ziv-Welch, understood by every TIFF reader since 1992 and the
    /// default here: a page of text stores in a fraction of its pixels, and an
    /// uncompressed 40-page archive is half a gigabyte for no reason.
    #[default]
    Lzw,
    /// Deflate, which packs a little tighter than LZW and is a little slower.
    Deflate,
    /// Raw pixels, for a reader that cannot cope with either.
    None,
}

/// How to write the archive.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct TiffOptions {
    pub color: TiffColor,
    pub compression: TiffCompression,
}

impl TiffOptions {
    pub fn new(color: TiffColor) -> Self {
        Self {
            color,
            ..Default::default()
        }
    }
}

/// Writes `pages` as one multi-page TIFF.
///
/// Pages may differ in size; each becomes its own directory (IFD), which is
/// exactly how multi-page scans are stored.
pub fn write_pages(pages: &[RgbImage], opts: TiffOptions) -> Result<Vec<u8>> {
    let mut writer = Writer::new(opts)?;
    for page in pages {
        writer.add(page)?;
    }
    writer.finish()
}

/// Appends pages to a multi-page TIFF as they are produced.
///
/// The batch form needs every page alive at once, which for a 100-page scan is
/// a gigabyte of pixels held only to be encoded one at a time. This takes each
/// page as it comes and lets the caller drop it.
pub struct Writer {
    /// One encoder for the whole file: a TIFF's directories are a chain, and a
    /// second encoder over the same buffer writes a second header instead of
    /// continuing the chain.
    encoder: TiffEncoder<SharedBuffer>,
    /// The same buffer the encoder writes into. `TiffEncoder` does not hand its
    /// writer back, and it cannot borrow a field of this struct, so the two
    /// share one.
    buffer: SharedBuffer,
    opts: TiffOptions,
    pages: usize,
}

/// A byte buffer two owners can write through.
#[derive(Clone, Default)]
struct SharedBuffer(Rc<RefCell<Cursor<Vec<u8>>>>);

impl Write for SharedBuffer {
    fn write(&mut self, data: &[u8]) -> std::io::Result<usize> {
        self.0.borrow_mut().write(data)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.0.borrow_mut().flush()
    }
}

impl Seek for SharedBuffer {
    fn seek(&mut self, to: SeekFrom) -> std::io::Result<u64> {
        self.0.borrow_mut().seek(to)
    }
}

impl std::fmt::Debug for Writer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("tiff::Writer")
            .field("pages", &self.pages)
            .field("bytes", &self.buffer.0.borrow().get_ref().len())
            .finish()
    }
}

impl Writer {
    pub fn new(opts: TiffOptions) -> Result<Self> {
        let buffer = SharedBuffer::default();
        let encoder = TiffEncoder::new(buffer.clone())
            .map_err(|e| Error::Unsupported(format!("TIFF writer: {e}")))?
            .with_compression(match opts.compression {
                TiffCompression::Lzw => Compression::Lzw,
                TiffCompression::Deflate => Compression::Deflate(Default::default()),
                TiffCompression::None => Compression::Uncompressed,
            });
        Ok(Self {
            encoder,
            buffer,
            opts,
            pages: 0,
        })
    }

    /// Appends one page.
    pub fn add(&mut self, page: &RgbImage) -> Result<()> {
        let number = self.pages + 1;
        let (w, h) = page.dimensions();
        if w == 0 || h == 0 {
            return Err(Error::config(format!("page {number} is empty")));
        }
        match self.opts.color {
            TiffColor::Rgb => self
                .encoder
                .write_image::<colortype::RGB8>(w, h, page.as_raw())
                .map_err(|e| Error::Unsupported(format!("TIFF page {number}: {e}")))?,
            TiffColor::Gray => {
                let gray = image::imageops::grayscale(page);
                self.encoder
                    .write_image::<colortype::Gray8>(w, h, gray.as_raw())
                    .map_err(|e| Error::Unsupported(format!("TIFF page {number}: {e}")))?
            }
        }
        self.pages += 1;
        Ok(())
    }

    pub fn pages(&self) -> usize {
        self.pages
    }

    /// Finishes the file.
    pub fn finish(self) -> Result<Vec<u8>> {
        if self.pages == 0 {
            return Err(Error::config("cannot write a TIFF without pages"));
        }
        let Self {
            encoder, buffer, ..
        } = self;
        // The encoder goes first so nothing is still holding the buffer.
        drop(encoder);
        let cell = Rc::try_unwrap(buffer.0)
            .map_err(|_| Error::config("the TIFF buffer is still shared"))?;
        Ok(cell.into_inner().into_inner())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ingest::{self, IngestConfig, Source};

    fn page(w: u32, h: u32, shade: u8) -> RgbImage {
        RgbImage::from_pixel(w, h, image::Rgb([shade, shade / 2, 0]))
    }

    #[test]
    fn writes_a_multipage_tiff_we_can_read_back() {
        let pages = vec![page(40, 30, 200), page(20, 50, 100), page(10, 10, 10)];
        let bytes = write_pages(&pages, TiffOptions::new(TiffColor::Rgb)).unwrap();
        assert!(bytes.starts_with(b"II*\0") || bytes.starts_with(b"MM\0*"));

        // Round-trip through our own ingest path.
        let read =
            ingest::load(&Source::bytes(bytes, "out.tiff"), &IngestConfig::default()).unwrap();
        assert_eq!(read.len(), 3);
        assert_eq!(read[0].image.dimensions(), (40, 30));
        assert_eq!(read[1].image.dimensions(), (20, 50));
        assert_eq!(read[2].image.dimensions(), (10, 10));
        assert_eq!(read[0].image.get_pixel(0, 0), &image::Rgb([200, 100, 0]));
    }

    #[test]
    fn grayscale_output_round_trips_as_gray() {
        let bytes = write_pages(&[page(16, 16, 255)], TiffOptions::new(TiffColor::Gray)).unwrap();
        let read = ingest::load(&Source::bytes(bytes, "g.tiff"), &IngestConfig::default()).unwrap();
        let px = read[0].image.get_pixel(0, 0).0;
        // Gray means all three channels carry the same value.
        assert_eq!(px[0], px[1]);
        assert_eq!(px[1], px[2]);
    }

    #[test]
    fn empty_input_is_rejected() {
        assert!(write_pages(&[], TiffOptions::new(TiffColor::Rgb)).is_err());
    }

    #[test]
    fn pages_added_one_at_a_time_produce_the_same_file() {
        // The streaming writer exists so a 100-page conversion holds one page,
        // not a hundred; it must still write the same TIFF.
        let pages: Vec<RgbImage> = (0..3)
            .map(|i| RgbImage::from_pixel(5, 4, image::Rgb([i * 30, 0, 0])))
            .collect();
        let batch = write_pages(&pages, TiffOptions::new(TiffColor::Rgb)).unwrap();

        let mut writer = Writer::new(TiffOptions::new(TiffColor::Rgb)).unwrap();
        for page in &pages {
            writer.add(page).unwrap();
        }
        assert_eq!(writer.pages(), 3);
        assert_eq!(writer.finish().unwrap(), batch);
    }

    #[test]
    fn compression_shrinks_a_page_of_text_and_reads_back_the_same() {
        // An uncompressed 40-page archive is half a gigabyte for no reason, so
        // LZW is the default; it has to give back the same pixels.
        let mut page = RgbImage::from_pixel(120, 60, image::Rgb([255, 255, 255]));
        for x in 10..110 {
            for y in 20..24 {
                page.put_pixel(x, y, image::Rgb([0, 0, 0]));
            }
        }
        let raw = write_pages(
            &[page.clone()],
            TiffOptions {
                color: TiffColor::Rgb,
                compression: TiffCompression::None,
            },
        )
        .unwrap();
        let packed = write_pages(&[page.clone()], TiffOptions::new(TiffColor::Rgb)).unwrap();
        assert!(
            packed.len() * 4 < raw.len(),
            "lzw {} vs raw {}",
            packed.len(),
            raw.len()
        );

        let back =
            ingest::load(&Source::bytes(packed, "c.tiff"), &IngestConfig::default()).unwrap();
        assert_eq!(back.len(), 1);
        assert_eq!(back[0].image.as_raw(), page.as_raw());
    }

    #[test]
    fn a_writer_with_no_pages_is_an_error() {
        assert!(Writer::new(TiffOptions::new(TiffColor::Gray))
            .unwrap()
            .finish()
            .is_err());
    }
}
