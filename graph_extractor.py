import msal
import requests
import base64
import os
import hashlib
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv # NEW IMPORT
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions

# --- NEW: Azure Library ---
from azure.storage.blob import BlobServiceClient

from database_manager import insert_email, insert_attachment, initialize_database, email_exists
from ai_helper import generate_email_summary

load_dotenv()
# --- Configuration ---
TENANT_ID = os.getenv('TENANT_ID')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
AZURE_CONNECTION_STRING = os.getenv('AZURE_CONNECTION_STRING')
AZURE_CONTAINER_NAME = os.getenv('AZURE_CONTAINER_NAME', 'avocarbon-emails')

SEARCH_DOMAIN = 'mahle'
BASE_OUTPUT_FOLDER = 'extracted_emails' # Local storage for POC!

IS_CRON_JOB = True # Set to True so it automatically grabs the last 24 hours at midnight


# Leave empty to search everyone, or add specific emails for testing
TARGET_EMAILS = [
]

AUTHORITY = f'https://login.microsoftonline.com/{TENANT_ID}'
SCOPES = ['https://graph.microsoft.com/.default']
GRAPH_ENDPOINT = 'https://graph.microsoft.com/v1.0'

def sanitize_filename(filename):
    clean_name = re.sub(r'[\\/*?:"<>|]', "", filename)
    return clean_name[:100] if len(clean_name) > 100 else clean_name

def get_access_token():
    app = msal.ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET)
    result = app.acquire_token_silent(SCOPES, account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=SCOPES)
    return result.get('access_token')

def download_attachments(access_token, user_email, message_id, cloud_folder_path):
    headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
    url = f"{GRAPH_ENDPOINT}/users/{user_email}/messages/{message_id}/attachments"
    
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        attachments = response.json().get('value', [])
        
        for att in attachments:
            if att.get('@odata.type') == '#microsoft.graph.fileAttachment':
                file_name = att.get('name')
                content_bytes = att.get('contentBytes')
                
                if content_bytes and file_name:
                    file_data = base64.b64decode(content_bytes)
                    safe_file_name = sanitize_filename(file_name)
                    _, file_extension = os.path.splitext(safe_file_name)
                    
                    # Create the cloud path: user@domain.com/2026-03-25_hash/filename.pdf
                    blob_name = f"{cloud_folder_path}/{safe_file_name}"
                    
                    try:
                        # Upload directly to Azure
                        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
                        container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)
                        blob_client = container_client.get_blob_client(blob_name)
                        
                        blob_client.upload_blob(file_data, overwrite=True)

                        # --- GENERATE SAS TOKEN ---
                        sas_token = generate_blob_sas(
                            account_name=blob_service_client.account_name,
                            container_name=AZURE_CONTAINER_NAME,
                            blob_name=blob_name,
                            account_key=blob_service_client.credential.account_key,
                            permission=BlobSasPermissions(read=True),
                            expiry=datetime.utcnow() + timedelta(days=365) # Or whatever duration you prefer
                        )

                        # Create the signed URL
                        azure_file_url = f"{blob_client.url}?{sas_token}"

                        # Save the SIGNED URL to your database
                        insert_attachment(message_id, safe_file_name, file_extension.lower(), azure_file_url)
                        
                        print(f"      ☁️ Uploaded to Azure & logged: {safe_file_name}")
                    except Exception as e:
                        print(f"      ❌ Azure Upload Failed for {safe_file_name}: {e}")
    else:
        print(f"      ❌ Failed to download attachments: {response.status_code}")

