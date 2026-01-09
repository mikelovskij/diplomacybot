from openai import OpenAI
import os
import asyncio
from config import OPENAI_API_KEY, BASE_OPENAI_MODEL, SERVICE_TIER

client_ai = OpenAI(api_key=OPENAI_API_KEY)

async def call_openai(system_prompt: str, user_text: str, model=BASE_OPENAI_MODEL) -> str:
    resp = await asyncio.to_thread(
        lambda: client_ai.with_options(timeout=200.0).responses.create(
            model=model,
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