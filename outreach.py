import json
import asyncio
from config import AI_COUNTRY
from prompts import build_outreach_prompt  



def parse_outreach(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        to = item.get("to")
        msg = item.get("message")
        if isinstance(to, str) and isinstance(msg, str):
            out.append({"to": to.strip(), "message": msg.strip()})
    return out

async def send_outreach(bot, db, call_openai, system_prompt: str, phase: str, state_text: str, ai_memory: str, max_messages: int = 3) -> int:
    claims = db.get_claims()  # [(country, display_name, discord_user_id), ...]
    country_to_uid = {country: int(uid) for country, _, uid in claims if country}

    # don’t DM self
    country_to_uid.pop(AI_COUNTRY, None)

    allowed = list(country_to_uid.keys())
    if not allowed:
        return 0

    summaries = db.get_all_summaries_for_claimed_players()
    prompt = build_outreach_prompt(phase, state_text, summaries, ai_memory, allowed, max_messages)

    raw = await call_openai(system_prompt=system_prompt, user_text=prompt)
    proposals = parse_outreach(raw)

    sent = 0
    used = set()

    for p in proposals:
        if sent >= max_messages:
            break
        to = p["to"]
        if to not in country_to_uid or to in used:
            continue

        msg = p["message"]
        if not msg:
            continue
        if len(msg) > 2000: # Discord message limit
            msg = msg[:2000 - 1] + "…"

        try:
            user = await bot.fetch_user(country_to_uid[to])
            await user.send(msg)
            used.add(to)
            sent += 1
            # Save the message in the database
            thread = db.load_thread(country_to_uid[to])
            msgs = thread["messages"]
            summary = thread["summary"]
            msgs.append({"role": "assistant", "content": msg})
            db.save_thread(country_to_uid[to], msgs, summary, summary_last_updated=None)
            await asyncio.sleep(0.6)
        except Exception:
            continue

    return sent
