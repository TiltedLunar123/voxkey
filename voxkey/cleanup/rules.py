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


# Fillers that are also ordinary words doing ordinary work. "Um" is never
# anything but noise, so it goes wherever it appears; "like" is a preposition
# most of the time it is said, and cutting it turned "looks exactly like a key"
# into "looks exactly a key" and "what time is the standup tomorrow, do you
# know?" into "do?". These only come out where the speaker paused around them,
# which is what a comma in the transcript records.
HEDGES = frozenset({
    "like", "you know", "i mean", "kind of", "sort of", "right", "so", "well",
    "actually", "basically", "literally", "obviously", "i guess", "or something",
})


def strip_fillers(text: str, fillers: list[str], protect: set[str] | None = None) -> str:
    """Drop filler words when they stand alone as their own token."""
    protect = {p.lower() for p in (protect or set())}
    candidates = (f.strip() for f in fillers if f.strip() and f.strip().lower() not in protect)
    for filler in sorted(candidates, key=len, reverse=True):
        escaped = r"\s+".join(re.escape(part) for part in filler.split())
        if filler.lower().rstrip("?.!") in HEDGES:
            # A comma on one side or the other is the only evidence that this
            # was a pause and not the word meaning what it usually means.
            pattern = re.compile(
                rf"(?:,\s*{escaped}(?!\w)\s*,?|(?<!\w){escaped}(?!\w)\s*,)",
                re.IGNORECASE,
            )
        else:
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


# Soundex keeps only the first letter and three consonant digits, so every
# word sharing a beginning collides: "SecPlus" and "SecPlusMastery" are both
# S241. Without a length check the single-word pass rewrote "SecPlus Mastery"
# to "SecPlusMastery Mastery", snapping the first half to the whole name and
# leaving the second half stranded. Two words that sound alike are not that
# different in length; putting a split name back together is the joining
# pass's job, not this one's.
_LENGTH_RATIO = 0.7


def _sounds_like(heard: str, wanted: str) -> bool:
    if heard.lower() == wanted.lower():
        return True
    if soundex(heard) != soundex(wanted):
        return False
    if min(len(heard), len(wanted)) / max(len(heard), len(wanted), 1) < _LENGTH_RATIO:
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


# How many words the recogniser might split one name into.
_MAX_SPLIT = 3


def _join_split_term(
    text: str, tokens: list[re.Match], start_index: int, terms: list[str]
) -> tuple[int, int, str] | None:
    """Put back together a one-word name the recogniser wrote as several.

    "SecPlusMastery" comes back as "SecPlus Mastery", which the word-for-word
    pass cannot see because it is comparing one heard word against one wanted
    word. Every piece has to be capitalised for this to fire: that is the
    recogniser's own signal that it thought it was writing a name, and it is
    what keeps "the sec plus mastery of it" out of reach.
    """
    single = [t for t in terms if " " not in t and len(t) >= 6]
    if not single:
        return None
    for size in range(_MAX_SPLIT, 1, -1):
        window = tokens[start_index:start_index + size]
        if len(window) != size:
            continue
        heard = [w.group(0) for w in window]
        if not all(word[:1].isupper() for word in heard):
            continue
        span = text[window[0].start():window[-1].end()]
        if any(c in span for c in ".,;:!?\n"):
            continue
        glued = "".join(heard)
        for term in single:
            if span == term:
                continue  # already exactly right
            if glued.lower() == term.lower() or _sounds_like(glued, term):
                return window[0].start(), window[-1].end(), term
    return None


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
        else:
            joined = _join_split_term(text, tokens, start_index, terms)
            if joined:
                edits.append(joined)
                taken = joined[1]
    for start, end, term in reversed(edits):
        text = text[:start] + term + text[end:]
    return text


# -- spoken paths and file names ---------------------------------------------

# Extensions and domains common enough that "pipeline dot py" is never anybody
# saying the word "dot". Anything outside this list is left as spoken, because
# the cost of a wrong join is a mangled sentence and the cost of a miss is a
# space.
_EXTENSIONS = frozenset("""
py js ts tsx jsx mjs cjs json md txt rst yml yaml toml ini cfg conf env csv tsv
html htm css scss sql sh bash ps1 bat cmd exe dll log xml svg png jpg jpeg gif
go rs rb java kt swift c cpp cc h hpp cs php lua vim zip tar gz
com org net io dev ai co uk gov edu
""".split())

_SPOKEN_DOT = re.compile(
    rf"(?<![\w.])([A-Za-z][\w-]*)\s+dot\s+({'|'.join(sorted(_EXTENSIONS))})(?![\w])",
    re.IGNORECASE,
)
# The same shape with any short word in the extension slot, for the second
# pass, where the stem has already been shown to be a path.
_SPOKEN_DOT_LOOSE = re.compile(
    r"(?<![\w.])([A-Za-z][\w./\\-]*[/\\_0-9][\w./\\-]*)\s+dot\s+([A-Za-z]{1,4})(?![\w])",
    re.IGNORECASE,
)
_SPOKEN_SLASH = re.compile(
    r"(?<![\w/])([A-Za-z][\w.-]*)((?:\s+slash\s+[A-Za-z][\w.-]*)+)(?![\w])",
    re.IGNORECASE,
)
_SLASH_PART = re.compile(r"\s+slash\s+([A-Za-z][\w.-]*)", re.IGNORECASE)


