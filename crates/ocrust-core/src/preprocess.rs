//! Image preparation before detection.
//!
//! Scanners, phone cameras and screenshots all arrive differently: skewed,
//! inverted, too small, or far too large. Fixing that up front costs
//! milliseconds and buys more accuracy than any detector tuning.

use image::{GrayImage, RgbImage};
use imageproc::geometric_transformations::{rotate_about_center_no_crop, Interpolation};

/// Which clean-up steps to run.
#[derive(Debug, Clone)]
pub struct PreprocessConfig {
    /// Invert light-on-dark pages (dark mode screenshots, negatives).
    pub auto_invert: bool,
    /// Estimate and correct page skew.
    pub deskew: bool,
    /// Largest skew that is still treated as skew rather than layout, in degrees.
    pub max_skew_deg: f32,
    /// Stretch contrast using the 2nd/98th luminance percentiles.
    pub auto_contrast: bool,
    /// Remove salt-and-pepper noise with a 3x3 median filter.
    pub denoise: bool,
    /// Upscale images whose longest side is below this, up to 2x.
    pub upscale_below: u32,
    /// Downscale anything above this pixel count (width * height).
    pub max_pixels: u64,
}

impl Default for PreprocessConfig {
    fn default() -> Self {
        Self {
            auto_invert: true,
            deskew: true,
            max_skew_deg: 12.0,
            auto_contrast: false,
            denoise: false,
            upscale_below: 800,
            max_pixels: 40_000_000,
        }
    }
}

impl PreprocessConfig {
    /// Everything off: the image is handed to the detector untouched.
    pub fn none() -> Self {
        Self {
            auto_invert: false,
            deskew: false,
            max_skew_deg: 0.0,
            auto_contrast: false,
            denoise: false,
            upscale_below: 0,
            max_pixels: 0,
        }
    }
}

/// A prepared page plus what was done to it.
#[derive(Debug, Clone)]
pub struct Prepared {
    pub image: RgbImage,
    /// Rotation applied, in degrees (positive clockwise).
    pub rotation: f32,
    /// Scale applied relative to the input image.
    pub scale: f32,
    pub inverted: bool,
}

/// Runs the configured clean-up steps.
///
/// Coordinates produced downstream refer to [`Prepared::image`], so callers
/// should report that image's dimensions as the page size.
pub fn prepare(mut image: RgbImage, cfg: &PreprocessConfig) -> Prepared {
    let mut scale = 1.0f32;
    let mut rotation = 0.0f32;
    let mut inverted = false;

    if cfg.max_pixels > 0 {
        let pixels = image.width() as u64 * image.height() as u64;
        if pixels > cfg.max_pixels {
            let factor = (cfg.max_pixels as f32 / pixels as f32).sqrt();
            image = resize_by(&image, factor, image::imageops::FilterType::Triangle);
            scale *= factor;
        }
    }

    if cfg.upscale_below > 0 {
        let longest = image.width().max(image.height());
        if longest > 0 && longest < cfg.upscale_below {
            let factor = (cfg.upscale_below as f32 / longest as f32).min(2.0);
            if factor > 1.01 {
                image = resize_by(&image, factor, image::imageops::FilterType::Lanczos3);
                scale *= factor;
            }
        }
    }

    if cfg.auto_invert && is_dark_background(&image) {
        image::imageops::invert(&mut image);
        inverted = true;
    }

    if cfg.denoise {
        image = imageproc::filter::median_filter(&image, 1, 1);
    }

    if cfg.auto_contrast {
        stretch_contrast(&mut image);
    }

    if cfg.deskew && cfg.max_skew_deg > 0.0 {
        let angle = estimate_skew(&image, cfg.max_skew_deg);
        if angle.abs() > 0.15 {
            image = rotate_about_center_no_crop(
                &image,
                -angle.to_radians(),
                Interpolation::Bilinear,
                imageproc::geometric_transformations::Border::Constant(image::Rgb([
                    255u8, 255, 255,
                ])),
            );
            rotation = -angle;
        }
    }

    Prepared {
        image,
        rotation,
        scale,
        inverted,
    }
}

fn resize_by(img: &RgbImage, factor: f32, filter: image::imageops::FilterType) -> RgbImage {
    let w = ((img.width() as f32 * factor).round() as u32).max(1);
    let h = ((img.height() as f32 * factor).round() as u32).max(1);
    image::imageops::resize(img, w, h, filter)
}

/// True when the page is light text on a dark background.
fn is_dark_background(img: &RgbImage) -> bool {
    // Sample a grid instead of every pixel: plenty for a global decision.
    let step = (img.width().max(img.height()) / 256).max(1);
    let mut sum = 0u64;
    let mut count = 0u64;
    for y in (0..img.height()).step_by(step as usize) {
        for x in (0..img.width()).step_by(step as usize) {
            let p = img.get_pixel(x, y).0;
            sum += luma(p[0], p[1], p[2]) as u64;
            count += 1;
        }
    }
    count > 0 && (sum / count) < 110
}

