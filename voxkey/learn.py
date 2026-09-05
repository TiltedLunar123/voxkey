"""What VoxKey picks up from watching you use it.

Two honest signals, both local:

* the distinctive words you actually dictate, fed back to Whisper as a
  recognition hint so it stops mangling them;
* which cleanup profile you choose in which application, so the per-app rules
  end up matching what you really do rather than what was guessed for you.

It cannot learn a word it has never once heard correctly. This reinforces what
already gets through, which is the part that measurably helps.
"""

from __future__ import annotations

import json
import re
import threading
import time

from .config import CONFIG_DIR

LEARNED_PATH = CONFIG_DIR / "learned.json"

PROMOTE_AFTER = 3      # sightings before a term is trusted
MAX_TERMS = 40         # the ASR hint has to stay short to stay useful
PROFILE_AFTER = 3      # consistent manual picks before a rule is learned

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#._\-']*")
_SENTENCE = re.compile(r"(?<=[.!?\n])\s+")

# Capitalised words that start clauses constantly and mean nothing on their own.
COMMON = {
    "the", "and", "but", "for", "not", "you", "your", "yours", "our", "ours",
    "this", "that", "these", "those", "there", "their", "then", "than", "them",
    "they", "have", "has", "had", "was", "were", "will", "would", "should",
    "could", "can", "did", "does", "done", "with", "from", "into", "onto",
    "about", "after", "before", "because", "just", "like", "make", "made",
    "need", "needs", "want", "wants", "know", "think", "thing", "things",
    "when", "what", "where", "which", "while", "who", "why", "how", "some",
    "also", "still", "here", "very", "much", "more", "most", "well", "good",
    "okay", "yeah", "yes", "sure", "right", "let", "lets", "get", "got",
    "going", "gonna", "really", "maybe", "probably", "actually", "basically",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
}


TRAILING = ".,;:!?'\"-_"


def _distinctive(token: str, first_in_sentence: bool) -> bool:
    """Is this worth remembering, or is it just a capitalised ordinary word?"""
    # The token pattern allows dots and dashes so that "SY0-701" and "voxkey.py"
    # survive, which also means a sentence-final "again." arrives with its full
    # stop attached and then looks like an identifier. Strip the tail first.
    bare = token.rstrip(TRAILING).strip("'")
    if len(bare) < 3 or bare.lower() in COMMON:
        return False
    # Symbols and digits mean a product or identifier: Security+, SY0-701, useQuiz.
    if any(character.isdigit() for character in bare) or any(c in bare for c in "+#._-"):
        return True
    if bare[0].isupper() and not first_in_sentence:
        return True
    # A capital inside the word is camelCase or a brand.
    return any(character.isupper() for character in bare[1:])


class Learner:
    def __init__(self, config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self.terms: dict[str, dict] = {}     # term -> {"n": count, "at": epoch}
        self.app_profiles: dict[str, dict[str, int]] = {}
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        try:
            raw = json.loads(LEARNED_PATH.read_text(encoding="utf-8"))
            self.terms = raw.get("terms", {})
            self.app_profiles = raw.get("app_profiles", {})
        except (OSError, ValueError, AttributeError):
            self.terms, self.app_profiles = {}, {}

    def save(self) -> None:
        try:
            LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
            LEARNED_PATH.write_text(
                json.dumps({"terms": self.terms, "app_profiles": self.app_profiles}, indent=1),
                encoding="utf-8",
            )
        except OSError:
            pass

    def forget_all(self) -> None:
        with self._lock:
            self.terms, self.app_profiles = {}, {}
        self.save()

    def forget_term(self, term: str) -> None:
        with self._lock:
            self.terms.pop(term, None)
        self.save()

    # -- vocabulary -------------------------------------------------------
    def observe_text(self, text: str) -> None:
        if not self.config.get("learn.vocabulary", True) or not text.strip():
            return
        manual = {t.strip().lower() for t in self.config.get("asr.vocabulary", [])}
        now = time.time()
        with self._lock:
            for sentence in _SENTENCE.split(text):
                for index, match in enumerate(_TOKEN.finditer(sentence)):
                    token = match.group(0).rstrip(TRAILING)
                    if not token or token.lower() in manual:
                        continue  # already pinned by hand
                    if not _distinctive(token, first_in_sentence=(index == 0)):
                        continue
                    entry = self.terms.setdefault(token, {"n": 0, "at": now})
                    entry["n"] += 1
                    entry["at"] = now
            self._prune()
        self.save()

    def _prune(self) -> None:
        if len(self.terms) <= MAX_TERMS * 4:
            return
        # Keep the ones used most, breaking ties on how recently they were said.
        ranked = sorted(
            self.terms.items(), key=lambda kv: (kv[1]["n"], kv[1]["at"]), reverse=True
        )
        self.terms = dict(ranked[: MAX_TERMS * 2])

    def promoted_terms(self) -> list[str]:
        """Terms confident enough to hand to the recogniser."""
        if not self.config.get("learn.vocabulary", True):
            return []
        with self._lock:
            ready = [
                (term, data) for term, data in self.terms.items()
                if data.get("n", 0) >= PROMOTE_AFTER
            ]
        ready.sort(key=lambda kv: (kv[1]["n"], kv[1]["at"]), reverse=True)
        return [term for term, _ in ready[:MAX_TERMS]]

    def pending_terms(self) -> list[tuple[str, int]]:
        with self._lock:
            items = [(term, data.get("n", 0)) for term, data in self.terms.items()]
        items.sort(key=lambda kv: kv[1], reverse=True)
        return items

    # -- profile habits ---------------------------------------------------
    def observe_profile_choice(self, app: str, profile: str) -> None:
        """Called when the profile is changed by hand while `app` is in front."""
        if not self.config.get("learn.profiles", True) or not app or not profile:
            return
        with self._lock:
            counts = self.app_profiles.setdefault(app.lower(), {})
            counts[profile] = counts.get(profile, 0) + 1
        self.save()

    def profile_for(self, app: str) -> str | None:
        if not self.config.get("learn.profiles", True) or not app:
            return None
        with self._lock:
            counts = self.app_profiles.get(app.lower(), {})
        if not counts:
            return None
        profile, count = max(counts.items(), key=lambda kv: kv[1])
        return profile if count >= PROFILE_AFTER else None

    def learned_rules(self) -> list[tuple[str, str, int]]:
        rows: list[tuple[str, str, int]] = []
        with self._lock:
            for app, counts in self.app_profiles.items():
                if not counts:
                    continue
                profile, count = max(counts.items(), key=lambda kv: kv[1])
                rows.append((app, profile, count))
        rows.sort(key=lambda row: row[2], reverse=True)
        return rows
