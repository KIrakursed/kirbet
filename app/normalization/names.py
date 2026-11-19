"""Name / category normalisation."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

_ALIASES_FILE = Path(__file__).resolve().parents[2] / "config" / "aliases.json"

_TEAM_NOISE = {
    "team", "esports", "esport", "e-sports", "gaming", "club", "gg", "fc", "academy",
    "the", "clan", "org", "official", "фк", "fk", "cf", "sc",
    # gender / youth / reserve markers are compared separately by team_markers()
    "ж", "жен", "w", "women", "womens", "мол", "рез", "reserve", "reserves", "ii", "youth",
}

SPORT_FAMILY = {
    "atp": "tennis", "wta": "tennis", "tennis": "tennis", "challenger": "tennis",
    "itf": "tennis",
    "cs2": "cs2", "csgo": "cs2", "counter-strike": "cs2", "counter strike": "cs2",
    "dota2": "dota2", "dota 2": "dota2", "dota": "dota2",
    "lol": "lol", "league of legends": "lol",
    "val": "valorant", "valorant": "valorant",
    "sc2": "sc2", "starcraft": "sc2", "starcraft ii": "sc2",
    "volleyball": "volleyball", "football": "football", "soccer": "football",
}


@lru_cache(maxsize=200_000)
def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


@lru_cache(maxsize=200_000)
def basic(s: str) -> str:
    s = strip_accents(s or "").lower().replace("ё", "е")
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9а-яё ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    out: dict[str, str] = {}
    if _ALIASES_FILE.exists():
        data = json.loads(_ALIASES_FILE.read_text(encoding="utf-8"))
        for canonical, alist in data.items():
            c = basic(canonical)
            out[c] = c
            for a in alist:
                out[basic(a)] = c
    return out


def canonical_name(s: str) -> str:
    b = basic(s)
    return _aliases().get(b, b)


@lru_cache(maxsize=200_000)
def team_key(s: str) -> str:
    toks = [t for t in canonical_name(s).split() if t not in _TEAM_NOISE]
    return " ".join(toks) or canonical_name(s)


@lru_cache(maxsize=200_000)
def _person_parts(s: str) -> tuple[str, str]:
    """(surname, first-initial) best effort for 'A. Zverev', 'Zverev A.', 'Alexander Zverev'."""
    raw = strip_accents(s).replace(".", ". ").split()
    toks = [basic(t) for t in raw if basic(t)]
    if not toks:
        return "", ""
    if len(toks) == 1:
        return toks[0], ""
    # 'Zverev A.' style -> last token is an initial
    if len(toks[-1]) == 1:
        return " ".join(toks[:-1]), toks[-1]
    return toks[-1], toks[0][0]


_W = r"(?<![a-zа-я0-9])"  # word start that also works for Cyrillic
_E = r"(?![a-zа-я0-9])"  # word end
_MARKER_RX = [
    ("women", re.compile(rf"\((ж|жен|w|zh|zhen)\)|{_W}women('s)?{_E}|{_W}женщины{_E}|{_W}жен{_E}", re.I)),
    ("youth", re.compile(rf"{_W}u-?\d{{2}}{_E}|\((мол|mol)\)|{_W}молод|{_W}youth{_E}", re.I)),
    ("reserve", re.compile(rf"\((рез|rez)\)|{_W}резерв|{_W}reserves?{_E}|{_W}ii{_E}", re.I)),
    ("amateur", re.compile(rf"\((люб|lyub|liub)\)|{_W}любител|{_W}amateurs?{_E}|\(am\)", re.I)),
]


@lru_cache(maxsize=200_000)
def team_markers(name: str) -> frozenset[str]:
    """Women / youth / reserve markers — teams with different markers are different teams."""
    out = {tag for tag, rx in _MARKER_RX if rx.search(name or "")}
    m = re.search(rf"{_W}u-?(\d{{2}}){_E}", name or "", re.I)
    if m:
        out.add(f"u{m.group(1)}")
    return frozenset(out)


@lru_cache(maxsize=500_000)
def similarity(a: str, b: str, *, person: bool = False) -> float:
    """0..1 similarity of two participant names."""
    if not person and team_markers(a) != team_markers(b):
        return 0.0
    ca, cb = canonical_name(a), canonical_name(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    ta, tb = team_key(a), team_key(b)
    if ta and ta == tb:
        return 0.98
    score = fuzz.token_sort_ratio(ta, tb) / 100.0
    if has_cyrillic(a) != has_cyrillic(b):
        score = max(score, _cross_script(a, b, person))
    if person:
        sa, ia = _person_parts(a)
        sb, ib = _person_parts(b)
        if sa and sa == sb and (not ia or not ib or ia == ib):
            score = max(score, 0.95)
        elif _persons_equal(a, b):
            score = max(score, 0.95)  # "Ben Shelton" = "Шелтон Бен" = "Shelton B."
        elif sa and sb and sa != sb:
            # surnames differ: penalise strongly (Murray vs Murphy ...)
            score = min(score, fuzz.ratio(sa, sb) / 100.0 * 0.9)
    return score


# ---- Russian ↔ English matching helpers -----------------------------------------------
# Words that are spelled differently in the two languages (not transliterations).
_WORD_MAP = {
    "moskva": "moscow", "moskvy": "moscow", "sankt": "saint", "peterburg": "petersburg", "sp": "saint petersburg",
    "spb": "saint petersburg", "st": "saint", "kiev": "kyiv", "kiyev": "kyiv", "kiyv": "kyiv",
    "myunkhen": "munich", "munkhen": "munich", "muenchen": "munich", "praga": "prague", "varshava": "warsaw",
    "rim": "rome", "neapol": "naples", "london": "london", "sevilya": "sevilla", "seville": "sevilla",
}
# City / region words that a bookmaker may append to a club name ("Akron Tolyatti" = "Акрон").
_CITY_WORDS = {
    "moscow", "saint", "petersburg", "tolyatti", "togliatti", "kazan", "samara", "kaliningrad", "krasnodar",
    "sochi", "grozny", "groznyi", "makhachkala", "orenburg", "nizhny", "novgorod", "yekaterinburg", "perm",
    "almaty", "astana", "karaganda", "shymkent", "aktobe", "kostanay", "pavlodar", "munich", "madrid", "milan",
    "london", "manchester", "lisbon", "prague", "kyiv", "minsk", "tbilisi", "baku", "yerevan", "tashkent",
}


# Club names that exist in many cities: "Dinamo" alone must never absorb "Dinamo Minsk".
_GENERIC_CLUBS = {"dinamo", "spartak", "lokomotiv", "torpedo", "sparta", "real", "atletico", "sporting",
                  "olimpik", "olimpic", "shahter", "metalurg", "neftchi", "energetik", "krilia", "ural", "saturn",
                  "arsenal", "union", "national", "nacional", "universidad", "ska", "cska", "avangard"}


def _phon(t: str) -> str:
    t = " ".join(_WORD_MAP.get(w, w) for w in t.split())
    t = t.replace("kh", "h").replace("ya", "ia").replace("yu", "iu").replace("ye", "e").replace("y", "i")
    return re.sub(r"(.)+", r"", t)  # double letters


@lru_cache(maxsize=200_000)
def latin_keys(s: str) -> frozenset[str]:
    """Phonetic Latin keys of a name in either script (for RU↔EN comparison only)."""
    t = _phon(translit(basic(s)) if has_cyrillic(s) else basic(s))
    keys = {t, t.replace("j", "h"), t.replace("j", "i"), t.replace("x", "ks")}  # Jodar=Ходар, Juventus=Ювентус
    return frozenset(" ".join(w for w in k.split() if w not in _TEAM_NOISE) or k for k in keys)


def _cross_script(a: str, b: str, person: bool) -> float:
    best = 0.0
    for ka in latin_keys(a):
        for kb in latin_keys(b):
            if person:
                continue  # persons are compared token by token in _persons_equal
            best = max(best, fuzz.token_sort_ratio(ka, kb) / 100.0)
            wa, wb = set(ka.split()), set(kb.split())
            # one side only adds a city word: "akron toliati" vs "akron"
            small, big = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
            if (small and small < big and all(len(w) >= 4 for w in small) and not small & _GENERIC_PHON
                    and (big - small) <= _CITY_PHON):
                best = max(best, 0.92)
    return best


_CITY_PHON = frozenset(_phon(c) for c in _CITY_WORDS)
_GENERIC_PHON = frozenset(_phon(c) for c in _GENERIC_CLUBS)


def _persons_equal(a: str, b: str) -> bool:
    """Same player in any of the formats bookmakers use: 'Ben Shelton', 'Shelton B.', 'Шелтон Бен'."""
    for ka in latin_keys(a):
        for kb in latin_keys(b):
            ta, tb = ka.split(), kb.split()
            if not ta or not tb:
                continue
            for sa in {ta[0], ta[-1]}:
                for sb in {tb[0], tb[-1]}:
                    if len(sa) < 3 or len(sb) < 3 or fuzz.ratio(sa, sb) < 92:
                        continue
                    ra = [w for w in ta if w != sa]
                    rb = [w for w in tb if w != sb]
                    if not ra or not rb:
                        return True
                    if len(ra[0]) == 1 or len(rb[0]) == 1:  # "Shelton B." — only the initial is known
                        if ra[0][0] == rb[0][0]:
                            return True
                    elif fuzz.ratio(ra[0], rb[0]) >= 92:  # both full: the other name must agree too
                        return True  # (Andy Murray ≠ Andy Murphy although "Andy" = "Andy")
    return False


@lru_cache(maxsize=200_000)
def sport_family(code: str) -> str:
    c = basic(code)
    return SPORT_FAMILY.get(c, c)


_BO_RE = re.compile(r"\bbo\s?([1-7])\b", re.I)


def parse_best_of(text: str) -> int | None:
    m = _BO_RE.search(text or "")
    return int(m.group(1)) if m else None


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya", "ё": "e", "і": "i", "ә": "a", "ғ": "g", "қ": "k", "ң": "n", "ө": "o", "ұ": "u",
    "ү": "u", "һ": "h",
}


def has_cyrillic(s: str) -> bool:
    return any("а" <= ch <= "я" or ch == "ё" for ch in (s or "").lower())


@lru_cache(maxsize=200_000)
def translit(s: str) -> str:
    """Rough Russian→Latin transliteration used only as an extra matching variant."""
    return "".join(_TRANSLIT.get(ch, ch) for ch in (s or "").lower())


def name_variants(name: str, alt: str | None = None) -> list[str]:
    return list(_name_variants(name, alt))


@lru_cache(maxsize=200_000)
def _name_variants(name: str, alt: str | None) -> tuple[str, ...]:
    out = [name]
    if alt and alt not in out:
        out.append(alt)
    for n in list(out):
        if has_cyrillic(n):
            out.append(translit(n))
    return tuple(out)
