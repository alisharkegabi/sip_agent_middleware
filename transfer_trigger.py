"""
Arabic phrase normalisation + matching for the internal call-transfer trigger.

Pure, socket-free, session-free -- the same reason OutboundResampler was
lifted out of RtpAudioInterface (see audio_bridge.py's docstring): this runs
on the ElevenLabs websocket receive thread inside CallSession.
on_agent_response for every single agent utterance on every live call, so it
must be cheap and it must be unit-testable without constructing a
CallSession.
"""
from __future__ import annotations

import re
import unicodedata

# Tashkeel / harakat + Quranic annotation marks.
_TASHKEEL_RE = re.compile(r"[ً-ٰٟۖ-ۭ]")

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

# ASCII + Arabic punctuation, stripped entirely (not replaced with a space --
# see normalize_arabic's whitespace-collapse step, which handles the case
# where stripping a punctuation mark leaves adjacent words needing a
# separator only if one was already there).
_PUNCTUATION_RE = re.compile(
    r"[،؛؟؟،؛.,!?…\"'\-_ـ:;()\[\]{}«»]"
)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_arabic(text: str) -> str:
    """Normalise Arabic text for loose substring matching.

    Order matters: tashkeel/tatweel/alef-forms are structural normalisation
    that must happen before punctuation stripping (punctuation stripping
    could otherwise merge/garble diacritic sequences), and whitespace
    collapse must be last since every prior step can leave gaps.
    """
    if not text:
        return ""

    text = unicodedata.normalize("NFC", text)
    text = _TASHKEEL_RE.sub("", text)
    text = text.replace(_TATWEEL, "")
    text = text.translate(_ALEF_VARIANTS)
    text = text.translate(_ARABIC_INDIC_DIGITS)
    text = _PUNCTUATION_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.casefold()


def matches_transfer_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    """True if any (already- or not-yet-normalised) phrase in `phrases`
    normalises to a substring of `text`'s normalised form.

    `phrases` is expected to already be normalised (Settings does this once
    at load time -- see config.py's transfer_trigger_phrases_normalized) but
    normalising here too is idempotent and cheap, so a caller passing raw
    phrases still gets correct behaviour.

    An empty/blank phrase never matches -- without this guard, a blank entry
    (e.g. from a trailing comma in TRANSFER_TRIGGER_EXTRA_PHRASES) would be
    an empty string, and "" is a substring of everything, silently
    transferring on the agent's first word.
    """
    if not phrases:
        return False
    normalized_text = normalize_arabic(text)
    if not normalized_text:
        return False
    for phrase in phrases:
        normalized_phrase = normalize_arabic(phrase)
        if normalized_phrase and normalized_phrase in normalized_text:
            return True
    return False
