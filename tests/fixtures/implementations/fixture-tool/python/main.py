# Fixture implementation (mechanism test only). Reads {"n": N} on stdin, prints a JSON result.
import json
import sys

data = json.load(sys.stdin)
n = data["n"]
print(json.dumps({"ok": True, "lang": "python", "n": n, "double": n * 2}))
