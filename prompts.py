from typing import List, Dict, Optional
from config import AI_COUNTRY

def build_dm_prompt(phase: str, state_text: str, summary: str, messages: List[Dict], user_country: Optional[str]) -> str:
    """Constructs the prompt for a private DM negotiation with a player.
    Includes phase, authoritative game state, rolling summary, and recent messages."""
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
    """Constructs the prompt for generating orders for the AI country.
    Includes phase, authoritative game state, and private negotiation summaries."""
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