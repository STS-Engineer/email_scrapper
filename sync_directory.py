import os
import requests
import msal
from dotenv import load_dotenv
from database_manager import get_db_connection

load_dotenv()

# --- Configuration ---
TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]

def get_access_token():
    print("🔐 Authenticating with Microsoft Graph...")
    app = msal.ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET
    )
    result = app.acquire_token_silent(SCOPES, account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=SCOPES)
    return result.get("access_token")

def sync_licensed_users():
    token = get_access_token()
    if not token:
        print("❌ Failed to get access token.")
        return

    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    # Requesting assignedLicenses is the key to filtering out system accounts
    url = "https://graph.microsoft.com/v1.0/users?$select=mail,accountEnabled,assignedLicenses&$top=999"
    
    real_human_emails = []
    
    print("📡 Fetching user directory and checking Microsoft subscriptions...")
    while url:
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print(f"❌ Graph API Error: {response.text}")
            break
            
        data = response.json()
        for user in data.get('value', []):
            email = user.get('mail')
            is_enabled = user.get('accountEnabled')
            licenses = user.get('assignedLicenses', [])
            
            # RULE: Must have an email, must be an active account, and MUST have a paid license
            if email and is_enabled and len(licenses) > 0:
                real_human_emails.append(email.lower())
                
        url = data.get('@odata.nextLink')

    if not real_human_emails:
        print("⚠️ No licensed users found.")
        return

    print(f"✅ Found {len(real_human_emails)} real, licensed employee mailboxes.")
    print("💾 Saving to PostgreSQL 'TargetMailboxes' table...")

    # Save them to the database
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            for email in real_human_emails:
                cursor.execute(
                    '''
                    INSERT INTO TargetMailboxes (email_address, is_active) 
                    VALUES (%s, TRUE)
                    ON CONFLICT (email_address) DO NOTHING
                    ''', 
                    (email,)
                )
        conn.commit()
        
    print("🚀 Sync complete! Your database is now populated and ready for the daily cron job.")

if __name__ == "__main__":
    sync_licensed_users()