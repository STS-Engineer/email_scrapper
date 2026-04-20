import contextlib
import json
import os
import re

import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv
from openai import OpenAI

from database_manager import close_db_pool, get_db_connection, initialize_database

load_dotenv()

OPENAI_ROLLUP_MODEL = os.getenv("OPENAI_ROLLUP_MODEL", "gpt-4o-mini")
DIRECTORY_DB_URL = os.environ.get("DIRECTORY_DATABASE_URL")
ISO_TIMESTAMP_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MANAGER_ROLE_PATTERN = re.compile(r"\bmanager\b", re.IGNORECASE)
VALID_PRIORITY_LEVELS = {3, 5, 7, 9}

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def safe_print_str(text):
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def safe_print(message):
    print(safe_print_str(message))


@contextlib.contextmanager
def get_directory_connection():
    if not DIRECTORY_DB_URL:
        raise RuntimeError("DIRECTORY_DATABASE_URL is not configured.")

    conn = psycopg2.connect(DIRECTORY_DB_URL)
    try:
        yield conn
    finally:
        conn.close()


def load_directory_members():
    query = """
        SELECT email, display_name, job_title, department, site
        FROM company_members
        WHERE email ILIKE '%@avocarbon.com'
          AND COALESCE(email, '') <> ''
        ORDER BY email
    """

    with get_directory_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)
            rows = cursor.fetchall()

    members = []
    member_lookup = {}

    for row in rows:
        email = (row[0] or "").strip().lower()
        if not email or email in member_lookup:
            continue

        member = {
            "email": email,
            "name": (row[1] or "").strip(),
            "role": (row[2] or "").strip(),
            "department": (row[3] or "").strip(),
            "plant": (row[4] or "").strip(),
        }
        members.append(member)
        member_lookup[email] = member

    return members, member_lookup


def fetch_candidate_conversations():
    query = """
        SELECT
            conversation_id,
            search_domain,
            MIN(received_date::timestamptz) AS started_date,
            MAX(received_date::timestamptz) AS last_updated_date,
            COUNT(message_id) AS email_count,
            STRING_AGG(ai_summary, ' | ' ORDER BY received_date::timestamptz ASC) AS full_thread,
            STRING_AGG(
                COALESCE(sender_email, '') || ' ' || COALESCE(recipient_emails, ''),
                ' | ' ORDER BY received_date::timestamptz ASC
            ) AS participant_text
        FROM Emails
        WHERE email_type = 'LONG'
          AND COALESCE(conversation_id, '') <> ''
          AND COALESCE(search_domain, '') <> ''
          AND received_date ~ %s
        GROUP BY conversation_id, search_domain
        HAVING COUNT(message_id) > 1
        ORDER BY MAX(received_date::timestamptz) DESC
    """

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, (ISO_TIMESTAMP_PATTERN,))
            rows = cursor.fetchall()

    return [
        {
            "conversation_id": row[0],
            "search_domain": row[1],
            "started_date": row[2],
            "last_updated_date": row[3],
            "email_count": row[4],
            "full_thread": row[5] or "",
            "participant_text": row[6] or "",
        }
        for row in rows
    ]


def extract_involved_employees(full_thread, directory_lookup):
    matched_employees = []
    seen_emails = set()

    for email in EMAIL_PATTERN.findall(full_thread or ""):
        normalized_email = email.strip().lower()
        if normalized_email in seen_emails:
            continue
        seen_emails.add(normalized_email)

        member = directory_lookup.get(normalized_email)
        if member:
            matched_employees.append(member)

    return matched_employees


def strip_json_fence(text):
    fenced_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced_match:
        return fenced_match.group(1)
    return text.strip()


def parse_priority_level(value, field_name):
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit():
            value = int(value)

    if not isinstance(value, int):
        raise ValueError(
            f"OpenAI response is missing a valid integer '{field_name}'. "
            "It must be one of 3, 5, 7, or 9."
        )

    if value not in VALID_PRIORITY_LEVELS:
        allowed_levels = ", ".join(str(level) for level in sorted(VALID_PRIORITY_LEVELS))
        raise ValueError(f"{field_name} must be one of {allowed_levels}.")

    return value


def has_manager_involved(involved_employees):
    return any(
        MANAGER_ROLE_PATTERN.search((employee.get("role") or ""))
        for employee in (involved_employees or [])
    )


def parse_judge_payload(raw_content, involved_employees=None):
    payload = json.loads(strip_json_fence(raw_content))

    required_keys = {
        "is_important",
        "ai_summary",
        "type_of_issue",
        "urgency_level",
        "detection_level",
    }
    missing_keys = required_keys - payload.keys()
    if missing_keys:
        missing_list = ", ".join(sorted(missing_keys))
        raise ValueError(f"OpenAI response is missing required keys: {missing_list}")

    is_important = payload.get("is_important")
    if isinstance(is_important, str):
        normalized = is_important.strip().lower()
        if normalized in {"true", "false"}:
            is_important = normalized == "true"

    if not isinstance(is_important, bool):
        raise ValueError("OpenAI response is missing a valid boolean 'is_important'.")

    ai_summary = str(payload.get("ai_summary", "")).strip()
    type_of_issue = str(payload.get("type_of_issue", "")).strip()
    urgency_level = parse_priority_level(payload.get("urgency_level"), "urgency_level")
    detection_level = parse_priority_level(payload.get("detection_level"), "detection_level")

    if is_important and not ai_summary:
        raise ValueError("OpenAI marked the conversation important but did not return an ai_summary.")

    if has_manager_involved(involved_employees) and detection_level < 5:
        raise ValueError(
            "detection_level must be at least 5 when any involved employee role contains 'Manager'."
        )

    return {
        "is_important": is_important,
        "ai_summary": ai_summary,
        "type_of_issue": type_of_issue,
        "urgency_level": urgency_level,
        "detection_level": detection_level,
    }


