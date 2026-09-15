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
import re
import requests
import shutil
import cairosvg
from PIL import Image
from streamlit_image_coordinates import streamlit_image_coordinates


FEEDBACK_FORM_URL = "https://forms.gle/e1PbTnmRrAUj15UV8"


TIME_CLASSES = {"rapid", "blitz"}
MULTIPV = 5
GAMEPLAY_DEPTH = 8  # fixed depth for live bot moves -- more predictable than a wall-clock time limit on shared/slower cloud hardware

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

class UsernameNotFoundError(Exception):
    """Raised specifically when Chess.com has no account with this username."""
    pass


def get_archive_urls(username):
    url = f"https://api.chess.com/pub/player/{username}/games/archives"
    response = requests.get(url, headers={"User-Agent": "chess-bot-project"})
    if response.status_code == 404:
        raise UsernameNotFoundError(username)
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
    """
    Raises UsernameNotFoundError if the username doesn't exist on Chess.com.
    Returns an empty list if the username exists but some other request
    problem occurs, so the caller can distinguish the two cases.
    """
    archive_urls = get_archive_urls(username)  # lets UsernameNotFoundError bubble up

    all_pgns = []
    for archive_url in archive_urls:
        try:
            all_pgns.extend(get_games_from_archive(archive_url))
        except requests.exceptions.RequestException:
            continue  # skip a bad month rather than failing the whole pull
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

    try:
        all_pgns = get_all_pgns(username)
    except UsernameNotFoundError:
        return None, None, None, None, "username_not_found"

    if len(all_pgns) == 0:
        return None, None, None, None, "no_games"

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
        return None, None, None, None, "no_games"

    all_losses = [g["avg_centipawn_loss"] for g in game_results]
    all_blunder_rates = [g["blunder_rate"] for g in game_results]

    profile = {
        "username": username,
        "games_analyzed": len(game_results),
        "avg_centipawn_loss": sum(all_losses) / len(all_losses),
        "avg_blunder_rate": sum(all_blunder_rates) / len(all_blunder_rates),
    }

    white_first_moves, black_first_moves = build_first_move_distribution(all_pgns, username)

    return profile, white_first_moves, black_first_moves, all_pgns, None


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
        return data["profile"], data["white_first_moves"], data["black_first_moves"], None

    profile, white_first_moves, black_first_moves, _, error_reason = build_profile_for_user(
        username, engine, status_placeholder
    )

    if profile is None:
        return None, None, None, error_reason

    with open(profile_path, "w") as f:
        json.dump(
            {
                "profile": profile,
                "white_first_moves": white_first_moves,
                "black_first_moves": black_first_moves,
            },
            f,
        )

    return profile, white_first_moves, black_first_moves, None


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


def get_board_grid_geometry(svg_text, size):
    """
    With coordinate labels enabled, python-chess's SVG board doesn't
    necessarily fill the full image edge-to-edge -- some space may be
    used for the file/rank labels, and exactly how much can depend on
    the library version. Rather than assume a fixed margin (which is
    what caused the earlier dot-alignment bug), this parses the actual
    <rect> elements python-chess draws for the checkered squares and
    measures their real pixel position and size directly -- so our
    click/marker math is always correct for whatever is really rendered.
    """
    rect_tag_pattern = re.compile(r"<rect\b([^>]*)/?>")
    attr_pattern = re.compile(r'([\w:-]+)="([^"]*)"')

    candidates = []
    for rect_match in rect_tag_pattern.finditer(svg_text):
        attrs = dict(attr_pattern.findall(rect_match.group(1)))
        try:
            x = float(attrs.get("x", "nan"))
            y = float(attrs.get("y", "nan"))
            w = float(attrs.get("width", "nan"))
            h = float(attrs.get("height", "nan"))
        except ValueError:
            continue
        if any(math.isnan(v) for v in (x, y, w, h)):
            continue
        # a real chess square is roughly square-shaped and clearly
        # smaller than the whole image (excludes any full-canvas
        # background rect that happens to also be size x size)
        if abs(w - h) < 0.01 and 0 < w < size / 2:
            candidates.append((round(x, 2), round(y, 2), w))

    if not candidates:
        # fallback: assume the grid fills the image with no margin
        return 0.0, 0.0, size / 8

    square_size = candidates[0][2]
    origin_x = min(c[0] for c in candidates)
    origin_y = min(c[1] for c in candidates)
    return origin_x, origin_y, square_size


