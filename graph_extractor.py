import base64
import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from threading import Lock
from urllib.parse import quote

import msal
import requests
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas
from dotenv import load_dotenv

from ai_helper import generate_email_summary
from database_manager import (
    email_exists,
    get_active_mailboxes,
    initialize_database,
    insert_attachment,
    insert_email,
)

load_dotenv()

# --- Configuration ---
TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
AZURE_CONNECTION_STRING = os.getenv("AZURE_CONNECTION_STRING")
AZURE_CONTAINER_NAME = os.getenv("AZURE_CONTAINER_NAME", "avocarbon-emails")

SEARCH_DOMAIN = "mahle,valeo,nidec,bosch"
BASE_OUTPUT_FOLDER = "extracted_emails"

GRAPH_REQUEST_TIMEOUT_SECONDS = 60
GRAPH_RETRY_LIMIT = 5
GRAPH_RETRY_AFTER_FALLBACK_SECONDS = 30
CLIENT_DOMAIN_BATCH_SIZE = 15
JUNK_PATTERNS = [
    "no-reply",
    "noreply",
    "notification",
    "newsletter",
    "mailer-daemon",
    "do-not-reply",
    "support@",
]
SYSTEM_SENDERS = [
    "info@zoll-service.eu",
    "accounting.ger@avocarbon.com",
    "noreply@avocarbon.com",
]

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]
GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0"
GRAPH_ACCESS_TOKEN = None
_GRAPH_TOKEN_LOCK = Lock()


def safe_print_str(text):
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def safe_print(message):
    print(safe_print_str(message))


def sanitize_filename(filename):
    clean_name = re.sub(r'[\\/*?:"<>|]', "", filename)
    return clean_name[:100] if len(clean_name) > 100 else clean_name


def chunk_list(items, chunk_size):
    for start_index in range(0, len(items), chunk_size):
        yield items[start_index:start_index + chunk_size]


def format_utc_timestamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_client_domains(raw_domains):
    if raw_domains is None or (isinstance(raw_domains, str) and not raw_domains.strip()):
        raw_domains = SEARCH_DOMAIN

    if isinstance(raw_domains, str):
        candidates = raw_domains.split(",")
    elif isinstance(raw_domains, (list, tuple, set)):
        candidates = []
        for item in raw_domains:
            if item is None:
                continue
            if isinstance(item, str):
                candidates.extend(item.split(","))
            else:
                candidates.append(str(item))
    else:
        candidates = [str(raw_domains)]

    normalized_domains = []
    seen_domains = set()

    for candidate in candidates:
        domain = str(candidate).strip().lower()
        if not domain or domain in seen_domains:
            continue
        seen_domains.add(domain)
        normalized_domains.append(domain)

    return normalized_domains


def resolve_search_window(custom_start=None, custom_end=None):
    if custom_start and custom_end:
        try:
            start_dt = datetime.strptime(custom_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_dt = datetime.strptime(custom_end, "%Y-%m-%d").replace(
                hour=23,
                minute=59,
                second=59,
                tzinfo=timezone.utc,
            )
        except ValueError as exc:
            raise ValueError("Manual dates must use YYYY-MM-DD format.") from exc
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=1)

    return format_utc_timestamp(start_dt), format_utc_timestamp(end_dt)


def parse_retry_after_seconds(retry_after_value):
    try:
        retry_seconds = int(retry_after_value)
        if retry_seconds > 0:
            return retry_seconds
    except (TypeError, ValueError):
        pass

    return GRAPH_RETRY_AFTER_FALLBACK_SECONDS


