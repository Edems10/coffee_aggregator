from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

from coffee_aggregator.models import Processing, ProcessMethod, RoastLevel, RoastProfile

_DASHES = "‐‑‒–—―−"
_SEPARATORS = f"-_/{_DASHES}"
_TRANSLATION = {ord(ch): " " for ch in _SEPARATORS}
_DASH_TRANSLATION: dict[int, str] = {ord(ch): "-" for ch in _DASHES} | {0x00A0: " "}
_WHITESPACE_RE = re.compile(r"\s+")
_PLUS_RE = re.compile(r"\s*\+")
_NUMBER_RE = re.compile(r"\d[\d\s .,]*")
_LIST_SPLIT_RE = re.compile(r"[,;/|•·∙‧\n\r]+")
_MAX_PERCENT = 100
_MIN_ALTITUDE_M = 100
_MAX_ALTITUDE_M = 4000
_DECIMAL_DIGITS = 2
_ISO_YEAR_MIN = 1900
_ISO_YEAR_MAX = 2100


def fold(text: str | None) -> str:
    """Lower-case, de-accent and flatten separators so tables can match.

    Args:
        text: Arbitrary shop text.

    Returns:
        A folded ASCII-ish string, or an empty string for ``None``.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    flattened = stripped.lower().translate(_TRANSLATION)
    flattened = _PLUS_RE.sub(" +", flattened)
    return _WHITESPACE_RE.sub(" ", flattened).strip()


def dash_fold(text: str | None) -> str:
    """Lower-case and de-accent while keeping dashes, so ranges stay readable.

    Args:
        text: Arbitrary shop text.

    Returns:
        A folded string in which every kind of dash became an ASCII hyphen.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    hyphenated = stripped.lower().translate(_DASH_TRANSLATION)
    return _WHITESPACE_RE.sub(" ", hyphenated).strip()


def _first_match(folded: str, table: tuple[tuple[str, str], ...]) -> str | None:
    for needle, value in table:
        if needle in folded:
            return value
    return None


def _to_float(raw: str) -> float | None:
    """Turn a localised number such as ``1 249,50`` into a float."""
    cleaned = raw.replace(" ", "").replace(" ", "").strip(".,")
    if not cleaned:
        return None
    last_comma = cleaned.rfind(",")
    last_dot = cleaned.rfind(".")
    decimal_at = max(last_comma, last_dot)
    if decimal_at == -1:
        integer, fraction = cleaned, ""
    elif len(cleaned) - decimal_at - 1 <= _DECIMAL_DIGITS:
        integer, fraction = cleaned[:decimal_at], cleaned[decimal_at + 1 :]
    else:
        integer, fraction = cleaned, ""
    digits = re.sub(r"\D", "", integer)
    if not digits and not fraction:
        return None
    try:
        return float(f"{digits or '0'}.{fraction or '0'}")
    except ValueError:  # pragma: no cover - defensive, regex guarantees digits
        return None


_WEIGHT_RE = re.compile(
    r"(\d[\d\s .,]*)\s*(kg|kilogram\w*|kilo|gram\w*|gr|g)\b",
    re.IGNORECASE,
)


def parse_weight_grams(text: str | None) -> int | None:
    """Parse a package weight into grams.

    Args:
        text: Text such as ``"200 g"``, ``"1 kg"``, ``"0,25 kg"`` or ``"250g"``.

    Returns:
        The weight in grams, or None when no weight is present.
    """
    if not text:
        return None
    match = _WEIGHT_RE.search(text)
    if match is None:
        return None
    value = _to_float(match.group(1))
    if value is None or value <= 0:
        return None
    unit = match.group(2).lower()
    grams = value * 1000 if unit.startswith(("kg", "kilo")) else value
    return round(grams)


_CURRENCIES: tuple[tuple[str, str], ...] = (
    ("eur", "EUR"),
    ("€", "EUR"),
    ("czk", "CZK"),
    ("kc", "CZK"),
    ("usd", "USD"),
    ("$", "USD"),
    ("gbp", "GBP"),
    ("pln", "PLN"),
)


