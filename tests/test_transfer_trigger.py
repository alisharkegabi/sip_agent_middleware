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

    def test_trailing_question_mark_makes_it_a_QUESTION_not_an_announcement(self):
        """SEMANTICS CHANGED DELIBERATELY. This case used to assert a match,
        on the reasoning that trailing punctuation shouldn't defeat the
        matcher. But "هيتم تحويل المكالمة دلوقتي؟" is the agent ASKING
        whether to transfer -- plausibly checking with the customer first --
        and transferring them on that question is exactly the "drops a live
        customer" failure the matcher exists to avoid.

        The original intent (trailing punctuation is not a barrier) is still
        covered by the trailing-period and trailing-comma cases above."""
        assert not matches_transfer_phrase(f"{PHRASE}؟", (PHRASE,))

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


class TestNegatedPhrasesDoNotTrigger:
    """A wrongly-triggered transfer drops a real customer mid-conversation,
    so a sentence that says the phrase in order to DENY it must not fire.
    Plain substring matching could not tell the two apart."""

    def test_mish_directly_before_the_phrase(self):
        assert not matches_transfer_phrase(f"مش {PHRASE}", (PHRASE,))

    def test_mish_with_attached_conjunction(self):
        assert not matches_transfer_phrase(f"ومش {PHRASE}", (PHRASE,))
        assert not matches_transfer_phrase(f"فمش {PHRASE}", (PHRASE,))

    def test_lan_negation(self):
        assert not matches_transfer_phrase(f"لن {PHRASE}", (PHRASE,))

    def test_negation_two_words_back(self):
        assert not matches_transfer_phrase(f"مش دايما {PHRASE}", (PHRASE,))

    def test_negation_inside_a_longer_sentence(self):
        text = f"للأسف مش {PHRASE}، محتاجين نراجع الحساب الأول."
        assert not matches_transfer_phrase(text, (PHRASE,))

    def test_negation_survives_tashkeel(self):
        assert not matches_transfer_phrase(f"مِش {PHRASE}", (PHRASE,))


class TestNegationDoesNotSuppressRealTransfers:
    """The other half of the trade-off: a suppressed transfer is its own
    failure, because the agent has already announced it to the caller."""

    def test_positive_occurrence_after_a_negated_one_still_fires(self):
        text = f"مش {PHRASE}... لا، اعتذر، {PHRASE}"
        assert matches_transfer_phrase(text, (PHRASE,))

    def test_negator_far_earlier_in_the_sentence_does_not_suppress(self):
        text = f"مش هينفع نكمل كده وانا بعتذر لحضرتك جدا، {PHRASE}"
        assert matches_transfer_phrase(text, (PHRASE,))

    def test_plain_phrase_still_matches(self):
        assert matches_transfer_phrase(PHRASE, (PHRASE,))

    def test_common_words_are_not_treated_as_negators(self):
        # "لا" and "ما" are deliberately NOT in the negator list -- they are
        # far too common in ordinary speech to suppress a transfer on.
        assert matches_transfer_phrase(f"لا مشكلة، {PHRASE}", (PHRASE,))


class TestPunctuationSeparatesWords:
    def test_unspaced_comma_does_not_merge_tokens(self):
        """Punctuation used to be deleted rather than replaced with a space,
        so an unspaced mark fused the words either side and a plainly
        spoken phrase stopped matching."""
        assert matches_transfer_phrase("هيتم تحويل المكالمة،دلوقتي", (PHRASE,))

    def test_normalize_replaces_punctuation_with_a_space(self):
        assert normalize_arabic("المكالمة،دلوقتي") == "المكالمه دلوقتي"


