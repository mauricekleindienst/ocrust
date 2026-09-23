//! Tick boxes on forms, and whether they are ticked.
//!
//! The recognizer reads the words beside a tick box and hardly ever the box:
//! "☐ offen ☒ VS-NfD ☐ VS-VERTRAULICH" comes out as "offen VS-NfD
//! VS-VERTRAULICH", and which grade the form carries is gone. The boxes are
//! found in the pixels instead — a small square outline, empty or crossed —
//! and handed on beside the text.

use image::{GrayImage, Luma, RgbImage};
use imageproc::region_labelling::{connected_components, Connectivity};

use crate::geom::Rect;

/// One tick box on a page.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TickBox {
    pub bbox: Rect,
    pub ticked: bool,
}

/// Luminance below which a pixel is ink.
const INK: u8 = 140;
/// Smallest and largest box side, as a share of the page width: 1.5 to 8.5 mm
/// on an A4 page, which covers the boxes forms print and leaves out table
/// cells and letters.
const MIN_SIDE: f32 = 0.006;
const MAX_SIDE: f32 = 0.035;
/// Share of each side that must be ink for the outline to count as closed.
const CLOSED: f32 = 0.85;
/// Ink inside the outline: below the first an empty box, from the second a
/// crossed or checked one. A box filled solid is a bullet, not a tick.
const EMPTY: f32 = 0.04;
const TICKED: f32 = 0.10;
const SOLID: f32 = 0.60;

/// Every tick box on the page, empty or ticked.
pub fn find(image: &RgbImage) -> Vec<TickBox> {
    let (w, h) = image.dimensions();
    if w < 64 || h < 64 {
        return Vec::new();
    }
    let ink = GrayImage::from_fn(w, h, |x, y| {
        let [r, g, b] = image.get_pixel(x, y).0;
        let luma = (r as u32 * 299 + g as u32 * 587 + b as u32 * 114) / 1000;
        Luma([if luma < INK as u32 { 255 } else { 0 }])
    });
    let labels = connected_components(&ink, Connectivity::Eight, Luma([0u8]));
    let count = labels.pixels().map(|p| p.0[0]).max().unwrap_or(0) as usize;
    // Bounding box per component: (x0, y0, x1, y1), inclusive.
    let mut bounds = vec![(u32::MAX, u32::MAX, 0u32, 0u32); count + 1];
    for (x, y, p) in labels.enumerate_pixels() {
        let label = p.0[0] as usize;
        if label == 0 {
            continue;
        }
        let b = &mut bounds[label];
        b.0 = b.0.min(x);
        b.1 = b.1.min(y);
        b.2 = b.2.max(x);
        b.3 = b.3.max(y);
    }
    let (min_side, max_side) = (
        (w as f32 * MIN_SIDE).max(10.0),
        (w as f32 * MAX_SIDE).max(12.0),
    );
    let mut found = Vec::new();
    for &(x0, y0, x1, y1) in bounds.iter().skip(1) {
        if x0 == u32::MAX {
            continue;
        }
        let (bw, bh) = ((x1 - x0 + 1) as f32, (y1 - y0 + 1) as f32);
        if bw < min_side || bh < min_side || bw > max_side || bh > max_side {
            continue;
        }
        if (bw / bh - 1.0).abs() > 0.2 || !closed(&ink, x0, y0, x1, y1) {
            continue;
        }
        let inside = interior(&ink, x0, y0, x1, y1);
        let ticked = if inside < EMPTY {
            false
        } else if (TICKED..SOLID).contains(&inside) {
            true
        } else {
            continue; // unsure, or a solid bullet
        };
        found.push(TickBox {
            bbox: Rect::new(x0 as f32, y0 as f32, (x1 + 1) as f32, (y1 + 1) as f32),
            ticked,
        });
    }
    found
}

fn is_ink(ink: &GrayImage, x: u32, y: u32) -> bool {
    ink.get_pixel(x, y).0[0] > 0
}