@st.cache_resource
def get_grid_geometry():
    """
    The grid's pixel position never changes between renders (same size,
    same coordinate settings every time) -- only the pieces move -- so
    we measure it once on a fresh board and reuse it everywhere.
    """
    sample_svg = chess.svg.board(board=chess.Board(), size=400, coordinates=True)
    return get_board_grid_geometry(sample_svg, 400)


def render_board_image(board, orientation, selected_square=None):
    """
    Renders the board as a PNG image (via cairosvg converting python-chess's
    SVG output) so it can be shown with streamlit_image_coordinates, which
    needs a raster image to detect click positions on.

    When a square is selected, draws a small dot on every square that piece
    can legally move to for a quiet move, or a ring around the square for
    a capture (matching Lichess's convention) -- drawn manually rather than
    using python-chess's built-in "squares" highlight, since that renders
    captures as an X and doesn't distinguish capture vs quiet moves.

    coordinate labels are enabled (coordinates=True) since you want them
    visible -- the grid position is measured at runtime via
    get_grid_geometry() rather than assumed, so the dots/rings and click
    detection stay correctly aligned regardless of how much space the
    labels actually take up.

    If the side to move is in check, the king's square gets a red tint,
    using python-chess's built-in "check" highlighting. The most recently
    played move is also highlighted, using python-chess's "lastmove".
    """
    fill = {}
    if selected_square is not None:
        fill[selected_square] = "#aaa23b"

    check_square = board.king(board.turn) if board.is_check() else None
    last_move = board.peek() if board.move_stack else None

    svg_text = chess.svg.board(
        board=board,
        size=400,
        orientation=orientation,
        fill=fill,
        check=check_square,
        lastmove=last_move,
        coordinates=True,
    )

    if selected_square is not None:
        origin_x, origin_y, square_size = get_grid_geometry()
        markers_svg = ""
        for move in board.legal_moves:
            if move.from_square != selected_square:
                continue
            dest_square = move.to_square
            is_capture = board.is_capture(move)

            file_idx = chess.square_file(dest_square)
            rank_idx = chess.square_rank(dest_square)
            if orientation == chess.WHITE:
                col = file_idx
                row = 7 - rank_idx
            else:
                col = 7 - file_idx
                row = rank_idx
            center_x = origin_x + col * square_size + square_size / 2
            center_y = origin_y + row * square_size + square_size / 2

            if is_capture:
                # ring around the edge of the square, so the piece being
                # captured is still visible underneath -- matches Lichess
                markers_svg += (
                    f'<circle cx="{center_x}" cy="{center_y}" r="{square_size / 2 - 3}" '
                    f'fill="none" stroke="rgba(0,0,0,0.4)" stroke-width="4" />'
                )
            else:
                markers_svg += (
                    f'<circle cx="{center_x}" cy="{center_y}" r="8" '
                    f'fill="rgba(0,0,0,0.35)" />'
                )
        svg_text = svg_text.replace("</svg>", markers_svg + "</svg>")

    png_bytes = cairosvg.svg2png(bytestring=svg_text.encode("utf-8"), output_width=400, output_height=400)
    return Image.open(io.BytesIO(png_bytes))