def graph_get(url, headers, params=None):
    last_response = None
    auth_refresh_attempted = False

    for attempt in range(GRAPH_RETRY_LIMIT + 1):
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=GRAPH_REQUEST_TIMEOUT_SECONDS,
        )
        last_response = response

        if response.status_code == 401 and not auth_refresh_attempted:
            refreshed_token = get_access_token(force_refresh=True)
            if refreshed_token:
                headers["Authorization"] = f"Bearer {refreshed_token}"
                auth_refresh_attempted = True
                safe_print("Graph API returned 401. Refreshed access token and retrying request.")
                continue

            safe_print("Graph API returned 401 and token refresh failed.")
            return response

        if response.status_code != 429:
            return response

        if attempt == GRAPH_RETRY_LIMIT:
            break

        retry_after_seconds = parse_retry_after_seconds(response.headers.get("Retry-After"))
        safe_print(
            f"Graph API throttled with 429. Sleeping for {retry_after_seconds} seconds "
            f"before retry {attempt + 1}/{GRAPH_RETRY_LIMIT}."
        )
        time.sleep(retry_after_seconds)

    safe_print(f"Graph API request failed after {GRAPH_RETRY_LIMIT} retries due to throttling.")
    return last_response


def build_graph_headers(access_token):
    token = GRAPH_ACCESS_TOKEN or access_token
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": 'outlook.body-content-type="text"',
        "ConsistencyLevel": "eventual",
    }


def get_access_token(force_refresh=False):
    global GRAPH_ACCESS_TOKEN

    with _GRAPH_TOKEN_LOCK:
        app = msal.ConfidentialClientApplication(
            CLIENT_ID,
            authority=AUTHORITY,
            client_credential=CLIENT_SECRET,
        )

        result = None
        if not force_refresh:
            result = app.acquire_token_silent(SCOPES, account=None)

        if not result:
            result = app.acquire_token_for_client(scopes=SCOPES)

        GRAPH_ACCESS_TOKEN = result.get("access_token")
        return GRAPH_ACCESS_TOKEN


def get_matched_domain(domain_batch, sender, recipients, subject, body_content):
    searchable_text = " ".join(
        [
            sender or "",
            recipients or "",
            subject or "",
            body_content or "",
        ]
    ).lower()

    matched_domains = []
    for domain in domain_batch:
        if domain in searchable_text:
            matched_domains.append(domain)

    if matched_domains:
        return ",".join(matched_domains)

    if len(domain_batch) == 1:
        return domain_batch[0]

    return f"Match in: {','.join(domain_batch)}"


def upload_email_body(cloud_folder_path, text_content):
    blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)
    text_blob_client = container_client.get_blob_client(f"{cloud_folder_path}/email_content.txt")
    text_blob_client.upload_blob(text_content.encode("utf-8"), overwrite=True)


def download_attachments(access_token, user_email, graph_message_id, global_msg_id, cloud_folder_path):
    headers = build_graph_headers(access_token)
    encoded_user_email = quote(user_email, safe="")
    encoded_graph_message_id = quote(graph_message_id, safe="")
    url = f"{GRAPH_ENDPOINT}/users/{encoded_user_email}/messages/{encoded_graph_message_id}/attachments"

    response = graph_get(url, headers=headers)
    if response is None:
        safe_print(f"      Failed to fetch attachments for {user_email}: no response received.")
        return

    if response.status_code != 200:
        safe_print(f"      Failed to download attachments for {user_email}: {response.status_code}")
        return

    attachments = response.json().get("value", [])
    if not attachments:
        return

    blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)

    for attachment in attachments:
        if attachment.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue

        file_name = attachment.get("name")
        content_bytes = attachment.get("contentBytes")

        if not content_bytes or not file_name:
            continue

        file_data = base64.b64decode(content_bytes)
        safe_file_name = sanitize_filename(file_name)
        _, file_extension = os.path.splitext(safe_file_name)
        blob_name = f"{cloud_folder_path}/{safe_file_name}"

        try:
            blob_client = container_client.get_blob_client(blob_name)
            blob_client.upload_blob(file_data, overwrite=True)

            sas_token = generate_blob_sas(
                account_name=blob_service_client.account_name,
                container_name=AZURE_CONTAINER_NAME,
                blob_name=blob_name,
                account_key=blob_service_client.credential.account_key,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(timezone.utc) + timedelta(days=365),
            )

            azure_file_url = f"{blob_client.url}?{sas_token}"
            insert_attachment(global_msg_id, safe_file_name, file_extension.lower(), azure_file_url)
            safe_print(f"      Uploaded to Azure and logged: {safe_file_name}")
        except Exception as exc:
            safe_print(f"      Azure upload failed for {safe_file_name}: {exc}")


