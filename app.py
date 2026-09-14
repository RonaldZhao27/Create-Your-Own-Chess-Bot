"""
app.py (public version)

Anyone can enter their Chess.com username and play against a bot built
from their own game history.

LOCAL TESTING (Windows):
    streamlit run app.py
    (uses your local Stockfish .exe automatically)

DEPLOYING (Streamlit Community Cloud):
    Needs two extra files alongside this one in your GitHub repo:
    - requirements.txt  (listing: streamlit, requests, python-chess)
    - packages.txt      (listing: stockfish)
    packages.txt tells Streamlit Cloud's Linux server to install Stockfish
    via apt-get, so it's available as just the command "stockfish" --
    no manual download needed on the server.
"""

import streamlit as st
import chess
import chess.pgn
import chess.svg
import chess.engine
import io
import json
import os
import random
import math
import requests
import shutil


TIME_CLASSES = {"rapid", "blitz"}
MULTIPV = 5
ENGINE_TIME_LIMIT = 0.15

# Live profile-building settings -- kept small/fast since this now runs
# in real time whenever a NEW visitor enters their username, rather than
# as a one-time offline script like before.
SAMPLE_SIZE = 10
ANALYSIS_DEPTH = 6
MAX_USER_MOVES_PER_GAME = 15
BLUNDER_THRESHOLD_CP = 150

PROFILES_DIR = "profiles"  # per-user cached data lives here


def get_stockfish_path():
    """
    Tries your known local Windows path first (for local testing).
    On Linux (Streamlit Cloud), checks common install locations directly --
    the apt "stockfish" package often installs to /usr/games/stockfish,
    which isn't always included in the PATH Streamlit's environment uses,
    so shutil.which() alone can miss it.
    """
    local_path = r"C:\Users\omega\Downloads\stockfish-windows-x86-64-universal\stockfish\stockfish-windows-x86-64-universal.exe"
    if os.path.exists(local_path):
        return local_path

    common_linux_paths = [
        "/usr/games/stockfish",
        "/usr/bin/stockfish",
        "/usr/local/bin/stockfish",
        "/usr/local/games/stockfish",
    ]
    for path in common_linux_paths:
        if os.path.exists(path):
            return path

    found = shutil.which("stockfish")
    if found:
        return found

    return "stockfish"  # last resort -- will error clearly if truly not found


STOCKFISH_PATH = get_stockfish_path()


# ---- Chess.com API helpers ----

def get_archive_urls(username):
    url = f"https://api.chess.com/pub/player/{username}/games/archives"
    response = requests.get(url, headers={"User-Agent": "chess-bot-project"})
    response.raise_for_status()
    return response.json()["archives"]


def get_games_from_archive(archive_url):
    response = requests.get(archive_url, headers={"User-Agent": "chess-bot-project"})
    response.raise_for_status()
    data = response.json()
    pgn_list = []
    for game in data["games"]:
        if game.get("time_class", "") not in TIME_CLASSES:
            continue
        if "pgn" in game:
            pgn_list.append(game["pgn"])
    return pgn_list


def get_all_pgns(username):
    archive_urls = get_archive_urls(username)
    all_pgns = []
    for archive_url in archive_urls:
        all_pgns.extend(get_games_from_archive(archive_url))
    return all_pgns


# ---- Style profile building (condensed from build_profile.py) ----

def analyze_game_with_engine(pgn_text, username, engine):
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None

    headers = game.headers
    is_user_white = headers.get("White", "").lower() == username.lower()

    board = game.board()
    centipawn_losses = []
    blunder_count = 0
    user_move_count = 0

    for move in game.mainline_moves():
        user_is_to_move = (board.turn == chess.WHITE and is_user_white) or (
            board.turn == chess.BLACK and not is_user_white
        )

        if user_is_to_move:
            if user_move_count >= MAX_USER_MOVES_PER_GAME:
                break

            info_before = engine.analyse(board, chess.engine.Limit(depth=ANALYSIS_DEPTH))
            best_eval = info_before["score"].pov(board.turn).score(mate_score=10000)

            board.push(move)
            info_after = engine.analyse(board, chess.engine.Limit(depth=ANALYSIS_DEPTH))
            actual_eval = info_after["score"].pov(not board.turn).score(mate_score=10000)

            if best_eval is not None and actual_eval is not None:
                loss = max(0, best_eval - actual_eval)
                centipawn_losses.append(loss)
                if loss >= BLUNDER_THRESHOLD_CP:
                    blunder_count += 1

            user_move_count += 1
        else:
            board.push(move)

    if user_move_count == 0:
        return None

    avg_cp_loss = sum(centipawn_losses) / len(centipawn_losses) if centipawn_losses else 0
    return {
        "avg_centipawn_loss": avg_cp_loss,
        "blunder_rate": blunder_count / user_move_count,
    }