class TestNegationDoesNotCrossAClauseBoundary:
    """A negator on the far side of a comma is negating its own clause, not
    the trigger sentence. Without this, "مش مشكلة، {PHRASE}" ("no problem,
    the call will be transferred now") -- an entirely ordinary thing for a
    collections agent to say -- suppressed a legitimate transfer."""

    def test_mish_mushkila_then_the_phrase_still_transfers(self):
        assert matches_transfer_phrase(f"مش مشكلة، {PHRASE}", (PHRASE,))

    def test_negated_clause_then_the_phrase_still_transfers(self):
        assert matches_transfer_phrase(f"مش هينفع نكمل كده، {PHRASE}", (PHRASE,))

    def test_full_stop_is_a_boundary_too(self):
        assert matches_transfer_phrase(f"مش مشكلة. {PHRASE}", (PHRASE,))

    def test_negation_in_the_same_clause_still_suppresses(self):
        """The boundary rule must not become a way around the negation
        check: with no boundary in between, it still applies."""
        assert not matches_transfer_phrase(f"مش {PHRASE}", (PHRASE,))
        assert not matches_transfer_phrase(f"مش دايما {PHRASE}", (PHRASE,))

    def test_boundary_before_the_negator_does_not_rescue_it(self):
        # The comma is before "مش", not between "مش" and the phrase.
        assert not matches_transfer_phrase(f"لأ، مش {PHRASE}", (PHRASE,))


class TestScopeRulesFromReviewCounterExamples:
    """The five utterances a review produced as counter-examples to the
    plain negation lookbehind. Each needed a different rule; notably the
    first two require a lookbehind NARROWER than two words while the third
    requires a WIDER one, which is why distance alone cannot settle it."""

    def test_not_only_is_an_assertion(self):
        """مش بس = 'not only'. The negator lands on بس, never on the verb."""
        assert matches_transfer_phrase(f"مش بس {PHRASE}", (PHRASE,))

    def test_no_problem_without_a_comma_is_an_assertion(self):
        """مفيش مشكلة = 'no problem'. STT routinely drops the comma, so the
        clause-boundary rule alone does not save this one."""
        assert matches_transfer_phrase(f"مفيش مشكلة {PHRASE}", (PHRASE,))

    def test_negated_predicate_with_complementizer_suppresses(self):
        """مش المفروض إنه ... = 'it is not supposed to be that ...'. The
        phrase is the complement of the negated predicate, so the negator
        takes scope from three words back."""
        assert not matches_transfer_phrase(f"مش المفروض إنه {PHRASE}", (PHRASE,))

    def test_never_construction_suppresses(self):
        """عمرها ما = 'it will never'. Bare ما is not a negator on its own
        (far too common as a relative pronoun); only this fixed pair is."""
        assert not matches_transfer_phrase(f"عمرها ما {PHRASE}", (PHRASE,))

    def test_alternative_question_suppresses(self):
        assert not matches_transfer_phrase(f"{PHRASE} ولا لأ؟", (PHRASE,))

    def test_alternative_question_suppresses_without_the_mark(self):
        """STT drops the question mark; the ولا لأ tag still marks it."""
        assert not matches_transfer_phrase(f"{PHRASE} ولا لأ", (PHRASE,))


class TestScopeRulesDoNotOverreach:
    """Each rule above is a licence to suppress a transfer, so each needs a
    matching guard that it does not suppress an ordinary announcement."""

    def test_distant_negator_without_a_complementizer_still_fires(self):
        """The complementizer rule scans the whole clause, so this is the
        case that would break if it scanned unconditionally."""
        text = f"مش عارف أساعدك أكتر من كده {PHRASE}"
        assert matches_transfer_phrase(text, (PHRASE,))

    def test_absorber_does_not_excuse_an_adjacent_negator(self):
        assert not matches_transfer_phrase(f"مش {PHRASE}", (PHRASE,))

    def test_announcement_followed_by_a_tag_question_still_fires(self):
        """The question rule is scoped to the phrase's own clause."""
        assert matches_transfer_phrase(f"{PHRASE}. تمام؟", (PHRASE,))

    def test_question_mark_in_an_earlier_clause_does_not_suppress(self):
        assert matches_transfer_phrase(f"تمام؟ {PHRASE}", (PHRASE,))


class TestTashkeelClassDoesNotEatDigits:
    def test_arabic_indic_digits_survive_tashkeel_stripping(self):
        """REGRESSION: the tashkeel character class is written as escaped
        codepoints because as literals it renders right-to-left, and
        retyping it silently reordered U+065F and U+0670 into a range that
        swallowed U+0660-U+0669 -- the Arabic-Indic digits -- before
        _ARABIC_INDIC_DIGITS could convert them."""
        assert normalize_arabic("١٢٣") == "123"
        assert normalize_arabic("٥٠٠ جنيه") == "500 جنيه"

    def test_tashkeel_is_still_stripped(self):
        assert normalize_arabic("هَيتم") == "هيتم"
