"""Client for the local model served by LM Studio (OpenAI-compatible API)."""
from __future__ import annotations

import json
import re
import urllib.request

from .config import Settings

SYSTEM_PROMPT = """You are a short-form video editor. You read timestamped livestream
transcript excerpts and pick moments that would work as standalone vertical clips for
TikTok, Instagram Reels and YouTube Shorts.

Good moments: a clear setup and payoff, a funny reaction, a hot take, a clutch play,
a heated or emotional exchange, a story that makes sense without context.
Bad moments: small talk, dead air, moments that only make sense with earlier context,
anything that identifies or targets a private person.

Reply with JSON only, no prose, in this shape:
{"clips": [{"start": 12.3, "end": 41.0, "score": 0-100, "title": "...",
            "hook": "first line a viewer hears", "caption": "...",
            "hashtags": ["#example"], "reason": "why it works"}]}
Use the timestamps given in the transcript. Return an empty list if nothing is good."""


def chat(messages: list[dict], s: Settings, temperature: float = 0.2) -> str:
    """POST to LM Studio's OpenAI-compatible endpoint. Standard library only."""
    body = json.dumps({
        "model": s.lmstudio_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 1500,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{s.lmstudio_base_url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def parse_json_reply(text: str) -> dict:
    """Pull the first JSON object out of a model reply, tolerating code fences."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    body = fenced.group(1) if fenced else text
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model reply")
    return json.loads(body[start : end + 1])


def ask_for_clips(window_text: str, s: Settings) -> list[dict]:
    """Send one transcript window and return the raw candidate clips."""
    reply = chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": window_text},
        ],
        s,
    )
    return parse_json_reply(reply).get("clips", [])
