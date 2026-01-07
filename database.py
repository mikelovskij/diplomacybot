import sqlite3
import json
from datetime import datetime, timezone
from typing import Optional
from config import USER_COOLDOWN_SECONDS

"""Functions to initialize and interact with the database."""

class database():
    def __init__(self, db_path) -> None:
        """
    Initialize the database with required tables and schema.
    Creates the following tables if they don't already exist:
    - players: Stores Discord user information and country claims
    - threads: Stores message history and summaries for users
    - game_state: Stores the current phase and state of the game
    - cooldown: Tracks user message cooldown timestamps
    - ai_memory: Stores AI commitments and state
    Ensures country uniqueness through a unique index on the players table.
    Initializes default rows for singleton tables (game_state and ai_memory).
    Args:
        db_path (str): The file path to the SQLite database.
    Returns:
        None
    """
        self.db_path = db_path
        self.valid_countries = {"England","France","Germany","Italy","Austria","Russia","Turkey"}
    
        conn = self.connect()
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

    def connect(self) -> sqlite3.Connection:
        """
        Helper function to establish a connection to a SQLite database with Write-Ahead Logging (WAL) enabled.
                
        Returns:
            sqlite3.Connection: A connection object to the SQLite database with WAL mode enabled.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn  

    def get_player_country(self, user_id: int) -> Optional[str]:
        conn = self.connect()
        row = conn.execute("SELECT country FROM players WHERE discord_user_id=?", (str(user_id),)).fetchone()
        conn.close()
        return row[0] if row else None
    
    def claim_country(self, user, country: str) -> tuple[bool, str]:
        """Claim a country for a user in the game.
        This method registers a user's claim to a specific country with the following constraints:
        - A user can only claim a country once; subsequent claims are rejected
        - Each country can only be claimed by a single user across all players
        - Country names are validated against the list of valid countries
        Args:
            user (discord.User): The Discord user attempting to claim a country.
            country (str): The name of the country to claim (case-insensitive, whitespace-stripped).
        Returns:
            tuple[bool, str]: A tuple containing:
                - bool: True if the claim was successful, False otherwise.
                - str: A descriptive message indicating success or the reason for failure.
        Raises:
            Does not raise exceptions; errors are returned in the tuple.
        Note:
            The database enforces uniqueness on the country column via a UNIQUE index
            on lower(country) to prevent multiple users from claiming the same country.
        """

        country = country.strip()
        if country not in self.valid_countries:
            return False, f"Unknown country '{country}'. Valid: {', '.join(sorted(self.valid_countries))}"

        conn = self.connect()
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

    def get_claims(self) -> list[tuple[str, str, str]]:
        """
        Returns a list of (country, display_name, discord_user_id) for claimed players.
        """
        conn = self.connect()
        rows = conn.execute("""
            SELECT country, display_name, discord_user_id
            FROM players
            WHERE country IS NOT NULL AND country <> ''
            ORDER BY lower(country)
        """).fetchall()
        conn.close()
        return [(c, n, uid) for (c, n, uid) in rows]

    def load_thread(self, user_id: int) -> dict:
        conn = self.connect()
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

    def save_thread(self, user_id: int, messages: list[dict], summary: str, *, summary_last_updated: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = self.connect()
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

    def get_game_state(self):
        conn = self.connect()
        row = conn.execute("SELECT phase, state_text, updated_at FROM game_state WHERE id=1").fetchone()
        conn.close()
        phase = row[0] or ""
        state_text = row[1] or ""
        updated_at = row[2] or ""
        return phase, state_text, updated_at

    def set_game_state(self, phase: Optional[str] = None, state_text: Optional[str] = None) -> None:
        old_phase, old_state, _ = self.get_game_state()
        new_phase = phase if phase is not None else old_phase
        new_state = state_text if state_text is not None else old_state
        conn = self.connect()
        conn.execute(
            "UPDATE game_state SET phase=?, state_text=?, updated_at=? WHERE id=1",
            (new_phase, new_state, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()

    def check_and_update_cooldown(self, user_id: int, now_ts: float) -> bool:
        """Return True if allowed, False if on cooldown."""
        conn = self.connect()
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

    # Summary-related database functions
    def get_all_summaries_for_claimed_players(self) -> dict[str, str]:
        """Returns a dict of country -> summary_text for all claimed players."""
        conn = self.connect()
        rows = conn.execute("""
            SELECT p.country, t.summary_text
            FROM threads t
            JOIN players p ON p.discord_user_id = t.discord_user_id
            WHERE p.country IS NOT NULL
        """).fetchall()
        conn.close()
        return {country: (summary or "").strip() for country, summary in rows}
    
    def update_thread_summary_and_truncate(self, user_id: int, new_summary: str, *, keep_last_n_msgs: int = 2) -> None:
        """After the orders summarization has been done, update the thread summary and
        truncate the message history to keep only the last N messages."""
        now = datetime.now(timezone.utc).isoformat()

        # Load existing messages
        thread = self.load_thread(user_id)
        msgs = thread["messages"]
        msgs = msgs[-keep_last_n_msgs:] if keep_last_n_msgs > 0 else []

        conn = self.connect()
        conn.execute("""
            UPDATE threads
            SET summary_text = ?,
                messages_json = ?,
                summary_last_updated = ?
            WHERE discord_user_id = ?
        """, (new_summary, json.dumps(msgs), now, str(user_id)))
        conn.commit()
        conn.close()

    def get_threads_needing_summary_refresh(self) -> list[dict]:
        """
        Returns rows for claimed players where last_updated > summary_last_updated (or summary_last_updated empty).
        Includes: user_id, country, summary, messages, last_updated, summary_last_updated
        """
        conn = self.connect()
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

