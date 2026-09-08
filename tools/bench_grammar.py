"""Score the grammar fix against a held-out set of broken sentences.

    .venv\\Scripts\\python tools\\bench_grammar.py [--runs 1] [--verbose]

Every case is (input, must contain, must not contain). None of them appear in
the profile's few-shot examples, so a pass means the instruction and the
deterministic pass did the work rather than the model recalling a
demonstration. The last group is about damage rather than repair: code, paths
and numbers have to come back untouched, and a fix that mangles a file name to
tidy a comma is a worse outcome than leaving the sentence alone.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxkey.cleanup.pipeline import CleanupPipeline, fix_text  # noqa: E402
from voxkey.config import Config  # noqa: E402

# (input, must appear, must not appear)
CASES: list[tuple[str, list[str], list[str]]] = [
    # -- agreement -----------------------------------------------------------
    ("we was going to the store but it were closed", ["we were going"], ["we was", "it were"]),
    ("neither of the options are acceptible", ["is accept"], ["are acceptible", "are acceptable"]),
    ("each of the servers have there own config", ["has its own"], ["have there", "have their"]),
    ("the list of files are getting long", ["list of files is"], ["files are getting"]),
    ("there is three tickets left in the queue", ["there are three"], ["there is three"]),
    ("everybody have to sign in before they start", ["everybody has"], ["everybody have"]),

    # -- tense and participles -----------------------------------------------
    ("i have went there before", ["have gone"], ["have went"]),
    ("the report was wrote by sarah", ["was written"], ["was wrote"]),
    ("he has took the last one already", ["has taken"], ["has took"]),
    ("i seen the email yesterday", ["i saw"], ["i seen"]),
    ("we have ran the migration twice", ["have run"], ["have ran"]),
    ("she had drank all the coffee", ["had drunk"], ["had drank"]),

    # -- pronoun case ---------------------------------------------------------
    ("him and me seen it happen", ["saw it happen"], ["him and me seen", "seen it"]),
    ("between you and i this is bad", ["between you and me"], ["between you and i"]),
    ("me and him went to the standup", ["he and i went"], ["me and him went"]),
    ("they gave the ticket to john and i", ["john and me"], ["john and i"]),

    # -- double negatives and non-standard forms ------------------------------
    ("she dont got no time for this", ["doesn't have"], ["dont got no"]),
    ("he dont know nothing about it", ["doesn't know anything"], ["dont know nothing"]),
    ("if i would of knew i wouldnt of came", ["had known"], ["would of", "wouldnt of", "of came"]),
    ("we should of shipped it on friday", ["should have shipped"], ["should of"]),

    # -- homophones ----------------------------------------------------------
    ("your going to loose the file if you dont save it",
     ["you're going", "lose the file", "don't save"], ["your going", "loose the"]),
    ("theres less people here then last time",
     ["fewer people", "than last"], ["less people", "then last"]),
    ("the team are playing good and there winning",
     ["playing well", "they're winning"], ["playing good", "there winning"]),
    ("its been broke since last tuesday", ["it's been"], ["its been"]),
    ("their is a problem with the deploy", ["there is"], ["their is"]),
    ("who's laptop is this", ["whose laptop"], ["who's laptop"]),
    ("i could care less about the theme", ["couldn't care less"], ["could care less"]),
    ("the affect on the build was immediate", ["effect on the build"], ["affect on the"]),

    # -- duration, comparatives, adverbs --------------------------------------
    ("ive been waiting since three days", ["for three days"], ["since three days"]),
    ("this one runs more faster than the old one", ["faster than"], ["more faster"]),
    ("he did the migration quick and quiet", ["quickly"], ["did the migration quick and"]),
    ("its the most cleanest solution we have", ["cleanest"], ["most cleanest"]),

    # -- spelling ------------------------------------------------------------
    ("i recieved the seperate invoice on tuesday", ["received", "separate"], ["recieved", "seperate"]),
    ("its definately a maintainance problem", ["definitely", "maintenance"], ["definately", "maintainance"]),
    ("we need to acheive a succesful deploy", ["achieve", "successful"], ["acheive", "succesful"]),

    # -- punctuation and structure --------------------------------------------
    ("whats the deadline for this project again", ["What's", "?"], ["whats"]),
    ("the build failed we should roll it back", ["back."], ["failed we should"]),

    # -- damage control: none of this may change ------------------------------
    ("the error is in voxkey/cleanup/pipeline.py on line 88",
     ["voxkey/cleanup/pipeline.py", "88"], []),
    ("run npm run build and then check the dist folder for errors",
     ["npm run build"], []),
    ("set llm.base_url to http://127.0.0.1:11434 before you start",
     ["http://127.0.0.1:11434", "llm.base_url"], []),
    ("the timeout was 30 seconds and we cut it to 400 ms",
     ["30", "400"], []),
    ("she said `git stash push` in the wrong worktree and lost 8 files",
     ["git stash push", "8"], []),
    ("email jude at hilgendorfjude@gmail.com about the SecPlusMastery invoice",
     ["hilgendorfjude@gmail.com", "SecPlusMastery"], []),

    # -- multi-sentence, where one bad clause used to cost the whole chunk ----
    ("the deploy went out on tuesday. me and him seen the error in the log but "
     "we was not sure what caused it. the fix is in auth.ts and it should of "
     "been caught by the tests.",
     # "we weren't sure" is as good an answer as "we were not sure", so the
     # forbid list carries the actual requirement here.
     ["auth.ts", "should have been"],
     ["me and him seen", "we was not", "should of"]),
]


def score(pipeline, verbose: bool) -> tuple[int, list[str]]:
    passed = 0
    failures = []
    for text, wants, forbids in CASES:
        got = fix_text(pipeline, text).text
        low = got.lower()
        missing = [w for w in wants if w.lower() not in low]
        present = [f for f in forbids if f.lower() in low]
        if not missing and not present:
            passed += 1
            if verbose:
                print(f"  ok    {text[:58]}")
            continue
        why = []
        if missing:
            why.append("missing " + ", ".join(repr(m) for m in missing))
        if present:
            why.append("still has " + ", ".join(repr(p) for p in present))
        failures.append(f"{text[:58]}\n        got: {got.strip()[:110]}\n        {'; '.join(why)}")
        if verbose:
            print(f"  FAIL  {failures[-1]}")
    return passed, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    pipeline = CleanupPipeline(Config())
    if not pipeline.llm.reachable():
        print("Ollama is not reachable; start it first.")
        return 2

    totals = []
    for run in range(args.runs):
        started = time.perf_counter()
        passed, failures = score(pipeline, args.verbose)
        totals.append(passed)
        elapsed = time.perf_counter() - started
        print(f"run {run + 1}: {passed}/{len(CASES)}  ({elapsed:.1f}s)")
        if failures and not args.verbose:
            for line in failures:
                print(f"  FAIL  {line}")

    if args.runs > 1:
        print(f"\nbest {max(totals)}  worst {min(totals)}  mean {sum(totals) / len(totals):.1f}"
              f"  of {len(CASES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
