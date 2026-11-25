"""Durable chat requests and their links to the conversation and model run."""
import json
import re

from harness.core.utils import now_ms


ACTIVE_TURN_STATES = ('queued', 'running')


def initialize_chat_turns(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS chat_turns (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
        user_message_id TEXT NOT NULL, assistant_message_id TEXT, run_id TEXT,
        context TEXT, error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_chat_turns_session ON chat_turns(session_id,created_at DESC);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_turns_active ON chat_turns((1))
        WHERE status IN ('queued','running');
    ''')


class ChatTurns:
    def __init__(self, store, sessions):
        self.store = store
        self.db = store.db
        self.sessions = sessions

    def _view(self, row):
        if row is None:
            return None
        return dict(id=row['id'], sessionId=row['session_id'], status=row['status'],
                    userMessageId=row['user_message_id'], assistantMessageId=row['assistant_message_id'],
                    runId=row['run_id'], error=row['error'],
                    context=json.loads(row['context']) if row['context'] else None,
                    createdAt=row['created_at'], updatedAt=row['updated_at'])

    def get(self, identifier):
        return self._view(self.db.execute('SELECT * FROM chat_turns WHERE id=?', (identifier,)).fetchone())

    def latest(self, session_id):
        return self._view(self.db.execute('''SELECT * FROM chat_turns WHERE session_id=?
            ORDER BY created_at DESC,rowid DESC LIMIT 1''', (session_id,)).fetchone())

    def message_content(self, message_id):
        row = self.db.execute('SELECT content FROM messages WHERE id=?', (message_id,)).fetchone()
        return row[0] if row else None

    def accept(self, identifier, session_id, message, worker_busy):
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identifier):
            raise ValueError('Invalid chat request ID')

        def work():
            existing = self.get(identifier)
            if existing:
                if existing['sessionId'] != session_id or self.message_content(existing['userMessageId']) != message:
                    raise ValueError('Chat request ID already belongs to another message')
                return existing, False
            self.sessions.active(session_id)
            if worker_busy or self.db.execute("SELECT 1 FROM chat_turns WHERE status IN ('queued','running')").fetchone():
                raise ValueError('A chat turn is already running')
            user_message = self.store.add_message('user', message, run_id=identifier, session_id=session_id)
            now = now_ms()
            self.db.execute('''INSERT INTO chat_turns
                (id,session_id,status,user_message_id,created_at,updated_at) VALUES (?,?,'queued',?,?,?)''',
                (identifier, session_id, user_message['id'], now, now))
            return self.get(identifier), True

        return self.store._tx(work)

    def start(self, identifier, context):
        self.db.execute("UPDATE chat_turns SET status='running',context=?,updated_at=? WHERE id=? AND status='queued'",
                        (json.dumps(context), now_ms(), identifier))

    def link_run(self, identifier, run_id):
        self.db.execute('UPDATE chat_turns SET run_id=? WHERE id=?', (run_id, identifier))

    def complete(self, identifier, text):
        def work():
            turn = self.get(identifier)
            if turn['status'] != 'running':
                return
            reply = self.store.add_message('assistant', text, run_id=turn['runId'], session_id=turn['sessionId'])
            self.db.execute("UPDATE chat_turns SET status='succeeded',assistant_message_id=?,updated_at=? WHERE id=?",
                            (reply['id'], now_ms(), identifier))

        self.store._tx(work)

    def stop(self, identifier, status, error):
        self.db.execute("UPDATE chat_turns SET status=?,error=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
                        (status, error, now_ms(), identifier))

    def recover(self):
        self.db.execute("""UPDATE chat_turns SET status='interrupted',error=?,updated_at=?
            WHERE status IN ('queued','running')""",
            ('Server restarted during this reply. Review any proposed actions before sending again.', now_ms()))
