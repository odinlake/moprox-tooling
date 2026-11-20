#!/usr/bin/env python3
"""Send feedback or a bug report to Anthropic through Claude Code's own /feedback form.

There is no API or headless route: `claude -p "/feedback ..."` answers "/feedback isn't available in
this environment". The form only exists in the interactive TUI, so this drives one in a detached
tmux session, started in an empty scratch directory so that the transcript the form attaches
("This session only") contains nothing but the feedback itself.

Every step waits for the text the form is expected to show and fails loudly, with the screen, if it
does not appear. A Claude Code release that reworks the form therefore breaks this visibly instead
of sending something half-filled. See README.md.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

CLAUDE = os.path.expanduser("~/.local/bin/claude")
CHUNK = 200          # characters per send-keys call; the TUI drops input pasted in one huge burst


class FormError(RuntimeError):
    pass


def tmux(*args, check=True):
    return subprocess.run(["tmux", *args], check=check, capture_output=True, text=True).stdout


class Form:
    def __init__(self, session):
        self.session = session

    def screen(self):
        return tmux("capture-pane", "-t", self.session, "-p")

    def keys(self, *keys):
        tmux("send-keys", "-t", self.session, *keys)
        time.sleep(0.4)

    def type(self, text):
        for i in range(0, len(text), CHUNK):
            tmux("send-keys", "-t", self.session, "-l", text[i:i + CHUNK])
            time.sleep(0.15)

    def wait(self, *needles, timeout=30):
        """Wait until one of `needles` is on screen; return the one that appeared."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            shown = self.screen()
            for n in needles:
                if n in shown:
                    return n
            time.sleep(0.5)
        raise FormError(f"expected one of {needles!r} within {timeout}s; the screen shows:\n"
                        + self.screen().rstrip())

    def select(self, label, max_moves=12):
        """Move the menu cursor (the line starting with ❯) onto `label`, then confirm."""
        for _ in range(max_moves):
            for line in self.screen().splitlines():
                if line.strip().startswith("❯") and label in line:
                    self.keys("Enter")
                    return
            self.keys("Down")
        raise FormError(f"could not put the cursor on {label!r}; the screen shows:\n"
                        + self.screen().rstrip())


def send(text, dry_run=False):
    workdir = tempfile.mkdtemp(prefix="anthropic-feedback-")
    session = f"anthropic-feedback-{os.getpid()}"
    tmux("new-session", "-d", "-s", session, "-x", "160", "-y", "50", "-c", workdir, CLAUDE)
    form = Form(session)
    try:
        if form.wait("trust this folder", "Claude Code v", timeout=60) == "trust this folder":
            form.select("Yes, I trust this folder")     # an empty directory this script just made
            form.wait("Claude Code v", timeout=60)
        time.sleep(1)

        form.type("/feedback")
        form.wait("Send feedback to Anthropic")
        form.keys("Enter")
        # Unsent drafts from other sessions are listed first. They are someone else's: skip them.
        if form.wait("Feedback drafts", "Describe the issue") == "Feedback drafts":
            form.select("Write new feedback")
            form.wait("Describe the issue")

        form.type(text)
        time.sleep(0.5)
        probe = " ".join(text.split()[:4])
        if probe not in " ".join(form.screen().replace("│", " ").split()):
            raise FormError("the feedback text did not land in the form; the screen shows:\n"
                            + form.screen().rstrip())
        form.keys("Enter")

        form.wait("How much session history")
        form.select("This session only")
        form.wait("This report will include")
        if dry_run:
            review = form.screen().rstrip()
            form.keys("Escape")
            return None, review
        form.select("Yes, send it")
        form.wait("Feedback sent", timeout=60)
        m = re.search(r"Reference ID:\s*(\S+)", form.screen())
        if not m:
            raise FormError("the form says the feedback was sent but shows no Reference ID:\n"
                            + form.screen().rstrip())
        return m.group(1), None
    finally:
        tmux("kill-session", "-t", session, check=False)   # benign if the TUI already exited
        shutil.rmtree(workdir, ignore_errors=True)         # scratch dir, nothing in it to lose


def main():
    ap = argparse.ArgumentParser(
        prog="anthropic-feedback",
        description="Send feedback or a bug report to Anthropic via Claude Code's /feedback form.")
    ap.add_argument("text", nargs="?", help="the feedback; read from stdin when omitted or '-'")
    ap.add_argument("--dry-run", action="store_true",
                    help="fill in the form, print the review screen, cancel without sending")
    args = ap.parse_args()

    raw = sys.stdin.read() if args.text in (None, "-") else args.text
    text = " ".join(raw.split())        # the form's field is one line: Enter submits it
    if len(text) < 20:
        ap.error("feedback text is missing or too short to be a useful report")
    if not shutil.which("tmux"):
        sys.exit("anthropic-feedback: tmux is not installed")
    if not os.path.exists(CLAUDE):
        sys.exit(f"anthropic-feedback: no Claude Code at {CLAUDE}")

    try:
        ref, review = send(text, dry_run=args.dry_run)
    except (FormError, subprocess.CalledProcessError) as e:
        print(f"<3>anthropic-feedback: NOT SENT. {e}", file=sys.stderr)
        sys.exit(1)
    if args.dry_run:
        print(review)
        print("\nDRY RUN: cancelled at the review screen, nothing was sent.")
    else:
        print(f"Feedback sent. Reference ID: {ref}")


if __name__ == "__main__":
    main()
