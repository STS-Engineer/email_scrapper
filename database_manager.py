import os
from contextlib import contextmanager
from threading import Lock

import psycopg2
from psycopg2 import pool
from psycopg2.extensions import STATUS_READY
from dotenv import load_dotenv

load_dotenv()

# --- PostgreSQL Configuration ---
DB_HOST = os.getenv('DB_HOST')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME')
DB_USER = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')

MIN_POOL_CONNECTIONS = 1
MAX_POOL_CONNECTIONS = 20

_connection_pool = None
_pool_lock = Lock()


def _create_connection_pool():
    """Builds the shared PostgreSQL connection pool."""
    return pool.SimpleConnectionPool(
        minconn=MIN_POOL_CONNECTIONS,
        maxconn=MAX_POOL_CONNECTIONS,
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def _get_connection_pool():
    """Initializes the connection pool lazily."""
    global _connection_pool

    if _connection_pool is None:
        with _pool_lock:
            if _connection_pool is None:
                _connection_pool = _create_connection_pool()

    return _connection_pool


def _ping_connection(conn):
    """Runs a lightweight query to confirm the pooled connection is still alive."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()

    if conn.status != STATUS_READY:
        conn.rollback()


def _acquire_healthy_connection():
    """Borrows a connection from the pool and replaces stale sockets if needed."""
    db_pool = _get_connection_pool()
    conn = db_pool.getconn()

    try:
        _ping_connection(conn)
        return db_pool, conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        try:
            db_pool.putconn(conn, close=True)
        except Exception:
            pass

        close_db_pool()
        db_pool = _get_connection_pool()
        conn = db_pool.getconn()

        try:
            _ping_connection(conn)
        except Exception:
            try:
                db_pool.putconn(conn, close=True)
            except Exception:
                pass
            raise

        return db_pool, conn


@contextmanager
def get_db_connection():
    """Yields a pooled PostgreSQL connection and always returns it."""
    db_pool, conn = _acquire_healthy_connection()
    discard_connection = conn.closed != 0

    try:
        yield conn
    finally:
        if not discard_connection:
            try:
                if conn.status != STATUS_READY:
                    conn.rollback()
            except psycopg2.Error:
                discard_connection = True

        db_pool.putconn(conn, close=discard_connection)


def close_db_pool():
    """Closes all pooled PostgreSQL connections."""
    global _connection_pool

    with _pool_lock:
        if _connection_pool is not None:
            _connection_pool.closeall()
            _connection_pool = None


def initialize_database():
    """Creates the PostgreSQL tables used by the scraper."""
    with get_db_connection() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS Emails (
                        message_id TEXT PRIMARY KEY,
                        conversation_id TEXT,
                        search_domain TEXT,
                        sender_email TEXT,
                        recipient_emails TEXT,
                        received_date TEXT,
                        subject TEXT,
                        body_text TEXT,
                        email_type TEXT,
                        ai_summary TEXT
                    )
                    '''
                )

                cursor.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS Attachments (
                        attachment_id SERIAL PRIMARY KEY,
                        message_id TEXT REFERENCES Emails(message_id),
                        file_name TEXT,
                        file_extension TEXT,
                        local_file_path TEXT,
                        extracted_content TEXT
                    )
                    '''
                )

                cursor.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS TargetMailboxes (
                        email_address TEXT PRIMARY KEY,
                        is_active BOOLEAN DEFAULT TRUE
                    )
                    '''
                )

                cursor.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS Conversations (
                        conversation_id VARCHAR NOT NULL,
                        search_domain VARCHAR NOT NULL,
                        started_date TIMESTAMP WITH TIME ZONE,
                        last_updated_date TIMESTAMP WITH TIME ZONE,
                        email_count INTEGER,
                        summary TEXT,
                        PRIMARY KEY (conversation_id, search_domain)
                    )
                    '''
                )

                cursor.execute(
                    '''
                    ALTER TABLE Conversations
                    ADD COLUMN IF NOT EXISTS ai_analysis JSONB
                    '''
                )

                cursor.execute(
                    '''
                    ALTER TABLE Conversations
                    ADD COLUMN IF NOT EXISTS involved_employees JSONB NOT NULL DEFAULT '[]'::jsonb
                    '''
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    print("PostgreSQL database tables initialized successfully.")


def insert_email(msg_id, conv_id, domain, sender, recipients, date, subject, body, email_type, summary=""):
    """Inserts an email and skips duplicates by message_id."""
    with get_db_connection() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    '''
                    INSERT INTO Emails
                    (message_id, conversation_id, search_domain, sender_email, recipient_emails, received_date, subject, body_text, email_type, ai_summary)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (message_id) DO NOTHING
                    ''',
                    (msg_id, conv_id, domain, sender, recipients, date, subject, body, email_type, summary),
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise


def insert_attachment(msg_id, file_name, file_extension, local_path):
    """Records the downloaded file metadata for the UI."""
    with get_db_connection() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    '''
                    INSERT INTO Attachments (message_id, file_name, file_extension, local_file_path, extracted_content)
                    VALUES (%s, %s, %s, %s, %s)
                    ''',
                    (msg_id, file_name, file_extension, local_path, ""),
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise


def email_exists(msg_id):
    """Checks if the email is already safely in the database."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM Emails WHERE message_id = %s", (msg_id,))
            return cursor.fetchone() is not None


def get_active_mailboxes():
    """Returns active mailbox addresses in alphabetical order."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                SELECT email_address
                FROM TargetMailboxes
                WHERE is_active = TRUE
                ORDER BY email_address
                '''
            )
            return [row[0] for row in cursor.fetchall()]


if __name__ == "__main__":
    initialize_database()
    close_db_pool()
