"""Score the paraphrase chord on the four things it can get wrong.

    .venv\\Scripts\\python tools\\bench_paraphrase.py [--runs 1] [--verbose]

faithful   every number, name, path and address in the source came back intact
reworded   it actually chose different words rather than echoing the input
sized      it is a rewording, not a summary and not an essay
grammar    the result is standard English

Grammaticality is judged by handing the paraphrase to the grammar profile and
seeing whether it wants to change anything of substance. That is the same
model marking its own homework, so it is a floor rather than a verdict: it
reliably catches "have the invoice been sent", which is the failure that
actually turned up, and it would miss something subtle.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxkey.cleanup import guard  # noqa: E402
from voxkey.cleanup.pipeline import CleanupPipeline, fix_text, paraphrase_text  # noqa: E402
from voxkey.config import Config  # noqa: E402

SOURCES = [
    "The build is broken again and I have no idea what caused it.",
    "We need to fix the login bug and update the docs before we ship on Friday at 3:15.",
    "Ask Zihaad whether the CompTIA voucher covers a retake, because Julian said it does not.",
    "The error is in voxkey/cleanup/pipeline.py on line 88 and the timeout is 400 ms.",
    "Could you let me know whether the invoice has gone out yet?",
    "I spent the whole morning going through the log and every one of those warnings "
    "turned out to be Minecraft.",
    "The reason the streak looked stuck is that the auth context resolves in two steps, "
    "so the loading flag clears later than the user object does.",
    "Please send the invoice to hilgendorfjude@gmail.com before the end of the month.",
    "Warren Consolidated Schools pays every other Friday and the timesheet is due Wednesday.",
    "There were 207 questions on the practice exam and I got 181 of them right.",
    "Run npm run build and check the dist folder before you push anything to main.",
    "She said the deploy would be finished by four, but nothing has moved since two.",
]

# How many of the source's three word runs may survive and still count as a
# rewording. Vocabulary is the wrong thing to measure here: "there were 207
# questions on the practice exam" and "the practice exam had 207 questions"
# share every content word and are a complete rewording, while swapping "is"
# for "occurs" shares almost as many and is not one. Phrasing is what changes.
MAX_OVERLAP = 0.60


def overlap(source: str, output: str, mask: bool = True) -> float:
    """The share of the source's word trigrams that came back untouched.

    Names, numbers, paths and addresses are masked out first, because the
    paraphrase is required to keep those and scoring it down for obeying that
    requirement measures the sentence rather than the rewrite. "Please send
    the invoice to hilgendorfjude@gmail.com before the end of the month" is
    half address; what is being judged is the other half.
    """
    a = _trigrams(guard.words(_mask(source) if mask else source))
    b = _trigrams(guard.words(_mask(output) if mask else output))
    return len(a & b) / max(1, len(a))


def _mask(text: str) -> str:
    for _start, _end, atom in reversed(guard.literal_atoms(text)):
        text = text.replace(atom, " ")
    for name in guard.proper_nouns(text):
        text = text.replace(name, " ")
    return text


def _trigrams(tokens: list[str]) -> set[tuple[str, ...]]:
    if len(tokens) < 3:
        return {tuple(tokens)}
    return {tuple(tokens[i:i + 3]) for i in range(len(tokens) - 2)}


def substantive(before: str, after: str) -> bool:
    """Did the grammar fixer change more than punctuation and casing?"""
    return guard.words(before) != guard.words(after)


def run_once(pipeline, verbose: bool) -> dict[str, int]:
    tally = {"faithful": 0, "reworded": 0, "sized": 0, "grammar": 0, "total": len(SOURCES)}
    for source in SOURCES:
        result = paraphrase_text(pipeline, source)
        out = result.text

        atoms = [a[2] for a in guard.literal_atoms(source)]
        kept_atoms = [a for a in atoms if a in out]
        faithful = len(kept_atoms) == len(atoms) and not (
            guard.proper_nouns(source) - guard.proper_nouns(out)
        )
        checked = guard.check_paraphrase(source, out)
        share = overlap(source, out)
        raw = overlap(source, out, mask=False)
        reworded = share <= MAX_OVERLAP and guard.words(source) != guard.words(out)
        sized = checked.ok or "length" not in checked.reason
        grammar = not substantive(out, fix_text(pipeline, out).text)

        tally["faithful"] += faithful
        tally["reworded"] += reworded
        tally["sized"] += sized
        tally["grammar"] += grammar
        if verbose or not (faithful and reworded and sized and grammar):
            flags = "".join(
                letter if ok else letter.upper()
                for letter, ok in
                (("f", faithful), ("r", reworded), ("s", sized), ("g", grammar))
            )
            print(f"  [{flags}] overlap {share:.2f} (raw {raw:.2f})")
            print(f"     in : {source}")
            print(f"     out: {out}")
    return tally


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    pipeline = CleanupPipeline(Config())
    if not pipeline.llm.reachable():
        print("Ollama is not reachable; start it first.")
        return 2

    for run in range(args.runs):
        started = time.perf_counter()
        tally = run_once(pipeline, args.verbose)
        total = tally.pop("total")
        line = "  ".join(f"{name} {count}/{total}" for name, count in tally.items())
        print(f"run {run + 1}: {line}   ({time.perf_counter() - started:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
