"""Language and vision reasoning through OpenCode Go (OpenAI-compatible API).

What leaves the laptop, per call: the persona prompt, the recent transcript
text, the Ledger text, and (when relevant) ONE camera frame, downscaled to
640 px JPEG. Never audio, never a video stream. The model returns a JSON
decision; the brain validates it and the body executes it within limits.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid

import httpx

log = logging.getLogger("mind")


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class Mind:
    def __init__(self, model: str | None = None):
        self.base = os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/inference/go/openai/v1")
        self.key = os.environ.get("OPENCODE_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL") or "glm-5.3-flash"
        self.client = httpx.AsyncClient(base_url=self.base, timeout=30, headers={
            "Authorization": f"Bearer {self.key}",
            "x-opencode-session": str(uuid.uuid4()),
            "User-Agent": "architecture-character/0.1",
        })
        self.no_thinking = True
        self.history: list[dict] = []

    @property
    def ready(self) -> bool:
        return bool(self.key)

    async def _chat(self, messages: list[dict], max_tokens: int = 500, temperature: float = 0.8) -> tuple[dict, float]:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                "response_format": {"type": "json_object"}}
        if self.no_thinking:
            body["thinking"] = {"type": "disabled"}  # latency: no hidden reasoning tokens
        t = time.perf_counter()
        for attempt in range(2):
            r = await self.client.post("/chat/completions", json=body)
            if r.status_code == 400 and attempt == 0:
                body.pop("thinking", None)
                body.pop("response_format", None)
                continue
            r.raise_for_status()
            break
        text = r.json()["choices"][0]["message"].get("content") or ""
        ms = (time.perf_counter() - t) * 1000
        try:
            return _parse_json(text), ms
        except Exception:
            log.warning("unparseable reply: %r", text[:300])
            return {"say": text.strip()[:300]}, ms

    async def respond(self, system: str, ledger: str, situation: str, user: str, image_b64: str | None) -> tuple[dict, float]:
        content: list = [{"type": "text", "text": f"{ledger}\n\nSITUATION: {situation}\n\nVISITOR SAYS: \"{user}\""}]
        if image_b64:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})
        msgs = [{"role": "system", "content": system}, *self.history[-12:], {"role": "user", "content": content}]
        reply, ms = await self._chat(msgs)
        self.history += [{"role": "user", "content": f"(visitor) {user}"},
                         {"role": "assistant", "content": json.dumps({"say": reply.get("say", "")})}]
        return reply, ms

    async def locate(self, target: str, image_b64: str) -> tuple[dict, float]:
        """Find one object in a fresh frame. Used to re-observe after every aim."""
        msgs = [
            {"role": "system", "content": "You locate objects in images. Reply ONLY with JSON: "
             "{\"found\": bool, \"box\": [x0,y0,x1,y1], \"label\": str} with coordinates normalized 0-1 "
             "(x left to right, y top to bottom). If the object is not visible, found=false and box=null."},
            {"role": "user", "content": [{"type": "text", "text": f"Object: {target}"},
                                         {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}]},
        ]
        return await self._chat(msgs, max_tokens=120, temperature=0.0)

    async def observe(self, image_b64: str) -> tuple[dict, float]:
        """Silent glance: list notable objects for the Ledger (people excluded)."""
        msgs = [
            {"role": "system", "content": "List the distinct notable physical objects in the image (not people, "
             "not furniture or walls). Reply ONLY with JSON: {\"objects\": [{\"name\": str, \"details\": str "
             "(colour, material, markings, any readable text), \"where\": str (position in frame)}]}. At most 5."},
            {"role": "user", "content": [{"type": "text", "text": "What objects are here?"},
                                         {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}]},
        ]
        return await self._chat(msgs, max_tokens=400, temperature=0.2)

    def reset(self) -> None:
        self.history.clear()
