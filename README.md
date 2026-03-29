# auto-ai

`auto-ai` wraps a plain terminal program such as OpenAI Codex and only sends
automatic numeric replies when the visible menu is an exact, recognized match.

Default behavior:

- exact options `1` and `2` -> send `1`
- exact options `1`, `2`, and `3` -> send `2`
- anything else -> do nothing

## Why this exists

Codex sometimes presents short numbered prompts in the terminal. This tool
monitors terminal output conservatively and only responds when the prompt looks
like a real interactive menu, not ordinary numbered prose.

## Features

- PTY-aware wrapper built on `pexpect`
- strict numbered-menu detection
- idle timeout before replying
- local logging
- runtime enable/disable state file
- in-process emergency stop while `auto-ai` is running
- signal forwarding to the wrapped process

## Install

On Arch or CachyOS:

```bash
sudo pacman -S --needed python python-pexpect
```

User install from the repo:

```bash
cd ~/Git/auto-ai
python -m pip install --user .
```

## Usage

Wrap Codex:

```bash
auto-ai
```

Explicit command form:

```bash
auto-ai -- codex
auto-ai -- codex --model gpt-5.4
```

State controls:

```bash
auto-ai --status
auto-ai --enable
auto-ai --disable
auto-ai --toggle
```

Emergency stop:

- In Kitty, while `auto-ai` is running, press `Ctrl+Shift+A`
- This disables auto replies immediately
- No Kitty keymap or global hotkey is required

Log file:

```text
~/.local/state/auto-ai/auto-ai.log
```

## Detection rules

`auto-ai` only replies when all of these are true:

- the menu options are contiguous and numbered exactly `1,2` or `1,2,3`
- the numbering style is consistent, such as `1.` / `2.` or `1)` / `2)`
- there is an adjacent cue line such as `Choose`, `Select`, `Enter choice`, or
  `Selection:`
- the recognized prompt is still at the bottom of the live terminal output

That combination keeps false positives low for daily terminal use.

## Emergency stop behavior

`auto-ai` enables Kitty's keyboard protocol itself while it is running, so
`Ctrl+Shift+A` is available as an app-local emergency stop in Kitty without any
terminal config bind. Outside `auto-ai`, the key does nothing special.