def _misheard_extension(word: str) -> str | None:
    """The extension this short word was meant to be, if there is exactly one.

    "Pipeline dot py" comes back as "pipeline dot pi" often enough to be worth
    handling, and two letters is too little for the usual similarity ratio to
    say anything. Soundex alone is enough here only because the caller has
    already established that the stem is a path, and because an ambiguous
    match is refused rather than guessed at.
    """
    low = word.lower()
    if low in _EXTENSIONS:
        return low
    if len(low) > 3:
        return None
    # Same length as well as the same sound. Soundex drops vowels, so "pi"
    # codes the same as both "py" and "php", and a two letter mishearing was
    # two letters when it was said. It also keeps "in" away from "ini", which
    # matters more, because "in" is a word people say.
    code = soundex(low)
    matches = {e for e in _EXTENSIONS if len(e) == len(low) and soundex(e) == code}
    return matches.pop() if len(matches) == 1 else None


def join_spoken_paths(text: str) -> str:
    """Turn dictated file names and paths into the thing they name.

    Someone reading a path out loud says "voxkey slash cleanup slash pipeline
    dot py", and what belongs in the text box is voxkey/cleanup/pipeline.py.
    The recogniser has no way to know that, so it writes the words.

    Deliberately narrow at every step. A dot only joins when what follows is a
    real extension or top level domain, and a run of slashes only joins when
    there are two or more of them, or when the last piece already carries an
    extension. "Twenty slash twenty vision" and "the dot on the i" are left
    exactly as they were said.

    The order matters. Exact extensions first, so "src slash app dot tsx" has
    something for the slash rule to recognise; then the slashes; then the
    near-miss extensions, by which point a stem containing a slash has proved
    it is a path and "pi" can safely become "py".
    """
    text = _SPOKEN_DOT.sub(lambda m: f"{m.group(1)}.{m.group(2).lower()}", text)

    def join(match: re.Match) -> str:
        parts = [match.group(1)] + _SLASH_PART.findall(match.group(2))
        if len(parts) < 3 and "." not in parts[-1]:
            return match.group(0)
        return "/".join(parts)

    text = _SPOKEN_SLASH.sub(join, text)

    def loose(match: re.Match) -> str:
        extension = _misheard_extension(match.group(2))
        return f"{match.group(1)}.{extension}" if extension else match.group(0)

    return _SPOKEN_DOT_LOOSE.sub(loose, text)


def run(text: str, cfg, level: str, protect_corrections: bool = False) -> str:
    """level is one of: minimal, punctuation, clean.

    protect_corrections keeps self-correction wording intact for a model pass.
    """
    if not text.strip():
        return ""
    text = tidy_spacing(text)
    if cfg.get("cleanup.snap_vocabulary", True):
        text = snap_to_vocabulary(text, cfg.get("asr.vocabulary", []))

    if level in ("punctuation", "clean") and cfg.get("cleanup.spoken_paths", True):
        # Before the voice commands, which would turn a spoken "dot" into a
        # full stop and leave the file name split in half around it.
        text = join_spoken_paths(text)

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
    "recieved": "received", "seperated": "separated", "acheived": "achieved",
    "occassion": "occasion", "arguement": "argument", "enviroment": "environment",
    "goverment": "government", "independant": "independent", "noticable": "noticeable",
    "priviledge": "privilege", "questionaire": "questionnaire", "refered": "referred",
    "relevent": "relevant", "responsability": "responsibility", "sucess": "success",
    "supress": "suppress", "truely": "truly", "wich": "which", "writting": "writing",
    "begining": "beginning", "commited": "committed", "concious": "conscious",
    "dissapoint": "disappoint", "embarass": "embarrass", "familar": "familiar",
    "greatful": "grateful", "harrass": "harass", "immediatly": "immediately",
    "knowlege": "knowledge", "libary": "library", "occuring": "occurring",
    "particurly": "particularly", "posession": "possession", "prefered": "preferred",
    "seige": "siege", "similiar": "similar", "speach": "speech", "tendancy": "tendency",
    "threshhold": "threshold", "vaccum": "vacuum", "visable": "visible",
}

# Fixed phrases where the whole expression is wrong, not one word of it, and
# where the correction never depends on context. The model gets most of these
# and misses the idioms: "could care less" came back untouched every run,
# because as a string of words there is nothing ungrammatical about it.
# Deliberately absent: "try and" -> "try to", which would turn "try and try
# again" into nonsense, and anything already covered by _OF_FOR_HAVE.
_PHRASE: list[tuple[str, str]] = [
    ("could care less", "couldn't care less"),
    ("for all intensive purposes", "for all intents and purposes"),
    ("case and point", "case in point"),
    ("one in the same", "one and the same"),
    ("nip it in the butt", "nip it in the bud"),
    ("deep seeded", "deep seated"),
    ("free reign", "free rein"),
    ("peaked my interest", "piqued my interest"),
    ("supposably", "supposedly"),
    ("suppose to be", "supposed to be"),
    ("use to be", "used to be"),
    ("different then", "different than"),
    ("more better", "better"),
    ("most easiest", "easiest"),
]

# "who's" is a contraction of "who is"; before a noun it is nearly always the
# possessive "whose" that was wanted. Restricted to a following noun phrase so
# "who's coming" and "who's the lead" are untouched.
_WHOSE = re.compile(
    r"(?<!\w)who'?s(?=\s+(?:the\s+|a\s+|an\s+|this\s+|that\s+|my\s+|your\s+|his\s+|her\s+|"
    r"their\s+|our\s+)?[a-z]+\s+(?:is|are|was|were|will|would|should|do|does|did)\b)",
    re.IGNORECASE,
)
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
    text = _OF_FOR_HAVE.sub(lambda m: f"{m.group(1)} have", text)

    for wrong, right in _PHRASE:
        spaced = r"\s+".join(re.escape(part) for part in wrong.split())
        text = re.sub(
            rf"(?<!\w){spaced}(?!\w)",
            lambda m, r=right: _match_case(r, m.group(0)),
            text,
            flags=re.IGNORECASE,
        )
    return _WHOSE.sub(lambda m: _match_case("whose", m.group(0)), text)


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
