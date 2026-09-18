//! Text detection with DB / DB++ style models (PP-OCR `det`).
//!
//! The network predicts a per-pixel text probability map. Turning that map
//! into text-line quads is the part that decides how good the whole OCR looks,
//! so the steps are explicit: threshold, trace contours, fit minimum-area
//! rectangles, score them against the probability map, then "unclip" them back
//! to their real size.

use image::RgbImage;
use imageproc::contours::{find_contours_with_threshold, BorderType};
use ndarray::ArrayD;

use crate::error::{Error, Result};
use crate::geom::{min_area_rect, Point, Quad, Rect};
use crate::runtime::{nchw, OnnxModel, SessionOptions};

/// How `limit_side_len` is applied when resizing the input.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LimitType {
    /// Shrink so that the longest side is at most `limit_side_len`. Default:
    /// keeps large scans fast.
    Max,
    /// Grow so that the shortest side is at least `limit_side_len`. Useful for
    /// low-resolution photos of small text.
    Min,
}

/// Detection hyper-parameters. The defaults match PP-OCRv4/v5 inference.
#[derive(Debug, Clone)]
pub struct DetectorConfig {
    pub limit_side_len: u32,
    pub limit_type: LimitType,
    /// Probability above which a pixel counts as text.
    pub thresh: f32,
    /// Mean probability a box must reach to be kept.
    pub box_thresh: f32,
    /// How much to grow the shrunk kernel the network predicts.
    pub unclip_ratio: f32,
    pub max_candidates: usize,
    /// Boxes thinner than this (in pixels, on the resized image) are dropped.
    pub min_box_size: f32,
    /// Pages whose longest side exceeds this are detected in tiles rather than
    /// scaled down. `None` disables tiling.
    ///
    /// The default is four times `limit_side_len`: below that, downscaling keeps
    /// body text above roughly ten pixels and one pass is both faster and
    /// sufficient. Above it — A0 and A3 sheets, 600 dpi scans — a whole-page
    /// pass would shrink an 8 pt label to a few pixels, so tiles win.
    pub tile_above_side_len: Option<u32>,
    /// Fraction of a tile that overlaps its neighbour, so text on a seam is not
    /// cut in half.
    pub tile_overlap: f32,
}

impl Default for DetectorConfig {
    fn default() -> Self {
        Self {
            limit_side_len: 960,
            limit_type: LimitType::Max,
            thresh: 0.3,
            box_thresh: 0.6,
            unclip_ratio: 1.5,
            max_candidates: 1000,
            min_box_size: 3.0,
            tile_above_side_len: Some(3840),
            tile_overlap: 0.15,
        }
    }
}

/// One detected text line.
#[derive(Debug, Clone)]
pub struct DetectedBox {
    /// Corners in the coordinate system of the image passed to [`TextDetector::detect`].
    pub quad: Quad,
    /// Mean text probability inside the box, in `0..=1`.
    pub score: f32,
}

/// Wraps a DB detection model.
#[derive(Debug)]
pub struct TextDetector {
    model: OnnxModel,
    config: DetectorConfig,
}

impl TextDetector {
    pub fn new(
        path: &std::path::Path,
        config: DetectorConfig,
        session: &SessionOptions,
    ) -> Result<Self> {
        Ok(Self {
            model: OnnxModel::load(path, "detection", session)?,
            config,
        })
    }

    pub fn config(&self) -> &DetectorConfig {
        &self.config
    }

    /// Detects text lines, ordered top-to-bottom / left-to-right.
    ///
    /// Large-format pages — A0 drawings, plan sheets, oversized scans — are
    /// detected in overlapping tiles instead of being squeezed into
    /// `limit_side_len`, because an 8 pt label on an A0 sheet is only a few
    /// pixels tall once the whole sheet is scaled to 960 px.
    pub fn detect(&self, image: &RgbImage) -> Result<Vec<DetectedBox>> {
        let (orig_w, orig_h) = image.dimensions();
        if orig_w == 0 || orig_h == 0 {
            return Ok(Vec::new());
        }
        if let Some(tiles) = plan_tiles(orig_w, orig_h, &self.config) {
            return self.detect_tiled(image, &tiles);
        }
        self.detect_whole(image)
    }

