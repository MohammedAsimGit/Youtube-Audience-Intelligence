"""Unit tests: text normalization pipeline (Unicode-safe, non-destructive)."""
from app.services.text_pipeline import normalize_comment_text, strip_invisible


class TestEntityHandling:
    def test_html_entities_unescaped(self):
        assert normalize_comment_text("a &amp; b") == "a & b"
        assert normalize_comment_text("&lt;tag&gt;") == "<tag>"

    def test_plain_ampersand_untouched(self):
        assert normalize_comment_text("cats & dogs") == "cats & dogs"


class TestInvisibleCharacters:
    def test_zero_width_space_removed(self):
        assert normalize_comment_text("hi\u200bthere") == "hithere"

    def test_bom_and_soft_hyphen_removed(self):
        assert normalize_comment_text("\ufeffhello") == "hello"
        assert normalize_comment_text("co\u00adoperate") == "cooperate"

    def test_directional_marks_removed(self):
        assert strip_invisible("a\u200eb\u200fc") == "abc"

    def test_emoji_zero_width_joiner_sequence_preserved(self):
        # Family emoji depends on ZWJ - it must survive cleaning.
        family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"
        assert normalize_comment_text(f"{family} so cute") == f"{family} so cute"

    def test_variation_selector_preserved(self):
        heart = "\u2764\uFE0F"
        assert normalize_comment_text(f"{heart} it") == f"{heart} it"


class TestSentimentSignalPreserved:
    def test_emojis_punctuation_hashtags_mentions_kept(self):
        text = "OMG \U0001F602\U0001F602 amazing!!! ??? #tag @user loved it"
        assert normalize_comment_text(text) == text

    def test_accented_characters_kept(self):
        text = "C'est très génial!"
        assert normalize_comment_text(text) == text

    def test_devanagari_and_kannada_kept(self):
        hindi = "यह वीडियो बहुत अच्छा है"
        kannada = "ಈ ವಿಡಿಯೋ ತುಂಬಾ ಚೆನ್ನಾಗಿದೆ"
        assert normalize_comment_text(hindi) == hindi
        assert normalize_comment_text(kannada) == kannada


class TestUnicodeAndWhitespace:
    def test_whitespace_collapse(self):
        assert normalize_comment_text("  a \n\n  b \t c  ") == "a b c"

    def test_nfc_composition(self):
        decomposed = "cafe\u0301"  # e + combining acute
        assert normalize_comment_text(decomposed) == "caf\u00e9"  # precomposed

    def test_non_breaking_space_collapsed(self):
        assert normalize_comment_text("a\u00a0b") == "a b"

    def test_empty_input_returns_empty(self):
        assert normalize_comment_text("") == ""

    def test_purely_invisible_input_returns_empty(self):
        # Callers reject empty results - we never invent replacement text.
        assert normalize_comment_text("\u200e\u200e") == ""

    def test_raw_input_never_mutated(self):
        raw = "  spaced &amp; text "
        snapshot = raw[:]
        normalize_comment_text(raw)
        assert raw == snapshot
