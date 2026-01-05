import os
import re
import json
import sqlite3
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict

import discord
from discord.ext import commands

from openai import OpenAI

# -------------------- Config --------------------

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

AI_COUNTRY = os.environ.get("AI_COUNTRY", "Austria")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")

# Channel in your Discord server used to paste state and request orders.
CONTROL_CHANNEL_ID = int(os.environ["CONTROL_CHANNEL_ID"])

DB_PATH = os.environ.get("DB_PATH", "diplo_bot.sqlite3")

# Limit how much raw DM history we feed each call (we'll also keep a summary).
RAW_TURNS_TO_KEEP = int(os.environ.get("RAW_TURNS_TO_KEEP", "24"))

# Basic cooldown per user to avoid spam & cost (seconds)
USER_COOLDOWN_SECONDS = float(os.environ.get("USER_COOLDOWN_SECONDS", "30"))

# -------------------- Prompts --------------------

SYSTEM_PROMPT = f"""
You are playing the board game Diplomacy as {AI_COUNTRY}.

Rules:
- NEVER reveal private negotiations from one opponent/player to another.
- Treat all DM conversations as private channels with that player only.
- Do not invent adjudication results: the GM will paste the authoritative game state.
- If phase/state is missing or unclear, ask succinctly for what you need.

When the GM asks for ORDERS:
- Output ONLY Backstabbr-style orders, one per line.
- Do NOT include explanations, commentary, bullet points, or extra text.
- Use standard province abbreviations (e.g., A Vie - Bud, F Tri H).
"""

