import re
import unicodedata
from dataclasses import dataclass, field

from casting_watch.models import MatchDecision, RawListing


GENDER_TERMS = {
    "fille": [
        "fille", "filles", "fillette", "fillettes", "comedienne", "comediennes",
        "actrice", "actrices", "danseuse", "danseuses", "chanteuse", "chanteuses",
        "femme", "femmes", "mannequin fille", "adolescente", "adolescentes", "collegienne", "collegiennes",
        "lyceenne", "lyceennes", "jeune fille",
    ],
    "garcon": [
        "garcon", "garcons", "petit gars", "homme", "hommes", "collegien", "collegiens", "lyceen", "lyceens",
        "jeune homme",
    ],
}

GENDER_LABELS = {"fille": "Féminin", "garcon": "Masculin", "": "Peu importe"}

# Categories d'age, a cocher ou non (aucune cochee = pas de filtre). Une annonce correspond si ses ages
# chevauchent une categorie cochee, ou, sans age precis, si elle emploie un mot de cette categorie.
CATEGORY_LABELS = {"enfant": "Enfant", "ado": "Ado", "adulte": "Adulte"}
CATEGORY_AGES = {"enfant": (0, 11), "ado": (12, 17), "adulte": (18, 99)}
CATEGORY_TERMS = {
    "enfant": ["enfant", "enfants", "fillette", "fillettes", "bebe", "bebes", "petite fille", "petit garcon", "kids"],
    "ado": [
        "ado", "ados", "adolescent", "adolescente", "adolescents", "adolescentes", "collegien", "collegienne",
        "collegiens", "lyceen", "lyceenne", "lyceens", "teen", "teenager",
    ],
    "adulte": ["adulte", "adultes", "femme", "femmes", "homme", "hommes"],
}

# Un mot-cle de lieu comme "Ile-de-France" est elargi a ses departements et grandes villes.
LOCATION_ALIASES = {
    "ile de france": [
        "ile de france", "idf", "region parisienne", "paris", "75", "77", "78", "91", "92", "93", "94", "95",
        "seine et marne", "yvelines", "essonne", "hauts de seine", "seine saint denis", "val de marne",
        "val d oise", "boulogne billancourt", "saint denis", "argenteuil", "montreuil", "nanterre",
        "vitry sur seine", "creteil", "versailles", "courbevoie", "colombes", "aubervilliers", "asnieres",
        "rueil malmaison", "aulnay sous bois", "champigny", "saint maur", "drancy", "issy les moulineaux",
        "noisy le grand", "levallois", "antony", "neuilly sur seine", "clichy", "sarcelles", "ivry sur seine",
        "cergy", "pontoise", "evry", "meaux", "melun", "puteaux", "la defense", "cachan", "vincennes",
        "pantin", "bobigny", "saint ouen", "montrouge", "massy", "palaiseau", "saint germain en laye",
        "poissy", "marne la vallee", "chessy", "fontainebleau", "rambouillet", "bagnolet", "villejuif",
        "saclay", "bry sur marne", "epinay sur seine", "longjumeau", "suresnes", "malakoff", "arcueil",
    ],
}
LOCATION_ALIASES["idf"] = LOCATION_ALIASES["ile de france"]
LOCATION_ALIASES["region parisienne"] = LOCATION_ALIASES["ile de france"]

# Sert seulement a savoir si une annonce indique un lieu (option "inclure les annonces sans lieu").
KNOWN_PLACES = LOCATION_ALIASES["ile de france"] + [
    "auvergne", "rhone alpes", "bourgogne", "franche comte", "bretagne", "centre val de loire", "corse",
    "grand est", "alsace", "lorraine", "champagne", "hauts de france", "nord", "picardie", "normandie",
    "aquitaine", "occitanie", "languedoc", "midi pyrenees", "pays de la loire", "provence", "paca",
    "cote d azur", "oise", "belgique", "suisse", "luxembourg", "monaco", "reunion", "guadeloupe",
    "martinique", "guyane", "mayotte", "lyon", "marseille", "toulouse", "nice", "nantes", "montpellier",
    "strasbourg", "bordeaux", "lille", "rennes", "reims", "toulon", "saint etienne", "le havre",
    "grenoble", "dijon", "angers", "nimes", "clermont ferrand", "aix en provence", "brest", "tours",
    "amiens", "limoges", "annecy", "perpignan", "metz", "besancon", "orleans", "rouen", "mulhouse",
    "caen", "nancy", "avignon", "cannes", "la rochelle", "pau", "bayonne", "biarritz", "troyes",
    "poitiers", "le mans", "lorient", "valence", "chambery", "roubaix", "bruxelles", "liege", "geneve",
    "lausanne", "haute saone", "savoie", "vendee", "gironde", "herault",
]
DEPARTMENT_OR_POSTCODE = re.compile(r"\((?:\d{2}|2a|2b|97\d)\)|(?<!\d)\d{5}(?!\d)(?!\s*(?:€|euros?|eur))")


