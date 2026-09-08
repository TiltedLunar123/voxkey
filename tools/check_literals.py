"""What the rewrite is not allowed to change, and what it is.

    .venv\\Scripts\\python tools\\check_literals.py

Pure text in, pure text out: no model and no network, so this runs in a second
and can be trusted to say whether a change to the repair broke something.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxkey.cleanup.guard import leaked_phrases, repair_literals  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'' if ok else '  <- ' + str(detail)}")
    if not ok:
        failures.append(name)


# (name, source, what the model returned, what should be delivered)
REPAIRS = [
    ("a mangled number",
     "the timeout was 30 seconds and we cut it to 400 ms",
     "The timeout was 30 seconds, and we cut it to 40-0 milliseconds.",
     "The timeout was 30 seconds, and we cut it to 400 ms."),
    ("a dropped unit",
     "we cut it to 400 ms",
     "We reduced it to 400.",
     "We reduced it to 400 ms."),
    ("a dropped percentage",
     "coverage went up to 78% this week",
     "Coverage rose to 78 this week.",
     "Coverage rose to 78% this week."),
    ("an invented file extension",
     "the error is in voxkey/cleanup/pipeline.py on line 88",
     "The error is in voxkey/cleanup/pipeline.pyc on line 88.",
     "The error is in voxkey/cleanup/pipeline.py on line 88."),
    ("a truncated port",
     "set the url to http://127.0.0.1:11434 before you start",
     "Set the URL to http://127.0.0.1:1143 before you start.",
     "Set the URL to http://127.0.0.1:11434 before you start."),
    ("a clipped email address",
     "email jude at hilgendorfjude@gmail.com about it",
     "Email Jude at hilgendorfjude@gmail.co about it.",
     "Email Jude at hilgendorfjude@gmail.com about it."),
    ("a shortened command",
     "run `git stash push` in the worktree",
     "Run `git stash` in the worktree.",
     "Run `git stash push` in the worktree."),
    ("a windows path",
     r"open C:\Users\hilge\Downloads\voxkey and look",
     r"Open C:\Users\hilge\Downloads\vox key and look.",
     r"Open C:\Users\hilge\Downloads\voxkey and look."),
]

# Cases the repair must keep its hands off.
UNTOUCHED = [
    ("a correct rewrite",
     "the fix is in auth.ts and it should of been caught",
     "The fix is in auth.ts and it should have been caught."),
    ("a small number spelled out",
     "we had 3 people there",
     "We had three people there."),
    ("a time that survived",
     "the meeting is at 3:15 on tuesday",
     "The meeting is at 3:15 on Tuesday."),
    ("a sentence with no literals at all",
     "me and him seen it happen",
     "He and I saw it happen."),
    ("a number the rewrite moved",
     "there was 88 of them in the queue on friday",
     "On Friday there were 88 of them in the queue."),
]

print("repairing a corrupted literal")
for name, source, output, want in REPAIRS:
    got, restored = repair_literals(source, output)
    check(name, got == want, f"got {got!r}")

print("leaving a good rewrite alone")
for name, source, output in UNTOUCHED:
    got, restored = repair_literals(source, output)
    check(name, got == output and not restored, f"changed to {got!r}")

print("telling a copied example from a correction")
LEAKS = [
    ("the auth file case is still caught",
     "once you figure it out do it and then merge it to the repo",
     "Once the auth file is modified to increase the timeout from thirty seconds",
     ["In the auth file, increase the timeout. It is currently 30 seconds."],
     True),
    ("a whole example swapped in is caught",
     "tell dave the build is red",
     "What time is the standup tomorrow?",
     ["What time is the standup tomorrow?"],
     True),
    ("wrote to written is not a leak",
     "the report was wrote by sarah",
     "The report was written by Sarah.",
     ["The report was written by Sarah and me, and they are going to send their copy tomorrow."],
     False),
    ("seen to saw is not a leak",
     "i seen the email yesterday",
     "I saw the email yesterday.",
     ["I saw the email yesterday, and he and I were going to reply."],
     False),
    ("the config example working is not a leak",
     "each of the servers have there own config",
     "Each of the servers has its own config.",
     ["He does not know anything about it, and each of the servers has its own config."],
     False),
]
for name, source, output, examples, want_flagged in LEAKS:
    flagged = bool(leaked_phrases(source, output, examples))
    check(name, flagged == want_flagged, f"flagged={flagged}")

print()
print("FAILED: " + ", ".join(failures) if failures else "all checks passed")
sys.exit(1 if failures else 0)
