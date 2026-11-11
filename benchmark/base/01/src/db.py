import sqlite3


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> tuple[object, ...] | None:
    cursor = conn.cursor()
    cursor.execute("SELECT id, email FROM users WHERE id = ?", (user_id,))
    return cursor.fetchone()
