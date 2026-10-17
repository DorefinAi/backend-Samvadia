"""Hindi (Devanagari) to plain Latin letters, for slug keywords.

पर्यटन -> paryatan, जयपुर -> jaypur, बारिश -> barish.
Long and short vowels collapse (आ and अ both give "a") because slugs are uppercase ASCII.
The inherent "a" is dropped at word end and in the usual Hindi middle position
(between a vowel+consonant and a consonant+vowel), so कमला gives kamla, not kamala.
"""
import re

_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "ळ": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
}
# Letters with a nukta, precomposed or written as letter + ़
_NUKTA = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f", "य": "y"}
_PRECOMPOSED_NUKTA = {
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z",
    "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y",
}
_VOWELS = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e",
}
_MATRAS = {
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}
_VIRAMA = "्"
_NUKTA_SIGN = "़"
_NASALS = {"ँ": "n", "ं": "n"}  # chandrabindu, anusvara
_VISARGA = "ः"
_DIGITS = {chr(0x0966 + d): str(d) for d in range(10)}

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text))


def _tokens(word: str) -> list[list]:
    """Each token is [kind, latin, inherent]; kind is C (consonant), V (vowel) or L (other)."""
    out: list[list] = []
    i = 0
    while i < len(word):
        ch = word[i]
        nxt = word[i + 1] if i + 1 < len(word) else ""
        if word.startswith("ज्ञ", i):
            out += [["C", "gy", False], ["V", "a", True]]
            i += 3
            continue
        if ch in _CONSONANTS or ch in _PRECOMPOSED_NUKTA:
            latin = _PRECOMPOSED_NUKTA.get(ch) or _CONSONANTS[ch]
            if nxt == _NUKTA_SIGN:
                latin = _NUKTA.get(ch, latin)
                i += 1
                nxt = word[i + 1] if i + 1 < len(word) else ""
            out.append(["C", latin, False])
            if nxt in _MATRAS:
                out.append(["V", _MATRAS[nxt], False])
                i += 1
            elif nxt == _VIRAMA:
                i += 1
            else:
                out.append(["V", "a", True])
        elif ch in _VOWELS:
            out.append(["V", _VOWELS[ch], False])
        elif ch in _NASALS:
            out.append(["C", _NASALS[ch], False])
        elif ch == _VISARGA:
            out.append(["C", "h", False])
        elif ch in _DIGITS:
            out.append(["L", _DIGITS[ch], False])
        elif ch == _NUKTA_SIGN or ch in _MATRAS or ch == _VIRAMA:
            pass  # stray sign
        else:
            out.append(["L", ch, False])
        i += 1
    return out


def _drop_schwas(tokens: list[list]) -> list[list]:
    vowels = sum(1 for t in tokens if t[0] == "V")
    if vowels > 1 and tokens and tokens[-1][0] == "V" and tokens[-1][2]:
        tokens = tokens[:-1]
    # Right to left: drop an inherent a in V C _a_ C V.
    for idx in range(len(tokens) - 3, 1, -1):
        t = tokens[idx]
        if not (t[0] == "V" and t[2]):
            continue
        if tokens[idx - 1][0] == "C" and tokens[idx - 2][0] == "V" and tokens[idx + 1][0] == "C" and tokens[idx + 2][0] == "V":
            tokens = tokens[:idx] + tokens[idx + 1:]
    return tokens


def transliterate_word(word: str) -> str:
    return "".join(t[1] for t in _drop_schwas(_tokens(word)))


def transliterate(text: str) -> str:
    return " ".join(transliterate_word(w) for w in text.split())