def detect_currency(text: str | None) -> str | None:
    """Detect the currency a price is quoted in.

    Args:
        text: Text such as ``"9,99 EUR"`` or ``"249 Kc"``.

    Returns:
        An ISO 4217 code, or None when no currency marker is present.
    """
    if not text:
        return None
    lowered = fold(text)
    for needle, code in _CURRENCIES:
        if needle in lowered:
            return code
    return None


def parse_amount(text: str | None) -> float | None:
    """Parse the first localised number in the text into a float.

    Args:
        text: Text such as ``"9,99"``, ``"1 299"`` or ``"249,-"``.

    Returns:
        The number, or None when the text holds no digits.
    """
    if not text:
        return None
    match = _NUMBER_RE.search(text.replace("\u00a0", " ").replace("\u202f", " "))
    if match is None:
        return None
    return _to_float(match.group(0))


def parse_price(text: str | None) -> tuple[float | None, str | None]:
    """Parse a localised price together with the currency it is quoted in.

    Args:
        text: Text such as ``"9,99 EUR"``, ``"249 Kc"``, ``"1 299 Kc"``,
            ``"249,-"`` or ``"9.99"``.

    Returns:
        An ``(amount, currency)`` tuple; either member is None when absent.
    """
    return (parse_amount(text), detect_currency(text))


_ALTITUDE_RE = re.compile(
    r"(\d[\d\s .,]*)(?:\s*(?:az|do|to|\+)?\s*[-]\s*|\s+az\s+|\s+do\s+|\s+to\s+)"
    r"(\d[\d\s .,]*)"
)
#: A number immediately followed by a metre marker — the only reliable way to
#: tell an altitude from the harvest year that so often shares the same cell.
_ALTITUDE_UNIT_RE = re.compile(
    r"(\d[\d\s]*\d|\d)\s*(?:m\s*\.?\s*n\.?\s*m|masl|metrov|metru|metrech|metres|meters|m)\b",
    re.IGNORECASE,
)
#: A bare number; spaces group thousands, but a comma or a dot ends it, so
#: "2024, 1800" is two candidates rather than one unparsable blob.
_ALTITUDE_SINGLE_RE = re.compile(r"(\d[\d\s]*\d|\d)")


def _altitude_value(raw: str) -> int | None:
    value = _to_float(raw)
    if value is None:
        return None
    metres = round(value)
    if not _MIN_ALTITUDE_M <= metres <= _MAX_ALTITUDE_M:
        return None
    return metres


def parse_altitude(text: str | None) -> tuple[int | None, int | None]:
    """Parse an altitude statement into a metre range.

    Args:
        text: Text such as ``"1500 - 1700 m n. m."`` or ``"1 200 m.n.m."``.

    Returns:
        A ``(minimum, maximum)`` tuple; both members are None when unparsable
        and both are equal when a single altitude is given.
    """
    folded = dash_fold(text)
    if not folded:
        return (None, None)
    ranged = _ALTITUDE_RE.search(folded)
    if ranged is not None:
        low = _altitude_value(ranged.group(1))
        high = _altitude_value(ranged.group(2))
        if low is not None and high is not None:
            return (min(low, high), max(low, high))
    # A number carrying a metre marker wins over a bare one: "zber 2024, 1800 m
    # n.m." states a harvest year first, and a year is a plausible altitude.
    for candidate in (_ALTITUDE_UNIT_RE, _ALTITUDE_SINGLE_RE):
        for match in candidate.finditer(folded):
            value = _altitude_value(match.group(1))
            if value is not None:
                return (value, value)
    return (None, None)


