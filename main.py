
import re
from datetime import datetime, timezone
from typing import List

# Import local modules
from database import database
import summaries as summ
import prompts as pr
from config import DISCORD_TOKEN, CONTROL_CHANNEL_ID, DB_PATH, AI_COUNTRY, SYSTEM_PROMPT, RAW_TURNS_TO_KEEP, OUTREACH_MAX_DEFAULT
from openai_calls import call_openai
import outreach

import discord
from discord.ext import commands

# -------------------- Orders-only filter (console safety) --------------------

# This is intentionally strict: better to reject than leak.
PROV = r"[A-Za-z]{3}"
UNIT = r"[AF]"
SPACE = r"\s+"

# Common order forms (Movement/Retreat/Adjustment basics)
ORDER_PATTERNS = [
    rf"^{UNIT}{SPACE}{PROV}{SPACE}H$",  # Hold
    rf"^{UNIT}{SPACE}{PROV}{SPACE}-{SPACE}{PROV}$",  # Move
    rf"^{UNIT}{SPACE}{PROV}{SPACE}S{SPACE}{UNIT}{SPACE}{PROV}{SPACE}H$",  # Support hold
    rf"^{UNIT}{SPACE}{PROV}{SPACE}S{SPACE}{UNIT}{SPACE}{PROV}{SPACE}-{SPACE}{PROV}$",  # Support move
    rf"^{UNIT}{SPACE}{PROV}{SPACE}C{SPACE}{UNIT}{SPACE}{PROV}{SPACE}-{SPACE}{PROV}$",  # Convoy
    rf"^{UNIT}{SPACE}{PROV}{SPACE}R{SPACE}{PROV}$",  # Retreat (Backstabbr often uses "R")
    rf"^{UNIT}{SPACE}{PROV}{SPACE}D$",  # Disband (retreat/adjust)
    rf"^A{SPACE}{PROV}{SPACE}B$",  # Build army
    rf"^F{SPACE}{PROV}{SPACE}B$",  # Build fleet
]

ORDER_RE = re.compile("|".join(f"(?:{p})" for p in ORDER_PATTERNS))

