//! Language support: what a recognition model can actually spell.
//!
//! A recognizer can only emit characters from its own class list, so asking a
//! Chinese-only model for German text does not fail — it silently returns
//! "Grusse" for "Grüße". That is the worst possible outcome, so `ocrust` checks
//! the model's character set against the requested languages up front and
//! refuses with the exact list of characters it cannot represent.
//!
//! The per-language letters below are the alphabets themselves, cross-checked
//! against PaddleOCR's own dictionaries
//! (`ppocr/utils/dict/{german,french,it,latin,cyrillic,…}_dict.txt`). Only
//! letters are required; punctuation coverage is reported separately because a
//! missing typographic quote does not corrupt a word.

use crate::dict::CharDict;

/// Writing system a language uses. Models are usually trained per script, so
/// this is what decides which model bundle is needed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Script {
    Latin,
    Cyrillic,
    Greek,
    /// Chinese characters, including Japanese kanji.
    Han,
    /// Japanese hiragana and katakana.
    Kana,
    Hangul,
    Arabic,
    Devanagari,
}

impl Script {
    pub fn name(&self) -> &'static str {
        match self {
            Self::Latin => "latin",
            Self::Cyrillic => "cyrillic",
            Self::Greek => "greek",
            Self::Han => "han",
            Self::Kana => "kana",
            Self::Hangul => "hangul",
            Self::Arabic => "arabic",
            Self::Devanagari => "devanagari",
        }
    }
}

/// A language `ocrust` knows how to check for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Language {
    /// ISO 639-1 code where one exists, otherwise a BCP-47-style tag.
    pub code: &'static str,
    pub name: &'static str,
    pub script: Script,
    /// Letters beyond plain ASCII that this language needs, both cases.
    letters: &'static str,
    /// Sample characters used to probe non-alphabetic scripts.
    sample: &'static str,
}

impl Language {
    /// Letters beyond ASCII that a model must be able to emit.
    pub fn required_letters(&self) -> impl Iterator<Item = char> + '_ {
        self.letters.chars().chain(self.sample.chars())
    }

    /// True for scripts whose base alphabet is ASCII.
    pub fn is_latin(&self) -> bool {
        self.script == Script::Latin
    }
}

/// Looks up a language by ISO code or English name, case-insensitively.
///
/// Accepts `"de"`, `"deu"`, `"german"`, `"de-DE"` and `"DE"`.
pub fn parse(input: &str) -> Option<&'static Language> {
    let raw = input.trim().to_ascii_lowercase();
    // The full tag is tried before its primary subtag, so "zh-hant" does not
    // collapse onto "zh".
    let exact = LANGUAGES
        .iter()
        .find(|l| l.code == raw)
        .or_else(|| {
            LANGUAGES
                .iter()
                .find(|l| l.name.to_ascii_lowercase() == raw)
        })
        .or_else(|| LANGUAGES.iter().find(|l| alias_matches(l, &raw)));
    if exact.is_some() {
        return exact;
    }
    // A tag written with an underscore is the same tag: `zh_hant` must not fall
    // through to `zh`, which is a different script.
    let dashed = raw.replace('_', "-");
    if dashed != raw {
        if let Some(found) = LANGUAGES.iter().find(|l| l.code == dashed) {
            return Some(found);
        }
    }
    let primary = raw.split(['-', '_']).next().unwrap_or(&raw);
    LANGUAGES
        .iter()
        .find(|l| l.code == primary)
        .or_else(|| LANGUAGES.iter().find(|l| alias_matches(l, primary)))
}

/// Three-letter and colloquial aliases, so `deu`/`ger`/`fra` work too.
fn alias_matches(language: &Language, needle: &str) -> bool {
    let aliases: &[&str] = match language.code {
        "de" => &["deu", "ger"],
        "en" => &["eng"],
        "fr" => &["fra", "fre"],
        "es" => &["spa"],
        "it" => &["ita"],
        "pt" => &["por"],
        "nl" => &["nld", "dut"],
        "sv" => &["swe"],
        "da" => &["dan"],
        "nb" => &["no", "nor", "nob"],
        "fi" => &["fin"],
        "pl" => &["pol"],
        "cs" => &["ces", "cze"],
        "sk" => &["slk", "slo"],
        "hu" => &["hun"],
        "ro" => &["ron", "rum"],
        "tr" => &["tur"],
        "hr" => &["hrv"],
        "sl" => &["slv"],
        "et" => &["est"],
        "lv" => &["lav"],
        "lt" => &["lit"],
        "el" => &["ell", "gre"],
        "ru" => &["rus"],
        "uk" => &["ukr"],
        "bg" => &["bul"],
        "sr" => &["srp"],
        "ja" => &["jpn", "japan"],
        "ko" => &["kor", "korean"],
        "zh" => &["chi", "zho", "ch", "chinese"],
        "zh-hant" => &["cht", "chinese_cht", "traditional"],
        "ar" => &["ara", "arabic"],
        "hi" => &["hin", "devanagari"],
        "vi" => &["vie"],
        _ => &[],
    };
    aliases.contains(&needle)
}

