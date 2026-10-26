"""
Rotational Game Event — live dashboard.

How the "host sees only their 10 / everyone sees universal scores" gap is solved:
every player is ONE row with two fields that matter:
    score         -> universal score, summed across all games
    current_game  -> which game they're in right now (or empty if between games)
Host view   = rows WHERE current_game = <host's game>   (max 10)
Leaderboard = ALL rows ORDER BY score                   (ignores current_game)
Moving a player to another game only changes current_game; their score travels with them.

Players pick their own games (any order) from the Player page; the `played` table
stops anyone from joining the same game twice.
"""

import re
import threading

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

GAMES = ["Game 1", "Game 2", "Game 3", "Game 4"]
MAX_PER_GAME = 10
# PINs and the database URL come from Streamlit secrets (.streamlit/secrets.toml locally,
# or the app's Secrets settings on Streamlit Cloud) so they never live in the repo.
try:
    SECRETS = st.secrets.to_dict()
except Exception:  # no secrets file: run with a local db and locked host/admin pages
    SECRETS = {}
HOST_PINS = {g: str(SECRETS.get("host_pins", {}).get(g, "")) for g in GAMES}
ADMIN_PIN = str(SECRETS.get("admin_pin", ""))
# Postgres (e.g. Supabase) in production so scores survive app restarts; local file otherwise.
DB_URL = SECRETS.get("db_url", "sqlite:///event.db")
REFRESH_SECONDS = 3


# ---------- database ----------
@st.cache_resource
def engine():
    return create_engine(DB_URL, pool_pre_ping=True)


@st.cache_resource
def seat_lock():
    # Streamlit runs every session in one process, so this serialises all joins.
    return threading.Lock()


def _bind(sql, params):
    """Turn `?` placeholders into named binds so the same SQL runs on SQLite and Postgres."""
    names = iter(range(len(params)))
    return text(re.sub(r"\?", lambda _: f":p{next(names)}", sql)), {f"p{i}": v for i, v in enumerate(params)}


def init_db():
    id_col = "INTEGER PRIMARY KEY AUTOINCREMENT" if engine().dialect.name == "sqlite" else "SERIAL PRIMARY KEY"
    with engine().begin() as conn:
        conn.execute(text(
            f"""CREATE TABLE IF NOT EXISTS players (
                   id {id_col},
                   name TEXT NOT NULL,
                   name_key TEXT UNIQUE NOT NULL,  -- lower-cased name, so "ana" == "Ana"
                   score INTEGER NOT NULL DEFAULT 0,
                   current_game TEXT
               )"""
        ))
        conn.execute(text(
            """CREATE TABLE IF NOT EXISTS played (
                   player_id INTEGER NOT NULL REFERENCES players(id),
                   game TEXT NOT NULL,
                   PRIMARY KEY (player_id, game)
               )"""
        ))


def query(sql, params=()):
    stmt, binds = _bind(sql, params)
    with engine().connect() as conn:
        return pd.read_sql_query(stmt, conn, params=binds)


def execute(sql, params=()):
    stmt, binds = _bind(sql, params)
    with engine().begin() as conn:
        conn.execute(stmt, binds)


def add_to_game(name, game):
    """Create the player if new, then seat them in `game`. Returns an error string or None.

    Runs under a lock in one transaction so two people can't grab the 10th seat at once.
    """
    name = name.strip()
    if not name:
        return "Enter a name."

    def run(conn, sql, *params):
        return conn.execute(*_bind(sql, params))

    def one(conn, sql, *params):
        return run(conn, sql, *params).fetchone()

    with seat_lock(), engine().begin() as conn:
        pid, current = one(conn, "SELECT id, current_game FROM players WHERE name_key = ?", name.lower()) or (None, None)
        if current == game:
            return f"{name} is already in {game}."
        if current:
            return f"{name} is currently in {current}. That host must release them first."
        if pid and one(conn, "SELECT 1 FROM played WHERE player_id = ? AND game = ?", pid, game):
            return f"{name} has already played {game}."
        if one(conn, "SELECT COUNT(*) FROM players WHERE current_game = ?", game)[0] >= MAX_PER_GAME:
            return f"{game} is full ({MAX_PER_GAME} players)."
        # Only create the player once they actually get a seat, so rejected names don't hit the leaderboard.
        if pid is None:
            run(conn, "INSERT INTO players (name, name_key) VALUES (?, ?)", name, name.lower())
            pid = one(conn, "SELECT id FROM players WHERE name_key = ?", name.lower())[0]
        run(conn, "UPDATE players SET current_game = ? WHERE id = ?", game, pid)
        run(conn, "INSERT INTO played (player_id, game) VALUES (?, ?)", pid, game)
    return None


# ---------- pages ----------
def leaderboard_page():
    st.title("🏆 Universal Leaderboard")

    @st.fragment(run_every=REFRESH_SECONDS)
    def live_board():
        df = query(
            "SELECT p.name, p.score, p.current_game, COUNT(pl.game) AS games "
            "FROM players p LEFT JOIN played pl ON pl.player_id = p.id "
            "GROUP BY p.id ORDER BY p.score DESC, p.name"
        )
        if df.empty:
            st.info("No players yet.")
            return
        df.insert(0, "Rank", df["score"].rank(method="min", ascending=False).astype(int))
        df["current_game"] = df["current_game"].fillna("—")
        df["games"] = df["games"].astype(str) + f"/{len(GAMES)}"
        df.columns = ["Rank", "Player", "Score", "Now playing", "Games played"]

        top = df.head(3)
        for col, (_, row), medal in zip(st.columns(3), top.iterrows(), ["🥇", "🥈", "🥉"]):
            col.metric(f"{medal} {row['Player']}", row["Score"])

        st.dataframe(df, hide_index=True, width='stretch', height=600)
        st.caption(f"Auto-refreshes every {REFRESH_SECONDS}s")

    live_board()


