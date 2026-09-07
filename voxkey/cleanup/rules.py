"""Deterministic text tidying. No model, no network, sub-millisecond."""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r'([.!?])(["\')\]]*)(\s+)')
_MULTISPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?%])")
_SPACE_AFTER_OPEN = re.compile(r"([(\[“])\s+")
_SPACE_BEFORE_CLOSE = re.compile(r"\s+([)\]”])")
_BLANKLINES = re.compile(r"\n{3,}")
_REPEAT_WORD = re.compile(r"\b(\w+)((?:\s+\1\b)+)", re.IGNORECASE)
_FALSE_START = re.compile(r"\b(\w{1,3})-\s*\1\b", re.IGNORECASE)


def tidy_spacing(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MULTISPACE.sub(" ", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _SPACE_AFTER_OPEN.sub(r"\1", text)
    text = _SPACE_BEFORE_CLOSE.sub(r"\1", text)
    text = _BLANKLINES.sub("\n\n", text)
    # A spoken "new line" leaves the following space behind; dictation never
    # wants a line that starts indented.
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    # Punctuation that ended up glued to the next word after a filler was cut.
    text = re.sub(r"([,.;:!?])(?=[A-Za-z])", r"\1 ", text)
    return text.strip()


def collapse_repeats(text: str) -> str:
    """Fold stutters: "the the the plan" -> "the plan"."""
    text = _FALSE_START.sub(r"\1", text)
    return _REPEAT_WORD.sub(r"\1", text)


# Phrases that are filler in ordinary speech but carry the correction in
# "send it to Dave, I mean Sarah". Stripping them before a model rewrite throws
# away the only evidence that a correction happened.
CORRECTION_MARKERS = {"i mean", "i meant", "no wait", "wait no"}


def strip_fillers(text: str, fillers: list[str], protect: set[str] | None = None) -> str:
    """Drop filler words when they stand alone as their own token."""
    protect = {p.lower() for p in (protect or set())}
    candidates = (f.strip() for f in fillers if f.strip() and f.strip().lower() not in protect)
    for filler in sorted(candidates, key=len, reverse=True):
        escaped = r"\s+".join(re.escape(part) for part in filler.split())
        # Eat one adjacent comma so "well, um, anyway" does not leave ", ,".
        pattern = re.compile(rf"(?<!\w)\s*,?\s*{escaped}\s*,?(?!\w)", re.IGNORECASE)
        text = pattern.sub(" ", text)
    text = re.sub(r"\s*,\s*(?=[,.;:!?])", "", text)
    text = re.sub(r"^\s*[,;]\s*", "", text)
    return tidy_spacing(text)


def apply_voice_commands(text: str, commands: list[dict]) -> str:
    """Turn spoken punctuation into real punctuation."""
    active = [c for c in commands if c.get("enabled") and c.get("say")]
    if not active:
        return text
    # Longest first so "new paragraph" is not eaten by a shorter "new line" rule.
    active.sort(key=lambda c: len(c["say"]), reverse=True)
    for command in active:
        spoken = r"\s+".join(re.escape(part) for part in command["say"].split())
        insert = command.get("insert", "")
        # Whisper often writes the spoken word already punctuated ("comma,").
        pattern = re.compile(rf"(?<!\w)[,.]?\s*{spoken}\s*[,.]?(?!\w)", re.IGNORECASE)
        text = pattern.sub(lambda _m, ins=insert: ins, text)
    return text


# Only the unambiguous discard markers live here. "no wait, Tuesday" needs to
# know that Tuesday replaces Monday, which is a judgement the model makes and a
# regex cannot.
_DISCARD = re.compile(
    r"\b(?:scratch that|delete that|ignore that|forget that)\b[,.]?\s*", re.IGNORECASE
)


def _last_boundary(chunk: str) -> int:
    return max(chunk.rfind("."), chunk.rfind("!"), chunk.rfind("?"), chunk.rfind("\n"))


def apply_self_corrections(text: str) -> str:
    """Drop what the speaker explicitly threw away, not just the phrase.

    Removing only the marker would leave the retracted words in place, which is
    the opposite of what "scratch that" means.
    """
    while True:
        marker = _DISCARD.search(text)
        if marker is None:
            return tidy_spacing(text)
        head, tail = text[: marker.start()], text[marker.end():]
        boundary = _last_boundary(head)
        if head[boundary + 1:].strip():
            # Mid-sentence: "meet at three, scratch that, at four".
            head = head[: boundary + 1]
        else:
            # The marker opened a new sentence, so the retracted thought is the
            # whole sentence before it.
            previous = head[: boundary + 1].rstrip()
            head = previous[: _last_boundary(previous[:-1]) + 1]
        text = head + " " + tail


def capitalize_sentences(text: str) -> str:
    def _upper_first(chunk: str) -> str:
        for index, char in enumerate(chunk):
            if char.isalpha():
                return chunk[:index] + char.upper() + chunk[index + 1:]
            if char not in " \t\"'(“[":
                break
        return chunk

    out: list[str] = []
    for block in text.split("\n"):
        pieces = _SENTENCE_END.split(block)
        # split() yields [text, punct, closers, gap, text, ...]
        rebuilt = ""
        index = 0
        while index < len(pieces):
            if index == 0 or index % 4 == 0:
                rebuilt += _upper_first(pieces[index])
            else:
                rebuilt += pieces[index]
            index += 1
        out.append(rebuilt)
    text = "\n".join(out)
    return re.sub(r"(?<!\w)i(?=\b|')", "I", text)


def ensure_final_punctuation(text: str, skip_if_short: bool = True) -> str:
    stripped = text.rstrip()
    if not stripped:
        return text
    if stripped[-1] in ".!?:;,\"')]”":
        return stripped
    # A three-word fragment is usually a search box or a filename, not a sentence.
    if skip_if_short and len(stripped.split()) <= 3:
        return stripped
    return stripped + "."


def replace_em_dashes(text: str) -> str:
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    return re.sub(r"\s+--\s+", ", ", text)


def apply_replacements(text: str, replacements: list[dict]) -> str:
    for rule in replacements:
        source = rule.get("from", "")
        target = rule.get("to", "")
        if not source:
            continue
        flags = 0 if rule.get("case_sensitive") else re.IGNORECASE
        try:
            if rule.get("regex"):
                text = re.sub(source, target, text, flags=flags)
            else:
                text = re.sub(rf"(?<!\w){re.escape(source)}(?!\w)", target.replace("\\", "\\\\"), text, flags=flags)
        except re.error:
            continue  # a half-typed regex in Settings must not break dictation
    return text


# -- snapping names to the vocabulary ---------------------------------------

_SNAP_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")
_SOUNDEX_CODES = str.maketrans(
    "bfpvcgjkqsxzdtlmnr", "111122222222334556"
)


def soundex(word: str) -> str:
    """Plain American Soundex: the first letter, then three digits for the
    consonants that follow. "Nectar" and "Nekter" both come out as N236."""
    letters = [c for c in word.lower() if c.isalpha()]
    if not letters:
        return ""
    head = letters[0].upper()
    coded = "".join(letters).translate(_SOUNDEX_CODES)
    digits = []
    previous = coded[0]
    for code in coded[1:]:
        if code.isdigit() and code != previous:
            digits.append(code)
        # h and w do not separate two of the same code; vowels do.
        if code not in "hw":
            previous = code
    return (head + "".join(digits) + "000")[:4]


def _sounds_like(heard: str, wanted: str) -> bool:
    if heard.lower() == wanted.lower():
        return True
    if soundex(heard) != soundex(wanted):
        return False
    from difflib import SequenceMatcher

    return SequenceMatcher(None, heard.lower(), wanted.lower()).ratio() >= 0.6


def snap_to_vocabulary(text: str, vocabulary: list[str]) -> str:
    """Replace a name the recogniser nearly got with the one in the vocabulary.

    Whisper writes a name it does not know as the nearest thing it does know,
    so "Nekter" comes out as "Nectar". The vocabulary hint helps, but not
    always. This looks for a run of words that sound like a vocabulary term
    and swaps it, under two conditions that keep it out of ordinary prose: the
    heard word must be capitalised somewhere other than the start of a
    sentence, which is Whisper's own signal that it thought it heard a name,
    or the term must be several words long and every one of them must match.
    A lower-case "cloud" in "cloud storage" is never touched, whatever is in
    the list.
    """
    terms = [t.strip() for t in vocabulary if t.strip() and _SNAP_WORD.fullmatch(t.strip().replace(" ", "a"))]
    if not terms or not text:
        return text
    # Filenames, paths and identifiers are masked first, the same as for the
    # grammar rules, so Nectar.txt keeps its name.
    return _without_protected(text, lambda masked: _snap(masked, terms))


def _snap(text: str, terms: list[str]) -> str:
    tokens = list(_SNAP_WORD.finditer(text))
    if not tokens:
        return text
    lowered = {t.lower() for t in terms}
    edits: list[tuple[int, int, str]] = []
    taken = 0
    for start_index, token in enumerate(tokens):
        if token.start() < taken:
            continue
        preceding = text[:token.start()].rstrip()
        sentence_start = not preceding or preceding[-1] in ".!?\n"
        for term in sorted(terms, key=len, reverse=True):
            parts = term.split()
            window = tokens[start_index:start_index + len(parts)]
            if len(window) != len(parts):
                continue
            heard = [w.group(0) for w in window]
            if " ".join(heard).lower() in lowered or " ".join(heard) == term:
                break  # already right
            # Words in between must be adjacent, not separated by punctuation.
            span = text[window[0].start():window[-1].end()]
            if any(c in span for c in ".,;:!?\n"):
                continue
            if not all(_sounds_like(h, w) for h, w in zip(heard, parts)):
                continue
            named = heard[0][:1].isupper() and not sentence_start
            if not named and len(parts) < 2:
                continue
            edits.append((window[0].start(), window[-1].end(), term))
            taken = window[-1].end()
            break
    for start, end, term in reversed(edits):
        text = text[:start] + term + text[end:]
    return text


def run(text: str, cfg, level: str, protect_corrections: bool = False) -> str:
    """level is one of: minimal, punctuation, clean.

    protect_corrections keeps self-correction wording intact for a model pass.
    """
    if not text.strip():
        return ""
    text = tidy_spacing(text)
    if cfg.get("cleanup.snap_vocabulary", True):
        text = snap_to_vocabulary(text, cfg.get("asr.vocabulary", []))

    if level in ("punctuation", "clean") and cfg.get("cleanup.voice_commands", True):
        text = apply_voice_commands(text, cfg.get("cleanup.voice_command_list", []))
        text = tidy_spacing(text)

    if level == "clean":
        if cfg.get("cleanup.self_corrections", True):
            text = apply_self_corrections(text)
        if cfg.get("cleanup.collapse_repeats", True):
            text = collapse_repeats(text)
        keep = CORRECTION_MARKERS if protect_corrections else set()
        text = strip_fillers(text, cfg.get("cleanup.fillers", []), protect=keep)

    if cfg.get("cleanup.no_em_dashes", True):
        text = replace_em_dashes(text)
    if cfg.get("cleanup.capitalize_sentences", True):
        text = capitalize_sentences(text)
    if cfg.get("cleanup.ensure_final_punctuation", True):
        text = ensure_final_punctuation(
            text, cfg.get("cleanup.strip_trailing_period_short", True)
        )

    text = apply_replacements(text, cfg.get("cleanup.replacements", []))
    return tidy_spacing(text)


# -- deterministic grammar, for the classes a regex is simply better at -----

# Written forms that are not English words without their apostrophe, so the
# correction is never ambiguous. its/your/were/theirs are all excluded because
# each one is a real word and needs context the model has and a regex does not.
_APOSTROPHE = {
    "dont": "don't", "doesnt": "doesn't", "didnt": "didn't", "wont": "won't",
    "cant": "can't", "isnt": "isn't", "arent": "aren't", "wasnt": "wasn't",
    "werent": "weren't", "havent": "haven't", "hasnt": "hasn't", "hadnt": "hadn't",
    "wouldnt": "wouldn't", "couldnt": "couldn't", "shouldnt": "shouldn't",
    "mustnt": "mustn't", "youre": "you're", "theyre": "they're", "weve": "we've",
    "youve": "you've", "ive": "I've", "whats": "what's",
    "thats": "that's", "theres": "there's", "heres": "here's", "wheres": "where's",
    "hows": "how's", "whos": "who's", "somethings": "something's",
    # Deliberately absent: "id" and "im". Both are ordinary words in prose and
    # extremely common identifiers in code, so expanding them to "I'd" and "I'm"
    # corrupts far more than it fixes.
}
_MISSPELLING = {
    "alot": "a lot", "teh": "the", "recieve": "receive", "seperate": "separate",
    "definately": "definitely", "occured": "occurred", "untill": "until",
    "becuase": "because", "thier": "their", "freind": "friend", "wierd": "weird",
    "acheive": "achieve", "beleive": "believe", "accomodate": "accommodate",
    "tommorow": "tomorrow", "calender": "calendar", "neccessary": "necessary",
    "recomend": "recommend", "succesful": "successful", "publically": "publicly",
    "maintainance": "maintenance", "existance": "existence", "occurence": "occurrence",
}
_OF_FOR_HAVE = re.compile(
    r"\b(could|should|would|must|might)\s+of\b", re.IGNORECASE
)

# "Me and him" is never a correct subject. Restricted to the start of a
# sentence, which is the position that guarantees subject case and keeps
# "between you and me" untouched.
_SUBJECT_PAIR = re.compile(
    r"(?:^|(?<=[.!?\n])\s*)"
    r"(?:me and (him|her|them|[A-Z][a-z]+)|(him|her|them|[A-Z][a-z]+) and me)\b",
)
_SUBJECT_CASE = {"him": "He", "her": "She", "them": "They"}


# Regions the mechanical pass must never touch. The model is told to leave code
# alone and does; a regex has no such judgement.
_PROTECTED = re.compile(
    r"`[^`]*`"                       # inline code
    r"|https?://\S+|www\.\S+"        # urls
    r"|[A-Za-z]:\[^\s]+"            # windows paths
    r"|(?<![\w.])\.{0,2}/[^\s]{2,}"   # unix paths and ./ ../
    r"|\w+(?:\.\w+)+"                # user.id, settings.json, example.com
    r"|\w*_\w+"                      # snake_case identifiers
)


def _without_protected(text: str, transform) -> str:
    """Run a transform with code, paths and URLs masked out of the way."""
    stash: list[str] = []

    def hide(match: re.Match) -> str:
        stash.append(match.group(0))
        return f"{len(stash) - 1}"

    masked = _PROTECTED.sub(hide, text)
    result = transform(masked)
    for index, original in enumerate(stash):
        result = result.replace(f"{index}", original)
    return result


def _match_case(replacement: str, original: str) -> str:
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def fix_mechanical(text: str) -> str:
    """Corrections that are certain, so the model never has to spend a token."""
    return _without_protected(text, _fix_mechanical)


def _fix_mechanical(text: str) -> str:
    def swap(match: re.Match) -> str:
        word = match.group(0)
        target = _APOSTROPHE.get(word.lower()) or _MISSPELLING.get(word.lower())
        return _match_case(target, word) if target else word

    known = sorted(set(_APOSTROPHE) | set(_MISSPELLING), key=len, reverse=True)
    text = re.sub(rf"(?<!\w)(?:{'|'.join(known)})(?!\w)", swap, text, flags=re.IGNORECASE)
    return _OF_FOR_HAVE.sub(lambda m: f"{m.group(1)} have", text)


def fix_subject_pronouns(text: str) -> str:
    """"Me and him seen it" -> "He and I saw it", for the subject position only."""
    return _without_protected(text, _fix_subject_pronouns)


def _fix_subject_pronouns(text: str) -> str:
    def swap(match: re.Match) -> str:
        other = match.group(1) or match.group(2)
        subject = _SUBJECT_CASE.get(other.lower(), other)
        lead = match.group(0)[: len(match.group(0)) - len(match.group(0).lstrip())]
        return f"{lead}{subject} and I"

    return _SUBJECT_PAIR.sub(swap, text)
