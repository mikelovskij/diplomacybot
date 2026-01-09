#!/usr/bin/env python3
import argparse
import re
import sys

"""
Helper script to reformat Backstabbr adjudication output into a cleaner format."""

POWERS = {"Austria", "England", "France", "Germany", "Italy", "Russia", "Turkey"}

def read_input(path: str | None) -> str:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return sys.stdin.read()

def format_backstabbr(text: str) -> str:
    lines = text.splitlines()

    out = []
    current_power = None
    buffer = []

    def flush():
        nonlocal buffer, current_power
        if current_power is None:
            buffer = []
            return
        # Remove empty lines inside the block, keep content lines only
        content = [ln for ln in buffer if ln.strip()]
        out.append(current_power)
        out.append("-" * 30)
        out.extend(content)
        out.append("")  # blank line between countries
        buffer = []

    for raw in lines:
        ln = raw.strip()
        if not ln:
            # just ignore blank lines entirely
            continue

        # Normalize weird spacing
        ln = re.sub(r"\s+", " ", ln)

        if ln in POWERS:
            # new country begins
            flush()
            current_power = ln
            continue

        # otherwise it’s a unit/order line
        buffer.append(ln)

    flush()

    # If nothing parsed, return original cleaned text
    if not out:
        return "\n".join([re.sub(r"\s+", " ", ln.strip()) for ln in lines if ln.strip()])

    # Remove trailing blank line
    while out and out[-1] == "":
        out.pop()

    return "\n".join(out)

def main() -> int:
    ap = argparse.ArgumentParser(description="Light formatter for Backstabbr adjudication output.")
    ap.add_argument("-i", "--input", help="Input file. If omitted, read from stdin.")
    args = ap.parse_args()

    raw = read_input(args.input)
    if not raw.strip():
        print("No input received.", file=sys.stderr)
        return 2

    print(format_backstabbr(raw))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

