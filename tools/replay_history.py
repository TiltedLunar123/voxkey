"""Run your own dictation history through the pipeline as it is now.

    .venv\\Scripts\\python.exe tools\\replay_history.py [--profile prompt] [--limit 30]

Every model-backed entry is rewritten again from the raw transcript that was
actually heard, and the guard's verdict on each is counted. It also reports
how many of the outputs stored at the time would have been caught, which is
the before number. Needs Ollama running. Reads history.json, writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxkey.cleanup import CleanupPipeline, guard  # noqa: E402
from voxkey.cleanup.pipeline import fix_text  # noqa: E402
from voxkey.config import HISTORY_PATH, PROFILES, Config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", help="only entries made with this profile")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many")
    args = parser.parse_args()

    config = Config()
    pipeline = CleanupPipeline(config)
    if not pipeline.llm.reachable():
        print("Ollama is not reachable, nothing to replay against.")
        return 1

    entries = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    entries = [
        e for e in entries
        if PROFILES.get(e["profile"], {}).get("llm") and (not args.profile or e["profile"] == args.profile)
    ]
    if args.limit:
        entries = entries[: args.limit]

    stored_caught = 0
    verdicts = {"clean": 0, "retried": 0, "rejected": 0, "unchecked": 0}
    started = time.perf_counter()
    for entry in entries:
        profile, raw = entry["profile"], entry["raw"]
        targets = pipeline.llm.shot_targets(PROFILES[profile].get("examples"))
        if guard.leaked_phrases(raw, entry["text"], targets) or not guard.check_fidelity(raw, entry["text"], profile).ok:
            stored_caught += 1

        # seconds == 0 marks a grammar-chord fix of typed text rather than speech.
        if profile == "grammar" and not entry.get("seconds"):
            result = fix_text(pipeline, raw)
        else:
            result = pipeline.process(raw, profile)
        verdicts[result.guard or "unchecked"] = verdicts.get(result.guard or "unchecked", 0) + 1
        if result.guard in ("retried", "rejected"):
            when = time.strftime("%m-%d %H:%M", time.localtime(entry["at"]))
            print(f"\n{result.guard.upper()} {when} {profile}: {result.warning or pipeline.last_guard}")
            print("  heard :", raw[:140].replace("\n", " "))
            print("  now   :", result.text[:140].replace("\n", " "))

    elapsed = time.perf_counter() - started
    print(f"\n{len(entries)} entries replayed in {elapsed:.0f}s")
    print(f"stored outputs the guard would have caught: {stored_caught}")
    print("this run:", ", ".join(f"{k} {v}" for k, v in verdicts.items() if v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
