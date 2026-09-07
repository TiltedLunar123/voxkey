"""Settings model, defaults and JSON persistence."""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "VoxKey"
CONFIG_PATH = CONFIG_DIR / "settings.json"
HISTORY_PATH = CONFIG_DIR / "history.json"
LOG_PATH = CONFIG_DIR / "voxkey.log"

DEFAULT_FILLERS = [
    "um", "uh", "erm", "ah", "eh", "hmm", "mhm",
    "you know", "i mean", "like", "sort of", "kind of",
    "basically", "actually", "literally", "right?",
]

DEFAULT_VOCABULARY = [
    # Examples. Replace these with the names and jargon you actually dictate;
    # anything here is fed to the recogniser as a hint.
    "Kubernetes", "PostgreSQL", "TypeScript", "OAuth", "VoxKey",
]

# Each profile: label, blurb, whether it needs the language model, and the
# instruction handed to that model when it does.
PROFILES: dict[str, dict[str, Any]] = {
    "none": {
        "label": "None (verbatim)",
        "blurb": "Exactly what the model heard. No edits at all.",
        "llm": False,
        "prompt": "",
    },
    "minimal": {
        "label": "Minimal fixes",
        "blurb": "Whitespace, capitalisation and a closing full stop. Nothing else.",
        "llm": False,
        "prompt": "",
    },
    "punctuation": {
        "label": "Punctuation",
        "blurb": "Minimal fixes plus spoken punctuation commands and sentence splitting.",
        "llm": False,
        "prompt": "",
    },
    "clean": {
        "label": "Clean up",
        "blurb": "Punctuation plus filler words, stutters and false starts removed.",
        "llm": False,
        "prompt": "",
    },
    "casual": {
        "label": "Casual tone",
        "blurb": "Rewritten to read like a relaxed message. Contractions, short sentences.",
        "llm": True,
        "examples": [
            ("so um what time is the standup tomorrow do you know",
             "So what time's the standup tomorrow, do you know?"),
            ("um so i guess what i am trying to say is that the the build is broken again and i do not know why",
             "So what I'm trying to say is the build is broken again and I don't know why."),
        ],
        "prompt": (
            "Rewrite the dictated text so it reads like a relaxed, natural message "
            "between people who know each other. Use contractions. Keep it short and "
            "plain. Keep the speaker's own words and personality wherever you can. "
            "Do not add information, opinions, greetings or sign-offs."
        ),
    },
    "formal": {
        "label": "Formal tone",
        "blurb": "Rewritten as professional prose. Full words, complete sentences.",
        "llm": True,
        "examples": [
            ("so um what time is the standup tomorrow do you know",
             "What time is the standup tomorrow?"),
            ("so um i was thinking that we should probably ship it on friday i guess",
             "I recommend that we ship on Friday."),
        ],
        "prompt": (
            "Rewrite the dictated text as clear professional prose suitable for a work "
            "document. Expand contractions, cut hedging openers such as 'so', 'I was "
            "thinking that' and 'probably', and state the point directly in complete "
            "sentences. Keep it concise rather than ornate, and do not add information, "
            "flattery, greetings or sign-offs."
        ),
    },
    "email": {
        "label": "Email",
        "blurb": "Shaped into an email body with paragraph breaks. No greeting or sign-off.",
        "llm": True,
        "examples": [
            ("um so could you let me know whether the invoice went out yet",
             "Could you let me know whether the invoice has gone out yet?"),
            ("hey so the report is late because the data did not come in until tuesday sorry about that",
             "The report is late because the data did not arrive until Tuesday. Apologies for the delay."),
        ],
        "prompt": (
            "Shape the dictated text into the body of an email: clear paragraphs, "
            "professional but human tone. Do not invent a subject line, greeting or "
            "sign-off, and do not add information that was not spoken."
        ),
    },
    "notes": {
        "label": "Notes / bullets",
        "blurb": "Broken into short bullet points, one idea per line.",
        "llm": True,
        "examples": [
            ("so what do we still need to do before friday",
             "- What do we still need to do before Friday?"),
            ("we need to fix the login bug and also update the docs and then ship it",
             "- Fix the login bug\n- Update the docs\n- Ship it"),
        ],
        "prompt": (
            "Turn the dictated text into short bullet points, one idea per line, each "
            "starting with '- '. Keep the speaker's wording. Do not add points that "
            "were not spoken and do not add a heading or summary."
        ),
    },
    "prompt": {
        "label": "Prompt to an agent",
        "blurb": "Tightened into an unambiguous instruction. Good for dictating into Claude.",
        "llm": True,
        "examples": [
            ("um can you tell me why the build is failing",
             "Why is the build failing?"),
            ("um can you like go into the auth file and make the timeout longer i think it is thirty seconds right now",
             "In the auth file, increase the timeout. It is currently 30 seconds."),
        ],
        "prompt": (
            "Rewrite the dictated text as a clear, unambiguous instruction to a coding "
            "assistant. Keep every requirement and constraint the speaker stated, drop "
            "hesitation and repetition, and preserve any file paths, names or code "
            "verbatim. Do not answer the request or add requirements of your own."
        ),
    },
    "grammar": {
        "label": "Grammar fix",
        "blurb": "Corrects spelling, grammar and punctuation. Changes nothing else.",
        "llm": True,
        "examples": [
            ("whats the deadline for this project again",
             "What's the deadline for this project again?"),
            ("i seen the email yesterday and me and him was gonna reply but we dont no what to say",
             "I saw the email yesterday, and he and I were going to reply, but we don't know what to say."),
            ("the report was wrote by sarah and i and their going to send there copy tommorow",
             "The report was written by Sarah and me, and they're going to send their copy tomorrow."),
            ("he dont know nothing about it and each of the servers have there own config",
             "He doesn't know anything about it, and each of the servers has its own config."),
        ],
        # Naming the error classes outright measurably beats "fix the grammar":
        # it took a fixed set of broken sentences from 5/8 to 6/8 on the same
        # model. A vague instruction leaves a small model too willing to decide
        # a faulty sentence was already fine.
        "prompt": (
            "Fix every grammar and spelling error in the text. You must correct "
            "subject-verb agreement, verb tense, past participles ('was wrote' -> "
            "'was written'), pronoun case ('Sarah and I' as an object -> 'Sarah and "
            "me', 'me and him' as a subject -> 'he and I'), double negatives, "
            "'could of' and 'should of', their/there/they're, its/it's, to/too, "
            "adjectives used where an adverb belongs ('playing good' -> 'playing "
            "well'), indefinite pronouns that take a singular verb ('neither of the "
            "options are' -> 'is', likewise each, either and none), 'since' used for "
            "a duration ('since three days' -> 'for three days'), fewer/less, "
            "than/then, and every misspelling. Rewrite each faulty sentence so it is "
            "standard English. Keep the author's vocabulary, tone and register, keep "
            "every line break and indent, leave code, commands, file paths, URLs and "
            "proper nouns exactly as they are, and add or remove nothing. Never "
            "introduce an em dash or an en dash; use a comma, a full stop or a colon."
        ),
    },
    "custom": {
        "label": "Custom",
        "blurb": "Your own instruction, edited on the Cleanup tab.",
        "llm": True,
        "prompt": "",
    },
}

