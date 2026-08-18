"""
Unit tests for the internal call-transfer phrase matcher.

No network, no sockets: normalize_arabic/matches_transfer_phrase are pure
functions over strings, which is why they were split out into their own
module (transfer_trigger.py) rather than living inline in call_session.py --
same rationale as OutboundResampler being lifted out of RtpAudioInterface.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transfer_trigger import matches_transfer_phrase, normalize_arabic  # noqa: E402

PHRASE = "هيتم تحويل المكالمة دلوقتي"


class TestExactAndVariantMatches:
    def test_exact_sentence(self):
        assert matches_transfer_phrase(PHRASE, (PHRASE,))

    def test_with_tashkeel(self):
        text = "هَيْتِمّ تحويل المكالمة دلوقتي"
        assert matches_transfer_phrase(text, (PHRASE,))

    def test_with_tatweel(self):
        text = "هيتم تحويل المكالمـــة دلوقتي"
        assert matches_transfer_phrase(text, (PHRASE,))

    def test_with_alef_variant_at_sentence_start(self):
        # A caller-facing rewording that opens with an alef-hamza variant
        # elsewhere in the sentence must still normalise consistently, even
        # though the trigger phrase itself has no leading alef.
        text = "أوكي، " + PHRASE
        assert matches_transfer_phrase(text, (PHRASE,))

    def test_taa_marbuta_to_haa(self):
        # المكالمة (ends taa marbuta) must match even if the source text
        # used ه instead of ة for that letter.
        text = PHRASE.replace("المكالمة", "المكالمه")
        assert matches_transfer_phrase(text, (PHRASE,))

    def test_embedded_with_trailing_period(self):
        text = f"تمام، {PHRASE}."
        assert matches_transfer_phrase(text, (PHRASE,))

    def test_embedded_with_trailing_question_mark(self):
        text = f"{PHRASE}؟"
        assert matches_transfer_phrase(text, (PHRASE,))

    def test_embedded_with_trailing_comma(self):
        text = f"{PHRASE}،"
        assert matches_transfer_phrase(text, (PHRASE,))

    def test_embedded_in_longer_sentence(self):
        text = f"لحظة معايا، {PHRASE}، شكرا لصبرك."
        assert matches_transfer_phrase(text, (PHRASE,))


class TestNearMissesDoNotMatch:
    def test_truncated_phrase_alone(self):
        assert not matches_transfer_phrase("هيتم تحويل المكالمة", (PHRASE,))

    def test_word_transfer_only(self):
        assert not matches_transfer_phrase("في تحويل هيحصل قريب", (PHRASE,))

    def test_unrelated_sentence(self):
        assert not matches_transfer_phrase("معاك سارة من فريق التحصيل", (PHRASE,))

    def test_empty_text(self):
        assert not matches_transfer_phrase("", (PHRASE,))


class TestExtraPhrasesFromConfig:
    def test_extra_phrase_is_honoured(self):
        extra = "تم الربط بالفريق"
        assert matches_transfer_phrase(extra, (PHRASE, extra))
        assert matches_transfer_phrase(f"لحظة، {extra} حالا", (PHRASE, extra))

    def test_only_matches_configured_phrases(self):
        assert not matches_transfer_phrase("جملة عشوائية تماما", (PHRASE, "تم الربط بالفريق"))

    def test_blank_extra_phrase_does_not_match_everything(self):
        """Regression guard: TRANSFER_TRIGGER_EXTRA_PHRASES="" splits on ","
        into [""], and "" in anything is True in plain Python -- an
        unfiltered blank phrase here would transfer on the agent's very
        first word. Config filters blanks before this is ever called
        (config.py's __post_init__), but matches_transfer_phrase itself
        must also refuse to treat a blank phrase as a match, defense in
        depth."""
        assert not matches_transfer_phrase("أي كلام عادي تماما", ("",))
        assert not matches_transfer_phrase("أي كلام عادي تماما", (PHRASE, ""))

    def test_empty_phrase_tuple(self):
        assert not matches_transfer_phrase("أي كلام", ())


class TestNormalizeArabic:
    def test_empty_string(self):
        assert normalize_arabic("") == ""

    def test_digits_normalised(self):
        assert normalize_arabic("١٢٣") == "123"

    def test_whitespace_collapsed(self):
        assert normalize_arabic("هيتم    تحويل") == "هيتم تحويل"

    def test_idempotent(self):
        once = normalize_arabic(PHRASE)
        twice = normalize_arabic(once)
        assert once == twice
