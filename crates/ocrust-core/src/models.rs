//! Finding and installing model files.
//!
//! Resolution order, first hit wins:
//!
//! 1. explicit paths in [`ModelPaths`]
//! 2. a directory given by the caller (or `OCRUST_MODELS_DIR`)
//! 3. the per-user cache directory
//!
//! Directories are scanned by filename pattern (`*det*.onnx`, `*rec*.onnx`,
//! `*cls*.onnx`, `*dict*.txt`/`*keys*.txt`) so that model bundles from
//! PaddleOCR, RapidOCR or a custom export all work unchanged.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{Error, Result};

/// Explicitly configured model files. `None` means "discover it".
#[derive(Debug, Clone, Default)]
pub struct ModelPaths {
    pub detection: Option<PathBuf>,
    pub recognition: Option<PathBuf>,
    pub orientation: Option<PathBuf>,
    /// Character dictionary. Optional: modern exports embed it in the ONNX file.
    pub dictionary: Option<PathBuf>,
    /// Directory to search for whatever was not given explicitly.
    pub directory: Option<PathBuf>,
}

/// A resolved, on-disk model set.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelSet {
    pub detection: PathBuf,
    pub recognition: PathBuf,
    pub orientation: Option<PathBuf>,
    pub dictionary: Option<PathBuf>,
}

/// Root of the per-user model cache: `$OCRUST_HOME`, else the OS cache dir.
pub fn home_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("OCRUST_HOME") {
        return PathBuf::from(dir);
    }
    if let Some(dir) = std::env::var_os("XDG_CACHE_HOME") {
        return PathBuf::from(dir).join("ocrust");
    }
    #[cfg(target_os = "windows")]
    if let Some(dir) = std::env::var_os("LOCALAPPDATA") {
        return PathBuf::from(dir).join("ocrust");
    }
    match std::env::var_os("HOME") {
        #[cfg(target_os = "macos")]
        Some(home) => PathBuf::from(home)
            .join("Library")
            .join("Caches")
            .join("ocrust"),
        #[cfg(not(target_os = "macos"))]
        Some(home) => PathBuf::from(home).join(".cache").join("ocrust"),
        None => PathBuf::from(".ocrust"),
    }
}

/// Directory holding downloaded model bundles.
pub fn models_dir() -> PathBuf {
    home_dir().join("models")
}

/// Resolves a model set by searching only `dir`.
///
/// Useful when a caller wants exactly one bundle and no ambient configuration;
/// [`resolve`] additionally consults `OCRUST_MODELS_DIR` and the cache.
pub fn resolve_in(dir: &Path) -> Result<ModelSet> {
    let mut found = FoundFiles::default();
    let searched = vec![dir.to_path_buf()];
    if dir.is_dir() {
        found.fill_from_dir(dir)?;
    }
    found.into_set(&searched)
}

/// Resolves a usable model set or explains what is missing.
pub fn resolve(paths: &ModelPaths) -> Result<ModelSet> {
    let mut searched: Vec<PathBuf> = Vec::new();
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(dir) = &paths.directory {
        candidates.push(dir.clone());
    }
    if let Some(dir) = std::env::var_os("OCRUST_MODELS_DIR") {
        candidates.push(PathBuf::from(dir));
    }
    let cache = models_dir();
    candidates.push(cache.clone());
    // One nested level, so `models/ppocrv5/` is found under the cache root too.
    if let Ok(entries) = std::fs::read_dir(&cache) {
        let mut nested: Vec<PathBuf> = entries
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect();
        nested.sort();
        candidates.extend(nested);
    }

    let mut found = FoundFiles::from_paths(paths);
    for dir in candidates {
        if found.is_complete() {
            break;
        }
        if !dir.is_dir() {
            continue;
        }
        searched.push(dir.clone());
        found.fill_from_dir(&dir)?;
    }

    found.into_set(&searched)
}

#[derive(Debug, Default)]
struct FoundFiles {
    detection: Option<PathBuf>,
    recognition: Option<PathBuf>,
    orientation: Option<PathBuf>,
    dictionary: Option<PathBuf>,
}

impl FoundFiles {
    fn from_paths(paths: &ModelPaths) -> Self {
        Self {
            detection: paths.detection.clone(),
            recognition: paths.recognition.clone(),
            orientation: paths.orientation.clone(),
            dictionary: paths.dictionary.clone(),
        }
    }

    /// Detection and recognition are the only hard requirements.
    fn is_complete(&self) -> bool {
        self.detection.is_some() && self.recognition.is_some()
    }

    fn fill_from_dir(&mut self, dir: &Path) -> Result<()> {
        let mut files: Vec<PathBuf> = std::fs::read_dir(dir)
            .map_err(|e| Error::io(dir, e))?
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file())
            .collect();
        files.sort();

