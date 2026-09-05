"""Routes a transcript through the selected cleanup profile."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from ..config import PROFILES
from . import rules
from .llm import OllamaClient

log = logging.getLogger("voxkey.cleanup")

RULE_LEVELS = {"minimal": "minimal", "punctuation": "punctuation", "clean": "clean"}


@dataclass
class ProfileResult:
    text: str
    profile: str
    used_llm: bool = False
    warning: str | None = None
    ms: float = 0.0


class CleanupPipeline:
    def __init__(self, config) -> None:
        self.config = config
        self.llm = OllamaClient(config)

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
            rewritten = self.llm.rewrite(pre, instruction, on_status, examples)
        except Exception as exc:
            log.warning("rewrite failed: %s", exc)
            if self.config.get("llm.fallback_to_rules", True):
                return finish(
                    ProfileResult(pre, profile, warning=f"Model unavailable, used Clean up ({exc})")
                )
            raise

        out = rewritten
        if self.config.get("cleanup.no_em_dashes", True):
            out = rules.replace_em_dashes(out)
        out = rules.apply_replacements(out, self.config.get("cleanup.replacements", []))
        return finish(ProfileResult(rules.tidy_spacing(out), profile, used_llm=True))


CHUNK_CHARS = 2200          # keeps a chunk plus its reply inside num_ctx


def _split_paragraphs(text: str, limit: int) -> list[str]:
    """Group paragraphs into chunks small enough for one pass."""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if current and len(candidate) > limit:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


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

    instruction = pipeline.instruction_for(profile)
    examples = PROFILES.get(profile, {}).get("examples")
    chunks = _split_paragraphs(text, CHUNK_CHARS)

    fixed: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        if on_status and len(chunks) > 1:
            on_status(f"Fixing {index} of {len(chunks)}")
        fixed.append(pipeline.llm.rewrite(chunk, instruction, None, examples))

    out = "\n\n".join(fixed)
    if config.get("cleanup.no_em_dashes", True):
        out = rules.replace_em_dashes(out)
    out = rules.apply_replacements(out, config.get("cleanup.replacements", []))

    warning = f"only the first {limit} characters were fixed" if truncated else None
    return ProfileResult(
        out, profile, used_llm=True, warning=warning,
        ms=(time.perf_counter() - started) * 1000,
    )