_PROCESS_TABLE: tuple[tuple[str, str], ...] = (
    ("carbonic maceration", ProcessMethod.EXPERIMENTAL),
    ("karbonicka maceracia", ProcessMethod.EXPERIMENTAL),
    ("karbonicka macerace", ProcessMethod.EXPERIMENTAL),
    ("experiment", ProcessMethod.EXPERIMENTAL),
    ("anaerob", ProcessMethod.ANAEROBIC),
    ("wet hulled", ProcessMethod.WET_HULLED),
    ("wet hulling", ProcessMethod.WET_HULLED),
    ("giling basah", ProcessMethod.WET_HULLED),
    ("pulped natural", ProcessMethod.PULPED_NATURAL),
    ("poloprana", ProcessMethod.PULPED_NATURAL),
    ("honey", ProcessMethod.HONEY),
    ("medova", ProcessMethod.HONEY),
    ("medove", ProcessMethod.HONEY),
    ("medovy", ProcessMethod.HONEY),
    ("washed", ProcessMethod.WASHED),
    ("wet process", ProcessMethod.WASHED),
    ("prana", ProcessMethod.WASHED),
    ("prane", ProcessMethod.WASHED),
    ("prany", ProcessMethod.WASHED),
    ("myta", ProcessMethod.WASHED),
    ("myte", ProcessMethod.WASHED),
    ("mokra metoda", ProcessMethod.WASHED),
    ("mokrou metodou", ProcessMethod.WASHED),
    ("mokra cesta", ProcessMethod.WASHED),
    ("mokr", ProcessMethod.WASHED),  # mokré / mokro spracovaná — SK/CZ for "wet"
    ("natural", ProcessMethod.NATURAL),
    ("dry process", ProcessMethod.NATURAL),
    ("sucha metoda", ProcessMethod.NATURAL),
    ("suchou metodou", ProcessMethod.NATURAL),
    ("sucha", ProcessMethod.NATURAL),
    ("suche", ProcessMethod.NATURAL),
    ("susena", ProcessMethod.NATURAL),
    ("susene", ProcessMethod.NATURAL),
    ("prirodni", ProcessMethod.NATURAL),
    ("prirodna", ProcessMethod.NATURAL),
)


#: How many words a chunk may have and still be read as a method name of its own.
_MAX_METHOD_WORDS = 2
#: Members that carry no information about how the cherry was processed.
_UNSPECIFIC_METHODS = (ProcessMethod.OTHER, ProcessMethod.UNKNOWN, ProcessMethod.MIXED)


def normalize_process(text: str | None) -> ProcessMethod:
    """Map a free-form processing description onto :class:`ProcessMethod`.

    Args:
        text: Text such as ``"prana"``, ``"natural"`` or ``"giling basah"``.

    Returns:
        The matching member, ``OTHER`` for unrecognised non-empty text and
        ``UNKNOWN`` for empty input.
    """
    folded = fold(text)
    if not folded:
        return ProcessMethod.UNKNOWN
    matched = _first_match(folded, _PROCESS_TABLE)
    return ProcessMethod(matched) if matched is not None else ProcessMethod.OTHER


def parse_processing(text: str | None) -> Processing:
    """Read a processing statement that may name more than one method.

    A blend, or a lot marked ``"Washed · Natural"``, really was processed in
    several ways; collapsing that to one method loses the fact. Every recognised
    method is kept, and ``method`` becomes :attr:`ProcessMethod.MIXED` when there
    is more than one.

    Args:
        text: Text such as ``"Washed · Natural"`` or ``"natural / ruční sběr"``.

    Returns:
        The processing block, with ``raw`` untouched.
    """
    methods: list[ProcessMethod] = []
    for item in split_list(text):
        # An enumeration names methods ("Washed · Natural"); a description names
        # the stages of one ("mokré, sušené na slnku" is a washed coffee that was
        # sun dried). Only a bare name is read as a separate method.
        if len(item.split()) > _MAX_METHOD_WORDS:
            continue
        method = normalize_process(item)
        if method in _UNSPECIFIC_METHODS or method in methods:
            continue
        methods.append(method)
    if len(methods) == 1:
        return Processing(method=methods[0], raw=text, methods=methods)
    if len(methods) > 1:
        return Processing(method=ProcessMethod.MIXED, raw=text, methods=methods)
    # Nothing recognised: keep the single-value verdict, which is OTHER for text
    # that says *something* and UNKNOWN for text that says nothing at all.
    return Processing(method=normalize_process(text), raw=text, methods=[])


