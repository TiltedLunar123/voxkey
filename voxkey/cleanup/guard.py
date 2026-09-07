"""Checks on what the rewriting model hands back.

A small instruct model is fast and usually faithful, and every so often it is
neither. Two failures were caught in real dictations on the machine this was
built on:

* It swapped one of its own worked examples into the output. A dictation that
  began "Once you figure it out, do it, and then merge it" came back as "Once
  the auth file is modified to increase the timeout from thirty seconds", which
  is the example the Prompt profile ships to teach it the style. Three runs out
  of three did it, and nobody had said a word about an auth file.
* It returned the first half of an instruction, nicely polished, and dropped
  the second half.

Both are cheap to detect after the fact. The pipeline runs every rewrite past
these checks, retries once without the examples when something is off, and
otherwise falls back to the rule-based cleanup rather than paste words the
user never said.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-z0-9']+")

# Words too common to say anything about whether a rewrite kept its meaning.
STOPWORDS = frozenset("""
a about above after again all also an and any are as at be because been before
being below between both but by can could did do does doing don down during each
few for from further get go going gonna got had has have having he her here hers
him his how i if in into is it its itself just know like lot lots make made me
more most my no nor not now of off on once one only or other our ours out over
own said same say she should so some something such than that the their theirs
them then there these they thing things this those through to too two under
until up us very want was way we were what when where which while who whom why
will with would yeah yes you your yours okay ok well right kind sort three
dont doesnt didnt cant cannot couldnt wouldnt shouldnt wont isnt arent wasnt
werent im ive youre youve theyre weve thats whats theres lets
""".split())


def words(text: str) -> list[str]:
    # Apostrophes go, so "can't" and "cant" and "cannot" line up as the near
    # matches they are instead of three different words.
    return _WORD.findall(text.lower().replace("'", "").replace("’", ""))


def content_words(text: str) -> set[str]:
    return {w for w in words(text) if len(w) >= 3 and w not in STOPWORDS}


def _ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def leaked_phrases(source: str, output: str, examples: list[str], n: int = 3) -> list[str]:
    """Runs of `n` words from a worked example that turned up in the output
    without ever being in the source. Empty means clean."""
    source_grams = _ngrams(words(source), n)
    output_grams = _ngrams(words(output), n)
    found: list[str] = []
    for example in examples:
        for gram in _ngrams(words(example), n):
            # "were going to" is in half the sentences ever written; only a
            # run with a real word in it says anything about copying.
            if all(word in STOPWORDS for word in gram):
                continue
            if gram in output_grams and gram not in source_grams:
                found.append(" ".join(gram))
    return sorted(set(found))


@dataclass
class Fidelity:
    ok: bool
    reason: str = ""
    coverage: float = 1.0
    novel: list[str] = field(default_factory=list)


# Per profile: the share of the source's content words that must survive, the
# number of invented words needed before the rewrite counts as invention, and
# the share of the source those must amount to. The tone profiles are allowed
# to reword; Prompt and Grammar are meant to keep what was said.
#
# The floors come from a hundred stored rewrites on the machine this was
# built on. The lowest one that was actually fine kept 0.75 of its words; the
# one that dropped the second half of an instruction kept 0.69.
_LIMITS: dict[str, tuple[float, int, float]] = {
    "grammar": (0.78, 4, 0.20),
    "prompt": (0.72, 5, 0.20),
}
_LOOSE = (0.50, 6, 0.30)
MIN_WORDS = 8   # below this a rewrite is too short to judge by counting


def check_fidelity(source: str, output: str, profile: str) -> Fidelity:
    """Did the rewrite keep the words that carry the meaning?

    Spelling corrections and inflections are forgiven: a word in the output
    counts as kept if it is close to something in the source, so "erorr" to
    "error" is not an invention and "seen" to "saw" is not a loss. So is a
    longer form of a word already there, "app" to "application".

    The same bar applies to the retry without examples. It was tempting to go
    easier on that pass, since it cannot copy an example, but the one real
    case it was measured on came back as a paraphrase that changed the
    meaning, and the rules were the better fallback.
    """
    kept_floor, novel_count, novel_share = _LIMITS.get(profile, _LOOSE)
    source_words = content_words(source)
    output_words = content_words(output)
    if len(source_words) < MIN_WORDS:
        return Fidelity(True, coverage=1.0)

    def close(word: str, pool: set[str]) -> bool:
        if word in pool or difflib.get_close_matches(word, pool, n=1, cutoff=0.8):
            return True
        return any(
            (other.startswith(word) or word.startswith(other)) and min(len(word), len(other)) >= 3
            for other in pool
        )

    kept = sum(1 for w in source_words if close(w, output_words))
    coverage = kept / len(source_words)
    novel = sorted(w for w in output_words if not close(w, source_words))

    if coverage < kept_floor:
        lost = int(round((1 - coverage) * 100))
        return Fidelity(False, f"it dropped about {lost}% of what was said", coverage, novel)
    if len(novel) >= novel_count and len(novel) >= novel_share * len(source_words):
        sample = ", ".join(novel[:4])
        return Fidelity(False, f"it added words that were never said ({sample})", coverage, novel)
    return Fidelity(True, coverage=coverage, novel=novel)


# -- recogniser loops --------------------------------------------------------

# Whisper occasionally gets stuck and emits the same word or phrase over and
# over. A single word has to repeat five times before it is touched, because
# "no no no no" is something people actually say; a phrase only needs three.
_TOKEN = r"[\w'’-]+"
_GAP = r"[ ,.]+"
_WORD_LOOP = re.compile(
    rf"(?<![\w'])({_TOKEN})(?:{_GAP}\1(?![\w'])){{4,}}", re.IGNORECASE
)
_PHRASE_LOOP = re.compile(
    rf"(?<![\w'])((?:{_TOKEN}{_GAP}){{1,7}}{_TOKEN})(?:{_GAP}\1(?![\w'])){{2,}}", re.IGNORECASE
)


def collapse_loops(text: str) -> tuple[str, int]:
    """Fold a runaway repetition down to one copy. Returns (text, how many)."""
    count = 0

    def once(match: re.Match) -> str:
        nonlocal count
        count += 1
        return match.group(1)

    # Single words first, or "no no no no no no" reads as three copies of the
    # phrase "no no" and one copy survives.
    text = _WORD_LOOP.sub(once, text)
    text = _PHRASE_LOOP.sub(once, text)
    return text, count
