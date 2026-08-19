"""
Arabic phrase normalisation + matching for the internal call-transfer trigger.

Pure, socket-free, session-free -- the same reason OutboundResampler was
lifted out of RtpAudioInterface (see audio_bridge.py's docstring): this runs
on the ElevenLabs websocket receive thread inside CallSession.
on_agent_response for every single agent utterance on every live call, so it
must be cheap and it must be unit-testable without constructing a
CallSession.

WHY THIS IS MORE THAN A SUBSTRING TEST
-------------------------------------
Firing this trigger transfers a live customer to a human and ends the AI
leg. It is irreversible, so an utterance that merely CONTAINS the trigger
sentence is not enough -- the agent has to be ASSERTING it. Four things
defeat a plain substring test, each of them observed:

  1. Negation      "مش هيتم تحويل المكالمة دلوقتي"      (will NOT be)
  2. Clause scope  "مش مشكلة، هيتم تحويل المكالمة دلوقتي" (the مش negates
                   "مشكلة" in its own clause -- this one SHOULD fire)
  3. Absorption    "مش بس هيتم تحويل المكالمة دلوقتي"   ("not only" -- the
                   negator lands on بس, so this SHOULD fire)
  4. Interrogative "هيتم تحويل المكالمة دلوقتي ولا لأ؟"  (asking, not telling)

So matching is: find the phrase, then decide whether the clause it sits in
asserts it. The rules are in _is_negated / _is_question, each independently
tested in tests/test_transfer_trigger.py.

KNOWN CEILING -- READ BEFORE ADDING A RULE
------------------------------------------
This is a heuristic over free LLM prose and it will keep meeting phrasings
it gets wrong; every rule below was added because a real counter-example
was found, and the next counter-example is a matter of time. The robust fix
is agent-side: have the agent emit a marker it would never produce
conversationally (a rare sentinel token, or the transfer_call client tool)
and match on that instead of inferring intent from prose. That is an
ElevenLabs dashboard change and it retires this entire module's cleverness.
See TRANSFER_FEATURE.md.
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from typing import NamedTuple

# Tashkeel / harakat + Quranic annotation marks.
#
# WRITTEN AS \u ESCAPES ON PURPOSE -- do not "simplify" it back to literal
# Arabic. As literals the class members render right-to-left, so U+065F and
# U+0670 appear in an order that has nothing to do with the order they are
# stored in, and retyping the line silently produced [ً-ٰ...]:
# a range that swallows U+0660-U+0669, the Arabic-Indic digits. The digits
# then vanished before _ARABIC_INDIC_DIGITS could convert them.
# tests/test_transfer_trigger.py::test_digits_normalised is the guard.
_TASHKEEL_RE = re.compile("[\u064B-\u065F\u0670\u06D6-\u06ED]")

_TATWEEL = "ـ"

_ALEF_VARIANTS = str.maketrans(
    {
        "أ": "ا",  # أ -> ا
        "إ": "ا",  # إ -> ا
        "آ": "ا",  # آ -> ا
        "ٱ": "ا",  # ٱ -> ا
        "ى": "ي",  # ى -> ي
        "ة": "ه",  # ة -> ه
        "ؤ": "و",  # ؤ -> و
        "ئ": "ي",  # ئ -> ي
    }
)

_ARABIC_INDIC_DIGITS = str.maketrans(
    {
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
    }
)

# Marks that END A CLAUSE. Kept apart from the rest of the punctuation
# because negation does not reach across one (rule 2 above).
_CLAUSE_BOUNDARY_RE = re.compile(r"[،؛.,!…:;]")

# Question marks end a clause AND mark it interrogative (rule 4 above), so
# they are substituted separately and carry their own sentinel.
_QUESTION_RE = re.compile(r"[؟?]")

# Punctuation that carries no clause meaning: replaced with a SPACE, not
# deleted. Deleting merged the tokens either side of an unspaced mark, so
# "المكالمة،دلوقتي" became one word and stopped matching a phrase that was
# plainly spoken. Tatweel is absent here on purpose: it is stripped outright
# a step earlier, and turning it into a space would split words instead.
_PUNCTUATION_RE = re.compile(r"[\"'\-_()\[\]{}«»]")

# Sentinels standing in for clause boundaries between the normalisation and
# tokenisation steps. Neither can occur in real transcript text.
_CLAUSE_SENTINEL = chr(0)
_QUESTION_SENTINEL = chr(1)

# Negation particles. Deliberately short and unambiguous: an over-broad list
# (e.g. bare "لا", extremely common and usually negating nothing) would
# silently suppress legitimate transfers, and a suppressed transfer is its
# own failure -- the agent has already told the caller they are being
# transferred.
#
# Vocalised forms ("مِش") need no entry: tashkeel is stripped before matching.
# Conjunctions that attach orthographically ("ومش", "فمش") are handled by
# _is_negator rather than by listing every combination.
#
# Bare "ما" is deliberately ABSENT -- it is far too common as a relative
# pronoun. It is recognised only in the fixed "عمر... ما" ("never")
# construction; see _is_never_construction.
_NEGATORS = frozenset({"مش", "لن", "ليس", "لست", "مافيش", "مفيش"})

# Words that ABSORB a preceding negator, i.e. the negation lands on them and
# never reaches the verb: "مش بس" (not only), "مفيش مشكلة" (no problem),
# "مفيش مانع" (no objection). Without this, the two commonest ways an agent
# introduces a transfer were read as refusals to perform it.
_ABSORBERS = frozenset({"بس", "مشكله", "مانع"})

# Complementizers ("that"). A negator separated from the phrase by one of
# these is negating a predicate whose COMPLEMENT is the phrase, so it does
# take scope however far back it sits: "مش المفروض إنه هيتم تحويل المكالمة
# دلوقتي". Normalisation has already folded إ/أ to ا.
_COMPLEMENTIZERS = frozenset({"ان", "انه", "انها", "انهم", "انك", "انكم", "اننا"})

# "عمر" + "ما" is the fixed Egyptian "never" construction ("عمرها ما هيتم").
_NEVER_HEAD_PREFIX = "عمر"

# How many words before the match to inspect for an ADJACENT negator. Two
# covers the real phrasings ("مش هيتم..." and "مش دايما هيتم...") without
# reaching so far back that unrelated material starts suppressing transfers.
# Negation that sits further back is handled by the complementizer rule, not
# by widening this -- widening it broke "مش عارف أساعدك أكتر من كده هيتم
# تحويل المكالمة دلوقتي", which must fire.
_NEGATION_LOOKBEHIND_WORDS = 2


class _Parsed(NamedTuple):
    """Normalised text in the several shapes the matcher needs at once."""
    words: list[str]
    boundary_before: list[bool]   # a clause boundary sits before words[i]
    clause_of_word: list[int]     # which clause words[i] belongs to
    question_clauses: frozenset[int]


def _is_negator(word: str) -> bool:
    """True for a negation particle, with or without an attached و/ف."""
    if word in _NEGATORS:
        return True
    return word[:1] in ("و", "ف") and word[1:] in _NEGATORS


def _normalize_parts(text: str) -> _Parsed:
    """Normalise to a word list plus the clause structure around it.

    Several parallel outputs rather than one string because the consumers
    want opposite things: phrase matching needs punctuation GONE (so an
    unspaced comma doesn't split a phrase in half), while the scope rules
    need to know a boundary was there and whether it was a question mark.
    Carrying boundaries as sentinel tokens and stripping them during
    tokenisation satisfies both."""
    if not text:
        return _Parsed([], [], [], frozenset())

    text = unicodedata.normalize("NFC", text)
    text = _TASHKEEL_RE.sub("", text)
    text = text.replace(_TATWEEL, "")
    text = text.translate(_ALEF_VARIANTS)
    text = text.translate(_ARABIC_INDIC_DIGITS)
    text = _QUESTION_RE.sub(f" {_QUESTION_SENTINEL} ", text)
    text = _CLAUSE_BOUNDARY_RE.sub(f" {_CLAUSE_SENTINEL} ", text)
    text = _PUNCTUATION_RE.sub(" ", text)

    words: list[str] = []
    boundary_before: list[bool] = []
    clause_of_word: list[int] = []
    question_clauses: set[int] = set()
    clause = 0
    pending = False
    for token in text.split():
        if token in (_CLAUSE_SENTINEL, _QUESTION_SENTINEL):
            if token == _QUESTION_SENTINEL:
                # The mark ends the clause it terminates, not the next one.
                question_clauses.add(clause)
            pending = True
            clause += 1
            continue
        words.append(token.casefold())
        boundary_before.append(pending)
        clause_of_word.append(clause)
        pending = False
    return _Parsed(words, boundary_before, clause_of_word, frozenset(question_clauses))


def _is_never_construction(parsed: _Parsed, index: int) -> bool:
    """True if words[index] is the "ما" of a "عمر... ما" ("never") pair."""
    if parsed.words[index] != "ما" or index == 0:
        return False
    if parsed.clause_of_word[index - 1] != parsed.clause_of_word[index]:
        return False
    return parsed.words[index - 1].startswith(_NEVER_HEAD_PREFIX)


def _absorbed(parsed: _Parsed, negator_index: int, match_index: int) -> bool:
    """True if the word directly after the negator absorbs it, so the
    negation never reaches the phrase ("مش بس ...", "مفيش مشكلة ...").

    The absorber must lie strictly BETWEEN the negator and the phrase --
    otherwise "مش هيتم..." would count its own phrase as the absorbed word."""
    following = negator_index + 1
    return following < match_index and parsed.words[following] in _ABSORBERS


def _is_negated(parsed: _Parsed, match_index: int) -> bool:
    """True if a negation takes scope over the phrase starting at
    `match_index`, by either of the two routes that occur in practice."""
    clause = parsed.clause_of_word[match_index]

    # Route 1 -- adjacent: a negator within the lookbehind window, in the
    # same clause, not absorbed by the word it directly negates.
    for offset in range(1, _NEGATION_LOOKBEHIND_WORDS + 1):
        j = match_index - offset
        if j < 0 or parsed.clause_of_word[j] != clause:
            break
        if _is_never_construction(parsed, j):
            return True
        if _is_negator(parsed.words[j]) and not _absorbed(parsed, j, match_index):
            return True

    # Route 2 -- complement: the phrase is the complement of a negated
    # predicate ("مش المفروض إنه <phrase>"). Distance stops mattering once a
    # complementizer links the two, so scan the whole clause -- but ONLY
    # when such a link is actually present, which is what keeps "مش عارف
    # أساعدك أكتر من كده <phrase>" firing.
    linked = any(
        parsed.words[j] in _COMPLEMENTIZERS
        for j in range(match_index - 1, -1, -1)
        if parsed.clause_of_word[j] == clause
    )
    if linked:
        for j in range(match_index - 1, -1, -1):
            if parsed.clause_of_word[j] != clause:
                break
            if _is_negator(parsed.words[j]):
                return True
    return False


def _is_question(parsed: _Parsed, match_index: int) -> bool:
    """True if the phrase sits in an interrogative clause -- the agent is
    asking whether to transfer, not announcing that it is transferring.

    Scoped to the phrase's OWN clause, so an announcement followed by a tag
    question ("هيتم تحويل المكالمة دلوقتي. تمام؟") still fires."""
    clause = parsed.clause_of_word[match_index]
    if clause in parsed.question_clauses:
        return True
    # Alternative-question tag with the mark dropped, which STT does:
    # "... ولا لأ" ("... or not"). لأ has already normalised to لا.
    for j in range(match_index, len(parsed.words) - 1):
        if parsed.clause_of_word[j] != clause:
            break
        if parsed.words[j] == "ولا" and parsed.words[j + 1] == "لا":
            return True
    return False


def normalize_arabic(text: str) -> str:
    """Normalise Arabic text for loose substring matching.

    Thin wrapper over _normalize_parts, which does the actual work and also
    reports clause structure. Public because config.Settings normalises the
    configured phrases once at load time, and both sides of a comparison
    must go through the same normalisation.
    """
    return " ".join(_normalize_parts(text).words)


def matches_transfer_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    """True if `text` ASSERTS any phrase in `phrases` -- see the module
    docstring for why that is not the same as containing it.

    `phrases` is expected to already be normalised (Settings does this once
    at load time -- see config.py's transfer_trigger_phrases_normalized) but
    normalising here too is idempotent and cheap, so a caller passing raw
    phrases still gets correct behaviour.

    An empty/blank phrase never matches -- without this guard, a blank entry
    (e.g. from a trailing comma in TRANSFER_TRIGGER_EXTRA_PHRASES) would be
    an empty string, and "" is a substring of everything, silently
    transferring on the agent's first word.

    Every occurrence is checked, not just the first: a sentence that negates
    the phrase once and then states it positively ("مش هيتم تحويل المكالمة
    دلوقتي، بس هيتم تحويل المكالمة دلوقتي") must still transfer.
    """
    if not phrases:
        return False
    parsed = _normalize_parts(text)
    if not parsed.words:
        return False

    normalized_text = " ".join(parsed.words)
    # Character offset at which each word begins in normalized_text, so a
    # match position can be mapped back to a word index for the scope rules.
    word_starts: list[int] = []
    offset = 0
    for word in parsed.words:
        word_starts.append(offset)
        offset += len(word) + 1

    for phrase in phrases:
        normalized_phrase = normalize_arabic(phrase)
        if not normalized_phrase:
            continue
        at = normalized_text.find(normalized_phrase)
        while at != -1:
            # bisect, not an exact lookup: a phrase can start mid-word
            # (substring semantics, deliberately unchanged), and the word
            # CONTAINING the match is the right place to apply scope from.
            index = bisect.bisect_right(word_starts, at) - 1
            if not _is_negated(parsed, index) and not _is_question(parsed, index):
                return True
            at = normalized_text.find(normalized_phrase, at + 1)
    return False
