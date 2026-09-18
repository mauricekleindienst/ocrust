//! The scanning engine: models in, documents out.

use std::time::Instant;

use image::RgbImage;
use rayon::prelude::*;

use crate::classify::{OrientationClassifier, OrientationConfig};
use crate::detect::{DetectorConfig, TextDetector};
use crate::doc::{Document, Line, Page};
use crate::error::{Error, Result};
use crate::geom::crop_quad;
use crate::ingest::{self, IngestConfig, RawPage, Source};
use crate::lang::{self, Coverage, Language};
use crate::layout::{self, LayoutConfig};
use crate::models::{self, ModelPaths, ModelSet};
use crate::preprocess::{prepare, PreprocessConfig};
use crate::recognize::{RecognizerConfig, TextRecognizer};
use crate::runtime::{Device, SessionOptions};

/// Everything needed to build an [`Engine`].
#[derive(Debug, Clone, Default)]
pub struct EngineConfig {
    pub models: ModelPaths,
    pub session: SessionOptions,
    pub detector: DetectorConfig,
    pub recognizer: RecognizerConfig,
    pub orientation: OrientationConfig,
    pub preprocess: PreprocessConfig,
    pub layout: LayoutConfig,
    pub ingest: IngestConfig,
    /// Use the 180° text-line classifier when a model is available.
    pub fix_orientation: bool,
    /// Compute per-word boxes from the recognizer's character positions.
    pub word_boxes: bool,
    /// Pages scanned in parallel. `0` means "one per CPU core".
    pub page_workers: usize,
    /// Keep the preprocessed page image on each [`Page`], which
    /// [`crate::export::pdf`] needs to write a searchable PDF.
    pub keep_page_images: bool,
    /// Languages the documents are expected to be in.
    ///
    /// [`Engine::new`] refuses to build when the recognition model cannot spell
    /// one of them — a model that silently drops `ö` and `ß` produces text that
    /// looks fine and is wrong, which is worse than an error.
    pub languages: Vec<&'static Language>,
}

impl EngineConfig {
    /// Config with sensible defaults for document scanning.
    pub fn new() -> Self {
        Self {
            fix_orientation: true,
            word_boxes: true,
            page_workers: 0,
            ..Default::default()
        }
    }

    /// Sets the directory to look for models in.
    pub fn with_models_dir(mut self, dir: impl Into<std::path::PathBuf>) -> Self {
        self.models.directory = Some(dir.into());
        self
    }

    /// Selects the inference device.
    pub fn with_device(mut self, device: Device) -> Self {
        self.session.device = device;
        self
    }

    /// Requires the model to cover these languages, given as ISO codes or
    /// English names (`"de"`, `"deu"`, `"german"`, `"zh-Hant"`).
    pub fn with_languages<I, S>(mut self, languages: I) -> Result<Self>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut parsed = Vec::new();
        for code in languages {
            let code = code.as_ref();
            let language = lang::parse(code).ok_or_else(|| {
                Error::config(format!(
                    "unknown language {code:?}; known codes: {}",
                    lang::all()
                        .iter()
                        .map(|l| l.code)
                        .collect::<Vec<_>>()
                        .join(", ")
                ))
            })?;
            parsed.push(language);
        }
        self.languages = parsed;
        Ok(self)
    }
}

/// Progress reported while scanning.
#[derive(Debug, Clone, Copy)]
pub struct Progress {
    /// Zero-based page index inside the current document.
    pub page: usize,
    /// Total pages in the current document, when known.
    pub total_pages: usize,
    /// Text lines recognized on this page.
    pub lines: usize,
}

/// A loaded OCR engine. Cheap to share across threads; expensive to build, so
/// build it once and reuse it.
#[derive(Debug)]
pub struct Engine {
    detector: TextDetector,
    recognizer: TextRecognizer,
    orientation: Option<OrientationClassifier>,
    config: EngineConfig,
    model_set: ModelSet,
}

impl Engine {
    /// Loads models and prepares inference sessions.
    pub fn new(mut config: EngineConfig) -> Result<Self> {
        let model_set = models::resolve(&config.models)?;
        if config.page_workers == 0 {
            config.page_workers = std::thread::available_parallelism()
                .map(|n| n.get())
                .unwrap_or(4)
                .min(8);
        }
        // Give each worker its own session so page-level parallelism is real.
        let mut session = config.session.clone();
        if session.replicas <= 1 {
            session.replicas = config.page_workers.clamp(1, 4);
        }

        let detector = TextDetector::new(&model_set.detection, config.detector.clone(), &session)?;
        let recognizer = TextRecognizer::new(
            &model_set.recognition,
            model_set.dictionary.as_deref(),
            config.recognizer.clone(),
            &session,
        )?;
        let orientation = match (&model_set.orientation, config.fix_orientation) {
            (Some(path), true) => Some(OrientationClassifier::new(
                path,
                config.orientation.clone(),
                &session,
            )?),
            _ => None,
        };

        let engine = Self {
            detector,
            recognizer,
            orientation,
            config,
            model_set,
        };
        engine.check_languages()?;
        Ok(engine)
    }