def extract_valid_orders(text: str) -> List[str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    valid = [ln for ln in lines if ORDER_RE.match(ln)]
    return valid

# Initialize database
db = database(DB_PATH)
orders_sent = False

# -------------------- Discord --------------------

intents = discord.Intents.default()
intents.message_content = True  # required for reading DMs
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    phase, _, updated = db.get_game_state()
    print(f"Logged in as {bot.user} (AI plays {AI_COUNTRY}). Phase='{phase}' state_updated='{updated}'")

def is_control_channel(message: discord.Message) -> bool:
    return message.guild is not None and message.channel.id == CONTROL_CHANNEL_ID

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # ---------- Console channel ----------
    if is_control_channel(message):
        content = message.content

        # phase: ...
        if content.lower().startswith("phase:"):
            ph = content.split(":", 1)[1].strip()
            db.set_game_state(phase=ph)
            await message.reply(f"✅ Phase set to: {ph}")
            return

        # state: (supports multi-line after the first line)
        if content.lower().startswith("state:"):
            st = content.split(":", 1)[1].lstrip()
            # If the user wrote "state:" and then pasted on new lines, include those too
            if "\n" in content:
                st = content.split("state:", 1)[1].lstrip()
            db.set_game_state(state_text=st)
            await message.reply("✅ Game state updated.")
            return

        # status
        if content.lower().strip() == "status":
            phase, _, updated = db.get_game_state()

            claims = db.get_claims()
            if claims:
                claims_lines = "\n".join(
                             f"- {country}: <@{uid}>"
                                for country, _, uid in claims
                            )
            else:
                claims_lines = "(none)"

            await message.reply(
                f"🧭 AI power: {AI_COUNTRY}\n"
                f"Phase: {phase or '(unset)'}\n"
                f"State updated: {updated or '(unset)'}\n"
                f"\n👥 Claims:\n{claims_lines}"
            )
            return
        
        if content.lower().startswith("outreach"):
            phase, state_text, _ = db.get_game_state()
            if not phase or not state_text.strip():
                await message.reply("I need both PHASE and STATE before outreach.")
                return            

            
            ai_memory = db.get_ai_memory()
            all_summaries = db.get_all_summaries_for_claimed_players()
            mem_prompt = pr.build_ai_memory_after_adjudication_prompt(
                phase=phase,
                state_text=state_text,
                ai_memory=ai_memory,
                summaries=all_summaries,
            )
            mem_after = await call_openai(system_prompt="You maintain a concise strategy journal.", user_text=mem_prompt)
            db.set_ai_memory(mem_after.strip())


            # allow `outreach 2` override
            parts = content.split()
            max_msgs = OUTREACH_MAX_DEFAULT
            if len(parts) == 2 and parts[1].isdigit():
                max_msgs = max(0, min(6, int(parts[1])))  # clamp

            n = await outreach.send_outreach(
                bot=bot,
                db=db,
                call_openai=call_openai,
                system_prompt=SYSTEM_PROMPT,
                phase=phase,
                state_text=state_text,
                ai_memory=mem_after.strip(),
                max_messages=max_msgs,
            )
            # Unlock press after outreach
            db.set_press_locked(False)
            await message.reply(f"📨 Outreach complete. DMs sent: {n}")
            
            return

        # orders
        if content.lower().strip() == "orders":
            phase, state_text, updated = db.get_game_state()
            if not phase or not state_text.strip():
                await message.reply("I need both PHASE and STATE to generate orders.")
                return

            # 1) Refresh summaries for threads that changed since last summary refresh
            rows = db.get_threads_needing_summary_refresh()
            if rows:
                payload = summ.build_summary_payload(rows, ai_country=AI_COUNTRY,
                                                                max_recent_msgs=RAW_TURNS_TO_KEEP)
                expected_keys = list(payload.keys())

                sum_prompt = summ.build_summary_prompt(AI_COUNTRY, payload)
                raw_sum = await call_openai(system_prompt="You are a helpful summarizer.", user_text=sum_prompt)
                summaries = summ.parse_summaries(raw_sum, expected_keys)

                if summaries is not None:
                    # apply updates + truncate messages to last 2 per your preference
                    # map country -> user_id
                    country_to_uid = {r["country"]: r["user_id"] for r in rows}
                    for country, new_summary in summaries.items():
                        uid = country_to_uid[country]
                        db.update_thread_summary_and_truncate(uid, new_summary, keep_last_n_msgs=2)
                else:
                    await message.reply("⚠️ Failed to parse updated summaries from AI. Aborting orders generation.")
                    return

            # 2) Now build orders prompt using ONLY the (now fresh) summaries
            # You need a helper to load all summaries for claimed players:
            all_summaries = db.get_all_summaries_for_claimed_players()
            ai_memory = db.get_ai_memory()
            raw = await call_openai(SYSTEM_PROMPT, pr.build_orders_prompt(phase, state_text, all_summaries, ai_memory))
            orders = extract_valid_orders(raw)

            if not orders:
                stricter = SYSTEM_PROMPT + "\n\nIMPORTANT: Output must be ONLY valid order lines. No other text."
                raw2 = await call_openai(stricter, pr.build_orders_prompt(phase, state_text, all_summaries))
                orders = extract_valid_orders(raw2)

            if not orders:
                await message.reply("⚠️ I couldn't produce valid orders from the current input. Check phase/state formatting.")
                return

            await message.reply("\n".join(orders))
            # Lock press until outreach
            db.set_press_locked(True)

            mem_prompt = pr.build_ai_memory_after_orders_prompt(
                                                                phase=phase,
                                                                state_text=state_text,
                                                                ai_memory=ai_memory,
                                                                summaries=all_summaries,
                                                                orders=orders,
                                                                 )
        mem_after = await call_openai(system_prompt="You maintain a concise strategy journal.", user_text=mem_prompt)
        db.set_ai_memory(mem_after.strip())
        return


        # Ignore anything else in console (to avoid accidental chatter)
        return

    # ---------- DMs only ----------
    if message.guild is not None:
        return  # ignore other server channels entirely

    # Cooldown
    now_ts = datetime.now(timezone.utc).timestamp()
    if not db.check_and_update_cooldown(message.author.id, now_ts):
        # Keep it quiet; don't spam warnings. Just ignore rapid-fire.
        return
    if db.is_press_locked():
        await message.reply("⚠️ The AI's press is currently locked. No negotiations are being accepted until adjudication and outreach.")
        return

    dm_text = message.content.strip()

    # claim <Country>
    if dm_text.lower().startswith("claim"):
        parts = dm_text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("Usage: `claim <Country>` (e.g., `claim England`).")
            return
        ok, msg = db.claim_country(message.author, parts[1])
        await message.reply(msg)
        return

    # Load per-user thread
    user_country = db.get_player_country(message.author.id)
    if not user_country:
        await message.reply("⚠️ You need to `claim <Country>` first before negotiating. Your messages will be ignored until then.")
        return
    thread = db.load_thread(message.author.id)
    msgs = thread["messages"]
    summary = thread["summary"]

    msgs.append({"role": "user", "content": dm_text})

    # Summarize if needed
    summary, did_refresh = await summ.maybe_summarize_thread(summary, user_country, msgs)

    # Build prompt & reply
    phase, state_text, _ = db.get_game_state()
    ai_memory = db.get_ai_memory()
    prompt = pr.build_dm_prompt(phase, state_text, summary, msgs, user_country, ai_memory)
    reply = await call_openai(SYSTEM_PROMPT, prompt)

    msgs.append({"role": "assistant", "content": reply})
    db.save_thread(message.author.id, msgs, summary,
                 summary_last_updated=datetime.now(timezone.utc).isoformat() if did_refresh else None)

    await message.reply(reply)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
