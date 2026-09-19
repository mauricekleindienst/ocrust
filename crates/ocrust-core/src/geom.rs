//! Geometry primitives for text boxes: quads, convex hulls, minimum-area
//! rectangles and perspective crops.
//!
//! Text detectors emit blobs, not rectangles, so almost every step of the
//! pipeline needs cheap and *correct* polygon maths. Everything here works in
//! `f32` image coordinates where `(0, 0)` is the top-left pixel corner.

use image::RgbImage;
use imageproc::geometric_transformations::{warp_into, Interpolation, Projection};
use serde::{Deserialize, Serialize};

/// A point in image space.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct Point {
    pub x: f32,
    pub y: f32,
}

impl Point {
    pub const fn new(x: f32, y: f32) -> Self {
        Self { x, y }
    }

    fn sub(self, other: Self) -> Self {
        Self::new(self.x - other.x, self.y - other.y)
    }

    fn cross(self, other: Self) -> f32 {
        self.x * other.y - self.y * other.x
    }

    fn norm(self) -> f32 {
        self.x.hypot(self.y)
    }

    /// Euclidean distance to `other`.
    pub fn distance(self, other: Self) -> f32 {
        self.sub(other).norm()
    }
}

/// An axis-aligned rectangle, always normalized so `x1 >= x0` and `y1 >= y0`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct Rect {
    pub x0: f32,
    pub y0: f32,
    pub x1: f32,
    pub y1: f32,
}

impl Rect {
    pub fn new(x0: f32, y0: f32, x1: f32, y1: f32) -> Self {
        Self {
            x0: x0.min(x1),
            y0: y0.min(y1),
            x1: x0.max(x1),
            y1: y0.max(y1),
        }
    }

    pub fn width(&self) -> f32 {
        self.x1 - self.x0
    }

    pub fn height(&self) -> f32 {
        self.y1 - self.y0
    }

    pub fn area(&self) -> f32 {
        self.width() * self.height()
    }

    pub fn center_y(&self) -> f32 {
        (self.y0 + self.y1) * 0.5
    }

    pub fn center_x(&self) -> f32 {
        (self.x0 + self.x1) * 0.5
    }

    /// Size of the vertical overlap with `other` (0 when disjoint).
    pub fn vertical_overlap(&self, other: &Rect) -> f32 {
        (self.y1.min(other.y1) - self.y0.max(other.y0)).max(0.0)
    }

    /// Size of the horizontal overlap with `other` (0 when disjoint).
    pub fn horizontal_overlap(&self, other: &Rect) -> f32 {
        (self.x1.min(other.x1) - self.x0.max(other.x0)).max(0.0)
    }

    /// Smallest rectangle containing both inputs.
    pub fn union(&self, other: &Rect) -> Rect {
        Rect {
            x0: self.x0.min(other.x0),
            y0: self.y0.min(other.y0),
            x1: self.x1.max(other.x1),
            y1: self.y1.max(other.y1),
        }
    }

    /// Intersection over union, the usual box-similarity measure.
    pub fn iou(&self, other: &Rect) -> f32 {
        let inter = self.horizontal_overlap(other) * self.vertical_overlap(other);
        let union = self.area() + other.area() - inter;
        if union <= 0.0 {
            0.0
        } else {
            inter / union
        }
    }
}

/// A four-point polygon around one text line, in `[top-left, top-right,
/// bottom-right, bottom-left]` order once [`Quad::ordered`] has run.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct Quad {
    pub points: [Point; 4],
}

impl Quad {
    pub const fn new(points: [Point; 4]) -> Self {
        Self { points }
    }

    /// Quad covering an axis-aligned rectangle.
    pub fn from_rect(r: Rect) -> Self {
        Self::new([
            Point::new(r.x0, r.y0),
            Point::new(r.x1, r.y0),
            Point::new(r.x1, r.y1),
            Point::new(r.x0, r.y1),
        ])
    }

