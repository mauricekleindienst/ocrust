//! Multi-page TIFF output.
//!
//! TIFF is still the lingua franca of document archives and fax workflows, so
//! `ocrust` reads it and writes it: a scanned PDF or a pile of JPEGs can be
//! normalized into one deskewed, ready-to-archive TIFF.

use image::RgbImage;
use tiff::encoder::{colortype, TiffEncoder};

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

/// Writes `pages` as one multi-page TIFF.
///
/// Pages may differ in size; each becomes its own directory (IFD), which is
/// exactly how multi-page scans are stored.
pub fn write_pages(pages: &[RgbImage], color: TiffColor) -> Result<Vec<u8>> {
    if pages.is_empty() {
        return Err(Error::config("cannot write a TIFF without pages"));
    }
    let mut buffer = std::io::Cursor::new(Vec::new());
    {
        let mut encoder = TiffEncoder::new(&mut buffer)
            .map_err(|e| Error::Unsupported(format!("TIFF writer: {e}")))?;
        for (index, page) in pages.iter().enumerate() {
            let (w, h) = page.dimensions();
            if w == 0 || h == 0 {
                return Err(Error::config(format!("page {} is empty", index + 1)));
            }
            match color {
                TiffColor::Rgb => encoder
                    .write_image::<colortype::RGB8>(w, h, page.as_raw())
                    .map_err(|e| Error::Unsupported(format!("TIFF page {}: {e}", index + 1)))?,
                TiffColor::Gray => {
                    let gray = image::imageops::grayscale(page);
                    encoder
                        .write_image::<colortype::Gray8>(w, h, gray.as_raw())
                        .map_err(|e| Error::Unsupported(format!("TIFF page {}: {e}", index + 1)))?
                }
            }
        }
    }
    Ok(buffer.into_inner())
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
        let bytes = write_pages(&pages, TiffColor::Rgb).unwrap();
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
        let bytes = write_pages(&[page(16, 16, 255)], TiffColor::Gray).unwrap();
        let read = ingest::load(&Source::bytes(bytes, "g.tiff"), &IngestConfig::default()).unwrap();
        let px = read[0].image.get_pixel(0, 0).0;
        // Gray means all three channels carry the same value.
        assert_eq!(px[0], px[1]);
        assert_eq!(px[1], px[2]);
    }

    #[test]
    fn empty_input_is_rejected() {
        assert!(write_pages(&[], TiffColor::Rgb).is_err());
    }
}
