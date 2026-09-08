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
    without ever being in the source. Empty means clean.

    A run only counts as copied when it brings two or more content words the
    speaker never used. Without that condition the check fires on its own
    examples working: the Grammar profile demonstrates "the report was written
    by Sarah", so a correctly fixed "was wrote" came back "leaking" the phrase
    it was supposed to produce and got thrown away in favour of a slower,
    worse retry. One unfamiliar word is what a correction looks like, because
    "wrote" to "written" and "seen" to "saw" share no letters to match on.

    The failure this exists to catch looked nothing like that. A dictation
    about merging a branch came back discussing an auth file and a thirty
    second timeout, and phrases that carry a whole idea nobody mentioned bring
    a whole idea's worth of new words with them: "the auth file" and "increase
    the timeout" are two apiece. Anything subtler than that is left to the
    fidelity check, which is measuring the same drift from the other side.
    """
    source_grams = _ngrams(words(source), n)
    output_grams = _ngrams(words(output), n)
    source_content = content_words(source)
    found: list[str] = []
    for example in examples:
        for gram in _ngrams(words(example), n):
            # "were going to" is in half the sentences ever written; only a
            # run with a real word in it says anything about copying.
            if all(word in STOPWORDS for word in gram):
                continue
            if gram not in output_grams or gram in source_grams:
                continue
            novel = [
                word for word in gram
                if word not in STOPWORDS and len(word) >= 3 and not _near(word, source_content)
            ]
            if len(novel) >= 2:
                found.append(" ".join(gram))
    return sorted(set(found))


def _near(word: str, pool: set[str]) -> bool:
    """Is this word already in the source, allowing for the rewrite having
    corrected its spelling or inflection? "servers" covers "server"."""
    if word in pool:
        return True
    if difflib.get_close_matches(word, pool, n=1, cutoff=0.8):
        return True
    return any(
        (other.startswith(word) or word.startswith(other)) and min(len(word), len(other)) >= 4
        for other in pool
    )


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


# -- paraphrasing ------------------------------------------------------------

# A paraphrase is the one rewrite that is supposed to change the words, so
# check_fidelity's coverage floor would reject every good one. What has to
# survive instead is everything the reader would have no way of checking: the
# names, the numbers, and roughly how much was said.
PARAPHRASE_SHORTEST = 0.55
PARAPHRASE_LONGEST = 1.9
# Below this a sentence is too short for the length band to mean anything.
PARAPHRASE_MIN_WORDS = 6
# How many of the source's three word runs may come back untouched before the
# result is an echo rather than a rewording. Measuring phrasing rather than
# vocabulary is the point: a paraphrase is supposed to keep the nouns.
PARAPHRASE_MAX_ECHO = 0.75


def phrasing_overlap(source: str, output: str) -> float:
    """The share of the source's word trigrams that survived verbatim."""
    a = _ngrams(words(source), 3)
    if not a:
        return 0.0
    return len(a & _ngrams(words(output), 3)) / len(a)

_PROPER = re.compile(r"(?<![.!?]\s)(?<!^)(?<![\"'(\[])\b([A-Z][a-zA-Z'’-]{2,})")


def proper_nouns(text: str) -> set[str]:
    """Capitalised words that are not just starting a sentence.

    Whatever else a paraphrase does, it does not get to rename people,
    products or places, and those are exactly the words a reader skimming the
    result would never think to check.
    """
    found = set()
    for line in text.split("\n"):
        for match in _PROPER.finditer(line):
            word = match.group(1)
            if word.lower() not in STOPWORDS:
                found.add(word)
    return found


