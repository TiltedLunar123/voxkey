"""Synthesise the benchmark sentences to WAV with the local Kokoro voice.

Runs under the media-tools interpreter, not VoxKey's venv:

    C:/Users/hilge/.local/media-tools/Scripts/python.exe tools/bench_speech.py out_dir

Kokoro writes 24k mono; the recogniser resamples to 16k itself in bench_asr.py.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_sentences import EXPECTED, SENTENCES  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "bench_audio")
    out.mkdir(parents=True, exist_ok=True)

    from kokoro_tts import synth

    manifest = []
    for index, (sentence, expected) in enumerate(zip(SENTENCES, EXPECTED)):
        wav = out / f"{index:03d}.wav"
        if not wav.exists():
            synth(sentence, str(wav))
            print(f"  {index:03d}  {sentence[:60]}")
        manifest.append({"wav": wav.name, "text": expected, "spoken": sentence})

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"{len(manifest)} clips in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
