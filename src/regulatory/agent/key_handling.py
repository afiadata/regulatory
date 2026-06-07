"""Anthropic API key detection, validation, and optional persistence.

Key never appears in:
  - agent_audit_log rows
  - application log records
  - exception tracebacks

The log scrubbing filter is installed by the runner at startup.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import stat
import sys
import time
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_SECRETS_PATH = Path.home() / ".config" / "regulatory" / "secrets.env"
_KEY_PATTERN = re.compile(r"sk-ant-[\w\-]+")
_MAX_RETRIES = 3
_VALIDATION_RETRY_DELAYS = (1.0, 2.0, 4.0)


class ApiKeyError(Exception):
    """Raised when API key acquisition or validation fails."""


class _ScrubFilter(logging.Filter):
    """Logging filter that removes API key substrings from log records."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self._key = key

    def filter(self, record: logging.LogRecord) -> bool:
        if self._key:
            # Evaluate the full formatted message (key may be in args, not msg).
            formatted = record.getMessage()
            if self._key in formatted:
                record.msg = formatted.replace(self._key, "[REDACTED]")
                record.args = ()
        return True


def install_scrub_filter(key: str) -> None:
    """Install a log filter that scrubs the key from all regulatory log records.

    Python propagation bypasses ancestor logger filters, so the filter is
    installed on both the root logger and the ``regulatory`` logger to cover
    records emitted from within this package.

    Args:
        key: The API key string to redact from all future log records.
    """
    if not key:
        return
    f = _ScrubFilter(key)
    logging.getLogger().addFilter(f)
    logging.getLogger("regulatory").addFilter(f)


def load_key_from_secrets_file() -> str | None:
    """Read ANTHROPIC_API_KEY from ~/.config/regulatory/secrets.env if present.

    Returns:
        The key string if found, else None.
    """
    if not _SECRETS_PATH.exists():
        return None
    try:
        content = _SECRETS_PATH.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line[len("ANTHROPIC_API_KEY="):].strip()
    except OSError:
        pass
    return None


def save_key_to_secrets_file(key: str) -> bool:
    """Write the key to ~/.config/regulatory/secrets.env with chmod 600.

    Args:
        key: The API key to persist.

    Returns:
        True if written successfully, False on error.
    """
    try:
        _SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SECRETS_PATH.write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")
        _SECRETS_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return True
    except OSError as exc:
        log.warning("secrets_file_write_failed", error=str(exc))
        return False


def _validate_key(key: str, fallback_model: str) -> bool:
    """Make a minimal test call to confirm the key is valid.

    Args:
        key: The API key to test.
        fallback_model: Model to use for the test call.

    Returns:
        True if the call returns 200, False on auth error.

    Raises:
        ApiKeyError: On persistent network failures after retries.
    """
    import anthropic

    for attempt, delay in enumerate(_VALIDATION_RETRY_DELAYS):
        try:
            client = anthropic.Anthropic(api_key=key)
            client.messages.create(
                model=fallback_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except anthropic.AuthenticationError:
            return False
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            if attempt == len(_VALIDATION_RETRY_DELAYS) - 1:
                raise ApiKeyError(f"Network error validating API key: {exc}") from exc
            log.warning("api_key_validation_network_retry", attempt=attempt + 1, error=str(exc))
            time.sleep(delay)
    return False  # unreachable but satisfies type checker


def acquire_api_key(fallback_model: str) -> str:
    """Ensure a valid ANTHROPIC_API_KEY is available and return it.

    Checks os.environ first, then ~/.config/regulatory/secrets.env.
    If absent and stdin is a TTY, prompts interactively (up to 3 attempts).
    If absent and stdin is not a TTY, exits with code 2.

    Args:
        fallback_model: Model ID used for key validation test call.

    Returns:
        Validated API key string.

    Raises:
        SystemExit: Code 2 on non-interactive missing key or repeated auth failures.
        ApiKeyError: On persistent network failures during validation.
    """
    # Check environment first, then secrets file.
    key = os.environ.get("ANTHROPIC_API_KEY") or load_key_from_secrets_file()

    if key:
        if not _validate_key(key, fallback_model):
            # Key present but invalid — fall through to prompt if interactive.
            log.warning("api_key_present_but_invalid")
            if not sys.stdin.isatty():
                print(
                    "ANTHROPIC_API_KEY is set but authentication failed.\n"
                    "Check that the key is correct.",
                    file=sys.stderr,
                )
                sys.exit(2)
            key = None  # trigger interactive prompt below
        else:
            os.environ["ANTHROPIC_API_KEY"] = key
            install_scrub_filter(key)
            return key

    if not sys.stdin.isatty():
        print(
            "ANTHROPIC_API_KEY is not set and stdin is not a TTY.\n"
            "Set the environment variable (e.g. `export ANTHROPIC_API_KEY=sk-ant-...`)\n"
            "or run this command in an interactive terminal.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Interactive prompt.
    print(
        "Anthropic API key required for this operation.\n"
        "The key is not set in your environment (ANTHROPIC_API_KEY).",
        file=sys.stderr,
    )

    for attempt in range(_MAX_RETRIES):
        entered = getpass.getpass("Enter your Anthropic API key (input hidden): ")
        if not entered.strip():
            print("No key entered. Try again.", file=sys.stderr)
            continue
        if _validate_key(entered.strip(), fallback_model):
            key = entered.strip()
            break
        remaining = _MAX_RETRIES - attempt - 1
        if remaining > 0:
            print(f"Authentication failed. {remaining} attempt(s) remaining.", file=sys.stderr)
        else:
            print("Authentication failed. Exiting.", file=sys.stderr)
            sys.exit(2)

    if key is None:
        sys.exit(2)

    os.environ["ANTHROPIC_API_KEY"] = key
    install_scrub_filter(key)

    # Offer optional persistence.
    try:
        save_choice = input(
            "Save key to ~/.config/regulatory/secrets.env for future sessions? (y/N): "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        save_choice = "n"

    if save_choice == "y":
        if save_key_to_secrets_file(key):
            print(f"Key saved to {_SECRETS_PATH} (mode 600).", file=sys.stderr)
        else:
            print("Could not save key to disk; continuing without persistence.", file=sys.stderr)

    return key
