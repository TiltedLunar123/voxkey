"""Routes a transcript through the selected cleanup profile."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

from ..config import PROFILES
from . import guard, rules
from .llm import OllamaClient

log = logging.getLogger("voxkey.cleanup")

RULE_LEVELS = {"minimal": "minimal", "punctuation": "punctuation", "clean": "clean"}

# Profiles whose whole job is to keep what was said. They run at temperature
# zero, which changes nothing about the model's load state: only num_ctx does.
FAITHFUL_PROFILES = {"grammar", "prompt"}


@dataclass
class ProfileResult:
    text: str
    profile: str
    used_llm: bool = False
    warning: str | None = None
    ms: float = 0.0
    # "" for a rule profile, else clean / retried / rejected / unchecked.
    guard: str = ""


class CleanupPipeline:
    def __init__(self, config) -> None:
        self.config = config
        self.llm = OllamaClient(config)
        # What happened to the most recent model rewrite, for Diagnostics.
        self.last_guard = "no rewrite yet"

    def warm(self) -> bool:
        """Prime the rewriter with the real prompt shape, not just a ping.

        Loading the weights is only half the cost. The system prompt and the
        few-shot turns are another few hundred tokens of prompt evaluation, and
        skipping them here just moves that onto the user's first dictation,
        which measured about six seconds instead of the usual four hundred ms.
        """
        profile = self.config.get("cleanup.profile", "clean")
        if not PROFILES.get(profile, {}).get("llm"):
            # A rule profile is active, but the user can switch from the tray at
            # any moment, so still get the weights resident.
            profile = "formal"
        return self.llm.warm(
            self.instruction_for(profile), PROFILES.get(profile, {}).get("examples")
        )

    def instruction_for(self, profile: str) -> str:
        if profile == "custom":
            return self.config.get("cleanup.custom_prompt", "").strip() or PROFILES["clean"]["blurb"]
        return PROFILES.get(profile, {}).get("prompt", "")

    # -- the checked rewrite ------------------------------------------------
    @staticmethod
    def _problem_with(source: str, output: str, profile: str, targets: list[str]) -> str:
        leaked = guard.leaked_phrases(source, output, targets)
        if leaked:
            return f"it copied its own example ({leaked[0]})"
        fidelity = guard.check_fidelity(source, output, profile)
        return "" if fidelity.ok else fidelity.reason

    def rewrite_checked(
        self,
        text: str,
        profile: str,
        instruction: str,
        examples: list[tuple[str, str]] | None,
        on_status: Callable[[str], None] | None = None,
    ) -> tuple[str | None, str, str]:
        """Rewrite, check the result, retry once without examples if it is off.

        Returns (text, outcome, detail). text is None when both attempts were
        rejected, and the caller falls back to the rules rather than paste a
        rewrite that drifted from what was said.
        """
        temperature = 0.0 if profile in FAITHFUL_PROFILES else None
        out = self.llm.rewrite(text, instruction, on_status, examples, temperature=temperature)
        if not self.config.get("llm.fidelity_guard", True):
            return out, "unchecked", ""

        problem = self._problem_with(text, out, profile, self.llm.shot_targets(examples))
        if not problem:
            return out, "clean", ""

        log.warning("rewrite rejected because %s; retrying without examples", problem)
        if on_status:
            on_status("Checking the rewrite...")
        again = self.llm.rewrite(
            text, instruction, None, examples, temperature=0.0, use_shots=False
        )
        problem_again = self._problem_with(text, again, profile, [])
        if not problem_again:
            return again, "retried", problem
        log.warning("rewrite rejected again because %s; using the rules", problem_again)
        return None, "rejected", problem_again

    def _note_guard(self, outcome: str, detail: str) -> None:
        stamp = time.strftime("%H:%M")
        if outcome == "clean":
            self.last_guard = f"passed at {stamp}"
        elif outcome == "retried":
            self.last_guard = f"retried without examples at {stamp} ({detail})"
        elif outcome == "rejected":
            self.last_guard = f"REJECTED at {stamp}, used the rules ({detail})"
        else:
            self.last_guard = f"guard switched off, last rewrite at {stamp}"

    def process(
        self,
        text: str,
        profile: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> ProfileResult:
        started = time.perf_counter()
        profile = profile or self.config.get("cleanup.profile", "clean")
        if profile not in PROFILES:
            profile = "clean"

        def finish(result: ProfileResult) -> ProfileResult:
            result.ms = (time.perf_counter() - started) * 1000
            return result

        if not text.strip():
            return finish(ProfileResult("", profile))

        if profile == "none":
            return finish(ProfileResult(text.strip(), profile))

        if profile in RULE_LEVELS:
            return finish(ProfileResult(rules.run(text, self.config, RULE_LEVELS[profile]), profile))

        # Model-backed profile: tidy first so the model is not paid to delete "um".
        pre = rules.run(text, self.config, "clean", protect_corrections=True)
        if not self.config.get("llm.enabled", True):
            return finish(ProfileResult(pre, profile, warning="Model rewriting is switched off"))

        instruction = self.instruction_for(profile)
        examples = PROFILES.get(profile, {}).get("examples")
        try:
            rewritten, outcome, detail = self.rewrite_checked(
                pre, profile, instruction, examples, on_status
            )
        except Exception as exc:
            log.warning("rewrite failed: %s", exc)
            if self.config.get("llm.fallback_to_rules", True):
                return finish(
                    ProfileResult(pre, profile, warning=f"Model unavailable, used Clean up ({exc})")
                )
            raise

        self._note_guard(outcome, detail)
        if rewritten is None:
            return finish(ProfileResult(
                pre, profile, guard=outcome,
                warning=f"The rewrite was discarded because {detail}. Used Clean up instead.",
            ))

        out = rewritten
        if self.config.get("cleanup.no_em_dashes", True):
            out = rules.replace_em_dashes(out)
        out = rules.apply_replacements(out, self.config.get("cleanup.replacements", []))
        return finish(ProfileResult(rules.tidy_spacing(out), profile, used_llm=True, guard=outcome))


# A whole paragraph of a thousand characters is too much for a 4B model to fix
# in one go: on the machine this was built on it corrupted the last clause six
# times out of six ("on my computer" came back as "on my when done"), and never
# once did when handed two sentence-sized pieces instead.
FIX_CHUNK_CHARS = 600
CHUNK_CHARS = FIX_CHUNK_CHARS

# A sentence end, with any closing quote or bracket, and the whitespace after
# it; or a blank line. Captured, so the text can be put back exactly as it was.
_BOUNDARY = re.compile(r"""((?<=[.!?])["')\]]*\s+|\n[ \t]*\n\s*)""")


def split_for_fixing(text: str, limit: int = FIX_CHUNK_CHARS) -> list[tuple[str, str]]:
    """Cut text into pieces of at most `limit` characters at sentence ends and
    blank lines. Returns (piece, the gap that followed it) pairs; joining each
    piece to its gap reproduces the input byte for byte, line breaks included.
    """
    parts = _BOUNDARY.split(text)
    sentences = parts[0::2]
    gaps = parts[1::2] + [""]
    out: list[tuple[str, str]] = []
    piece, gap_after = "", ""
    for sentence, gap in zip(sentences, gaps):
        candidate = f"{piece}{gap_after}{sentence}" if piece else sentence
        if piece and len(candidate) > limit:
            out.append((piece, gap_after))
            piece, gap_after = sentence, gap
        else:
            piece, gap_after = candidate, gap
    if piece or not out:
        out.append((piece, gap_after))
    return out


def _split_paragraphs(text: str, limit: int) -> list[str]:
    """Kept for callers that only want the pieces."""
    return [piece for piece, _gap in split_for_fixing(text, limit)]


def fix_text(pipeline: CleanupPipeline, text: str, on_status=None) -> ProfileResult:
    """Grammar-check text that is already written, not dictated.

    Deliberately skips the dictation rules: filler stripping, forced sentence
    capitals and a trailing full stop are all wrong for prose someone has
    already typed, and tidy_spacing would flatten the indentation of any code
    caught in the selection.
    """
    started = time.perf_counter()
    config = pipeline.config
    profile = config.get("fix.profile", "grammar")
    if profile not in PROFILES:
        profile = "grammar"

    limit = int(config.get("fix.max_chars", 12000))
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]

    # Settle the mechanical classes before the model sees the text. These are
    # certain, they cost no tokens, and a 4B model kept getting subject-case
    # pronouns wrong however the instruction was worded.
    if config.get("fix.deterministic_pass", True):
        text = rules.fix_mechanical(text)
        text = rules.fix_subject_pronouns(text)

    instruction = pipeline.instruction_for(profile)
    examples = PROFILES.get(profile, {}).get("examples")
    pieces = split_for_fixing(text, FIX_CHUNK_CHARS)

    fixed: list[str] = []
    kept = 0
    for index, (piece, gap) in enumerate(pieces, 1):
        if on_status and len(pieces) > 1:
            on_status(f"Fixing {index} of {len(pieces)}")
        if not piece.strip():
            fixed.append(piece + gap)
            continue
        result, outcome, detail = pipeline.rewrite_checked(piece, profile, instruction, examples)
        pipeline._note_guard(outcome, detail)
        if result is None:
            # The certain fixes already went in; better that than a rewrite
            # that drifted from what was written.
            kept += 1
            result = piece
        fixed.append(result + gap)

    out = "".join(fixed)
    if config.get("cleanup.no_em_dashes", True):
        out = rules.replace_em_dashes(out)
    out = rules.apply_replacements(out, config.get("cleanup.replacements", []))

    notes = []
    if truncated:
        notes.append(f"only the first {limit} characters were fixed")
    if kept:
        notes.append(
            f"{kept} of {len(pieces)} parts kept only the certain fixes, because the "
            "model's version drifted from what was written"
        )
    return ProfileResult(
        out, profile, used_llm=True, warning="; ".join(notes) or None,
        ms=(time.perf_counter() - started) * 1000, guard="rejected" if kept else "clean",
    )