def search_user_mailbox(access_token, user_email, client_domains, start_utc, end_utc):
    headers = build_graph_headers(access_token)
    encoded_user_email = quote(user_email, safe="")
    base_url = f"{GRAPH_ENDPOINT}/users/{encoded_user_email}/messages"
    select_fields = (
        "id,internetMessageId,conversationId,subject,sender,toRecipients,"
        "receivedDateTime,hasAttachments,body,uniqueBody"
    )

    # Graph API $search on /messages does not support date filtering in KQL
    # (>=, <=, and : range operators are all rejected). $filter cannot be combined
    # with $search either. So we search by domain keyword only and filter by date
    # in Python using receivedDateTime, which is already included in $select.
    start_dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))

    for batch_number, domain_batch in enumerate(chunk_list(client_domains, CLIENT_DOMAIN_BATCH_SIZE), start=1):

        # 1. Join the domains with OR
        domain_query = " OR ".join(f'"{domain}"' for domain in domain_batch)

        # 2. Only wrap in parentheses if there are multiple domains in the batch
        if len(domain_batch) > 1:
            domain_query = f"({domain_query})"

        # 3. KQL query — domain keywords only; date filtering is done post-fetch in Python.
        kql_query = domain_query

        params = {
            "$search": kql_query,
            "$select": select_fields,
            "$top": 100,
        }

        safe_print(
            f"Searching {user_email} with domain batch {batch_number} "
            f"({len(domain_batch)} domains) for window {start_utc} -> {end_utc}."
        )
        next_url = base_url
        next_params = params
        page_count = 0
        MAX_PAGES = 50

        while next_url:
            page_count += 1
            if page_count > MAX_PAGES:
                safe_print("Circuit breaker tripped! Exceeded 50 pages.")
                break

            response = graph_get(next_url, headers=headers, params=next_params)
            next_params = None

            if response is None:
                safe_print(f"Failed to search {user_email}: no response received.")
                break

            if response.status_code == 404:
                safe_print(f"Mailbox not found or inaccessible: {user_email}")
                return

            if response.status_code != 200:
                safe_print(f"Failed to search {user_email}: {response.status_code} - {response.text}")
                break

            data = response.json()
            emails = data.get("value", [])

            if emails:
                safe_print(f"Processing page of {len(emails)} emails for {user_email}...")

            for email in emails:
                graph_msg_id = email.get("id")
                global_msg_id = email.get("internetMessageId") or graph_msg_id
                subject = email.get("subject") or "No Subject"
                sender = email.get("sender", {}).get("emailAddress", {}).get("address", "Unknown Sender")
                sender_lower = sender.lower()

                if sender_lower in SYSTEM_SENDERS or any(
                    pattern in sender_lower for pattern in JUNK_PATTERNS
                ):
                    safe_print(
                        "  Dropping junk email... "
                        f"sender={sender_lower}, subject={subject[:30]}..."
                    )
                    continue

                if not global_msg_id:
                    safe_print(f"  Skipping message without a stable ID: {subject[:30]}...")
                    continue

                if email_exists(global_msg_id):
                    safe_print(f"  Skipping duplicate: {subject[:30]}...")
                    continue

                conversation_id = email.get("conversationId", "Unknown")
                received_date = email.get("receivedDateTime", "Unknown Date")

                # Date-window filter (KQL cannot do this via $search, so we do it here).
                try:
                    received_dt = datetime.fromisoformat(received_date.replace("Z", "+00:00"))
                    if not (start_dt <= received_dt <= end_dt):
                        continue
                except (ValueError, AttributeError):
                    pass  # If parsing fails, let the email through rather than silently drop it.
                has_attachments = bool(email.get("hasAttachments"))

                to_recipients = [
                    recipient.get("emailAddress", {}).get("address", "")
                    for recipient in email.get("toRecipients", [])
                ]
                recipients_string = ", ".join(filter(None, to_recipients))
                unique_body = email.get("uniqueBody", {}).get("content", "").strip()
                full_body = email.get("body", {}).get("content", "").strip()
                body_content = unique_body if unique_body else full_body
                total_text_length = len(subject) + len(body_content)
                matched_domain = get_matched_domain(
                    domain_batch,
                    sender,
                    recipients_string,
                    subject,
                    body_content,
                )

                ai_summary = ""
                subject_lower = subject.lower()
                is_ooo_or_bounce = any(
                    phrase in subject_lower
                    for phrase in [
                        "out of office",
                        "automatic reply",
                        "undeliverable",
                        "delivery status notification",
                        "auto-reply",
                        "absence",
                    ]
                )
                if sender_lower in SYSTEM_SENDERS or is_ooo_or_bounce:
                    email_type = "SYSTEM"
                    ai_summary = "Automated system notification or auto-reply."
                    safe_print(
                        "  Bypassing AI summary for automated message: "
                        f"sender={sender_lower}, subject={subject[:30]}..."
                    )
                elif total_text_length < 800:
                    email_type = "SHORT"
                else:
                    email_type = "LONG"
                    safe_print(f"  Generating AI summary for: {subject[:30]}...")
                    ai_summary = generate_email_summary(subject, body_content[:3000])

                clean_date = received_date.split("T")[0] if "T" in received_date else "UnknownDate"
                short_id = hashlib.md5(global_msg_id.encode("utf-8")).hexdigest()[:6]
                cloud_folder_path = f"{user_email}/{clean_date}_{short_id}"
                text_content = (
                    f"Date: {received_date}\n"
                    f"From: {sender}\n"
                    f"Subject: {subject}\n"
                    f"Message ID: {global_msg_id}\n"
                    + ("-" * 40)
                    + f"\n\n{body_content}"
                )

                try:
                    upload_email_body(cloud_folder_path, text_content)
                except Exception as exc:
                    safe_print(f"      Failed to upload email body to Azure: {exc}")

                insert_email(
                    msg_id=global_msg_id,
                    conv_id=conversation_id,
                    domain=matched_domain,
                    sender=sender,
                    recipients=recipients_string,
                    date=received_date,
                    subject=subject,
                    body=body_content,
                    email_type=email_type,
                    summary=ai_summary,
                )
                safe_print(f"  Logged ({email_type}): {subject[:30]}...")

                if has_attachments:
                    if graph_msg_id:
                        safe_print("  Attachments found. Uploading to Azure...")
                        download_attachments(
                            access_token,
                            user_email,
                            graph_msg_id,
                            global_msg_id,
                            cloud_folder_path,
                        )
                    else:
                        safe_print("  Attachments skipped because the Graph message ID is missing.")

            next_url = data.get("@odata.nextLink")


def run_search(custom_domain=SEARCH_DOMAIN, custom_start=None, custom_end=None):
    initialize_database()

    safe_print("Authenticating...")
    token = get_access_token()
    if not token:
        safe_print("Authentication failed. No access token was returned.")
        return

    client_domains = normalize_client_domains(custom_domain)
    if not client_domains:
        safe_print("No client domains were provided. Nothing to search.")
        return

    start_utc, end_utc = resolve_search_window(custom_start, custom_end)
    users_to_search = get_active_mailboxes()

    if not users_to_search:
        safe_print("No active target mailboxes found in TargetMailboxes. Nothing to search.")
        return

    safe_print(
        f"Starting mailbox scan for {len(users_to_search)} active mailboxes "
        f"across {len(client_domains)} client domains."
    )
    safe_print(f"Search window: {start_utc} -> {end_utc}")
    safe_print("=" * 60)

    for mailbox in users_to_search:
        search_user_mailbox(token, mailbox, client_domains, start_utc, end_utc)
