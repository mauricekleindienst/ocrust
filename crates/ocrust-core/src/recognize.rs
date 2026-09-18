//! CTC text recognition (PP-OCR `rec`) with per-character positions.

use image::RgbImage;
use ndarray::ArrayD;

use crate::dict::CharDict;
use crate::error::{Error, Result};
use crate::runtime::{nchw, OnnxModel, SessionOptions};

/// Recognition hyper-parameters. Defaults match PP-OCRv4/v5 exports.
#[derive(Debug, Clone)]
pub struct RecognizerConfig {
    /// Fixed input height the model was trained with (48 for v4/v5, 32 for v2).
    pub image_height: u32,
    /// Upper bound for the padded batch width, protecting memory on very long lines.
    pub max_image_width: u32,
    /// Crops per inference call.
    pub batch_size: usize,
    /// Lines below this mean character confidence are dropped.
    pub drop_score: f32,
    /// Consider restoring a space when two glyphs are this many median glyph
    /// widths apart. `0.0` turns the whole pass off.
    ///
    /// A CTC recognizer emits a space only when it is confident about the space
    /// class, and on slightly degraded input — a JPEG-compressed scan, a fax — it
    /// drops them, turning `88 EUR` into `88EUR`. The timeline alone cannot tell
    /// that from the gap a wide `M` leaves behind, so this is only the cheap
    /// pre-filter; `space_ink_fraction` decides.
    pub space_gap_factor: f32,
    /// How much empty paper a restored space needs, as a fraction of the crop
    /// height.
    ///
    /// Measured on 200 dpi invoices: a swallowed word space leaves 0.38 to 0.40
    /// of the crop height in blank columns, the gap after a capital `M` or
    /// between a doubled `mm` leaves 0.04 to 0.06, and the widest innocent case —
    /// the paper around a narrow `1` in `1187` — reaches 0.27.
    pub space_ink_fraction: f32,
}

impl Default for RecognizerConfig {
    fn default() -> Self {
        Self {
            image_height: 48,
            max_image_width: 1600,
            batch_size: 8,
            drop_score: 0.5,
            space_gap_factor: 2.0,
            space_ink_fraction: 0.3,
        }
    }
}

/// One recognized character with where it sits along the line.
#[derive(Debug, Clone)]
pub struct CharSpan {
    pub text: String,
    /// Horizontal centre as a fraction of the line width, in `0..=1`.
    pub x_center: f32,
    /// Width as a fraction of the line width.
    pub x_width: f32,
    pub confidence: f32,
}

/// Recognition result for a single crop.
#[derive(Debug, Clone, Default)]
pub struct Recognition {
    pub text: String,
    /// Mean probability over the emitted characters, in `0..=1`.
    pub confidence: f32,
    pub chars: Vec<CharSpan>,
}

/// Wraps a CTC recognition model together with its character set.
#[derive(Debug)]
pub struct TextRecognizer {
    model: OnnxModel,
    dict: CharDict,
    config: RecognizerConfig,
    num_classes: usize,
}

impl TextRecognizer {
    /// Loads a recognition model.
    ///
    /// When `dict_path` is `None` the dictionary embedded in the ONNX file is
    /// used, which is how recent PP-OCR exports ship.
    pub fn new(
        path: &std::path::Path,
        dict_path: Option<&std::path::Path>,
        config: RecognizerConfig,
        session: &SessionOptions,
    ) -> Result<Self> {
        let model = OnnxModel::load(path, "recognition", session)?;
        let num_classes = probe_num_classes(&model, config.image_height)?;
        let dict = match dict_path {
            Some(p) => CharDict::from_file(p, Some(num_classes))?,
            None => {
                let embedded = model.metadata("character").ok_or_else(|| {
                    Error::Dict(format!(
                        "recognition model {} has no embedded character set; pass a dictionary file",
                        path.display()
                    ))
                })?;
                CharDict::from_model_metadata(embedded, Some(num_classes))?
            }
        };
        Ok(Self {
            model,
            dict,
            config,
            num_classes,
        })
    }

