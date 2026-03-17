import msal
import requests
import base64
import os
import hashlib
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv # NEW IMPORT

from database_manager import insert_email, insert_attachment, initialize_database, email_exists
from ai_helper import generate_email_summary

# Load the environment variables from the .env file
load_dotenv()
# --- Configuration ---
TENANT_ID = os.getenv('TENANT_ID')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
SEARCH_DOMAIN = 'mahle'
BASE_OUTPUT_FOLDER = 'extracted_emails' # Local storage for POC!

IS_CRON_JOB = True # Set to True so it automatically grabs the last 24 hours at midnight
# If you want to search all time, you can set these to None
MANUAL_START_DATE = '2026-02-01' 
MANUAL_END_DATE = '2026-03-10'

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

def download_attachments(access_token, user_email, message_id, target_folder):
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
                    file_path = os.path.join(target_folder, safe_file_name)
                    
                    # Save physical file LOCALLY
                    with open(file_path, 'wb') as f:
                        f.write(file_data)
                    
                    # Extract file extension for UI (e.g., '.pdf', '.xlsx')
                    _, file_extension = os.path.splitext(safe_file_name)
                    
                    # Insert into Database
                    insert_attachment(message_id, safe_file_name, file_extension.lower(), file_path)
                    
                    print(f"      ⬇️ Downloaded & logged attachment: {safe_file_name}")
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
                    
                    # Folder structure for LOCAL saving
                    clean_date = date.split('T')[0] if 'T' in date else 'UnknownDate'
                    short_id = hashlib.md5(message_id.encode('utf-8')).hexdigest()[:6]
                    folder_name = f"{clean_date}_{short_id}"
                    email_folder = os.path.join(BASE_OUTPUT_FOLDER, user_email, folder_name)
                    
                    os.makedirs(email_folder, exist_ok=True)
                    
                    text_file_path = os.path.join(email_folder, "email_content.txt")
                    with open(text_file_path, 'w', encoding='utf-8') as f:
                        f.write(f"Date: {date}\nFrom: {sender}\nSubject: {subject}\nMessage ID: {message_id}\n")
                        f.write("-" * 40 + "\n\n" + body_content)
                    
                    insert_email(
                        msg_id=message_id, conv_id=conversation_id, domain=search_term, 
                        sender=sender, recipients=recipients_string, date=date, 
                        subject=subject, body=body_content, email_type=email_type, summary=ai_summary
                    )
                    print(f"  📁 Logged ({email_type}): {subject[:30]}...")
                    
                    if has_attachments:
                        print("  📎 Attachments found. Downloading...")
                        download_attachments(access_token, user_email, message_id, email_folder)
            
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