def check_paraphrase(source: str, output: str) -> Fidelity:
    """Did the paraphrase say the same thing in different words?

    Three ways it can fail and one of them is not obvious: a model handed a
    short line often returns it verbatim, which is not a paraphrase, it is a
    round trip that cost the user a second and taught them the feature does
    not work. That counts as a failure here so the caller can say so.
    """
    source_words = words(source)
    output_words = words(output)
    if not output_words:
        return Fidelity(False, "it came back empty")

    if " ".join(source_words) == " ".join(output_words):
        return Fidelity(False, "it handed back the same words")

    lost = sorted(proper_nouns(source) - proper_nouns(output))
    # An acronym spelled out, or a name that moved into a possessive, is fine;
    # only a name that vanished from the text entirely is a problem.
    lost = [name for name in lost if name.lower() not in " ".join(output_words)]
    if lost:
        return Fidelity(False, f"it dropped a name ({', '.join(lost[:3])})")

    if len(source_words) >= PARAPHRASE_MIN_WORDS:
        ratio = len(output_words) / len(source_words)
        if ratio < PARAPHRASE_SHORTEST:
            return Fidelity(False, f"it cut it down to {int(ratio * 100)}% of the length")
        if ratio > PARAPHRASE_LONGEST:
            return Fidelity(False, f"it padded it out to {int(ratio * 100)}% of the length")

        echo = phrasing_overlap(source, output)
        if echo > PARAPHRASE_MAX_ECHO:
            return Fidelity(False, f"it changed almost nothing ({int(echo * 100)}% word for word)")

    return Fidelity(True, coverage=1.0)


# -- literals the rewrite is not allowed to touch ----------------------------

# Things whose exact characters are the whole point. A rewrite that tidies the
# prose around one of these and quietly edits it has done more harm than the
# comma it fixed was worth: seen in a benchmark run as "we cut it to 400 ms"
# coming back as "40-0 milliseconds", and a wrong port or a wrong file name is
# not something the reader can spot afterwards.
_ATOM = re.compile(
    r"`[^`]+`"                                   # inline code
    r"|<[^<>\s]+@[^<>\s]+>|[\w.+-]+@[\w-]+\.[\w.-]+"   # email addresses
    r"|https?://\S+|www\.\S+"                    # urls
    r"|[A-Za-z]:[\\/][^\s]+"                     # windows paths
    r"|(?<![\w.])\.{0,2}/[\w./-]{2,}"            # unix paths and ./ ../
    r"|(?<![\w/])[\w-]+(?:/[\w.-]+)+"            # a/b/c.py
    # A number and its unit are one fact. Left apart, a paraphrase of "we cut
    # it to 400 ms" came back "the timeout is 400", which reads as a complete
    # sentence and is missing the only part that mattered. Longest unit first,
    # so "seconds" is not matched as "sec"; and the lookahead here refuses a
    # following letter only, because "400 ms." at the end of a sentence is the
    # ordinary case and refusing a following full stop missed all of them.
    # Case folded for the unit only: MB, GB and Hz are written that way as
    # often as not, and the branches around this one are already explicit
    # about case.
    r"|(?<![\w.])\d+(?:[.:,]\d+)*\s?(?i:" + "|".join(sorted((
        "ms", "s", "sec", "secs", "seconds", "min", "mins", "minutes", "hrs",
        "hours", "days", "weeks", "months", "years",
        "kb", "mb", "gb", "tb", "kib", "mib", "gib", "bytes",
        "px", "pt", "em", "rem", "mm", "cm", "km", "kg", "lb", "lbs", "oz",
        "hz", "khz", "mhz", "ghz", "fps", "rpm", "wpm", "%",
    ), key=len, reverse=True)) + r")(?!\w)"
    r"|(?<![\w.])\d+(?:[.:,]\d+)*(?![\w.])"      # numbers, times, versions
    r"|(?<![\w.])[\w-]+\.[a-zA-Z]{1,5}(?![\w.])" # settings.json, auth.ts
)

# Numbers small enough that the model may legitimately spell them out, and
# words it may legitimately turn into digits. Below this a mismatch is a style
# choice rather than corruption.
_SPELLABLE = 20


