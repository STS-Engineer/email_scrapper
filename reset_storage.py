import os
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient

load_dotenv()

AZURE_CONNECTION_STRING = os.getenv('AZURE_CONNECTION_STRING')
AZURE_CONTAINER_NAME = os.getenv('AZURE_CONTAINER_NAME', 'avocarbon-emails')

def nuke_blob_storage():
    print(f"🌐 Connecting to Azure Blob Container: '{AZURE_CONTAINER_NAME}'...")
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)
        
        blobs = container_client.list_blobs()
        count = 0
        
        print("🗑️ Beginning mass deletion...")
        for blob in blobs:
            container_client.delete_blob(blob.name)
            count += 1
            if count % 100 == 0:
                print(f"  ...deleted {count} files so far.")
                
        print(f"\n✅ SUCCESS: Wiped {count} total files from Azure Blob Storage.")
        
    except Exception as e:
        print(f"\n❌ Error connecting to Azure: {e}")

if __name__ == "__main__":
    print("⚠️ WARNING: This will permanently delete ALL emails and attachments in your Azure cloud.")
    confirm = input("Type 'YES' to proceed: ")
    
    if confirm == 'YES':
        nuke_blob_storage()
    else:
        print("Aborted. Nothing was deleted.")