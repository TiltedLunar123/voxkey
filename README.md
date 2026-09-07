# VoxKey

Hold **Ctrl + Win**, say something, let go. It gets transcribed, tidied up the
way you asked, and typed into whatever window you were already in.

Everything runs on this machine. No audio and no text leaves the computer.

Windows only. It needs a microphone, and a GPU if you want it fast.

## Installing

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Drop the two `nvidia-*` lines from `requirements.txt` if you have no NVIDIA
card; it will run on the CPU instead, slower.

The tone profiles need [Ollama](https://ollama.com) with a non-thinking instruct
model:

```bash
ollama pull qwen3:4b-instruct
```

Skip that if you only want the rule-based profiles. They work with nothing
installed beyond the Python packages.

## Running it

```bash
.venv\Scripts\pythonw.exe VoxKey.pyw
```

The first run registers it with Windows: a **VoxKey** entry in the Start menu,
a line under Settings, Apps, Installed apps, and the Run key so it starts at
login. All three point at this folder, and all three are put right again at
every start, so moving the folder does not break anything. The speech model
downloads on first launch.

After that you open it the way you open anything else: press Start, type
"vox", press Enter. If it is already running in the tray, that brings up its
window instead of starting a second copy. Launching it by hand always shows
the window; only the automatic start at login stays hidden.

Two flags, should you want them:

```bash
.venv\Scripts\pythonw.exe VoxKey.pyw --quit
.venv\Scripts\pythonw.exe VoxKey.pyw --uninstall
```

The first stops the running copy. The second takes VoxKey out of the Start
menu, Installed apps and startup, and stops it; the folder and your settings
stay where they are. The Installed apps entry runs that same command.

Click the tray icon for the simple window: the talk key, the cleanup profile,
the microphone, and a startup toggle. Everything else is behind **All
settings**.

## The Flow Bar

A small capsule sits at the bottom of the screen all the time, the way Wispr
Flow's does.

- **Idle** it is a mic and your chord. Click it to start dictating without
  touching the keyboard.
- **Recording** it widens into a live waveform with a timer, a red **✕** to
  throw the take away, and a **■** to stop and paste. Clicking anywhere on the
  bar also stops, which is what Flow does in push-to-talk.
- **Working** it shrinks to a coloured dot and a word: Transcribing, then
  Rewriting, then Sent. Or **Copied, no text box**, which is covered below.

It sits bottom right and dims to half opacity when idle, so it stays clear of
the text box you are dictating into, then brightens on hover and while working.
Put it in any corner or edge from the settings window, or from its own
right-click menu under Move to. Drag it and it snaps to the nearest screen edge
and stays where you left it. Right-click for Settings, History, Paste last transcript, Hide for an
hour, and a toggle for whether it stays up when idle.

The bar carries `WS_EX_NOACTIVATE`, so clicking it never takes focus off the
window you were typing in. That matters: if it stole focus, Stop would paste
your text into the bar instead of your document. It is also a tool window, so
it stays out of alt-tab and off the taskbar.

## When there is nothing to paste into

Let go of the chord with the focus on the desktop, a file list, a button or a
web page with no box selected, and Ctrl+V goes nowhere. Worse, VoxKey then
handed the clipboard back to whatever you had copied before, so the words were
simply gone, apart from a line in History.

Now it looks first. Just before pasting it asks Windows, through UI
Automation, what has keyboard focus. When that is plainly not somewhere text
can go, the dictation is left on the clipboard, the bar says **Copied, no text
box**, and a notification says what did have focus and that Ctrl+V will put
the words wherever you click next. Wispr Flow does the same.

Only a definite answer stops a paste: a list, a list item, a button, a link, a
menu, a tab, a toolbar, a title bar, a piece of plain text, or a browser page
that reports itself read-only with nothing selected. Anything VoxKey cannot
identify is pasted as before, so a game's chat box or an unusual toolkit loses
nothing. Terminals are never second-guessed, since every one of them takes
Ctrl+V and they describe themselves to Windows in a dozen different ways.

One detail worth knowing: an Electron or Chromium app switches its
accessibility tree on the first time anything asks, and answers that first
question with the bare minimum. VoxKey asks twice when the first answer looks
like that, and warms the client up at startup.

Switch it off under All settings, Output, if an app you use is misreported.

## When it stops working

There is a **Diagnostics** tab under All settings. It reports, live, every part
that can fail without saying so: whether the key listener is running, whether
any key is stuck down, whether the audio stream is actually delivering, the
live input level, which models are loaded, what happened to the last model
rewrite, and when the last dictation was. "Copy report" puts the lot on the
clipboard.

That tab exists because three different faults all present identically as "the
hotkey stopped working": a key latched down by Windows, an input stream that
died without raising, and an unhandled exception under `pythonw.exe`, which has
no stderr to print to. All three are now written to
`%APPDATA%\VoxKey\voxkey.log` as they happen, and the app recovers from the
first two on its own.

The stall detector is not hypothetical: it fired twice in half an hour of
ordinary use on the machine this was built on, and reopened the device both
times.

A stuck **modifier** still gets a notification, because it stops the chord
matching until it is tapped. An ordinary key held down does not. That is
someone walking forward in a game or leaning on an arrow key, and an evening
of W, A, S, D and Space each held for ten seconds produced eighteen
notifications before this changed. It goes in the log and on the Diagnostics
tab, and the chord simply waits for the key to come back up.

## What it does with your words

Four profiles are plain text rules. They are instant, work offline, and never
change your meaning.

| Profile | What it does |
| --- | --- |
| None | Exactly what was heard. |
| Minimal fixes | Whitespace, capitals, closing full stop. |
| Punctuation | Adds spoken punctuation ("new line", "question mark"). |
| Clean up | Also removes "um", "uh", stutters and false starts. *(default)* |

Five more hand the transcript to a local model for a real rewrite: **Casual**,
**Formal**, **Email**, **Notes / bullets**, **Prompt to an agent**, plus
**Custom** for your own instruction.

Switch profile without opening anything: right-click the tray icon.

### Checking the model's work

A small instruct model is fast and usually faithful. Every so often it is
neither, and the failure that forced this was caught in a real dictation. The
Prompt profile is taught its style with two worked examples, one of which is
"In the auth file, increase the timeout. It is currently 30 seconds." A
thirty-second dictation that began "Once you figure it out, do it, and then
merge it to the repo" came back as "Once the auth file is modified to increase
the timeout from thirty seconds", and went off to a coding assistant that
then went looking for an auth file. Three runs out of three did it. The other
failure seen in the wild is quieter: the first half of a long instruction,
nicely tightened, with the second half missing.

Every rewrite is now checked against the transcript before it is pasted:

- Any run of three words from a worked example that appears in the output
  and not in what you said means the example was copied.
- For the Prompt and Grammar profiles, the output has to keep most of the
  words that carry the meaning, and it may not introduce many that were never
  said. Spelling fixes, inflections and contractions are forgiven; "erorr" to
  "error" is not an invention, and "can't" to "cannot" is not either. The
  floors come from a hundred stored rewrites: the lowest one that was fine kept
  three quarters of its words, and the one that dropped half an instruction
  kept just over two thirds.

A rewrite that fails is tried once more with no examples at all, since a model
cannot copy what it was not shown. If that one is also off, the rule-based
Clean up text is used instead and the notification says why. On the case
above it was off: the retry paraphrased the meaning away, and your own words,
tidied, were the better result. The Diagnostics tab shows what happened to the
last rewrite, and the Preview on the Cleanup tab says when it retried. Over
the hundred stored rewrites the check trips on one, the copied example, and
nothing else.

The Prompt and Grammar profiles also run at temperature zero, because their
whole job is to keep what you said. That is safe: Ollama reloads the model
when `num_ctx` changes, not when the temperature does, and both were measured.

## Speed

Measured here, on the RTX 5070 Ti, for an 11 second dictation:

| Step | Time |
| --- | --- |
| Transcription (`large-v3-turbo`, CUDA fp16) | ~0.40 s |
| Rule profiles | ~0.003 s |
| Model rewrite (`qwen3:4b-instruct`) | ~0.30 s |
| The check on that rewrite | ~0.001 s |
| A retry, only when the check fails | ~1 s more |

So about half a second for the rule profiles and under a second with a rewrite.
Both models are warmed at startup with the exact request shape they will get
later, because Ollama reloads a model whenever the request options change. A
warm-up that did not match cost 7 to 13 seconds on the first real rewrite.

## The two model choices, and why

**Speech: `large-v3-turbo`.** Full Whisper large-v3 accuracy at roughly four
times the speed. Change it under All settings ▸ Transcription; `distil-large-v3`
is faster still and `large-v3` is the most accurate.

**Rewriting: `qwen3:4b-instruct`, and it must be an instruct build.** Plain
`qwen3:4b` is a hybrid reasoning model: it spends 900 to 4,000 tokens thinking
before it answers, which turns a 0.4 second rewrite into 30+ seconds. Setting
`think: false` does not stop it and neither does `/no_think`; both were tried.
The settings screen warns you if you pick a model that looks like a reasoner.

## Fixing text you already wrote

Press **Ctrl + Win + Alt** in any text box. VoxKey selects what is there, fixes
the spelling, grammar, punctuation and capitalisation, and puts it back in
place. It keeps your wording, your tone and your line breaks, leaves code and
file paths alone, and never introduces an em dash.

The chord is a superset of the talk chord, which is deliberate: a binding only
matches when the modifiers outside it are up, so holding Ctrl+Win+Alt cannot
also start a dictation.

Two passes, in that order. The certain classes are settled by rule before the
model ever sees the text: missing apostrophes where the bare form is not a word
(`dont`, `wouldnt`, `youre`), "could of" and "should of", a set of common
misspellings, and subject-case pronouns, because "me and him" is never a correct
subject. Those cost no tokens and cannot be got wrong. Everything needing
judgement goes to the model.

The rule pass masks inline code, URLs, Windows and Unix paths, dotted
identifiers and snake_case names before it runs, so `user.id` and
`C:\path\dont_touch.txt` come back untouched. `id` and `im` are deliberately
absent from the table: both are ordinary words and extremely common identifiers,
and expanding them turned `SELECT id` into `SELECT I'd`.

The model gets the text in pieces of about six hundred characters, cut at
sentence ends and blank lines. It used to get whole paragraphs, and a paragraph
of a thousand characters was too much for it: on the machine this was built on
it corrupted the last clause six times out of six, turning "an actual app on my
computer" into "an actual app on my when done", and never once did when handed
two sentence-sized pieces. The pieces go back together byte for byte, line
breaks included, so a bullet list stays a bullet list. Each piece gets the same
check as a dictation, and a piece whose fix drifted keeps only the certain
rule-based corrections.

Scored against ten held-out sentences that appear nowhere in its examples, it
fixes 10 of 10; on ten error classes never named in its instruction, 9 of 10;
on a harder set covering multi-error paragraphs and preservation of code, paths,
URLs, bullet lists and already-correct prose, 10 of 10. Naming the error classes
outright rather than asking for "correct grammar" is what took it from 5 of 10.
A 7B model was tried and scored *worse*, because it expands contractions and so
changes your register.

Guards worth knowing about:

- A sentinel is parked on the clipboard first. If the app ignores Ctrl+C, or
  Ctrl+A grabbed files rather than text, nothing is pasted and you get told why.
- If focus moves while it is thinking, the corrected text goes to the clipboard
  rather than into whatever window you switched to.
- Text longer than the limit is cut off at the limit, and the notification says
  so.

Change the chord, or make it fix only the current selection instead of the whole
box, on the Dictation tab.

## Catching the first word

The microphone is held open and the last 0.6 seconds are kept in a rolling
buffer, so audio from *before* the chord registers is already captured. Two
things used to eat the start of a take: the 250 ms hold threshold, and the
roughly 100 ms an audio stream needs to start. Speaking the instant you pressed
lost the first word, which is how a real dictation came out as `Emge.`

Recording also continues for 200 ms after you let go, because people release on
the last syllable.

If you would rather the microphone were only open while dictating, turn off
"Catch speech from before the key registers" under All settings, Audio. The
first word will be clipped again; nothing is recorded to disk either way and no
audio leaves the machine.

The chosen input device is remembered by name, not by index. PortAudio
renumbers devices whenever hardware or a driver changes, and a saved index here
had drifted onto an HDMI output with no input channels at all: the app went on
reporting it as the selected microphone while every attempt to open it failed.
An index that no longer points at an input is now detected and the system
default used instead.

The microphone is opened as mono when the device allows it, and otherwise at
whatever channel count it does offer, mixed down by averaging. That is not
theoretical either: every input device on the machine this was built on reports
two or four channels and none offers mono, so asking for one channel returns
"Invalid number of channels" from PortAudio.

Near-silent clips are dropped rather than transcribed. Handed silence, Whisper
confidently returns "Thank you.", which is exactly what a 1.1 second empty take
produced before.

## Hearing names right

Whisper has no word list. What it has is a slot for "the text that came
before", and anything in that slot is more likely to be heard. Four things use
it, or clean up after it.

**The vocabulary goes in as hotwords.** It used to go in as an initial prompt,
which fills that slot for the first thirty-second window only, and dictations
here regularly run longer than that. Hotwords fill it for every window, so a
name said in the second minute gets the same help as one said in the first.

**The previous dictation goes in as context.** Whisper decodes each window as
a continuation of what it was told came before; that is how it keeps casing,
spelling and punctuation consistent across a long recording. Between two
dictations a few minutes apart the same trick applies, since the names and
jargon of the last one are exactly what the next one is likely to contain. The
last sixty words of your previous dictation are handed over, if it was within
ten minutes and this take is longer than two seconds. Off under All settings,
Transcription, if you would rather it did not.

**The decoder can recover.** A single fixed temperature switches off Whisper's
own fallback: when a window decodes into a stuck loop or scores badly it retries
warmer, but only if it has somewhere warmer to go. It now has two steps. Any
phrase it still repeats three times in a row, or any word five times, is folded
to one. "No no no no" is left alone, because people say that.

**Names it nearly got are snapped to the vocabulary.** Whisper writes a name
it does not know as the nearest thing it does know, so Nekter comes out as
Nectar. If Nekter is in your vocabulary, a word that sounds like it and is
capitalised somewhere other than the start of a sentence, which is Whisper's
own signal that it thought it heard a name, is swapped. So is a whole
multi-word term when every word matches, whatever the case. "Cloud storage" is
never touched by a Claude in the list; "I saw Judy" will become "I saw Jude"
if Jude is in the list, which is the trade, and why the list should hold names
you actually use. Sounds-like is plain Soundex plus a similarity floor. Off
under All settings, Cleanup.

## Learning as it goes

Nothing here is uploaded; it all lives in `%APPDATA%\VoxKey\learned.json`.

- **The profile follows the window.** Dictating into VS Code gets Prompt, into
  Outlook or a Gmail tab gets Email, into Slack or Discord gets Casual. Rules
  are editable on the Smart tab.
- **It notices when you disagree.** Change profile from the tray after
  dictating into an app three times and that becomes the rule for that app,
  ahead of the shipped ones.
- Distinctive words you actually dictate are fed back to Whisper as a
  recognition hint after the third use. This reinforces words that already get
  through; it cannot teach one the recogniser has never once heard correctly.
  Because of that it can also reinforce a misheard name, which is what the
  vocabulary snapping above is for: pin the right spelling and the learned
  wrong one stops being heard. Words an earlier build stored with their full
  stop still attached are dropped on load.
- **Spoken corrections are acted on.** "Scratch that" drops the retracted
  sentence by rule, in any profile. "Send it to Dave, I mean Sarah" needs to
  work out that Sarah replaces Dave, so that one is resolved by the model and
  applies to the tone profiles only.

## Things worth knowing

- **The chord is a prefix for real shortcuts.** Recording only starts after
  you have held it for 250 ms, and pressing any other key cancels it, so
  Win+Ctrl+Right still switches virtual desktops instead of leaving a recording
  running. Both are adjustable.
- **Ctrl + Win will not open the Start menu.** Windows only opens Start when
  Win goes down and up with nothing pressed in between, and Ctrl is held the
  whole time here.
- **If you switch to Ctrl + Alt**, note that AltGr reports as Ctrl+Alt on
  non-US layouts, so typing an accented character would open the microphone.
- **Pasting borrows the clipboard.** The old contents go back 500 ms later,
  and only if nothing else has changed it since. If an app is slow enough to
  read the clipboard after that, raise the delay under All settings ▸ Output,
  or switch delivery to typing.
- **Consecutive dictations space themselves.** Dictate twice into the same box
  and the second take gets a space in front so the words do not run together.
  Windows offers no way to read the character before the cursor in another
  application, so this is based on what VoxKey itself last inserted in that
  window: it stays quiet after a newline, after a trailing space, before
  punctuation, and in a window it has not just typed into. Force it on or off
  under All settings, Output, Spacing.
- **Dictated questions stay questions.** The rewriting model is instructed and
  given worked examples so that "what is the capital of France" comes back as
  your sentence, not as "Paris". Same for anything phrased as an instruction.
- **The taskbar knows it is VoxKey.** The process sets an AppUserModelID and
  the Start menu shortcut carries the same one, so the window groups under
  VoxKey rather than pythonw.exe and pinning it to the taskbar gives a pin that
  actually launches it.
- Words it keeps mishearing go in two places: **Vocabulary** (nudges Whisper,
  and snaps near misses) and **Always replace** (a hard find-and-replace
  applied last).

## Layout

```
voxkey/
  hotkey.py      chord detection (GetAsyncKeyState polling)
  audio.py       microphone capture, level meter, blips
  asr.py         faster-whisper wrapper, CUDA DLL bootstrap, hotwords, context
  cleanup/
    rules.py     deterministic tidying, vocabulary snapping
    guard.py     checks on the model's rewrites, loop folding
    llm.py       Ollama client, guardrails, few-shot
    pipeline.py  profile routing, the checked rewrite, sentence-sized fixing
  inject.py      clipboard and SendInput delivery, the focus check
  overlay.py     floating status pill
  tray.py        tray icon and menu
  settings_ui.py simple view + ten advanced tabs
  register.py    Start menu shortcut, Installed apps entry, AppUserModelID
  com.py         the little bit of COM the two above need
  app.py         the engine that ties it together
tools/
  make_icon.py       renders voxkey/assets/voxkey.ico from the bar's glyph
  replay_history.py  runs your own history through the pipeline as it is now
```

Settings, history and the log live in `%APPDATA%\VoxKey`.

`selftest.py` checks everything that does not need a microphone or a window,
and puts your clipboard back when it is done. `tools\replay_history.py` needs
Ollama and reads your history; it writes nothing.

## Changes in 1.1.0

- The model's rewrites are checked, retried without examples, and fall back
  to the rules. Found because a dictation came back with one of the prompt's
  own examples in it.
- The grammar chord fixes in sentence-sized pieces and no longer corrupts the
  end of a long paragraph.
- Prompt and Grammar run at temperature zero.
- The vocabulary is handed to Whisper as hotwords; the previous dictation is
  handed over as context; the decoder gets a temperature ladder; stuck loops
  are folded; near-miss names are snapped to the vocabulary.
- No paste into nowhere: when the focus is on something that cannot take
  text, the words stay on the clipboard and the bar says so.
- VoxKey is in the Start menu and under Installed apps, opening it while it
  runs brings up its window, and it has an icon. `--quit` and `--uninstall`.
- A held W key in a game is no longer a "stuck key" notification.
- Words learned with a trailing full stop are dropped.
