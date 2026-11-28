"""Shared Telegram board for group and solo Connect Four games."""

from collections.abc import Collection

from app.features.games.connect_four import COLS


def board_lines(board: str, winning: Collection[int]) -> list[str]:
    marks = {"X": "🔴", "O": "🔵", "_": "⚪"}
    # Keep the numbered columns left to right in Persian Telegram clients.
    lines = ["", "\u200e1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"]
    for start in range(0, len(board), COLS):
        lines.append(
            "\u200e"
            + "".join(
                "🟡" if pos in winning else marks[board[pos]] for pos in range(start, start + COLS)
            )
        )
    if winning:
        lines.append("🟡 مهره‌های خط برنده")
    return lines
