//! Python bindings for `ocrust`.
//!
//! The extension stays deliberately thin: it owns the engine, releases the GIL
//! while scanning, and hands results back as JSON that the Python layer turns
//! into dataclasses. Everything user-facing (defaults, dataclasses, the CLI)
//! lives in the `ocrust` Python package.

use std::path::PathBuf;
use std::sync::Mutex;

use ocrust_core::export::overlay::{OverlayOptions, OverlayReport};
use ocrust_core::export::pdf::{build_with_images, PdfOptions};
use ocrust_core::export::tiff::TiffColor;
use ocrust_core::{lang, Device, Document, EngineConfig, Format, Source};
use pyo3::exceptions::{PyIOError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};

/// One model bundle as Python sees it: detection, recognition, orientation and
/// dictionary paths.
type ModelPathsTuple = (String, String, Option<String>, Option<String>);

/// One planned overlay page: index, width, height, rotation and whether it
/// needs OCR.
type PagePlanTuple = (usize, f32, f32, i64, bool);

/// A written text layer: the PDF plus the counts from the run.
type TextLayerResult<'py> = (Bound<'py, PyBytes>, usize, usize, usize, usize, usize);

/// Maps engine errors onto the Python exception a caller would expect.
fn to_py_err(e: ocrust_core::Error) -> PyErr {
    use ocrust_core::Error as E;
    match e {
        E::Io { .. } | E::PlainIo(_) => PyIOError::new_err(e.to_string()),
        E::Unsupported(_) | E::Config(_) | E::Dict(_) => PyValueError::new_err(e.to_string()),
        other => PyRuntimeError::new_err(other.to_string()),
    }
}

/// A loaded OCR engine.
#[pyclass(name = "Engine", module = "ocrust._ocrust", frozen)]
struct PyEngine {
    inner: ocrust_core::Engine,
}