def search_user_mailbox(access_token, user_email, search_term, start_date=None, end_date=None):
    headers = {
        'Authorization': f'Bearer {access_token}', 
        'Content-Type': 'application/json',
        'Prefer': 'outlook.body-content-type="text"'
    }
    
    # We build a single KQL (Keyword Query Language) string
    query_parts = [search_term]
    
    if start_date:
        query_parts.append(f"received>={start_date}")
    if end_date:
        query_parts.append(f"received<={end_date}")
        
    # Join them with AND
    kql_query = " AND ".join(query_parts)
    
    # Inject the KQL query
    url = f"{GRAPH_ENDPOINT}/users/{user_email}/messages?$search=\"{kql_query}\"&$select=id,conversationId,subject,sender,toRecipients,receivedDateTime,hasAttachments,body&$top=100"
    
    while url:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            emails = data.get('value', [])
            
            if emails:
                print(f"\n✅ Processing batch of {len(emails)} emails for {user_email}...")
                for email in emails:
                    message_id = email.get('id')
                    subject = email.get('subject', 'No Subject')
                    
                    # Check PostgreSQL before doing ANYTHING
                    if email_exists(message_id):
                        print(f"  ⏭️ Skipping duplicate: {subject[:30]}...")
                        continue 
                    
                    conversation_id = email.get('conversationId', 'Unknown')
                    date = email.get('receivedDateTime', 'Unknown Date')
                    has_attachments = email.get('hasAttachments')
                    
                    sender = email.get('sender', {}).get('emailAddress', {}).get('address', 'Unknown Sender')
                    
                    to_recipients = [r.get('emailAddress', {}).get('address', '') for r in email.get('toRecipients', [])]
                    recipients_string = ", ".join(filter(None, to_recipients))
                    
                    body_content = email.get('body', {}).get('content', '').strip()
                    total_text_length = len(subject) + len(body_content)
                    
                    ai_summary = ""
                    if total_text_length < 300:
                        email_type = 'SHORT'
                    else:
                        email_type = 'LONG'
                        print(f"  🧠 Generating AI summary for: {subject[:30]}...")
                        ai_summary = generate_email_summary(subject, body_content) 
                    
                    # --- NEW: Cloud Folder Structure ---
                    clean_date = date.split('T')[0] if 'T' in date else 'UnknownDate'
                    short_id = hashlib.md5(message_id.encode('utf-8')).hexdigest()[:6]
                    cloud_folder_path = f"{user_email}/{clean_date}_{short_id}"
                    
                    # Upload the email body text directly to Azure
                    text_content = f"Date: {date}\nFrom: {sender}\nSubject: {subject}\nMessage ID: {message_id}\n"
                    text_content += "-" * 40 + "\n\n" + body_content
                    
                    try:
                        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
                        container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)
                        text_blob_client = container_client.get_blob_client(f"{cloud_folder_path}/email_content.txt")
                        text_blob_client.upload_blob(text_content.encode('utf-8'), overwrite=True)
                    except Exception as e:
                        print(f"      ❌ Failed to upload email body to Azure: {e}")
                    
                    insert_email(
                        msg_id=message_id, conv_id=conversation_id, domain=search_term, 
                        sender=sender, recipients=recipients_string, date=date, 
                        subject=subject, body=body_content, email_type=email_type, summary=ai_summary
                    )
                    print(f"  📁 Logged ({email_type}): {subject[:30]}...")
                    
                    if has_attachments:
                        print("  📎 Attachments found. Uploading to Azure...")
                        # Pass the cloud folder path instead of a local path
                        download_attachments(access_token, user_email, message_id, cloud_folder_path)
            
            url = data.get('@odata.nextLink')
            
        elif response.status_code == 404:
            break # Mailbox doesn't exist
        else:
            print(f"Failed to search {user_email}: {response.status_code}")
            break

def get_all_users(access_token):
    headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
    url = f"{GRAPH_ENDPOINT}/users?$select=id,userPrincipalName,mail"
    users = []
    while url:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            users.extend(data.get('value', []))
            url = data.get('@odata.nextLink') 
        else:
            break
    return users

def run_search(custom_domain=SEARCH_DOMAIN, custom_start=None, custom_end=None):
    initialize_database()
    
    print("Authenticating...")
    token = get_access_token()
    if not token:
        return

    # Set the active domain based on the API or fallback to the default
    active_domain = custom_domain

    if not (custom_start and custom_end) and not IS_CRON_JOB:
        raise ValueError("run_search requires custom_start/custom_end when IS_CRON_JOB is False.")

    if custom_start and custom_end:
        print(f"🚀 API Triggered Mode: Fetching {active_domain} from {custom_start} to {custom_end}...")
        active_start_date = custom_start
        active_end_date = custom_end
    # Date Calculation Logic
    elif IS_CRON_JOB:
        print("🕒 Running in CRON Mode: Fetching emails from the last 24 hours...")
        now = datetime.utcnow()
        yesterday = now - timedelta(days=1)
        
        active_start_date = yesterday.strftime('%Y-%m-%d')
        active_end_date = now.strftime('%Y-%m-%d')
    else:
        print(f"📅 Running in MANUAL Mode: Fetching emails from {MANUAL_START_DATE} to {MANUAL_END_DATE}...")
        active_start_date = MANUAL_START_DATE
        active_end_date = MANUAL_END_DATE
        
    if TARGET_EMAILS:
        print(f"Starting targeted search for {len(TARGET_EMAILS)} specific users...\n")
        users_to_search = TARGET_EMAILS
    else:
        print("Fetching Avocarbon company directory...")
        all_users = get_all_users(token)
        users_to_search = [user.get('mail') or user.get('userPrincipalName') for user in all_users]
        users_to_search = [u for u in users_to_search if u] 
        print(f"Found {len(users_to_search)} users. Starting global search...\n")
        
    print("=" * 60)
    for email in users_to_search:
        search_user_mailbox(token, email, active_domain, active_start_date, active_end_date)
