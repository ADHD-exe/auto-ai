from __future__ import annotations

import argparse
import logging
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pexpect


RULES: dict[tuple[int, ...], str] = {
    (1, 2): "1",
    (1, 2, 3): "2",
}

PROMPT_CUE_REGEX = re.compile(
    r"(?ix)"
    r"(?:"
    r"\b(?:choose|select|pick|enter|type|reply|respond|answer)\b.*\b(?:option|choice|number)?\b"
    r"|"
    r"\b(?:choice|selection|option)\b\s*[:?]"
    r")"
)

MENU_IDLE_SECONDS = 0.80
MENU_MATCH_TIMEOUT_SECONDS = 10.0
SCAN_WINDOW_LINES = 24
READ_POLL_SECONDS = 0.20
HANDLED_FINGERPRINT_LIMIT = 64
STATE_DIR = Path.home() / ".local" / "state" / "auto-ai"
STATE_FILE = STATE_DIR / "enabled"
LOG_FILE = STATE_DIR / "auto-ai.log"
DEFAULT_ENABLED = True
LOG_RAW_OUTPUT = True

ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:"
    r"[@-Z\\-_]"
    r"|"
    r"\[[0-?]*[ -/]*[@-~]"
    r"|"
    r"\][^\x07]*(?:\x07|\x1b\\)"
    r")"
)

OPTION_PATTERNS = (
    re.compile(r"^\s*(?P<num>[1-9])(?P<style>[.)])\s+(?P<label>\S.*)\s*$"),
    re.compile(r"^\s*\[(?P<num>[1-9])\]\s+(?P<label>\S.*)\s*$"),
)


@dataclass
class MenuMatch:
    numbers: tuple[int, ...]
    response: str
    fingerprint: str
    block_text: str


@dataclass
class Candidate:
    match: MenuMatch
    first_seen: float
    last_seen: float


def setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("auto-ai")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)

    return logger


