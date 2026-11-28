"""Pure rules for the six-row, seven-column Connect Four board."""

ROWS = 6
COLS = 7
EMPTY_BOARD = "_" * (ROWS * COLS)


def drop_piece(board: str, column: int, player: int) -> tuple[str, int] | None:
    if not 0 <= column < COLS:
        return None
    for row in range(ROWS - 1, -1, -1):
        pos = row * COLS + column
        if board[pos] == "_":
            return board[:pos] + "XO"[player] + board[pos + 1 :], pos
    return None


def winning_cells(board: str, pos: int) -> list[int]:
    """Return all lines completed by the last move, including intersecting lines."""
    origin_row, origin_col = divmod(pos, COLS)
    mark = board[pos]
    if mark == "_":
        return []
    cells: set[int] = set()
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        line = [pos]
        for sign in (-1, 1):
            row, col = origin_row + dr * sign, origin_col + dc * sign
            while 0 <= row < ROWS and 0 <= col < COLS and board[row * COLS + col] == mark:
                line.append(row * COLS + col)
                row += dr * sign
                col += dc * sign
        if len(line) >= 4:
            cells.update(line)
    return sorted(cells)


def choose_bot_column(board: str) -> int | None:
    """Win now if possible, otherwise prefer safe, central moves."""
    order = sorted(range(COLS), key=lambda col: abs(col - COLS // 2))
    legal = [(col, move) for col in order if (move := drop_piece(board, col, 1)) is not None]
    for col, move in legal:
        if winning_cells(*move):
            return col
    for col, (next_board, _) in legal:
        if not any(
            winning_cells(*reply)
            for target in order
            if (reply := drop_piece(next_board, target, 0)) is not None
        ):
            return col
    return legal[0][0] if legal else None