fn luma(r: u8, g: u8, b: u8) -> u8 {
    ((r as u32 * 299 + g as u32 * 587 + b as u32 * 114) / 1000) as u8
}

/// Linear contrast stretch between the 2nd and 98th luminance percentiles.
fn stretch_contrast(img: &mut RgbImage) {
    let mut hist = [0u32; 256];
    for p in img.pixels() {
        hist[luma(p.0[0], p.0[1], p.0[2]) as usize] += 1;
    }
    let total: u32 = hist.iter().sum();
    if total == 0 {
        return;
    }
    let pick = |target: u32| {
        let mut acc = 0u32;
        for (v, &c) in hist.iter().enumerate() {
            acc += c;
            if acc >= target {
                return v as f32;
            }
        }
        255.0
    };
    let lo = pick(total / 50);
    let hi = pick(total - total / 50);
    if hi - lo < 16.0 {
        return; // already flat or nearly blank; stretching would amplify noise
    }
    let scale = 255.0 / (hi - lo);
    for p in img.pixels_mut() {
        for c in 0..3 {
            p.0[c] = (((p.0[c] as f32 - lo) * scale).round()).clamp(0.0, 255.0) as u8;
        }
    }
}

/// Estimates page skew in degrees (positive means the text runs downhill to the
/// right) using a sheared horizontal projection profile.
///
/// Text lines produce sharp peaks in the row-ink histogram only when the shear
/// matches the true skew, so the angle maximizing the profile's variance is the
/// skew. Coarse-to-fine keeps it cheap.
pub fn estimate_skew(img: &RgbImage, max_deg: f32) -> f32 {
    let small = downscale_for_skew(img);
    let (w, h) = small.dimensions();
    if w < 32 || h < 32 {
        return 0.0;
    }
    let ink = ink_mask(&small);

    // A page with almost no ink carries no skew signal: every angle scores the
    // same and the search would return an arbitrary one, rotating blank scans.
    let ink_pixels = ink.iter().filter(|&&v| v > 0.0).count();
    if (ink_pixels as f32) < (w * h) as f32 * 0.0005 {
        return 0.0;
    }

    let upright = profile_score(&ink, w, h, 0.0);
    let mut best = (upright, 0.0f32);
    let evaluate = |angle: f32, best: &mut (f32, f32)| {
        let score = profile_score(&ink, w, h, angle);
        if score > best.0 {
            *best = (score, angle);
        }
    };

    let coarse_steps = (max_deg * 2.0).round() as i32;
    for i in -coarse_steps..=coarse_steps {
        evaluate(i as f32 * 0.5, &mut best);
    }
    let around = best.1;
    for i in -5..=5 {
        let angle = around + i as f32 * 0.1;
        if angle.abs() <= max_deg {
            evaluate(angle, &mut best);
        }
    }

    // Only rotate when the tilted profile is clearly sharper than the upright
    // one; small gains are noise and a needless resample costs quality.
    if best.0 <= upright * 1.02 {
        return 0.0;
    }
    best.1
}

fn downscale_for_skew(img: &RgbImage) -> GrayImage {
    const TARGET: u32 = 600;
    let longest = img.width().max(img.height());
    let gray = image::imageops::grayscale(img);
    if longest <= TARGET {
        return gray;
    }
    let f = TARGET as f32 / longest as f32;
    image::imageops::resize(
        &gray,
        ((img.width() as f32 * f) as u32).max(1),
        ((img.height() as f32 * f) as u32).max(1),
        image::imageops::FilterType::Triangle,
    )
}

/// Marks "ink" pixels: darker than the image mean by a margin.
fn ink_mask(gray: &GrayImage) -> Vec<f32> {
    let mean = gray.pixels().map(|p| p.0[0] as u32).sum::<u32>() as f32
        / (gray.width() * gray.height()).max(1) as f32;
    let threshold = mean - 18.0;
    gray.pixels()
        .map(|p| {
            if (p.0[0] as f32) < threshold {
                1.0
            } else {
                0.0
            }
        })
        .collect()
}

