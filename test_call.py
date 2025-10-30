# test_call.py

import requests
import json

# The URL where your FastAPI server is running (Localhost is fine for triggering)
CALL_TRIGGER_URL = "http://127.0.0.1:8000/call" 

print(f"Attempting to trigger call at: {CALL_TRIGGER_URL}")

try:
    # Send an HTTP POST request to the /call endpoint
    response = requests.post(CALL_TRIGGER_URL)
    response.raise_for_status() # Raises an exception for 4xx or 5xx errors
    
    data = response.json()
    print("\n--- Call Trigger Status ---")
    print(f"Status: {data.get('status')}")
    print(f"Message: {data.get('message')}")
    
    # Print the Call SID if it was successfully initiated
    if data.get('call_sid'):
        print(f"Call SID (Twilio ID): {data.get('call_sid')}")
    if data.get('row_number'):
        print(f"Processing Lead Row: {data.get('row_number')}")
        
except requests.exceptions.RequestException as e:
    print("\n--- ERROR ---")
    print(f"Error triggering call. Make sure:")
    print("1. Your Uvicorn server is running in another terminal.")
    print("2. Ngrok is running and forwarding to port 8000.")
    print(f"Details: {e}")