DEFAULTS: dict[str, Any] = {
    "hotkey": {
        # Ctrl+Win rather than Ctrl+Alt: it collides with far fewer application
        # shortcuts, and it is what Wispr Flow binds by default.
        "modifiers": ["ctrl", "win"],
        "key": "",
        "mode": "hold",
        "hold_threshold_ms": 250,
        "cancel_on_other_key": True,
        "profile_switch": {},
    },
    "audio": {
        "device": None,
        # The name is what actually identifies the device. The index is only a
        # hint, because PortAudio renumbers devices when hardware changes.
        "device_name": "",
        "gain": 1.0,
        "min_duration_ms": 350,
        "max_duration_s": 300,
        # Open the mic on chord-down rather than after the hold threshold, and
        # keep capturing briefly after release. Both ends were clipping words.
        "preroll": True,
        "tail_pad_ms": 200,
    },
    "asr": {
        "model": "large-v3-turbo",
        "device": "auto",
        "compute_type": "auto",
        "language": "en",
        "beam_size": 5,
        "vad_filter": True,
        "vad_min_silence_ms": 500,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "vocabulary": list(DEFAULT_VOCABULARY),
        "preload_on_start": True,
        # Hand the recogniser the end of the previous dictation as context, so
        # a name said ten minutes ago is still fresh in its mind.
        "use_context": True,
    },
    "cleanup": {
        "profile": "clean",
        "fillers": list(DEFAULT_FILLERS),
        "voice_commands": True,
        "voice_command_list": [
            {"say": "new paragraph", "insert": "\n\n", "enabled": True},
            {"say": "new line", "insert": "\n", "enabled": True},
            {"say": "bullet point", "insert": "\n- ", "enabled": True},
            {"say": "question mark", "insert": "?", "enabled": True},
            {"say": "exclamation point", "insert": "!", "enabled": True},
            {"say": "exclamation mark", "insert": "!", "enabled": True},
            {"say": "open paren", "insert": "(", "enabled": True},
            {"say": "close paren", "insert": ")", "enabled": True},
            {"say": "open quote", "insert": "“", "enabled": True},
            {"say": "close quote", "insert": "”", "enabled": True},
            {"say": "semicolon", "insert": ";", "enabled": True},
            {"say": "full stop", "insert": ".", "enabled": True},
            {"say": "period", "insert": ".", "enabled": False},
            {"say": "comma", "insert": ",", "enabled": False},
            {"say": "colon", "insert": ":", "enabled": False},
            {"say": "dash", "insert": "-", "enabled": False},
        ],
        "collapse_repeats": True,
        # "Nectar" becomes "Nekter" when Nekter is in the vocabulary and the
        # recogniser capitalised what it heard.
        "snap_vocabulary": True,
        "capitalize_sentences": True,
        "ensure_final_punctuation": True,
        "strip_trailing_period_short": True,
        "no_em_dashes": True,
        "self_corrections": True,
        # Whisper spells these out however it heard them, so cover both the
        # spoken shorthand and the expanded form.
        # Applied last, so they always win. Whisper writes what it hears, and
        # these are the shapes it tends to produce for names it does not know.
        "replacements": [
            {"from": "post gres", "to": "PostgreSQL", "regex": False, "case_sensitive": False},
            {"from": "o auth", "to": "OAuth", "regex": False, "case_sensitive": False},
        ],
        "custom_prompt": "Rewrite the dictated text to be clearer, keeping my voice.",
    },
    "llm": {
        "enabled": True,
        "base_url": "http://127.0.0.1:11434",
        # Must be a non-thinking instruct model. Hybrid reasoners such as plain
        # qwen3:4b spend thousands of tokens thinking before answering, which
        # turns a 0.3s rewrite into a 35s one even with think disabled.
        "model": "qwen3:4b-instruct",
        "timeout_s": 30,
        "temperature": 0.2,
        "num_ctx": 4096,
        "keep_alive": "10m",
        "fallback_to_rules": True,
        # Check every rewrite for copied examples and dropped content, retry
        # once without examples, and fall back to the rules if it is still off.
        "fidelity_guard": True,
    },
    "output": {
        "method": "paste",
        "restore_clipboard": True,
        "restore_delay_ms": 500,
        "type_delay_ms": 2,
        "press_enter": False,
        "trailing_space": False,
        # smart: add a space only when this dictation continues the previous one
        # in the same window. always / never override it.
        "leading_space": "smart",
        # When nothing that takes text has focus, leave the dictation on the
        # clipboard and say so, instead of pasting into thin air.
        "require_text_field": True,
    },
    "ui": {
        "overlay": True,
        "overlay_position": "bottom-center",
        # The bar sits on screen all the time, like the Wispr Flow Bar, and
        # remembers where it was dragged to.
        "bar_always": True,
        "bar_position": "bottom-right",   # a POSITIONS key, or "custom" once dragged
        "bar_x": -1,
        "bar_y": -1,
        # Sits dim and out of the way until you need it, so it stops covering
        # the text box you are dictating into.
        "bar_idle_opacity": 0.5,
        "sounds": True,
        "start_minimized": True,
        "notify_errors": True,
    },
    "fix": {
        # A second chord that grammar-checks whatever is already in the box,
        # rather than dictating something new.
        "enabled": True,
        "modifiers": ["ctrl", "win", "alt"],
        "key": "",
        "scope": "all",          # all = Ctrl+A first, selection = use what is selected
        "profile": "grammar",
        "max_chars": 12000,
        # Apply the certain corrections (missing apostrophes, "could of",
        # subject-case pronouns) by rule before the model runs.
        "deterministic_pass": True,
    },
    "context": {
        # Follow the window you are dictating into instead of one fixed profile.
        "auto_profile": True,
        "rules": [],          # empty means use context.DEFAULT_RULES
    },
    "learn": {
        "vocabulary": True,   # remember the distinctive words you actually say
        "profiles": True,     # remember which profile you pick in which app
    },
    "history": {"enabled": True, "max_items": 100},
    "advanced": {
        "start_with_windows": True,
        # A Start menu shortcut and an Installed apps entry, kept in step with
        # wherever the folder currently is.
        "register_app": True,
        "log_level": "INFO",
    },
}


def _merge(base: Any, override: Any) -> Any:
    """Recursively fill missing keys from defaults, keeping user values."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for key, value in override.items():
            out[key] = _merge(base.get(key), value) if key in base else value
        return out
    return copy.deepcopy(override)


class Config:
    """Thread-safe dotted-path access over the settings dict."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = _merge(DEFAULTS, raw)
            except FileNotFoundError:
                self.data = copy.deepcopy(DEFAULTS)
            except (json.JSONDecodeError, OSError):
                # A corrupt file should not stop the app from starting.
                self.data = copy.deepcopy(DEFAULTS)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self.data
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node) if isinstance(node, (dict, list)) else node

    def set(self, dotted: str, value: Any) -> None:
        with self._lock:
            parts = dotted.split(".")
            node = self.data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value

    def reset_section(self, section: str) -> None:
        with self._lock:
            self.data[section] = copy.deepcopy(DEFAULTS[section])