/// Whether all four sides of the box are drawn: along each, nearly every
/// position has ink within a thin band of the edge.
fn closed(ink: &GrayImage, x0: u32, y0: u32, x1: u32, y1: u32) -> bool {
    let side = (x1 - x0 + 1).min(y1 - y0 + 1);
    let band = (side / 8).max(2);
    let share = |hits: usize, of: u32| hits as f32 >= CLOSED * of as f32;
    let top = (x0..=x1)
        .filter(|&x| (y0..y0 + band).any(|y| is_ink(ink, x, y)))
        .count();
    let bottom = (x0..=x1)
        .filter(|&x| (y1 + 1 - band..=y1).any(|y| is_ink(ink, x, y)))
        .count();
    let left = (y0..=y1)
        .filter(|&y| (x0..x0 + band).any(|x| is_ink(ink, x, y)))
        .count();
    let right = (y0..=y1)
        .filter(|&y| (x1 + 1 - band..=x1).any(|x| is_ink(ink, x, y)))
        .count();
    let (bw, bh) = (x1 - x0 + 1, y1 - y0 + 1);
    share(top, bw) && share(bottom, bw) && share(left, bh) && share(right, bh)
}

/// Share of ink inside the outline, well clear of its strokes.
fn interior(ink: &GrayImage, x0: u32, y0: u32, x1: u32, y1: u32) -> f32 {
    let side = (x1 - x0 + 1).min(y1 - y0 + 1);
    let margin = (side * 2 / 9).max(2);
    if x0 + margin >= x1.saturating_sub(margin) || y0 + margin >= y1.saturating_sub(margin) {
        return 0.0;
    }
    let (ix0, iy0, ix1, iy1) = (x0 + margin, y0 + margin, x1 - margin, y1 - margin);
    let mut inked = 0usize;
    for y in iy0..=iy1 {
        for x in ix0..=ix1 {
            inked += is_ink(ink, x, y) as usize;
        }
    }
    inked as f32 / ((ix1 - ix0 + 1) * (iy1 - iy0 + 1)) as f32
}

#[cfg(test)]
mod tests {
    use super::*;

    const INK_RGB: image::Rgb<u8> = image::Rgb([20, 20, 20]);

    fn page() -> RgbImage {
        RgbImage::from_pixel(1000, 600, image::Rgb([255, 255, 255]))
    }

    /// A square outline `side` px wide at (x, y), strokes 3 px.
    fn outline(img: &mut RgbImage, x: u32, y: u32, side: u32) {
        for i in 0..side {
            for t in 0..3 {
                img.put_pixel(x + i, y + t, INK_RGB);
                img.put_pixel(x + i, y + side - 1 - t, INK_RGB);
                img.put_pixel(x + t, y + i, INK_RGB);
                img.put_pixel(x + side - 1 - t, y + i, INK_RGB);
            }
        }
    }

    /// The two diagonals of the box, 3 px wide.
    fn cross(img: &mut RgbImage, x: u32, y: u32, side: u32) {
        for i in 3..side - 3 {
            for t in 0..3u32 {
                let a = (i + t).min(side - 1);
                img.put_pixel(x + i, y + a, INK_RGB);
                img.put_pixel(x + i, y + side - 1 - a, INK_RGB);
            }
        }
    }

    #[test]
    fn an_empty_and_a_crossed_box_are_told_apart() {
        let mut img = page();
        outline(&mut img, 100, 100, 28);
        outline(&mut img, 300, 100, 28);
        cross(&mut img, 300, 100, 28);
        let mut found = find(&img);
        found.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
        assert_eq!(found.len(), 2, "{found:?}");
        assert!(!found[0].ticked);
        assert!(found[1].ticked);
        assert_eq!(found[1].bbox, Rect::new(300.0, 100.0, 328.0, 128.0));
    }

    #[test]
    fn a_solid_square_is_a_bullet_not_a_tick() {
        let mut img = page();
        for y in 100..128 {
            for x in 100..128 {
                img.put_pixel(x, y, INK_RGB);
            }
        }
        assert!(find(&img).is_empty());
    }

    #[test]
    fn letters_and_table_cells_are_not_boxes() {
        let mut img = page();
        // A table cell: far larger than a tick box.
        outline(&mut img, 50, 50, 200);
        // An "O"-like ring: its sides are not straight.
        for a in 0..360 {
            let (s, c) = (a as f32).to_radians().sin_cos();
            for r in 11..14 {
                let x = 500.0 + c * r as f32;
                let y = 300.0 + s * r as f32;
                img.put_pixel(x as u32, y as u32, INK_RGB);
            }
        }
        // A bar, not a square.
        for y in 400..410 {
            for x in 600..640 {
                img.put_pixel(x, y, INK_RGB);
            }
        }
        assert!(find(&img).is_empty(), "{:?}", find(&img));
    }
}
