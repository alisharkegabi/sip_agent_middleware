"""
Arabic name orthography normalization for TTS.

The problem: customer names arrive from the .NET client in degraded Arabic
orthography and are spoken aloud by the ElevenLabs agent. The damaging case is
the dotless ya -- the name `علي` (Ali) stored as `على`, which is also the
preposition "on". TTS reads the preposition, and the caller hears the wrong
word entirely. The same defect hits every nisba name and surname in the book
(`فهمي`, `حلمي`, `شوقي`, `حجازي`, `الدسوقي`, ...), which is the bulk of what
this repairs.

Design, in short:

  * ONE rule -- a token ending in `ى` gets `ي` -- guarded by a curated list of
    names that legitimately end in `ى` (`مصطفى`, `هدى`, `ليلى`, ...).
  * Everything else is an exact lookup table. Hamza restoration cannot be a
    rule (bare `ا` maps to `ا`/`أ`/`إ`/`آ` depending on the word), and neither
    can `ه → ة` (it would corrupt `طه → طة`, `عبده → عبدة`, `الله → اللة`).
    Those names are protected by simply not appearing in the table.

Data lives in arabic_names.json and is loaded and validated once at import --
never mid-call. See Claude_files/NAME_NORMALIZATION_DESIGN.md.

A second stage, `pronunciation`, runs after the spelling is settled. Correct
orthography is not always correct speech: `خلاف` (Khallaf) is spelled exactly
right and the voice still reads it as the word خِلاف, so TTS is handed `خَلَف`
instead. Stage-2 output is a deliberate misspelling and only ElevenLabs ever
sees it -- `dynamic_variables` keeps what the client sent. Every alias is
chosen by listening to the production voice, never by theory; the probe rig is
in Claude_files/tts_probe/.

This module never logs a name. Names are borrower data; only rule names and
counts leave here.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
import unicodedata
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

DATA_FILE = pathlib.Path(__file__).resolve().parent / "arabic_names.json"

ALEF_MAKSURA = "ى"  # ى
YA = "ي"            # ي

# Stripped from the *match key* only -- the emitted token is always built from
# the original text, so tatweel and any diacritics the source supplied survive
# into what the agent says.
_TATWEEL = "ـ"
_HARAKAT = {chr(c) for c in range(0x064B, 0x0653)} | {"ٰ"}
_STRIP_FOR_MATCH = _HARAKAT | {_TATWEEL}

# Leading/trailing only. An interior hyphen (`عبد-الله`) is deliberately left
# alone -- no evidence it occurs in a banking name field, and splitting on it
# would be a guess.
_EDGE_PUNCT = " \t\r\n.,،؛;:!?؟()[]{}<>\"'«»/\\|*#_~`^"

_AL = "ال"  # ال

# Rule names used as counter keys. Kept as constants so the log strings and the
# tests cannot drift apart.
RULE_OVERRIDE = "override"
RULE_DOTLESS_YA = "dotless_ya"
RULE_GENDER_PROTECTED = "gender_protected"
RULE_PRONUNCIATION = "pronunciation"
RULE_PRONUNCIATION_FEMALE = "pronunciation_female"
RULE_ERROR = "error"

FEMALE = "female"
MALE = "male"
_MALE_WORDS = {"male", "m", "1", "ذكر", "مذكر"}
_FEMALE_WORDS = {"female", "f", "2", "أنثى", "انثى", "مؤنث", "مونث"}


def parse_gender(raw: Any) -> Optional[str]:
    """Returns FEMALE, MALE, or None for absent/unrecognised.

    The payload mixes cases (`cr_gender` is "male", `br_gender` is "Male") and
    sends "" for an absent guarantor, so this is deliberately forgiving. Arabic
    and single-letter forms are accepted too -- if the source system ever
    changes vocabulary, an unrecognised value degrades to None, which protects
    the name rather than rewriting it.
    """
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if v in _MALE_WORDS:
        return MALE
    if v in _FEMALE_WORDS:
        return FEMALE
    return None


class NameDataError(Exception):
    """arabic_names.json is missing, malformed, or internally inconsistent."""


def _validate(
    data: Any,
) -> tuple[dict[str, str], frozenset[str], frozenset[str], dict[str, str]]:
    if not isinstance(data, dict):
        raise NameDataError("top level is not an object")

    overrides = data.get("overrides")
    protected = data.get("protected_final_ya")
    if_female = data.get("protected_if_female", [])
    pronunciation = data.get("pronunciation", {})
    pron_if_female = data.get("pronunciation_if_female", {})
    if not isinstance(overrides, dict):
        raise NameDataError("`overrides` missing or not an object")

    if not isinstance(protected, list):
        raise NameDataError("`protected_final_ya` missing or not a list")
    if not isinstance(if_female, list):
        raise NameDataError("`protected_if_female` must be a list")

    for k, v in overrides.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise NameDataError("`overrides` must map string to string")
        if k == v:
            raise NameDataError(f"override maps a token to itself: {k!r}")

    def _ya_final_set(items: list, label: str) -> set[str]:
        out = set()
        for t in items:
            if not isinstance(t, str):
                raise NameDataError(f"`{label}` must contain only strings")
            if not t.endswith(ALEF_MAKSURA):
                # An entry that does not end in ى protects nothing -- the rule
                # would not have touched it anyway. Almost always a typo.
                raise NameDataError(f"{label} entry does not end in ى: {t!r}")
            out.add(t)
        return out

    protected_set = _ya_final_set(protected, "protected_final_ya")
    if_female_set = _ya_final_set(if_female, "protected_if_female")

    for label, s in (("protected_final_ya", protected_set), ("protected_if_female", if_female_set)):
        both = s & set(overrides)
        if both:
            raise NameDataError(f"token is both in {label} and an override key: {sorted(both)}")
    overlap = protected_set & if_female_set
    if overlap:
        # Unconditional protection would always win, so the gender entry is
        # dead data and the male reading silently stays broken.
        raise NameDataError(f"token is both unconditionally and conditionally protected: {sorted(overlap)}")

    def _pron_table(table: Any, label: str) -> dict[str, str]:
        if not isinstance(table, dict):
            raise NameDataError(f"`{label}` must be an object")
        for k, v in table.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise NameDataError(f"`{label}` must map string to string")
            if k == v:
                raise NameDataError(f"{label} maps a token to itself: {k!r}")
            # Lookups canonicalize, so a key carrying harakat or tatweel could
            # never be matched by anything. Always a typo.
            if set(k) & _STRIP_FOR_MATCH:
                raise NameDataError(f"{label} key carries harakat/tatweel: {k!r}")
            # Stage 1 would rewrite the token before stage 2 saw this key,
            # leaving the entry dead. Key it on the corrected spelling instead.
            if k in overrides:
                raise NameDataError(f"{label} key is also an override key: {k!r}")
        return dict(table)

    pron = _pron_table(pronunciation, "pronunciation")
    pron_f = _pron_table(pron_if_female, "pronunciation_if_female")
    # The unconditional table is consulted first, so a token in both would make
    # the gender-conditional entry unreachable.
    both = set(pron) & set(pron_f)
    if both:
        raise NameDataError(f"token is in both pronunciation tables: {sorted(both)}")

    return (
        dict(overrides),
        frozenset(protected_set),
        frozenset(if_female_set),
        pron,
        pron_f,
    )


def _load() -> tuple[
    dict[str, str], frozenset[str], frozenset[str], dict[str, str], dict[str, str],
    Optional[str]
]:
    """Fail open. A broken data file disables normalization and is logged
    loudly; it must never stop the service from placing calls. This is a
    pronunciation enhancement, not a safety control."""
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        overrides, protected, if_female, pronunciation, pron_female = _validate(raw)
        logger.info(
            f"name normalization data loaded: {len(protected)} protected, "
            f"{len(if_female)} gender-conditional, {len(overrides)} overrides, "
            f"{len(pronunciation)} pronunciation, "
            f"{len(pron_female)} female-conditional pronunciation"
        )
        return overrides, protected, if_female, pronunciation, pron_female, None
    except Exception as e:
        logger.error(
            f"name normalization DISABLED -- could not load {DATA_FILE.name}: {e}"
        )
        return {}, frozenset(), frozenset(), {}, {}, str(e)


(
    OVERRIDES,
    PROTECTED,
    PROTECTED_IF_FEMALE,
    PRONUNCIATION,
    PRONUNCIATION_IF_FEMALE,
    LOAD_ERROR,
) = _load()


def _canonical(core: str) -> str:
    """Match key for `core`. NFC first so that a decomposed `أ` (alef plus a
    combining hamza) matches a precomposed one.

    Deliberately does NOT fold ى→ي or ة→ه. Nearly every general-purpose Arabic
    normalizer does exactly that as a canonicalization step -- here it would
    erase the very distinction this module exists to restore.
    """
    core = unicodedata.normalize("NFC", core)
    return "".join(ch for ch in core if ch not in _STRIP_FOR_MATCH)


def _normalize_token(
    token: str, *, gender: Optional[str] = None, is_given_name: bool = False
) -> tuple[str, tuple[str, ...]]:
    """Returns (token, rules_fired). Punctuation on either edge is set aside
    and restored so that `على،` resolves the same as `على`.

    Two stages in order: `_spell` settles the orthography, then `_pronounce`
    respells the result for the voice. Both can fire on one token, which is why
    the return is a tuple and not a single rule.

    `is_given_name` marks the first token of the value. In an Arabic full name
    that is the person's own name and takes their gender; the tokens after it
    are the father's and grandfather's names, which are male. So `فاطمة يسرى`
    reads token 1 as Yosri (the father), not Yosra.
    """
    head_len = len(token) - len(token.lstrip(_EDGE_PUNCT))
    tail_start = len(token.rstrip(_EDGE_PUNCT))
    if head_len >= tail_start:  # punctuation only
        return token, ()
    head = token[:head_len]
    stripped = token[head_len:tail_start]
    tail = token[tail_start:]

    fixed, rule = _spell(stripped, gender=gender, is_given_name=is_given_name)
    spoken, prule = _pronounce(fixed, gender=gender, is_given_name=is_given_name)
    rules = tuple(r for r in (rule, prule) if r)
    return head + spoken + tail, rules


def _spell(
    stripped: str, *, gender: Optional[str], is_given_name: bool
) -> tuple[str, Optional[str]]:
    """Stage 1 -- orthography. What the name should have been stored as."""
    key = _canonical(stripped)

    # Order matters: an explicit override is the most specific statement of
    # intent, so it wins outright and the rule never runs on its result.
    if key in OVERRIDES:
        return OVERRIDES[key], RULE_OVERRIDE
    if key.startswith(_AL) and key[2:] in OVERRIDES:
        return _AL + OVERRIDES[key[2:]], RULE_OVERRIDE

    # Protection covers the bare name and its ال- form, so `نور الهدى` needs no
    # multi-token entry: `الهدى` resolves through `هدى`.
    if key in PROTECTED or (key.startswith(_AL) and key[2:] in PROTECTED):
        return stripped, None

    # Gender-conditional: `يسرى` is Yosra for a woman and a degraded Yosri for
    # a man. Only the given-name position consults gender, and only a positive
    # MALE reading unlocks the rule -- absent or unrecognised gender protects,
    # which is the behaviour from before gender was wired in.
    if _in_conditional(key) and is_given_name and gender != MALE:
        return stripped, RULE_GENDER_PROTECTED

    if stripped.endswith(ALEF_MAKSURA):
        return stripped[:-1] + YA, RULE_DOTLESS_YA

    return stripped, None


def _pronounce(
    text: str, *, gender: Optional[str] = None, is_given_name: bool = False
) -> tuple[str, Optional[str]]:
    """Stage 2 -- how the voice says it, not how the name is spelled.

    Runs on stage 1's output, so `نيره` and `نيرة` reach the same alias
    through one entry rather than needing one each.

    The female-conditional table is for skeletons that are two DIFFERENT names
    depending on the bearer: `ملك` is Malak for a woman and Malik for a man,
    so a flat alias would rename half of them. Same rule as the stage-1 gender
    logic -- only the given-name position consults gender, because the tokens
    after it are the father's and grandfather's names -- and only a positive
    FEMALE reading applies the alias. Absent or unrecognised gender leaves the
    name alone, which is the safe direction.
    """
    key = _canonical(text)
    hit = _lookup(key, PRONUNCIATION)
    if hit is not None:
        return hit, RULE_PRONUNCIATION
    if is_given_name and gender == FEMALE:
        hit = _lookup(key, PRONUNCIATION_IF_FEMALE)
        if hit is not None:
            return hit, RULE_PRONUNCIATION_FEMALE
    return text, None


def _lookup(key: str, table: dict[str, str]) -> Optional[str]:
    """The bare name or its ال- form."""
    if key in table:
        return table[key]
    if key.startswith(_AL) and key[2:] in table:
        return _AL + table[key[2:]]
    return None


def _in_conditional(key: str) -> bool:
    return key in PROTECTED_IF_FEMALE or (
        key.startswith(_AL) and key[2:] in PROTECTED_IF_FEMALE
    )


def normalize_name(value: Any, gender: Optional[str] = None) -> tuple[Any, dict[str, int]]:
    """Normalize one name value. Non-string and blank values pass through --
    `dynamic_variables` is typed `dict[str, Any]`, so a caller can legitimately
    send `None`, a number, or an empty string for an absent guarantor.

    `gender` is FEMALE/MALE/None (see parse_gender) and applies only to the
    first token.
    """
    counters: dict[str, int] = {}
    if not isinstance(value, str) or not value.strip():
        return value, counters

    # Splitting on runs of whitespace and keeping the separators lets the
    # original spacing survive reassembly.
    parts = re.split(r"(\s+)", value)
    out = []
    seen_word = False
    for part in parts:
        if part.isspace() or not part:
            out.append(part)
            continue
        new_part, rules = _normalize_token(
            part, gender=gender, is_given_name=not seen_word
        )
        seen_word = True
        out.append(new_part)
        for rule in rules:
            counters[rule] = counters.get(rule, 0) + 1
    return "".join(out), counters


def normalize_dynamic_variables(
    dynamic_variables: dict[str, Any],
    keys: Iterable[str],
    gender_keys: Optional[dict[str, str]] = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Return a corrected COPY plus per-rule counters.

    The input is never mutated: the caller keeps the raw payload byte-for-byte
    for the webhook, and only this copy reaches the voice agent. Only `keys`
    are inspected -- contract refs, dates, amounts and phone numbers are copied
    through untouched.

    `gender_keys` maps a name key to the dynamic variable holding that person's
    gender (`user_name` -> `br_gender`, `call_receiver` -> `cr_gender`, ...).
    A name key with no mapping, or whose gender value is blank or
    unrecognised, is normalized without gender.

    Any failure degrades to the raw payload. A bug in here must not fail a call.
    """
    counters: dict[str, int] = {}
    if LOAD_ERROR is not None:
        return dict(dynamic_variables), counters

    gender_keys = gender_keys or {}
    try:
        out = dict(dynamic_variables)
        for key in keys:
            if key not in out:
                continue
            gender = parse_gender(dynamic_variables.get(gender_keys.get(key, "")))
            new_value, c = normalize_name(out[key], gender)
            out[key] = new_value
            for rule, n in c.items():
                counters[rule] = counters.get(rule, 0) + n
        return out, counters
    except Exception:
        logger.exception("name normalization failed; using the raw payload")
        return dict(dynamic_variables), {RULE_ERROR: 1}


def format_counters(counters: dict[str, int]) -> str:
    """Log-safe summary. Rule names and counts only -- never a name."""
    return " ".join(f"{k}={v}" for k, v in sorted(counters.items()))