#[pymethods]
impl PyEngine {
    /// Builds an engine. Every argument is optional; see the Python wrapper for
    /// the documented defaults.
    #[new]
    #[pyo3(signature = (
        models_dir = None,
        detection_model = None,
        recognition_model = None,
        orientation_model = None,
        dictionary = None,
        device = None,
        threads = None,
        page_workers = None,
        pdf_dpi = None,
        io_retries = None,
        preprocess = true,
        deskew = None,
        word_boxes = true,
        keep_page_images = false,
        drop_score = None,
        det_limit_side = None,
        det_box_threshold = None,
        det_unclip_ratio = None,
        rec_batch_size = None,
        rec_image_height = None,
        rec_space_gap = None,
        fix_orientation = true,
        tables = true,
        languages = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        models_dir: Option<PathBuf>,
        detection_model: Option<PathBuf>,
        recognition_model: Option<PathBuf>,
        orientation_model: Option<PathBuf>,
        dictionary: Option<PathBuf>,
        device: Option<&str>,
        threads: Option<usize>,
        page_workers: Option<usize>,
        pdf_dpi: Option<f32>,
        io_retries: Option<u32>,
        preprocess: bool,
        deskew: Option<bool>,
        word_boxes: bool,
        keep_page_images: bool,
        drop_score: Option<f32>,
        det_limit_side: Option<u32>,
        det_box_threshold: Option<f32>,
        det_unclip_ratio: Option<f32>,
        rec_batch_size: Option<usize>,
        rec_image_height: Option<u32>,
        rec_space_gap: Option<f32>,
        fix_orientation: bool,
        tables: bool,
        languages: Option<Vec<String>>,
    ) -> PyResult<Self> {
        let mut config = EngineConfig::new();
        config.models.directory = models_dir;
        config.models.detection = detection_model;
        config.models.recognition = recognition_model;
        config.models.orientation = orientation_model;
        config.models.dictionary = dictionary;
        config.word_boxes = word_boxes;
        config.keep_page_images = keep_page_images;
        config.fix_orientation = fix_orientation;
        config.layout.detect_tables = tables;

        if let Some(d) = device {
            config.session.device = Device::parse(d).map_err(to_py_err)?;
        }
        if let Some(t) = threads {
            config.session.intra_threads = t;
        }
        if let Some(w) = page_workers {
            config.page_workers = w;
        }
        if let Some(dpi) = pdf_dpi {
            config.ingest.pdf_dpi = dpi;
        }
        if let Some(retries) = io_retries {
            config.ingest.read_retries = retries;
        }
        if !preprocess {
            config.preprocess = ocrust_core::PreprocessConfig::none();
        }
        if let Some(on) = deskew {
            config.preprocess.deskew = on;
        }
        if let Some(s) = drop_score {
            config.recognizer.drop_score = s;
        }
        if let Some(v) = det_limit_side {
            config.detector.limit_side_len = v;
        }
        if let Some(v) = det_box_threshold {
            config.detector.box_thresh = v;
        }
        if let Some(v) = det_unclip_ratio {
            config.detector.unclip_ratio = v;
        }
        if let Some(v) = rec_batch_size {
            config.recognizer.batch_size = v;
        }
        if let Some(v) = rec_image_height {
            config.recognizer.image_height = v;
        }
        if let Some(v) = rec_space_gap {
            config.recognizer.space_gap_factor = v;
        }

        if let Some(codes) = languages {
            config = config.with_languages(codes).map_err(to_py_err)?;
        }

        let inner = ocrust_core::Engine::new(config).map_err(to_py_err)?;
        Ok(Self { inner })
    }

    /// The model files in use, as `(detection, recognition, orientation, dictionary)`.
    fn models(&self) -> ModelPathsTuple {
        let set = self.inner.model_set();
        (
            set.detection.display().to_string(),
            set.recognition.display().to_string(),
            set.orientation.as_ref().map(|p| p.display().to_string()),
            set.dictionary.as_ref().map(|p| p.display().to_string()),
        )
    }

    /// Scans a file and returns the result as JSON.
    #[pyo3(signature = (path, pages = None, progress = None))]
    fn scan_path(
        &self,
        py: Python<'_>,
        path: PathBuf,
        pages: Option<Vec<usize>>,
        progress: Option<Py<PyAny>>,
    ) -> PyResult<String> {
        self.scan_source(py, Source::path(path), pages, progress)
    }

    /// Scans an encoded document or image from memory.
    #[pyo3(signature = (data, name = "<bytes>", pages = None, progress = None))]
    fn scan_bytes(
        &self,
        py: Python<'_>,
        data: Vec<u8>,
        name: &str,
        pages: Option<Vec<usize>>,
        progress: Option<Py<PyAny>>,
    ) -> PyResult<String> {
        self.scan_source(py, Source::bytes(data, name), pages, progress)
    }

    /// Scans raw RGB pixels (`height * width * 3` bytes).
    #[pyo3(signature = (data, width, height, name = "<array>", pages = None, progress = None))]
    #[allow(clippy::too_many_arguments)]
    fn scan_rgb(
        &self,
        py: Python<'_>,
        data: Vec<u8>,
        width: u32,
        height: u32,
        name: &str,
        pages: Option<Vec<usize>>,
        progress: Option<Py<PyAny>>,
    ) -> PyResult<String> {
        let expected = width as usize * height as usize * 3;
        if data.len() != expected {
            return Err(PyValueError::new_err(format!(
                "expected {expected} bytes for {width}x{height} RGB, got {}",
                data.len()
            )));
        }
        let image = image::RgbImage::from_raw(width, height, data)
            .ok_or_else(|| PyValueError::new_err("invalid RGB buffer"))?;
        let source = Source::Image {
            image,
            name: name.to_string(),
        };
        // An array is one page, but a caller who asks for page 7 of it deserves
        // to hear so rather than be handed page 1.
        self.scan_source(py, source, pages, progress)
    }

    /// Scans several files, in parallel, and returns one JSON string per input.
    ///
    /// Failures are reported per item as `{"error": "..."}` so one bad file does
    /// not lose the whole batch.
    fn scan_many(&self, py: Python<'_>, paths: Vec<PathBuf>) -> PyResult<Vec<String>> {
        let sources: Vec<Source> = paths.into_iter().map(Source::path).collect();
        let results = py.detach(|| self.inner.scan_many(&sources));
        results
            .into_iter()
            .map(|r| match r {
                Ok(doc) => {
                    serde_json::to_string(&doc).map_err(|e| PyRuntimeError::new_err(e.to_string()))
                }
                Err(e) => Ok(serde_json::json!({ "error": e.to_string() }).to_string()),
            })
            .collect()
    }

    /// Scans `path` and returns a searchable PDF: the page images with an
    /// invisible text layer on top.
    #[pyo3(signature = (path, dpi = None, jpeg_quality = 80))]
    fn searchable_pdf<'py>(
        &self,
        py: Python<'py>,
        path: PathBuf,
        dpi: Option<f32>,
        jpeg_quality: u8,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let dpi = dpi.unwrap_or(self.inner.config().ingest.pdf_dpi);
        let bytes = py.detach(|| -> ocrust_core::Result<Vec<u8>> {
            let doc = self.inner.scan(&Source::path(path))?;
            let images: Vec<image::RgbImage> = doc
                .pages
                .iter()
                .filter_map(|p| p.image.as_ref().map(|i| i.as_ref().clone()))
                .collect();
            build_with_images(
                &doc,
                &images,
                &PdfOptions {
                    dpi,
                    jpeg_quality,
                    text_layer: true,
                },
            )
        });
        Ok(PyBytes::new(py, &bytes.map_err(to_py_err)?))
    }

    /// Languages the loaded recognition model covers completely.
    ///
    /// Each entry is `(code, name, script)`.
    fn languages(&self) -> Vec<(String, String, String)> {
        self.inner
            .supported_languages()
            .into_iter()
            .map(|l| {
                (
                    l.code.to_string(),
                    l.name.to_string(),
                    l.script.name().to_string(),
                )
            })
            .collect()
    }

    /// Languages the model nearly covers: `(code, name, ratio, missing)`.
    #[pyo3(signature = (min_ratio = 0.8))]
    fn partial_languages(&self, min_ratio: f32) -> Vec<(String, String, f32, String)> {
        self.inner
            .partial_languages(min_ratio)
            .into_iter()
            .map(|c| {
                (
                    c.language.code.to_string(),
                    c.language.name.to_string(),
                    c.ratio(),
                    c.missing_display(),
                )
            })
            .collect()
    }

    /// Number of characters the recognition model can emit.
    fn charset_size(&self) -> usize {
        self.inner.charset_size()
    }

    /// Adds an invisible OCR text layer to an existing PDF.
    ///
    /// Returns the new PDF and a report: `(pdf, pages, pages_with_layer,
    /// pages_skipped, lines, unmappable_chars)`.
    #[pyo3(signature = (data, dpi = None, skip_pages_with_text = true, compress = true))]
    fn pdf_text_layer<'py>(
        &self,
        py: Python<'py>,
        data: Vec<u8>,
        dpi: Option<f32>,
        skip_pages_with_text: bool,
        compress: bool,
    ) -> PyResult<TextLayerResult<'py>> {
        let options = OverlayOptions {
            dpi: dpi.unwrap_or(self.inner.config().ingest.pdf_dpi),
            skip_pages_with_text,
            compress,
        };
        let outcome: ocrust_core::Result<(Vec<u8>, OverlayReport)> =
            py.detach(|| self.inner.add_pdf_text_layer(&data, &options));
        let (bytes, report) = outcome.map_err(to_py_err)?;
        Ok((
            PyBytes::new(py, &bytes),
            report.pages,
            report.pages_with_layer,
            report.pages_skipped,
            report.lines,
            report.unmappable_chars,
        ))
    }

    /// Reports what `pdf_text_layer` would do: one
    /// `(index, width, height, rotate, needs_ocr)` per page.
    #[pyo3(signature = (data, skip_pages_with_text = true))]
    fn plan_pdf_text_layer(
        &self,
        data: Vec<u8>,
        skip_pages_with_text: bool,
    ) -> PyResult<Vec<PagePlanTuple>> {
        let options = OverlayOptions {
            skip_pages_with_text,
            ..Default::default()
        };
        let plans = self
            .inner
            .plan_pdf_text_layer(&data, &options)
            .map_err(to_py_err)?;
        Ok(plans
            .into_iter()
            .map(|p| {
                (
                    p.index,
                    p.page_box.width,
                    p.page_box.height,
                    p.page_box.rotate,
                    p.needs_ocr,
                )
            })
            .collect())
    }

    /// Scans `path` and returns `(multipage_tiff, document_json)`.
    #[pyo3(signature = (path, gray = false))]
    fn to_tiff<'py>(
        &self,
        py: Python<'py>,
        path: PathBuf,
        gray: bool,
    ) -> PyResult<(Bound<'py, PyBytes>, String)> {
        let color = if gray {
            TiffColor::Gray
        } else {
            TiffColor::Rgb
        };
        let outcome = py.detach(|| self.inner.to_tiff(&Source::path(path), color));
        let (bytes, doc) = outcome.map_err(to_py_err)?;
        let json =
            serde_json::to_string(&doc).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        Ok((PyBytes::new(py, &bytes), json))
    }

    fn __repr__(&self) -> String {
        let set = self.inner.model_set();
        format!(
            "<ocrust.Engine detection={:?} recognition={:?}>",
            set.detection.file_name().unwrap_or_default(),
            set.recognition.file_name().unwrap_or_default()
        )
    }
}