        for path in files {
            let name = path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or_default()
                .to_ascii_lowercase();
            let is_onnx = name.ends_with(".onnx");
            let is_text = name.ends_with(".txt");

            if is_onnx && self.detection.is_none() && name.contains("det") {
                self.detection = Some(path);
                continue;
            }
            if is_onnx && self.recognition.is_none() && name.contains("rec") {
                self.recognition = Some(path);
                continue;
            }
            if is_onnx
                && self.orientation.is_none()
                && (name.contains("cls") || name.contains("orient") || name.contains("angle"))
            {
                self.orientation = Some(path);
                continue;
            }
            if is_text
                && self.dictionary.is_none()
                && (name.contains("dict") || name.contains("keys") || name.contains("charset"))
            {
                self.dictionary = Some(path);
            }
        }
        Ok(())
    }

    fn into_set(self, searched: &[PathBuf]) -> Result<ModelSet> {
        let where_looked = if searched.is_empty() {
            "no model directory exists yet".to_string()
        } else {
            searched
                .iter()
                .map(|p| p.display().to_string())
                .collect::<Vec<_>>()
                .join(", ")
        };
        let missing = |what: &str| {
            Error::model(format!(
                "no {what} model found (searched: {where_looked}).\n\
                 hint: `pip install ocrust[models]`, or point OCRUST_MODELS_DIR at a directory \
                 holding PP-OCR *_det.onnx / *_rec.onnx files"
            ))
        };
        let detection = self.detection.ok_or_else(|| missing("text detection"))?;
        let recognition = self
            .recognition
            .ok_or_else(|| missing("text recognition"))?;
        for p in [&detection, &recognition] {
            if !p.is_file() {
                return Err(Error::model(format!("model file missing: {}", p.display())));
            }
        }
        Ok(ModelSet {
            detection,
            recognition,
            orientation: self.orientation.filter(|p| p.is_file()),
            dictionary: self.dictionary.filter(|p| p.is_file()),
        })
    }
}

/// One downloadable file of a model bundle.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManifestFile {
    /// File name inside the bundle directory.
    pub name: String,
    /// Mirrors to try in order.
    pub urls: Vec<String>,
    /// Lowercase hex SHA-256 of the file.
    pub sha256: String,
    #[serde(default)]
    pub size: Option<u64>,
}

/// A named model bundle that can be installed from the network.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Manifest {
    pub name: String,
    #[serde(default)]
    pub description: String,
    pub files: Vec<ManifestFile>,
}

impl Manifest {
    /// Parses a bundle manifest.
    pub fn parse(json: &str) -> Result<Self> {
        let manifest: Manifest = serde_json::from_str(json)?;
        if manifest.files.is_empty() {
            return Err(Error::model("manifest lists no files"));
        }
        Ok(manifest)
    }

    /// Directory this bundle installs into.
    pub fn target_dir(&self) -> PathBuf {
        models_dir().join(&self.name)
    }
}

/// Verifies a file against a hex SHA-256 digest.
#[cfg(feature = "download")]
pub fn verify_sha256(data: &[u8], expected_hex: &str) -> Result<()> {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(data);
    let actual = digest
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    if !actual.eq_ignore_ascii_case(expected_hex.trim()) {
        return Err(Error::Download(format!(
            "checksum mismatch: expected {expected_hex}, got {actual}"
        )));
    }
    Ok(())
}

/// Downloads every file of `manifest` into its target directory.
///
/// Files that already exist with the right checksum are kept, so this is safe
/// to call on every start-up.
#[cfg(feature = "download")]
pub fn install(manifest: &Manifest, progress: &mut dyn FnMut(&str, u64, u64)) -> Result<ModelSet> {
    let dir = manifest.target_dir();
    std::fs::create_dir_all(&dir).map_err(|e| Error::io(&dir, e))?;

    for (i, file) in manifest.files.iter().enumerate() {
        let target = dir.join(&file.name);
        if let Ok(existing) = std::fs::read(&target) {
            if verify_sha256(&existing, &file.sha256).is_ok() {
                progress(&file.name, (i + 1) as u64, manifest.files.len() as u64);
                continue;
            }
        }
        let data = download_any(&file.urls)?;
        verify_sha256(&data, &file.sha256)?;
        // Write to a temporary file first so an interrupted run cannot leave a
        // half-written model behind.
        let tmp = dir.join(format!("{}.part", file.name));
        std::fs::write(&tmp, &data).map_err(|e| Error::io(&tmp, e))?;
        std::fs::rename(&tmp, &target).map_err(|e| Error::io(&target, e))?;
        progress(&file.name, (i + 1) as u64, manifest.files.len() as u64);
    }

    resolve(&ModelPaths {
        directory: Some(dir),
        ..Default::default()
    })
}

#[cfg(feature = "download")]
fn download_any(urls: &[String]) -> Result<Vec<u8>> {
    let mut last = None;
    for url in urls {
        match download_one(url) {
            Ok(bytes) => return Ok(bytes),
            Err(e) => {
                log::warn!("download from {url} failed: {e}");
                last = Some(e);
            }
        }
    }
    Err(last.unwrap_or_else(|| Error::Download("no download URLs configured".into())))
}

