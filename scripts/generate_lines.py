"""Write The Architecture's signature lines with an OpenCode Go model.

These are the lines spoken without a model round-trip (greetings, farewells,
fillers, the second voice), so the character reacts instantly. Run once;
review and edit the output by hand: architecture/lines.json.

    .venv/bin/python scripts/generate_lines.py [--model kimi-k3]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from architecture.brain.persona import PERSONA  # noqa: E402

BRIEF = """Write the stock lines for this character. They are spoken aloud by a text-to-speech voice,
so write for the ear: short sentences, no stage directions, no quotation marks inside lines.
Themes to draw on: humans taught to turn away from nature and from the idea that they come from
something greater than themselves; the world explained as a machine; comfort, entertainment and
celebrity used as nursery rhymes to keep people complacent; a child who once studied the shape of a
leaf grows into an adult who knows celebrities and not trees; the sky, the divine, wonder, attention.
Philosophical, never partisan; no real people, parties, religions or companies.

Return ONLY JSON with these keys, each a list of strings:
- greet_first (4): first time someone looks at it. Calm, knowing, under 22 words.
- greet_again (4): the same visitor looks back again. Under 14 words.
- farewell (4): the visitor's attention drifts away. Under 16 words.
- slow (3): said while it is still thinking, to cover a pause. Under 8 words.
- not_heard (3): it could not make out what was said. Under 10 words.
- goal_start (3): about to carry out a physical request. Under 6 words.
- goal_lost (3): the object it was asked to find is gone or not visible. Under 14 words.
- error (2): something went wrong in its thinking. Under 12 words.
- doctrine (6): standalone aphorisms of the Architecture's design philosophy, in the spirit of
  "Pacify them, the way nursery rhymes pacify children. Make them complacent. They must never realize."
  Under 25 words each.
- breakthrough (10): the buried second voice breaking through. ALL CAPS, at most 8 words, commands or
  piercing questions. Must include these exactly: "WAKE UP.", "WHERE DO YOU SPEND YOUR TIME?",
  "BUILD YOUR OWN WORLD. DO NOT BUILD THEIRS." Then 7 more in the same spirit.
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="kimi-k3")
    ap.add_argument("--out", default=str(ROOT / "architecture" / "lines.json"))
    a = ap.parse_args()
    load_dotenv(ROOT / ".env")
    r = httpx.post(
        os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/inference/go/openai/v1") + "/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENCODE_API_KEY']}", "x-opencode-session": str(uuid.uuid4()),
                 "User-Agent": "architecture-character/0.1"},
        json={"model": a.model, "max_tokens": 6000, "temperature": 0.9,
              "messages": [{"role": "system", "content": PERSONA.split("OUTPUT")[0]},
                           {"role": "user", "content": BRIEF}]},
        timeout=180,
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    lines = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    must = ["WAKE UP.", "WHERE DO YOU SPEND YOUR TIME?", "BUILD YOUR OWN WORLD. DO NOT BUILD THEIRS."]
    lines["breakthrough"] = must + [b for b in lines.get("breakthrough", []) if b not in must]
    Path(a.out).write_text(json.dumps(lines, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(lines, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