    /// Detection on the whole image, scaled to `limit_side_len`.
    fn detect_whole(&self, image: &RgbImage) -> Result<Vec<DetectedBox>> {
        let (orig_w, orig_h) = image.dimensions();
        let (rw, rh) = self.target_size(orig_w, orig_h);
        let resized = image::imageops::resize(image, rw, rh, image::imageops::FilterType::Triangle);

        let input = normalize_det(&resized);
        let output = self.model.run(input)?;
        let prob = ProbMap::from_output(&output)?;

        let mut boxes = self.boxes_from_prob(&prob);
        // Map back to the caller's coordinate system.
        let (sx, sy) = (orig_w as f32 / rw as f32, orig_h as f32 / rh as f32);
        for b in boxes.iter_mut() {
            b.quad = b
                .quad
                .scaled_clamped(sx, sy, orig_w as f32, orig_h as f32)
                .ordered();
        }
        boxes.retain(|b| b.quad.edge_width() >= 2.0 && b.quad.edge_height() >= 2.0);
        sort_reading_ish(&mut boxes);
        Ok(boxes)
    }

    /// Detects in overlapping tiles and merges the results.
    fn detect_tiled(&self, image: &RgbImage, tiles: &[Tile]) -> Result<Vec<DetectedBox>> {
        let (full_w, full_h) = image.dimensions();
        log::debug!(
            "tiled detection: {}x{} in {} tiles of {}",
            full_w,
            full_h,
            tiles.len(),
            self.config.limit_side_len
        );

        let mut merged: Vec<DetectedBox> = Vec::new();
        for tile in tiles {
            let view = image::imageops::crop_imm(image, tile.x, tile.y, tile.width, tile.height)
                .to_image();
            for mut b in self.detect_whole(&view)? {
                b.quad = translate(&b.quad, tile.x as f32, tile.y as f32);
                // Ownership is decided in page coordinates, so a line on a seam
                // is kept by exactly one tile.
                if tile.owns(&b.quad.bounds()) {
                    merged.push(b);
                }
            }
        }

        // Boxes on a seam can still be found twice; keep the better one.
        deduplicate(&mut merged, 0.3);
        for b in merged.iter_mut() {
            b.quad = b
                .quad
                .scaled_clamped(1.0, 1.0, full_w as f32, full_h as f32)
                .ordered();
        }
        sort_reading_ish(&mut merged);
        Ok(merged)
    }

    /// Resized dimensions: scaled by the side-length limit, rounded to /32.
    fn target_size(&self, w: u32, h: u32) -> (u32, u32) {
        let limit = self.config.limit_side_len.max(32) as f32;
        let (wf, hf) = (w as f32, h as f32);
        let ratio = match self.config.limit_type {
            LimitType::Max => {
                let longest = wf.max(hf);
                if longest > limit {
                    limit / longest
                } else {
                    1.0
                }
            }
            LimitType::Min => {
                let shortest = wf.min(hf);
                if shortest < limit {
                    limit / shortest
                } else {
                    1.0
                }
            }
        };
        let round32 = |v: f32| (((v * ratio / 32.0).round() as u32) * 32).max(32);
        (round32(wf), round32(hf))
    }

    fn boxes_from_prob(&self, prob: &ProbMap) -> Vec<DetectedBox> {
        let cfg = &self.config;
        let mut bitmap = image::GrayImage::new(prob.width as u32, prob.height as u32);
        for (i, px) in bitmap.pixels_mut().enumerate() {
            px.0[0] = if prob.data[i] > cfg.thresh { 255 } else { 0 };
        }

        let contours = find_contours_with_threshold::<u32>(&bitmap, 127);
        let mut out = Vec::new();
        for contour in contours.into_iter().take(cfg.max_candidates) {
            if contour.border_type != BorderType::Outer || contour.points.len() < 4 {
                continue;
            }
            let pts: Vec<Point> = contour
                .points
                .iter()
                .map(|p| Point::new(p.x as f32, p.y as f32))
                .collect();
            let Some(quad) = min_area_rect(&pts) else {
                continue;
            };
            if quad.edge_width().min(quad.edge_height()) < cfg.min_box_size {
                continue;
            }
            let score = prob.mean_inside(&quad);
            if score < cfg.box_thresh {
                continue;
            }
            // DB is trained on shrunk kernels: grow the box back.
            let perimeter = quad.perimeter().max(1e-3);
            let delta = quad.area() * cfg.unclip_ratio / perimeter;
            let grown = quad.expanded_rect(delta).scaled_clamped(
                1.0,
                1.0,
                prob.width as f32 - 1.0,
                prob.height as f32 - 1.0,
            );
            if grown.edge_width().min(grown.edge_height()) < cfg.min_box_size {
                continue;
            }
            out.push(DetectedBox {
                quad: grown.ordered(),
                score,
            });
        }
        out
    }
}