_ROAST_TABLE: tuple[tuple[str, str], ...] = (
    ("full city +", RoastLevel.DARK),
    ("full city plus", RoastLevel.DARK),
    ("full city", RoastLevel.MEDIUM_DARK),
    ("city +", RoastLevel.MEDIUM),
    ("city plus", RoastLevel.MEDIUM),
    ("cinnamon", RoastLevel.LIGHT),
    ("svetlo stredn", RoastLevel.MEDIUM_LIGHT),
    ("svetle stredn", RoastLevel.MEDIUM_LIGHT),
    ("stredne svetl", RoastLevel.MEDIUM_LIGHT),
    ("light medium", RoastLevel.MEDIUM_LIGHT),
    ("medium light", RoastLevel.MEDIUM_LIGHT),
    ("stredne tmav", RoastLevel.MEDIUM_DARK),
    ("stredni tmav", RoastLevel.MEDIUM_DARK),
    ("medium dark", RoastLevel.MEDIUM_DARK),
    ("viedensk", RoastLevel.MEDIUM_DARK),
    ("vieden", RoastLevel.MEDIUM_DARK),
    ("french", RoastLevel.DARK),
    ("italian", RoastLevel.DARK),
    ("taliansk", RoastLevel.DARK),
    ("francuzsk", RoastLevel.DARK),
    ("tmav", RoastLevel.DARK),
    ("dark", RoastLevel.DARK),
    ("svetl", RoastLevel.LIGHT),
    ("light", RoastLevel.LIGHT),
    ("city", RoastLevel.LIGHT),
    ("stredn", RoastLevel.MEDIUM),
    ("medium", RoastLevel.MEDIUM),
)


def normalize_roast_level(text: str | None) -> RoastLevel:
    """Map a free-form roast description onto :class:`RoastLevel`.

    Args:
        text: Text such as ``"Full City +"``, ``"svetle prazena"`` or ``"dark"``.

    Returns:
        The matching member, or ``UNKNOWN`` when nothing matches.
    """
    folded = fold(text)
    if not folded:
        return RoastLevel.UNKNOWN
    matched = _first_match(folded, _ROAST_TABLE)
    return RoastLevel(matched) if matched is not None else RoastLevel.UNKNOWN


_ESPRESSO_MARKERS = ("espresso", "espreso", "moka", "kavovar", "pakova")
_FILTER_MARKERS = (
    "filter",
    "filtr",
    "prekvapkav",
    "prekapav",
    "preliv",
    "v60",
    "chemex",
    "aeropress",
    "french press",
    "dripper",
)
_OMNI_MARKERS = ("omni", "univerzal", "vsestrann")


def normalize_roast_profile(text: str | None) -> RoastProfile:
    """Decide whether a roast targets espresso, filter or both.

    Args:
        text: Text such as ``"Kava na espresso"`` or ``"filter / espresso"``.

    Returns:
        The matching member, or ``UNKNOWN`` when nothing matches.
    """
    folded = fold(text)
    if not folded:
        return RoastProfile.UNKNOWN
    if any(marker in folded for marker in _OMNI_MARKERS):
        return RoastProfile.OMNI
    espresso = any(marker in folded for marker in _ESPRESSO_MARKERS)
    filtered = any(marker in folded for marker in _FILTER_MARKERS)
    if espresso and filtered:
        return RoastProfile.OMNI
    if espresso:
        return RoastProfile.ESPRESSO
    if filtered:
        return RoastProfile.FILTER
    return RoastProfile.UNKNOWN