def square_from_click(x, y, orientation):
    """
    Converts a pixel click position on the 400x400 board image into the
    actual chess square underneath it, accounting for whether the board
    is currently drawn from White's or Black's perspective. Uses the
    measured grid geometry (get_grid_geometry) rather than assuming the
    grid starts at pixel (0,0), since coordinate labels may offset it.
    """
    origin_x, origin_y, square_size = get_grid_geometry()
    col = int((x - origin_x) // square_size)
    row = int((y - origin_y) // square_size)
    col = max(0, min(7, col))
    row = max(0, min(7, row))

    if orientation == chess.WHITE:
        file_idx = col
        rank_idx = 7 - row
    else:
        file_idx = 7 - col
        rank_idx = row

    return chess.square(file_idx, rank_idx)


def choose_bot_move(board, engine, temperature):
    info = engine.analyse(board, chess.engine.Limit(depth=GAMEPLAY_DEPTH), multipv=MULTIPV)

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
st.link_button("Report a bug / suggestion", FEEDBACK_FORM_URL)

engine = get_engine()

if "stage" not in st.session_state:
    st.session_state.stage = "enter_username"  # enter_username -> choose_color -> playing

# ---- Stage 1: enter username ----
if st.session_state.stage == "enter_username":
    username_input = st.text_input("Chess.com username:")
    if st.button("Build My Bot") and username_input.strip():
        status_placeholder = st.empty()
        with st.spinner("This takes about 30-60 seconds the first time..."):
            profile, white_first_moves, black_first_moves, error_reason = get_or_build_profile(
                username_input.strip(), engine, status_placeholder
            )
        status_placeholder.empty()

        if profile is None:
            if error_reason == "username_not_found":
                st.error(
                    f"'{username_input.strip()}' isn't a Chess.com username. "
                    "Double check the spelling and try again."
                )
            else:
                st.error(
                    "That username exists, but doesn't have enough rapid/blitz games "
                    "to build a profile from. Try a different account."
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

    if "selected_square" not in st.session_state:
        st.session_state.selected_square = None
    if "last_click_processed" not in st.session_state:
        st.session_state.last_click_processed = None

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
        if "view_index" in st.session_state:
            del st.session_state["view_index"]
        st.session_state.selected_square = None
        st.rerun()

    move_history = get_move_history_string(board)
    if move_history:
        st.text_area("Move history", move_history, height=80, disabled=True)

    # --- position browsing (one ply/half-move at a time) ---
    positions = get_position_history(board)
    last_index = len(positions) - 1

    if "view_index" not in st.session_state:
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

    if viewing_live and not board.is_game_over():
        # click-to-move: render as a clickable image instead of a static one
        board_image = render_board_image(display_board, board_orientation, st.session_state.selected_square)
        click_result = streamlit_image_coordinates(board_image, key="board_click")

        if click_result is not None and click_result != st.session_state.last_click_processed:
            st.session_state.last_click_processed = click_result
            clicked_square = square_from_click(click_result["x"], click_result["y"], board_orientation)
            piece_at_click = board.piece_at(clicked_square)
            side_to_move = board.turn

            if st.session_state.selected_square is None:
                # first click: only select if there's actually a piece
                # belonging to whoever's turn it is
                if piece_at_click is not None and piece_at_click.color == side_to_move:
                    st.session_state.selected_square = clicked_square
                    st.rerun()
            else:
                if clicked_square == st.session_state.selected_square:
                    # clicking the same square again deselects it
                    st.session_state.selected_square = None
                    st.rerun()
                else:
                    from_sq = st.session_state.selected_square
                    move = chess.Move(from_sq, clicked_square)

                    # handle pawn promotion -- defaults to queen for now
                    moving_piece = board.piece_at(from_sq)
                    if moving_piece is not None and moving_piece.piece_type == chess.PAWN:
                        promo_rank = 7 if moving_piece.color == chess.WHITE else 0
                        if chess.square_rank(clicked_square) == promo_rank:
                            move = chess.Move(from_sq, clicked_square, promotion=chess.QUEEN)

                    if move in board.legal_moves:
                        board.push(move)
                        st.session_state.move_count += 1
                        if "view_index" in st.session_state:
                            del st.session_state["view_index"]
                        st.session_state.selected_square = None
                        st.rerun()
                    elif piece_at_click is not None and piece_at_click.color == side_to_move:
                        # clicked a different one of your own pieces -- reselect
                        st.session_state.selected_square = clicked_square
                        st.rerun()
                    else:
                        st.session_state.selected_square = None
                        st.warning("That's not a legal move.")
                        st.rerun()
    else:
        # browsing history or game over -- static, non-clickable image
        static_check_square = display_board.king(display_board.turn) if display_board.is_check() else None
        static_last_move = display_board.peek() if display_board.move_stack else None
        board_svg = chess.svg.board(
            board=display_board,
            size=400,
            orientation=board_orientation,
            check=static_check_square,
            lastmove=static_last_move,
            coordinates=True,
        )
        st.image(board_svg, use_container_width=False)

    if board.is_game_over() and viewing_live:
        st.success(f"Game over: {board.result()}")
        if st.button("Play Again"):
            st.session_state.stage = "choose_color"
            st.rerun()
    elif not viewing_live:
        st.write("")
        if st.button("New Bot / Restart"):
            st.session_state.stage = "enter_username"
            st.rerun()
    else:
        st.caption("Click a piece, then click where it should move.")
        if st.button("New Bot / Restart"):
            st.session_state.stage = "enter_username"
            st.rerun()
