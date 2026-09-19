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

/// The manifest of the model bundle shipped with this repository.
///
/// Every URL points at `raw.githubusercontent.com`, so a network that allows
/// GitHub and nothing else is enough to install models.
pub const BUILTIN_MANIFEST: &str = include_str!("../../../models/ppocrv6.json");

/// Git ref the built-in manifest downloads from.
///
/// Override with `OCRUST_MODEL_REF` to install models from a branch, tag or
/// commit other than the default.
pub fn manifest_ref() -> String {
    std::env::var("OCRUST_MODEL_REF").unwrap_or_else(|_| "main".to_string())
}

/// The bundle `ocrust models install` uses by default.
pub fn builtin_manifest() -> Result<Manifest> {
    let mut manifest = Manifest::parse(BUILTIN_MANIFEST)?;
    let git_ref = manifest_ref();
    for file in &mut manifest.files {
        for url in &mut file.urls {
            *url = url.replace("{ref}", &git_ref);
        }
    }
    Ok(manifest)
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
    ///
    /// A manifest decides where files are written, so it is checked here rather
    /// than trusted: a bundle may only name plain files inside its own directory.
    pub fn parse(json: &str) -> Result<Self> {
        let manifest: Manifest = serde_json::from_str(json)?;
        if manifest.files.is_empty() {
            return Err(Error::model("manifest lists no files"));
        }
        check_file_name(&manifest.name, "bundle name")?;
        for file in &manifest.files {
            check_file_name(&file.name, "file name")?;
            let hex = file.sha256.trim();
            if hex.len() != 64 || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
                return Err(Error::model(format!(
                    "{}: sha256 must be 64 hex digits, got {:?}",
                    file.name, file.sha256
                )));
            }
            if file.urls.is_empty() {
                return Err(Error::model(format!(
                    "{} lists no download URLs",
                    file.name
                )));
            }
        }
        Ok(manifest)
    }

    /// Directory this bundle installs into.
    pub fn target_dir(&self) -> PathBuf {
        models_dir().join(&self.name)
    }
}

/// Rejects a manifest name that is anything but a plain file name.
///
/// The name goes straight into a path under the model cache, so a `..` or a
/// separator in it would let a manifest write wherever it likes.
fn check_file_name(value: &str, what: &str) -> Result<()> {
    let plain = !value.is_empty()
        && value != "."
        && value != ".."
        && !value.contains(['/', '\\', '\0'])
        && Path::new(value).components().count() == 1;
    if plain {
        Ok(())
    } else {
        Err(Error::model(format!(
            "{what} {value:?} is not a plain file name"
        )))
    }
}