def build_first_move_distribution(pgns, username):
    white_first_moves = {}
    black_first_moves = {}

    for pgn_text in pgns:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if game is None:
            continue
        headers = game.headers
        is_user_white = headers.get("White", "").lower() == username.lower()
        moves = list(game.mainline_moves())
        if len(moves) == 0:
            continue
        board = game.board()
        if is_user_white:
            san = board.san(moves[0])
            white_first_moves[san] = white_first_moves.get(san, 0) + 1
        else:
            if len(moves) < 2:
                continue
            board.push(moves[0])
            san = board.san(moves[1])
            black_first_moves[san] = black_first_moves.get(san, 0) + 1

    return white_first_moves, black_first_moves


def build_profile_for_user(username, engine, status_placeholder):
    status_placeholder.info(f"Pulling {username}'s games from Chess.com...")
    all_pgns = get_all_pgns(username)

    if len(all_pgns) == 0:
        return None, None, None, None

    status_placeholder.info(
        f"Found {len(all_pgns)} rapid/blitz games. Building your style profile..."
    )

    sample = random.sample(all_pgns, min(SAMPLE_SIZE, len(all_pgns)))
    game_results = []
    for pgn_text in sample:
        result = analyze_game_with_engine(pgn_text, username, engine)
        if result is not None:
            game_results.append(result)

    if not game_results:
        return None, None, None, None

    all_losses = [g["avg_centipawn_loss"] for g in game_results]
    all_blunder_rates = [g["blunder_rate"] for g in game_results]

    profile = {
        "username": username,
        "games_analyzed": len(game_results),
        "avg_centipawn_loss": sum(all_losses) / len(all_losses),
        "avg_blunder_rate": sum(all_blunder_rates) / len(all_blunder_rates),
    }

    white_first_moves, black_first_moves = build_first_move_distribution(all_pgns, username)

    return profile, white_first_moves, black_first_moves, all_pgns


def compute_temperature(profile):
    acpl = profile["avg_centipawn_loss"]
    return max(10, acpl / 1.2)


def get_or_build_profile(username, engine, status_placeholder):
    """
    Checks for a cached profile for this username first. If found, loads
    instantly. Otherwise builds it live (slower, shows progress messages)
    and saves it for next time.
    """
    os.makedirs(PROFILES_DIR, exist_ok=True)
    profile_path = os.path.join(PROFILES_DIR, f"{username.lower()}_profile.json")

    if os.path.exists(profile_path):
        with open(profile_path, "r") as f:
            data = json.load(f)
        return data["profile"], data["white_first_moves"], data["black_first_moves"]

    profile, white_first_moves, black_first_moves, _ = build_profile_for_user(
        username, engine, status_placeholder
    )

    if profile is None:
        return None, None, None

    with open(profile_path, "w") as f:
        json.dump(
            {
                "profile": profile,
                "white_first_moves": white_first_moves,
                "black_first_moves": black_first_moves,
            },
            f,
        )

    return profile, white_first_moves, black_first_moves


def weighted_choice(options_dict):
    choices = list(options_dict.keys())
    weights = list(options_dict.values())
    return random.choices(choices, weights=weights, k=1)[0]


def get_move_history_string(final_board):
    """
    Replays the game from the start to build a readable move list like:
    "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6"
    """
    history_board = chess.Board()
    moves_san = []
    for move in final_board.move_stack:
        moves_san.append(history_board.san(move))
        history_board.push(move)

    formatted = []
    for i in range(0, len(moves_san), 2):
        move_num = i // 2 + 1
        white_move = moves_san[i]
        black_move = moves_san[i + 1] if i + 1 < len(moves_san) else ""
        formatted.append(f"{move_num}. {white_move} {black_move}".strip())
    return "  ".join(formatted)


