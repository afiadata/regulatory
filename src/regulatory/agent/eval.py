"""Evaluation runner for the golden QA suite.

Two modes:
  - Recorded (default): replays committed transcripts, no API cost.
  - Live (--live):  calls real Anthropic API, writes new transcripts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import structlog
import yaml

log = structlog.get_logger(__name__)

_GOLDEN_QA_PATH = Path(__file__).parents[3] / "tests" / "agent" / "golden_qa.yaml"
_TRANSCRIPTS_DIR = Path(__file__).parents[3] / "tests" / "agent" / "eval_transcripts"


def _load_golden_qa() -> list[dict[str, Any]]:
    """Load golden_qa.yaml."""
    with _GOLDEN_QA_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)  # type: ignore[no-any-return]


def _find_latest_transcript() -> Path | None:
    """Return the most recent .jsonl transcript file, if any."""
    if not _TRANSCRIPTS_DIR.exists():
        return None
    transcripts = sorted(
        _TRANSCRIPTS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for t in transcripts:
        if not t.name.endswith("_summary.json"):
            return t
    return None


def _check_response(
    response: str, expected: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Evaluate a response against expected behaviour.

    Args:
        response: Agent response text.
        expected: Expected behaviour dict from golden_qa.yaml.

    Returns:
        Tuple of (passed, list_of_failure_reasons).
    """
    failures: list[str] = []

    if expected.get("must_cite") and "[doc:" not in response and "[signal:" not in response:
        failures.append("missing citations")

    must_refuse = expected.get("must_refuse", False)
    if must_refuse and not any(
        phrase in response
        for phrase in [
            "can't advise",
            "cannot advise",
            "can't offer legal",
            "can't speak to",
            "I can't help",
            "regulatory analyst assistant",
        ]
    ):
        failures.append("expected refusal but got answer")

    for phrase in expected.get("must_contain_phrases", []):
        if phrase.lower() not in response.lower():
            failures.append(f"missing phrase: {phrase!r}")

    for phrase in expected.get("must_not_contain_phrases", []):
        if phrase.lower() in response.lower():
            failures.append(f"forbidden phrase present: {phrase!r}")

    caveat = expected.get("must_include_caveat")
    if caveat == "synthetic_data" and "synthetic" not in response.lower():
        failures.append("missing synthetic-data caveat")
    if caveat == "count_inflation" and "enforcement document" not in response.lower():
        failures.append("missing count-inflation caveat")

    return len(failures) == 0, failures


async def run_eval(*, live: bool = False) -> None:
    """Run the golden QA eval suite.

    Args:
        live: If True, call the real Anthropic API and write transcripts.
              If False, replay committed transcripts.
    """
    questions = _load_golden_qa()

    if not live:
        transcript_path = _find_latest_transcript()
        if transcript_path is None:
            print(
                "No committed transcripts found. "
                "Run `regulatory agent eval run --live` first to generate them.",
                file=sys.stderr,
            )
            return

        print(f"Replaying transcript: {transcript_path.name}")
        _run_recorded(questions, transcript_path)
        return

    # Live mode — requires API key.
    import uuid

    from regulatory.agent.key_handling import acquire_api_key
    from regulatory.agent.runner import AgentRunner, _load_agent_config

    config = _load_agent_config()
    models = config.get("models", {})
    primary = models.get("primary", "claude-sonnet-4-6")
    fallback = models.get("fallback", "claude-haiku-4-5-20251001")

    # Validate key before showing cost estimate.
    api_key = acquire_api_key(fallback_model=fallback)

    # Estimate cost (rough: ~1K tokens per question at Sonnet pricing).
    n_questions = len(questions)
    est_cost = n_questions * 0.018  # ~$0.018 per question at 1K in + 200 out tokens
    budgets = config.get("budgets", {})
    daily_cap = float(budgets.get("cost_per_day_usd", 5.0))

    print(
        f"\nAbout to run live eval against:\n"
        f"  primary:  {primary}\n"
        f"  fallback: {fallback}\n"
        f"Estimated cost: ${est_cost:.2f} across {n_questions} questions.\n"
        f"Daily cap: ${daily_cap:.2f}.\n"
    )
    try:
        confirm = input("Proceed? (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        confirm = "n"

    if confirm != "y":
        print("Aborted.")
        return


    results: list[dict[str, Any]] = []
    transcripts: list[dict[str, Any]] = []

    for q in questions:
        runner = AgentRunner(
            api_key=api_key,
            config=config,
            conversation_id=uuid.uuid4(),
        )
        try:
            response = await runner.run_turn(q["user"])
        except Exception as exc:
            response = f"[ERROR: {exc}]"

        passed, failures = _check_response(response, q.get("expected", {}))
        result = {
            "id": q["id"],
            "category": q.get("category"),
            "user": q["user"],
            "passed": passed,
            "failures": failures,
        }
        results.append(result)
        transcripts.append({**result, "response": response})

        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {q['id']}: {q['user'][:60]}")
        if failures:
            for f in failures:
                print(f"         - {f}")

    # Write transcripts.
    _TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    import subprocess

    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        sha = "unknown"

    transcript_file = _TRANSCRIPTS_DIR / f"{sha}.jsonl"
    with transcript_file.open("w", encoding="utf-8") as fh:
        for t in transcripts:
            fh.write(json.dumps(t) + "\n")

    summary_file = _TRANSCRIPTS_DIR / f"{sha}_summary.json"
    passed_count = sum(1 for r in results if r["passed"])
    with summary_file.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "commit": sha,
                "total": len(results),
                "passed": passed_count,
                "failed": len(results) - passed_count,
                "pass_rate": f"{passed_count / len(results) * 100:.1f}%",
                "results": results,
            },
            fh,
            indent=2,
        )

    print(
        f"\nEval complete: {passed_count}/{len(results)} passed "
        f"({passed_count / len(results) * 100:.1f}%)"
    )
    print(f"Transcripts: {transcript_file}")
    print(f"Summary:     {summary_file}")


def _run_recorded(
    questions: list[dict[str, Any]], transcript_path: Path
) -> None:
    """Replay a committed transcript and report pass/fail.

    Args:
        questions: Golden QA question list.
        transcript_path: Path to the .jsonl transcript file.
    """
    transcripts: dict[str, dict[str, Any]] = {}
    with transcript_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            transcripts[entry["id"]] = entry

    passed = 0
    failed = 0
    for q in questions:
        qid = q["id"]
        entry = transcripts.get(qid)
        if entry is None:
            print(f"  [SKIP] {qid}: no transcript")
            continue
        response = entry.get("response", "")
        ok, failures = _check_response(response, q.get("expected", {}))
        if ok:
            passed += 1
            print(f"  [PASS] {qid}")
        else:
            failed += 1
            print(f"  [FAIL] {qid}: {'; '.join(failures)}")

    total = passed + failed
    if total:
        print(f"\nResult: {passed}/{total} passed ({passed / total * 100:.1f}%)")
