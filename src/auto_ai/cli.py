#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import re
import signal
import struct
import sys
import termios
import threading
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
STATE_POLL_SECONDS = 0.05
HANDLED_FINGERPRINT_LIMIT = 64
STATE_DIR = Path.home() / ".local" / "state" / "auto-ai"
STATE_FILE = STATE_DIR / "enabled"
LOG_FILE = STATE_DIR / "auto-ai.log"
DEFAULT_ENABLED = True
LOG_RAW_OUTPUT = True

KITTY_KEYBOARD_ENABLE = b"\x1b[>1u"
KITTY_KEYBOARD_DISABLE = b"\x1b[<u"
EMERGENCY_STOP_BYTES = b"\x1b[97;6u"

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


def longest_prefix_suffix(data: bytes, prefix: bytes) -> int:
    max_len = min(len(data), len(prefix) - 1)
    for size in range(max_len, 0, -1):
        if data.endswith(prefix[:size]):
            return size
    return 0


class AutoAIController:
    def __init__(self, child: pexpect.spawn, logger: logging.Logger) -> None:
        self.child = child
        self.logger = logger
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.parse_buffer = ""
        self.last_output_time = time.monotonic()
        self.candidate: Optional[Candidate] = None
        self.handled = deque(maxlen=HANDLED_FINGERPRINT_LIMIT)
        self.last_enabled = read_enabled_state()
        self.pending_input = b""
        self.kitty_keyboard_active = False

    def emit_notice(self, message: str) -> None:
        try:
            os.write(sys.stderr.fileno(), f"\r\n{message}\r\n".encode("utf-8"))
        except OSError:
            pass

    def enable_keyboard_protocol(self) -> None:
        is_kitty = os.environ.get("TERM") == "xterm-kitty" or bool(
            os.environ.get("KITTY_WINDOW_ID")
        )
        if not sys.stdout.isatty() or not is_kitty:
            return
        try:
            os.write(sys.stdout.fileno(), KITTY_KEYBOARD_ENABLE)
            self.kitty_keyboard_active = True
            self.logger.info("enabled kitty keyboard protocol")
        except OSError:
            self.logger.info("failed to enable kitty keyboard protocol")

    def disable_keyboard_protocol(self) -> None:
        if not sys.stdout.isatty() or not self.kitty_keyboard_active:
            return
        try:
            os.write(sys.stdout.fileno(), KITTY_KEYBOARD_DISABLE)
            self.logger.info("restored previous keyboard protocol")
        except OSError:
            self.logger.info("failed to restore previous keyboard protocol")

    def handle_child_output(self, data: bytes) -> bytes:
        now = time.monotonic()
        text = data.decode("utf-8", "replace")

        with self.lock:
            self.last_output_time = now

            if LOG_RAW_OUTPUT:
                self.logger.info("raw %r", text)

            self.parse_buffer += strip_control_sequences(text)
            if len(self.parse_buffer) > 16000:
                self.parse_buffer = self.parse_buffer[-16000:]

            match = find_menu_match(self.parse_buffer)
            if match and match.fingerprint not in self.handled:
                if self.candidate and self.candidate.match.fingerprint == match.fingerprint:
                    self.candidate.last_seen = now
                else:
                    self.candidate = Candidate(match=match, first_seen=now, last_seen=now)
                    self.logger.info(
                        "menu candidate numbers=%s response=%s block=%r",
                        match.numbers,
                        match.response,
                        match.block_text,
                    )
            elif self.candidate:
                self.logger.info(
                    "clearing stale candidate numbers=%s because prompt changed",
                    self.candidate.match.numbers,
                )
                self.candidate = None

        return data

    def _trigger_emergency_stop(self) -> None:
        write_enabled_state(False)
        with self.lock:
            self.candidate = None
        self.logger.warning("emergency stop activated via ctrl+shift+a")
        self.emit_notice("[auto-ai emergency stop: disabled]")

    def handle_user_input(self, data: bytes) -> bytes:
        combined = self.pending_input + data
        self.pending_input = b""
        output = bytearray()
        pos = 0

        while True:
            hit = combined.find(EMERGENCY_STOP_BYTES, pos)
            if hit == -1:
                tail = combined[pos:]
                keep = longest_prefix_suffix(tail, EMERGENCY_STOP_BYTES)
                if keep:
                    output.extend(tail[:-keep])
                    self.pending_input = tail[-keep:]
                else:
                    output.extend(tail)
                break

            output.extend(combined[pos:hit])
            self._trigger_emergency_stop()
            pos = hit + len(EMERGENCY_STOP_BYTES)

        return bytes(output)

    def monitor(self) -> None:
        while not self.stop_event.is_set():
            enabled = read_enabled_state()
            now = time.monotonic()

            with self.lock:
                if enabled != self.last_enabled:
                    self.logger.info("state changed enabled=%s", enabled)
                    self.last_enabled = enabled

                if self.candidate:
                    if now - self.candidate.first_seen > MENU_MATCH_TIMEOUT_SECONDS:
                        self.logger.info(
                            "menu candidate expired numbers=%s block=%r",
                            self.candidate.match.numbers,
                            self.candidate.match.block_text,
                        )
                        self.candidate = None
                    elif enabled and now - self.last_output_time >= MENU_IDLE_SECONDS:
                        self.child.sendline(self.candidate.match.response)
                        self.handled.append(self.candidate.match.fingerprint)
                        self.logger.info(
                            "auto-sent response=%s for menu=%s",
                            self.candidate.match.response,
                            self.candidate.match.numbers,
                        )
                        self.candidate = None

            time.sleep(STATE_POLL_SECONDS)

    def sigwinch_passthrough(self, _signum: int, _frame: object) -> None:
        packed = struct.pack("HHHH", 0, 0, 0, 0)
        rows, cols, _, _ = struct.unpack(
            "hhhh",
            fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, packed),
        )
        if not self.child.closed:
            self.child.setwinsize(rows, cols)


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

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("auto-ai requires an interactive TTY.", file=sys.stderr)
        return 2

    command = build_command(ns.command)
    logger = setup_logging(Path(ns.log_file).expanduser())
    logger.info("starting wrapper command=%r enabled=%s", command, read_enabled_state())

    child = pexpect.spawn(
        command[0],
        command[1:],
        encoding=None,
        timeout=None,
    )
    controller = AutoAIController(child, logger)

    def forward_signal(signum: int, _frame: object) -> None:
        logger.info("forwarding signal=%s to child pid=%s", signum, child.pid)
        try:
            child.kill(signum)
        except Exception:
            pass

    signal.signal(signal.SIGINT, forward_signal)
    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGWINCH, controller.sigwinch_passthrough)
    controller.sigwinch_passthrough(signal.SIGWINCH, None)

    monitor_thread = threading.Thread(target=controller.monitor, daemon=True)
    monitor_thread.start()

    try:
        controller.enable_keyboard_protocol()
        if controller.kitty_keyboard_active:
            controller.emit_notice("[auto-ai running: emergency stop is Ctrl+Shift+A in Kitty]")
        child.interact(
            escape_character=None,
            input_filter=controller.handle_user_input,
            output_filter=controller.handle_child_output,
        )
    finally:
        controller.stop_event.set()
        monitor_thread.join(timeout=1.0)
        controller.disable_keyboard_protocol()
        child.close()

    logger.info("wrapper exiting status=%s", child.exitstatus)
    return child.exitstatus if child.exitstatus is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
