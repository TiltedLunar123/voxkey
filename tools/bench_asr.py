"""Word error rate for the recogniser, over the clips bench_speech.py made.

    .venv\\Scripts\\python tools\\bench_asr.py bench_audio

Prints a per-clip WER and the total, and lists every clip that came back with
anything wrong, so a change to the decoding settings can be judged instead of
guessed at. Ground truth is compared loosely: case, punctuation and the
difference between "do not" and "don't" are not what this is measuring.
"""

import json
import re
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxkey.asr import Transcriber  # noqa: E402
from voxkey.cleanup import rules  # noqa: E402
from voxkey.config import Config  # noqa: E402

TARGET_RATE = 16000

_CONTRACTIONS = {
    "do not": "dont", "does not": "doesnt", "did not": "didnt",
    "will not": "wont", "cannot": "cant", "can not": "cant",
    "i will": "ill", "i am": "im", "it is": "its", "that is": "thats",
    "we will": "well", "you will": "youll", "is not": "isnt",
}

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def _numbers_to_digits(tokens: list[str]) -> list[str]:
    """"two hundred and seven" -> "207". Spoken numbers and written ones are
    the same dictation; only a mishearing should cost anything here.

    Words are only added together where English actually adds them: a tens
    word followed by a unit ("twenty three"), and the hundred and thousand
    scales. Everything else stays a separate token, because "three fifteen"
    is a time and "one one four three four" is a port number, and summing
    either one to 18 or 13 would be nonsense. The digit-merging step further
    down puts those neighbours back together as one written number.
    """
    out: list[str] = []
    index = 0
    while index < len(tokens):
        word = tokens[index]
        if word not in _UNITS and word not in _TENS:
            out.append(word)
            index += 1
            continue

        value = _UNITS.get(word, _TENS.get(word, 0))
        index += 1
        if word in _TENS and index < len(tokens) and tokens[index] in _UNITS \
                and _UNITS[tokens[index]] < 10:
            value += _UNITS[tokens[index]]
            index += 1

        while index < len(tokens) and tokens[index] in ("hundred", "thousand"):
            value *= 100 if tokens[index] == "hundred" else 1000
            index += 1
            # "two hundred and seven", "three thousand two hundred"
            tail = index + 1 if index < len(tokens) and tokens[index] == "and" else index
            if tail < len(tokens) and (tokens[tail] in _UNITS or tokens[tail] in _TENS):
                nested = _numbers_to_digits(tokens[tail:])
                if nested and nested[0].isdigit() and int(nested[0]) < value:
                    value += int(nested[0])
                    consumed = len(tokens[tail:]) - len(nested) + 1
                    index = tail + consumed
        out.append(str(value))
    return out


def normalise(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    # "stand-up" and "standup" are the same word said the same way.
    text = text.replace("-", "")
    for long, short in _CONTRACTIONS.items():
        text = re.sub(rf"(?<!\w){long}(?!\w)", short, text)
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    text = text.replace("'", "")
    tokens = _numbers_to_digits(text.split())
    # A string of digits read out loud arrives as separate tokens; written
    # down it is one. "one one four three four" and "11434" are one word.
    merged: list[str] = []
    for token in tokens:
        if token.isdigit() and merged and merged[-1].isdigit():
            merged[-1] += token
        else:
            merged.append(token)
    return merged


def wer(reference: list[str], hypothesis: list[str]) -> tuple[int, int]:
    """Levenshtein distance over words. Returns (edits, reference length)."""
    previous = list(range(len(hypothesis) + 1))
    for i, ref in enumerate(reference, 1):
        current = [i]
        for j, hyp in enumerate(hypothesis, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ref != hyp),
            ))
        previous = current
    return previous[-1], len(reference)


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != TARGET_RATE:
        length = int(round(len(audio) * TARGET_RATE / rate))
        audio = np.interp(
            np.linspace(0, len(audio) - 1, length, dtype=np.float64),
            np.arange(len(audio), dtype=np.float64),
            audio,
        ).astype(np.float32)
    return audio


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    raw_only = "--raw" in sys.argv
    folder = Path(args[0] if args else "bench_audio")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))

    config = Config()
    transcriber = Transcriber(config)
    if not transcriber.load():
        print(f"model failed to load: {transcriber.last_error}")
        return 2

    # What the user gets is the transcript after the rule pass, which is where
    # the vocabulary snapping and the spoken paths happen. --raw scores the
    # recogniser on its own, which is the number to watch when changing the
    # decoding settings rather than the cleanup.
    def deliver(audio) -> str:
        heard = transcriber.transcribe(audio)
        return heard if raw_only else rules.run(heard, config, "clean")

    print("scoring the recogniser alone" if raw_only else "scoring transcript plus cleanup")

    total_edits = total_words = 0
    misses = []
    for item in manifest:
        audio = load_wav(folder / item["wav"])
        heard = deliver(audio)
        reference, hypothesis = normalise(item["text"]), normalise(heard)
        edits, length = wer(reference, hypothesis)
        total_edits += edits
        total_words += length
        if edits:
            misses.append((item["wav"], edits, length, item["text"], heard.strip()))

    for wav, edits, length, want, got in misses:
        print(f"\n{wav}  {edits}/{length} wrong")
        print(f"  want: {want}")
        print(f"  got : {got}")

    rate = 100.0 * total_edits / max(1, total_words)
    print(f"\n{len(manifest) - len(misses)}/{len(manifest)} clips perfect")
    print(f"WER {rate:.2f}%  ({total_edits} edits over {total_words} words)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
