import sqlite3
import json

class Database:
    def __init__(self, db_name="bot_state.db"):
        self.conn = sqlite3.connect(db_name)
        self.cursor = self.conn.cursor()
        self.setup()

    def setup(self):
        # Баг #1: Составной PRIMARY KEY, чтобы message_id из разных каналов не конфликтовали
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id INTEGER,
                chat_id INTEGER,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, chat_id)
            )
        ''')
        # Баг #6: Хранение pending_applications в БД, а не в памяти
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_applications (
                message_id INTEGER PRIMARY KEY,
                author TEXT,
                cover_letter TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()

    def is_processed(self, message_id, chat_id):
        self.cursor.execute(
            'SELECT 1 FROM processed_messages WHERE message_id = ? AND chat_id = ?',
            (message_id, chat_id)
        )
        return self.cursor.fetchone() is not None

    def mark_processed(self, message_id, chat_id):
        self.cursor.execute(
            'INSERT OR IGNORE INTO processed_messages (message_id, chat_id) VALUES (?, ?)',
            (message_id, chat_id)
        )
        self.conn.commit()

    # --- Pending Applications ---

    def save_pending(self, message_id, author, cover_letter):
        self.cursor.execute(
            'INSERT OR REPLACE INTO pending_applications (message_id, author, cover_letter) VALUES (?, ?, ?)',
            (message_id, author, cover_letter)
        )
        self.conn.commit()

    def get_pending(self, message_id):
        self.cursor.execute(
            'SELECT author, cover_letter FROM pending_applications WHERE message_id = ?',
            (message_id,)
        )
        row = self.cursor.fetchone()
        if row:
            return {"author": row[0], "cover_letter": row[1]}
        return None

    def delete_pending(self, message_id):
        self.cursor.execute(
            'DELETE FROM pending_applications WHERE message_id = ?',
            (message_id,)
        )
        self.conn.commit()

db = Database()
