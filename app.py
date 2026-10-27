"""
Rotational Game Event — live dashboard.

How the "host sees only their 10 / everyone sees universal scores" gap is solved:
every player is ONE row with two fields that matter:
    score         -> universal score, summed across all games
    current_game  -> which game they're in right now (or empty if between games)
Host view   = rows WHERE current_game = <host's game>   (max 10)
Leaderboard = ALL rows ORDER BY score                   (ignores current_game)
Moving a player to another game only changes current_game; their score travels with them.

Players sign up once (name is then locked) and pick their own games in any order.
The `played` table records games a host has marked finished, so nobody repeats one.
Hosts lock their game once it starts; players can leave a game only before that.
"""

import re
import secrets
import threading
import time

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

GAMES = ["Game 1", "Game 2", "Game 3", "Game 4"]
MAX_PER_GAME = 10
NAME_MIN, NAME_MAX = 2, 20
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I lookalikes
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


def run(conn, sql, *params):
    return conn.execute(*_bind(sql, params))


def one(conn, sql, *params):
    return run(conn, sql, *params).fetchone()


def new_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


@st.cache_resource
def init_db():
    """Create the tables, or upgrade ones made by an older version of the app. Runs once per server."""
    id_col = "INTEGER PRIMARY KEY AUTOINCREMENT" if engine().dialect.name == "sqlite" else "SERIAL PRIMARY KEY"
    with engine().begin() as conn:
        run(conn, f"""CREATE TABLE IF NOT EXISTS players (
                          id {id_col},
                          name TEXT NOT NULL,
                          name_key TEXT UNIQUE NOT NULL,  -- lower-cased name, so "ana" == "Ana"
                          score INTEGER NOT NULL DEFAULT 0,
                          current_game TEXT
                      )""")
        # One row per game a host marked finished for that player.
        run(conn, """CREATE TABLE IF NOT EXISTS played (
                         player_id INTEGER NOT NULL REFERENCES players(id),
                         game TEXT NOT NULL,
                         PRIMARY KEY (player_id, game)
                     )""")
        run(conn, "CREATE TABLE IF NOT EXISTS games (name TEXT PRIMARY KEY, locked INTEGER NOT NULL DEFAULT 0)")
        run(conn, "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    columns = {c["name"] for c in inspect(engine()).get_columns("players")}
    with engine().begin() as conn:
        # token = the player's private login code; joined_at = when they took their current seat.
        for col, kind in [("token", "TEXT"), ("joined_at", "BIGINT")]:
            if col not in columns:
                run(conn, f"ALTER TABLE players ADD COLUMN {col} {kind}")
        run(conn, "CREATE UNIQUE INDEX IF NOT EXISTS players_token ON players (token)")
        for (pid,) in run(conn, "SELECT id FROM players WHERE token IS NULL").fetchall():
            run(conn, "UPDATE players SET token = ? WHERE id = ?", new_code(), pid)
        for game in GAMES:
            run(conn, "INSERT INTO games (name) VALUES (?) ON CONFLICT (name) DO NOTHING", game)
        run(conn, "INSERT INTO settings (key, value) VALUES ('event_open', '1') ON CONFLICT (key) DO NOTHING")


def query(sql, params=()):
    stmt, binds = _bind(sql, params)
    with engine().connect() as conn:
        return pd.read_sql_query(stmt, conn, params=binds)


def execute(sql, params=()):
    with engine().begin() as conn:
        run(conn, sql, *params)


def event_open():
    return query("SELECT value FROM settings WHERE key = 'event_open'")["value"][0] == "1"


def locked_games():
    return {g for g, locked in query("SELECT name, locked FROM games").values if locked}


def clean_name(raw):
    """Tidy a typed name. Returns (name, error or None)."""
    name = " ".join(raw.split())
    if not NAME_MIN <= len(name) <= NAME_MAX:
        return name, f"Name must be {NAME_MIN}–{NAME_MAX} characters."
    if not re.search(r"[^\W\d_]", name):
        return name, "Name must contain letters."
    return name, None


def create_player(raw):
    """Register a new player. Returns (player id, login code, error or None)."""
    name, err = clean_name(raw)
    if err:
        return None, None, err
    code = new_code()
    try:
        with engine().begin() as conn:
            run(conn, "INSERT INTO players (name, name_key, token) VALUES (?, ?, ?)", name, name.lower(), code)
            pid = one(conn, "SELECT id FROM players WHERE token = ?", code)[0]
    except IntegrityError:
        return None, None, f'"{name}" is already taken. Add your last initial, e.g. "{name} K".'
    return pid, code, None


def add_to_game(pid, game):
    """Seat a player in `game`. Returns an error string or None.

    Runs under a lock in one transaction so two people can't grab the 10th seat at once.
    """
    with seat_lock(), engine().begin() as conn:
        row = one(conn, "SELECT name, current_game FROM players WHERE id = ?", pid)
        if row is None:
            return "Player not found."
        name, current = row
        if one(conn, "SELECT value FROM settings WHERE key = 'event_open'")[0] != "1":
            return "The event isn't open right now."
        if current == game:
            return f"{name} is already in {game}."
        if current:
            return f"{name} is currently in {current}. That host must release them first."
        if one(conn, "SELECT 1 FROM played WHERE player_id = ? AND game = ?", pid, game):
            return f"{name} has already played {game}."
        if one(conn, "SELECT locked FROM games WHERE name = ?", game)[0]:
            return f"{game} has already started. Wait for the next round."
        if one(conn, "SELECT COUNT(*) FROM players WHERE current_game = ?", game)[0] >= MAX_PER_GAME:
            return f"{game} is full ({MAX_PER_GAME} players)."
        run(conn, "UPDATE players SET current_game = ?, joined_at = ? WHERE id = ?", game, int(time.time()), pid)
    return None


def leave_game(pid):
    """Let a player back out of a game that hasn't started. Returns an error string or None."""
    with seat_lock(), engine().begin() as conn:
        current = one(conn, "SELECT current_game FROM players WHERE id = ?", pid)[0]
        if current and one(conn, "SELECT locked FROM games WHERE name = ?", current)[0]:
            return f"{current} has started. Ask your host if you need to leave."
        run(conn, "UPDATE players SET current_game = NULL, joined_at = NULL WHERE id = ?", pid)
    return None


def release(where, params, finished):
    """Free the players matching `where`. finished=True records the game as played for them."""
    with engine().begin() as conn:
        if finished:
            run(conn, f"INSERT INTO played (player_id, game) SELECT id, current_game FROM players "
                      f"WHERE current_game IS NOT NULL AND {where} ON CONFLICT DO NOTHING", *params)
        run(conn, f"UPDATE players SET current_game = NULL, joined_at = NULL WHERE {where}", *params)


def code_of(pid):
    return query("SELECT token FROM players WHERE id = ?", (pid,))["token"][0]


def minutes_since(joined_at):
    return 0 if pd.isna(joined_at) else int((time.time() - joined_at) // 60)


# ---------- pages ----------
def leaderboard_page():
    st.title("🏆 Universal Leaderboard")

    @st.fragment(run_every=REFRESH_SECONDS)
    def live_board():
        # Players who signed up but never joined a game stay off the board.
        df = query(
            "SELECT p.name, p.score, p.current_game, COUNT(pl.game) AS games "
            "FROM players p LEFT JOIN played pl ON pl.player_id = p.id "
            "GROUP BY p.id "
            "HAVING p.current_game IS NOT NULL OR p.score <> 0 OR COUNT(pl.game) > 0 "
            "ORDER BY p.score DESC, p.name"
        )
        if not event_open():
            st.info("The event is closed. These are the final scores.")
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


def signed_in_player():
    """The player whose login code is in the page link (?p=CODE), or None."""
    code = st.query_params.get("p", "")
    if not code:
        return None
    row = query("SELECT id FROM players WHERE token = ?", (code,))
    if row.empty:
        del st.query_params["p"]
        return None
    return int(row["id"][0])


def sign_up():
    with st.form("signup"):
        raw = st.text_input("Your name", max_chars=NAME_MAX, placeholder="e.g. Sara K")
        st.caption("You can't change your name after this, so check the spelling.")
        if st.form_submit_button("Start", type="primary"):
            _, code, err = create_player(raw)
            if err:
                st.error(err)
            else:
                st.query_params["p"] = code
                st.rerun()

    with st.expander("Already signed up? Enter your code"):
        code = st.text_input("Your code", max_chars=6).strip().upper()
        if code:
            if query("SELECT 1 FROM players WHERE token = ?", (code,)).empty:
                st.error("No player with that code.")
            else:
                st.query_params["p"] = code
                st.rerun()


def player_page():
    st.title("🃏 Pick your next game")
    pid = signed_in_player()
    if pid is None:
        sign_up()
        return

    @st.fragment(run_every=REFRESH_SECONDS)
    def cards():
        me = query("SELECT name, score, current_game, token FROM players WHERE id = ?", (pid,))
        if me.empty:  # removed by the admin
            st.error("Your player was removed. Ask the organiser.")
            if st.button("Sign up again"):
                del st.query_params["p"]
                st.rerun()
            return
        name, score, current, code = me.iloc[0]
        current = current if isinstance(current, str) else None
        played = set(query("SELECT game FROM played WHERE player_id = ?", (pid,))["game"])
        counts = dict(
            query("SELECT current_game, COUNT(*) AS n FROM players "
                  "WHERE current_game IS NOT NULL GROUP BY current_game").values
        )
        locked = locked_games()
        is_open = event_open()

        st.subheader(f"👤 {name}")
        st.write(f"Score **{int(score)}** · played {len(played)}/{len(GAMES)}")
        st.caption(f"Your code: **{code}**. Bookmark this page, or use the code to get back in on another phone.")
        if not is_open:
            st.warning("The event isn't open right now. The cards unlock when it starts.")
        if current:
            st.success(f"You're in **{current}**. Your host will finish you when the game ends.")
            if current not in locked:
                st.caption("Joined the wrong game? You can leave until it starts.")
        elif len(played) == len(GAMES):
            if not st.session_state.get("celebrated"):
                st.balloons()
                st.session_state["celebrated"] = True
            st.success("You've played every game! 🎉")

        for col, game in zip(st.columns(len(GAMES)), GAMES):
            seats = int(counts.get(game, 0))
            with col.container(border=True):
                st.subheader(game)
                st.progress(seats / MAX_PER_GAME, text=f"{seats}/{MAX_PER_GAME} seats")
                if game == current:
                    st.button("▶ Playing now", key=f"j{game}", disabled=True, width="stretch")
                    if game not in locked and st.button("Leave", key=f"l{game}", width="stretch"):
                        err = leave_game(pid)
                        st.error(err) if err else st.rerun()
                elif game in played:
                    st.button("✅ Played", key=f"j{game}", disabled=True, width="stretch")
                elif game in locked:
                    st.button("🔒 In progress", key=f"j{game}", disabled=True, width="stretch")
                elif seats >= MAX_PER_GAME:
                    st.button("Full, try another", key=f"j{game}", disabled=True, width="stretch")
                elif st.button("Join", key=f"j{game}", disabled=bool(current) or not is_open,
                               type="primary", width="stretch"):
                    err = add_to_game(pid, game)
                    st.error(err) if err else st.rerun()

    cards()


def host_page():
    st.title("🎮 Host Panel")
    game = st.selectbox("Your game", GAMES)
    pin = st.text_input("Host PIN", type="password")
    if not HOST_PINS[game] or pin != HOST_PINS[game]:
        st.warning("Enter your host PIN to manage this game.")
        return

    is_open = event_open()
    if not is_open:
        st.warning("The event is closed: joining and scoring are paused.")

    # Locking stops new players joining (and current ones leaving) once the game is under way.
    if game in locked_games():
        st.info(f"🔒 {game} has started. No one can join.")
        if st.button("Reopen for joining"):
            execute("UPDATE games SET locked = 0 WHERE name = ?", (game,))
            st.rerun()
    else:
        st.info(f"🟢 {game} is open for players to join.")
        if st.button("Start game (lock seats)", type="primary"):
            execute("UPDATE games SET locked = 1 WHERE name = ?", (game,))
            st.rerun()

    # Add a player to this game, e.g. someone without a phone.
    with st.form("add", clear_on_submit=True):
        c1, c2 = st.columns([3, 1])
        raw = c1.text_input("Add player to your game", placeholder="Player name", max_chars=NAME_MAX)
        if c2.form_submit_button("Add", width='stretch'):
            name, _ = clean_name(raw)
            found = query("SELECT id FROM players WHERE name_key = ?", (name.lower(),))
            if found.empty:
                pid, _, err = create_player(raw)
            else:
                pid, err = int(found["id"][0]), None
            err = err or add_to_game(pid, game)
            st.error(err) if err else st.success(f"Added {name}. Their code is **{code_of(pid)}**")

    @st.fragment(run_every=REFRESH_SECONDS)
    def roster():
        df = query(
            "SELECT id, name, score, joined_at FROM players WHERE current_game = ? ORDER BY name", (game,)
        )
        st.subheader(f"Players in {game} right now ({len(df)}/{MAX_PER_GAME})")
        if df.empty:
            st.info("No players in your game.")
            return

        for _, p in df.iterrows():
            pid = int(p["id"])
            c1, c2, c3, c4, c5, c6 = st.columns([3, 1.2, 1.6, 1, 1.1, 1.1])
            c1.markdown(f"**{p['name']}**  \n⏱ {minutes_since(p['joined_at'])} min")
            c2.markdown(f"Score: **{p['score']}**")
            delta = c3.number_input(
                "points", value=1, step=1, key=f"d{pid}", label_visibility="collapsed"
            )
            # Increment in SQL (score = score + ?) so two hosts never overwrite each other.
            if c4.button("➕ Add", key=f"a{pid}", disabled=not is_open):
                execute("UPDATE players SET score = score + ? WHERE id = ?", (delta, pid))
                st.rerun()
            if c5.button("✅ Finish", key=f"f{pid}", help="Played this game; can't join it again"):
                release("id = ?", (pid,), finished=True)
                st.rerun()
            if c6.button("Remove", key=f"r{pid}", help="Didn't play; can join this game again later"):
                release("id = ?", (pid,), finished=False)
                st.rerun()

        if st.button("End round: finish everyone and reopen game", type="secondary"):
            release("current_game = ?", (game,), finished=True)
            execute("UPDATE games SET locked = 0 WHERE name = ?", (game,))
            st.rerun()

    roster()


def admin_page():
    st.title("🛠️ Admin")
    if not ADMIN_PIN or st.text_input("Admin PIN", type="password") != ADMIN_PIN:
        st.warning("Enter admin PIN.")
        return

    st.subheader("Event")
    if event_open():
        st.success("🟢 Open: players can join games and hosts can score.")
        if st.button("Close event (freeze joining and scores)"):
            execute("UPDATE settings SET value = '0' WHERE key = 'event_open'")
            st.rerun()
    else:
        st.error("🔴 Closed: nobody can join and scores are frozen.")
        if st.button("Open event", type="primary"):
            execute("UPDATE settings SET value = '1' WHERE key = 'event_open'")
            st.rerun()

    st.subheader("Games")
    seated = query("SELECT id, name, current_game, joined_at FROM players "
                   "WHERE current_game IS NOT NULL ORDER BY joined_at")
    locked = locked_games()
    st.dataframe(pd.DataFrame({
        "Game": GAMES,
        "Players": [int((seated["current_game"] == g).sum()) for g in GAMES],
        "Status": ["🔒 Started" if g in locked else "🟢 Open" for g in GAMES],
    }), hide_index=True)

    st.subheader("Players in a game")
    st.caption("Longest-seated first, so anyone a host forgot to release shows up at the top.")
    if seated.empty:
        st.write("Nobody is in a game.")
    for _, p in seated.iterrows():
        pid = int(p["id"])
        c1, c2, c3 = st.columns([4, 1, 1])
        c1.write(f"**{p['name']}** in {p['current_game']} · ⏱ {minutes_since(p['joined_at'])} min")
        if c2.button("✅ Finish", key=f"af{pid}"):
            release("id = ?", (pid,), finished=True)
            st.rerun()
        if c3.button("Remove", key=f"ar{pid}"):
            release("id = ?", (pid,), finished=False)
            st.rerun()

    st.subheader("Player tools")
    players = query("SELECT id, name, score, current_game, token FROM players ORDER BY name")
    if players.empty:
        st.write("No players yet.")
    else:
        names = dict(zip(players["id"], players["name"]))
        pid = int(st.selectbox("Player", list(names), format_func=names.get))
        p = players[players["id"] == pid].iloc[0]
        played = query("SELECT game FROM played WHERE player_id = ? ORDER BY game", (pid,))["game"]
        st.write(f"Code **{p['token']}** · score **{p['score']}** · "
                 f"now in **{p['current_game'] if isinstance(p['current_game'], str) else '—'}** · "
                 f"played: {', '.join(played) or 'none'}")

        c1, c2 = st.columns([3, 1])
        raw = c1.text_input("Rename to", key=f"rename{pid}", max_chars=NAME_MAX)
        if c2.button("Rename", width="stretch"):
            name, err = clean_name(raw)
            if not err:
                try:
                    execute("UPDATE players SET name = ?, name_key = ? WHERE id = ?", (name, name.lower(), pid))
                except IntegrityError:
                    err = f'"{name}" is already taken.'
            st.error(err) if err else st.rerun()

        c1, c2 = st.columns([3, 1])
        game = c1.selectbox("Seat in", GAMES, key="admin_game")
        if c2.button("Seat", width="stretch"):
            err = add_to_game(pid, game)
            st.error(err) if err else st.success(f"{p['name']} → {game}")

        if st.checkbox(f"I want to delete {p['name']}", key=f"del{pid}"):
            if st.button(f"Delete {p['name']}", type="primary"):
                execute("DELETE FROM played WHERE player_id = ?", (pid,))
                execute("DELETE FROM players WHERE id = ?", (pid,))
                st.rerun()

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
