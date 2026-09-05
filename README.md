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

It lives in the tray, and ticking "Start VoxKey when Windows starts" means you
never run that again. The speech model downloads on first launch.

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
  Rewriting, then Sent.

It sits bottom right and dims to half opacity when idle, so it stays clear of
the text box you are dictating into, then brightens on hover and while working.
Drag it anywhere and it snaps to the nearest screen edge and stays there next
time. Right-click for Settings, History, Paste last transcript, Hide for an
hour, and a toggle for whether it stays up when idle.

The bar carries `WS_EX_NOACTIVATE`, so clicking it never takes focus off the
window you were typing in. That matters: if it stole focus, Stop would paste
your text into the bar instead of your document. It is also a tool window, so
it stays out of alt-tab and off the taskbar.

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

## Speed

Measured here, on the RTX 5070 Ti, for an 11 second dictation:

| Step | Time |
| --- | --- |
| Transcription (`large-v3-turbo`, CUDA fp16) | ~0.40 s |
| Rule profiles | ~0.003 s |
| Model rewrite (`qwen3:4b-instruct`) | ~0.30 s |

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

Scored against ten held-out sentences that appear nowhere in its examples, it
fixes 10 of 10; on ten error classes never named in its instruction, 9 of 10.
That came from naming the error classes outright rather than asking for "correct
grammar", which took it from 5 of 10. A 7B model was tried and scored *worse*,
because it expands contractions and so changes your register.

Guards worth knowing about:

- A sentinel is parked on the clipboard first. If the app ignores Ctrl+C, or
  Ctrl+A grabbed files rather than text, nothing is pasted and you get told why.
- If focus moves while it is thinking, the corrected text goes to the clipboard
  rather than into whatever window you switched to.
- Text longer than the limit is split on blank lines and fixed a few paragraphs
  at a time, so a long document does not overflow the model's context.

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

Near-silent clips are dropped rather than transcribed. Handed silence, Whisper
confidently returns "Thank you.", which is exactly what a 1.1 second empty take
produced before.

## Learning as it goes

Nothing here is uploaded; it all lives in `%APPDATA%\VoxKey\learned.json`.

- **The profile follows the window.** Dictating into VS Code gets Prompt, into
  Outlook or a Gmail tab gets Email, into Slack or Discord gets Casual. Rules
  are editable on the Smart tab.
- **It notices when you disagree.** Change profile from the tray after
  dictating into an app three times and that becomes the rule for that app,
  ahead of the shipped ones.
- Distinctive words you actually dictate are fed Distinctive words you actually dictate are fed
  back to Whisper as a recognition hint after the third use. This reinforces
  words that already get through; it cannot teach one the recogniser has never
  once heard correctly.
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
- Words it keeps mishearing go in two places: **Vocabulary** (nudges Whisper)
  and **Always replace** (a hard find-and-replace applied last).

## Layout

```
voxkey/
  hotkey.py      chord detection (GetAsyncKeyState polling)
  audio.py       microphone capture, level meter, blips
  asr.py         faster-whisper wrapper, CUDA DLL bootstrap
  cleanup/
    rules.py     deterministic tidying
    llm.py       Ollama client, guardrails, few-shot
    pipeline.py  profile routing
  inject.py      clipboard and SendInput delivery
  overlay.py     floating status pill
  tray.py        tray icon and menu
  settings_ui.py simple view + eight advanced tabs
  app.py         the engine that ties it together
```

Settings, history and the log live in `%APPDATA%\VoxKey`.