_COUNTRY_TERMS: dict[str, tuple[str, ...]] = {
    "BR": ("brazilia", "brazilie", "brazil", "brasil", "brazilska", "brazilian"),
    "CO": ("kolumbia", "kolumbie", "colombia", "kolumbijska", "colombian"),
    "ET": ("etiopia", "etiopie", "ethiopia", "etiopska", "ethiopian", "abesinia"),
    "KE": ("kena", "kenya", "kenska", "kenyan"),
    "VN": ("vietnam", "vietnamska", "vietnamese"),
    "ID": ("indonezia", "indonezie", "indonesia", "indonezska", "sumatra", "sulawesi", "flores"),
    "HN": ("honduras", "honduraska", "honduran"),
    "UG": ("uganda", "ugandska", "ugandan"),
    "PE": ("peru", "peruanska", "peruvian"),
    "IN": ("india", "indie", "indicka", "indian"),
    "GT": ("guatemala", "guatemalska", "guatemalan"),
    "NI": ("nikaragua", "nicaragua", "nikaragujska", "nicaraguan"),
    "CR": ("kostarika", "costa rica", "kostaricka", "costa rican"),
    "MX": ("mexiko", "mexico", "mexicka", "mexican"),
    "TZ": ("tanzania", "tanzanie", "tanzanska", "tanzanian", "kilimandzaro"),
    "SV": ("el salvador", "salvador", "salvadorska", "salvadoran"),
    "CN": ("cina", "china", "cinska", "chinese", "yunnan"),
    "CI": ("pobrezie slonoviny", "pobrezi slonoviny", "ivory coast"),
    "PG": ("papua nova guinea", "papua new guinea", "papua"),
    "EC": ("ekvador", "ecuador", "ekvadorska", "ecuadorian"),
    "LA": ("laos", "laoska", "laotian"),
    "TH": ("thajsko", "thailand", "thajska", "thai"),
    "VE": ("venezuela", "venezuelska", "venezuelan"),
    "DO": ("dominikanska republika", "dominican republic", "dominikanska"),
    "HT": ("haiti", "haitska", "haitian"),
    "CD": ("kongo", "congo", "konzska", "congolese"),
    "RW": ("rwanda", "rwandska", "rwandan"),
    "BI": ("burundi", "burundska", "burundian"),
    "YE": ("jemen", "yemen", "jemenska", "yemeni"),
    "PA": ("panama", "panamska", "panamanian"),
    "BO": ("bolivia", "bolivie", "bolivijska", "bolivian"),
    "CU": ("kuba", "cuba", "kubanska", "cuban"),
    "JM": ("jamajka", "jamaica", "jamajska", "jamaican", "blue mountain"),
    "MW": ("malawi", "malawijska", "malawian"),
    "ZM": ("zambia", "zambie", "zambijska", "zambian"),
    "ZW": ("zimbabwe", "zimbabwianska"),
    "CM": ("kamerun", "cameroon", "kamerunska", "cameroonian"),
    "MG": ("madagaskar", "madagascar", "madagaskarska"),
    "MM": ("mjanmarsko", "myanmar", "barma", "burma"),
    "PH": ("filipiny", "philippines", "filipinska", "philippine"),
    "TL": ("vychodny timor", "vychodni timor", "east timor", "timor leste", "timor"),
    "NP": ("nepal", "nepalska", "nepalese"),
    "AO": ("angola", "angolska", "angolan"),
    "PR": ("portoriko", "puerto rico"),
    "US": ("havaj", "hawaii", "hawaiian"),
    "GH": ("ghana", "ghanska", "ghanaian"),
    "CV": ("kapverdy", "cape verde"),
}

_COUNTRY_BY_TERM: dict[str, str] = {
    fold(term): code for code, terms in _COUNTRY_TERMS.items() for term in terms
}
_COUNTRY_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(term) for term in sorted(_COUNTRY_BY_TERM, key=lambda t: (-len(t), t)))
    + r")\b"
)


def detect_country(text: str | None) -> str | None:
    """Detect a coffee-producing country mentioned anywhere in the text.

    Args:
        text: Product name, description or keyword list in SK, CZ or EN.

    Returns:
        An ISO 3166-1 alpha-2 code, or None when no country is recognised.
    """
    folded = fold(text)
    if not folded:
        return None
    match = _COUNTRY_RE.search(folded)
    if match is None:
        return None
    return _COUNTRY_BY_TERM[match.group(0)]