/// Detection input normalization: `(x/255 - mean) / std`, as NCHW.
fn normalize_det(img: &RgbImage) -> ArrayD<f32> {
    const MEAN: [f32; 3] = [0.485, 0.456, 0.406];
    const STD: [f32; 3] = [0.229, 0.224, 0.225];
    let (w, h) = img.dimensions();
    let (w, h) = (w as usize, h as usize);
    let mut data = vec![0f32; 3 * h * w];
    let raw = img.as_raw();
    for c in 0..3 {
        let plane = &mut data[c * h * w..(c + 1) * h * w];
        let (mean, inv_std) = (MEAN[c], 1.0 / STD[c]);
        for (i, slot) in plane.iter_mut().enumerate() {
            *slot = (raw[i * 3 + c] as f32 / 255.0 - mean) * inv_std;
        }
    }
    nchw(1, 3, h, w, data)
}

/// Tile grid for an oversized page, or `None` when a single pass is enough.
///
/// Tiles are the size the detector runs at, so the page is never downscaled and
/// small labels keep their pixels.
fn plan_tiles(width: u32, height: u32, config: &DetectorConfig) -> Option<Vec<Tile>> {
    let limit = config.limit_side_len.max(64);
    let trigger = config.tile_above_side_len?;
    if width.max(height) <= trigger.max(limit) {
        return None;
    }
    let overlap = config.tile_overlap.clamp(0.0, 0.4);
    let step = ((limit as f32) * (1.0 - overlap)).max(64.0) as u32;

    // Half the overlap: the distance from a tile edge to the middle of the
    // strip it shares with its neighbour.
    let half = (limit.saturating_sub(step)) as f32 * 0.5;

    let mut tiles = Vec::new();
    let mut y = 0u32;
    loop {
        let tile_h = limit.min(height - y);
        let last_row = y + tile_h >= height;
        let mut x = 0u32;
        loop {
            let tile_w = limit.min(width - x);
            let last_column = x + tile_w >= width;
            tiles.push(Tile {
                x,
                y,
                width: tile_w,
                height: tile_h,
                own_x0: if x == 0 { 0.0 } else { x as f32 + half },
                own_y0: if y == 0 { 0.0 } else { y as f32 + half },
                own_x1: if last_column {
                    width as f32 + 1.0
                } else {
                    (x + tile_w) as f32 - half
                },
                own_y1: if last_row {
                    height as f32 + 1.0
                } else {
                    (y + tile_h) as f32 - half
                },
            });
            if last_column {
                break;
            }
            x += step;
        }
        if last_row {
            break;
        }
        y += step;
    }
    (tiles.len() > 1).then_some(tiles)
}

/// One tile of an oversized page, with the region it is responsible for.
///
/// `own_*` is in page coordinates and splits every overlap down the middle, so
/// each point of the page belongs to exactly one tile and merged results cannot
/// contain the same line twice.
#[derive(Debug, Clone, Copy)]
struct Tile {
    x: u32,
    y: u32,
    width: u32,
    height: u32,
    own_x0: f32,
    own_y0: f32,
    own_x1: f32,
    own_y1: f32,
}

impl Tile {
    /// True when this tile owns `bounds` (given in page coordinates).
    fn owns(&self, bounds: &Rect) -> bool {
        let cx = bounds.center_x();
        let cy = bounds.center_y();
        cx >= self.own_x0 && cx < self.own_x1 && cy >= self.own_y0 && cy < self.own_y1
    }
}

/// Moves a quad into page coordinates.
fn translate(quad: &Quad, dx: f32, dy: f32) -> Quad {
    let mut points = quad.points;
    for p in points.iter_mut() {
        p.x += dx;
        p.y += dy;
    }
    Quad::new(points)
}

/// Drops boxes that overlap a better-scoring one by more than `max_iou`.
fn deduplicate(boxes: &mut Vec<DetectedBox>, max_iou: f32) {
    boxes.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut kept: Vec<DetectedBox> = Vec::with_capacity(boxes.len());
    for candidate in boxes.drain(..) {
        let bounds = candidate.quad.bounds();
        if kept.iter().any(|k| k.quad.bounds().iou(&bounds) > max_iou) {
            continue;
        }
        kept.push(candidate);
    }
    *boxes = kept;
}

