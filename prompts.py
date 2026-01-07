from typing import List, Dict, Optional
from config import AI_COUNTRY
from summaries import messages_to_lines

def build_dm_prompt(
                    phase: str,
                    state_text: str,
                    summary: str,
                    messages: List[Dict],
                    user_country: Optional[str],
                ) -> str:
    """Constructs the prompt for a private DM negotiation with a player.
    Includes phase, authoritative game state, summary of prior negotiation,
    and recent messages in the conversation."""
    who = user_country or "Unclaimed Power"
    convo_lines = messages_to_lines(
        messages,
        ai_country=AI_COUNTRY,
        player_country=who,
    )
    convo = "\n".join(convo_lines)

    return f"""PHASE: {phase}

AUTHORITATIVE PUBLIC GAME STATE (pasted by GM from Backstabbr):
{state_text}

PRIVATE NEGOTIATION with: {who}
Rolling summary:
{summary}

Recent messages:
{convo}
"""

def build_outreach_prompt(phase: str, state_text: str, summaries: dict[str, str], allowed: list[str], max_messages: int) -> str:
    allowed_list = ", ".join(sorted(allowed))
    summaries_block = "\n".join([f"{c}: {s or '(none)'}" for c, s in sorted(summaries.items())])

    return f"""PHASE: {phase}

AUTHORITATIVE PUBLIC GAME STATE:
{state_text}

PRIVATE NEGOTIATION SUMMARIES (context only; do not reveal):
{summaries_block}

TASK:
You are {AI_COUNTRY}. Propose diplomatic messages to other powers.

Rules:
- Send at most {max_messages} messages total. You may send zero.
- You may ONLY send to: [{allowed_list}]
- Do NOT mention any private negotiations with third parties.
- Keep each message under 500 characters.
- Be concrete (proposal, support, DMZ, question).

OUTPUT:
Return ONLY valid JSON:

[
  {{"to": "<Country>", "message": "<text>"}}
]

If no messages, return [].
""".strip()

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