    /// Tight axis-aligned bounds.
    pub fn bounds(&self) -> Rect {
        let mut r = Rect {
            x0: f32::MAX,
            y0: f32::MAX,
            x1: f32::MIN,
            y1: f32::MIN,
        };
        for p in self.points {
            r.x0 = r.x0.min(p.x);
            r.y0 = r.y0.min(p.y);
            r.x1 = r.x1.max(p.x);
            r.y1 = r.y1.max(p.y);
        }
        r
    }

    /// Reorders the corners to `[tl, tr, br, bl]`.
    ///
    /// Sorting by `x` and then splitting by `y` is robust for the near-axis
    /// aligned quads a text detector produces, including moderate rotation.
    pub fn ordered(&self) -> Quad {
        let mut pts = self.points;
        pts.sort_by(|a, b| a.x.partial_cmp(&b.x).unwrap_or(std::cmp::Ordering::Equal));
        let (mut left, mut right) = ([pts[0], pts[1]], [pts[2], pts[3]]);
        if left[0].y > left[1].y {
            left.swap(0, 1);
        }
        if right[0].y > right[1].y {
            right.swap(0, 1);
        }
        Quad::new([left[0], right[0], right[1], left[1]])
    }

    /// Longest horizontal edge, i.e. the text-line length in pixels.
    pub fn edge_width(&self) -> f32 {
        let p = self.points;
        p[0].distance(p[1]).max(p[3].distance(p[2]))
    }

    /// Longest vertical edge, i.e. the text-line height in pixels.
    pub fn edge_height(&self) -> f32 {
        let p = self.points;
        p[0].distance(p[3]).max(p[1].distance(p[2]))
    }

    /// Rotation of the top edge in degrees, positive clockwise.
    pub fn angle_deg(&self) -> f32 {
        let d = self.points[1].sub(self.points[0]);
        d.y.atan2(d.x).to_degrees()
    }

    /// Shoelace area (always non-negative).
    pub fn area(&self) -> f32 {
        let p = &self.points;
        let mut acc = 0.0;
        for i in 0..4 {
            let a = p[i];
            let b = p[(i + 1) % 4];
            acc += a.x * b.y - b.x * a.y;
        }
        acc.abs() * 0.5
    }

    pub fn perimeter(&self) -> f32 {
        let p = &self.points;
        (0..4).map(|i| p[i].distance(p[(i + 1) % 4])).sum()
    }

    /// True when `(x, y)` lies inside the quad (winding test, edges included).
    pub fn contains(&self, x: f32, y: f32) -> bool {
        let p = Point::new(x, y);
        let mut sign = 0i8;
        for i in 0..4 {
            let a = self.points[i];
            let b = self.points[(i + 1) % 4];
            let cr = b.sub(a).cross(p.sub(a));
            if cr.abs() < 1e-6 {
                continue;
            }
            let s = if cr > 0.0 { 1 } else { -1 };
            if sign == 0 {
                sign = s;
            } else if sign != s {
                return false;
            }
        }
        true
    }

    /// Offsets the four corners outwards along the quad's own axes.
    ///
    /// For a rotated rectangle this is the exact polygon offset (width and
    /// height each grow by `2 * delta`), which is what DB's "unclip" step
    /// needs; [`Quad::expanded`] only approximates it for general polygons.
    pub fn expanded_rect(&self, delta: f32) -> Quad {
        let p = self.ordered().points;
        let unit = |a: Point, b: Point| {
            let d = b.sub(a);
            let n = d.norm();
            if n < 1e-6 {
                Point::new(0.0, 0.0)
            } else {
                Point::new(d.x / n, d.y / n)
            }
        };
        let u = unit(p[0], p[1]); // along the text direction
        let v = unit(p[0], p[3]); // across the text line
        let off = |base: Point, su: f32, sv: f32| {
            Point::new(
                base.x + (u.x * su + v.x * sv) * delta,
                base.y + (u.y * su + v.y * sv) * delta,
            )
        };
        Quad::new([
            off(p[0], -1.0, -1.0),
            off(p[1], 1.0, -1.0),
            off(p[2], 1.0, 1.0),
            off(p[3], -1.0, 1.0),
        ])
    }

