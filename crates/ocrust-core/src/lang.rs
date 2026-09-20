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

    /// Characters this language accepts but can be written correctly without.
    pub fn optional_letters(&self) -> impl Iterator<Item = char> + '_ {
        OPTIONAL
            .iter()
            .filter(move |(code, _)| *code == self.code)
            .flat_map(|(_, chars)| chars.chars())
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
    lang(
        "it",
        "Italian",
        Script::Latin,
        "àèéìíîòóùúÀÈÉÌÍÎÒÓÙÚ",
        "",
    ),
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
        "áäčďéíĺľňóôŕšťúýžÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ",
        "",
    ),
    lang("hu", "Hungarian", Script::Latin, "áéíóöőúüűÁÉÍÓÖŐÚÜŰ", ""),
    lang("ro", "Romanian", Script::Latin, "ăâîșțĂÂÎȘȚ", ""),
    lang("tr", "Turkish", Script::Latin, "çğıöşüÇĞİÖŞÜ", ""),
    lang("hr", "Croatian", Script::Latin, "čćđšžČĆĐŠŽ", ""),
    lang("sl", "Slovenian", Script::Latin, "čšžČŠŽ", ""),
    lang("et", "Estonian", Script::Latin, "äöõüšžÄÖÕÜŠŽ", ""),
    lang("lv", "Latvian", Script::Latin, "āčēģīķļņšūžĀČĒĢĪĶĻŅŠŪŽ", ""),
    lang("lt", "Lithuanian", Script::Latin, "ąčęėįšųūžĄČĘĖĮŠŲŪŽ", ""),
    lang(
        "vi",
        "Vietnamese",
        Script::Latin,
        // Quoc ngu in full: 12 vowel letters x 5 tones, both cases, plus d-bar.
        // The tones are the language, not decoration — `phai`, `phái`, `phải`
        // and `phải` are different words — so a sample of the alphabet is not a
        // coverage test. Listing twenty of these forms once put Vietnamese at
        // "98%, missing ạ ả" when the bundle is short of 88 of the 146.
        "aàáảãạăằắẳẵặâầấẩẫậeèéẻẽẹêềếểễệiìíỉĩịoòóỏõọôồốổỗộơờớởỡợuùúủũụưừứửữựyỳýỷỹỵđAÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬEÈÉẺẼẸÊỀẾỂỄỆIÌÍỈĨỊOÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢUÙÚỦŨỤƯỪỨỬỮỰYỲÝỶỸỴĐ",
        "",
    ),
    lang(
        "el",
        "Greek",
        Script::Greek,
        "",
        // Monotonic Greek in full. The accented vowels and the final sigma are
        // not decoration: every polysyllabic Greek word carries an accent, and
        // ς ends a large share of them, so a model without them cannot write
        // the language at all. Listing only the plain letters — as this entry
        // once did — made the bundled recognizer look like it covered Greek
        // while it silently dropped every accent it met.
        "αβγδεζηθικλμνξοπρστυφχψωςάέήίόύώΐΰϊϋΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩΆΈΉΊΌΎΏΪΫ",
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
        "абвгґдеєжзиіїйклмнопрстуфхцчшщьюяАБВГҐДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯ",
    ),
    lang(
        "bg",
        "Bulgarian",
        Script::Cyrillic,
        "",
        "абвгдежзийклмнопрстуфхцчшщъьюяАБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЬЮЯ",
    ),
    lang(
        "sr",
        "Serbian",
        Script::Cyrillic,
        "",
        "абвгдђежзијклљмнњопрстћуфхцчџшАБВГДЂЕЖЗИЈКЛЉМНЊОПРСТЋУФХЦЧЏШ",
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

/// Characters a language accepts but does not need in order to be written
/// correctly. A model that lacks these still covers the language, so they are
/// reported and never refused.
///
/// `ẞ` is the case in point: the capital of `ß`, permitted since 2017 and never
/// required — the canonical uppercase of `ß` is `SS`, and a recognizer without
/// `ẞ` returns `STRAßE` for `STRAẞE`: the right letters, one in the wrong case.
/// Requiring it would refuse German over a character German does not oblige
/// anyone to use, which is the opposite of what the check is for.
static OPTIONAL: &[(&str, &str)] = &[("de", "ẞ")];

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
    /// Accepted-but-optional characters the model cannot emit. These never
    /// count against coverage — see `OPTIONAL` — but a reader deserves to know
    /// that `ẞ` will come back as `ß`.
    pub missing_optional: Vec<char>,
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
    let missing_optional: Vec<char> = language
        .optional_letters()
        .filter(|c| !dict.contains(*c))
        .collect();
    Coverage {
        language,
        required: required.len(),
        missing,
        missing_optional,
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
/// Covered languages whose accepted-but-optional characters the model is short
/// of: the language is written correctly without them, so this is a note, not a
/// refusal.
pub fn optional_gaps(dict: &CharDict) -> Vec<Coverage> {
    all()
        .iter()
        .map(|l| coverage(dict, l))
        .filter(|c| c.is_complete() && !c.missing_optional.is_empty())
        .collect()
}

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

    /// `ẞ` is reported, never required. Requiring it would refuse German over a
    /// character German does not oblige anyone to use — and the substitute a
    /// model without it produces, `STRAßE`, is the right letters in the wrong
    /// case, not a corrupted word.
    #[test]
    fn a_missing_capital_sharp_s_does_not_cost_german_its_coverage() {
        let de = parse("de").unwrap();
        assert!(de.optional_letters().any(|c| c == 'ẞ'));
        assert!(
            !de.required_letters().any(|c| c == 'ẞ'),
            "ẞ must not be required"
        );

        let dict = dict_of(&format!("{}äöüÄÖÜß", ascii()));
        let cov = coverage(&dict, de);
        assert!(cov.is_complete(), "German must stay covered without ẞ");
        assert_eq!(cov.missing_optional, vec!['ẞ']);

        // And when the model does have it there is nothing to report.
        let richer = dict_of(&format!("{}äöüÄÖÜßẞ", ascii()));
        assert!(coverage(&richer, de).missing_optional.is_empty());
    }

    /// Vietnamese tone marks are the language, not decoration: `phai`, `phái`
    /// and `phải` are three words. Declaring a sample of the alphabet reported
    /// the bundle at 98% when it is short of most of it.
    #[test]
    fn vietnamese_requires_every_tone_mark() {
        let vi = parse("vi").unwrap();
        let required: String = vi.required_letters().collect();
        assert_eq!(required.chars().count(), 146, "{required}");
        for c in ['ả', 'Ả', 'ệ', 'Ệ', 'ớ', 'Ớ', 'ự', 'Ự', 'đ', 'Đ'] {
            assert!(required.contains(c), "Vietnamese must require {c}");
        }
        // The twenty forms that used to stand in for the alphabet are not a
        // coverage test: a dict holding just those must not pass.
        let thin = dict_of(&format!("{}ăâđêôơưáàảãạÁÀĂÂĐÊÔƠƯ", ascii()));
        let cov = coverage(&thin, vi);
        assert!(!cov.is_complete());
        assert!(cov.ratio() < 0.5, "ratio {}", cov.ratio());
    }

    /// Every declared letter needs its other case declared too, or text set in
    /// that case walks past the check. This is how `Ả` hid behind `ả`.
    #[test]
    fn declared_alphabets_name_both_cases() {
        for language in all() {
            let declared: Vec<char> = language
                .required_letters()
                .chain(language.optional_letters())
                .collect();
            for c in &declared {
                for other in [c.to_uppercase().next(), c.to_lowercase().next()] {
                    let Some(other) = other else { continue };
                    // Only single-character case pairs: `ß` uppercases to `SS`.
                    // ASCII needs no declaring — `coverage` requires all of it
                    // for a Latin script, which is why Turkish `ı` may name no
                    // `I` of its own.
                    if other == *c
                        || other.is_ascii()
                        || other.to_lowercase().count() != 1
                        || c.to_uppercase().count() != 1
                    {
                        continue;
                    }
                    assert!(
                        declared.contains(&other),
                        "{} declares {c} but not {other}",
                        language.code
                    );
                }
            }
        }
    }

    /// The bundled PP-OCRv6 recognizer carries plain Greek and nothing else.
    /// Greek is listed as known so that asking for it fails with the characters
    /// named, rather than returning accent-stripped text at high confidence.
    #[test]
    fn plain_greek_letters_do_not_cover_greek() {
        let el = parse("el").unwrap();
        let required: String = el.required_letters().collect();
        for c in ['ς', 'ά', 'έ', 'ή', 'ί', 'ό', 'ύ', 'ώ'] {
            assert!(required.contains(c), "Greek must require {c}");
        }

        // Exactly what the bundle can emit: both cases, no accents, no final
        // sigma — the charset that once passed as full Greek coverage.
        let plain = "αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ";
        let cov = coverage(&dict_of(&format!("{}{plain}", ascii())), el);
        assert!(!cov.is_complete(), "plain Greek must not count as coverage");
        assert!(cov.missing.contains(&'ς'));
        assert!(cov.missing.contains(&'ά'));
        // Below the 0.8 the CLI uses for "nearly covered", so Greek is not
        // offered as a near miss either.
        assert!(cov.ratio() < 0.8, "ratio {}", cov.ratio());
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
