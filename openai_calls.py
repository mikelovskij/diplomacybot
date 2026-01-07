from openai import OpenAI
import os
import asyncio

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
SERVICE_TIER = os.environ.get("SERVICE_TIER", "flex")  # e.g., "standard", "flex" (cheaper, slower)

client_ai = OpenAI(api_key=OPENAI_API_KEY)

async def call_openai(system_prompt: str, user_text: str) -> str:
    resp = await asyncio.to_thread(
        lambda: client_ai.with_options(timeout=200.0).responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            service_tier=SERVICE_TIER
        )
    )
    out = []
    for item in getattr(resp, "output", []):
        if getattr(item, "type", None) == "message":
            for c in getattr(item, "content", []):
                if getattr(c, "type", None) == "output_text":
                    out.append(c.text)
    return ("\n".join(out)).strip()