_SPECIES_RE = re.compile(
    r"(?:(?P<pct_first>\d{1,3})\s*%?\s*(?P<name_last>arabi\w*|robus\w*)"
    r"|(?P<name_first>arabi\w*|robus\w*)\s*[:\-]?\s*(?P<pct_last>\d{1,3})\s*%)",
    re.IGNORECASE,
)


def parse_species(text: str | None) -> tuple[int | None, int | None]:
    """Parse an arabica/robusta split.

    Args:
        text: Text such as ``"90 % Arabika, 10 % Robusta"`` or ``"100% arabica"``.

    Returns:
        A ``(arabica_pct, robusta_pct)`` tuple; members are None when unknown.
        When only one species is stated the other is inferred as the remainder.
    """
    folded = fold(text)
    if not folded:
        return (None, None)
    arabica: int | None = None
    robusta: int | None = None
    for match in _SPECIES_RE.finditer(folded):
        name = match.group("name_last") or match.group("name_first") or ""
        raw_pct = match.group("pct_first") or match.group("pct_last")
        if raw_pct is None:
            continue
        pct = int(raw_pct)
        if pct > _MAX_PERCENT:
            continue
        if name.startswith("arabi") and arabica is None:
            arabica = pct
        elif name.startswith("robus") and robusta is None:
            robusta = pct
    if arabica is not None and robusta is None:
        robusta = _MAX_PERCENT - arabica
    elif robusta is not None and arabica is None:
        arabica = _MAX_PERCENT - robusta
    return (arabica, robusta)


_BLEND_MARKERS = ("blend", "zmes", "smes", "smesi", "zmesi", "mix", "espresso blend")


def detect_blend(text: str | None, arabica_pct: int | None, robusta_pct: int | None) -> bool:
    """Decide whether a product is a blend rather than a single origin.

    Args:
        text: Product name or description.
        arabica_pct: Arabica share, when known.
        robusta_pct: Robusta share, when known.

    Returns:
        True when the name says blend or when neither species reaches 100 %.
    """
    folded = fold(text)
    if any(marker in folded for marker in _BLEND_MARKERS):
        return True
    known = [pct for pct in (arabica_pct, robusta_pct) if pct is not None]
    if not known:
        return False
    return not any(pct == _MAX_PERCENT for pct in known)


def split_list(text: str | None) -> list[str]:
    """Split a human-written enumeration into trimmed, de-duplicated items.

    Args:
        text: Text such as ``"kakao, karamel / tabakove listy"``.

    Returns:
        The individual items in their original order and spelling.
    """
    if not text:
        return []
    items: list[str] = []
    seen: set[str] = set()
    for chunk in _LIST_SPLIT_RE.split(text):
        cleaned = _WHITESPACE_RE.sub(" ", chunk).strip(" \t.-")
        key = fold(cleaned)
        if not cleaned or key in seen:
            continue
        seen.add(key)
        items.append(cleaned)
    return items


_DATE_RE = re.compile(r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def parse_date_dmy(text: str | None) -> date | None:
    """Parse a day-first date such as ``"11.09.2026"``.

    Also accepts ISO ``YYYY-MM-DD`` because shops mix both in microdata.

    Args:
        text: Text containing a date.

    Returns:
        The parsed date, or None when no valid date is present.
    """
    if not text:
        return None
    iso = _ISO_DATE_RE.search(text)
    if iso is not None:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    match = _DATE_RE.search(text)
    if match is None:
        return None
    return _safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))


def _safe_date(year: int, month: int, day: int) -> date | None:
    if not _ISO_YEAR_MIN <= year <= _ISO_YEAR_MAX:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_int(text: str | None) -> int | None:
    """Parse the first integer in the text.

    Args:
        text: Text such as ``"51 hodnoteni"`` or ``"Uprazene a vypite: 24339x"``.

    Returns:
        The integer, or None when the text holds no digits.
    """
    if not text:
        return None
    match = re.search(r"\d[\d\s ]*", text)
    if match is None:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    return int(digits) if digits else None


