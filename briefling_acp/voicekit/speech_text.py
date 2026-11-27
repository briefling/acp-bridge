"""Text the French TTS can read: numbers written out in words.

Pocket TTS reads digits badly (« 0,30 $ » came out as noise with gaps up to 1.4 s), while the same sentence in words
reads fine. The daemon runs `normalize_fr` on each sentence before it queues it for the TTS and before it records it
for the echo check, so the words compared with the microphone are the words actually spoken.
Rules run in this order, each one consuming what the next ones would misread: versions, amounts, `#n`, ordinals,
times, units and %, decimals, integers.
"""
import re

from num2words import num2words

UNITS = {"s": "secondes", "ms": "millisecondes", "min": "minutes", "h": "heures", "Go": "gigaoctets",
         "Mo": "mégaoctets", "Ko": "kilooctets", "Hz": "hertz", "kHz": "kilohertz", "dB": "décibels", "%": "pour cent"}
CURRENCY = {"$": ("dollars", "dollar"), "€": ("euros", "euro")}
_UNIT_RE = "|".join(sorted(map(re.escape, UNITS), key=len, reverse=True))


def _w(n) -> str:
    return num2words(int(n), lang="fr")


def _decimal(s: str) -> str:
    """« 1,3 » -> « un virgule trois », « 1.05 » -> « un virgule zéro cinq » (leading zeros read digit by digit)."""
    i, f = re.split(r"[.,]", s)
    frac = " ".join(_w(c) for c in f) if f.startswith("0") and f.strip("0") else _w(f)
    return f"{_w(i)} virgule {frac}"


def _amount(units: str, cents: str, sym: str) -> str:
    plural, single = CURRENCY[sym]
    u, c = int(units), int(cents.ljust(2, "0")) if cents else 0
    parts = [f"{_w(u)} {single if u == 1 else plural}"] if u or not c else []
    if c:
        parts.append(f"{_w(c)} {'centime' if c == 1 else 'centimes'}")
    return " et ".join(parts)


def _time(h: str, m: str) -> str:
    out = f"{_w(h)} {'heure' if int(h) == 1 else 'heures'}"
    return f"{out} {_w(m)}" if m and int(m) else out


def normalize_fr(text: str) -> str:
    t = re.sub(r"\bv?(\d+(?:\.\d+){2,})\b", lambda m: " point ".join(_w(p) for p in m.group(1).split(".")), text)
    t = re.sub(r"(\d+)(?:[,.](\d{1,2}))?\s?([$€])", lambda m: _amount(m.group(1), m.group(2), m.group(3)), t)
    t = re.sub(r"#(\d+)", lambda m: f"numéro {_w(m.group(1))}", t)
    t = re.sub(r"\b(\d+)(er|re|e|ème)\b", lambda m: ("premier" if m.group(2) == "er" else "première")
               if m.group(1) == "1" else num2words(int(m.group(1)), lang="fr", to="ordinal"), t)
    t = re.sub(r"\b([01]?\d|2[0-3])h([0-5]\d)?\b", lambda m: _time(m.group(1), m.group(2)), t)
    t = re.sub(rf"(\d+(?:[,.]\d+)?)\s?({_UNIT_RE})(?!\w)",
               lambda m: f"{_decimal(m.group(1)) if re.search('[,.]', m.group(1)) else _w(m.group(1))} "
                         f"{UNITS[m.group(2)]}", t)
    t = re.sub(r"\d+[,.]\d+", lambda m: _decimal(m.group(0)), t)
    return re.sub(r"\d+", lambda m: _w(m.group(0)), t)