/// Variance of the row-ink histogram after shearing by `angle` degrees.
fn profile_score(ink: &[f32], w: u32, h: u32, angle: f32) -> f32 {
    let tan = angle.to_radians().tan();
    let mut rows = vec![0f32; h as usize];
    for y in 0..h {
        let base = y as usize * w as usize;
        for x in 0..w {
            let v = ink[base + x as usize];
            if v == 0.0 {
                continue;
            }
            // Shift each column so a skewed line lands on one histogram row.
            let shifted = y as f32 - (x as f32 - w as f32 * 0.5) * tan;
            if shifted < 0.0 {
                continue;
            }
            let row = shifted as usize;
            if row < rows.len() {
                rows[row] += v;
            }
        }
    }
    // Sum of squared row sums: peaks dominate, so sharper profiles score higher.
    rows.iter().map(|&v| v * v).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Draws horizontal text-like bars, optionally rotated.
    fn striped_page(w: u32, h: u32, skew_deg: f32) -> RgbImage {
        let mut img = RgbImage::from_pixel(w, h, image::Rgb([255, 255, 255]));
        let tan = skew_deg.to_radians().tan();
        for band in 1..(h / 20) {
            let y0 = band * 20;
            for x in (w / 10)..(w * 9 / 10) {
                let shift = ((x as f32 - w as f32 * 0.5) * tan).round() as i32;
                for dy in 0..6i32 {
                    let y = y0 as i32 + dy + shift;
                    if y >= 0 && (y as u32) < h {
                        img.put_pixel(x, y as u32, image::Rgb([20, 20, 20]));
                    }
                }
            }
        }
        img
    }

    #[test]
    fn skew_estimate_is_near_zero_for_straight_text() {
        let img = striped_page(400, 300, 0.0);
        assert!(estimate_skew(&img, 12.0).abs() < 0.4);
    }

    #[test]
    fn skew_estimate_recovers_known_rotation() {
        for truth in [-6.0f32, 3.0, 5.0] {
            let img = striped_page(500, 400, truth);
            let est = estimate_skew(&img, 12.0);
            assert!(
                (est - truth).abs() < 1.0,
                "expected ~{truth}, estimated {est}"
            );
        }
    }

    #[test]
    fn deskew_rotates_and_reports_the_angle() {
        let img = striped_page(500, 400, 5.0);
        let out = prepare(
            img,
            &PreprocessConfig {
                upscale_below: 0,
                ..Default::default()
            },
        );
        assert!(out.rotation < -3.0, "rotation {}", out.rotation);
        assert!(estimate_skew(&out.image, 12.0).abs() < 1.2);
    }

    #[test]
    fn blank_pages_are_not_rotated() {
        // Regression: an empty ink mask used to make every angle score zero,
        // so the search returned the first candidate and skewed blank scans.
        let blank = RgbImage::from_pixel(600, 200, image::Rgb([255, 255, 255]));
        assert_eq!(estimate_skew(&blank, 12.0), 0.0);
        let out = prepare(blank, &PreprocessConfig::default());
        assert_eq!(out.rotation, 0.0);
        // 600px upscaled to the 800px floor, and nothing else.
        assert_eq!(out.image.dimensions(), (800, 267));
    }

    #[test]
    fn nearly_empty_pages_are_not_rotated() {
        let mut img = RgbImage::from_pixel(600, 400, image::Rgb([255, 255, 255]));
        // A couple of stray dark pixels must not be mistaken for text lines.
        for x in 10..14 {
            img.put_pixel(x, 20, image::Rgb([0, 0, 0]));
        }
        assert_eq!(estimate_skew(&img, 12.0), 0.0);
    }

    #[test]
    fn dark_pages_are_inverted() {
        let dark = RgbImage::from_pixel(50, 50, image::Rgb([10, 10, 10]));
        let out = prepare(dark, &PreprocessConfig::default());
        assert!(out.inverted);
        assert!(out.image.get_pixel(0, 0).0[0] > 200);
    }

    #[test]
    fn light_pages_are_left_alone() {
        let light = RgbImage::from_pixel(1000, 1000, image::Rgb([250, 250, 250]));
        let out = prepare(light, &PreprocessConfig::default());
        assert!(!out.inverted);
        assert_eq!(out.scale, 1.0);
    }

    #[test]
    fn oversized_images_are_downscaled() {
        let big = RgbImage::from_pixel(3000, 2000, image::Rgb([255, 255, 255]));
        let out = prepare(
            big,
            &PreprocessConfig {
                max_pixels: 1_000_000,
                deskew: false,
                ..Default::default()
            },
        );
        assert!((out.image.width() as u64 * out.image.height() as u64) <= 1_100_000);
        assert!(out.scale < 1.0);
    }

    #[test]
    fn small_images_are_upscaled_once() {
        let small = RgbImage::from_pixel(200, 100, image::Rgb([255, 255, 255]));
        let out = prepare(
            small,
            &PreprocessConfig {
                deskew: false,
                ..Default::default()
            },
        );
        assert_eq!(out.image.width(), 400, "capped at 2x");
        assert!((out.scale - 2.0).abs() < 1e-3);
    }

    #[test]
    fn contrast_stretch_expands_range() {
        let mut img = RgbImage::from_pixel(100, 100, image::Rgb([120, 120, 120]));
        for x in 0..50 {
            for y in 0..100 {
                img.put_pixel(x, y, image::Rgb([140, 140, 140]));
            }
        }
        stretch_contrast(&mut img);
        let lo = img.pixels().map(|p| p.0[0]).min().unwrap();
        let hi = img.pixels().map(|p| p.0[0]).max().unwrap();
        assert!(hi - lo > 100, "range {lo}..{hi}");
    }

    #[test]
    fn preprocess_none_is_a_no_op() {
        let img = striped_page(300, 200, 4.0);
        let out = prepare(img.clone(), &PreprocessConfig::none());
        assert_eq!(out.image.dimensions(), img.dimensions());
        assert_eq!(out.rotation, 0.0);
        assert_eq!(out.scale, 1.0);
    }
}