def mentions_a_place(haystack: str) -> bool:
    """`haystack` deja passe par canonical()."""
    if DEPARTMENT_OR_POSTCODE.search(haystack):
        return True
    return any(term_in_text(place, haystack) for place in KNOWN_PLACES)

DEFAULT_EXCLUDE_TERMS = ["charme", "lingerie", "nudite", "nu integral", "erotique", "sexy"]

NO_CONSTRAINT = {"", "tous", "toutes", "peu importe", "indifferent", "toute la france", "france"}


def normalize_text(value: str) -> str:
    lowered = str(value).lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", without_accents).strip()


def canonical(value: str) -> str:
    """Comme normalize_text, mais tirets et apostrophes deviennent des espaces."""
    return re.sub(r"\s+", " ", re.sub(r"[-'’_/]", " ", normalize_text(value))).strip()


def term_in_text(term: str, text: str) -> bool:
    """`text` doit deja etre passe par canonical()."""
    needle = canonical(term)
    if not needle:
        return False
    if needle.isdigit():
        # Code departement : "(92)", "92800", "le 92" ; jamais "1975", "92 ans" ni "92 euros".
        pattern = rf"(?<!\d){needle}(?:\d{{3}})?(?!\d)(?!\s*(?:ans?|€|euros?|eur|%|h\b|min|cm|kg))"
        return re.search(pattern, text) is not None
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", text) is not None


def text_contains_any(text: str, terms: list) -> list[str]:
    haystack = canonical(text)
    return [str(term) for term in terms if term_in_text(str(term), haystack)]


AGE_RANGE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:-|a|au|et|/|–)\s*(\d{1,2})\s*ans\b")
AGE_PLUS = re.compile(r"(?:\+\s*|plus de\s*|des\s*)(\d{1,2})\s*ans\b|(?<!\d)(\d{1,2})\s*ans\s*(?:et\s*)?(?:\+|plus)")
AGE_SINGLE = re.compile(r"(?<!\d)(\d{1,2})\s*ans\b")


def extract_age_ranges(text: str) -> list[tuple[int, int]]:
    normalized = normalize_text(text)
    ranges: list[tuple[int, int]] = []
    consumed: list[tuple[int, int]] = []
    for match in AGE_RANGE.finditer(normalized):
        low, high = int(match.group(1)), int(match.group(2))
        if low <= high:
            ranges.append((low, high))
            consumed.append(match.span())
    for match in AGE_PLUS.finditer(normalized):
        value = int(match.group(1) or match.group(2))
        ranges.append((value, 99))
        consumed.append(match.span())
    for match in AGE_SINGLE.finditer(normalized):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        value = int(match.group(1))
        ranges.append((value, value))
    return ranges


