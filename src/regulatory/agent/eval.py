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

_MAX_CONSECUTIVE_ERRORS: int = 3


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


def _check_response(response: str, expected: dict[str, Any]) -> tuple[bool, list[str]]:
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


async def _preflight_db_check() -> None:
    """Verify DB is reachable before starting the eval.

    Without this check, a Docker outage produces identical connection-error
    'responses' that the per-question pass/fail logic evaluates against,
    producing meaningless results from a run that never made an API call.
    """
    from sqlalchemy import text

    from regulatory.db.session import get_session

    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise RuntimeError(
            f"Database connection failed: {exc}\n"
            "Ensure Postgres is running (Docker container started, "
            "DATABASE_URL correct, port reachable) and re-run."
        ) from exc


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

    # Live mode — requires API key and healthy DB.
    import uuid

    from regulatory.agent.key_handling import acquire_api_key
    from regulatory.agent.runner import AgentRunner, _load_agent_config

    await _preflight_db_check()

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
    consecutive_errors = 0
    aborted = False

    for q in questions:
        runner = AgentRunner(
            api_key=api_key,
            config=config,
            conversation_id=uuid.uuid4(),
        )
        try:
            response = await runner.run_turn(q["user"])
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            log.error("eval_question_error", qid=q["id"], error=str(exc))
            err_result: dict[str, Any] = {
                "id": q["id"],
                "category": q.get("category"),
                "user": q["user"],
                "errored": True,
                "error": str(exc),
            }
            results.append(err_result)
            transcripts.append({**err_result, "response": ""})
            print(f"  [ERROR] {q['id']}: {str(exc)[:80]}")
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                aborted = True
                print(
                    f"\nEval ABORTED after {_MAX_CONSECUTIVE_ERRORS} consecutive errors. "
                    f"Last error: {exc}"
                )
                break
            continue

        passed, failures = _check_response(response, q.get("expected", {}))
        result: dict[str, Any] = {
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
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        sha = "unknown"

    transcript_file = _TRANSCRIPTS_DIR / f"{sha}.jsonl"
    with transcript_file.open("w", encoding="utf-8") as fh:
        for t in transcripts:
            fh.write(json.dumps(t) + "\n")

    summary_file = _TRANSCRIPTS_DIR / f"{sha}_summary.json"
    passed_count = sum(1 for r in results if r.get("passed", False))
    failed_count = sum(
        1 for r in results if not r.get("passed", False) and not r.get("errored", False)
    )
    errored_count = sum(1 for r in results if r.get("errored", False))
    scored_count = passed_count + failed_count
    pass_rate = f"{passed_count / scored_count * 100:.1f}%" if scored_count else "N/A"

    with summary_file.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "commit": sha,
                "total": len(questions),
                "scored": scored_count,
                "passed": passed_count,
                "failed": failed_count,
                "errored": errored_count,
                "aborted": aborted,
                "pass_rate": pass_rate,
                "results": results,
            },
            fh,
            indent=2,
        )

    summary_line = f"\nEval complete: {passed_count}/{scored_count} passed ({pass_rate})"
    if errored_count:
        summary_line += f", {errored_count} errored"
    if aborted:
        summary_line += " [ABORTED]"
    print(summary_line)
    print(f"Transcripts: {transcript_file}")
    print(f"Summary:     {summary_file}")


def _run_recorded(questions: list[dict[str, Any]], transcript_path: Path) -> None:
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
        if entry.get("errored"):
            print(f"  [ERROR] {qid}: {entry.get('error', 'unknown error')}")
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