def literal_atoms(text: str) -> list[tuple[int, int, str]]:
    """Every (start, end, text) span a rewrite has to hand back untouched."""
    return [(m.start(), m.end(), m.group(0)) for m in _ATOM.finditer(text)]


# Characters that would make a hit part of a longer literal rather than the
# literal itself. Without this, pipeline.py counts as present in
# pipeline.pyc and the corruption goes out unrepaired.
_GLUE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./:@\\")


def _present(atom: str, text: str) -> bool:
    """Does this literal appear whole, rather than inside a longer one?"""
    at = text.find(atom)
    while at != -1:
        before = text[at - 1] if at else ""
        after = text[at + len(atom):at + len(atom) + 1]
        if before not in _GLUE and after not in _GLUE:
            return True
        at = text.find(atom, at + 1)
    return False


def _mapped_span(opcodes, start: int, end: int) -> tuple[int, int]:
    """Where a source span ended up in the output, following the diff."""
    out_start = out_end = None
    for tag, i1, i2, j1, j2 in opcodes:
        if i2 <= start or i1 >= end:
            continue
        if tag == "equal":
            lead = max(0, start - i1)
            trail = max(0, i2 - end)
            piece = (j1 + lead, j2 - trail)
        else:
            piece = (j1, j2)
        out_start = piece[0] if out_start is None else min(out_start, piece[0])
        out_end = piece[1] if out_end is None else max(out_end, piece[1])
    return (0, 0) if out_start is None else (out_start, out_end)


def _sentence_punctuation(text: str, at: int) -> bool:
    """Is the character at `at` punctuation closing a sentence rather than
    part of the token beside it? The full stop in "line 88." is not glue."""
    if text[at] not in ".,:;":
        return False
    rest = text[at + 1:at + 2]
    return rest == "" or rest.isspace()


def _widen(text: str, start: int, end: int) -> tuple[int, int]:
    """Grow a span over characters that would leave the literal embedded in a
    longer one. Repairing pipeline.py inside pipeline.pyc means taking the
    stray c with it, or the splice puts the right name back and leaves the
    wrong extension hanging off the end of it."""
    while end < len(text) and text[end] in _GLUE and not _sentence_punctuation(text, end):
        end += 1
    while start > 0 and text[start - 1] in _GLUE and not _sentence_punctuation(text, start - 1):
        start -= 1
    return start, end


def repair_literals(source: str, output: str) -> tuple[str, list[str]]:
    """Put back any number, path, URL or code span the rewrite corrupted.

    Returns (text, what was restored). Only spans that went missing are
    touched, and only where the diff says plainly where they went, so a
    rewrite that merely moved a clause around is left alone. A span whose
    replacement region has grown out of all proportion is skipped rather than
    spliced, because at that point the alignment is guessing.
    """
    atoms = [a for a in literal_atoms(source) if not _present(a[2], output)]
    if not atoms:
        return output, []

    restored: list[str] = []
    opcodes = difflib.SequenceMatcher(None, source, output, autojunk=False).get_opcodes()
    # Right to left, so an earlier splice never shifts a later offset.
    for start, end, atom in reversed(atoms):
        if atom.isdigit() and int(atom) <= _SPELLABLE:
            continue  # "3" written as "three" is a rewrite doing its job
        out_start, out_end = _mapped_span(opcodes, start, end)
        if out_end <= out_start:
            continue
        out_start, out_end = _widen(output, out_start, out_end)
        # The diff can hand back a span with the sentence's full stop on the
        # end of it, and splicing over that loses the full stop.
        while out_end > out_start and _sentence_punctuation(output, out_end - 1) \
                and not atom.endswith(output[out_end - 1]):
            out_end -= 1
        if out_end - out_start > max(12, 3 * len(atom)):
            continue
        if output[out_start:out_end] == atom:
            continue
        output = output[:out_start] + atom + output[out_end:]
        restored.append(atom)
        opcodes = difflib.SequenceMatcher(None, source, output, autojunk=False).get_opcodes()
    return output, sorted(set(restored))


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