def player_page():
    st.title("🃏 Pick your next game")
    name = st.text_input("Your name", value=st.session_state.get("me", "")).strip()
    if not name:
        st.info("Enter your name to see the games.")
        return
    st.session_state["me"] = name

    @st.fragment(run_every=REFRESH_SECONDS)
    def cards():
        me = query("SELECT id, score, current_game FROM players WHERE name_key = ?", (name.lower(),))
        current = me["current_game"][0] if not me.empty else None
        played = set() if me.empty else set(
            query("SELECT game FROM played WHERE player_id = ?", (int(me["id"][0]),))["game"]
        )
        counts = dict(
            query("SELECT current_game, COUNT(*) AS n FROM players "
                  "WHERE current_game IS NOT NULL GROUP BY current_game").values
        )

        score = 0 if me.empty else int(me["score"][0])
        st.write(f"**{name}** · score **{score}** · played {len(played)}/{len(GAMES)}")
        if current:
            st.success(f"You're in **{current}**. Your host will release you when the round ends.")
        elif len(played) == len(GAMES):
            st.balloons()
            st.success("You've played every game! 🎉")

        for col, game in zip(st.columns(len(GAMES)), GAMES):
            seats = int(counts.get(game, 0))
            with col.container(border=True):
                st.subheader(game)
                st.progress(seats / MAX_PER_GAME, text=f"{seats}/{MAX_PER_GAME} seats")
                if game == current:
                    st.button("▶ Playing now", key=f"j{game}", disabled=True, width="stretch")
                elif game in played:
                    st.button("✅ Played", key=f"j{game}", disabled=True, width="stretch")
                elif seats >= MAX_PER_GAME:
                    st.button("Full, try another", key=f"j{game}", disabled=True, width="stretch")
                elif st.button("Join", key=f"j{game}", disabled=bool(current), type="primary", width="stretch"):
                    err = add_to_game(name, game)
                    if err:
                        st.error(err)
                    else:
                        st.rerun()

    cards()


def host_page():
    st.title("🎮 Host Panel")
    game = st.selectbox("Your game", GAMES)
    pin = st.text_input("Host PIN", type="password")
    if not HOST_PINS[game] or pin != HOST_PINS[game]:
        st.warning("Enter your host PIN to manage this game.")
        return

    # Add a player to this game
    with st.form("add", clear_on_submit=True):
        c1, c2 = st.columns([3, 1])
        name = c1.text_input("Add player to your game", placeholder="Player name")
        if c2.form_submit_button("Add", width='stretch'):
            err = add_to_game(name, game)
            st.error(err) if err else st.success(f"Added {name.strip()}")

    @st.fragment(run_every=REFRESH_SECONDS)
    def roster():
        df = query(
            "SELECT id, name, score FROM players WHERE current_game = ? ORDER BY name", (game,)
        )
        st.subheader(f"Players in {game} right now ({len(df)}/{MAX_PER_GAME})")
        if df.empty:
            st.info("No players in your game.")
            return

        for _, p in df.iterrows():
            c1, c2, c3, c4, c5 = st.columns([3, 1.2, 2, 1, 1])
            c1.markdown(f"**{p['name']}**")
            c2.markdown(f"Score: **{p['score']}**")
            delta = c3.number_input(
                "points", value=1, step=1, key=f"d{p['id']}", label_visibility="collapsed"
            )
            # Increment in SQL (score = score + ?) so two hosts never overwrite each other.
            if c4.button("➕ Add", key=f"a{p['id']}"):
                execute("UPDATE players SET score = score + ? WHERE id = ?", (delta, int(p["id"])))
                st.rerun()
            if c5.button("Release", key=f"r{p['id']}", help="Player leaves your game"):
                execute("UPDATE players SET current_game = NULL WHERE id = ?", (int(p["id"]),))
                st.rerun()

        if st.button("End round: release everyone", type="secondary"):
            execute("UPDATE players SET current_game = NULL WHERE current_game = ?", (game,))
            st.rerun()

    roster()


def admin_page():
    st.title("🛠️ Admin")
    if not ADMIN_PIN or st.text_input("Admin PIN", type="password") != ADMIN_PIN:
        st.warning("Enter admin PIN.")
        return

    st.subheader("Current seating")
    seating = query("SELECT current_game, COUNT(*) AS players FROM players GROUP BY current_game")
    seating["current_game"] = seating["current_game"].fillna("Not in a game")
    st.dataframe(seating, hide_index=True)

    st.subheader("Seat a player manually")
    c1, c2, c3 = st.columns([3, 2, 1])
    name = c1.text_input("Name", key="admin_name")
    game = c2.selectbox("Game", GAMES, key="admin_game")
    if c3.button("Seat"):
        err = add_to_game(name, game)
        st.error(err) if err else st.success(f"{name.strip()} → {game}")

    with st.expander("Danger zone"):
        if st.button("Reset ALL scores to 0"):
            execute("UPDATE players SET score = 0")
            st.success("Scores reset.")
        if st.button("Delete ALL players"):
            execute("DELETE FROM played")
            execute("DELETE FROM players")
            st.success("All players deleted.")


# ---------- main ----------
st.set_page_config(page_title="Game Event", page_icon="🏆", layout="wide")
init_db()
pages = {"Player": player_page, "Leaderboard": leaderboard_page, "Host": host_page, "Admin": admin_page}
pages[st.sidebar.radio("View", list(pages))]()
