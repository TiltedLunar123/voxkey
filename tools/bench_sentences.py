"""Ground truth for the speech benchmark.

Each entry is what gets spoken and, where the two differ, what a good
transcript of it looks like. They come apart whenever the natural written form
is not the natural spoken one: "settings dot json" is said out loud but
`settings.json` is what belongs in the text box, and scoring the recogniser
down for getting that right would be measuring the benchmark, not the app.

Weighted towards what this app actually gets dictated into it: work chatter
with product names in it, shell and file references, numbers, and the kind of
short take where the recogniser is most tempted to invent something.
"""

# (spoken, expected) - expected omitted when it is the same as spoken.
PAIRS: list[tuple[str, ...]] = [
    # -- vocabulary the recogniser has no reason to know ---------------------
    ("I also work at Nekter Juice Bar as a shift lead on weekends.",),
    ("Push the Supabase migration before the Netlify build runs.",),
    ("The SecPlusMastery dashboard is showing the wrong streak count again.",),
    ("Warren Consolidated Schools sends the timesheet every other Friday.",),
    ("Ask Zihaad whether the CompTIA voucher covers a retake.",),
    ("The SIEM rule fired twice but the SOC queue never picked it up.",),
    ("Julian took the referral code and Corbin is still waiting on his.",),

    # -- paths, commands and identifiers -------------------------------------
    ("Open settings dot json and change the timeout to sixty seconds.",
     "Open settings.json and change the timeout to 60 seconds."),
    ("Run npm run build and then check the dist folder.",),
    ("The error came from voxkey slash cleanup slash pipeline dot py.",
     "The error came from voxkey/cleanup/pipeline.py."),
    ("Set the base URL to localhost port one one four three four.",
     "Set the base URL to localhost port 11434."),

    # -- ordinary work speech, with hesitation --------------------------------
    # The cleanup is supposed to take the "um" out, so the expected form does
    # not have one. Scored end to end this is a pass; scored with --raw the
    # recogniser is marked down for the filler it correctly heard.
    ("So um I think we should probably ship it on Friday instead.",
     "So I think we should probably ship it on Friday instead."),
    ("What time is the standup tomorrow, do you know?",),
    ("The build is broken again and I do not know why yet.",),
    ("Can you let me know whether the invoice has gone out?",),
    ("I will take a look at it after the interview on Tuesday.",),
    ("We need to fix the login bug and update the docs before release.",),

    # -- numbers and dates, where a small slip really matters -----------------
    ("The meeting moved from three fifteen to four thirty on Wednesday.",
     "The meeting moved from 3:15 to 4:30 on Wednesday."),
    ("It went from thirty seconds down to about four hundred milliseconds.",
     "It went from 30 seconds down to about 400 milliseconds."),
    ("There were two hundred and seven questions on the practice exam.",
     "There were 207 questions on the practice exam."),

    # -- short takes, where the recogniser tends to hallucinate ---------------
    ("Sounds good to me.",),
    ("Not yet.",),
    ("Send it over when you can.",),

    # -- longer, so the second decoding window is exercised too ---------------
    ("The reason the streak looked stuck is that the auth context resolves in "
     "two steps, so the loading flag clears later than the user object does, "
     "and the effect was clobbering the value it had just hydrated from the "
     "database.",),
    ("I spent the morning going through the log and every single one of those "
     "stuck key warnings turned out to be Minecraft, because holding W or space "
     "for ten seconds looks exactly like a key that never came back up.",),
]

SENTENCES = [pair[0] for pair in PAIRS]
EXPECTED = [pair[-1] for pair in PAIRS]