def judge_conversation(search_domain, full_thread, involved_employees):
    thread_text = full_thread.strip()
    if not thread_text:
        raise ValueError("Conversation thread is empty.")

    response = client.chat.completions.create(
        model=OPENAI_ROLLUP_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a supply chain executive assistant. "
                    "Return only a JSON object with exactly these keys: "
                    "'is_important', 'ai_summary', 'type_of_issue', 'urgency_level', and 'detection_level'. "
                    "If the thread contains a routine interaction, approval, or general FYI, set 'is_important' to false. "
                    "If the thread contains a conflict, delay, escalation, urgent request, or problem, set 'is_important' to true "
                    "and provide a concise 2-sentence summary in 'ai_summary'. "
                    "You MUST return urgency_level as an integer (3, 5, 7, or 9). "
                    "DO NOT return strings or words like High, Medium, or Low. "
                    "Always return urgency_level as one of these integers: "
                    "3: Routine follow-up or low operational concern; "
                    "5: Risk of supply chain issue, discussions about lack of competitiveness, "
                    "talk about a big quality issue (Even if potential financial impact or large quantities are discussed); "
                    "7: Severe escalation with major operational risk but no confirmed debit note or realized hard business loss yet; "
                    "9: Debit note, actual business loss. "
                    "Do NOT assign a 9 unless an actual debit note has been officially issued or hard business loss has already occurred. "
                    "Potential costs are not a 9. "
                    "Always return 'detection_level' as one of these integers based on the highest relevant level involved: "
                    "3: Supply chain / Quality level involved; "
                    "5: Department managers involved; "
                    "7: Plant managers involved; "
                    "9: Central purchasing involved. "
                    "Deduce 'detection_level' from titles or context of the people discussing the issue. "
                    "If ANY person involved in the thread has the word Manager in their role "
                    "(including Deputy Manager or Senior Manager), you MUST assign a minimum detection_level of 5, "
                    "regardless of the context. "
                    "If the level is unclear, default to 3. "
                    "For routine threads, 'ai_summary' may be empty."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Search domain: {search_domain}\n\n"
                    "Involved employees JSON:\n"
                    f"{json.dumps(involved_employees, ensure_ascii=False)}\n\n"
                    "Thread log:\n"
                    f"{thread_text}"
                ),
            },
        ],
        max_tokens=220,
        temperature=0.1,
    )

    content = response.choices[0].message.content or ""
    return parse_judge_payload(content, involved_employees)


def upsert_conversation(conversation, ai_analysis, involved_employees):
    query = """
        INSERT INTO Conversations (
            conversation_id,
            search_domain,
            started_date,
            last_updated_date,
            email_count,
            summary,
            ai_analysis,
            involved_employees
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (conversation_id, search_domain) DO UPDATE
        SET
            started_date = EXCLUDED.started_date,
            last_updated_date = EXCLUDED.last_updated_date,
            email_count = EXCLUDED.email_count,
            summary = EXCLUDED.summary,
            ai_analysis = EXCLUDED.ai_analysis,
            involved_employees = EXCLUDED.involved_employees
    """

    with get_db_connection() as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    query,
                    (
                        conversation["conversation_id"],
                        conversation["search_domain"],
                        conversation["started_date"],
                        conversation["last_updated_date"],
                        conversation["email_count"],
                        ai_analysis["ai_summary"],
                        Json(ai_analysis),
                        Json(involved_employees),
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def run_rollup():
    initialize_database()

    directory_members, directory_lookup = load_directory_members()
    safe_print(
        f"Loaded {len(directory_members)} Avocarbon directory members from the separate directory database."
    )

    candidates = fetch_candidate_conversations()
    if not candidates:
        safe_print("No eligible LONG multi-email conversations were found.")
        return

    safe_print(f"Evaluating {len(candidates)} conversation rollups...")

    inserted_count = 0
    skipped_count = 0
    failed_count = 0

    for conversation in candidates:
        conversation_id = conversation["conversation_id"]
        search_domain = conversation["search_domain"]
        search_text = f"{conversation['participant_text']} {conversation['full_thread']}".strip()
        involved_employees = extract_involved_employees(search_text, directory_lookup)

        try:
            ai_analysis = judge_conversation(
                search_domain,
                conversation["full_thread"],
                involved_employees,
            )
        except Exception as exc:
            failed_count += 1
            safe_print(
                "Failed to judge conversation "
                f"{conversation_id} / {search_domain}: {exc}"
            )
            continue

        if not ai_analysis["is_important"]:
            skipped_count += 1
            safe_print(
                "Skipping routine conversation "
                f"{conversation_id} / {search_domain}"
            )
            continue

        try:
            upsert_conversation(conversation, ai_analysis, involved_employees)
            inserted_count += 1
            safe_print(
                "Upserted important conversation "
                f"{conversation_id} / {search_domain}"
            )
        except Exception as exc:
            failed_count += 1
            safe_print(
                "Failed to upsert conversation "
                f"{conversation_id} / {search_domain}: {exc}"
            )

    safe_print(
        "Conversation rollup complete. "
        f"Important upserts={inserted_count}, routine skips={skipped_count}, failures={failed_count}."
    )


if __name__ == "__main__":
    try:
        run_rollup()
    finally:
        close_db_pool()