def strip_control_sequences(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return ANSI_ESCAPE_RE.sub("", text)


def parse_option_line(line: str) -> Optional[tuple[int, str]]:
    for pattern in OPTION_PATTERNS:
        match = pattern.match(line)
        if match:
            return int(match.group("num")), match.groupdict().get("style", "[]")
    return None


def adjacent_prompt_cue_indices(lines: list[str], start: int, end: int) -> list[int]:
    checks: list[int] = []

    if start - 1 >= 0 and PROMPT_CUE_REGEX.search(lines[start - 1]):
        checks.append(start - 1)
    if end + 1 < len(lines) and PROMPT_CUE_REGEX.search(lines[end + 1]):
        checks.append(end + 1)

    if start - 2 >= 0 and not lines[start - 1].strip():
        if PROMPT_CUE_REGEX.search(lines[start - 2]):
            checks.append(start - 2)
    if end + 2 < len(lines) and not lines[end + 1].strip():
        if PROMPT_CUE_REGEX.search(lines[end + 2]):
            checks.append(end + 2)

    return checks


def only_blank_after(lines: list[str], idx: int) -> bool:
    return all(not line.strip() for line in lines[idx + 1 :])


def find_menu_match(buffer_text: str) -> Optional[MenuMatch]:
    lines = buffer_text.splitlines()
    if not lines:
        return None

    window = lines[-SCAN_WINDOW_LINES:]
    best_match: Optional[MenuMatch] = None
    idx = 0

    while idx < len(window):
        parsed = parse_option_line(window[idx])
        if not parsed:
            idx += 1
            continue

        start = idx
        numbers: list[int] = []
        style = parsed[1]

        while idx < len(window):
            current = parse_option_line(window[idx])
            if not current or current[1] != style:
                break
            numbers.append(current[0])
            idx += 1

        end = idx - 1
        numbers_tuple = tuple(numbers)

        if numbers_tuple not in RULES:
            continue

        expected = tuple(range(1, len(numbers_tuple) + 1))
        if numbers_tuple != expected:
            continue

        if start > 0 and parse_option_line(window[start - 1]):
            continue
        if end + 1 < len(window) and parse_option_line(window[end + 1]):
            continue

        cue_indices = adjacent_prompt_cue_indices(window, start, end)
        if not cue_indices:
            continue

        prompt_end: Optional[int] = None
        prompt_start = start
        for cue_idx in cue_indices:
            candidate_end = max(end, cue_idx)
            if only_blank_after(window, candidate_end):
                prompt_end = candidate_end
                prompt_start = min(start, cue_idx)
                break

        if prompt_end is None:
            continue

        block_lines = window[start : end + 1]
        fingerprint_source = "\n".join(window[prompt_start : prompt_end + 1])
        best_match = MenuMatch(
            numbers=numbers_tuple,
            response=RULES[numbers_tuple],
            fingerprint=fingerprint_source,
            block_text="\n".join(block_lines),
        )

    return best_match


def read_enabled_state() -> bool:
    try:
        raw = STATE_FILE.read_text(encoding="utf-8").strip().lower()
    except FileNotFoundError:
        return DEFAULT_ENABLED

    return raw not in {"0", "off", "false", "disabled"}


def write_enabled_state(enabled: bool) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text("1\n" if enabled else "0\n", encoding="utf-8")


def print_state(enabled: bool) -> None:
    print(f"auto-ai: {'enabled' if enabled else 'disabled'}")


def build_command(args: list[str]) -> list[str]:
    command = args[:]
    if command and command[0] == "--":
        command = command[1:]
    return command or ["codex"]


def handle_state_command(ns: argparse.Namespace) -> int:
    if ns.enable:
        write_enabled_state(True)
        print_state(True)
        return 0
    if ns.disable:
        write_enabled_state(False)
        print_state(False)
        return 0
    if ns.toggle:
        enabled = not read_enabled_state()
        write_enabled_state(enabled)
        print_state(enabled)
        return 0
    if ns.status:
        print_state(read_enabled_state())
        return 0
    return -1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Codex behind a strict numbered-menu auto-responder."
    )
    parser.add_argument(
        "--log-file",
        default=str(LOG_FILE),
        help=f"Path to the log file (default: {LOG_FILE})",
    )
    parser.add_argument("--enable", action="store_true", help="Enable auto replies.")
    parser.add_argument("--disable", action="store_true", help="Disable auto replies.")
    parser.add_argument("--toggle", action="store_true", help="Toggle auto replies.")
    parser.add_argument("--status", action="store_true", help="Print current state.")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run. Use -- before the command. Default: codex",
    )
    ns = parser.parse_args()

    state_rc = handle_state_command(ns)
    if state_rc >= 0:
        return state_rc

    command = build_command(ns.command)
    logger = setup_logging(Path(ns.log_file).expanduser())
    logger.info("starting wrapper command=%r enabled=%s", command, read_enabled_state())

    child = pexpect.spawn(
        command[0],
        command[1:],
        encoding="utf-8",
        codec_errors="replace",
        timeout=None,
    )

    def forward_signal(signum: int, _frame: object) -> None:
        logger.info("forwarding signal=%s to child pid=%s", signum, child.pid)
        try:
            child.kill(signum)
        except Exception:
            pass

    signal.signal(signal.SIGINT, forward_signal)
    signal.signal(signal.SIGTERM, forward_signal)

    parse_buffer = ""
    last_output_time = time.monotonic()
    candidate: Optional[Candidate] = None
    handled = deque(maxlen=HANDLED_FINGERPRINT_LIMIT)
    last_enabled = read_enabled_state()

    while True:
        now = time.monotonic()

        try:
            chunk = child.read_nonblocking(size=4096, timeout=READ_POLL_SECONDS)
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
                last_output_time = now

                if LOG_RAW_OUTPUT:
                    logger.info("raw %r", chunk)

                parse_buffer += strip_control_sequences(chunk)
                if len(parse_buffer) > 16000:
                    parse_buffer = parse_buffer[-16000:]

                match = find_menu_match(parse_buffer)

                if match and match.fingerprint not in handled:
                    if candidate and candidate.match.fingerprint == match.fingerprint:
                        candidate.last_seen = now
                    else:
                        candidate = Candidate(match=match, first_seen=now, last_seen=now)
                        logger.info(
                            "menu candidate numbers=%s response=%s block=%r",
                            match.numbers,
                            match.response,
                            match.block_text,
                        )
                elif candidate:
                    logger.info(
                        "clearing stale candidate numbers=%s because prompt changed",
                        candidate.match.numbers,
                    )
                    candidate = None

        except pexpect.TIMEOUT:
            pass
        except pexpect.EOF:
            break

        enabled = read_enabled_state()
        if enabled != last_enabled:
            logger.info("state changed enabled=%s", enabled)
            last_enabled = enabled

        now = time.monotonic()
        if candidate:
            if now - candidate.first_seen > MENU_MATCH_TIMEOUT_SECONDS:
                logger.info(
                    "menu candidate expired numbers=%s block=%r",
                    candidate.match.numbers,
                    candidate.match.block_text,
                )
                candidate = None
                continue

            if enabled and now - last_output_time >= MENU_IDLE_SECONDS:
                child.sendline(candidate.match.response)
                handled.append(candidate.match.fingerprint)
                logger.info(
                    "auto-sent response=%s for menu=%s",
                    candidate.match.response,
                    candidate.match.numbers,
                )
                candidate = None

    child.close()
    logger.info("wrapper exiting status=%s", child.exitstatus)
    return child.exitstatus if child.exitstatus is not None else 0
