#!/usr/bin/env bash
# Fixture implementation (mechanism test only). Reads {"n": N} on stdin, prints a JSON result.
input=$(cat)
n=$(printf '%s' "$input" | sed -n 's/.*"n": *\([0-9][0-9]*\).*/\1/p')
printf '{"ok": true, "lang": "bash", "n": %s, "double": %s}\n' "$n" "$((n * 2))"
