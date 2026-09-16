# Project overview

This is a Streamlit chess app that builds a bot from a Chess.com user's rapid and blitz game history.

## Run

Use the **Start application** workflow. It runs:

`streamlit run app.py --server.address 0.0.0.0 --server.port 5000 --server.headless true`

## Dependencies

- Python packages are listed in `requirements.txt`.
- The Stockfish system package is required by the chess engine integration.
- The app uses Chess.com's public API and does not require API credentials.