@dataclass
class SearchCriteria:
    gender: str = ""            # "fille", "garcon" ou "" (peu importe)
    min_age: int | None = None  # None = pas de limite
    max_age: int | None = None
    location: str = ""          # "" = toute la France ; plusieurs lieux separes par des virgules
    include_unknown_age: bool = False
    include_unknown_location: bool = True
    categories: list[str] = field(default_factory=list)  # "enfant", "ado", "adulte" ; vide = pas de filtre
    types: list[str] = field(default_factory=list)  # "role", "figuration" ; vide = les deux

    @classmethod
    def from_config(cls, config: dict) -> "SearchCriteria":
        return cls.from_dict(config.get("search") or {})

    @classmethod
    def from_dict(cls, search: dict) -> "SearchCriteria":
        return cls(
            gender=clean_gender(search.get("gender")),
            min_age=clean_age(search.get("min_age")),
            max_age=clean_age(search.get("max_age")),
            location=clean_location(search.get("location")),
            include_unknown_age=bool(search.get("include_unknown_age", False)),
            include_unknown_location=bool(search.get("include_unknown_location", True)),
            categories=clean_categories(search.get("categories")),
            types=clean_types(search.get("types")),
        )

    def to_config(self) -> dict:
        return {
            "gender": self.gender,
            "min_age": self.min_age,
            "max_age": self.max_age,
            "location": self.location,
            "include_unknown_age": self.include_unknown_age,
            "include_unknown_location": self.include_unknown_location,
            "categories": list(self.categories),
            "types": list(self.types),
        }

    def describe(self) -> str:
        parts = [" et ".join(TYPE_LABELS[t].lower() for t in self.types)] if len(self.types) == 1 else []
        parts.append(GENDER_LABELS.get(self.gender, self.gender) if self.gender else "tous sexes")
        if self.categories:
            parts.append(" ou ".join(CATEGORY_LABELS[c].lower() for c in self.categories))
        if self.min_age is not None and self.max_age is not None:
            parts.append(f"{self.min_age}-{self.max_age} ans")
        elif self.min_age is not None:
            parts.append(f"{self.min_age} ans et plus")
        elif self.max_age is not None:
            parts.append(f"jusqu'à {self.max_age} ans")
        elif not self.categories:
            parts.append("tout âge")
        parts.append(self.location or "toute la France")
        return ", ".join(parts)


def clean_gender(value) -> str:
    text = canonical(value or "")
    if text in ("fille", "filles", "f", "feminin"):
        return "fille"
    if text in ("garcon", "garcons", "g", "masculin"):
        return "garcon"
    return ""


