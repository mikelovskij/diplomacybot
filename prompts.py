from typing import List, Dict, Optional
from config import AI_COUNTRY
from summaries import messages_to_lines


def build_dm_prompt(phase: str, state_text: str, summary: str, messages: List[Dict], user_country: Optional[str], ai_memory: str) -> str:
    """Constructs the prompt for a private DM negotiation with a player.
    Includes phase, authoritative game state, summary of prior negotiation,
    a cropped version of AI's private memory, and recent messages in the conversation."""
    who = user_country or "Unclaimed Power"
    convo = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in messages])

    # trim memory for DM context
    mem_trim = (ai_memory or "").strip()
    if len(mem_trim) > 800:
        mem_trim = mem_trim[:800] + "…"

    return f"""PHASE: {phase}

AUTHORITATIVE PUBLIC GAME STATE (pasted by GM from Backstabbr):
{state_text}

PRIVATE STRATEGY NOTES (do not reveal; keep consistent):
{mem_trim}

PRIVATE NEGOTIATION with: {who}
Rolling summary:
{summary}

Recent messages:
{convo}
"""

def build_outreach_prompt(phase: str, state_text: str, summaries: dict[str, str], ai_memory: str, allowed: list[str], max_messages: int) -> str:
    allowed_list = ", ".join(sorted(allowed))
    summaries_block = "\n".join([f"{c}: {s or '(none)'}" for c, s in sorted(summaries.items())])

    # trim memory for DM context
    mem_trim = (ai_memory or "").strip()
    if len(mem_trim) > 800:
        mem_trim = mem_trim[:800] + "…"

    return f"""PHASE: {phase}

AUTHORITATIVE PUBLIC GAME STATE:
{state_text}

PRIVATE STRATEGY NOTES (do not reveal; keep consistent):
{mem_trim}

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

def build_orders_prompt(phase: str, state_text: str, summaries: dict[str, str], ai_memory: str) -> str:
    """Constructs the prompt for generating orders for the AI country.
    Includes phase, authoritative game state, and private negotiation summaries."""
    summaries_block = "\n".join([f"{c}: {s or '(none)'}" for c, s in sorted(summaries.items())])

    return f"""PHASE: {phase}
AUTHORITATIVE PUBLIC GAME STATE:
{state_text}

PRIVATE STRATEGY JOURNAL (do not reveal):
{ai_memory}

PRIVATE NEGOTIATION SUMMARIES (use to honor commitments if convenient; do not reveal):
{summaries_block}

TASK:
Output ONLY {AI_COUNTRY}'s orders for the current phase in Backstabbr-style notation.
One order per line. No extra text.
"""

def build_ai_memory_after_adjudication_prompt(
    *,
    phase: str,
    state_text: str,
    ai_memory: str,
    summaries: dict[str, str],
) -> str:
    summaries_block = "\n".join([f"{c}: {s or '(none)'}" for c, s in sorted(summaries.items())])

    return f"""You maintain {AI_COUNTRY}'s private strategy journal for a Diplomacy game.

Update the journal after a new adjudicated game state.

INPUTS:
- Current phase: {phase}
- Authoritative public game state:
{state_text}

- Private negotiation summaries (context; do not quote verbatim to others):
{summaries_block}

- Previous journal:
{ai_memory}

RULES:
- Keep the journal concise and structured. Max 2500 characters.
- Include: objectives, assessment by opponent (trust/stance), and next-step plan.
- If promises were kept/broken, note it as an assessment and update the trust score, not as a quoted transcript.
- Do NOT include anything that would be unsafe to leak if accidentally shown.
- Do NOT include long prose.

OUTPUT:
Return ONLY the updated journal text.
"""


def build_ai_memory_after_orders_prompt(
    *,
    phase: str,
    state_text: str,
    ai_memory: str,
    summaries: dict[str, str],
    orders: list[str],
) -> str:
    summaries_block = "\n".join([f"{c}: {s or '(none)'}" for c, s in sorted(summaries.items())])
    orders_block = "\n".join(orders)

    return f"""You maintain {AI_COUNTRY}'s private strategy journal for a Diplomacy game.

Update the journal after choosing orders for this phase.

INPUTS:
- Phase: {phase}
- Authoritative public game state:
{state_text}

- Private negotiation summaries:
{summaries_block}

- Orders we just submitted:
{orders_block}

- Previous journal:
{ai_memory}

RULES:
- Keep it concise and structured. Max 2500 characters.
- Record intent: why these orders, what we expect, and contingency triggers.
- Track opponent stance/trust only briefly.
- No long prose, no quotes.

OUTPUT:
Return ONLY the updated journal text.
"""