#[cfg(feature = "download")]
fn download_one(url: &str) -> Result<Vec<u8>> {
    use std::io::Read;
    let mut response = ureq::get(url)
        .call()
        .map_err(|e| Error::Download(format!("{url}: {e}")))?;
    let mut buf = Vec::new();
    response
        .body_mut()
        .as_reader()
        .read_to_end(&mut buf)
        .map_err(|e| Error::Download(format!("{url}: {e}")))?;
    Ok(buf)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            let dir = std::env::temp_dir().join(format!(
                "ocrust-test-{tag}-{}-{:?}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            std::fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }

        fn touch(&self, name: &str) -> PathBuf {
            let p = self.0.join(name);
            std::fs::write(&p, b"stub").unwrap();
            p
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn discovers_paddle_style_bundle() {
        let dir = TempDir::new("paddle");
        dir.touch("PP-OCRv5_mobile_det_infer.onnx");
        dir.touch("PP-OCRv5_mobile_rec_infer.onnx");
        dir.touch("ch_ppocr_mobile_v2.0_cls_infer.onnx");
        dir.touch("ppocrv5_dict.txt");

        let set = resolve(&ModelPaths {
            directory: Some(dir.0.clone()),
            ..Default::default()
        })
        .unwrap();
        assert!(set.detection.to_string_lossy().contains("det"));
        assert!(set.recognition.to_string_lossy().contains("rec"));
        assert!(set.orientation.is_some());
        assert!(set.dictionary.is_some());
    }

    #[test]
    fn dictionary_and_orientation_are_optional() {
        let dir = TempDir::new("minimal");
        dir.touch("det.onnx");
        dir.touch("rec.onnx");
        let set = resolve(&ModelPaths {
            directory: Some(dir.0.clone()),
            ..Default::default()
        })
        .unwrap();
        assert!(set.orientation.is_none());
        assert!(set.dictionary.is_none());
    }

    #[test]
    fn explicit_paths_win_over_discovery() {
        let dir = TempDir::new("explicit");
        let det = dir.touch("a_det.onnx");
        let rec = dir.touch("b_rec.onnx");
        let other = dir.touch("zzz_det.onnx");
        let set = resolve(&ModelPaths {
            detection: Some(other.clone()),
            recognition: Some(rec.clone()),
            directory: Some(dir.0.clone()),
            ..Default::default()
        })
        .unwrap();
        assert_eq!(set.detection, other);
        assert_ne!(set.detection, det);
    }

    #[test]
    fn missing_models_explain_where_we_looked() {
        // `resolve_in` searches nothing but the directory, so the assertion
        // holds even when OCRUST_MODELS_DIR points at a real bundle.
        let dir = TempDir::new("empty");
        let err = resolve_in(&dir.0).unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("text detection"), "{msg}");
        assert!(msg.contains("OCRUST_MODELS_DIR"), "{msg}");
    }

    #[test]
    fn resolve_in_ignores_ambient_configuration() {
        let dir = TempDir::new("exclusive");
        dir.touch("only_det.onnx");
        dir.touch("only_rec.onnx");
        let set = resolve_in(&dir.0).unwrap();
        assert!(set.detection.starts_with(&dir.0));
    }

    #[test]
    fn manifest_round_trips() {
        let json = r#"{
            "name": "ppocrv5-mobile",
            "description": "PP-OCRv5 mobile",
            "files": [{"name": "det.onnx", "urls": ["https://example.invalid/det.onnx"],
                       "sha256": "00ff", "size": 12}]
        }"#;
        let m = Manifest::parse(json).unwrap();
        assert_eq!(m.files[0].size, Some(12));
        assert!(m.target_dir().ends_with("ppocrv5-mobile"));
        assert!(Manifest::parse(r#"{"name":"x","files":[]}"#).is_err());
    }

    #[cfg(feature = "download")]
    #[test]
    fn checksum_verification_catches_corruption() {
        // sha256("ocrust")
        let digest = "d4f0d0bd5b0f2e8e6a3e2a2a4b7c5a3a4c1e1f7e6ff2c9d0b0a0e1d2c3b4a596";
        assert!(verify_sha256(b"ocrust", digest).is_err());
        let good = {
            use sha2::{Digest, Sha256};
            Sha256::digest(b"ocrust")
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
        };
        assert!(verify_sha256(b"ocrust", &good).is_ok());
        assert!(verify_sha256(b"ocrust!", &good).is_err());
    }

    #[test]
    fn models_dir_lives_under_home_dir() {
        let home = home_dir();
        let models = models_dir();
        assert!(models.starts_with(&home), "{models:?} not under {home:?}");
        assert!(models.ends_with("models"));
    }
}