def clean_types(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    wanted = {canonical(item).replace("roles", "role") for item in (value or [])}
    return [key for key in TYPE_LABELS if key in wanted]


def clean_categories(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    wanted = {canonical(item) for item in (value or [])}
    return [key for key in CATEGORY_LABELS if key in wanted]


def clean_age(value) -> int | None:
    if value is None or str(value).strip() in ("", "0"):
        return None
    return int(value)


def clean_location(value) -> str:
    text = str(value or "").strip()
    return "" if canonical(text) in NO_CONSTRAINT else text


def location_terms(location: str) -> list[str]:
    terms: list[str] = []
    for part in location.split(","):
        key = canonical(part)
        if not key:
            continue
        terms.extend(LOCATION_ALIASES.get(key, [part.strip()]))
    return terms


# Type d'annonce : figuration (figurants, silhouettes) ou role (personnage a jouer). Une annonce peut chercher
# les deux. Termes « forts » : suffisent a eux seuls ; termes simples : comptent, surtout dans le titre.
TYPE_LABELS = {"role": "Rôles", "figuration": "Figuration"}
TYPE_TERMS = {
    "figuration": {
        "strong": ["figuration", "figurant", "figurants", "figurante", "figurantes", "tarif figuration", "silhouette",
                   "silhouettes", "journee de figuration"],
        "weak": [],
    },
    "role": {
        "strong": ["role principal", "roles principaux", "role secondaire", "roles secondaires", "premier role",
                   "second role", "tarif role", "tarif syndical role", "role tv", "role cinema", "role remunere",
                   "role d enfant", "personnage", "self tape", "selftape", "essai filme", "essais filmes"],
        "weak": ["role", "roles", "comedien", "comedienne", "comediens", "comediennes", "jouer", "interpreter"],
    },
}


def listing_types(title: str, text: str) -> list[str]:
    """Types d'une annonce : ["figuration"], ["role"], les deux, ou [] si on ne sait pas."""
    title_c, text_c = canonical(title), canonical(text)
    found = []
    for kind, terms in TYPE_TERMS.items():
        strong = any(term_in_text(t, title_c) or term_in_text(t, text_c) for t in terms["strong"])
        weak_in_title = any(term_in_text(t, title_c) for t in terms["weak"])
        weak_count = sum(term_in_text(t, text_c) for t in terms["weak"])
        if strong or weak_in_title or weak_count >= 2:
            found.append(kind)
    # « role de figurant » ou « le role de la figuration » : c'est de la figuration.
    if found == ["figuration", "role"] and not any(
        term_in_text(t, f"{title_c} {text_c}") for t in TYPE_TERMS["role"]["strong"]
    ) and not any(term_in_text(t, title_c) for t in TYPE_TERMS["role"]["weak"]):
        found = ["figuration"]
    return [kind for kind in TYPE_LABELS if kind in found]


def category_match(text: str, haystack: str, categories: list[str]) -> str | bool | None:
    """Nom de la categorie qui correspond, False si l'annonce vise seulement d'autres categories,
    None si elle ne dit rien d'utile (ni age, ni mot comme « enfant » ou « ado »)."""
    ranges = extract_age_ranges(text)
    if ranges:
        for category in categories:
            low, high = CATEGORY_AGES[category]
            if any(a <= high and b >= low for a, b in ranges):
                return CATEGORY_LABELS[category].lower()
        return False
    mentioned = [c for c, terms in CATEGORY_TERMS.items() if any(term_in_text(t, haystack) for t in terms)]
    wanted = [c for c in categories if c in mentioned]
    if wanted:
        return CATEGORY_LABELS[wanted[0]].lower()
    return False if mentioned else None


def evaluate_listing(
    listing: RawListing,
    criteria: SearchCriteria,
    exclude_terms: list | None = None,
) -> MatchDecision:
    combined = f"{listing.title}\n{listing.text}"
    haystack = canonical(combined)

    excluded = [str(t) for t in (exclude_terms if exclude_terms is not None else DEFAULT_EXCLUDE_TERMS) if term_in_text(str(t), haystack)]
    if excluded:
        return MatchDecision(False, -10, [f"exclue: {', '.join(excluded)}"])

    reasons: list[str] = []
    score = 0

    if criteria.types:
        kinds = listing_types(listing.title, listing.text)
        if kinds and not set(kinds) & set(criteria.types):
            return MatchDecision(False, 0, [f"type: seulement {TYPE_LABELS[kinds[0]].lower()}"])
        reasons.append("type: " + (" et ".join(TYPE_LABELS[k].lower() for k in kinds) if kinds else "non précisé"))

    if criteria.gender:
        other = "garcon" if criteria.gender == "fille" else "fille"
        wanted_hits = [t for t in GENDER_TERMS[criteria.gender] if term_in_text(t, haystack)]
        other_hits = [t for t in GENDER_TERMS[other] if term_in_text(t, haystack)]
        if other_hits and not wanted_hits:
            return MatchDecision(False, 0, [f"sexe: seulement {GENDER_LABELS[other].lower()}"])
        if wanted_hits:
            score += 2
            reasons.append(f"sexe: {wanted_hits[0]}")
        else:
            reasons.append("sexe: non precise")

    if criteria.min_age is not None or criteria.max_age is not None:
        low = criteria.min_age if criteria.min_age is not None else 0
        high = criteria.max_age if criteria.max_age is not None else 99
        ranges = extract_age_ranges(combined)
        matching = list(dict.fromkeys(r for r in ranges if r[0] <= high and r[1] >= low))
        if matching:
            score += 4
            reasons.append("age: " + ", ".join(f"{a}-{b} ans" if a != b else f"{a} ans" for a, b in matching[:3]))
        elif ranges or not criteria.include_unknown_age:
            return MatchDecision(False, 0, ["age: hors tranche" if ranges else "age: non indique"])
        else:
            reasons.append("age: non indique")

    if criteria.categories:
        verdict = category_match(combined, haystack, criteria.categories)
        if verdict is False:
            return MatchDecision(False, 0, ["catégorie: ne correspond pas"])
        if verdict is None and not criteria.include_unknown_age:
            return MatchDecision(False, 0, ["catégorie: âge non indiqué"])
        if verdict:
            score += 2
            reasons.append(f"catégorie: {verdict}")
        else:
            reasons.append("catégorie: non indiquée")

    if criteria.location:
        hits = [t for t in location_terms(criteria.location) if term_in_text(t, haystack)]
        if hits:
            score += 3
            reasons.append(f"lieu: {', '.join(hits[:3])}")
        elif criteria.include_unknown_location and not mentions_a_place(haystack):
            reasons.append("lieu: non indique")
        else:
            return MatchDecision(False, 0, ["lieu: ne correspond pas"])

    return MatchDecision(True, score, reasons)