    /// Scales every corner by `(sx, sy)` and clamps to `(0, 0)..(w, h)`.
    pub fn scaled_clamped(&self, sx: f32, sy: f32, w: f32, h: f32) -> Quad {
        let mut out = self.points;
        for p in out.iter_mut() {
            p.x = (p.x * sx).clamp(0.0, w);
            p.y = (p.y * sy).clamp(0.0, h);
        }
        Quad::new(out)
    }

    /// Moves every corner outwards from the centroid by `delta` pixels.
    ///
    /// This is the "unclip" step of DB-style detectors: the network is trained
    /// on shrunk text kernels, so the raw blob has to be grown back before the
    /// crop is handed to the recognizer.
    pub fn expanded(&self, delta: f32) -> Quad {
        let cx = self.points.iter().map(|p| p.x).sum::<f32>() / 4.0;
        let cy = self.points.iter().map(|p| p.y).sum::<f32>() / 4.0;
        let mut out = self.points;
        for p in out.iter_mut() {
            let dx = p.x - cx;
            let dy = p.y - cy;
            let len = dx.hypot(dy);
            if len > 1e-6 {
                p.x += dx / len * delta;
                p.y += dy / len * delta;
            }
        }
        Quad::new(out)
    }
}

/// Andrew's monotone chain convex hull. Returns the hull counter-clockwise.
pub fn convex_hull(points: &[Point]) -> Vec<Point> {
    if points.len() < 3 {
        return points.to_vec();
    }
    let mut pts = points.to_vec();
    pts.sort_by(|a, b| {
        a.x.partial_cmp(&b.x)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.y.partial_cmp(&b.y).unwrap_or(std::cmp::Ordering::Equal))
    });
    pts.dedup_by(|a, b| (a.x - b.x).abs() < 1e-6 && (a.y - b.y).abs() < 1e-6);
    if pts.len() < 3 {
        return pts;
    }

    let mut hull: Vec<Point> = Vec::with_capacity(pts.len() + 1);
    for pass in 0..2 {
        let start = hull.len();
        let iter: Box<dyn Iterator<Item = &Point>> = if pass == 0 {
            Box::new(pts.iter())
        } else {
            Box::new(pts.iter().rev())
        };
        for &p in iter {
            while hull.len() >= start + 2 {
                let a = hull[hull.len() - 2];
                let b = hull[hull.len() - 1];
                if b.sub(a).cross(p.sub(a)) <= 0.0 {
                    hull.pop();
                } else {
                    break;
                }
            }
            hull.push(p);
        }
        hull.pop();
    }
    hull
}