def get_position_history(final_board):
    """
    Replays the game from the start and returns a snapshot of the board
    at EVERY position (position 0 = starting position, position N = after
    N plies). Used to let the user browse back through past positions
    without altering the actual live game.
    """
    history_board = chess.Board()
    positions = [history_board.copy()]
    for move in final_board.move_stack:
        history_board.push(move)
        positions.append(history_board.copy())
    return positions


def undo_last_round(board, user_plays_white):
    """
    Pops moves off the board until it's the user's turn again -- so one
    click of "Undo" takes back both your last move AND the bot's reply,
    returning control to you rather than leaving it on the bot's turn.
    """
    while board.move_stack:
        board.pop()
        is_users_turn = (board.turn == chess.WHITE and user_plays_white) or (
            board.turn == chess.BLACK and not user_plays_white
        )
        if is_users_turn:
            break


def choose_bot_move(board, engine, temperature):
    info = engine.analyse(board, chess.engine.Limit(time=ENGINE_TIME_LIMIT), multipv=MULTIPV)

    candidates = []
    for entry in info:
        move = entry["pv"][0]
        score = entry["score"].pov(board.turn).score(mate_score=10000)
        if score is not None:
            candidates.append((move, score))

    if not candidates:
        return random.choice(list(board.legal_moves))

    best_score = candidates[0][1]
    weights = []
    for move, score in candidates:
        loss = best_score - score
        weight = math.exp(-loss / temperature)
        weights.append(weight)

    moves = [c[0] for c in candidates]
    return random.choices(moves, weights=weights, k=1)[0]


@st.cache_resource
def get_engine():
    return chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)


# ---- Streamlit app ----

st.set_page_config(page_title="Play Your Own Chess Bot", layout="centered")
st.title("Play Your Own Chess Bot")
st.caption("Enter any Chess.com username to build a bot that plays like them.")

engine = get_engine()

if "stage" not in st.session_state:
    st.session_state.stage = "enter_username"  # enter_username -> choose_color -> playing

# ---- Stage 1: enter username ----
if st.session_state.stage == "enter_username":
    username_input = st.text_input("Chess.com username:")
    if st.button("Build My Bot") and username_input.strip():
        status_placeholder = st.empty()
        with st.spinner("This takes about 30-60 seconds the first time..."):
            profile, white_first_moves, black_first_moves = get_or_build_profile(
                username_input.strip(), engine, status_placeholder
            )
        status_placeholder.empty()

        if profile is None:
            st.error(
                "Couldn't find enough rapid/blitz games for that username. "
                "Double check the spelling, or try a different account."
            )
        else:
            st.session_state.username = username_input.strip()
            st.session_state.profile = profile
            st.session_state.temperature = compute_temperature(profile)
            st.session_state.white_first_moves = white_first_moves
            st.session_state.black_first_moves = black_first_moves
            st.session_state.stage = "choose_color"
            st.rerun()

# ---- Stage 2: choose color ----
elif st.session_state.stage == "choose_color":
    profile = st.session_state.profile
    st.caption(
        f"Bot built from {profile['games_analyzed']} of {st.session_state.username}'s games "
        f"(avg centipawn loss: {profile['avg_centipawn_loss']:.1f}, "
        f"blunder rate: {profile['avg_blunder_rate']*100:.1f}%)"
    )
    color = st.radio("Play as:", ["White", "Black"])
    if st.button("Start Game"):
        st.session_state.board = chess.Board()
        st.session_state.user_plays_white = (color == "White")
        st.session_state.move_count = 0
        st.session_state.stage = "playing"
        st.rerun()

