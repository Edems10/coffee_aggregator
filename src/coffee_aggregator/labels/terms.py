from __future__ import annotations

from typing import Final

from coffee_aggregator import normalize

__all__ = [
    "F_ACIDITY",
    "F_ALTITUDE",
    "F_BEST_BEFORE",
    "F_BITTERNESS",
    "F_BODY",
    "F_BREWING",
    "F_CERTIFICATIONS",
    "F_COUNTRY",
    "F_DECAF",
    "F_FARM",
    "F_FLAVOR",
    "F_HARVEST",
    "F_IGNORE",
    "F_PROCESS",
    "F_PRODUCER",
    "F_REGION",
    "F_ROAST",
    "F_ROAST_DATE",
    "F_SCA",
    "F_SHIP_WEIGHT",
    "F_SPECIES",
    "F_STATION",
    "F_SWEETNESS",
    "F_VARIETY",
    "F_WEIGHT",
    "KNOWN_FIELDS",
    "LABEL_MAP",
    "TERMS",
    "build_map",
]

F_IGNORE: Final = ""
F_COUNTRY: Final = "country"
F_REGION: Final = "region"
F_FARM: Final = "farm"
F_PRODUCER: Final = "producer"
F_STATION: Final = "washing_station"
F_ALTITUDE: Final = "altitude"
F_VARIETY: Final = "variety"
F_HARVEST: Final = "harvest"
F_PROCESS: Final = "process"
F_ROAST: Final = "roast"
F_BREWING: Final = "brewing"
F_FLAVOR: Final = "flavor_notes"
F_SCA: Final = "sca_score"
F_SPECIES: Final = "species"
F_DECAF: Final = "decaf"
F_WEIGHT: Final = "weight"
F_SHIP_WEIGHT: Final = "shipping_weight"
F_ROAST_DATE: Final = "roast_date"
F_BEST_BEFORE: Final = "best_before"
F_CERTIFICATIONS: Final = "certifications"
F_BODY: Final = "body"
F_BITTERNESS: Final = "bitterness"
F_ACIDITY: Final = "acidity"
F_SWEETNESS: Final = "sweetness"

KNOWN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        F_IGNORE,
        F_COUNTRY,
        F_REGION,
        F_FARM,
        F_PRODUCER,
        F_STATION,
        F_ALTITUDE,
        F_VARIETY,
        F_HARVEST,
        F_PROCESS,
        F_ROAST,
        F_BREWING,
        F_FLAVOR,
        F_SCA,
        F_SPECIES,
        F_DECAF,
        F_WEIGHT,
        F_SHIP_WEIGHT,
        F_ROAST_DATE,
        F_BEST_BEFORE,
        F_CERTIFICATIONS,
        F_BODY,
        F_BITTERNESS,
        F_ACIDITY,
        F_SWEETNESS,
    }
)

