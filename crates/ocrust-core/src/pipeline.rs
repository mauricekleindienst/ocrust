//! The scanning engine: models in, documents out.

use std::time::Instant;

use image::RgbImage;
use rayon::prelude::*;

use crate::classify::{OrientationClassifier, OrientationConfig};
use crate::detect::{DetectorConfig, TextDetector};
use crate::doc::{Block, BlockKind, Document, Line, Page};
use crate::error::{Error, Result};
use crate::geom::{crop_quad, Rect};
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
    /// Pages scanned in parallel. `0` picks the default, which is 1.
    ///
    /// ONNX Runtime already spreads one inference across every core, so workers
    /// share the cores rather than adding any. Measured on four cores with a
    /// 12-page scan: one worker took 7.8 s, four workers 5.8 s — but a
    /// single-page document is about twice as slow with four workers, because
    /// each one only gets a quarter of the cores. So: leave it at 1 for
    /// interactive, page-at-a-time work and raise it for batches and long PDFs.
    pub page_workers: usize,
    /// Keep the preprocessed page image on each [`Page`], which
    /// [`crate::export::pdf`] needs to write a searchable PDF.
    pub keep_page_images: bool,
    /// Read what is printed in coloured ink a second time, on its own.
    ///
    /// A red or blue stamp across the body text, or over a bold letterhead, is
    /// lost among the black lines it crosses: the detector sees one tangle of
    /// strokes. Separated by colour it reads cleanly — which is how a person
    /// reads it too. Lines found only this way arrive as blocks of kind
    /// [`BlockKind::Stamp`](crate::doc::BlockKind::Stamp). A page without
    /// coloured ink costs one look at its pixels (12 ms on an A4 page at
    /// 200 dpi, about 1 %), a page with a stamp a second, smaller reading
    /// (about 13 %). Off by default, because it is for finding stamps rather
    /// than for reading text.
    pub read_stamps: bool,
    /// Find the tick boxes on a page and whether each is ticked.
    ///
    /// The recognizer reads the words beside a tick box and hardly ever the
    /// box, so "☐ offen ☒ VS-NfD" arrives as "offen VS-NfD" and the choice is
    /// lost. Each box found in the pixels arrives as a block of kind
    /// [`BlockKind::TickBox`](crate::doc::BlockKind::TickBox) reading `☒` or
    /// `☐`, placed where it is printed. Off by default.
    pub tick_boxes: bool,
    /// Detect pages that are rotated by a quarter turn and straighten them.
    ///
    /// Sideways scans are common — a viewer applies `/Rotate`, a feeder pulled
    /// the sheet in landscape — and the recognizer reads such lines but the page
    /// comes out in column order. Box geometry decides the quarter turn; the
    /// 180-degree line classifier then sorts out upside-down text.
    pub auto_page_orientation: bool,
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
            auto_page_orientation: true,
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
        let cores = std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(4);
        if config.page_workers == 0 {
            // One worker with all cores is the fastest setting for a single page,
            // which is the common case. Batches and long PDFs benefit from more,
            // so this is a knob rather than a guess.
            config.page_workers = 1;
        }
        let mut session = config.session.clone();
        let plan = plan_parallelism(
            cores,
            config.page_workers,
            session.intra_threads,
            session.replicas,
        );
        config.page_workers = plan.workers;
        session.intra_threads = plan.intra_threads;
        session.replicas = plan.replicas;

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
    /// Covered languages the model is short of an optional character for, such
    /// as German without `ẞ`. A note for the reader, never a refusal.
    pub fn optional_language_gaps(&self) -> Vec<Coverage> {
        lang::optional_gaps(self.recognizer.dict())
    }

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
        self.run(source, None, &mut |_| {})
    }

    /// Scans `source`, reporting progress after every page.
    pub fn scan_with_progress(
        &self,
        source: &Source,
        progress: &mut (dyn FnMut(Progress) + Send),
    ) -> Result<Document> {
        self.run(source, None, progress)
    }

    /// Scans selected pages of a multi-page source, reporting progress.
    pub fn scan_pages_with_progress(
        &self,
        source: &Source,
        pages: &[usize],
        progress: &mut (dyn FnMut(Progress) + Send),
    ) -> Result<Document> {
        self.run(source, Some(pages), progress)
    }

    /// The one scan implementation: every other entry point routes through it, so
    /// page selection and progress reporting cannot drift apart.
    /// [`Self::run_unguarded`], with a panic turned into this document's error.
    ///
    /// A bug met on one page of one document — an index out of range in the
    /// layout, say — used to unwind through the batch and end it, taking every
    /// other document with it. It is still a bug, and says so; but it is this
    /// document's failure, reported like any other.
    fn run(
        &self,
        source: &Source,
        pages: Option<&[usize]>,
        progress: &mut (dyn FnMut(Progress) + Send),
    ) -> Result<Document> {
        let guarded = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.run_unguarded(source, pages, progress)
        }));
        guarded.unwrap_or_else(|payload| {
            let message = payload
                .downcast_ref::<&str>()
                .map(|s| s.to_string())
                .or_else(|| payload.downcast_ref::<String>().cloned())
                .unwrap_or_else(|| "a panic without a message".to_string());
            Err(Error::Internal(message))
        })
    }

    fn run_unguarded(
        &self,
        source: &Source,
        pages: Option<&[usize]>,
        progress: &mut (dyn FnMut(Progress) + Send),
    ) -> Result<Document> {
        let started = Instant::now();
        let ingest_config = match pages {
            // Page selection is per call, so it cannot live in the shared config.
            Some(list) => IngestConfig {
                pages: Some(list.to_vec()),
                ..self.config.ingest.clone()
            },
            None => self.config.ingest.clone(),
        };
        let mut reader = ingest::open(source, &ingest_config)?;
        let total = reader.len();

        // Pages are decoded in batches of `page_workers` and dropped as soon as
        // they are read, so peak memory follows the number of workers rather
        // than the length of the document: a 120-page PDF used to hold 1.8 GB of
        // pixels before the first line was recognized.
        let batch_size = self.config.page_workers.max(1);
        let mut doc = Document::new(source.name());
        let mut batch: Vec<RawPage> = Vec::with_capacity(batch_size);
        let mut scan_batch = |batch: &mut Vec<RawPage>, doc: &mut Document| -> Result<()> {
            let taken = std::mem::take(batch);
            let scanned: Vec<Result<Page>> = if taken.len() > 1 {
                // Pages are independent; run them across the rayon pool.
                taken
                    .into_par_iter()
                    .map(|raw| self.scan_page(raw))
                    .collect()
            } else {
                taken.into_iter().map(|raw| self.scan_page(raw)).collect()
            };
            for page in scanned {
                let page = page?;
                progress(Progress {
                    page: page.index,
                    total_pages: total,
                    lines: page.lines().count(),
                });
                doc.pages.push(page);
            }
            Ok(())
        };

        reader.for_each_page(&mut |raw| {
            batch.push(raw);
            if batch.len() >= batch_size {
                scan_batch(&mut batch, &mut doc)?;
            }
            Ok(())
        })?;
        scan_batch(&mut batch, &mut doc)?;
        doc.pages.sort_by_key(|p| p.index);
        doc.elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        Ok(doc)
    }

    /// Scans only the given zero-based page indices of a multi-page source.
    pub fn scan_pages(&self, source: &Source, pages: &[usize]) -> Result<Document> {
        self.run(source, Some(pages), &mut |_| {})
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
        // The text layer is placed in the rendered page's coordinate system, so
        // this scan must not move the pixels: no deskew, no rescaling and no
        // quarter turns. Pixel-only clean-up (inversion, contrast) is kept.
        // Measurements on the test corpus show deskewing does not improve
        // recognition anyway, so nothing is given up here.
        let page_options = PageOptions {
            keep_image: false,
            auto_page_orientation: false,
            preprocess: PreprocessConfig {
                deskew: false,
                upscale_below: 0,
                max_pixels: 0,
                ..self.config.preprocess.clone()
            },
            read_stamps: self.config.read_stamps,
            tick_boxes: self.config.tick_boxes,
        };
        crate::export::overlay::add_text_layer(pdf, options, |index, image| {
            let raw = RawPage {
                index,
                image,
                origin: crate::doc::PageOrigin::PdfPage,
                text: None,
            };
            self.scan_page_inner(raw, page_options.clone())
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

    /// Scans `source` and returns a searchable PDF: the page pictures with an
    /// invisible text layer over them.
    ///
    /// Each page is compressed as soon as it is read and the pixels are let go,
    /// so the job holds one page of pixels and the finished JPEGs rather than
    /// every page at full size — on a 40-page scan that is tens of megabytes
    /// instead of half a gigabyte.
    #[cfg(feature = "pdf")]
    pub fn to_searchable_pdf(
        &self,
        source: &Source,
        opts: &crate::export::pdf::PdfOptions,
    ) -> Result<(Vec<u8>, Document)> {
        self.to_searchable_pdf_many(std::slice::from_ref(source), opts)
    }

    /// Scans every source in order and returns them as one searchable PDF.
    ///
    /// This is the converter: anything [`ingest`] can read — an image in any of
    /// the formats it offers, a multi-page TIFF, a PDF — becomes pages of one
    /// document, in the order given, each page sized from its own pixels rather
    /// than forced onto a common sheet.
    ///
    /// Page pictures are compressed as they are scanned, exactly as for a single
    /// source, so a hundred files cost what one does: nothing holds a raw page
    /// beyond the moment it is encoded.
    pub fn to_searchable_pdf_many(
        &self,
        sources: &[Source],
        opts: &crate::export::pdf::PdfOptions,
    ) -> Result<(Vec<u8>, Document)> {
        if sources.is_empty() {
            return Err(Error::config("a PDF needs at least one input"));
        }
        let started = Instant::now();
        let label = match sources {
            [only] => only.name(),
            many => format!("{} inputs", many.len()),
        };
        let mut doc = Document::new(label);
        let mut encoded = Vec::new();
        for source in sources {
            ingest::open(source, &self.pixel_ingest())?.for_each_page(&mut |raw| {
                let options = PageOptions {
                    // Needed here regardless of how the engine is configured.
                    keep_image: true,
                    ..PageOptions::from_config(&self.config)
                };
                let mut page = self.scan_page_inner(raw, options)?;
                let image = page.image.take().ok_or_else(|| {
                    Error::config("the page was scanned without keeping its picture")
                })?;
                encoded.push(crate::export::pdf::encode_page(&image, opts.jpeg_quality)?);
                // One running sequence: a merged document's page numbers are its
                // own, not the ones each file used.
                page.index = doc.pages.len();
                doc.pages.push(page);
                Ok(())
            })?;
        }
        if doc.pages.is_empty() {
            return Err(Error::config("the inputs held no pages"));
        }
        doc.elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        let bytes = crate::export::pdf::build_with_encoded(&doc, &encoded, opts)?;
        Ok((bytes, doc))
    }

    /// Scans `source` and returns its pages as one multi-page TIFF, together
    /// with the OCR result.
    ///
    /// The archived images are the preprocessed ones, so the file is already
    /// deskewed and upright.
    pub fn to_tiff(
        &self,
        source: &Source,
        opts: crate::export::tiff::TiffOptions,
    ) -> Result<(Vec<u8>, Document)> {
        let started = Instant::now();
        let mut doc = Document::new(source.name());
        let mut writer = crate::export::tiff::Writer::new(opts)?;
        // Each page is encoded and let go before the next is read, so a 100-page
        // conversion costs one page of pixels rather than a hundred.
        ingest::open(source, &self.pixel_ingest())?.for_each_page(&mut |raw| {
            let options = PageOptions {
                // Needed here regardless of how the engine is configured.
                keep_image: true,
                ..PageOptions::from_config(&self.config)
            };
            let mut page = self.scan_page_inner(raw, options)?;
            if let Some(image) = page.image.take() {
                writer.add(&image)?;
            }
            doc.pages.push(page);
            Ok(())
        })?;
        doc.elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        Ok((writer.finish()?, doc))
    }

    /// The ingest settings for a conversion that needs every page's pixels —
    /// a searchable PDF, a TIFF — whatever the engine says about text layers.
    fn pixel_ingest(&self) -> IngestConfig {
        IngestConfig {
            #[cfg(feature = "pdf")]
            pdf_text: crate::ingest::pdftext::PdfText::Never,
            ..self.config.ingest.clone()
        }
    }

    /// A page read from its PDF text layer: the same layout as a recognized
    /// page, from lines that are exact rather than recognized.
    fn text_page(&self, index: usize, text: crate::ingest::TextPage, started: Instant) -> Page {
        text_page(index, text, &self.config.layout, started)
    }

    /// Rotates a sideways page upright, when the box geometry says so.
    ///
    /// Returns the rotated image and its detections, or `None` when the page was
    /// already upright or rotating made it no better.
    fn straighten(
        &self,
        image: &RgbImage,
        detections: &[crate::detect::DetectedBox],
    ) -> Result<Option<(RgbImage, Vec<crate::detect::DetectedBox>)>> {
        const MIN_BOXES: usize = 4;
        const SIDEWAYS: f32 = 0.6;

        if detections.len() < MIN_BOXES || vertical_share(detections) < SIDEWAYS {
            return Ok(None);
        }
        let rotated = image::imageops::rotate90(image);
        let boxes = self.detector.detect(&rotated)?;
        if boxes.len() < MIN_BOXES || vertical_share(&boxes) >= vertical_share(detections) {
            // Rotating did not help: the page really does have vertical text.
            return Ok(None);
        }
        log::debug!(
            "page rotated 90 degrees: vertical share {:.2} -> {:.2}",
            vertical_share(detections),
            vertical_share(&boxes)
        );
        Ok(Some((rotated, boxes)))
    }

    /// Runs the full per-page pipeline with this engine's configuration.
    fn scan_page(&self, raw: RawPage) -> Result<Page> {
        self.scan_page_inner(raw, PageOptions::from_config(&self.config))
    }

    /// Runs the full per-page pipeline with per-call overrides.
    fn scan_page_inner(&self, raw: RawPage, options: PageOptions) -> Result<Page> {
        let started = Instant::now();
        if let Some(text) = raw.text {
            return Ok(self.text_page(raw.index, text, started));
        }
        let prepared = prepare(raw.image, &options.preprocess);
        let mut image = prepared.image;
        let mut page_rotation = prepared.rotation;

        let mut detections = self.detector.detect(&image)?;
        if options.auto_page_orientation {
            if let Some((upright, boxes)) = self.straighten(&image, &detections)? {
                image = upright;
                detections = boxes;
                page_rotation += 90.0;
            }
        }
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

        // The line classifier just told us something about the whole page: if it
        // had to turn most lines around, the page itself is upside down. The
        // crops are already correct, so only the geometry has to follow — which
        // is what puts the lines back into reading order.
        let upside_down = options.auto_page_orientation && flipped_share(&angles) > 0.6;
        if upside_down {
            let (w, h) = (image.width() as f32, image.height() as f32);
            for detection in detections.iter_mut() {
                detection.quad = rotate180_quad(&detection.quad, w, h);
            }
            image = image::imageops::rotate180(&image);
            page_rotation += 180.0;
            log::debug!("page was upside down: geometry rotated by 180 degrees");
        }

        let recognitions = self.recognizer.recognize(&crops)?;
        let drop_score = self.config.recognizer.drop_score;

        let mut lines: Vec<Line> = Vec::with_capacity(recognitions.len());
        for ((rec, &det_idx), flip) in recognitions.iter().zip(&kept).zip(&angles) {
            let text = rec.text.trim();
            if text.is_empty() || rec.confidence < drop_score {
                continue;
            }
            let det = &detections[det_idx];
            let quad = det.quad.ordered();
            // A crop the orientation classifier turned around was read against
            // the direction its quad describes; when the page geometry was
            // flipped too the two cancel out. Without this the word boxes of an
            // upside-down label land at the other end of the line.
            let read_backwards = (flip.abs() >= 90.0) != upside_down;
            let words = if !self.config.word_boxes {
                Vec::new()
            } else if read_backwards {
                layout::words_from_chars(&quad, &mirrored_chars(&rec.chars))
            } else {
                layout::words_from_chars(&quad, &rec.chars)
            };
            lines.push(Line {
                text: text.to_string(),
                confidence: rec.confidence,
                margin: rec.margin,
                bbox: quad.bounds(),
                angle: quad.angle_deg() + *flip,
                quad,
                det_score: det.score,
                words,
                segments: Vec::new(),
            });
        }

        let ordered = layout::reading_order(lines, &self.config.layout);
        let blocks = layout::group_blocks(ordered, &self.config.layout);

        let (width, height) = image.dimensions();
        let signals: Vec<crate::quality::LineSignals> = blocks
            .iter()
            .flat_map(|block| block.lines.iter())
            .map(|line| crate::quality::LineSignals {
                chars: line.text.chars().count(),
                confidence: line.confidence,
                margin: line.margin,
                height_ratio: line.bbox.height() / height.max(1) as f32,
            })
            .collect();
        let quality = crate::quality::page_quality(&signals);
        let mut blocks = blocks;
        if options.read_stamps {
            // After the quality estimate: it describes the page as read, and
            // a stamp read twice would count twice.
            let stamps = self.stamp_blocks(&image, &blocks)?;
            blocks.extend(stamps);
        }
        if options.tick_boxes {
            blocks.extend(crate::tickbox::find(&image).into_iter().map(|tick| {
                let line = Line {
                    text: if tick.ticked { "☒" } else { "☐" }.to_string(),
                    confidence: 1.0,
                    bbox: tick.bbox,
                    quad: crate::geom::Quad::from_rect(tick.bbox),
                    ..Default::default()
                };
                Block {
                    kind: BlockKind::TickBox,
                    bbox: tick.bbox,
                    lines: vec![line],
                    table: None,
                }
            }));
        }
        Ok(Page {
            quality,
            index: raw.index,
            width,
            height,
            rotation: page_rotation,
            origin: raw.origin,
            blocks,
            elapsed_ms: started.elapsed().as_secs_f64() * 1000.0,
            image: options.keep_image.then(|| std::sync::Arc::new(image)),
        })
    }

    /// Lines in coloured ink that the page's own reading missed.
    fn stamp_blocks(&self, image: &RgbImage, read: &[Block]) -> Result<Vec<Block>> {
        let Some(ink) = coloured_ink(image) else {
            return Ok(Vec::new());
        };
        let detections = self.detector.detect(&ink)?;
        let mut crops: Vec<RgbImage> = Vec::with_capacity(detections.len());
        let mut kept: Vec<usize> = Vec::with_capacity(detections.len());
        for (i, det) in detections.iter().enumerate() {
            if let Some(crop) = crop_quad(&ink, &det.quad) {
                crops.push(crop);
                kept.push(i);
            }
        }
        if crops.is_empty() {
            return Ok(Vec::new());
        }
        let angles = match &self.orientation {
            Some(classifier) => classifier.correct(&mut crops)?,
            None => vec![0.0; crops.len()],
        };
        let recognitions = self.recognizer.recognize(&crops)?;
        let drop_score = self.config.recognizer.drop_score;
        let mut stamps = Vec::new();
        for ((rec, &det_idx), flip) in recognitions.iter().zip(&kept).zip(&angles) {
            let text = rec.text.trim();
            if text.is_empty() || rec.confidence < drop_score {
                continue;
            }
            let det = &detections[det_idx];
            let quad = det.quad.ordered();
            let bbox = quad.bounds();
            if already_read(read, text, &bbox) {
                continue;
            }
            let words = if !self.config.word_boxes {
                Vec::new()
            } else if flip.abs() >= 90.0 {
                layout::words_from_chars(&quad, &mirrored_chars(&rec.chars))
            } else {
                layout::words_from_chars(&quad, &rec.chars)
            };
            let line = Line {
                text: text.to_string(),
                confidence: rec.confidence,
                margin: rec.margin,
                bbox,
                angle: quad.angle_deg() + *flip,
                quad,
                det_score: det.score,
                words,
                segments: Vec::new(),
            };
            stamps.push(Block {
                kind: BlockKind::Stamp,
                bbox,
                lines: vec![line],
                table: None,
            });
        }
        Ok(stamps)
    }
}

/// How far a pixel's channels must spread to count as coloured ink: red
/// stamp ink spreads 150 and more, faded red about 80, grey and black toner
/// under 20.
const INK_SPREAD: u8 = 60;
/// Coloured ink needed before a page is read a second time: a small stamp
/// covers some 0.2 % of an A4 page, a coloured logo or a highlighted word
/// less than this.
const INK_SHARE: f64 = 0.0002;

/// The page's coloured ink alone, dark on white, or `None` when there is too
/// little of it to hold a line of text.
///
/// Black text crossing a stamp drops out, leaving small gaps in the stamp's
/// strokes, which the recognizer reads through far better than it reads
/// through the black text itself.
fn coloured_ink(image: &RgbImage) -> Option<RgbImage> {
    let spread = |p: &image::Rgb<u8>| {
        let [r, g, b] = p.0;
        r.max(g).max(b) - r.min(g).min(b)
    };
    let coloured = image.pixels().filter(|p| spread(p) >= INK_SPREAD).count();
    let total = image.width() as usize * image.height() as usize;
    if coloured < 400 || (coloured as f64) < total as f64 * INK_SHARE {
        return None;
    }
    let mut ink = RgbImage::from_pixel(image.width(), image.height(), image::Rgb([255; 3]));
    for (out, p) in ink.pixels_mut().zip(image.pixels()) {
        let s = spread(p);
        if s >= INK_SPREAD {
            let v = 255u8.saturating_sub(s.saturating_mul(2));
            *out = image::Rgb([v, v, v]);
        }
    }
    Some(ink)
}

/// Whether the page's own reading already has this line: the same text in
/// much the same place. A red heading is read by both passes.
fn already_read(blocks: &[Block], text: &str, bbox: &Rect) -> bool {
    let key = |t: &str| -> String {
        t.chars()
            .filter(|c| !c.is_whitespace())
            .flat_map(char::to_lowercase)
            .collect()
    };
    let wanted = key(text);
    blocks.iter().flat_map(|b| &b.lines).any(|line| {
        let overlap = bbox.horizontal_overlap(&line.bbox) * bbox.vertical_overlap(&line.bbox);
        let union = bbox.area() + line.bbox.area() - overlap;
        union > 0.0 && overlap / union > 0.5 && key(&line.text) == wanted
    })
}

/// How a machine's cores are shared out between page workers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Parallelism {
    /// Pages in flight at once.
    pub workers: usize,
    /// Threads each inference may use; 0 leaves it to ONNX Runtime.
    pub intra_threads: usize,
    /// Sessions per model; a worker waits for one to be free.
    pub replicas: usize,
}

/// A page read from its PDF text layer: the same layout as a recognized
/// page, from lines that are exact rather than recognized.
fn text_page(
    index: usize,
    text: crate::ingest::TextPage,
    layout_config: &layout::LayoutConfig,
    started: Instant,
) -> Page {
    let ordered = layout::reading_order_exact(text.lines, layout_config);
    let blocks = layout::group_exact_blocks(ordered, layout_config);
    let signals: Vec<crate::quality::LineSignals> = blocks
        .iter()
        .flat_map(|block| block.lines.iter())
        .map(|line| crate::quality::LineSignals {
            chars: line.text.chars().count(),
            confidence: line.confidence,
            margin: line.margin,
            height_ratio: line.bbox.height() / text.height.max(1) as f32,
        })
        .collect();
    Page {
        quality: crate::quality::page_quality(&signals),
        index,
        width: text.width,
        height: text.height,
        rotation: 0.0,
        origin: crate::doc::PageOrigin::PdfText,
        blocks,
        elapsed_ms: started.elapsed().as_secs_f64() * 1000.0,
        image: None,
    }
}

/// A PDF's own text, laid out, without any recognition: a page without a
/// text layer comes back empty. Needs no models — what a conversion that must
/// not recognize anything reads a PDF with.
pub fn read_pdf_text(source: &Source, config: &EngineConfig) -> Result<Document> {
    let started = Instant::now();
    let ingest_config = IngestConfig {
        pdf_text: crate::ingest::pdftext::PdfText::Only,
        ..config.ingest.clone()
    };
    let mut reader = ingest::open(source, &ingest_config)?;
    let mut doc = Document::new(source.name());
    reader.for_each_page(&mut |raw| {
        let page_started = Instant::now();
        let text = raw.text.ok_or_else(|| {
            Error::Unsupported("only a PDF's own text can be read without recognizing it".into())
        })?;
        doc.pages
            .push(text_page(raw.index, text, &config.layout, page_started));
        Ok(())
    })?;
    doc.elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
    Ok(doc)
}

/// Shares `cores` out between `workers`, unless threads or replicas were set.
///
/// Every worker gets a session of its own and the cores are divided between
/// them, so that `workers × threads ≈ cores`: letting each worker ask for
/// every core is the oversubscription that made naive page parallelism slower
/// than a single worker. Sessions used to stop at four, which on a larger
/// machine left workers queueing for a session while their share of the cores
/// sat idle — sixteen workers on sixteen cores ran four inferences of one
/// thread each.
pub fn plan_parallelism(
    cores: usize,
    workers: usize,
    intra_threads: usize,
    replicas: usize,
) -> Parallelism {
    let cores = cores.max(1);
    let workers = workers.clamp(1, cores * 2);
    let replicas = if replicas <= 1 {
        workers.min(cores)
    } else {
        replicas
    };
    // The inferences that can run at once are bounded by the sessions.
    let running = workers.min(replicas).max(1);
    let intra_threads = if intra_threads == 0 && workers > 1 {
        (cores / running).max(1)
    } else {
        intra_threads
    };
    Parallelism {
        workers,
        intra_threads,
        replicas,
    }
}

/// Per-call overrides for one page.
///
/// Reading a document and writing a text layer over it want different things:
/// reading wants every clean-up available, while a text layer must land on the
/// pixels exactly as they were rendered.
#[derive(Debug, Clone)]
struct PageOptions {
    keep_image: bool,
    auto_page_orientation: bool,
    preprocess: PreprocessConfig,
    read_stamps: bool,
    tick_boxes: bool,
}

impl PageOptions {
    fn from_config(config: &EngineConfig) -> Self {
        Self {
            keep_image: config.keep_page_images,
            auto_page_orientation: config.auto_page_orientation,
            preprocess: config.preprocess.clone(),
            read_stamps: config.read_stamps,
            tick_boxes: config.tick_boxes,
        }
    }
}

/// Share of line crops the classifier had to turn around.
fn flipped_share(angles: &[f32]) -> f32 {
    if angles.is_empty() {
        return 0.0;
    }
    let flipped = angles.iter().filter(|a| a.abs() >= 90.0).count();
    flipped as f32 / angles.len() as f32
}

/// Mirrors character positions of a crop that was read back to front.
fn mirrored_chars(chars: &[crate::recognize::CharSpan]) -> Vec<crate::recognize::CharSpan> {
    chars
        .iter()
        .map(|span| crate::recognize::CharSpan {
            x_center: 1.0 - span.x_center,
            ..span.clone()
        })
        .collect()
}

/// Maps a quad through a 180-degree page rotation.
fn rotate180_quad(quad: &crate::geom::Quad, width: f32, height: f32) -> crate::geom::Quad {
    let mut points = quad.points;
    for p in points.iter_mut() {
        p.x = width - p.x;
        p.y = height - p.y;
    }
    crate::geom::Quad::new(points).ordered()
}

/// Share of detected boxes that are taller than they are wide.
///
/// Text lines are wide; a page full of tall boxes is a page lying on its side.
fn vertical_share(detections: &[crate::detect::DetectedBox]) -> f32 {
    if detections.is_empty() {
        return 0.0;
    }
    let vertical = detections
        .iter()
        .filter(|d| {
            let b = d.quad.bounds();
            b.height() > b.width() * 1.2
        })
        .count();
    vertical as f32 / detections.len() as f32
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::detect::DetectedBox;
    use crate::geom::{Quad, Rect};

    fn boxes(shapes: &[(f32, f32)]) -> Vec<DetectedBox> {
        shapes
            .iter()
            .map(|&(w, h)| DetectedBox {
                quad: Quad::from_rect(Rect::new(0.0, 0.0, w, h)),
                score: 0.9,
            })
            .collect()
    }

    #[test]
    fn a_crop_read_back_to_front_has_its_characters_mirrored() {
        use crate::recognize::CharSpan;
        let chars = vec![
            CharSpan {
                text: "A".into(),
                x_center: 0.1,
                x_width: 0.2,
                confidence: 0.9,
            },
            CharSpan {
                text: "B".into(),
                x_center: 0.9,
                x_width: 0.2,
                confidence: 0.8,
            },
        ];
        let mirrored = mirrored_chars(&chars);
        // Same characters in the same order, at the other end of the line.
        assert_eq!(mirrored[0].text, "A");
        assert!((mirrored[0].x_center - 0.9).abs() < 1e-6);
        assert!((mirrored[1].x_center - 0.1).abs() < 1e-6);
        assert!((mirrored[0].x_width - 0.2).abs() < 1e-6);
        assert_eq!(mirrored[1].confidence, 0.8);
    }

    #[test]
    fn upright_pages_have_a_low_vertical_share() {
        // Ordinary text lines: wide and short.
        let share = vertical_share(&boxes(&[(300.0, 20.0), (280.0, 22.0), (310.0, 20.0)]));
        assert_eq!(share, 0.0);
    }

    #[test]
    fn sideways_pages_have_a_high_vertical_share() {
        let share = vertical_share(&boxes(&[(20.0, 300.0), (22.0, 280.0), (300.0, 20.0)]));
        assert!((share - 2.0 / 3.0).abs() < 1e-6, "{share}");
    }

    #[test]
    fn nearly_square_boxes_do_not_count_as_vertical() {
        // A 1.2x margin keeps single characters and stamps from tipping the vote.
        assert_eq!(vertical_share(&boxes(&[(100.0, 110.0)])), 0.0);
        assert_eq!(vertical_share(&boxes(&[(100.0, 130.0)])), 1.0);
    }

    #[test]
    fn empty_detections_are_not_sideways() {
        assert_eq!(vertical_share(&[]), 0.0);
    }

    #[test]
    fn flipped_share_counts_turned_lines() {
        assert_eq!(flipped_share(&[]), 0.0);
        assert_eq!(flipped_share(&[0.0, 0.0, 0.0]), 0.0);
        assert_eq!(flipped_share(&[180.0, 180.0, 0.0, 0.0]), 0.5);
        assert_eq!(flipped_share(&[180.0, 180.0]), 1.0);
    }

    #[test]
    fn rotating_a_quad_by_180_reverses_the_page() {
        // A line near the top of the page ends up near the bottom, and the
        // corners come back in reading order.
        let quad = Quad::from_rect(Rect::new(100.0, 50.0, 400.0, 80.0));
        let flipped = rotate180_quad(&quad, 1000.0, 800.0);
        let b = flipped.bounds();
        assert_eq!((b.x0, b.y0, b.x1, b.y1), (600.0, 720.0, 900.0, 750.0));
        // Flipping twice is the identity.
        let back = rotate180_quad(&flipped, 1000.0, 800.0).bounds();
        assert_eq!(
            (back.x0, back.y0, back.x1, back.y1),
            (100.0, 50.0, 400.0, 80.0)
        );
    }

    #[test]
    fn every_worker_gets_a_session_and_a_share_of_the_cores() {
        // Sixteen workers on sixteen cores: sixteen sessions of one thread,
        // not four sessions that the other twelve workers queue for.
        let plan = plan_parallelism(16, 16, 0, 0);
        assert_eq!(
            (plan.workers, plan.replicas, plan.intra_threads),
            (16, 16, 1)
        );
        // Four workers on sixteen cores: four threads each.
        let plan = plan_parallelism(16, 4, 0, 0);
        assert_eq!((plan.workers, plan.replicas, plan.intra_threads), (4, 4, 4));
        // One worker leaves the threads to ONNX Runtime.
        let plan = plan_parallelism(8, 1, 0, 0);
        assert_eq!((plan.workers, plan.replicas, plan.intra_threads), (1, 1, 0));
    }

    #[test]
    fn more_workers_than_cores_share_the_sessions() {
        // Eight workers on four cores keep a page decoding while another is
        // inferred, but there is no point in more sessions than cores.
        let plan = plan_parallelism(4, 8, 0, 0);
        assert_eq!((plan.workers, plan.replicas, plan.intra_threads), (8, 4, 1));
        assert_eq!(plan_parallelism(4, 100, 0, 0).workers, 8);
    }

    #[test]
    fn explicit_threads_and_replicas_are_kept() {
        let plan = plan_parallelism(16, 8, 3, 2);
        assert_eq!((plan.workers, plan.replicas, plan.intra_threads), (8, 2, 3));
        // Two sessions for eight workers: the threads follow the sessions.
        let plan = plan_parallelism(16, 8, 0, 2);
        assert_eq!(plan.intra_threads, 8);
    }

    /// A white page with black "text" bars and, optionally, a red block.
    fn inked_page(red: bool) -> RgbImage {
        let mut img = RgbImage::from_pixel(400, 300, image::Rgb([255, 255, 255]));
        for y in (20..280).step_by(20) {
            for x in 20..380 {
                for dy in 0..6 {
                    img.put_pixel(x, y + dy, image::Rgb([15, 15, 15]));
                }
            }
        }
        if red {
            for y in 100..160 {
                for x in 100..300 {
                    if img.get_pixel(x, y).0 == [255, 255, 255] {
                        img.put_pixel(x, y, image::Rgb([200, 20, 20]));
                    }
                }
            }
        }
        img
    }

    #[test]
    fn a_black_and_white_page_has_no_coloured_ink() {
        assert!(coloured_ink(&inked_page(false)).is_none());
    }

    #[test]
    fn coloured_ink_is_kept_and_black_text_dropped() {
        let ink = coloured_ink(&inked_page(true)).expect("a red block is ink");
        // The red stays, dark; the black bars crossing it and the paper go white.
        assert!(ink.get_pixel(150, 110).0[0] < 60, "red ink kept dark");
        assert_eq!(
            ink.get_pixel(150, 120).0,
            [255, 255, 255],
            "black text dropped"
        );
        assert_eq!(
            ink.get_pixel(10, 10).0,
            [255, 255, 255],
            "paper stays white"
        );
    }

    #[test]
    fn a_line_both_passes_read_is_kept_once() {
        let line = Line {
            text: "VS-NfD".into(),
            bbox: Rect::new(100.0, 100.0, 200.0, 130.0),
            ..Default::default()
        };
        let blocks = vec![Block {
            kind: BlockKind::Paragraph,
            bbox: line.bbox,
            lines: vec![line],
            table: None,
        }];
        let near = Rect::new(102.0, 101.0, 203.0, 131.0);
        assert!(already_read(&blocks, "VS - NFD", &near));
        assert!(!already_read(&blocks, "GEHEIM", &near), "other text is new");
        let far = Rect::new(500.0, 500.0, 600.0, 530.0);
        assert!(
            !already_read(&blocks, "VS-NfD", &far),
            "same text elsewhere is new"
        );
    }
}
