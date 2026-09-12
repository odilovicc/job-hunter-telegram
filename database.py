import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Легаси-значение: раньше отсутствие контакта писалось в БД строкой, а не NULL.
# Оставляем, чтобы старые записи в bot_state.db читались корректно.
LEGACY_NO_CONTACT = "Контакт не найден"


class Database:
    def __init__(self, db_name="bot_state.db"):
        db_path = os.path.join(BASE_DIR, db_name)
        self.conn = sqlite3.connect(db_path)
        # WAL + busy_timeout: защита от "database is locked", если рядом
        # окажется второй процесс (например, забытый старый инстанс бота).
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.cursor = self.conn.cursor()
        self.setup()

    def setup(self):
        # Составной PRIMARY KEY, чтобы message_id из разных каналов не конфликтовали
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id INTEGER,
                chat_id INTEGER,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, chat_id)
            )
        ''')
        # Хранение pending_applications в БД, а не в памяти.
        # PRIMARY KEY составной (message_id, chat_id) — id сообщений у каждого
        # канала своя независимая последовательность, поэтому одного message_id
        # недостаточно: вакансии из разных каналов с одинаковым id перезаписывали
        # друг друга, и кнопка могла отправить письмо не тому HR.
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_applications (
                message_id INTEGER,
                chat_id INTEGER,
                author TEXT,
                cover_letter TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, chat_id)
            )
        ''')
        # Журнал воронки вакансий, прошедших Уровень 1, для /stats: какие оценены
        # Уровнем 2, какой исход у прошедших (отправлено/пропущено/ждёт решения).
        # Уровень 1 сюда не пишется поштучно (отсеивает почти всё, было бы
        # раздувало БД) — для него есть отдельный простой счётчик в counters.
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS vacancy_log (
                message_id INTEGER,
                chat_id INTEGER,
                channel_username TEXT,
                score INTEGER,
                level2_passed INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                decided_at TIMESTAMP,
                PRIMARY KEY (message_id, chat_id)
            )
        ''')
        # Общие счётчики для /stats и алертов (level1_rejected, gemini_fallback и т.п.)
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            )
        ''')
        self.conn.commit()
        self._migrate_legacy_schema()

    def _migrate_legacy_schema(self):
        """Мигрирует таблицы, созданные ДО перехода на Bot API.

        CREATE TABLE IF NOT EXISTS не трогает уже существующую таблицу, даже если
        её схема устарела. Старые версии бота (когда отклики шли через
        Telethon-кнопки в «Избранном») создавали pending_applications с
        PRIMARY KEY только по message_id, без chat_id — простой ALTER TABLE
        ADD COLUMN тут не подходит, потому что нужно поменять и сам PRIMARY KEY.

        pending_applications — это очередь заявок, ожидающих нажатия кнопки
        (живёт минуты, максимум часы). Строки из старой схемы относятся к
        кнопкам, которые физически больше не существуют (старый UI в
        «Избранном» заменён этим ботом), так что переносить их некуда и незачем
        — просто архивируем на всякий случай и создаём таблицу с нужной схемой.
        """
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(pending_applications)")}
        if "chat_id" in cols:
            return

        import logging
        logging.getLogger("vacancy_bot").warning(
            "Обнаружена устаревшая схема pending_applications (без chat_id) — "
            "архивирую в pending_applications_legacy_backup и создаю таблицу заново."
        )
        with self.conn:
            self.conn.execute(
                "ALTER TABLE pending_applications RENAME TO pending_applications_legacy_backup"
            )
            self.conn.execute('''
                CREATE TABLE pending_applications (
                    message_id INTEGER,
                    chat_id INTEGER,
                    author TEXT,
                    cover_letter TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (message_id, chat_id)
                )
            ''')

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

    def save_pending(self, message_id, chat_id, author, cover_letter):
        """author=None означает, что контакт HR в тексте вакансии не найден."""
        self.cursor.execute(
            'INSERT OR REPLACE INTO pending_applications (message_id, chat_id, author, cover_letter) VALUES (?, ?, ?, ?)',
            (message_id, chat_id, author, cover_letter)
        )
        self.conn.commit()

    def _normalize(self, row):
        author = row[0]
        if author == LEGACY_NO_CONTACT:
            author = None
        return {"author": author, "cover_letter": row[1]}

    def get_pending(self, message_id, chat_id):
        self.cursor.execute(
            'SELECT author, cover_letter FROM pending_applications WHERE message_id = ? AND chat_id = ?',
            (message_id, chat_id)
        )
        row = self.cursor.fetchone()
        return self._normalize(row) if row else None

    def claim_pending(self, message_id, chat_id):
        """Атомарно забирает заявку: читает и сразу удаляет её в одной транзакции.

        Нужно против двойной отправки: админ может успеть нажать «Отправить»
        дважды, и два обработчика callback'а параллельно прочитали бы одну и ту
        же запись, отправив HR два резюме. Здесь запись достаётся ровно одному
        вызывающему — второй получит None.

        Если отправка потом упадёт, вызывающий обязан вернуть заявку через
        save_pending(), иначе вакансия будет потеряна.
        """
        with self.conn:  # неявная транзакция + commit/rollback
            cur = self.conn.execute(
                'SELECT author, cover_letter FROM pending_applications WHERE message_id = ? AND chat_id = ?',
                (message_id, chat_id)
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur = self.conn.execute(
                'DELETE FROM pending_applications WHERE message_id = ? AND chat_id = ?',
                (message_id, chat_id)
            )
            if cur.rowcount == 0:
                return None
            return self._normalize(row)

    def delete_pending(self, message_id, chat_id):
        self.cursor.execute(
            'DELETE FROM pending_applications WHERE message_id = ? AND chat_id = ?',
            (message_id, chat_id)
        )
        self.conn.commit()

    # --- Статистика (vacancy_log / counters), для /stats ---

    def log_candidate(self, message_id, chat_id, channel_username, score, passed):
        """Фиксирует вакансию, прошедшую Уровень 1 (была оценена на Уровне 2).
        outcome ставится 'rejected', если не прошла, или 'pending', если прошла
        (дальше обновится на 'sent'/'skipped' через set_vacancy_outcome).
        INSERT OR REPLACE — повторная обработка той же вакансии (например,
        после неудачной доставки отчёта и повторной попытки) просто перезапишет запись.
        """
        outcome = "pending" if passed else "rejected"
        self.cursor.execute(
            '''INSERT OR REPLACE INTO vacancy_log
               (message_id, chat_id, channel_username, score, level2_passed, outcome, created_at)
               VALUES (?, ?, ?, ?, ?, ?, COALESCE(
                   (SELECT created_at FROM vacancy_log WHERE message_id = ? AND chat_id = ?),
                   CURRENT_TIMESTAMP
               ))''',
            (message_id, chat_id, channel_username, score, int(passed), outcome, message_id, chat_id)
        )
        self.conn.commit()

    def set_vacancy_outcome(self, message_id, chat_id, outcome):
        """outcome: 'sent' или 'skipped'. Ничего не делает, если записи нет —
        такое возможно, если vacancy_log был добавлен после того, как заявка
        уже висела в pending_applications из старой версии бота.
        """
        self.cursor.execute(
            'UPDATE vacancy_log SET outcome = ?, decided_at = CURRENT_TIMESTAMP '
            'WHERE message_id = ? AND chat_id = ?',
            (outcome, message_id, chat_id)
        )
        self.conn.commit()

    def increment_counter(self, name, by=1):
        self.cursor.execute(
            'INSERT INTO counters (name, value) VALUES (?, ?) '
            'ON CONFLICT(name) DO UPDATE SET value = value + excluded.value',
            (name, by)
        )
        self.conn.commit()

    def get_counter(self, name):
        self.cursor.execute('SELECT value FROM counters WHERE name = ?', (name,))
        row = self.cursor.fetchone()
        return row[0] if row else 0

    def get_stats(self, recent_limit=8):
        """Собирает агрегированную статистику для /stats."""
        c = self.conn.execute('SELECT COUNT(*) FROM vacancy_log')
        total_candidates = c.fetchone()[0]

        c = self.conn.execute('SELECT COUNT(*) FROM vacancy_log WHERE level2_passed = 1')
        passed_level2 = c.fetchone()[0]

        c = self.conn.execute('SELECT COUNT(*) FROM vacancy_log WHERE level2_passed = 0')
        rejected_level2 = c.fetchone()[0]

        c = self.conn.execute(
            "SELECT outcome, COUNT(*) FROM vacancy_log WHERE level2_passed = 1 GROUP BY outcome"
        )
        outcome_counts = {"sent": 0, "skipped": 0, "pending": 0}
        for outcome, cnt in c.fetchall():
            outcome_counts[outcome] = cnt

        c = self.conn.execute('SELECT AVG(score) FROM vacancy_log WHERE level2_passed = 1')
        avg_score_row = c.fetchone()[0]
        avg_score = round(avg_score_row, 1) if avg_score_row is not None else None

        c = self.conn.execute(
            "SELECT COALESCE(channel_username, '?'), COUNT(*) FROM vacancy_log "
            "WHERE level2_passed = 1 GROUP BY channel_username ORDER BY COUNT(*) DESC LIMIT 3"
        )
        top_channels = c.fetchall()

        c = self.conn.execute(
            'SELECT message_id, chat_id, channel_username, score, created_at FROM vacancy_log '
            'WHERE level2_passed = 0 ORDER BY created_at DESC LIMIT ?',
            (recent_limit,)
        )
        recent_rejected = [
            {"message_id": r[0], "chat_id": r[1], "channel_username": r[2], "score": r[3], "created_at": r[4]}
            for r in c.fetchall()
        ]

        return {
            "level1_rejected": self.get_counter("level1_rejected"),
            "total_candidates": total_candidates,
            "passed_level2": passed_level2,
            "rejected_level2": rejected_level2,
            "sent": outcome_counts["sent"],
            "skipped": outcome_counts["skipped"],
            "awaiting_decision": outcome_counts["pending"],
            "avg_score": avg_score,
            "top_channels": top_channels,
            "recent_rejected": recent_rejected,
            "gemini_fallback": self.get_counter("gemini_fallback"),
        }


db = Database()