/// Minimum-area enclosing rectangle via rotating calipers over the convex hull.
///
/// Returns the rectangle as a quad in `[tl, tr, br, bl]` order.
pub fn min_area_rect(points: &[Point]) -> Option<Quad> {
    let hull = convex_hull(points);
    if hull.len() < 2 {
        return None;
    }
    if hull.len() == 2 {
        // Degenerate: a segment. Give it one pixel of thickness.
        let (a, b) = (hull[0], hull[1]);
        let d = b.sub(a);
        let len = d.norm().max(1e-6);
        let (nx, ny) = (-d.y / len * 0.5, d.x / len * 0.5);
        return Some(
            Quad::new([
                Point::new(a.x + nx, a.y + ny),
                Point::new(b.x + nx, b.y + ny),
                Point::new(b.x - nx, b.y - ny),
                Point::new(a.x - nx, a.y - ny),
            ])
            .ordered(),
        );
    }

    let mut best: Option<(f32, Quad)> = None;
    for i in 0..hull.len() {
        let a = hull[i];
        let b = hull[(i + 1) % hull.len()];
        let edge = b.sub(a);
        let len = edge.norm();
        if len < 1e-6 {
            continue;
        }
        // Orthonormal frame aligned with this hull edge.
        let (ux, uy) = (edge.x / len, edge.y / len);
        let (vx, vy) = (-uy, ux);

        let (mut min_u, mut max_u) = (f32::MAX, f32::MIN);
        let (mut min_v, mut max_v) = (f32::MAX, f32::MIN);
        for p in &hull {
            let d = p.sub(a);
            let pu = d.x * ux + d.y * uy;
            let pv = d.x * vx + d.y * vy;
            min_u = min_u.min(pu);
            max_u = max_u.max(pu);
            min_v = min_v.min(pv);
            max_v = max_v.max(pv);
        }
        let area = (max_u - min_u) * (max_v - min_v);
        if best.as_ref().is_some_and(|(b_area, _)| *b_area <= area) {
            continue;
        }
        let corner = |u: f32, v: f32| Point::new(a.x + ux * u + vx * v, a.y + uy * u + vy * v);
        let quad = Quad::new([
            corner(min_u, min_v),
            corner(max_u, min_v),
            corner(max_u, max_v),
            corner(min_u, max_v),
        ]);
        best = Some((area, quad));
    }
    best.map(|(_, q)| q.ordered())
}

/// Aspect ratio at which [`crop_quad`] stands a crop up.
const STAND_UP_RATIO: f32 = 1.5;

/// Whether [`crop_quad`] rotates this quad's crop 90 degrees to stand it up.
///
/// Recognition reports character positions as fractions of the *crop* width, so
/// everything that maps them back onto the page has to know which way the crop
/// was turned. Keeping the rule here means the two can never drift apart.
pub fn crop_stands_up(quad: &Quad) -> bool {
    let q = quad.ordered();
    let w = q.edge_width().round().max(1.0);
    let h = q.edge_height().round().max(1.0);
    h / w >= STAND_UP_RATIO
}