/// Largest download accepted for a file whose manifest gives no size.
#[cfg(feature = "download")]
const MAX_UNSIZED_DOWNLOAD: u64 = 512 * 1024 * 1024;

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
        let data = download_any(&file.urls, file.size)?;
        verify_sha256(&data, &file.sha256)?;
        // Write to a temporary file first so an interrupted run cannot leave a
        // half-written model behind. The process id keeps two installs running
        // side by side from writing the same scratch file.
        let tmp = dir.join(format!("{}.{}.part", file.name, std::process::id()));
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
fn download_any(urls: &[String], size: Option<u64>) -> Result<Vec<u8>> {
    let mut last = None;
    for url in urls {
        match download_one(url, size) {
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
fn download_one(url: &str, size: Option<u64>) -> Result<Vec<u8>> {
    use std::io::Read;
    let mut request = ureq::get(url);
    if let Some(token) = github_token(url) {
        request = request.header("Authorization", &format!("Bearer {token}"));
    }
    let mut response = request
        .call()
        .map_err(|e| Error::Download(format!("{url}: {e}")))?;
    // The manifest says how big the file is, so a mirror that keeps sending
    // cannot fill memory. One byte over the limit is enough to notice.
    let limit = size.unwrap_or(MAX_UNSIZED_DOWNLOAD);
    let mut buf = Vec::new();
    response
        .body_mut()
        .as_reader()
        .take(limit.saturating_add(1))
        .read_to_end(&mut buf)
        .map_err(|e| Error::Download(format!("{url}: {e}")))?;
    if buf.len() as u64 > limit {
        return Err(Error::Download(format!(
            "{url}: larger than the {limit} bytes the manifest declares"
        )));
    }
    Ok(buf)
}

/// A token to authenticate a GitHub download with, if one is configured.
///
/// Model bundles live in a repository, and that repository may be private or on
/// GitHub Enterprise. `OCRUST_GITHUB_TOKEN` (or `GITHUB_TOKEN`, which CI already
/// sets) is sent as a bearer token — but only to GitHub hosts, so a token can
/// never leak to a third-party mirror listed in a manifest.
#[cfg(feature = "download")]
fn github_token(url: &str) -> Option<String> {
    if !is_github_host(url) {
        return None;
    }
    ["OCRUST_GITHUB_TOKEN", "GITHUB_TOKEN"]
        .iter()
        .filter_map(|key| std::env::var(key).ok())
        .find(|value| !value.trim().is_empty())
}

/// Whether `url` points at GitHub over HTTPS.
#[cfg(feature = "download")]
fn is_github_host(url: &str) -> bool {
    let Some(rest) = url.strip_prefix("https://") else {
        return false;
    };
    let host = rest
        .split(['/', '?', '#'])
        .next()
        .unwrap_or_default()
        .rsplit('@')
        .next()
        .unwrap_or_default()
        .split(':')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    host == "github.com"
        || host == "raw.githubusercontent.com"
        || host == "api.github.com"
        || host == "codeload.github.com"
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

    #[cfg(feature = "download")]
    #[test]
    fn tokens_go_to_github_hosts_only() {
        assert!(is_github_host(
            "https://raw.githubusercontent.com/o/r/main/models/det.onnx"
        ));
        assert!(is_github_host("https://github.com/o/r/releases/download/x"));
        assert!(is_github_host("https://API.GitHub.com/repos/o/r"));
        // A mirror, a look-alike host and plain HTTP must never see the token.
        assert!(!is_github_host("https://mirror.example.com/det.onnx"));
        assert!(!is_github_host(
            "https://raw.githubusercontent.com.evil.test/det.onnx"
        ));
        assert!(!is_github_host(
            "https://evil.test/raw.githubusercontent.com"
        ));
        assert!(!is_github_host("http://raw.githubusercontent.com/o/r/det"));
        assert!(!is_github_host("https://user@evil.test/github.com/det"));
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
    fn builtin_manifest_points_at_github_and_carries_checksums() {
        let manifest = builtin_manifest().unwrap();
        assert_eq!(manifest.name, "ppocrv6");
        assert_eq!(manifest.files.len(), 3);
        for file in &manifest.files {
            assert_eq!(file.sha256.len(), 64, "{} has no sha256", file.name);
            assert!(file.size.unwrap_or(0) > 0, "{} has no size", file.name);
            assert!(!file.urls.is_empty());
            for url in &file.urls {
                assert!(
                    url.starts_with("https://raw.githubusercontent.com/"),
                    "{url} is not a GitHub source"
                );
                assert!(!url.contains("{ref}"), "ref placeholder left in {url}");
            }
        }
        // Detection, recognition and orientation must all be covered.
        let names: Vec<&str> = manifest.files.iter().map(|f| f.name.as_str()).collect();
        assert!(names.iter().any(|n| n.contains("det")), "{names:?}");
        assert!(names.iter().any(|n| n.contains("rec")), "{names:?}");
        assert!(names.iter().any(|n| n.contains("cls")), "{names:?}");
    }

    #[test]
    fn manifest_ref_is_overridable() {
        // Default ref plus the documented override knob.
        assert!(!manifest_ref().is_empty());
        assert!(
            BUILTIN_MANIFEST.contains("{ref}"),
            "template keeps the placeholder"
        );
    }

    #[test]
    fn a_manifest_may_not_write_outside_its_bundle() {
        // The names end up in a path under the model cache.
        let evil = r#"{"name": "ppocrv6", "files": [{"name": "../../.bashrc",
                       "urls": ["https://example.invalid/x"],
                       "sha256": "0000000000000000000000000000000000000000000000000000000000000000"}]}"#;
        let err = Manifest::parse(evil).unwrap_err();
        assert!(err.to_string().contains("plain file name"), "{err}");

        let escaping_bundle = r#"{"name": "../evil", "files": [{"name": "det.onnx",
                       "urls": ["https://example.invalid/x"],
                       "sha256": "0000000000000000000000000000000000000000000000000000000000000000"}]}"#;
        assert!(Manifest::parse(escaping_bundle).is_err());
    }

    #[test]
    fn a_manifest_without_a_usable_checksum_is_rejected() {
        // Without this the file would be downloaded before anything noticed.
        let short = r#"{"name": "b", "files": [{"name": "det.onnx",
                       "urls": ["https://example.invalid/x"], "sha256": "00ff"}]}"#;
        let err = Manifest::parse(short).unwrap_err();
        assert!(err.to_string().contains("64 hex digits"), "{err}");

        let no_urls = r#"{"name": "b", "files": [{"name": "det.onnx", "urls": [],
                       "sha256": "0000000000000000000000000000000000000000000000000000000000000000"}]}"#;
        assert!(Manifest::parse(no_urls).is_err());
    }

    #[test]
    fn manifest_round_trips() {
        let json = r#"{
            "name": "ppocrv5-mobile",
            "description": "PP-OCRv5 mobile",
            "files": [{"name": "det.onnx", "urls": ["https://example.invalid/det.onnx"],
                       "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
                       "size": 12}]
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