# ---- Stage 3: playing ----
elif st.session_state.stage == "playing":
    board = st.session_state.board
    temperature = st.session_state.temperature
    white_first_moves = st.session_state.white_first_moves
    black_first_moves = st.session_state.black_first_moves

    bot_plays_white = not st.session_state.user_plays_white
    is_bots_turn = (board.turn == chess.WHITE and bot_plays_white) or (
        board.turn == chess.BLACK and not bot_plays_white
    )

    if is_bots_turn and not board.is_game_over():
        if st.session_state.move_count == 0 and bot_plays_white and white_first_moves:
            san = weighted_choice(white_first_moves)
            move = board.parse_san(san)
        elif st.session_state.move_count == 0 and not bot_plays_white and black_first_moves:
            san = weighted_choice(black_first_moves)
            move = board.parse_san(san)
        else:
            move = choose_bot_move(board, engine, temperature)

        board.push(move)
        st.session_state.move_count += 1
        st.session_state.view_index = len(board.move_stack)
        st.rerun()

    move_history = get_move_history_string(board)
    if move_history:
        st.text_area("Move history", move_history, height=80, disabled=True)

    # --- position browsing ---
    positions = get_position_history(board)
    last_index = len(positions) - 1

    if "view_index" not in st.session_state:
        st.session_state.view_index = last_index
    # whenever a new move has been played, snap the view back to live
    if st.session_state.view_index > last_index:
        st.session_state.view_index = last_index

    viewing_live = st.session_state.view_index == last_index

    nav_cols = st.columns(4)
    with nav_cols[0]:
        if st.button("|< Start", disabled=(st.session_state.view_index == 0)):
            st.session_state.view_index = 0
            st.rerun()
    with nav_cols[1]:
        if st.button("< Back", disabled=(st.session_state.view_index == 0)):
            st.session_state.view_index -= 1
            st.rerun()
    with nav_cols[2]:
        if st.button("Forward >", disabled=viewing_live):
            st.session_state.view_index += 1
            st.rerun()
    with nav_cols[3]:
        if st.button("Current >|", disabled=viewing_live):
            st.session_state.view_index = last_index
            st.rerun()

    if not viewing_live:
        st.info(f"Viewing move {st.session_state.view_index} of {last_index} — not the current position.")

    display_board = positions[st.session_state.view_index]
    board_orientation = chess.WHITE if st.session_state.user_plays_white else chess.BLACK
    board_svg = chess.svg.board(board=display_board, size=400, orientation=board_orientation)
    st.image(board_svg, use_container_width=False)

    # only allow actually playing moves / undoing when looking at the
    # live position -- browsing history is view-only
    if not viewing_live:
        st.write("")
        if st.button("New Bot / Restart"):
            st.session_state.stage = "enter_username"
            st.rerun()
    elif board.is_game_over():
        st.success(f"Game over: {board.result()}")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Play Again"):
                st.session_state.stage = "choose_color"
                st.rerun()
        with col2:
            if st.button("Undo Last Round", disabled=(len(board.move_stack) == 0), key="undo_gameover"):
                undo_last_round(board, st.session_state.user_plays_white)
                st.session_state.move_count = len(board.move_stack)
                st.session_state.view_index = len(board.move_stack)
                st.rerun()
    else:
        st.write("Your move — click any option below:")

        legal_moves_san = sorted([board.san(m) for m in board.legal_moves])

        # lay moves out in a grid of buttons instead of a dropdown --
        # avoids the dropdown-opens-upward-and-covers-the-board problem,
        # and it's one click instead of select-then-confirm
        moves_per_row = 6
        for row_start in range(0, len(legal_moves_san), moves_per_row):
            row_moves = legal_moves_san[row_start:row_start + moves_per_row]
            cols = st.columns(moves_per_row)
            for col, san in zip(cols, row_moves):
                with col:
                    # key uses the row_start+index so buttons stay unique
                    # even if (rarely) two legal moves render identically
                    button_key = f"move_{row_start}_{san}"
                    if st.button(san, key=button_key):
                        move = board.parse_san(san)
                        board.push(move)
                        st.session_state.move_count += 1
                        st.session_state.view_index = len(board.move_stack)
                        st.rerun()

        st.write("")  # small spacer
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Undo Last Round", disabled=(len(board.move_stack) == 0), key="undo_live"):
                undo_last_round(board, st.session_state.user_plays_white)
                st.session_state.move_count = len(board.move_stack)
                st.session_state.view_index = len(board.move_stack)
                st.rerun()
        with col2:
            if st.button("New Bot / Restart"):
                st.session_state.stage = "enter_username"
                st.rerun()
