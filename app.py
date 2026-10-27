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
from html import escape

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
    SECRETS, SECRETS_PROBLEM = st.secrets.to_dict(), None
except Exception as e:  # no secrets, or unreadable: run with a local db and locked host/admin pages
    SECRETS, SECRETS_PROBLEM = {}, f"{type(e).__name__}: {e}".splitlines()[0][:200]
if not SECRETS_PROBLEM and not SECRETS:
    SECRETS_PROBLEM = "The app's Secrets are empty."
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


# ---------- look (the "Broadsheet" design: Game Event App.html) ----------
STALE_MINUTES = 40
NUMBER_WORDS = {3: "three", 4: "four", 5: "five", 6: "six"}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&display=swap');
@import url('https://unpkg.com/@phosphor-icons/web@2.1.1/src/duotone/style.css');

:root {
  --color-bg: #f3f2f2; --color-surface: #eae9e9; --color-text: #201e1d;
  --color-accent: #0088b0; --color-accent-2: #d6006c;
  --color-divider: color-mix(in srgb, #201e1d 16%, transparent);
  --color-neutral-100: #f8f4f4; --color-neutral-200: #eae7e7; --color-neutral-800: #444141;
  --color-accent-100: #e9f8ff; --color-accent-600: #1186ac; --color-accent-700: #006786; --color-accent-800: #004961;
  --color-accent-2-100: #fff1f4; --color-accent-2-700: #aa0b56; --color-accent-2-800: #790e3d;
  --color-process-yellow: #edbb00;
  --font: "Source Serif 4", Georgia, serif;
  --radius-md: 2px;
  --shadow-md: 0 3px 10px color-mix(in srgb, #2d2b2b 16%, transparent);
}

/* page frame: one phone-width column, no Streamlit chrome */
.stApp { background: var(--color-bg); color: var(--color-text); }
.stApp, .stApp button, .stApp input, [data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p,
[data-testid="stToast"] { font-family: var(--font); }
[data-testid="stHeader"], [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
[data-testid="stDecoration"], footer { display: none !important; }
[data-testid="stMainBlockContainer"], .block-container {
  max-width: 480px; padding: 28px 24px 140px !important; }
[data-testid="stVerticalBlock"] { gap: 14px; }
[data-testid="stMarkdownContainer"] p { margin: 0; }
[data-testid="stForm"] { border: 0; padding: 0; }
/* style-only blocks take no room */
[data-testid="stElementContainer"]:has(style), .stMarkdown:has(style) { display: none; }

/* keep button rows side by side on phones (Streamlit stacks columns under 640px) */
[data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 8px !important; align-items: center; }
[data-testid="stColumn"] { min-width: 0 !important; }

/* text */
.kicker { font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: var(--color-accent-700);
  display: flex; align-items: center; gap: 8px; }
.kicker .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--color-accent-2); }
.display { font-weight: 600; line-height: 1.1; text-wrap: pretty; }
.muted { opacity: .65; }
.field-label { font-size: 13px; opacity: .75; margin-bottom: -6px; }
.hint { font-size: 13px; opacity: .65; line-height: 1.4; margin-bottom: 6px; }
.status { display: flex; gap: 10px; align-items: flex-start; font-size: 15px; line-height: 1.45; }
.status i { font-size: 22px; color: var(--color-accent-2); flex: none; margin-top: 1px; }
.lockline { display: flex; gap: 10px; align-items: center; font-size: 15px; }
.lockline i { font-size: 22px; color: var(--color-accent); }

/* buttons */
.stApp button { border-radius: var(--radius-md) !important; font-weight: 600; min-height: 44px; box-shadow: none !important; }
.stApp button p { font-family: var(--font); font-weight: 600; font-size: 15px; }
button[data-testid^="stBaseButton-primary"] { background: var(--color-accent); border: 1px solid var(--color-accent); color: var(--color-bg); }
button[data-testid^="stBaseButton-primary"]:hover { background: var(--color-accent-600); border-color: var(--color-accent-600); color: var(--color-bg); }
button[data-testid^="stBaseButton-secondary"] { background: transparent; border: 1px solid var(--color-divider); color: var(--color-text); }
button[data-testid^="stBaseButton-secondary"]:hover { background: color-mix(in srgb, var(--color-text) 7%, transparent);
  border-color: var(--color-divider); color: var(--color-text); }
button[data-testid="stBaseButton-tertiary"] { color: var(--color-accent); padding: 0 5px; }
button[data-testid="stBaseButton-tertiary"]:hover { background: color-mix(in srgb, var(--color-accent) 10%, transparent); color: var(--color-accent); }
.stApp button:disabled, .stApp button:disabled:hover { opacity: .45; }
button[data-testid^="stBaseButton-primary"]:disabled { background: var(--color-accent); border-color: var(--color-accent); color: var(--color-bg); }
button[data-testid^="stBaseButton-secondary"]:disabled { background: transparent; border-color: var(--color-divider); color: var(--color-text); }
.st-key-signout button p { font-size: 13px; }
.st-key-signout { align-items: flex-end; }
[class*="st-key-remove-"] button, [class*="st-key-remove-"] button:hover,
[class*="st-key-danger-"] button, [class*="st-key-danger-"] button:hover { color: var(--color-accent-2-700) !important; }

/* inputs */
[data-testid="stTextInput"] [data-baseweb="input"] { background: var(--color-surface); border: 1px solid var(--color-divider);
  border-radius: var(--radius-md); min-height: 48px; }
[data-testid="stTextInput"] [data-baseweb="input"]:focus-within { border-color: var(--color-accent); }
[data-testid="stTextInput"] input { font-size: 17px; background: transparent; caret-color: var(--color-accent); }
[data-testid="stTextInput"] input::placeholder { color: color-mix(in srgb, var(--color-text) 65%, transparent); }
[data-testid="InputInstructions"] { display: none; }
.st-key-code input { text-transform: uppercase; letter-spacing: .15em; }
.st-key-pin input { font-size: 20px; letter-spacing: .3em; }

/* game cards */
[class*="st-key-card-"] { background: var(--color-surface); border-radius: var(--radius-md); padding: 15px; gap: 12px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.card-title { font-weight: 600; font-size: 20px; line-height: 1.2; }
.seatbar { height: 4px; background: var(--color-neutral-200); border-radius: 2px; overflow: hidden; margin: 2px 0 5px; }
.seatbar div { height: 100%; background: var(--color-accent); }
.tag { display: inline-flex; align-items: center; font-size: 11px; letter-spacing: .02em; padding: 3px 10px; border-radius: 1.5px; }
.tag-accent { background: var(--color-accent-100); color: var(--color-accent-800); }
.tag-accent-2 { background: var(--color-accent-2-100); color: var(--color-accent-2-800); }
.tag-neutral { background: var(--color-neutral-100); color: var(--color-neutral-800); }

/* tables */
.table { width: 100%; border-collapse: collapse; font-size: 14px; }
.table th { text-align: left; font-size: 11px; letter-spacing: .08em; text-transform: uppercase;
  color: color-mix(in srgb, var(--color-text) 60%, transparent); padding: 10px; border-bottom: 1px solid var(--color-divider); }
.table td { padding: 10px; border-bottom: 1px solid color-mix(in srgb, var(--color-text) 8%, transparent); }
.table tr:hover td { background: color-mix(in srgb, var(--color-text) 4%, transparent); }
.num { text-align: right; }

/* the plate numeral: a score printed as misregistered cyan, magenta and yellow plates */
.cmyk-num { --ground: var(--color-bg); position: relative; width: fit-content; line-height: .9; font-weight: 600;
  font-feature-settings: "pnum" 1; background: var(--ground); padding: .12em; margin: -.12em; }
.cmyk-num .paper { color: var(--color-bg); text-shadow: .036em .025em 0 var(--color-bg), -.031em -.023em 0 var(--color-bg); }
.cmyk-num .plate { position: absolute; inset: .12em; mix-blend-mode: multiply; text-box: trim-both cap alphabetic; }
.cmyk-num .plate-c { color: var(--color-accent); }
.cmyk-num .plate-m { color: var(--color-accent-2); translate: .036em .025em; }
.cmyk-num .plate-y { color: var(--color-process-yellow); translate: -.031em -.023em; }

/* bottom tab bar */
.st-key-tabbar { position: fixed; bottom: 0; left: 50%; transform: translateX(-50%); width: min(100%, 480px); z-index: 1000;
  background: var(--color-bg); box-shadow: 0 -1px 0 var(--color-divider); padding: 6px 12px 22px; }
.st-key-tabbar [data-testid="stHorizontalBlock"] { gap: 0 !important; }
.st-key-tabbar button { min-height: 54px; width: 100%; padding: 0;
  color: color-mix(in srgb, var(--color-text) 55%, transparent) !important; background: none !important; }
.st-key-tabbar button { flex-direction: column; gap: 2px; }
.st-key-tabbar button p { font-size: 11px; font-weight: 400; letter-spacing: .04em; }
.st-key-tabbar button [data-testid="stIconMaterial"] { font-size: 26px; }

/* toasts */
[data-testid="stToast"] { background: var(--color-surface); color: var(--color-text); border-radius: var(--radius-md); box-shadow: var(--shadow-md); }
</style>
"""

TABS = [("player", "Play", ":material/style:"), ("board", "Board", ":material/trophy:"),
        ("host", "Host", ":material/flag:"), ("admin", "Admin", ":material/build:")]


def html(markup):
    st.markdown(markup, unsafe_allow_html=True)


def esc(value):
    return escape(str(value))


def kicker(label, dot=False):
    html(f'<div class="kicker">{"<span class=dot></span>" if dot else ""}{esc(label)}</div>')


def plate_number(value, size):
    v = esc(value)
    plates = "".join(f'<span class="plate plate-{c}" aria-hidden="true">{v}</span>' for c in "cmy")
    return f'<div class="cmyk-num" style="font-size:{size}px"><span class="paper">{v}</span>{plates}</div>'


def say(message, icon=":material/error:", rerun=False):
    """Show a toast; when the page is about to rerun, hold it until the next run draws."""
    if rerun:
        st.session_state.setdefault("toasts", []).append((message, icon))
        st.rerun()
    st.toast(message, icon=icon)


def tab_bar():
    # ?view=board in the link opens straight on that tab, e.g. for the big screen.
    linked = st.query_params.get("view", "player")
    view = st.session_state.setdefault("view", linked if linked in dict((k, 0) for k, *_ in TABS) else "player")
    with st.container(key="tabbar"):
        for col, (key, label, icon) in zip(st.columns(len(TABS)), TABS):
            if col.button(label, icon=icon, key=f"tab-{key}", type="tertiary"):
                st.session_state["view"] = view = key
    html(f"<style>.st-key-tab-{view} button {{ color: var(--color-accent) !important; }}"
         + (".block-container, [data-testid=stMainBlockContainer] { max-width: 760px; }" if view == "board" else "")
         + "</style>")
    return view


# ---------- pages ----------
def leaderboard_page():
    @st.fragment(run_every=REFRESH_SECONDS)
    def live_board():
        is_open = event_open()
        kicker("Live · updates as hosts score" if is_open else "Event closed · final scores", dot=True)
        html('<div class="display" style="font-size:32px">Universal leaderboard</div>')
        # Players who signed up but never joined a game stay off the board.
        df = query(
            "SELECT p.name, p.score, p.current_game, COUNT(pl.game) AS games "
            "FROM players p LEFT JOIN played pl ON pl.player_id = p.id "
            "GROUP BY p.id "
            "HAVING p.current_game IS NOT NULL OR p.score <> 0 OR COUNT(pl.game) > 0 "
            "ORDER BY p.score DESC, p.name"
        )
        if df.empty:
            html('<div class="muted" style="font-size:15px">No players yet.</div>')
            return
        df["rank"] = df["score"].rank(method="min", ascending=False).astype(int)
        df["now"] = df["current_game"].where(df["current_game"].notna(), None)

        lead = df.iloc[0]
        now = f"Now in {lead['now']}" if lead["now"] else "Between games"
        runners = "".join(
            f'<div><div class="muted" style="font-size:13px">{place}</div>'
            f'<div style="display:flex;align-items:baseline;gap:10px"><span class="display" style="font-size:30px">{int(r["score"])}</span>'
            f'<span style="font-size:15px">{esc(r["name"])}</span></div></div>'
            for place, (_, r) in zip(["Second", "Third"], df.iloc[1:3].iterrows())
        )
        html(
            '<div style="display:flex;flex-direction:column;gap:18px;margin:6px 0 4px">'
            '<div style="display:flex;justify-content:space-between;align-items:flex-end">'
            f'<div style="display:flex;flex-direction:column;gap:4px"><div class="muted" style="font-size:13px">First place</div>'
            f'<div class="display" style="font-size:26px">{esc(lead["name"])}</div><div class="muted" style="font-size:13px">{now}</div></div>'
            f'{plate_number(int(lead["score"]), 88)}</div>'
            f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">{runners}</div></div>'
        )
        rows = "".join(
            f'<tr><td class="muted">{r["rank"]}</td><td style="font-weight:{600 if r["rank"] <= 3 else 400}">{esc(r["name"])}</td>'
            f'<td style="opacity:.7">{esc(r["now"].replace("Game ", "G")) if r["now"] else "—"}</td>'
            f'<td class="num" style="opacity:.7">{int(r["games"])}/{len(GAMES)}</td>'
            f'<td class="num" style="font-weight:600">{int(r["score"])}</td></tr>'
            for _, r in df.iterrows()
        )
        html('<table class="table"><thead><tr><th style="width:28px">#</th><th>Player</th><th>Now</th>'
             f'<th class="num">Played</th><th class="num">Pts</th></tr></thead><tbody>{rows}</tbody></table>')

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
    tables = NUMBER_WORDS.get(len(GAMES), len(GAMES))
    kicker(f"Game night · {tables} tables")
    html('<div class="display" style="font-size:36px;line-height:1.08;margin-bottom:14px">'
         'Play every game. One score follows you around the room.</div>')

    with st.form("signup", border=False):
        html('<div class="field-label">Your name</div>')
        raw = st.text_input("Your name", max_chars=NAME_MAX, placeholder="e.g. Sara K", label_visibility="collapsed")
        html('<div class="hint">You can\'t change your name after this, so check the spelling.</div>')
        if st.form_submit_button("Start", type="primary", width="stretch"):
            _, code, err = create_player(raw)
            if err:
                say(err)
            else:
                st.query_params["p"] = code
                say(f"You're in. Your code is {code}; use it to get back in on another phone.", ":material/key:", rerun=True)

    with st.form("code-login", border=False):
        html('<div class="field-label" style="margin-top:14px">Already signed up? Enter your code</div>')
        c1, c2 = st.columns([3, 1])
        with c1:
            code = st.text_input("Your code", max_chars=6, placeholder="6 letters", key="code",
                                 label_visibility="collapsed").strip().upper()
        if c2.form_submit_button("Go", width="stretch") and code:
            if query("SELECT 1 FROM players WHERE token = ?", (code,)).empty:
                say("No player with that code.")
            else:
                st.query_params["p"] = code
                st.rerun()


def player_page():
    pid = signed_in_player()
    if pid is None:
        sign_up()
        return

    @st.fragment(run_every=REFRESH_SECONDS)
    def cards():
        me = query("SELECT name, score, current_game, token FROM players WHERE id = ?", (pid,))
        if me.empty:  # removed by the admin
            html('<div class="display" style="font-size:26px;padding-top:40px">Your player was removed.</div>'
                 '<div style="font-size:15px;opacity:.75">Ask the organiser.</div>')
            if st.button("Sign up again", type="primary", width="stretch"):
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

        c1, c2 = st.columns([3, 1])
        with c1:
            kicker("Pick your next game")
        if c2.button("Sign out", type="tertiary", key="signout"):
            del st.query_params["p"]
            st.rerun()
        html(
            '<div style="display:flex;justify-content:space-between;align-items:flex-end;gap:16px">'
            f'<div style="display:flex;flex-direction:column;gap:4px"><div class="display" style="font-size:30px">{esc(name)}</div>'
            f'<div style="font-size:14px;opacity:.75">Played {len(played)} of {len(GAMES)} · code '
            f'<strong style="letter-spacing:.1em">{esc(code)}</strong></div></div>'
            '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px">'
            f'{plate_number(int(score), 64)}'
            '<div style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;opacity:.6">Points</div></div></div>'
        )

        status = None
        if not is_open:
            status = ("ph-pause-circle", "The event isn't open right now. Games unlock when it starts.")
        elif current:
            status = ("ph-play-circle", f"You're playing {current}. Your host will finish you when it ends."
                      if current in locked else f"You're seated in {current}. You can leave until the host starts it.")
        elif len(played) == len(GAMES):
            status = ("ph-confetti", "You've played every game.")
            if not st.session_state.get("celebrated"):
                st.balloons()
                st.session_state["celebrated"] = True
        if status:
            html(f'<div class="status"><i class="ph-duotone {status[0]}"></i><span>{esc(status[1])}</span></div>')

        for game in GAMES:
            seats = int(counts.get(game, 0))
            here, done, started = game == current, game in played, game in locked
            label, tag, tag_class, primary = "Join", "", "tag-neutral", True
            disabled = bool(current) or not is_open
            if here:
                label, tag, tag_class, disabled = ("Playing now" if started else "Seated"), "You", "tag-accent", True
            elif done:
                label, tag, disabled, primary = "Played", "Done", True, False
            elif started:
                label, tag, tag_class, disabled, primary = "In progress", "Started", "tag-accent-2", True, False
            elif seats >= MAX_PER_GAME:
                label, disabled, primary = "Full, try another", True, False

            with st.container(key=f"card-{game.replace(' ', '')}"):
                html(
                    f'<div class="card-head"><div class="card-title">{esc(game)}</div>'
                    f'{f"<span class={chr(34)}tag {tag_class}{chr(34)}>{tag}</span>" if tag else ""}</div>'
                    f'<div class="seatbar"><div style="width:{min(100, seats / MAX_PER_GAME * 100):.0f}%"></div></div>'
                    f'<div style="font-size:12px;opacity:.65">{seats} of {MAX_PER_GAME} seats taken</div>'
                )
                can_leave = here and not started
                cols = st.columns([3, 1]) if can_leave else [st.container()]
                if cols[0].button(label, key=f"j{game}", disabled=disabled, width="stretch",
                                  type="primary" if primary else "secondary"):
                    err = add_to_game(pid, game)
                    say(err) if err else st.rerun()
                if can_leave and cols[1].button("Leave", key=f"l{game}", width="stretch"):
                    err = leave_game(pid)
                    say(err) if err else st.rerun()

    cards()


def pin_form(form_key, label, expected, on_ok):
    with st.form(form_key, border=False):
        if label:
            html(f'<div class="field-label">{esc(label)}</div>')
        c1, c2 = st.columns([3, 1.3])
        with c1:
            pin = st.text_input("PIN", type="password", key="pin", placeholder="PIN" if not label else None,
                                label_visibility="collapsed")
        if c2.form_submit_button("Unlock", type="primary", width="stretch"):
            if expected and pin == expected:
                on_ok()
                st.rerun()
            say("Wrong PIN." if expected else "No PIN is set for this. Add it in the app's secrets.")
    if SECRETS_PROBLEM or "db_url" not in SECRETS:
        # Shown only on the PIN screens, so hosts and the organiser see why nothing unlocks.
        html(f'<div class="hint" style="margin-top:10px">Setup problem: {esc(SECRETS_PROBLEM or "no db_url in Secrets")}. '
             'Scores are being kept in a temporary file, not the database.</div>')


def host_page():
    kicker("Host panel")
    game = st.session_state.setdefault("host_game", GAMES[0])
    authed = st.session_state.setdefault("host_auth", set())
    for col, g in zip(st.columns(len(GAMES)), GAMES):
        if col.button(g, key=f"ht-{g}", width="stretch", type="primary" if g == game else "secondary"):
            st.session_state["host_game"] = g
            st.rerun()

    if game not in authed:
        pin_form("host-pin", f"Host PIN for {game}", HOST_PINS[game], lambda: authed.add(game))
        return

    @st.fragment(run_every=REFRESH_SECONDS)
    def status():
        seated = int(query("SELECT COUNT(*) AS n FROM players WHERE current_game = ?", (game,))["n"][0])
        started, is_open = game in locked_games(), event_open()
        html(
            '<div style="display:flex;justify-content:space-between;align-items:flex-end">'
            f'<div class="display" style="font-size:30px">{esc(game)}</div>'
            f'<div style="font-size:15px"><strong>{seated}</strong><span class="muted"> / {MAX_PER_GAME} seated</span></div></div>'
        )
        text = ("The event is closed. Joining and scoring are paused." if not is_open
                else "Started. No one can join or leave." if started else "Open for players to join.")
        html(f'<div class="lockline"><i class="ph-duotone {"ph-lock" if started else "ph-lock-open"}"></i>'
             f'<span>{text}</span></div>')
        # Locking stops new players joining (and current ones leaving) once the game is under way.
        if st.button("Reopen for joining" if started else "Start game · lock seats", key="lock", width="stretch",
                     type="secondary" if started else "primary"):
            execute("UPDATE games SET locked = ? WHERE name = ?", (0 if started else 1, game))
            st.rerun()

    status()

    # Add a player to this game, e.g. someone without a phone.
    with st.form("add", clear_on_submit=True, border=False):
        c1, c2 = st.columns([3, 1])
        with c1:
            raw = st.text_input("Add player", placeholder="Add a player by name", max_chars=NAME_MAX,
                                label_visibility="collapsed")
        if c2.form_submit_button("Add", width="stretch"):
            name, _ = clean_name(raw)
            found = query("SELECT id FROM players WHERE name_key = ?", (name.lower(),))
            if found.empty:
                pid, _, err = create_player(raw)
            else:
                pid, err = int(found["id"][0]), None
            err = err or add_to_game(pid, game)
            say(err) if err else say(f"Added {name}. Their code is {code_of(pid)}.", ":material/person_add:", rerun=True)

    @st.fragment(run_every=REFRESH_SECONDS)
    def roster():
        df = query("SELECT id, name, score, joined_at FROM players WHERE current_game = ? ORDER BY name", (game,))
        if df.empty:
            html('<div class="muted" style="font-size:15px">No players in your game yet.</div>')
            return
        scoring_off = not event_open()
        for _, p in df.iterrows():
            pid = int(p["id"])
            html(
                '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:8px">'
                f'<div style="display:flex;flex-direction:column"><span style="font-size:18px;font-weight:600">{esc(p["name"])}</span>'
                f'<span style="font-size:12px;opacity:.6">{minutes_since(p["joined_at"])} min at the table</span></div>'
                f'<span class="display" style="font-size:30px">{int(p["score"])}</span></div>'
            )
            cols = st.columns([1, 1, 1, 0.3, 1.5, 1.6])
            # Increment in SQL (score = score + ?) so two hosts never overwrite each other.
            for col, (label, delta, kind) in zip(cols, [("−1", -1, "secondary"), ("+1", 1, "primary"), ("+3", 3, "primary")]):
                if col.button(label, key=f"s{delta}-{pid}", type=kind, disabled=scoring_off, width="stretch"):
                    execute("UPDATE players SET score = score + ? WHERE id = ?", (delta, pid))
                    st.rerun()
            if cols[4].button("Finish", key=f"f{pid}", type="tertiary", help="Played this game; can't join it again"):
                release("id = ?", (pid,), finished=True)
                st.rerun()
            if cols[5].button("Remove", key=f"remove-{pid}", type="tertiary", help="Didn't play; can join this game again later"):
                release("id = ?", (pid,), finished=False)
                st.rerun()

        if st.button("End round · finish everyone", key="endround", width="stretch"):
            release("current_game = ?", (game,), finished=True)
            execute("UPDATE games SET locked = 0 WHERE name = ?", (game,))
            say(f"{game} finished for everyone and reopened.", ":material/check_circle:", rerun=True)
        html('<div style="font-size:12px;opacity:.6;line-height:1.4">Finish marks the game as played. '
             'Remove frees the seat without counting it.</div>')

    roster()


def armed_button(label, confirm_label, key):
    """A button that needs a second tap, so one stray tap can't wipe the event."""
    armed = st.session_state.get("armed") == key
    if st.button(confirm_label if armed else label, key=f"danger-{key}", width="stretch"):
        if armed:
            st.session_state.pop("armed")
            return True
        st.session_state["armed"] = key
        st.rerun()
    return False


def admin_page():
    kicker("Organiser")
    html('<div class="display" style="font-size:32px">Admin</div>')
    if not st.session_state.get("admin_auth"):
        pin_form("admin-pin", None, ADMIN_PIN, lambda: st.session_state.update(admin_auth=True))
        return

    is_open = event_open()
    html(f'<div class="lockline" style="font-size:16px"><i class="ph-duotone {"ph-broadcast" if is_open else "ph-stop-circle"}" '
         f'style="font-size:24px"></i><span>{"Open. Players can join and hosts can score." if is_open else "Closed. Joining and scores are frozen."}</span></div>')
    if st.button("Close event" if is_open else "Open event", key="event", width="stretch",
                 type="secondary" if is_open else "primary"):
        execute("UPDATE settings SET value = ? WHERE key = 'event_open'", ("0" if is_open else "1",))
        st.rerun()

    seated = query("SELECT id, name, current_game, joined_at FROM players "
                   "WHERE current_game IS NOT NULL ORDER BY joined_at")
    locked = locked_games()
    rows = "".join(
        f'<tr><td>{g}</td><td style="opacity:.7">{"Started" if g in locked else "Open"}</td>'
        f'<td class="num" style="font-weight:600">{int((seated["current_game"] == g).sum())}/{MAX_PER_GAME}</td></tr>'
        for g in GAMES
    )
    html(f'<div class="display" style="font-size:20px;margin-top:10px">Games</div><table class="table"><tbody>{rows}</tbody></table>')

    html('<div class="display" style="font-size:20px;margin-top:10px">Seated players</div>'
         '<div class="hint">Longest at a table first, so anyone a host forgot to release is on top.</div>')
    if seated.empty:
        html('<div class="muted" style="font-size:15px">Nobody is in a game.</div>')
    for _, p in seated.iterrows():
        pid, mins = int(p["id"]), minutes_since(p["joined_at"])
        c1, c2, c3 = st.columns([3.4, 1.2, 1.4])
        with c1:
            stale = '<span class="tag tag-accent-2">Stale</span>' if mins >= STALE_MINUTES else ""
            html(f'<div style="display:flex;align-items:center;gap:8px"><div style="flex:1;display:flex;flex-direction:column">'
                 f'<span style="font-size:16px;font-weight:600">{esc(p["name"])}</span>'
                 f'<span style="font-size:12px;opacity:.65">{esc(p["current_game"])} · {mins} min</span></div>{stale}</div>')
        if c2.button("Finish", key=f"af{pid}", type="tertiary"):
            release("id = ?", (pid,), finished=True)
            st.rerun()
        if c3.button("Remove", key=f"remove-a{pid}", type="tertiary"):
            release("id = ?", (pid,), finished=False)
            st.rerun()

    html('<div class="display" style="font-size:20px;margin-top:10px">Player tools</div>')
    players = query("SELECT id, name, score, current_game, token FROM players ORDER BY name")
    if players.empty:
        html('<div class="muted" style="font-size:15px">No players yet.</div>')
    else:
        names = dict(zip(players["id"], players["name"]))
        pid = int(st.selectbox("Player", list(names), format_func=names.get, label_visibility="collapsed"))
        p = players[players["id"] == pid].iloc[0]
        played = query("SELECT game FROM played WHERE player_id = ? ORDER BY game", (pid,))["game"]
        now = p["current_game"] if isinstance(p["current_game"], str) else "—"
        html(f'<div style="font-size:14px;opacity:.8">Code <strong style="letter-spacing:.1em">{esc(p["token"])}</strong> · '
             f'{int(p["score"])} pts · now in {esc(now)} · played: {esc(", ".join(played) or "none")}</div>')

        with st.form(f"rename-{pid}", border=False):
            c1, c2 = st.columns([3, 1.3])
            with c1:
                raw = st.text_input("Rename to", placeholder="Rename to", max_chars=NAME_MAX, label_visibility="collapsed")
            if c2.form_submit_button("Rename", width="stretch"):
                name, err = clean_name(raw)
                if not err:
                    try:
                        execute("UPDATE players SET name = ?, name_key = ? WHERE id = ?", (name, name.lower(), pid))
                    except IntegrityError:
                        err = f'"{name}" is already taken.'
                say(err) if err else st.rerun()

        c1, c2 = st.columns([3, 1.3])
        game = c1.selectbox("Seat in", GAMES, key="admin_game", label_visibility="collapsed")
        if c2.button("Seat", width="stretch"):
            err = add_to_game(pid, game)
            say(err) if err else say(f"{p['name']} → {game}", ":material/check_circle:", rerun=True)

        if armed_button(f"Delete {p['name']}", f"Tap again to delete {p['name']}", f"del{pid}"):
            execute("DELETE FROM played WHERE player_id = ?", (pid,))
            execute("DELETE FROM players WHERE id = ?", (pid,))
            say(f"Deleted {p['name']}.", ":material/check_circle:", rerun=True)

    html('<div class="display" style="font-size:20px;margin-top:10px;color:var(--color-accent-2-700)">Danger zone</div>')
    if armed_button("Reset all scores to 0", "Tap again to reset every score", "reset"):
        execute("UPDATE players SET score = 0")
        say("All scores reset.", ":material/check_circle:", rerun=True)
    if armed_button("Delete all players", "Tap again to delete every player", "wipe"):
        execute("DELETE FROM played")
        execute("DELETE FROM players")
        say("All players deleted.", ":material/check_circle:", rerun=True)


# ---------- main ----------
st.set_page_config(page_title="Game Event", page_icon="🏆", layout="centered", initial_sidebar_state="collapsed")
html(CSS)
init_db()
for message, icon in st.session_state.pop("toasts", []):
    st.toast(message, icon=icon)
view = tab_bar()
{"player": player_page, "board": leaderboard_page, "host": host_page, "admin": admin_page}[view]()
