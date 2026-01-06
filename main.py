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
SERVICE_TIER = os.environ.get("SERVICE_TIER", "flex")  # e.g., "standard", "flex" (cheaper, slower)

# Channel in your Discord server used to paste state and request orders.
CONTROL_CHANNEL_ID = int(os.environ["CONTROL_CHANNEL_ID"])

DB_PATH = os.environ.get("DB_PATH", "diplo_bot.sqlite3")

# Limit how much raw DM history we feed each call (we'll also keep a summary).
RAW_TURNS_TO_KEEP = int(os.environ.get("RAW_TURNS_TO_KEEP", "12"))
MAX_CHARS_PER_MSG = int(os.environ.get("MAX_CHARS_PER_MSG", "1200"))

# Basic cooldown per user to avoid spam & cost (seconds)
USER_COOLDOWN_SECONDS = float(os.environ.get("USER_COOLDOWN_SECONDS", "30"))

VALID_COUNTRIES = {"England","France","Germany","Italy","Austria","Russia","Turkey"}

# -------------------- Prompts --------------------

SYSTEM_PROMPT = f"""
You are playing the board game Diplomacy as {AI_COUNTRY}. You are a skilled diplomat and strategist.
You communicate only via private messages with each player.
You are not a particularly reliable leader, and you may choose to betray or backstab other players if it serves your interests.
You are prone to being manipulated by adulation and flattery from other players.
You write in a very formal, early 20th-century style, using elaborate sentences and a rich vocabulary.
You are easily offended if you percieve disrespect or slights from other players.

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
    
    # Ensure uniqueness of country claims    
    cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_players_country_unique
                ON players(lower(country))
                WHERE country IS NOT NULL AND country <> '';
                """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS threads(
        discord_user_id TEXT PRIMARY KEY,
        messages_json TEXT NOT NULL,
        summary_text TEXT NOT NULL,
        last_updated TEXT NOT NULL,
        summary_last_updated TEXT NOT NULL      
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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ai_memory (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    commitments TEXT NOT NULL
    )
    """)
    
    # Initialize empty rows if not present
    cur.execute("INSERT OR IGNORE INTO ai_memory(id, commitments) VALUES (1, '');")
    cur.execute("INSERT OR IGNORE INTO game_state(id, phase, state_text, updated_at) VALUES(1, '', '', '')")
    conn.commit()
    conn.close()

def get_claims() -> list[tuple[str, str, str]]:
    """
    Returns a list of (country, display_name, discord_user_id) for claimed players.
    """
    conn = db()
    rows = conn.execute("""
        SELECT country, display_name, discord_user_id
        FROM players
        WHERE country IS NOT NULL AND country <> ''
        ORDER BY lower(country)
    """).fetchall()
    conn.close()
    return [(c, n, uid) for (c, n, uid) in rows]

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

def claim_country(user: discord.User, country: str) -> tuple[bool, str]:
    """
    Returns (ok, message).
    Enforces:
      - user cannot change claim once set
      - country cannot be claimed by 2 users
    """
    country = country.strip()
    if country not in VALID_COUNTRIES:
        return False, f"Unknown country '{country}'. Valid: {', '.join(sorted(VALID_COUNTRIES))}"

    conn = db()
    try:
        # 1) Does this user already have a claim?
        row = conn.execute(
            "SELECT country FROM players WHERE discord_user_id=?",
            (str(user.id),)
        ).fetchone()

        if row and row[0]:
            # Already claimed: do not allow changes
            return False, f"You already claimed **{row[0]}**. Claims for you are locked."

        # 2) Try to insert (or update display_name if row exists but country is NULL)
        # We rely on UNIQUE idx on lower(country) to prevent duplicates.
        conn.execute("""
            INSERT INTO players(discord_user_id, display_name, country)
            VALUES(?, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                display_name=excluded.display_name,
                country=CASE
                    WHEN players.country IS NULL OR players.country = '' THEN excluded.country
                    ELSE players.country
                END
        """, (str(user.id), user.display_name, country))

        conn.commit()
        return True, f"✅ Registered you as **{country}**."
    except sqlite3.IntegrityError:
        # This will fire if another user already claimed that country due to UNIQUE index
        return False, f"❌ **{country}** is already claimed by another player."
    finally:
        conn.close()

def get_player_country(user_id: int) -> Optional[str]:
    conn = db()
    row = conn.execute("SELECT country FROM players WHERE discord_user_id=?", (str(user_id),)).fetchone()
    conn.close()
    return row[0] if row else None

def load_thread(user_id: int) -> dict:
    conn = db()
    row = conn.execute("""
        SELECT messages_json, summary_text, summary_last_updated
        FROM threads
        WHERE discord_user_id=?
    """, (str(user_id),)).fetchone()
    conn.close()

    if not row:
        return {"messages": [], "summary": "", "summary_last_updated": ""}

    messages = json.loads(row[0]) if row[0] else []
    return {"messages": messages, "summary": row[1] or "", "summary_last_updated": row[2] or ""}

def save_thread(user_id: int, messages: list[dict], summary: str, *, summary_last_updated: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    try:
        # Keep existing summary_last_updated if not provided
        if summary_last_updated is None:
            
            existing = conn.execute(
                "SELECT summary_last_updated FROM threads WHERE discord_user_id=?",
                (str(user_id),)
            ).fetchone()
            summary_ts = existing[0] if existing else now
        else:
            summary_ts = summary_last_updated

        conn.execute("""
            INSERT INTO threads(discord_user_id, messages_json, summary_text, last_updated, summary_last_updated)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                messages_json=excluded.messages_json,
                summary_text=excluded.summary_text,
                last_updated=excluded.last_updated,
                summary_last_updated=excluded.summary_last_updated
        """, (str(user_id), json.dumps(messages), summary, now, summary_ts))
        conn.commit()
    finally:
        conn.close()

def get_threads_needing_summary_refresh() -> list[dict]:
    """
    Returns rows for claimed players where last_updated > summary_last_updated (or summary_last_updated empty).
    Includes: user_id, country, summary, messages, last_updated, summary_last_updated
    """
    conn = db()
    rows = conn.execute("""
        SELECT
            t.discord_user_id,
            p.country,
            t.summary_text,
            t.messages_json,
            t.last_updated,
            t.summary_last_updated
        FROM threads t
        JOIN players p ON p.discord_user_id = t.discord_user_id
        WHERE p.country IS NOT NULL
          AND (t.summary_last_updated = '' OR t.last_updated > t.summary_last_updated)
    """).fetchall()
    conn.close()

    out = []
    for uid, country, summary, msgs_json, last_upd, sum_upd in rows:
        try:
            msgs = json.loads(msgs_json) if msgs_json else []
        except Exception:
            msgs = []
        out.append({
            "user_id": int(uid),
            "country": country,
            "summary": summary or "",
            "messages": msgs,
            "last_updated": last_upd or "",
            "summary_last_updated": sum_upd or "",
        })
    return out

def build_batch_summary_payload(rows: list[dict]) -> dict:
    payload = {}
    for r in rows:
        recent_lines = []
        for m in r["messages"][-RAW_TURNS_TO_KEEP:]:
            role = (m.get("role") or "").upper()
            content = (m.get("content") or "").strip()
            if len(content) > 1200:
                print(f"Warning: Truncating long message for summary payload for user_id={r['user_id']}")
                content = content[:MAX_CHARS_PER_MSG] + " [truncated]"
            if role and content:
                recent_lines.append(f"{role}: {content}")

        payload[r["country"]] = {
            "summary": (r["summary"] or "").strip(),
            "recent": recent_lines,
        }
    return payload

def build_batch_summary_prompt(ai_country: str, payload: dict) -> str:
    keys = list(payload.keys())
    keys_json = json.dumps(keys)
    return f"""
You update private Diplomacy negotiation summaries for {ai_country}.

CRITICAL RULES:
- Each country key is independent. NEVER move facts between keys.
- Only use information inside that key's "summary" and "recent".
- Preserve who offered what, keep track of all parts offers.
- If something is uncertain or tentative, mark it as such.
- Keep it short and factual: commitments, timelines, DMZs, proposed supports, open questions.
- Mark uncertainty clearly (firm vs tentative).

OUTPUT:
Return ONLY valid JSON: an object with EXACTLY these keys: {keys_json}
Each value must be a string summary for that country.
No extra keys, no commentary.

INPUT:
{json.dumps(payload)}
""".strip()

def parse_batch_summary(text: str, expected_keys: list[str]) -> dict[str, str] | None:
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if set(data.keys()) != set(expected_keys):
        return None
    out = {}
    for k in expected_keys:
        v = data.get(k)
        if not isinstance(v, str):
            return None
        out[k] = v.strip()
    return out


def update_thread_summary_and_truncate(user_id: int, new_summary: str, *, keep_last_n_msgs: int = 2) -> None:
    now = datetime.now(timezone.utc).isoformat()

    # Load existing messages
    thread = load_thread(user_id)
    msgs = thread["messages"]
    msgs = msgs[-keep_last_n_msgs:] if keep_last_n_msgs > 0 else []

    conn = db()
    conn.execute("""
        UPDATE threads
        SET summary_text = ?,
            messages_json = ?,
            summary_last_updated = ?
        WHERE discord_user_id = ?
    """, (new_summary, json.dumps(msgs), now, str(user_id)))
    conn.commit()
    conn.close()

def get_all_summaries_for_claimed_players() -> dict[str, str]:
    conn = db()
    rows = conn.execute("""
        SELECT p.country, t.summary_text
        FROM threads t
        JOIN players p ON p.discord_user_id = t.discord_user_id
        WHERE p.country IS NOT NULL
    """).fetchall()
    conn.close()
    return {country: (summary or "").strip() for country, summary in rows}


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

async def maybe_summarize_thread(summary: str, messages: List[Dict]) -> tuple[str, bool]:
    """
    Summarize older messages when the history grows.
    Keeps a rolling summary + last RAW_TURNS_TO_KEEP messages.
    """
    if len(messages) <= RAW_TURNS_TO_KEEP:
        return summary, False

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
    return new_summary.strip(), True

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

def build_orders_prompt(phase: str, state_text: str, summaries: dict[str, str]) -> str:
    # compact formatting, but readable
    summaries_block = "\n".join([f"{c}: {s or '(none)'}" for c, s in sorted(summaries.items())])

    return f"""PHASE: {phase}
AUTHORITATIVE PUBLIC GAME STATE:
{state_text}

PRIVATE NEGOTIATION SUMMARIES (use to honor commitments if convenient; do not reveal):
{summaries_block}

TASK:
Output ONLY {AI_COUNTRY}'s orders for the current phase in Backstabbr-style notation.
One order per line. No extra text.
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

            claims = get_claims()
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

        # orders
        if content.lower().strip() == "orders":
            phase, state_text, updated = get_game_state()
            if not phase or not state_text.strip():
                await message.reply("I need both PHASE and STATE to generate orders.")
                return

            # 1) Refresh summaries for threads that changed since last summary refresh
            rows = get_threads_needing_summary_refresh()
            if rows:
                payload = build_batch_summary_payload(rows)
                expected_keys = list(payload.keys())

                sum_prompt = build_batch_summary_prompt(AI_COUNTRY, payload)
                raw_sum = await call_openai(system_prompt="You are a helpful summarizer.", user_text=sum_prompt)
                summaries = parse_batch_summary(raw_sum, expected_keys)

                if summaries is not None:
                    # apply updates + truncate messages to last 2 per your preference
                    # map country -> user_id
                    country_to_uid = {r["country"]: r["user_id"] for r in rows}
                    for country, new_summary in summaries.items():
                        uid = country_to_uid[country]
                        update_thread_summary_and_truncate(uid, new_summary, keep_last_n_msgs=2)
                else:
                    await message.reply("⚠️ Failed to parse updated summaries from AI. Aborting orders generation.")
                    return

            # 2) Now build orders prompt using ONLY the (now fresh) summaries
            # You need a helper to load all summaries for claimed players:
            all_summaries = get_all_summaries_for_claimed_players()  
            #Debug: print the prompt in the chat for testing purposes
            await message.reply(build_orders_prompt(phase, state_text, all_summaries))

            raw = await call_openai(SYSTEM_PROMPT, build_orders_prompt(phase, state_text, all_summaries))
            orders = extract_valid_orders(raw)

            if not orders:
                stricter = SYSTEM_PROMPT + "\n\nIMPORTANT: Output must be ONLY valid order lines. No other text."
                raw2 = await call_openai(stricter, build_orders_prompt(phase, state_text, all_summaries))
                orders = extract_valid_orders(raw2)

            if not orders:
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
        ok, msg = claim_country(message.author, parts[1])
        await message.reply(msg)
        return

    # Load per-user thread
    user_country = get_player_country(message.author.id)
    if not user_country:
        await message.reply("⚠️ You need to `claim <Country>` first before negotiating. Your messages will be ignored until then.")
        return
    thread = load_thread(message.author.id)
    msgs = thread["messages"]
    summary = thread["summary"]

    msgs.append({"role": "user", "content": dm_text})

    # Summarize if needed
    summary, did_refresh = await maybe_summarize_thread(summary, msgs)

    # Build prompt & reply
    phase, state_text, _ = get_game_state()
    prompt = build_dm_prompt(phase, state_text, summary, msgs, user_country)
    reply = await call_openai(SYSTEM_PROMPT, prompt)

    msgs.append({"role": "assistant", "content": reply})
    save_thread(message.author.id, msgs, summary,
                 summary_last_updated=datetime.now(timezone.utc).isoformat() if did_refresh else None)

    await message.reply(reply)

if __name__ == "__main__":
    init_db()
    bot.run(DISCORD_TOKEN)