def parse_float(text: str | None) -> float | None:
    """Parse the first decimal number in the text.

    Args:
        text: Text such as ``"4,8 / 5"``.

    Returns:
        The number, or None when the text holds no digits.
    """
    return parse_amount(text)


#: Folded SK/CZ/EN intensity word -> its place on the 1-5 scale. Longer phrases
#: come first so "velmi vysoka" is never read as a plain "vysoka".
_INTENSITY_TABLE: tuple[tuple[str, int], ...] = (
    ("velmi vysoka", 5),
    ("velmi vysoke", 5),
    ("very high", 5),
    ("intenzivna", 5),
    ("intenzivni", 5),
    ("ziadna", 1),
    ("zadna", 1),
    ("none", 1),
    ("nizka", 2),
    ("nizke", 2),
    ("low", 2),
    ("jemna", 2),
    ("jemne", 2),
    ("stredna", 3),
    ("stredni", 3),
    ("stredne", 3),
    ("medium", 3),
    ("vysoka", 4),
    ("vysoke", 4),
    ("high", 4),
    ("plna", 4),
    ("plne", 4),
    ("plnej", 4),
    ("full", 4),
    ("vyrazna", 4),
    ("vyrazne", 4),
)
_MIN_INTENSITY = 1
_MAX_INTENSITY = 5
_WORD_RE = re.compile(r"[a-z0-9]+")


def parse_intensity(text: str | None) -> int | None:
    """Map an intensity word onto the same 1-5 scale the sensory bars use.

    Shops that draw no bars still describe body, acidity, bitterness and
    sweetness in words; putting both on one scale is what makes the two
    comparable across shops.

    Args:
        text: Text such as ``"vysok\u00e1"``, ``"st\u0159edn\u00ed"`` or ``"3"``.

    Returns:
        A value between 1 and 5, or None when the text names no intensity.
    """
    folded = fold(text)
    if not folded:
        return None
    # Whole words only: "príjemná" contains "jemná" and means pleasant, not weak.
    words = _WORD_RE.findall(folded)
    for needle, value in _INTENSITY_TABLE:
        wanted = needle.split()
        if any(
            words[start : start + len(wanted)] == wanted
            for start in range(len(words) - len(wanted) + 1)
        ):
            return value
    number = parse_int(folded)
    if number is not None and _MIN_INTENSITY <= number <= _MAX_INTENSITY:
        return number
    return None


#: Species names that introduce a variety rather than being one.
_SPECIES_PREFIX_RE = re.compile(
    r"^(?:arabica|arabika|arabic|robusta|canephora|liberica)\b[\s\-:.]*",
    re.IGNORECASE,
)
_VARIETY_SPLIT_RE = re.compile(r"[,/\u00b7\u2022\u2219\u2027;|]+")


def clean_variety(items: Iterable[str] | None) -> list[str]:
    """Turn shop variety text into plain cultivar names.

    ``"Arabica \u2013 Lempira"`` names one cultivar, not two, and
    ``"Castillo / Caturra"`` names two; both spellings are common enough that
    grouping by variety needs them flattened the same way.

    Args:
        items: The variety strings a shop stated, in any spelling.

    Returns:
        The cultivar names, de-duplicated, in their original order.
    """
    if items is None:
        return []
    cleaned: list[str] = []
    for item in items:
        for chunk in _VARIETY_SPLIT_RE.split(dash_fold_keep(item)):
            name = _SPECIES_PREFIX_RE.sub("", chunk).strip(" \t-\u2013\u2014.:")
            if name:
                cleaned.append(_WHITESPACE_RE.sub(" ", name))
    return _unique_preserving(cleaned)


def dash_fold_keep(text: str | None) -> str:
    """Collapse whitespace without touching case or diacritics.

    Args:
        text: Arbitrary shop text.

    Returns:
        The same text with runs of whitespace reduced to one space.
    """
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text.replace("\u00a0", " ")).strip()


def _unique_preserving(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = fold(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