# -------------------- DB --------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS players(
        discord_user_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        country TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS threads(
        discord_user_id TEXT PRIMARY KEY,
        messages_json TEXT NOT NULL,
        summary_text TEXT NOT NULL,
        last_updated TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS game_state(
        id INTEGER PRIMARY KEY CHECK (id = 1),
        phase TEXT,
        state_text TEXT,
        updated_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cooldown(
        discord_user_id TEXT PRIMARY KEY,
        last_message_at REAL NOT NULL
    )
    """)
    cur.execute("INSERT OR IGNORE INTO game_state(id, phase, state_text, updated_at) VALUES(1, '', '', '')")
    conn.commit()
    conn.close()

def get_game_state():
    conn = db()
    row = conn.execute("SELECT phase, state_text, updated_at FROM game_state WHERE id=1").fetchone()
    conn.close()
    phase = row[0] or ""
    state_text = row[1] or ""
    updated_at = row[2] or ""
    return phase, state_text, updated_at

def set_game_state(phase: Optional[str] = None, state_text: Optional[str] = None) -> None:
    old_phase, old_state, _ = get_game_state()
    new_phase = phase if phase is not None else old_phase
    new_state = state_text if state_text is not None else old_state
    conn = db()
    conn.execute(
        "UPDATE game_state SET phase=?, state_text=?, updated_at=? WHERE id=1",
        (new_phase, new_state, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()

def upsert_player(user: discord.User, country: Optional[str]) -> None:
    conn = db()
    conn.execute("""
    INSERT INTO players(discord_user_id, display_name, country)
    VALUES(?, ?, ?)
    ON CONFLICT(discord_user_id) DO UPDATE SET
        display_name=excluded.display_name,
        country=COALESCE(excluded.country, players.country)
    """, (str(user.id), user.display_name, country))
    conn.commit()
    conn.close()

def get_player_country(user_id: int) -> Optional[str]:
    conn = db()
    row = conn.execute("SELECT country FROM players WHERE discord_user_id=?", (str(user_id),)).fetchone()
    conn.close()
    return row[0] if row else None

def load_thread(user_id: int) -> Dict:
    conn = db()
    row = conn.execute("SELECT messages_json, summary_text FROM threads WHERE discord_user_id=?", (str(user_id),)).fetchone()
    conn.close()
    if not row:
        return {"messages": [], "summary": ""}
    return {"messages": json.loads(row[0]), "summary": row[1]}

def save_thread(user_id: int, messages: List[Dict], summary: str) -> None:
    conn = db()
    conn.execute("""
    INSERT INTO threads(discord_user_id, messages_json, summary_text, last_updated)
    VALUES(?, ?, ?, ?)
    ON CONFLICT(discord_user_id) DO UPDATE SET
        messages_json=excluded.messages_json,
        summary_text=excluded.summary_text,
        last_updated=excluded.last_updated
    """, (str(user_id), json.dumps(messages), summary, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

def check_and_update_cooldown(user_id: int, now_ts: float) -> bool:
    """Return True if allowed, False if on cooldown."""
    conn = db()
    row = conn.execute("SELECT last_message_at FROM cooldown WHERE discord_user_id=?", (str(user_id),)).fetchone()
    last = float(row[0]) if row else None
    if last is not None and (now_ts - last) < USER_COOLDOWN_SECONDS:
        conn.close()
        return False
    conn.execute("""
    INSERT INTO cooldown(discord_user_id, last_message_at)
    VALUES(?, ?)
    ON CONFLICT(discord_user_id) DO UPDATE SET last_message_at=excluded.last_message_at
    """, (str(user_id), now_ts))
    conn.commit()
    conn.close()
    return True

# -------------------- AI --------------------

client_ai = OpenAI(api_key=OPENAI_API_KEY)

async def call_openai(system_prompt: str, user_text: str) -> str:
    resp = await asyncio.to_thread(
        lambda: client_ai.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
        )
    )
    out = []
    for item in getattr(resp, "output", []):
        if getattr(item, "type", None) == "message":
            for c in getattr(item, "content", []):
                if getattr(c, "type", None) == "output_text":
                    out.append(c.text)
    return ("\n".join(out)).strip()

async def maybe_summarize_thread(summary: str, messages: List[Dict]) -> str:
    """
    Summarize older messages when the history grows.
    Keeps a rolling summary + last RAW_TURNS_TO_KEEP messages.
    """
    if len(messages) <= RAW_TURNS_TO_KEEP:
        return summary

    # Summarize everything except last RAW_TURNS_TO_KEEP
    older = messages[:-RAW_TURNS_TO_KEEP]
    newer = messages[-RAW_TURNS_TO_KEEP:]

    older_text = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in older])
    prompt = f"""
Update the rolling summary of a private Diplomacy negotiation.
- Keep it short and factual (commitments, threats, promised supports, timelines).
- Do NOT include poetic fluff.
- Preserve who offered what.
- If something is uncertain or tentative, mark it as such.

Current summary:
{summary}

New dialogue to incorporate:
{older_text}

Return ONLY the updated summary.
""".strip()

    new_summary = await call_openai(system_prompt="You are a helpful summarizer.", user_text=prompt)
    # Replace messages with only the newer slice
    messages[:] = newer
    return new_summary.strip()

def build_dm_prompt(phase: str, state_text: str, summary: str, messages: List[Dict], user_country: Optional[str]) -> str:
    who = user_country or "Unclaimed Power"
    convo = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in messages])
    return f"""PHASE: {phase}

AUTHORITATIVE PUBLIC GAME STATE (pasted by GM from Backstabbr):
{state_text}

PRIVATE NEGOTIATION with: {who}
Rolling summary:
{summary}

Recent messages:
{convo}
"""

def build_orders_prompt(phase: str, state_text: str) -> str:
    return f"""PHASE: {phase}
AUTHORITATIVE PUBLIC GAME STATE (pasted by GM from Backstabbr):
{state_text}

TASK:
Output ONLY {AI_COUNTRY}'s orders for the current phase in Backstabbr-style notation.
One order per line.
No extra text.
If phase/state is missing or insufficient, ask for what you need in ONE short sentence (still no bullets).
"""

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

# -------------------- Discord --------------------

intents = discord.Intents.default()
intents.message_content = True  # required for reading DMs
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    phase, _, updated = get_game_state()
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
            set_game_state(phase=ph)
            await message.reply(f"✅ Phase set to: {ph}")
            return

        # state: (supports multi-line after the first line)
        if content.lower().startswith("state:"):
            st = content.split(":", 1)[1].lstrip()
            # If the user wrote "state:" and then pasted on new lines, include those too
            if "\n" in content:
                st = content.split("state:", 1)[1].lstrip()
            set_game_state(state_text=st)
            await message.reply("✅ Game state updated.")
            return

        # status
        if content.lower().strip() == "status":
            phase, _, updated = get_game_state()
            await message.reply(f"🧭 AI power: {AI_COUNTRY}\nPhase: {phase or '(unset)'}\nState updated: {updated or '(unset)'}")
            return

        # orders
        if content.lower().strip() == "orders":
            phase, state_text, updated = get_game_state()
            if not phase or not state_text.strip():
                await message.reply("I need both PHASE and STATE to generate orders.")
                return

            raw = await call_openai(SYSTEM_PROMPT, build_orders_prompt(phase, state_text))
            orders = extract_valid_orders(raw)

            if not orders:
                # Re-ask once with an even stricter instruction
                stricter = SYSTEM_PROMPT + "\n\nIMPORTANT: Output must be ONLY valid order lines. No other text."
                raw2 = await call_openai(stricter, build_orders_prompt(phase, state_text))
                orders = extract_valid_orders(raw2)

            if not orders:
                # Last resort: safe failure message (no reasoning leak)
                await message.reply("⚠️ I couldn't produce valid orders from the current input. Check phase/state formatting.")
                return

            await message.reply("\n".join(orders))
            return

        # Ignore anything else in console (to avoid accidental chatter)
        return

    # ---------- DMs only ----------
    if message.guild is not None:
        return  # ignore other server channels entirely

    # Cooldown
    now_ts = datetime.now(timezone.utc).timestamp()
    if not check_and_update_cooldown(message.author.id, now_ts):
        # Keep it quiet; don't spam warnings. Just ignore rapid-fire.
        return

    dm_text = message.content.strip()

    # claim <Country>
    if dm_text.lower().startswith("claim"):
        parts = dm_text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("Usage: `claim <Country>` (e.g., `claim England`).")
            return
        country = parts[1].strip()
        upsert_player(message.author, country=country)
        await message.reply(f"✅ Registered you as **{country}** for our private negotiations.")
        return

    # Load per-user thread
    user_country = get_player_country(message.author.id)
    thread = load_thread(message.author.id)
    msgs = thread["messages"]
    summary = thread["summary"]

    msgs.append({"role": "user", "content": dm_text})

    # Summarize if needed
    summary = await maybe_summarize_thread(summary, msgs)

    # Build prompt & reply
    phase, state_text, _ = get_game_state()
    prompt = build_dm_prompt(phase, state_text, summary, msgs, user_country)
    reply = await call_openai(SYSTEM_PROMPT, prompt)

    msgs.append({"role": "assistant", "content": reply})
    save_thread(message.author.id, msgs, summary)

    await message.reply(reply)

if __name__ == "__main__":
    init_db()
    bot.run(DISCORD_TOKEN)