/// Cuts `quad` out of `image` and rectifies it into an upright crop.
///
/// The crop keeps the quad's own aspect ratio; when the quad is much taller
/// than wide (rotated 90°) it is rotated upright, matching what PP-OCR
/// recognition models expect.
pub fn crop_quad(image: &RgbImage, quad: &Quad) -> Option<RgbImage> {
    let q = quad.ordered();
    let w = q.edge_width().round().max(1.0);
    let h = q.edge_height().round().max(1.0);
    if w < 2.0 && h < 2.0 {
        return None;
    }
    let (out_w, out_h) = (w as u32, h as u32);

    let from = [
        (q.points[0].x, q.points[0].y),
        (q.points[1].x, q.points[1].y),
        (q.points[2].x, q.points[2].y),
        (q.points[3].x, q.points[3].y),
    ];
    let to = [
        (0.0, 0.0),
        (out_w as f32, 0.0),
        (out_w as f32, out_h as f32),
        (0.0, out_h as f32),
    ];
    // `warp_into` maps input -> output coordinates and inverts internally.
    let projection = Projection::from_control_points(from, to)?;
    let mut out = RgbImage::new(out_w, out_h);
    warp_into(
        image,
        projection,
        Interpolation::Bilinear,
        imageproc::geometric_transformations::Border::Constant(image::Rgb([255u8, 255, 255])),
        &mut out,
    );

    // Vertical text: stand the crop up so the recognizer sees a wide line.
    if out_h as f32 / out_w as f32 >= STAND_UP_RATIO {
        return Some(image::imageops::rotate90(&out));
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pts(v: &[(f32, f32)]) -> Vec<Point> {
        v.iter().map(|&(x, y)| Point::new(x, y)).collect()
    }

    #[test]
    fn tall_quads_stand_their_crop_up() {
        // The rule `crop_quad` applies, exposed so that callers mapping
        // character positions back onto the page agree with it.
        let wide = Quad::from_rect(Rect::new(0.0, 0.0, 100.0, 20.0));
        let tall = Quad::from_rect(Rect::new(0.0, 0.0, 20.0, 100.0));
        let square = Quad::from_rect(Rect::new(0.0, 0.0, 30.0, 40.0));
        assert!(!crop_stands_up(&wide));
        assert!(crop_stands_up(&tall));
        assert!(!crop_stands_up(&square), "4:3 is not vertical text");
    }

    #[test]
    fn hull_drops_interior_points() {
        let p = pts(&[
            (0.0, 0.0),
            (10.0, 0.0),
            (10.0, 10.0),
            (0.0, 10.0),
            (5.0, 5.0),
            (2.0, 8.0),
        ]);
        assert_eq!(convex_hull(&p).len(), 4);
    }

    #[test]
    fn min_area_rect_recovers_axis_aligned_box() {
        let p = pts(&[(2.0, 3.0), (12.0, 3.0), (12.0, 9.0), (2.0, 9.0), (7.0, 6.0)]);
        let q = min_area_rect(&p).expect("rect");
        let b = q.bounds();
        assert!((b.x0 - 2.0).abs() < 0.01, "{b:?}");
        assert!((b.x1 - 12.0).abs() < 0.01, "{b:?}");
        assert!((q.area() - 60.0).abs() < 0.1, "area {}", q.area());
    }

    #[test]
    fn min_area_rect_beats_bounding_box_on_rotation() {
        // A 20x6 rectangle rotated by 30 degrees: the axis-aligned bounds are
        // much larger than the true minimum-area rectangle (120 px^2).
        let (w, h, a) = (20.0f32, 6.0f32, 30f32.to_radians());
        let corners = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)];
        let rotated: Vec<Point> = corners
            .iter()
            .map(|&(x, y)| {
                Point::new(
                    x * a.cos() - y * a.sin() + 50.0,
                    x * a.sin() + y * a.cos() + 40.0,
                )
            })
            .collect();
        let q = min_area_rect(&rotated).expect("rect");
        assert!((q.area() - 120.0).abs() < 1.0, "area {}", q.area());
        assert!(q.bounds().area() > 150.0, "bounds should be looser");
        let ang = q.angle_deg();
        assert!((ang - 30.0).abs() < 1.0, "angle {ang}");
    }

    #[test]
    fn expand_grows_area() {
        let q = Quad::from_rect(Rect::new(10.0, 10.0, 30.0, 20.0));
        let e = q.expanded(3.0);
        assert!(e.area() > q.area());
        assert!(e.bounds().x0 < 10.0 && e.bounds().x1 > 30.0);
    }

    #[test]
    fn expanded_rect_grows_both_axes_exactly() {
        let q = Quad::from_rect(Rect::new(10.0, 10.0, 30.0, 20.0));
        let e = q.expanded_rect(2.0);
        let b = e.bounds();
        assert!((b.width() - 24.0).abs() < 0.01, "{b:?}");
        assert!((b.height() - 14.0).abs() < 0.01, "{b:?}");
    }

    #[test]
    fn contains_matches_rect_semantics() {
        let q = Quad::from_rect(Rect::new(0.0, 0.0, 10.0, 10.0));
        assert!(q.contains(5.0, 5.0));
        assert!(!q.contains(11.0, 5.0));
    }

    #[test]
    fn crop_quad_extracts_region() {
        let mut img = RgbImage::new(20, 20);
        for (x, y, px) in img.enumerate_pixels_mut() {
            *px = if (8..16).contains(&x) && (4..10).contains(&y) {
                image::Rgb([255, 0, 0])
            } else {
                image::Rgb([0, 0, 0])
            };
        }
        let q = Quad::from_rect(Rect::new(8.0, 4.0, 16.0, 10.0));
        let crop = crop_quad(&img, &q).expect("crop");
        assert_eq!(crop.dimensions(), (8, 6));
        assert_eq!(crop.get_pixel(4, 3), &image::Rgb([255, 0, 0]));
    }
}