#: What a shop calls each canonical field, in Czech, Slovak and English.
#:
#: This is the vocabulary of the two languages, not of any one shop: a term
#: belongs here when any Czech or Slovak coffee shop could reasonably print it.
#: Terms are matched folded (accents and case removed), so spelling them
#: naturally here is enough and "Nadmořská výška" also matches "nadmorska vyska".
#: A shop's own oddities stay in its TOML ``label_map``; a term that turns out to
#: be ordinary language belongs here instead, so every shop gains it at once.
#:
#: Two terms are deliberately absent because they mean different things on
#: different platforms: "hmotnost" and "weight" are the parcel weight in a
#: Shoptet parameter table and the bag weight in a WooCommerce attribute. Each
#: platform adapter states its own reading of those two.
TERMS: Final[dict[str, tuple[str, ...]]] = {
    F_COUNTRY: (
        "země původu",
        "země původu zrna",
        "stát",
        "země",
        "původ",
        "původ kávy",
        "krajina pôvodu",
        "krajina",
        "pôvod",
        "pôvod kávy",
        "country",
        "country of origin",
        "origin",
    ),
    F_REGION: (
        "region",
        "oblast",
        "oblasť",
        "lokalita",
        "bližší určení",
        "okres",
        "location",
    ),
    F_FARM: (
        "farma",
        "plantáž",
        "farm",
        "finca",
    ),
    F_PRODUCER: (
        "producent",
        "producenti",
        "farmář",
        "farmár",
        "farmári",
        "farmář / plantáž",
        "pěstovatel",
        "pestovateľ",
        "pěstitel",
        "pěstitelé",
        "produkce",
        "sdružení",
        "majitel",
        "majiteľ",
        "vyrobil",
        "spracovateľ",
        "producer",
        "producers",
    ),
    F_STATION: (
        "zpracovatelská stanice",
        "zpracovatelský závod",
        "spracovateľská stanica",
        "prac stanica",
        "washing station",
    ),
    F_ALTITUDE: (
        "nadmořská výška",
        "nadmorská výška",
        "nadm výška",
        "nadm. výška",
        "výška",
        "altitude",
        "masl",
    ),
    F_VARIETY: (
        "odrůda",
        "odrůdy",
        "odrůda kávy",
        "odroda",
        "odroda kávy",
        "odroda alebo varieta",
        "odrody",
        "cultivar",
        "varieta",
        "kultivar",
        "variety",
        "varieties",
    ),
    F_HARVEST: (
        "sběr",
        "sklizeň",
        "zber",
        "obdobie zberu",
        "zber čerešní",
        "období sklizně",
        "sklizně",
        "způsob sběru",
        "úroda",
        "harvest",
    ),
    F_PROCESS: (
        "zpracování",
        "zpracování kávy",
        "způsob zpracování",
        "metoda zpracování",
        "metóda spracovania",
        "spracovanie",
        "spôsob spracovania",
        "úprava",
        "process",
        "processing",
        "proces",
        "proces sklizně",
    ),
    F_ROAST: (
        "pražení",
        "praženie",
        "praženia",
        "praženo",
        "stupeň pražení",
        "stupeň pražení kávy",
        "stupeň praženia",
        "typ pražení",
        "typ praženia",
        "způsob pražení",
        "spôsob praženia",
        "štýl praženia",
        "úroveň pražení",
        "profil pražení",
        "stupně pražení",
        "roast",
        "roast level",
    ),
    F_ROAST_DATE: (
        "datum pražení",
        "dátum praženia",
        "upraženo",
        "upražené",
        "roast date",
        "roasted on",
    ),
    F_BEST_BEFORE: (
        "minimální trvanlivost",
        "minimálna trvanlivosť",
        "trvanlivost",
        "spotřebujte do",
        "expirace",
        "expirácia",
        "best before",
    ),
    F_BREWING: (
        "příprava",
        "príprava",
        "přípravu",
        "příprava kávy",
        "podle přípravy kávy",
        "podľa prípravy kávy",
        "způsob přípravy",
        "spôsob prípravy",
        "spôsoby prípravy",
        "metoda přípravy",
        "typ přípravy",
        "doporučená příprava",
        "odporúčaná príprava",
        "odporúčaný spôsob prípravy",
        "vhodné",
        "vhodné pro",
        "vhodná pro",
        "vhodné na",
        "vhodné použití",
        "určeno pro",
        "určené pre",
        "použití",
        "použitie",
        "mletí",
        "mletie",
        "mletí zrn",
        "mletí kávy",
        "druh mletí",
        "způsob mletí",
        "hrubost namletí",
        "zomlieť kávu",
        "vhodné na přípravu",
        "ideální příprava",
        "ideální pro",
        "typ mletia",
        "stupeň mletí",
        "hrubost mletí",
        "hrubosť mletia",
        "hrúbka mletia",
        "stav kávy",
        "na jakou kávu",
        "hrubost kávy",
        "namelte",
        "kávu namelte na",
        "pomlet na",
        "chci kávu namlít",
        "chceš kávu namlít",
        "chcete kávu namlít",
        "zrnkovou nebo mletou",
        "forma kávy",
        "mletá / zrnková",
        "brewing",
        "preparation method",
    ),
    F_FLAVOR: (
        "chuť",
        "chutě",
        "chuťový profil",
        "chuťový profil kávy",
        "chuťové tóny",
        "chuťová charakteristika",
        "chutná jako",
        "typická chuť",
        "vůně",
        "chuť a tóny",
        "tóny",
        "dochuť",
        "podchuť",
        "profil",
        "senzorický profil",
        "charakteristika",
        "skupina chutí",
        "aroma",
        "flavor",
        "flavour",
        "flavor profile",
        "flavour profile",
        "taste profile",
        "tasting notes",
        "notes",
        "primary flavour note",
        "our baristas notes",
    ),
    F_SCA: (
        "sca",
        "sca skóre",
        "cupping",
        "cupping score",
        "cup score",
        "bodové hodnotenie",
        "sca cupping score",
        "cuppingové skóre",
        "cupping scóre",
        "skóre",
        "skóre kvality",
        "score",
    ),
    F_SPECIES: (
        "druh",
        "druh kávy",
        "typ kávy",
        "kávová zrna",
        "složení",
        "zloženie",
        "zloženie kávy",
        "zmes",
        "zloženie podľa druhu",
        "složení kávy",
        "blend",
        "poměr",
        "pomer zŕn",
        "arabica",
        "arabika",
        "robusta",
        "species",
    ),
    F_DECAF: (
        "bezkofeinová",
        "bezkofeinová káva",
        "obsah kofeinu",
        "kofeín",
        "metoda odstranění kofeinu",
        "decaf",
    ),
    F_WEIGHT: (
        "velikost balení",
        "veľkosť balenia",
        "velikost",
        "veľkosť",
        "balení",
        "balenie",
        "typ balení",
        "typ balenia",
        "obsah balenia",
        "obal",
        "gramáž",
        "množství",
        "váha",
        "váha balenia",
        "hmotnosť kávy",
        "hmotnost balení",
        "hmotnosť balenia",
        "obsah balení",
        "hmotnosť v gramoch",
        "gramy",
        "hmotnost produktu",
        "net weight",
        "package size",
        "package weight",
    ),
    F_CERTIFICATIONS: (
        "certifikace",
        "certifikácia",
        "certifikát",
        "certification",
    ),
    F_BODY: (
        "tělo",
        "telo",
        "body",
    ),
    F_ACIDITY: (
        "acidita",
        "kyselost",
        "kyslosť",
        "kyslosť kávy",
        "acidity",
    ),
    F_BITTERNESS: (
        "hořkost",
        "horkosť",
        "horkosť kávy",
        "bitterness",
    ),
    F_SWEETNESS: (
        "sladkost",
        "sladkosť",
        "sweetness",
    ),
    #: Terms a shop prints that carry no coffee data. Naming them keeps them out
    #: of the typed fields instead of letting a longer term match part of them.
    F_IGNORE: (
        "kategorie",
        "kategória",
        "ean",
        "doplňky",
        "doplnky",
        "rozměr",
        "rozmer",
        "potlač",
        "balíček",
        "skladování",
        "skladovanie",
        "uskladnění",
        "storage",
        "dostupnost",
        "dostupnosť",
        "můžeme doručit do",
        "môžeme doručiť do",
        "záruka",
        "kód produktu",
        "měrná cena",
        "specifické zboží",
        "výrobní společnost",
        "krajina praženia",
        "požadované vlastnosti",
        "obecné informace",
        "závěr",
        "importér",
        "energetická hodnota",
        "tuky",
        "sacharidy",
        "bílkoviny",
        "sůl",
        "z toho cukry",
        "z toho nasycené mastné kyseliny",
        "místo pražení",
        "miesto praženia",
        "podľa oblasti",
        "veľkosť drippera",
        "ocenenia",
        "zdroj",
    ),
}


def build_map(
    terms: dict[str, tuple[str, ...]],
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Fold a vocabulary into the lookup a label matcher uses.

    Args:
        terms: Canonical field to the terms shops print for it.
        extra: Folded term to field, applied last, for a platform's own reading
            of a term the shared vocabulary leaves out or reads differently.

    Returns:
        Folded term to canonical field.
    """
    folded = {normalize.fold(term): field for field, group in terms.items() for term in group}
    if extra:
        folded.update({normalize.fold(term): field for term, field in extra.items()})
    return folded


#: The shared vocabulary, ready for lookup. Platform adapters overlay their own
#: readings on top of this rather than keeping a second copy.
LABEL_MAP: Final[dict[str, str]] = build_map(TERMS)
