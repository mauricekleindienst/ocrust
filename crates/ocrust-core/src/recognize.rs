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
}

impl Default for RecognizerConfig {
    fn default() -> Self {
        Self {
            image_height: 48,
            max_image_width: 1600,
            batch_size: 8,
            drop_score: 0.5,
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
        self.decode(&logits, &valid)
    }

    /// Greedy CTC decoding with per-character positions and confidence.
    fn decode(&self, logits: &ArrayD<f32>, valid: &[f32]) -> Result<Vec<Recognition>> {
        decode_ctc(logits, valid, &self.dict)
    }
}

/// Greedy CTC decoding, split out from [`TextRecognizer`] so it can be tested
/// without loading a model.
fn decode_ctc(logits: &ArrayD<f32>, valid: &[f32], dict: &CharDict) -> Result<Vec<Recognition>> {
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
            if best == 0 || repeated {
                continue; // CTC blank / repeat collapse
            }
            let Some(ch) = dict.get(best) else {
                continue;
            };
            text.push_str(ch);
            // Map the timestep onto the line: each step covers an equal
            // slice of the *valid* (unpadded) crop width.
            let step_w = 1.0 / t_valid as f32;
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
        out.push(Recognition {
            text,
            confidence,
            chars,
        });
    }
    Ok(out)
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

    #[test]
    fn collapses_repeats_and_blanks() {
        // classes: 0 blank, 1 a, 2 b, 3 c, 4 space
        let dict = CharDict::from_lines(["a", "b", "c"], Some(5)).unwrap();
        let out = decode_ctc(&logits(&[&[1, 1, 0, 1, 2, 0, 3]], 5), &[1.0], &dict).unwrap();
        assert_eq!(out[0].text, "aabc");
        assert!(out[0].confidence > 0.8, "{}", out[0].confidence);
        assert_eq!(out[0].chars.len(), 4);
    }

    #[test]
    fn space_class_is_emitted() {
        let dict = CharDict::from_lines(["a", "b"], Some(4)).unwrap();
        let out = decode_ctc(&logits(&[&[1, 3, 2]], 4), &[1.0], &dict).unwrap();
        assert_eq!(out[0].text, "a b");
    }

    #[test]
    fn padding_is_ignored_when_valid_fraction_is_small() {
        // The second half of the sequence is padding noise and must not be read.
        let dict = CharDict::from_lines(["a", "b"], Some(4)).unwrap();
        let out = decode_ctc(&logits(&[&[1, 0, 2, 2]], 4), &[0.5], &dict).unwrap();
        assert_eq!(out[0].text, "a");
    }

    #[test]
    fn char_positions_increase_left_to_right() {
        let dict = CharDict::from_lines(["a", "b", "c"], Some(5)).unwrap();
        let out = decode_ctc(&logits(&[&[1, 0, 2, 0, 3]], 5), &[1.0], &dict).unwrap();
        let xs: Vec<f32> = out[0].chars.iter().map(|c| c.x_center).collect();
        assert!(xs.windows(2).all(|w| w[0] < w[1]), "{xs:?}");
        assert!(xs.iter().all(|&x| (0.0..=1.0).contains(&x)), "{xs:?}");
    }

    #[test]
    fn batch_rows_decode_independently() {
        let dict = CharDict::from_lines(["a", "b", "c"], Some(5)).unwrap();
        let out = decode_ctc(&logits(&[&[1, 2], &[3, 3]], 5), &[1.0, 1.0], &dict).unwrap();
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].text, "ab");
        assert_eq!(out[1].text, "c");
    }

    #[test]
    fn wrong_rank_is_rejected() {
        let dict = CharDict::from_lines(["a"], Some(2)).unwrap();
        let bad = ArrayD::from_shape_vec(IxDyn(&[2, 3]), vec![0.5; 6]).unwrap();
        assert!(decode_ctc(&bad, &[1.0], &dict).is_err());
    }
}
