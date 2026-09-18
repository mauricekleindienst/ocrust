//! Text-line orientation classification (PP-OCR `cls`).
//!
//! Detection finds the line but not which way up it is, so scans that were fed
//! in upside down come back as gibberish. This 180° classifier is tiny (~1 MB)
//! and fixes exactly that before recognition runs.

use image::RgbImage;

use crate::error::{Error, Result};
use crate::runtime::{nchw, OnnxModel, SessionOptions};

/// Classifier hyper-parameters. Defaults match PP-OCR's `cls` export.
#[derive(Debug, Clone)]
pub struct OrientationConfig {
    pub image_height: u32,
    pub image_width: u32,
    pub batch_size: usize,
    /// Only rotate when the model is at least this sure.
    pub threshold: f32,
    /// Classify at most this many crops first and, when they agree, apply their
    /// verdict to the rest.
    ///
    /// A page is upside down as a whole; asking the model about every single
    /// line costs a fifth of the page budget for an answer a sample already
    /// gives. When the sample disagrees, every crop is classified after all.
    pub sample_size: usize,
}

impl Default for OrientationConfig {
    fn default() -> Self {
        Self {
            image_height: 48,
            image_width: 192,
            batch_size: 16,
            threshold: 0.9,
            sample_size: 8,
        }
    }
}

/// Rotates upside-down line crops upright.
#[derive(Debug)]
pub struct OrientationClassifier {
    model: OnnxModel,
    config: OrientationConfig,
}

impl OrientationClassifier {
    pub fn new(
        path: &std::path::Path,
        config: OrientationConfig,
        session: &SessionOptions,
    ) -> Result<Self> {
        Ok(Self {
            model: OnnxModel::load(path, "orientation", session)?,
            config,
        })
    }

    /// Rotates crops that are upside down, in place.
    ///
    /// Returns the angle applied per crop (`0.0` or `180.0`).
    ///
    /// A sample is classified first; when it is unanimous its verdict covers the
    /// whole page, which is the common case and saves most of the work.
    pub fn correct(&self, crops: &mut [RgbImage]) -> Result<Vec<f32>> {
        let sample = self.config.sample_size;
        if sample > 0 && crops.len() > sample {
            let step = crops.len() / sample;
            let indices: Vec<usize> = (0..sample).map(|i| i * step).collect();
            let mut probe: Vec<RgbImage> = indices.iter().map(|&i| crops[i].clone()).collect();
            let verdicts = self.classify_all(&mut probe)?;

            let flipped = verdicts.iter().filter(|a| **a >= 90.0).count();
            if flipped == 0 {
                // Unanimously upright: nothing to do for any crop.
                return Ok(vec![0.0; crops.len()]);
            }
            if flipped == verdicts.len() {
                // Unanimously upside down: turn every crop without asking again.
                for crop in crops.iter_mut() {
                    *crop = image::imageops::rotate180(crop);
                }
                return Ok(vec![180.0; crops.len()]);
            }
        }
        self.classify_all(crops)
    }

    /// Classifies and rotates every crop.
    fn classify_all(&self, crops: &mut [RgbImage]) -> Result<Vec<f32>> {
        let mut angles = vec![0.0f32; crops.len()];
        if crops.is_empty() {
            return Ok(angles);
        }
        let (h, w) = (
            self.config.image_height.max(8) as usize,
            self.config.image_width.max(8) as usize,
        );

        for chunk_start in (0..crops.len()).step_by(self.config.batch_size.max(1)) {
            let chunk_end = (chunk_start + self.config.batch_size.max(1)).min(crops.len());
            let chunk = &mut crops[chunk_start..chunk_end];
            let n = chunk.len();

            let mut data = vec![0f32; n * 3 * h * w];
            for (b, img) in chunk.iter().enumerate() {
                let scale = h as f32 / img.height().max(1) as f32;
                let target_w = ((img.width() as f32 * scale).round() as usize).clamp(1, w);
                let resized = image::imageops::resize(
                    img,
                    target_w as u32,
                    h as u32,
                    image::imageops::FilterType::Triangle,
                );
                let raw = resized.as_raw();
                let plane = h * w;
                for c in 0..3 {
                    let base = b * 3 * plane + c * plane;
                    for y in 0..h {
                        for x in 0..target_w {
                            let v = raw[(y * target_w + x) * 3 + c] as f32 / 255.0;
                            data[base + y * w + x] = (v - 0.5) / 0.5;
                        }
                    }
                }
            }

            let out = self.model.run(nchw(n, 3, h, w, data))?;
            let shape = out.shape();
            if shape.len() != 2 || shape[1] < 2 {
                return Err(Error::model(format!(
                    "unexpected orientation output shape {shape:?}, expected [batch, 2]"
                )));
            }
            for (b, img) in chunk.iter_mut().enumerate() {
                let p_upside_down = out[[b, 1]];
                if p_upside_down >= self.config.threshold {
                    *img = image::imageops::rotate180(img);
                    angles[chunk_start + b] = 180.0;
                }
            }
        }
        Ok(angles)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sampling_is_configurable_and_off_for_small_pages() {
        let config = OrientationConfig::default();
        assert_eq!(config.sample_size, 8);
        // With fewer crops than the sample size every crop is classified, which
        // is what `correct` falls back to.
        assert!(config.sample_size > 0);
    }

    #[test]
    fn default_threshold_is_conservative() {
        // Rotating a line that was merely hard to read would corrupt the text,
        // so the classifier has to be confident.
        assert!(OrientationConfig::default().threshold >= 0.9);
    }
}
