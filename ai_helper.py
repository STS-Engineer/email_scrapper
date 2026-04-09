import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

def generate_email_summary(subject, body_text):
    """Sends the Subject and Body to GPT-4o for a 1-2 sentence summary."""
    
    # Clean up empty fields just in case
    safe_subject = subject if subject else "[No Subject]"
    safe_body = body_text if body_text else "[Empty Body]"
    
    # We combine them so the AI has 100% of the context
    combined_content = f"Email Subject: {safe_subject}\n\nEmail Body:\n{safe_body}"
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o", # Using the mini model because it is incredibly cheap and fast
            messages=[
                {
                    "role": "system", 
                    "content": "You are a professional corporate assistant. Read the following email and provide a concise, 1 to 2 sentence summary of the interaction. Focus on the core request, action, or decision."
                },
                {
                    "role": "user", 
                    "content": combined_content
                }
            ],
            max_tokens=100, # Keeps the summary short and caps your costs
            temperature=0.3 # Low temperature keeps the AI factual and prevents hallucination
        )
        
        # Extract the text from the API response
        summary = response.choices[0].message.content.strip()
        return summary
        
    except Exception as e:
        print(f"❌ AI Summarization failed: {e}")
        return "Summary generation failed."