/// Every language with a coverage definition.
pub fn all() -> &'static [Language] {
    LANGUAGES
}

/// The table. `letters` lists the non-ASCII letters of the alphabet; `sample`
/// holds probe characters for scripts that are not alphabetic.
static LANGUAGES: &[Language] = &[
    lang("en", "English", Script::Latin, "", ""),
    lang("de", "German", Script::Latin, "äöüÄÖÜß", ""),
    lang(
        "fr",
        "French",
        Script::Latin,
        "àâçéèêëîïôùûüÿœæÀÂÇÉÈÊËÎÏÔÙÛÜŸŒÆ",
        "",
    ),
    lang("es", "Spanish", Script::Latin, "áéíóúüñÁÉÍÓÚÜÑ", ""),
    lang("it", "Italian", Script::Latin, "àèéìíîòóùúÀÈÉÌÒÙ", ""),
    lang(
        "pt",
        "Portuguese",
        Script::Latin,
        "ãõáéíóúâêôàçÃÕÁÉÍÓÚÂÊÔÀÇ",
        "",
    ),
    lang("nl", "Dutch", Script::Latin, "éëïöüÉËÏÖÜ", ""),
    lang("sv", "Swedish", Script::Latin, "åäöÅÄÖ", ""),
    lang("da", "Danish", Script::Latin, "æøåÆØÅ", ""),
    lang("nb", "Norwegian", Script::Latin, "æøåÆØÅ", ""),
    lang("fi", "Finnish", Script::Latin, "äöåÄÖÅ", ""),
    lang("is", "Icelandic", Script::Latin, "áðéíóúýþæöÁÐÉÍÓÚÝÞÆÖ", ""),
    lang("pl", "Polish", Script::Latin, "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", ""),
    lang(
        "cs",
        "Czech",
        Script::Latin,
        "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ",
        "",
    ),
    lang(
        "sk",
        "Slovak",
        Script::Latin,
        "áäčďéíĺľňóôŕšťúýžÁÄČĎÉÍĽŇÓÔŠŤÚÝŽ",
        "",
    ),
    lang("hu", "Hungarian", Script::Latin, "áéíóöőúüűÁÉÍÓÖŐÚÜŰ", ""),
    lang("ro", "Romanian", Script::Latin, "ăâîșțĂÂÎȘȚ", ""),
    lang("tr", "Turkish", Script::Latin, "çğıöşüÇĞİÖŞÜ", ""),
    lang("hr", "Croatian", Script::Latin, "čćđšžČĆĐŠŽ", ""),
    lang("sl", "Slovenian", Script::Latin, "čšžČŠŽ", ""),
    lang("et", "Estonian", Script::Latin, "äöõüÄÖÕÜ", ""),
    lang("lv", "Latvian", Script::Latin, "āčēģīķļņšūžĀČĒĢĪĶĻŅŠŪŽ", ""),
    lang("lt", "Lithuanian", Script::Latin, "ąčęėįšųūžĄČĘĖĮŠŲŪŽ", ""),
    lang(
        "vi",
        "Vietnamese",
        Script::Latin,
        "ăâđêôơưáàảãạÁÀĂÂĐÊÔƠƯ",
        "",
    ),
    lang(
        "el",
        "Greek",
        Script::Greek,
        "",
        "αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ",
    ),
    lang(
        "ru",
        "Russian",
        Script::Cyrillic,
        "",
        "абвгдежзийклмнопрстуфхцчшщъыьэюяАБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ",
    ),
    lang(
        "uk",
        "Ukrainian",
        Script::Cyrillic,
        "",
        "абвгґдеєжзиіїйклмнопрстуфхцчшщьюяАБВГДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЮЯ",
    ),
    lang(
        "bg",
        "Bulgarian",
        Script::Cyrillic,
        "",
        "абвгдежзийклмнопрстуфхцчшщъьюяАБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЮЯ",
    ),
    lang(
        "sr",
        "Serbian",
        Script::Cyrillic,
        "",
        "абвгдђежзијклљмнњопрстћуфхцчџшАБВГДЂЕЖЗИЈКЛЉМНЊОПРСТЋУФХЦЧДШ",
    ),
    lang(
        "ja",
        "Japanese",
        Script::Kana,
        "",
        "あいうえおかきくけこさしすせそたちつてとなにぬねのアイウエオカキクケコサシスセソ日本語",
    ),
    lang(
        "ko",
        "Korean",
        Script::Hangul,
        "",
        "가나다라마바사아자차카타파하한국어글",
    ),
    lang(
        "zh",
        "Chinese (Simplified)",
        Script::Han,
        "",
        "的一是不了人我在有他这中大来上国说们为",
    ),
    lang(
        "zh-hant",
        "Chinese (Traditional)",
        Script::Han,
        "",
        "的一是不了人我在有他這中大來上國說們為",
    ),
    lang(
        "ar",
        "Arabic",
        Script::Arabic,
        "",
        "ابتثجحخدذرزسشصضطظعغفقكلمنهوي",
    ),
    lang(
        "hi",
        "Hindi",
        Script::Devanagari,
        "",
        "अआइईउऊएऐओऔकखगघचछजझटठडढणतथदधनपफबभमयरलवशषसह",
    ),
];

