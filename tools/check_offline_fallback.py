"""Prove that a dead Ollama degrades the grammar fix to the certain fixes
instead of throwing the selection away. Run: .venv\\Scripts\\python tools\\check_offline_fallback.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxkey.config import Config
from voxkey.cleanup.pipeline import CleanupPipeline, fix_text

failures = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'' if ok else '  <- ' + str(detail)}")
    if not ok:
        failures.append(name)


config = Config()
pipeline = CleanupPipeline(config)

SOURCE = "me and him went to the store. we didnt find it there."


def dead(*args, **kwargs):
    raise RuntimeError(
        "HTTPConnectionPool(host='127.0.0.1', port=11434): Max retries exceeded"
    )


calls = {"n": 0}


def counted(*args, **kwargs):
    calls["n"] += 1
    return dead()


pipeline.llm.rewrite = counted

print("grammar fix with the model down")
try:
    result = fix_text(pipeline, SOURCE)
    raised = None
except Exception as exc:  # noqa: BLE001
    result, raised = None, exc

check("it does not raise", raised is None, raised)

if result is not None:
    check("it returns text", bool(result.text.strip()), repr(result.text))
    check("the deterministic pass still ran", result.text != SOURCE, repr(result.text))
    check("it reports the model was not used", result.used_llm is False, result.used_llm)
    check("the guard says unavailable", result.guard == "unavailable", result.guard)
    check("it warns why", "unreachable" in (result.warning or ""), result.warning)
    check("diagnostics says so", "unreachable" in pipeline.last_guard, pipeline.last_guard)
    print(f"  text: {result.text!r}")

print("it stops asking a server that refused once")
calls["n"] = 0
long_text = " ".join(["this is a sentence that needs fixing dont it."] * 40)
fix_text(pipeline, long_text)
check("only one attempt for many pieces", calls["n"] == 1, f"{calls['n']} calls")

print()
print("FAILED: " + ", ".join(failures) if failures else "all checks passed")
sys.exit(1 if failures else 0)
