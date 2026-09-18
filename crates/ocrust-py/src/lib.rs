//! Python bindings for `ocrust`.
//!
//! The extension stays deliberately thin: it owns the engine, releases the GIL
//! while scanning, and hands results back as JSON that the Python layer turns
//! into dataclasses. Everything user-facing (defaults, dataclasses, the CLI)
//! lives in the `ocrust` Python package.

use std::path::PathBuf;

use ocrust_core::export::pdf::{build_with_images, PdfOptions};
use ocrust_core::{Device, Document, EngineConfig, Format, Source};
use pyo3::exceptions::{PyIOError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};

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
        fix_orientation = true,
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
        fix_orientation: bool,
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

        let inner = ocrust_core::Engine::new(config).map_err(to_py_err)?;
        Ok(Self { inner })
    }

    /// The model files in use, as `(detection, recognition, orientation, dictionary)`.
    fn models(&self) -> (String, String, Option<String>, Option<String>) {
        let set = self.inner.model_set();
        (
            set.detection.display().to_string(),
            set.recognition.display().to_string(),
            set.orientation.as_ref().map(|p| p.display().to_string()),
            set.dictionary.as_ref().map(|p| p.display().to_string()),
        )
    }

    /// Scans a file and returns the result as JSON.
    #[pyo3(signature = (path, pages = None))]
    fn scan_path(
        &self,
        py: Python<'_>,
        path: PathBuf,
        pages: Option<Vec<usize>>,
    ) -> PyResult<String> {
        self.scan_source(py, Source::path(path), pages)
    }

    /// Scans an encoded document or image from memory.
    #[pyo3(signature = (data, name = "<bytes>", pages = None))]
    fn scan_bytes(
        &self,
        py: Python<'_>,
        data: Vec<u8>,
        name: &str,
        pages: Option<Vec<usize>>,
    ) -> PyResult<String> {
        self.scan_source(py, Source::bytes(data, name), pages)
    }

    /// Scans raw RGB pixels (`height * width * 3` bytes).
    #[pyo3(signature = (data, width, height, name = "<array>"))]
    fn scan_rgb(
        &self,
        py: Python<'_>,
        data: Vec<u8>,
        width: u32,
        height: u32,
        name: &str,
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
        self.scan_source(py, source, None)
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
    ) -> PyResult<String> {
        let doc = py.detach(|| match pages {
            // Page selection is per call, so it cannot live in the shared config.
            Some(list) => self.inner.scan_pages(&source, &list),
            None => self.inner.scan(&source),
        });
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
    Ok(())
}