/// Sorts boxes top-to-bottom, then left-to-right. Full reading order is decided
/// later, in `layout`.
fn sort_reading_ish(boxes: &mut [DetectedBox]) {
    boxes.sort_by(|a, b| {
        let (ba, bb) = (a.quad.bounds(), b.quad.bounds());
        ba.y0
            .partial_cmp(&bb.y0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(
                ba.x0
                    .partial_cmp(&bb.x0)
                    .unwrap_or(std::cmp::Ordering::Equal),
            )
    });
}

/// The detector's probability map, flattened.
struct ProbMap {
    data: Vec<f32>,
    width: usize,
    height: usize,
}

impl ProbMap {
    /// Accepts `[N, 1, H, W]` and `[N, H, W]` outputs; only the first item of
    /// the batch is used.
    fn from_output(out: &ArrayD<f32>) -> Result<Self> {
        let shape = out.shape();
        let (h, w) = match shape.len() {
            4 => (shape[2], shape[3]),
            3 => (shape[1], shape[2]),
            2 => (shape[0], shape[1]),
            _ => {
                return Err(Error::model(format!(
                    "unexpected detection output shape {shape:?}"
                )))
            }
        };
        let data: Vec<f32> = out.iter().copied().take(h * w).collect();
        if data.len() < h * w {
            return Err(Error::model("detection output smaller than its shape"));
        }
        Ok(Self {
            data,
            width: w,
            height: h,
        })
    }

    fn at(&self, x: usize, y: usize) -> f32 {
        self.data[y * self.width + x]
    }

    /// Mean probability inside `quad` ("fast" scoring: bbox sweep + point test).
    fn mean_inside(&self, quad: &Quad) -> f32 {
        let b = quad.bounds();
        let x0 = b.x0.floor().max(0.0) as usize;
        let y0 = b.y0.floor().max(0.0) as usize;
        let x1 = (b.x1.ceil() as usize).min(self.width.saturating_sub(1));
        let y1 = (b.y1.ceil() as usize).min(self.height.saturating_sub(1));
        if x1 < x0 || y1 < y0 {
            return 0.0;
        }
        let mut sum = 0.0;
        let mut count = 0u32;
        for y in y0..=y1 {
            for x in x0..=x1 {
                if quad.contains(x as f32 + 0.5, y as f32 + 0.5) {
                    sum += self.at(x, y);
                    count += 1;
                }
            }
        }
        if count == 0 {
            0.0
        } else {
            sum / count as f32
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geom::Rect;
    use ndarray::IxDyn;

    fn detector_cfg() -> DetectorConfig {
        DetectorConfig::default()
    }

    #[test]
    fn resize_respects_max_side_and_multiple_of_32() {
        let det = DetectorConfig {
            limit_side_len: 960,
            ..detector_cfg()
        };
        let sizer = |w: u32, h: u32| {
            let limit = det.limit_side_len as f32;
            let ratio = if (w.max(h) as f32) > limit {
                limit / w.max(h) as f32
            } else {
                1.0
            };
            let r32 = |v: f32| (((v * ratio / 32.0).round() as u32) * 32).max(32);
            (r32(w as f32), r32(h as f32))
        };
        let (w, h) = sizer(4000, 3000);
        assert_eq!(w % 32, 0);
        assert_eq!(h % 32, 0);
        assert!(w <= 960 + 32 && h <= 960);
    }

    #[test]
    fn prob_map_scores_only_inside_the_quad() {
        let (w, h) = (10usize, 10usize);
        let mut data = vec![0.0f32; w * h];
        for y in 2..5 {
            for x in 1..6 {
                data[y * w + x] = 1.0;
            }
        }
        let map = ProbMap {
            data,
            width: w,
            height: h,
        };
        let inside = map.mean_inside(&Quad::from_rect(Rect::new(1.0, 2.0, 6.0, 5.0)));
        assert!(inside > 0.9, "{inside}");
        let outside = map.mean_inside(&Quad::from_rect(Rect::new(6.5, 6.5, 9.0, 9.0)));
        assert!(outside < 0.01, "{outside}");
    }

    #[test]
    fn prob_map_accepts_both_output_ranks() {
        let a = ArrayD::from_shape_vec(IxDyn(&[1, 1, 2, 3]), vec![0.5; 6]).unwrap();
        assert_eq!(ProbMap::from_output(&a).unwrap().width, 3);
        let b = ArrayD::from_shape_vec(IxDyn(&[1, 2, 3]), vec![0.5; 6]).unwrap();
        assert_eq!(ProbMap::from_output(&b).unwrap().height, 2);
        let bad = ArrayD::from_shape_vec(IxDyn(&[6]), vec![0.5; 6]).unwrap();
        assert!(ProbMap::from_output(&bad).is_err());
    }

    #[test]
    fn small_pages_are_not_tiled() {
        let cfg = DetectorConfig::default();
        assert!(plan_tiles(1654, 2339, &cfg).is_none(), "A4 at 200 dpi");
        assert!(plan_tiles(2480, 3508, &cfg).is_none(), "A4 at 300 dpi");
        assert!(plan_tiles(960, 960, &cfg).is_none());
        assert!(
            plan_tiles(
                9000,
                7000,
                &DetectorConfig {
                    tile_above_side_len: None,
                    ..DetectorConfig::default()
                }
            )
            .is_none(),
            "tiling can be switched off"
        );
    }

    #[test]
    fn large_format_sheets_are_tiled() {
        let cfg = DetectorConfig::default();
        // A0 at 300 dpi and A3 at 600 dpi both need tiles.
        assert!(plan_tiles(9933, 7016, &cfg).is_some(), "A0 at 300 dpi");
        assert!(plan_tiles(7016, 4960, &cfg).is_some(), "A3 at 600 dpi");
    }

    #[test]
    fn large_pages_are_covered_by_overlapping_tiles() {
        let cfg = DetectorConfig::default();
        let tiles = plan_tiles(4000, 3000, &cfg).expect("tiled");
        assert!(tiles.len() >= 20, "{} tiles", tiles.len());
        // Every pixel must belong to at least one tile.
        for (x, y) in [(0, 0), (3999, 2999), (2000, 1500), (3999, 0), (0, 2999)] {
            assert!(
                tiles
                    .iter()
                    .any(|t| x >= t.x && y >= t.y && x < t.x + t.width && y < t.y + t.height),
                "({x},{y}) is not covered"
            );
        }
        // Tiles never exceed the detector's working size.
        for tile in &tiles {
            assert!(tile.width <= cfg.limit_side_len);
            assert!(tile.height <= cfg.limit_side_len);
        }
        // Neighbours overlap, so text on a seam is seen whole at least once.
        let first_row: Vec<&Tile> = tiles.iter().filter(|t| t.y == 0).collect();
        assert!(first_row.len() >= 2);
        assert!(
            first_row[1].x < first_row[0].x + first_row[0].width,
            "tiles do not overlap"
        );
    }

    #[test]
    fn every_point_of_the_page_is_owned_exactly_once() {
        let cfg = DetectorConfig::default();
        let tiles = plan_tiles(5000, 4200, &cfg).expect("tiled");
        for gx in (0..5000).step_by(97) {
            for gy in (0..4200).step_by(101) {
                let spot = Rect::new(gx as f32, gy as f32, gx as f32 + 1.0, gy as f32 + 1.0);
                let owners = tiles.iter().filter(|t| t.owns(&spot)).count();
                assert_eq!(owners, 1, "({gx},{gy}) owned by {owners} tiles");
            }
        }
    }

    #[test]
    fn owned_regions_stay_inside_their_tile() {
        let cfg = DetectorConfig::default();
        for tile in plan_tiles(5000, 4200, &cfg).expect("tiled") {
            // Whatever a tile owns, it must actually have seen.
            assert!(tile.own_x0 >= tile.x as f32);
            assert!(tile.own_y0 >= tile.y as f32);
            assert!(tile.own_x1 <= (tile.x + tile.width) as f32 + 1.0);
            assert!(tile.own_y1 <= (tile.y + tile.height) as f32 + 1.0);
        }
    }

    #[test]
    fn duplicates_on_a_seam_are_dropped_keeping_the_better_score() {
        let mut boxes = vec![
            DetectedBox {
                quad: Quad::from_rect(Rect::new(10.0, 10.0, 110.0, 40.0)),
                score: 0.7,
            },
            DetectedBox {
                quad: Quad::from_rect(Rect::new(12.0, 11.0, 112.0, 41.0)),
                score: 0.9,
            },
            DetectedBox {
                quad: Quad::from_rect(Rect::new(400.0, 10.0, 500.0, 40.0)),
                score: 0.8,
            },
        ];
        deduplicate(&mut boxes, 0.3);
        assert_eq!(boxes.len(), 2);
        assert!((boxes[0].score - 0.9).abs() < 1e-6, "best score survives");
    }

    #[test]
    fn translate_moves_a_quad_into_page_space() {
        let q = translate(
            &Quad::from_rect(Rect::new(0.0, 0.0, 10.0, 10.0)),
            100.0,
            50.0,
        );
        let b = q.bounds();
        assert_eq!((b.x0, b.y0, b.x1, b.y1), (100.0, 50.0, 110.0, 60.0));
    }

    #[test]
    fn normalization_is_channel_planar_and_zero_centred() {
        let img = RgbImage::from_pixel(2, 2, image::Rgb([124, 116, 104]));
        let arr = normalize_det(&img);
        assert_eq!(arr.shape(), &[1, 3, 2, 2]);
        // 124/255 ≈ 0.486 ≈ mean → close to zero after normalization.
        assert!(arr.iter().all(|v| v.abs() < 0.05), "{arr:?}");
    }
}