const fn lang(
    code: &'static str,
    name: &'static str,
    script: Script,
    letters: &'static str,
    sample: &'static str,
) -> Language {
    Language {
        code,
        name,
        script,
        letters,
        sample,
    }
}

/// How well a model's character set covers one language.
#[derive(Debug, Clone)]
pub struct Coverage {
    pub language: &'static Language,
    /// Characters the model cannot emit at all.
    pub missing: Vec<char>,
    /// Characters the language needs, in total.
    pub required: usize,
}

impl Coverage {
    pub fn is_complete(&self) -> bool {
        self.missing.is_empty()
    }

    /// Share of the required characters the model can emit, in `0..=1`.
    pub fn ratio(&self) -> f32 {
        if self.required == 0 {
            return 1.0;
        }
        1.0 - self.missing.len() as f32 / self.required as f32
    }

    /// The missing characters, as a readable list.
    pub fn missing_display(&self) -> String {
        self.missing
            .iter()
            .map(|c| c.to_string())
            .collect::<Vec<_>>()
            .join(" ")
    }
}

/// Checks `language` against a recognizer's character set.
pub fn coverage(dict: &CharDict, language: &'static Language) -> Coverage {
    let mut required: Vec<char> = language.required_letters().collect();
    if language.is_latin() {
        // Latin-script languages also need the ASCII alphabet and digits.
        required.extend('a'..='z');
        required.extend('A'..='Z');
        required.extend('0'..='9');
    }
    required.sort_unstable();
    required.dedup();

    let missing: Vec<char> = required
        .iter()
        .copied()
        .filter(|c| !dict.contains(*c))
        .collect();
    Coverage {
        language,
        required: required.len(),
        missing,
    }
}

/// Every language the character set covers completely.
pub fn supported(dict: &CharDict) -> Vec<&'static Language> {
    LANGUAGES
        .iter()
        .filter(|l| coverage(dict, l).is_complete())
        .collect()
}