impl PyEngine {
    /// Runs the scan with the GIL released.
    fn scan_source(
        &self,
        py: Python<'_>,
        source: Source,
        pages: Option<Vec<usize>>,
        progress: Option<Py<PyAny>>,
    ) -> PyResult<String> {
        // Reported errors are kept aside: the callback runs deep inside the
        // engine, which knows nothing about Python, so it is raised afterwards.
        let callback_error: Mutex<Option<PyErr>> = Mutex::new(None);
        let doc = py.detach(|| {
            let mut report = |p: ocrust_core::Progress| {
                let Some(callback) = progress.as_ref() else {
                    return;
                };
                if callback_error.lock().is_ok_and(|e| e.is_some()) {
                    return; // already failed once; do not keep calling it
                }
                Python::attach(|py| {
                    if let Err(e) = callback.call1(py, (p.page, p.total_pages, p.lines)) {
                        if let Ok(mut slot) = callback_error.lock() {
                            slot.get_or_insert(e);
                        }
                    }
                });
            };
            match pages {
                Some(list) => self
                    .inner
                    .scan_pages_with_progress(&source, &list, &mut report),
                None => self.inner.scan_with_progress(&source, &mut report),
            }
        });
        if let Some(e) = callback_error.lock().ok().and_then(|mut slot| slot.take()) {
            return Err(e);
        }
        let doc = doc.map_err(to_py_err)?;
        serde_json::to_string(&doc).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }
}

