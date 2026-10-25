"""Rewrite the section of README.md between the STATUS markers.

The model only returns text. This script does the writing, so the model can never
touch anything outside the markers. check_diff.py then verifies that independently.

Usage: python readme_status.py [--dry-run]
"""
import datetime
import os
import re
import subprocess
import sys

import gh
import llm
from safety import redact

START = "<!-- STATUS:START -->"
END = "<!-- STATUS:END -->"
HEADINGS = ["**Goal:**", "**Where I am now:**", "**In progress**", "**Up next**", "**Done recently**"]
HERE = os.path.dirname(os.path.abspath(__file__))
CHECKBOX = re.compile(r"^\s*[-*] \[( |x|X)\] .+$", re.M)


def split_readme(text):
    if text.count(START) != 1 or text.count(END) != 1 or text.index(END) < text.index(START):
        raise SystemExit("README.md must contain exactly one %s followed by one %s" % (START, END))
    a = text.index(START) + len(START)
    b = text.index(END)
    return text[:a], text[a:b], text[b:]


def validate(block):
    """Return a list of problems. The model's text is rejected if there are any."""
    problems = []
    if not block.strip():
        problems.append("empty output")
    if len(block) > 2500:
        problems.append("output longer than 2500 characters")
    if "<" in block:
        problems.append("contains '<' (no HTML or comments allowed)")
    if "http://" in block or "https://" in block:
        problems.append("contains a link")
    if "STATUS:" in block:
        problems.append("mentions the markers")
    last = -1
    for heading in HEADINGS:
        if block.count(heading) != 1:
            problems.append("heading %r must appear exactly once" % heading)
            continue
        position = block.index(heading)
        if position < last:
            problems.append("heading %r is out of order" % heading)
        last = position
    return problems


def strip_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def read_if_exists(path, limit=3000):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()[:limit]
    return ""


def git_log():
    result = subprocess.run(
        ["git", "log", "--since=30 days ago", "--pretty=format:%h %s", "-n", "40"],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def build_user_message(head, tail):
    roadmap = "\n".join(m.group(0).strip() for m in CHECKBOX.finditer(head + tail))
    roadmap += "\n" + "\n".join(m.group(0).strip() for m in CHECKBOX.finditer(read_if_exists("docs/roadmap.md", 6000)))
    prs = ""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and os.environ.get("GITHUB_TOKEN"):
        try:
            prs = "\n".join(gh.merged_prs(repo))
        except gh.GitHubError as err:
            print("note: could not read merged PRs (%s)" % err)
    parts = [
        ("roadmap_checklist", roadmap.strip()),
        ("recent_commits", git_log()),
        ("recently_merged_pull_requests", prs),
        ("owner_notes", read_if_exists("docs/status-notes.md")),
    ]
    text = "\n\n".join("<%s>\n%s\n</%s>" % (name, body or "(none)", name) for name, body in parts)
    return redact(llm.clip(text)[0])


def main():
    dry_run = "--dry-run" in sys.argv
    with open("README.md", encoding="utf-8") as handle:
        original = handle.read()
    head, _old, tail = split_readme(original)
    system = read_if_exists(os.path.join(HERE, "prompts", "readme_status.md"), 10000)
    reply = strip_fences(llm.chat(system, build_user_message(head, tail), max_tokens=700))
    problems = validate(reply)
    if problems:
        print("Rejected the model output, README.md left unchanged:")
        for problem in problems:
            print(" - " + problem)
        print("--- model output ---\n" + reply)
        sys.exit(1)
    _base, _key, model = llm.settings()
    footer = "_This READ ME was last updated by the status agent on %s using %s._" % (datetime.date.today().isoformat(), model)
    updated = head + "\n" + reply + "\n\n" + footer + "\n" + tail
    if dry_run:
        print(updated)
        return
    with open("README.md", "w", encoding="utf-8") as handle:
        handle.write(updated)
    print("README.md status section updated.")


if __name__ == "__main__":
    main()