/// Languages the character set covers at least `min_ratio` of, best first.
///
/// Useful for a diagnostic listing: a model at 0.95 is usable for most text of
/// that language but will stumble on a few accents.
pub fn partial(dict: &CharDict, min_ratio: f32) -> Vec<Coverage> {
    let mut out: Vec<Coverage> = LANGUAGES
        .iter()
        .map(|l| coverage(dict, l))
        .filter(|c| !c.is_complete() && c.ratio() >= min_ratio)
        .collect();
    out.sort_by(|a, b| {
        b.ratio()
            .partial_cmp(&a.ratio())
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dict_of(chars: &str) -> CharDict {
        let entries: Vec<String> = chars.chars().map(|c| c.to_string()).collect();
        CharDict::from_lines(entries, None).unwrap()
    }

    fn ascii() -> String {
        let mut s = String::new();
        s.extend('a'..='z');
        s.extend('A'..='Z');
        s.extend('0'..='9');
        s
    }

    #[test]
    fn language_lookup_accepts_codes_names_and_aliases() {
        assert_eq!(parse("de").unwrap().name, "German");
        assert_eq!(parse("DE").unwrap().code, "de");
        assert_eq!(parse("de-DE").unwrap().code, "de");
        assert_eq!(parse("deu").unwrap().code, "de");
        assert_eq!(parse("german").unwrap().code, "de");
        assert_eq!(parse("fra").unwrap().code, "fr");
        assert_eq!(parse("no").unwrap().code, "nb");
        assert_eq!(parse("chinese_cht").unwrap().code, "zh-hant");
        // An underscore is the same tag, not a reason to fall back to `zh`.
        assert_eq!(parse("zh_hant").unwrap().code, "zh-hant");
        assert_eq!(parse("zh-Hant").unwrap().code, "zh-hant");
        assert_eq!(parse("zh").unwrap().code, "zh");
        assert!(parse("klingon").is_none());
    }

    #[test]
    fn every_entry_is_unique_and_parseable() {
        let mut codes: Vec<&str> = all().iter().map(|l| l.code).collect();
        codes.sort_unstable();
        let count = codes.len();
        codes.dedup();
        assert_eq!(codes.len(), count, "duplicate language codes");
        for language in all() {
            assert_eq!(parse(language.code).map(|l| l.code), Some(language.code));
            assert!(!language.name.is_empty());
        }
    }

    #[test]
    fn ascii_only_dict_covers_english_but_not_german() {
        let dict = dict_of(&ascii());
        assert!(coverage(&dict, parse("en").unwrap()).is_complete());

        let german = coverage(&dict, parse("de").unwrap());
        assert!(!german.is_complete());
        assert_eq!(german.missing.len(), 7, "{:?}", german.missing);
        assert!(german.missing.contains(&'ß'));
        assert!(german.ratio() > 0.85, "{}", german.ratio());
    }

    #[test]
    fn german_dict_covers_german_and_english() {
        let dict = dict_of(&format!("{}äöüÄÖÜß", ascii()));
        assert!(coverage(&dict, parse("de").unwrap()).is_complete());
        assert!(coverage(&dict, parse("en").unwrap()).is_complete());
        // Not French: the accents are still missing.
        assert!(!coverage(&dict, parse("fr").unwrap()).is_complete());
    }

    #[test]
    fn supported_lists_only_complete_languages() {
        let dict = dict_of(&format!("{}äöüÄÖÜßåÅæÆøØ", ascii()));
        let codes: Vec<&str> = supported(&dict).iter().map(|l| l.code).collect();
        assert!(codes.contains(&"de"), "{codes:?}");
        assert!(codes.contains(&"en"), "{codes:?}");
        assert!(codes.contains(&"da"), "{codes:?}");
        assert!(!codes.contains(&"ru"), "{codes:?}");
        assert!(!codes.contains(&"ja"), "{codes:?}");
    }

    #[test]
    fn partial_ranks_close_matches_first() {
        // Everything but ß: German should come out as a near miss.
        let dict = dict_of(&format!("{}äöüÄÖÜ", ascii()));
        let near = partial(&dict, 0.9);
        assert_eq!(near.first().map(|c| c.language.code), Some("de"));
        assert_eq!(near[0].missing, vec!['ß']);
        assert!(near[0].missing_display().contains('ß'));
    }

    #[test]
    fn non_latin_languages_need_their_script() {
        let dict = dict_of(&ascii());
        for code in ["ru", "el", "ja", "ko", "zh", "ar", "hi"] {
            let cov = coverage(&dict, parse(code).unwrap());
            assert!(!cov.is_complete(), "{code} should not be covered by ASCII");
            assert!(cov.ratio() < 0.1, "{code}: {}", cov.ratio());
        }
    }

    #[test]
    fn scripts_have_names() {
        assert_eq!(parse("ru").unwrap().script.name(), "cyrillic");
        assert_eq!(parse("ja").unwrap().script, Script::Kana);
        assert!(parse("de").unwrap().is_latin());
    }
}