    /// Fails when the recognizer cannot spell a requested language.
    fn check_languages(&self) -> Result<()> {
        let dict = self.recognizer.dict();
        let unsupported: Vec<Coverage> = self
            .config
            .languages
            .iter()
            .map(|l| lang::coverage(dict, l))
            .filter(|c| !c.is_complete())
            .collect();
        if unsupported.is_empty() {
            return Ok(());
        }
        let details = unsupported
            .iter()
            .map(|c| {
                format!(
                    "{} ({}): cannot write {}",
                    c.language.name,
                    c.language.code,
                    c.missing_display()
                )
            })
            .collect::<Vec<_>>()
            .join("; ");
        Err(Error::config(format!(
            "recognition model {} does not cover the requested language(s): {details}.\n\
             hint: `ocrust languages` lists what this model covers; install a bundle for the \
             {} script, or drop the language requirement to accept partial results",
            self.model_set
                .recognition
                .file_name()
                .unwrap_or_default()
                .to_string_lossy(),
            unsupported
                .iter()
                .map(|c| c.language.script.name())
                .collect::<std::collections::BTreeSet<_>>()
                .into_iter()
                .collect::<Vec<_>>()
                .join("/")
        )))
    }

    /// Languages the loaded recognition model covers completely.
    pub fn supported_languages(&self) -> Vec<&'static Language> {
        lang::supported(self.recognizer.dict())
    }

    /// Languages the model *almost* covers, best first.
    pub fn partial_languages(&self, min_ratio: f32) -> Vec<Coverage> {
        lang::partial(self.recognizer.dict(), min_ratio)
    }

    /// Number of characters the recognition model can emit.
    pub fn charset_size(&self) -> usize {
        self.recognizer.dict().len().saturating_sub(1)
    }

    /// The model files in use.
    pub fn model_set(&self) -> &ModelSet {
        &self.model_set
    }

    pub fn config(&self) -> &EngineConfig {
        &self.config
    }

    /// Scans a file, byte buffer or in-memory image.
    pub fn scan(&self, source: &Source) -> Result<Document> {
        self.scan_with_progress(source, &mut |_| {})
    }

    /// Scans `source`, reporting progress after every page.
    pub fn scan_with_progress(
        &self,
        source: &Source,
        progress: &mut (dyn FnMut(Progress) + Send),
    ) -> Result<Document> {
        let started = Instant::now();
        let raw_pages = ingest::load(source, &self.config.ingest)?;
        let total = raw_pages.len();

        let mut doc = Document::new(source.name());
        if self.config.page_workers > 1 && total > 1 {
            // Pages are independent; run them across the rayon pool.
            let results: Vec<Result<Page>> = raw_pages
                .into_par_iter()
                .map(|raw| self.scan_page(raw))
                .collect();
            for page in results {
                let page = page?;
                progress(Progress {
                    page: page.index,
                    total_pages: total,
                    lines: page.lines().count(),
                });
                doc.pages.push(page);
            }
            doc.pages.sort_by_key(|p| p.index);
        } else {
            for raw in raw_pages {
                let page = self.scan_page(raw)?;
                progress(Progress {
                    page: page.index,
                    total_pages: total,
                    lines: page.lines().count(),
                });
                doc.pages.push(page);
            }
        }
        doc.elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        Ok(doc)
    }

    /// Scans only the given zero-based page indices of a multi-page source.
    pub fn scan_pages(&self, source: &Source, pages: &[usize]) -> Result<Document> {
        let mut engine_ingest = self.config.ingest.clone();
        engine_ingest.pages = Some(pages.to_vec());
        let started = Instant::now();
        let raw_pages = ingest::load(source, &engine_ingest)?;
        let mut doc = Document::new(source.name());
        for raw in raw_pages {
            doc.pages.push(self.scan_page(raw)?);
        }
        doc.elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        Ok(doc)
    }

    /// Scans an already decoded frame.
    pub fn scan_image(&self, image: RgbImage, label: impl Into<String>) -> Result<Document> {
        self.scan(&Source::Image {
            image,
            name: label.into(),
        })
    }

    /// Scans several sources, in parallel across documents.
    ///
    /// Results keep the input order; a failing source yields its own error
    /// rather than aborting the batch.
    pub fn scan_many(&self, sources: &[Source]) -> Vec<Result<Document>> {
        if self.config.page_workers > 1 && sources.len() > 1 {
            sources.par_iter().map(|s| self.scan(s)).collect()
        } else {
            sources.iter().map(|s| self.scan(s)).collect()
        }
    }

    /// Adds an invisible OCR text layer to an existing PDF.
    ///
    /// The original pages are preserved byte for byte; only a content stream and
    /// a font are added. Pages that already contain text are skipped unless the
    /// options say otherwise.
    #[cfg(feature = "pdf")]
    pub fn add_pdf_text_layer(
        &self,
        pdf: &[u8],
        options: &crate::export::overlay::OverlayOptions,
    ) -> Result<(Vec<u8>, crate::export::overlay::OverlayReport)> {
        crate::export::overlay::add_text_layer(pdf, options, |index, image| {
            let raw = RawPage {
                index,
                image,
                origin: crate::doc::PageOrigin::PdfPage,
            };
            self.scan_page(raw)
        })
    }

    /// Reports what [`Self::add_pdf_text_layer`] would do, without running OCR.
    #[cfg(feature = "pdf")]
    pub fn plan_pdf_text_layer(
        &self,
        pdf: &[u8],
        options: &crate::export::overlay::OverlayOptions,
    ) -> Result<Vec<crate::export::overlay::PagePlan>> {
        crate::export::overlay::plan(pdf, options)
    }

    /// Scans `source` and returns its pages as one multi-page TIFF, together
    /// with the OCR result.
    ///
    /// The archived images are the preprocessed ones, so the file is already
    /// deskewed and upright.
    pub fn to_tiff(
        &self,
        source: &Source,
        color: crate::export::tiff::TiffColor,
    ) -> Result<(Vec<u8>, Document)> {
        let started = Instant::now();
        let mut doc = Document::new(source.name());
        // Page images are needed here regardless of how the engine is configured.
        for raw in ingest::load(source, &self.config.ingest)? {
            doc.pages.push(self.scan_page_inner(raw, true)?);
        }
        doc.elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;

        let pages: Vec<image::RgbImage> = doc
            .pages
            .iter()
            .filter_map(|p| p.image.as_ref().map(|i| i.as_ref().clone()))
            .collect();
        let bytes = crate::export::tiff::write_pages(&pages, color)?;
        Ok((bytes, doc))
    }

    /// Runs the full per-page pipeline with this engine's configuration.
    fn scan_page(&self, raw: RawPage) -> Result<Page> {
        self.scan_page_inner(raw, self.config.keep_page_images)
    }

    /// Runs the full per-page pipeline, optionally retaining the page image.
    fn scan_page_inner(&self, raw: RawPage, keep_image: bool) -> Result<Page> {
        let started = Instant::now();
        let prepared = prepare(raw.image, &self.config.preprocess);
        let image = prepared.image;

        let detections = self.detector.detect(&image)?;
        let mut crops: Vec<RgbImage> = Vec::with_capacity(detections.len());
        let mut kept: Vec<usize> = Vec::with_capacity(detections.len());
        for (i, det) in detections.iter().enumerate() {
            if let Some(crop) = crop_quad(&image, &det.quad) {
                crops.push(crop);
                kept.push(i);
            }
        }

        let angles = match &self.orientation {
            Some(classifier) => classifier.correct(&mut crops)?,
            None => vec![0.0; crops.len()],
        };

        let recognitions = self.recognizer.recognize(&crops)?;
        let drop_score = self.config.recognizer.drop_score;

        let mut lines: Vec<Line> = Vec::with_capacity(recognitions.len());
        for ((rec, &det_idx), flip) in recognitions.iter().zip(&kept).zip(angles) {
            let text = rec.text.trim();
            if text.is_empty() || rec.confidence < drop_score {
                continue;
            }
            let det = &detections[det_idx];
            let quad = det.quad.ordered();
            let words = if self.config.word_boxes {
                layout::words_from_chars(&quad, &rec.chars)
            } else {
                Vec::new()
            };
            lines.push(Line {
                text: text.to_string(),
                confidence: rec.confidence,
                bbox: quad.bounds(),
                angle: quad.angle_deg() + flip,
                quad,
                det_score: det.score,
                words,
            });
        }

        let ordered = layout::reading_order(lines, &self.config.layout);
        let blocks = layout::group_blocks(ordered, &self.config.layout);

        let (width, height) = image.dimensions();
        Ok(Page {
            index: raw.index,
            width,
            height,
            rotation: prepared.rotation,
            origin: raw.origin,
            blocks,
            elapsed_ms: started.elapsed().as_secs_f64() * 1000.0,
            image: keep_image.then(|| std::sync::Arc::new(image)),
        })
    }
}

/// Scans one file with default settings.
///
/// Convenient for one-off use; build an [`Engine`] when scanning more than a
/// couple of documents, because this loads the models every time.
pub fn scan_file(path: impl AsRef<std::path::Path>) -> Result<Document> {
    let path = path.as_ref();
    if !path.exists() {
        return Err(Error::io(
            path,
            std::io::Error::new(std::io::ErrorKind::NotFound, "no such file"),
        ));
    }
    let engine = Engine::new(EngineConfig::new())?;
    engine.scan(&Source::path(path))
}
