"""
Unit tests for Arabic name normalization.

No network, no I/O beyond the real arabic_names.json -- the data-integrity
tests below run against the shipped file on purpose, because a bad entry there
mispronounces a real customer's name and no amount of logic testing catches it.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import name_normalizer as nn  # noqa: E402


ALEF_MAKSURA = "ى"
YA = "ي"


# --------------------------------------------------------------------------
# The data file must actually be loadable. Everything else is moot if not.
# --------------------------------------------------------------------------
def test_data_file_loads_cleanly():
    assert nn.LOAD_ERROR is None, f"arabic_names.json failed to load: {nn.LOAD_ERROR}"
    assert len(nn.PROTECTED) > 20
    assert len(nn.OVERRIDES) > 100


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------
def test_dotless_ya_is_repaired():
    out, counters = nn.normalize_name("على")
    assert out == "علي"
    assert counters == {nn.RULE_DOTLESS_YA: 1}


@pytest.mark.parametrize(
    "degraded,expected",
    [
        ("فهمى", "فهمي"),
        ("حلمى", "حلمي"),
        ("شوقى", "شوقي"),
        ("حجازى", "حجازي"),
        ("صبحى", "صبحي"),
        ("لطفى", "لطفي"),
        ("الشرقاوى", "الشرقاوي"),
        ("الدسوقى", "الدسوقي"),
    ],
)
def test_nisba_names_and_surnames_are_repaired(degraded, expected):
    """The bulk of what this feature fixes is not `علي` but every -i name in
    the book, which the source data stores with a dotless ya."""
    out, _ = nn.normalize_name(degraded)
    assert out == expected


# --------------------------------------------------------------------------
# The protected list -- the rule must not fire
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", ["مصطفى", "يحيى", "هدى", "منى", "ليلى", "سلمى", "عيسى", "موسى", "ندى", "سلوى"]
)
def test_protected_names_are_untouched(name):
    out, counters = nn.normalize_name(name)
    assert out == name
    assert counters == {}


def test_protection_covers_the_al_form():
    """`نور الهدى` needs no multi-token entry: الهدى resolves through هدى."""
    for name in ("المصطفى", "المرتضى", "الهدى"):
        out, counters = nn.normalize_name(name)
        assert out == name, name
        assert counters == {}


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "degraded,expected",
    [("احمد", "أحمد"), ("ابراهيم", "إبراهيم"), ("ادم", "آدم"), ("فاطمه", "فاطمة"),
     ("عائشه", "عائشة"), ("خديجه", "خديجة"), ("هبه", "هبة")],
)
def test_overrides_are_applied(degraded, expected):
    out, counters = nn.normalize_name(degraded)
    assert out == expected
    assert counters == {nn.RULE_OVERRIDE: 1}


def test_override_wins_and_the_rule_does_not_rerun_on_its_output():
    """اروى -> أروى ends in ى. If the rule ran on the override's result it
    would produce أروي, which is a different name."""
    out, counters = nn.normalize_name("اروى")
    assert out == "أروى"
    assert counters == {nn.RULE_OVERRIDE: 1}


# --------------------------------------------------------------------------
# Names that legitimately end in ه -- protected by the ABSENCE of a ه rule
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["طه", "عبده", "الله", "نبيه", "وجيه"])
def test_names_ending_in_ha_are_never_touched(name):
    out, counters = nn.normalize_name(name)
    assert out == name
    assert counters == {}


def test_abdullah_survives_intact():
    out, counters = nn.normalize_name("عبد الله")
    assert out == "عبد الله"
    assert counters == {}


def test_abd_al_ghani_is_repaired():
    """غنى was protected in the first pass, which silently blocked this. The
    ال- form resolves through غنى, so protecting it broke a very common name."""
    out, counters = nn.normalize_name("عبد الغنى")
    assert out == "عبد الغني"
    assert counters == {nn.RULE_DOTLESS_YA: 1}


@pytest.mark.parametrize(
    "degraded,expected",
    [("فواد", "فؤاد"), ("رافت", "رأفت"), ("نشات", "نشأت"), ("وايل", "وائل"),
     ("مومن", "مؤمن"), ("مامون", "مأمون")],
)
def test_hamza_on_waw_and_ya_seats(degraded, expected):
    """A whole defect class the first pass missed entirely -- hamza does not
    only sit on alif."""
    out, _ = nn.normalize_name(degraded)
    assert out == expected


@pytest.mark.parametrize(
    "degraded,expected",
    [("شنوده", "شنودة"), ("دميانه", "دميانة"), ("بشاره", "بشارة"),
     ("نخله", "نخلة"), ("اندراوس", "أندراوس"), ("ابرام", "إبرام")],
)
def test_coptic_egyptian_names(degraded, expected):
    out, _ = nn.normalize_name(degraded)
    assert out == expected


@pytest.mark.parametrize(
    "degraded,expected",
    [("شحاته", "شحاتة"), ("جمعه", "جمعة"), ("خليفه", "خليفة"),
     ("سلامه", "سلامة"), ("حمزه", "حمزة"), ("وهبه", "وهبة")],
)
def test_common_egyptian_surnames(degraded, expected):
    out, _ = nn.normalize_name(degraded)
    assert out == expected


# --------------------------------------------------------------------------
# Gender-conditional protection
# --------------------------------------------------------------------------
@pytest.mark.parametrize("token", ["يسرى", "تقى", "حسنى", "غنى", "بشرى", "ذكرى"])
def test_ambiguous_token_follows_gender(token):
    """These read as a female name with ى and a male name with ي. Neither
    reading can win on the token alone -- gender decides."""
    female_out, _ = nn.normalize_name(token, nn.FEMALE)
    male_out, _ = nn.normalize_name(token, nn.MALE)
    assert female_out == token
    assert male_out == token[:-1] + YA


def test_unknown_gender_protects():
    """Absent or unrecognised gender must behave as it did before gender was
    wired in -- leave the stored spelling alone rather than rewrite it."""
    for g in (None, "", "unknown", "x"):
        out, _ = nn.normalize_name("يسرى", nn.parse_gender(g))
        assert out == "يسرى"


def test_gender_applies_only_to_the_given_name():
    """Token 0 is the person; the tokens after it are the father's and
    grandfather's names, which are male. A woman named Fatma whose father is
    Yosri must not have his name read as Yosra."""
    out, _ = nn.normalize_name("فاطمة يسرى محمد", nn.FEMALE)
    assert out == "فاطمة يسري محمد"


def test_female_given_name_survives_in_position_zero():
    out, _ = nn.normalize_name("يسرى محمد على", nn.FEMALE)
    assert out == "يسرى محمد علي"


def test_male_given_name_is_repaired_in_position_zero():
    out, _ = nn.normalize_name("يسرى محمد على", nn.MALE)
    assert out == "يسري محمد علي"


def test_unconditional_protection_ignores_gender():
    """مصطفى is male and هدى is female, but neither has a ي counterpart in
    use, so gender must not change them either way."""
    for token in ("مصطفى", "هدى", "ليلى", "موسى"):
        for g in (nn.MALE, nn.FEMALE, None):
            out, _ = nn.normalize_name(token, g)
            assert out == token, (token, g)


@pytest.mark.parametrize(
    "raw,expected",
    [("male", nn.MALE), ("Male", nn.MALE), ("MALE", nn.MALE), ("  male  ", nn.MALE),
     ("female", nn.FEMALE), ("Female", nn.FEMALE), ("F", nn.FEMALE), ("m", nn.MALE),
     ("ذكر", nn.MALE), ("أنثى", nn.FEMALE),
     ("", None), ("   ", None), ("unknown", None), (None, None), (1, None)],
)
def test_parse_gender(raw, expected):
    assert nn.parse_gender(raw) == expected


def test_gender_is_read_from_the_mapped_key():
    dv = {
        "user_name": "يسرى محمد",
        "br_gender": "Male",
        "guarantor_name": "يسرى أحمد",
        "gr_gender": "female",
        "call_receiver": "يسرى",
        "cr_gender": "",
    }
    gender_keys = {
        "user_name": "br_gender",
        "guarantor_name": "gr_gender",
        "call_receiver": "cr_gender",
    }
    out, _ = nn.normalize_dynamic_variables(dv, KEYS, gender_keys)
    assert out["user_name"] == "يسري محمد"     # br_gender says male
    assert out["guarantor_name"] == "يسرى أحمد"  # gr_gender says female
    assert out["call_receiver"] == "يسرى"        # blank -> protected


def test_abd_al_ghani_still_repaired_for_a_male():
    """غنى moved to gender-conditional; the ال- form must still resolve."""
    out, _ = nn.normalize_name("عبد الغنى", nn.MALE)
    assert out == "عبد الغني"


# --------------------------------------------------------------------------
# Tokenization
# --------------------------------------------------------------------------
def test_multi_token_name_protects_and_repairs_independently():
    out, counters = nn.normalize_name("مصطفى محمد على")
    assert out == "مصطفى محمد علي"
    assert counters == {nn.RULE_DOTLESS_YA: 1}


def test_original_spacing_is_preserved():
    out, _ = nn.normalize_name("  مصطفى   محمد  على ")
    assert out == "  مصطفى   محمد  علي "


def test_tatweel_matches_and_survives():
    out, counters = nn.normalize_name("علــى")
    assert out == "علــي"
    assert counters == {nn.RULE_DOTLESS_YA: 1}


def test_harakat_do_not_defeat_the_protected_list():
    out, counters = nn.normalize_name("مُصطفى")
    assert out == "مُصطفى"
    assert counters == {}


def test_edge_punctuation_is_restored():
    out, counters = nn.normalize_name("(على)")
    assert out == "(علي)"
    assert counters == {nn.RULE_DOTLESS_YA: 1}


def test_kunya_compound():
    out, _ = nn.normalize_name("أبو على")
    assert out == "أبو علي"


# --------------------------------------------------------------------------
# Values that are not names
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, 123, 4.5, True, {"a": 1}, ["x"], "", "   "])
def test_non_string_and_blank_values_pass_through(value):
    out, counters = nn.normalize_name(value)
    assert out == value
    assert counters == {}


# --------------------------------------------------------------------------
# normalize_dynamic_variables
# --------------------------------------------------------------------------
KEYS = ["user_name", "user_name_full", "call_receiver", "guarantor_name", "guarantor_name_full"]


def test_only_configured_keys_are_touched():
    dv = {
        "user_name": "على محمد",
        "contract_ref": "CTR-00123",
        "due_date": "01/07/2026",
        "payment_amount": "1500",
        "tracking_id": "trk-1",
        "br_phone_number": "+201111111111",
    }
    out, counters = nn.normalize_dynamic_variables(dv, KEYS)
    assert out["user_name"] == "علي محمد"
    for k in ("contract_ref", "due_date", "payment_amount", "tracking_id", "br_phone_number"):
        assert out[k] == dv[k]
    assert counters == {nn.RULE_DOTLESS_YA: 1}


def test_input_dict_is_never_mutated():
    """The raw payload has to stay byte-identical -- to_webhook_payload()
    echoes it back to the .NET client."""
    dv = {"user_name": "على"}
    out, _ = nn.normalize_dynamic_variables(dv, KEYS)
    assert dv["user_name"] == "على"
    assert out["user_name"] == "علي"
    assert out is not dv


def test_missing_and_empty_keys_are_harmless():
    dv = {"user_name": "على", "guarantor_name": "", "guarantor_name_full": ""}
    out, counters = nn.normalize_dynamic_variables(dv, KEYS)
    assert out["guarantor_name"] == ""
    assert counters == {nn.RULE_DOTLESS_YA: 1}


def test_counters_accumulate_across_keys():
    dv = {"user_name": "على", "user_name_full": "على احمد", "call_receiver": "مصطفى"}
    _, counters = nn.normalize_dynamic_variables(dv, KEYS)
    assert counters == {nn.RULE_DOTLESS_YA: 2, nn.RULE_OVERRIDE: 1}


def test_format_counters_emits_no_name_text():
    line = nn.format_counters({nn.RULE_DOTLESS_YA: 2, nn.RULE_OVERRIDE: 1})
    assert line == "dotless_ya=2 override=1"


# --------------------------------------------------------------------------
# Data integrity -- runs against the shipped arabic_names.json
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def data():
    return json.loads(nn.DATA_FILE.read_text(encoding="utf-8"))


def test_file_is_utf8_without_bom():
    raw = nn.DATA_FILE.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_every_protected_entry_ends_in_alef_maksura(data):
    bad = [t for t in data["protected_final_ya"] if not t.endswith(ALEF_MAKSURA)]
    assert bad == [], f"these protect nothing: {bad}"


def test_no_token_is_both_protected_and_an_override_key(data):
    both = set(data["protected_final_ya"]) & set(data["overrides"])
    assert both == set()


def test_no_override_maps_a_token_to_itself(data):
    assert [k for k, v in data["overrides"].items() if k == v] == []


def test_no_override_value_carries_tashkeel_or_tatweel(data):
    marks = set("ًٌٍَُِّْـٰ")
    bad = [f"{k}->{v}" for k, v in data["overrides"].items() if marks & set(v)]
    assert bad == []


NISBA_MUST_NOT_BE_PROTECTED = [
    "علي", "فهمي", "حلمي", "شوقي", "حجازي", "صبحي", "لطفي", "رمزي", "حمدي",
    "فتحي", "سامي", "زكي", "مرسي", "حسني", "بيشوي", "شادي", "بدوي", "قناوي",
]


@pytest.mark.parametrize("correct", NISBA_MUST_NOT_BE_PROTECTED)
def test_protecting_a_nisba_name_would_block_its_own_repair(correct, data):
    """`حسني` (Hosni) is the trap: protecting `حسنى` leaves every degraded
    Hosni broken forever. Guard it explicitly."""
    degraded = correct[:-1] + ALEF_MAKSURA
    assert degraded not in set(data["protected_final_ya"])
    assert correct not in set(data["protected_final_ya"])


# --------------------------------------------------------------------------
# The /v/ sound. MEASURED 2026-08-17: the agent's voice renders ڤ (U+06A4) as
# /v/. Arabic has no native letter for it, so foreign-origin names are stored
# either way -- this is a lookup table and can never be a rule, since ف -> ڤ
# applied generally would say "Vatma" for فاطمة.
# --------------------------------------------------------------------------
VEH = "ڤ"


@pytest.mark.parametrize(
    "degraded,expected",
    [("مرفت", "مرڤت"), ("ميرفت", "ميرڤت"), ("نيفين", "نيڤين"),
     ("فيرا", "ڤيرا"), ("فيكتور", "ڤيكتور"), ("سيلفيا", "سيلڤيا"),
     ("فيوليت", "ڤيوليت"), ("فيرونيكا", "ڤيرونيكا")],
)
def test_v_names_get_the_veh_letter(degraded, expected):
    out, counters = nn.normalize_name(degraded)
    assert out == expected
    assert counters == {nn.RULE_OVERRIDE: 1}


def test_vivian_converts_both_v_positions():
    """Vivian is /ˈvɪviən/ -- both sounds are /v/. Converting only the first
    ف yields ڤيفيان, "Vi-fian", a non-word and worse than the ف spelling it
    replaces. Missed by the first list AND by Codex's audit of it; caught on a
    third read and then CONFIRMED BY EAR 2026-08-17 against the agent's own
    voice (Claude_files/probe_v_confirm.py)."""
    out, _ = nn.normalize_name("فيفيان")
    assert out == "ڤيڤيان"
    assert out.count(VEH) == 2


@pytest.mark.parametrize("name", ["ايفون", "ايفلين", "ايفيت"])
def test_earlier_coptic_entries_were_upgraded_to_veh(name):
    """These were added before ڤ was known to work, so their values used ف."""
    out, _ = nn.normalize_name(name)
    assert VEH in out


# The trap: Greek/Latin `ph` transliterates to ف but is pronounced /f/, so
# these look foreign yet must never acquire a ڤ. A wrong entry here does not
# approximate the name -- it says a different word.
GENUINE_F = [
    "جوزيف", "جوزفين", "رافائيل", "روفائيل", "سيرافيم", "اسطفانوس", "استفانوس",
    "ستيفاني", "صوفي", "صوفيا", "فليمون", "فيلومينا", "افرايم", "كريستوفر",
    "فيلوباتير", "فيليب", "فرانسيس", "فيبي",
    "فاطمة", "فهمي", "فؤاد", "فتحي", "فايزة", "فريدة", "فوزية", "يوسف",
    "شريف", "لطفي", "عفاف", "نفيسة", "صفية", "شفيقة", "مصطفى", "فيروز", "صفوت",
]


@pytest.mark.parametrize("name", GENUINE_F)
def test_genuine_f_names_never_acquire_a_veh(name):
    out, _ = nn.normalize_name(name)
    assert VEH not in out, f"{name} -> {out} wrongly given a /v/ sound"


@pytest.mark.parametrize("name", GENUINE_F)
def test_genuine_f_names_are_not_override_keys_producing_veh(name, data):
    assert VEH not in data["overrides"].get(name, "")


def test_v_map_entries_change_nothing_but_feh_to_veh(data):
    """Structural guarantee: a /v/ entry cannot quietly turn one name into a
    different name. Folding hamza seating out, key and value must differ only
    where ف became ڤ."""
    fold = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})
    for k, v in data["overrides"].items():
        if VEH not in v:
            continue
        fk, fv = k.translate(fold), v.translate(fold)
        assert len(fk) == len(fv), f"{k} -> {v} changes length"
        for a, b in zip(fk, fv):
            assert a == b or (a == "ف" and b == VEH), f"{k} -> {v} changes more than ف->ڤ"


def test_v_letter_survives_the_pipeline_untouched():
    """Records that already use ڤ must pass through unharmed -- the match key
    strips only tatweel and harakat, never ڤ."""
    for name in ("ڤيڤيان", "ڤيرا", "ڤيكتور"):
        out, counters = nn.normalize_name(name)
        assert out == name
        assert counters == {}


HA_FINAL_MUST_NOT_BE_OVERRIDE_KEYS = ["طه", "عبده", "الله", "نبيه", "وجيه"]


@pytest.mark.parametrize("name", HA_FINAL_MUST_NOT_BE_OVERRIDE_KEYS)
def test_names_ending_in_ha_are_not_override_keys(name, data):
    assert name not in data["overrides"]


# --------------------------------------------------------------------------
# The design's core guarantee: corrected names reach the agent, the raw
# payload reaches the .NET client. No SIP, no network -- construction only.
# --------------------------------------------------------------------------
def _session(raw, speech=None):
    from call_session import CallSession
    from config import Settings
    from port_allocator import PortAllocator

    return CallSession(
        phone_number="01000000000",
        dynamic_variables=raw,
        speech_dynamic_variables=speech,
        settings=Settings(),
        port_allocator=PortAllocator(40000, 40100),
        tracking_id="trk-1",
    )


def test_webhook_echoes_raw_while_the_agent_gets_the_correction():
    raw = {"user_name": "على", "tracking_id": "trk-1"}
    speech, _ = nn.normalize_dynamic_variables(raw, KEYS)
    s = _session(raw, speech)

    assert s.speech_dynamic_variables["user_name"] == "علي"   # what Sara says
    assert s.dynamic_variables["user_name"] == "على"          # what the client sent
    assert s.to_webhook_payload()["metadata"]["dynamic_variables"]["user_name"] == "على"


def test_omitting_the_speech_snapshot_falls_back_to_raw():
    """Every existing caller and test constructs CallSession without it."""
    raw = {"user_name": "على"}
    s = _session(raw)
    assert s.speech_dynamic_variables is raw


# --------------------------------------------------------------------------
# Stage 2 -- pronunciation. CHOSEN BY EAR 2026-08-31 against the production
# voice HqYWHPHcPZzpeN2p6AJM on eleven_multilingual_v2, not by theory. The
# probe rig is Claude_files/tts_probe/. `خلاف` is spelled correctly and is
# still read as the word خِلاف ("dispute"); `خَلَف` is what makes the voice say
# the surname. These are deliberate misspellings and only ElevenLabs sees them.
# --------------------------------------------------------------------------
KHALLAF_ALIAS = "خَلَف"                      # خَلَف
NAYYERA_ALIAS = "نَيِّرَة"  # نَيِّرَة
ESMAT_ALIAS = "عِصْمَت"           # عِصْمَت
GIRGIS_ALIAS = "جِرْجِس"          # جِرْجِس


@pytest.mark.parametrize("value,expected", [
    ("خلاف", KHALLAF_ALIAS),
    ("محمد خلاف", "محمد " + KHALLAF_ALIAS),
    ("نيرة", NAYYERA_ALIAS),
    ("عصمت", ESMAT_ALIAS),
    ("محمد عصمت", "محمد " + ESMAT_ALIAS),
    ("جرجس", GIRGIS_ALIAS),
])
def test_pronunciation_alias_is_applied(value, expected):
    out, _ = nn.normalize_name(value)
    assert out == expected


def test_spelling_repair_chains_into_the_pronunciation_alias():
    """`نيره` is repaired to `نيرة` by stage 1, which is what stage 2 is keyed
    on. Without the chain the ة/ه variant would silently keep the old reading,
    and only one of the two spellings would ever be said correctly."""
    out, counters = nn.normalize_name("نيره")
    assert out == NAYYERA_ALIAS
    assert counters == {nn.RULE_OVERRIDE: 1, nn.RULE_PRONUNCIATION: 1}


def test_alias_survives_edge_punctuation():
    out, _ = nn.normalize_name("خلاف،")
    assert out == KHALLAF_ALIAS + "،"


def test_aliases_are_the_exact_code_points_that_were_listened_to(data):
    """MEASURED: ElevenLabs does not NFC-normalize its input -- two strings
    that are Unicode-canonically equal produced different audio. So a
    visually-identical retyping of an alias is NOT the same alias, and this
    test pins the byte sequence that was actually approved rather than how it
    renders."""
    assert data["pronunciation"]["خلاف"] == KHALLAF_ALIAS
    assert data["pronunciation"]["نيرة"] == NAYYERA_ALIAS
    assert data["pronunciation"]["عصمت"] == ESMAT_ALIAS
    assert data["pronunciation"]["جرجس"] == GIRGIS_ALIAS


def test_only_configured_keys_get_an_alias():
    """A contract ref that happens to contain the name must not be respelled."""
    raw = {"user_name": "خلاف", "contract_ref": "خلاف-123"}
    out, _ = nn.normalize_dynamic_variables(raw, ["user_name"])
    assert out["user_name"] == KHALLAF_ALIAS
    assert out["contract_ref"] == "خلاف-123"


def test_pronunciation_keys_carry_no_harakat_or_tatweel(data):
    """Lookups canonicalize, so a key with marks on it can never match."""
    marks = set("ًٌٍَُِّْـٰ")
    bad = [k for k in data.get("pronunciation", {}) if marks & set(k)]
    assert bad == []


def test_no_pronunciation_key_is_also_an_override_key(data):
    """Stage 1 would rewrite the token first and the entry would be dead."""
    both = set(data.get("pronunciation", {})) & set(data["overrides"])
    assert both == set()


def test_no_pronunciation_maps_a_token_to_itself(data):
    assert [k for k, v in data.get("pronunciation", {}).items() if k == v] == []


# --------------------------------------------------------------------------
# Gender-conditional pronunciation. `ملك` is Malak for a woman and Malik for a
# man on the SAME spelling, so a flat alias would rename every Malik. The
# borrower payload's own gender column decides, exactly as it does for the
# stage-1 يسرى/يسري case. CHOSEN BY EAR 2026-08-31.
# --------------------------------------------------------------------------
MALAK_ALIAS = "مَلَك"  # مَلَك


@pytest.mark.parametrize("gender,expected", [
    ("female", MALAK_ALIAS),
    ("Female", MALAK_ALIAS),
    ("أنثى", MALAK_ALIAS),
    ("male", "ملك"),
    ("Male", "ملك"),
    ("", "ملك"),
    (None, "ملك"),
    ("wat", "ملك"),
])
def test_female_conditional_alias_follows_the_gender_column(gender, expected):
    """Only a positive FEMALE reading applies it. Blank, absent or unrecognised
    gender must leave the name alone -- that is the safe direction, because
    saying Malak to a man called Malik is worse than the status quo."""
    out, _ = nn.normalize_name("ملك", nn.parse_gender(gender))
    assert out == expected


def test_female_conditional_alias_is_given_name_position_only():
    """In `سارة ملك` the second token is the father's name and he is a man, so
    the alias must not fire there even though the record is female."""
    out, _ = nn.normalize_name("سارة ملك", nn.FEMALE)
    assert out == "سارة ملك"
    out, _ = nn.normalize_name("ملك محمد", nn.FEMALE)
    assert out == MALAK_ALIAS + " محمد"


def test_female_conditional_alias_end_to_end_reads_the_payload_gender():
    raw = {"user_name": "ملك", "br_gender": "Female", "call_receiver": "ملك",
           "cr_gender": "Male"}
    out, counters = nn.normalize_dynamic_variables(
        raw, ["user_name", "call_receiver"],
        {"user_name": "br_gender", "call_receiver": "cr_gender"})
    assert out["user_name"] == MALAK_ALIAS      # she is Malak
    assert out["call_receiver"] == "ملك"        # he is Malik -- untouched
    assert counters == {nn.RULE_PRONUNCIATION_FEMALE: 1}


def test_malak_alias_is_the_exact_code_points_that_were_listened_to(data):
    assert data["pronunciation_if_female"]["ملك"] == MALAK_ALIAS


def test_no_token_is_in_both_pronunciation_tables(data):
    """The unconditional table is consulted first, so the conditional entry
    would be unreachable and the gender logic silently dead."""
    both = set(data.get("pronunciation", {})) & set(data.get("pronunciation_if_female", {}))
    assert both == set()


def test_female_conditional_keys_are_clean(data):
    marks = set("ًٌٍَُِّْـٰ")
    table = data.get("pronunciation_if_female", {})
    assert [k for k in table if marks & set(k)] == []
    assert set(table) & set(data["overrides"]) == set()
    assert [k for k, v in table.items() if k == v] == []


# --------------------------------------------------------------------------
# Context-dependent pronunciation. MEASURED 2026-09-01: نيرة needs نَيِّرَة
# standing alone and نَيِّرا once another name sits beside it -- the voice
# re-parses the pair. `user_name` is often one token and `user_name_full`
# several, so the same borrower legitimately gets both forms on one call.
# --------------------------------------------------------------------------
NAYYERA_IN_FULL = "نَيِّرا"  # نَيِّرا


@pytest.mark.parametrize("value,expected", [
    ("نيرة", NAYYERA_ALIAS),                                   # alone
    ("نيرة،", NAYYERA_ALIAS + "،"),                            # punctuation is not a name
    ("نيرة خلاف", NAYYERA_IN_FULL + " " + KHALLAF_ALIAS),      # another name follows
    ("محمد نيرة", "محمد " + NAYYERA_IN_FULL),                  # another name precedes
    ("نيره خلاف", NAYYERA_IN_FULL + " " + KHALLAF_ALIAS),      # via the ه->ة repair
])
def test_full_name_context_selects_the_other_alias(value, expected):
    out, _ = nn.normalize_name(value)
    assert out == expected


def test_a_name_without_a_full_name_entry_falls_through_to_the_solo_table():
    """خلاف has one alias for both contexts. The full-name table must not
    swallow the lookup when it has no entry for that name."""
    out, _ = nn.normalize_name("خلاف")
    assert out == KHALLAF_ALIAS
    out, _ = nn.normalize_name("محمد خلاف")
    assert out == "محمد " + KHALLAF_ALIAS


def test_the_same_borrower_gets_both_forms_across_fields():
    """user_name is the short name, user_name_full the full one -- one call,
    one person, two correct renderings."""
    raw = {"user_name": "نيرة", "user_name_full": "نيرة خلاف"}
    out, _ = nn.normalize_dynamic_variables(raw, ["user_name", "user_name_full"])
    assert out["user_name"] == NAYYERA_ALIAS
    assert out["user_name_full"] == NAYYERA_IN_FULL + " " + KHALLAF_ALIAS


def test_full_name_alias_is_the_exact_code_points_that_were_listened_to(data):
    assert data["pronunciation_in_full_name"]["نيرة"] == NAYYERA_IN_FULL


def test_full_name_table_keys_are_clean(data):
    marks = set("ًٌٍَُِّْـٰ")
    table = data.get("pronunciation_in_full_name", {})
    assert [k for k in table if marks & set(k)] == []
    assert set(table) & set(data["overrides"]) == set()
    assert [k for k, v in table.items() if k == v] == []