/// Renders a document (as returned by the scan functions) into `format`.
#[pyfunction]
fn render_document(document_json: &str, format: &str) -> PyResult<String> {
    let fmt = Format::parse(format)
        .ok_or_else(|| PyValueError::new_err(format!("unknown format {format:?}")))?;
    let doc: Document = serde_json::from_str(document_json)
        .map_err(|e| PyValueError::new_err(format!("not an ocrust document: {e}")))?;
    ocrust_core::render(&doc, fmt).map_err(to_py_err)
}

/// Reports the ONNX Runtime the extension can load.
#[pyfunction]
fn runtime_version() -> PyResult<String> {
    ocrust_core::runtime_version().map_err(to_py_err)
}

/// Resolves the model set that would be used for `models_dir`.
#[pyfunction]
#[pyo3(signature = (models_dir = None))]
fn resolve_models(
    models_dir: Option<PathBuf>,
) -> PyResult<(String, String, Option<String>, Option<String>)> {
    let set = ocrust_core::models::resolve(&ocrust_core::ModelPaths {
        directory: models_dir,
        ..Default::default()
    })
    .map_err(to_py_err)?;
    Ok((
        set.detection.display().to_string(),
        set.recognition.display().to_string(),
        set.orientation.as_ref().map(|p| p.display().to_string()),
        set.dictionary.as_ref().map(|p| p.display().to_string()),
    ))
}

/// Every language `ocrust` can validate: `(code, name, script)`.
#[pyfunction]
fn known_languages() -> Vec<(String, String, String)> {
    lang::all()
        .iter()
        .map(|l| {
            (
                l.code.to_string(),
                l.name.to_string(),
                l.script.name().to_string(),
            )
        })
        .collect()
}

/// Downloads the bundled model set from GitHub into the model cache.
///
/// Returns the resolved `(detection, recognition, orientation, dictionary)`
/// paths. Files already present with the right checksum are kept.
#[pyfunction]
fn install_models(py: Python<'_>) -> PyResult<ModelPathsTuple> {
    let set = py.detach(|| -> ocrust_core::Result<ocrust_core::ModelSet> {
        let manifest = ocrust_core::models::builtin_manifest()?;
        ocrust_core::models::install(&manifest, &mut |name, done, total| {
            log::info!("ocrust: fetched {name} ({done}/{total})");
        })
    });
    let set = set.map_err(to_py_err)?;
    Ok((
        set.detection.display().to_string(),
        set.recognition.display().to_string(),
        set.orientation.as_ref().map(|p| p.display().to_string()),
        set.dictionary.as_ref().map(|p| p.display().to_string()),
    ))
}

/// The default model cache directory.
#[pyfunction]
fn models_cache_dir() -> String {
    ocrust_core::models::models_dir().display().to_string()
}

#[pymodule]
fn _ocrust(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", ocrust_core::VERSION)?;
    m.add_class::<PyEngine>()?;
    m.add_function(wrap_pyfunction!(render_document, m)?)?;
    m.add_function(wrap_pyfunction!(runtime_version, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_models, m)?)?;
    m.add_function(wrap_pyfunction!(models_cache_dir, m)?)?;
    m.add_function(wrap_pyfunction!(known_languages, m)?)?;
    m.add_function(wrap_pyfunction!(install_models, m)?)?;
    Ok(())
}
