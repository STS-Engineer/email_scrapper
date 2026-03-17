import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()
# --- PostgreSQL Configuration ---
DB_HOST = os.getenv('DB_HOST')
DB_PORT = os.getenv('DB_PORT', '5432') # Defaults to 5432 if not found
DB_NAME = os.getenv('DB_NAME')
DB_USER = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')


def get_db_connection():
    """Creates and returns a connection to the PostgreSQL database."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )

def initialize_database():
    """Creates the PostgreSQL tables optimized for AI reporting and a Web UI."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. The Emails Table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS Emails (
        message_id TEXT PRIMARY KEY,
        conversation_id TEXT,
        search_domain TEXT,
        sender_email TEXT,
        recipient_emails TEXT,
        received_date TEXT,
        subject TEXT,
        body_text TEXT,
        email_type TEXT, -- 'SHORT' or 'LONG'
        ai_summary TEXT  
    )
    ''')

    # 2. The Attachments Table
    # Note: PostgreSQL uses SERIAL for auto-incrementing IDs
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS Attachments (
        attachment_id SERIAL PRIMARY KEY,
        message_id TEXT REFERENCES Emails(message_id),
        file_name TEXT,
        file_extension TEXT,
        local_file_path TEXT,
        extracted_content TEXT
    )
    ''')

    conn.commit()
    cursor.close()
    conn.close()
    print("✅ PostgreSQL database tables initialized successfully.")

def insert_email(msg_id, conv_id, domain, sender, recipients, date, subject, body, email_type, summary=""):
    """Inserts an email, using ON CONFLICT to skip if the message_id already exists."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Note: PostgreSQL uses %s for placeholders, and ON CONFLICT DO NOTHING to prevent duplicates
    cursor.execute('''
    INSERT INTO Emails 
    (message_id, conversation_id, search_domain, sender_email, recipient_emails, received_date, subject, body_text, email_type, ai_summary)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (message_id) DO NOTHING
    ''', (msg_id, conv_id, domain, sender, recipients, date, subject, body, email_type, summary))
    
    conn.commit()
    cursor.close()
    conn.close()

def insert_attachment(msg_id, file_name, file_extension, local_path):
    """Records the downloaded file's metadata for the UI."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
    INSERT INTO Attachments (message_id, file_name, file_extension, local_file_path, extracted_content)
    VALUES (%s, %s, %s, %s, %s)
    ''', (msg_id, file_name, file_extension, local_path, ""))

    conn.commit()
    cursor.close()
    conn.close()


def email_exists(msg_id):
    """Checks if the email is already safely in the database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT 1 FROM Emails WHERE message_id = %s", (msg_id,))
    exists = cursor.fetchone() is not None
    
    cursor.close()
    conn.close()
    return exists

if __name__ == "__main__":
    # Run this file directly to build your tables in pgAdmin/Postgres
    initialize_database()