    pub fn config(&self) -> &RecognizerConfig {
        &self.config
    }

    pub fn num_classes(&self) -> usize {
        self.num_classes
    }

    /// The model's character set, for language-coverage checks.
    pub fn dict(&self) -> &CharDict {
        &self.dict
    }

    /// Recognizes a batch of line crops, preserving input order.
    ///
    /// Crops are grouped by aspect ratio so that padding stays small, which is
    /// where most of the throughput of a batched CTC recognizer comes from.
    pub fn recognize(&self, crops: &[RgbImage]) -> Result<Vec<Recognition>> {
        if crops.is_empty() {
            return Ok(Vec::new());
        }
        let mut order: Vec<usize> = (0..crops.len()).collect();
        let ratio = |img: &RgbImage| img.width() as f32 / img.height().max(1) as f32;
        order.sort_by(|&a, &b| {
            ratio(&crops[a])
                .partial_cmp(&ratio(&crops[b]))
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        let mut results = vec![Recognition::default(); crops.len()];
        for chunk in order.chunks(self.config.batch_size.max(1)) {
            let batch: Vec<&RgbImage> = chunk.iter().map(|&i| &crops[i]).collect();
            let decoded = self.run_batch(&batch)?;
            for (&idx, rec) in chunk.iter().zip(decoded) {
                results[idx] = rec;
            }
        }
        Ok(results)
    }

    fn run_batch(&self, batch: &[&RgbImage]) -> Result<Vec<Recognition>> {
        let h = self.config.image_height.max(8) as usize;
        let max_ratio = batch
            .iter()
            .map(|img| img.width() as f32 / img.height().max(1) as f32)
            .fold(1.0f32, f32::max);
        let padded_w =
            ((h as f32 * max_ratio).ceil() as usize).clamp(h, self.config.max_image_width as usize);

        let mut data = vec![0f32; batch.len() * 3 * h * padded_w];
        // Fraction of `padded_w` that carries real pixels, per item.
        let mut valid = vec![1.0f32; batch.len()];
        // Column ink of each crop, which is what tells a word space from the
        // gap the CTC timeline leaves after a wide capital.
        let mut profiles: Vec<InkProfile> = Vec::with_capacity(batch.len());
        for (b, img) in batch.iter().enumerate() {
            let scale = h as f32 / img.height().max(1) as f32;
            let target_w = ((img.width() as f32 * scale).round() as usize).clamp(1, padded_w);
            let resized = image::imageops::resize(
                *img,
                target_w as u32,
                h as u32,
                image::imageops::FilterType::Triangle,
            );
            valid[b] = target_w as f32 / padded_w as f32;
            profiles.push(InkProfile::of(&resized));
            let raw = resized.as_raw();
            let plane = h * padded_w;
            for c in 0..3 {
                let base = b * 3 * plane + c * plane;
                for y in 0..h {
                    for x in 0..target_w {
                        let v = raw[(y * target_w + x) * 3 + c] as f32 / 255.0;
                        data[base + y * padded_w + x] = (v - 0.5) / 0.5;
                    }
                }
            }
        }

        let logits = self.model.run(nchw(batch.len(), 3, h, padded_w, data))?;
        self.decode(&logits, &valid, &profiles)
    }

    /// Greedy CTC decoding with per-character positions and confidence.
    fn decode(
        &self,
        logits: &ArrayD<f32>,
        valid: &[f32],
        profiles: &[InkProfile],
    ) -> Result<Vec<Recognition>> {
        decode_ctc(logits, valid, &self.dict, &self.config, profiles)
    }
}

/// Greedy CTC decoding, split out from [`TextRecognizer`] so it can be tested
/// without loading a model.
fn decode_ctc(
    logits: &ArrayD<f32>,
    valid: &[f32],
    dict: &CharDict,
    cfg: &RecognizerConfig,
    profiles: &[InkProfile],
) -> Result<Vec<Recognition>> {
    let shape = logits.shape();
    if shape.len() != 3 {
        return Err(Error::model(format!(
            "unexpected recognition output shape {shape:?}, expected [batch, time, classes]"
        )));
    }
    let (n, t, c) = (shape[0], shape[1], shape[2]);
    let flat = logits
        .as_slice()
        .map(std::borrow::Cow::Borrowed)
        .unwrap_or_else(|| std::borrow::Cow::Owned(logits.iter().copied().collect()));

    let mut out = Vec::with_capacity(n);
    for b in 0..n {
        let mut text = String::new();
        let mut chars: Vec<CharSpan> = Vec::new();
        let mut conf_sum = 0.0f32;
        let mut conf_n = 0u32;
        let mut prev_class = usize::MAX;
        // Only the non-padded part of the sequence carries signal.
        let valid_frac = valid.get(b).copied().unwrap_or(1.0).clamp(0.05, 1.0);
        let t_valid = ((t as f32 * valid_frac).ceil() as usize).clamp(1, t);

        for step in 0..t_valid {
            let row = &flat[(b * t + step) * c..(b * t + step + 1) * c];
            let (best, &prob) = row
                .iter()
                .enumerate()
                .max_by(|(_, x), (_, y)| x.partial_cmp(y).unwrap_or(std::cmp::Ordering::Equal))
                .expect("non-empty class axis");
            let repeated = best == prev_class;
            prev_class = best;
            if best == 0 {
                continue; // CTC blank
            }
            // Map the timestep onto the line: each step covers an equal
            // slice of the *valid* (unpadded) crop width.
            let step_w = 1.0 / t_valid as f32;
            if repeated {
                // The same class two steps running is one wide glyph, not two
                // characters: grow the span instead of emitting again, so `m`
                // ends up wider than `i` and the gaps between glyphs mean
                // something.
                if let Some(last) = chars.last_mut() {
                    last.x_width += step_w;
                    last.x_center += step_w / 2.0;
                }
                continue;
            }
            let Some(ch) = dict.get(best) else {
                continue;
            };
            text.push_str(ch);
            chars.push(CharSpan {
                text: ch.to_string(),
                x_center: (step as f32 + 0.5) * step_w,
                x_width: step_w,
                confidence: prob,
            });
            conf_sum += prob;
            conf_n += 1;
        }

        let confidence = if conf_n == 0 {
            0.0
        } else {
            conf_sum / conf_n as f32
        };
        if let Some(profile) = profiles.get(b) {
            if restore_spaces(&mut chars, profile, cfg) {
                text = chars.iter().map(|c| c.text.as_str()).collect();
            }
        }
        out.push(Recognition {
            text,
            confidence,
            chars,
        });
    }
    Ok(out)
}

/// Column ink of one recognition crop, at the model's input scale.
///
/// A CTC timeline says where the classifier fired, not where the paper is empty:
/// the gap after a wide `M` looks the same as a missing word space. The pixels
/// know the difference, and this is the cheapest way to ask them — one pass over
/// the crop that is being copied into the input tensor anyway.
#[derive(Debug, Clone, Default)]
pub(crate) struct InkProfile {
    /// Ink per column, 0 for empty paper, normalized to the crop's contrast.
    columns: Vec<f32>,
    /// Crop height in pixels, the reference for how wide a space has to be.
    height: usize,
}

impl InkProfile {
    /// Measures a resized crop: ink is how far a pixel is below the paper.
    fn of(crop: &RgbImage) -> Self {
        let (w, h) = (crop.width() as usize, crop.height() as usize);
        if w == 0 || h == 0 {
            return Self::default();
        }
        let raw = crop.as_raw();
        let luma = |x: usize, y: usize| {
            let i = (y * w + x) * 3;
            0.299 * raw[i] as f32 + 0.587 * raw[i + 1] as f32 + 0.114 * raw[i + 2] as f32
        };
        // Paper is the bright end of the crop; taking a high percentile rather
        // than the maximum keeps a single blown-out pixel from setting it.
        let mut samples: Vec<f32> = (0..w).map(|x| luma(x, h / 2)).collect();
        samples.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let paper = samples[(samples.len() * 9 / 10).min(samples.len() - 1)].max(1.0);
        let columns = (0..w)
            .map(|x| {
                let ink: f32 = (0..h).map(|y| (paper - luma(x, y)).max(0.0)).sum();
                ink / (h as f32 * paper)
            })
            .collect();
        Self { height: h, columns }
    }

    /// Builds a profile from measured columns, for tests.
    #[cfg(test)]
    fn from_columns(columns: Vec<f32>, height: usize) -> Self {
        Self { columns, height }
    }

    /// Whether the profile carries no measurement.
    fn is_empty(&self) -> bool {
        self.columns.is_empty() || self.height == 0
    }

    /// Longest run of empty columns between two positions, in crop heights.
    ///
    /// `from` and `to` are fractions of the crop width, as the character spans
    /// report them.
    fn blank_run(&self, from: f32, to: f32) -> f32 {
        if self.columns.is_empty() || self.height == 0 || to <= from {
            return 0.0;
        }
        let w = self.columns.len() as f32;
        let start = ((from * w).floor().max(0.0) as usize).min(self.columns.len() - 1);
        let end = ((to * w).ceil() as usize).min(self.columns.len());
        let mut best = 0usize;
        let mut run = 0usize;
        for &ink in &self.columns[start..end] {
            if ink < INK_EMPTY {
                run += 1;
                best = best.max(run);
            } else {
                run = 0;
            }
        }
        best as f32 / self.height as f32
    }
}

/// Column ink below this counts as empty paper.
const INK_EMPTY: f32 = 0.02;

/// Adds the spaces the recognizer swallowed, from the gaps and the pixels.
///
/// Returns whether anything was inserted. A gap has to be both wide on the CTC
/// timeline and empty on the paper, because either signal alone is wrong: the
/// timeline leaves a wide gap after a capital `M`, and a narrow `1` is
/// surrounded by blank columns. Only scripts that separate words with spaces are
/// touched.
fn restore_spaces(chars: &mut Vec<CharSpan>, ink: &InkProfile, cfg: &RecognizerConfig) -> bool {
    if chars.len() < 2 || cfg.space_gap_factor <= 0.0 || ink.is_empty() {
        return false;
    }
    let mut widths: Vec<f32> = chars.iter().map(|c| c.x_width).collect();
    widths.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let min_gap = widths[widths.len() / 2].max(1e-6) * cfg.space_gap_factor;

    let mut out: Vec<CharSpan> = Vec::with_capacity(chars.len());
    let mut inserted = false;
    for span in chars.drain(..) {
        if let Some(previous) = out.last() {
            let from = previous.x_center + previous.x_width / 2.0;
            let to = span.x_center - span.x_width / 2.0;
            let gap = to - from;
            if gap > min_gap
                && spaces_apply(previous, &span)
                && ink.blank_run(from, to) >= cfg.space_ink_fraction
            {
                out.push(CharSpan {
                    text: " ".to_string(),
                    x_center: from + gap / 2.0,
                    x_width: gap,
                    confidence: previous.confidence.min(span.confidence),
                });
                inserted = true;
            }
        }
        out.push(span);
    }
    *chars = out;
    inserted
}

/// Whether a gap between these two spans could be a missing space.
///
/// Typography rules out some pairs whatever the pixels say: nothing is ever
/// spaced off a following full stop or comma, and an opening bracket is never
/// followed by one. Without that, a scan of `17.03.2026` whose `.` sits in its
/// own patch of paper comes back as `17 .03.2026`.
fn spaces_apply(left: &CharSpan, right: &CharSpan) -> bool {
    let spaced = |s: &str| {
        !s.is_empty()
            && !s
                .chars()
                .any(|c| c.is_whitespace() || is_unspaced_script(c))
    };
    if !spaced(&left.text) || !spaced(&right.text) {
        return false;
    }
    let trailing = left.text.chars().next_back();
    let leading = right.text.chars().next();
    !leading.is_some_and(no_space_before) && !trailing.is_some_and(no_space_after)
}

/// Characters that never take a space in front of them.
fn no_space_before(c: char) -> bool {
    matches!(
        c,
        '.' | ',' | ':' | ';' | '!' | '?' | ')' | ']' | '}' | '\'' | '»' | '…'
    )
}

/// Characters that never take a space after them.
fn no_space_after(c: char) -> bool {
    matches!(c, '(' | '[' | '{' | '«' | '/')
}

/// True for scripts written without spaces between words.
fn is_unspaced_script(c: char) -> bool {
    matches!(c as u32,
        0x3040..=0x30FF      // Hiragana, Katakana
        | 0x3400..=0x4DBF    // CJK extension A
        | 0x4E00..=0x9FFF    // CJK unified
        | 0xAC00..=0xD7AF    // Hangul syllables
        | 0xF900..=0xFAFF    // CJK compatibility
        | 0xFF00..=0xFFEF    // halfwidth and fullwidth forms
        | 0x20000..=0x2FA1F  // CJK extensions B and up
    )
}

/// Runs a 1x3xHx(4H) dummy input to learn the class count and warm the session.
fn probe_num_classes(model: &OnnxModel, height: u32) -> Result<usize> {
    let h = height.max(8) as usize;
    let w = h * 4;
    let out = model.run(nchw(1, 3, h, w, vec![0.0; 3 * h * w]))?;
    let shape = out.shape();
    shape.last().copied().filter(|&c| c > 1).ok_or_else(|| {
        Error::model(format!(
            "could not read class count from output shape {shape:?}"
        ))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use ndarray::IxDyn;

    /// Builds a `[batch, time, classes]` tensor from per-step class indices.
    fn logits(steps: &[&[usize]], classes: usize) -> ArrayD<f32> {
        let n = steps.len();
        let t = steps.iter().map(|s| s.len()).max().unwrap_or(1);
        let mut data = vec![0.02f32; n * t * classes];
        for (b, seq) in steps.iter().enumerate() {
            for (i, &cls) in seq.iter().enumerate() {
                data[(b * t + i) * classes + cls] = 0.9;
            }
        }
        ArrayD::from_shape_vec(IxDyn(&[n, t, classes]), data).unwrap()
    }

    /// A configuration with space restoration switched off.
    fn off() -> RecognizerConfig {
        RecognizerConfig {
            space_gap_factor: 0.0,
            ..RecognizerConfig::default()
        }
    }

    #[test]
    fn collapses_repeats_and_blanks() {
        // classes: 0 blank, 1 a, 2 b, 3 c, 4 space
        let dict = CharDict::from_lines(["a", "b", "c"], Some(5)).unwrap();
        let out = decode_ctc(
            &logits(&[&[1, 1, 0, 1, 2, 0, 3]], 5),
            &[1.0],
            &dict,
            &off(),
            &[],
        )
        .unwrap();
        assert_eq!(out[0].text, "aabc");
        assert!(out[0].confidence > 0.8, "{}", out[0].confidence);
        assert_eq!(out[0].chars.len(), 4);
    }

    #[test]
    fn wide_glyphs_keep_one_span() {
        // The same class two steps running is one wide glyph, not two letters.
        let dict = CharDict::from_lines(["a", "b"], Some(4)).unwrap();
        let out = decode_ctc(&logits(&[&[1, 1, 1, 0, 2]], 4), &[1.0], &dict, &off(), &[]).unwrap();
        assert_eq!(out[0].text, "ab");
        assert_eq!(out[0].chars.len(), 2);
        // `a` covers three of the five steps, `b` one.
        assert!(out[0].chars[0].x_width > out[0].chars[1].x_width * 2.0);
    }

    /// A crop whose paper is blank between `from` and `to` (fractions of the
    /// width) and inked everywhere else.
    fn ink_with_hole(from: f32, to: f32, height: usize) -> InkProfile {
        let width = 100usize;
        let columns = (0..width)
            .map(|x| {
                let f = x as f32 / width as f32;
                if f >= from && f < to {
                    0.0
                } else {
                    0.5
                }
            })
            .collect();
        InkProfile::from_columns(columns, height)
    }

    #[test]
    fn a_swallowed_space_is_restored_where_the_paper_is_blank() {
        // classes: 0 blank, 1 a, 2 b, 3 space. Two letters far apart on the CTC
        // timeline, with empty paper between them: that is a space the
        // classifier did not report.
        let dict = CharDict::from_lines(["a", "b"], Some(4)).unwrap();
        let wide = logits(&[&[1, 0, 0, 0, 0, 0, 0, 2]], 4);
        // The crop is 20 columns tall, so a 40-column hole is 2.0 crop heights.
        let blank = [ink_with_hole(0.15, 0.85, 20)];
        let out = decode_ctc(&wide, &[1.0], &dict, &RecognizerConfig::default(), &blank).unwrap();
        assert_eq!(out[0].text, "a b");
        assert_eq!(out[0].chars.len(), 3);
        assert_eq!(out[0].chars[1].text, " ");
    }

    #[test]
    fn a_wide_gap_over_inked_paper_is_not_a_space() {
        // The same timeline, but the gap is where a wide `M` was drawn: no space.
        let dict = CharDict::from_lines(["a", "b"], Some(4)).unwrap();
        let wide = logits(&[&[1, 0, 0, 0, 0, 0, 0, 2]], 4);
        let inked = [InkProfile::from_columns(vec![0.5; 100], 20)];
        let out = decode_ctc(&wide, &[1.0], &dict, &RecognizerConfig::default(), &inked).unwrap();
        assert_eq!(out[0].text, "ab");

        // Blank paper that is too narrow to be a space is not one either: a
        // 4-column hole in a 20-pixel-tall crop is 0.2 crop heights.
        let narrow = [ink_with_hole(0.48, 0.52, 20)];
        let out = decode_ctc(&wide, &[1.0], &dict, &RecognizerConfig::default(), &narrow).unwrap();
        assert_eq!(out[0].text, "ab");
    }

    #[test]
    fn neighbouring_letters_are_not_pulled_apart() {
        let dict = CharDict::from_lines(["a", "b"], Some(4)).unwrap();
        let tight = logits(&[&[1, 0, 2]], 4);
        let blank = [ink_with_hole(0.15, 0.85, 20)];
        let out = decode_ctc(&tight, &[1.0], &dict, &RecognizerConfig::default(), &blank).unwrap();
        assert_eq!(out[0].text, "ab");
    }

    #[test]
    fn space_restoration_can_be_turned_off() {
        let dict = CharDict::from_lines(["a", "b"], Some(4)).unwrap();
        let wide = logits(&[&[1, 0, 0, 0, 0, 0, 0, 2]], 4);
        let blank = [ink_with_hole(0.15, 0.85, 20)];
        let out = decode_ctc(&wide, &[1.0], &dict, &off(), &blank).unwrap();
        assert_eq!(out[0].text, "ab");
    }

    #[test]
    fn punctuation_is_never_spaced_off() {
        // classes: 0 blank, 1 digit, 2 full stop, 3 space. A scanned `17.03`
        // whose stop sits in its own patch of paper must not become `17 .03`.
        let dict = CharDict::from_lines(["7", "."], Some(4)).unwrap();
        let blank = [ink_with_hole(0.15, 0.85, 20)];
        let out = decode_ctc(
            &logits(&[&[1, 0, 0, 0, 0, 0, 0, 2]], 4),
            &[1.0],
            &dict,
            &RecognizerConfig::default(),
            &blank,
        )
        .unwrap();
        assert_eq!(out[0].text, "7.");
    }

    #[test]
    fn spaces_are_not_invented_in_cjk() {
        // Japanese and Chinese are written without spaces, so blank paper
        // between two kanji means nothing.
        let dict = CharDict::from_lines(["日", "本"], Some(4)).unwrap();
        let blank = [ink_with_hole(0.15, 0.85, 20)];
        let out = decode_ctc(
            &logits(&[&[1, 0, 0, 0, 0, 0, 0, 2]], 4),
            &[1.0],
            &dict,
            &RecognizerConfig::default(),
            &blank,
        )
        .unwrap();
        assert_eq!(out[0].text, "日本");
    }

    #[test]
    fn a_reported_space_is_not_doubled() {
        // classes: 0 blank, 1 a, 2 b, 3 space.
        let dict = CharDict::from_lines(["a", "b"], Some(4)).unwrap();
        let blank = [ink_with_hole(0.15, 0.85, 20)];
        let out = decode_ctc(
            &logits(&[&[1, 0, 0, 0, 3, 0, 0, 0, 2]], 4),
            &[1.0],
            &dict,
            &RecognizerConfig::default(),
            &blank,
        )
        .unwrap();
        assert_eq!(out[0].text, "a b");
    }

    #[test]
    fn space_class_is_emitted() {
        let dict = CharDict::from_lines(["a", "b"], Some(4)).unwrap();
        let out = decode_ctc(&logits(&[&[1, 3, 2]], 4), &[1.0], &dict, &off(), &[]).unwrap();
        assert_eq!(out[0].text, "a b");
    }

    #[test]
    fn padding_is_ignored_when_valid_fraction_is_small() {
        // The second half of the sequence is padding noise and must not be read.
        let dict = CharDict::from_lines(["a", "b"], Some(4)).unwrap();
        let out = decode_ctc(&logits(&[&[1, 0, 2, 2]], 4), &[0.5], &dict, &off(), &[]).unwrap();
        assert_eq!(out[0].text, "a");
    }

    #[test]
    fn char_positions_increase_left_to_right() {
        let dict = CharDict::from_lines(["a", "b", "c"], Some(5)).unwrap();
        let out = decode_ctc(&logits(&[&[1, 0, 2, 0, 3]], 5), &[1.0], &dict, &off(), &[]).unwrap();
        let xs: Vec<f32> = out[0].chars.iter().map(|c| c.x_center).collect();
        assert!(xs.windows(2).all(|w| w[0] < w[1]), "{xs:?}");
        assert!(xs.iter().all(|&x| (0.0..=1.0).contains(&x)), "{xs:?}");
    }

    #[test]
    fn batch_rows_decode_independently() {
        let dict = CharDict::from_lines(["a", "b", "c"], Some(5)).unwrap();
        let out = decode_ctc(
            &logits(&[&[1, 2], &[3, 3]], 5),
            &[1.0, 1.0],
            &dict,
            &off(),
            &[],
        )
        .unwrap();
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].text, "ab");
        assert_eq!(out[1].text, "c");
    }

    #[test]
    fn wrong_rank_is_rejected() {
        let dict = CharDict::from_lines(["a"], Some(2)).unwrap();
        let bad = ArrayD::from_shape_vec(IxDyn(&[2, 3]), vec![0.5; 6]).unwrap();
        assert!(decode_ctc(&bad, &[1.0], &dict, &off(), &[]).is_err());
    }
}
