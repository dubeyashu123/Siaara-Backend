# main.py

from google import genai
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi import Form #To handle POST data from Twilio's webhook
from deepgram import Deepgram
from deepgram import DeepgramClient, LiveTranscriptionEvents # ✅ NEW IMPORTS for Streaming
import json # ✅ NEW IMPORT for handling Twilio JSON
import base64 # ✅ NEW IMPORT for decoding audio data
from pydantic import BaseModel

from dotenv import load_dotenv
from twilio.rest import Client as RestClient 
from twilio.twiml.voice_response import VoiceResponse, Start, Stream,  Say, Pause,  Record 
from starlette.responses import Response as XMLResponse # For returning XML from webhook

import os
import requests # To download the audio file
import asyncio #new import for async tasks
import uvicorn
import gspread 
from oauth2client.service_account import ServiceAccountCredentials


# --- Configuration ---
# 1. Load Environment Variables (MUST BE FIRST)
load_dotenv()

# --- GLOBAL CONTEXT MANAGEMENT (Temporary for Dev) ---
# Store the conversation history {CallSid: [messages]}
CONVERSATION_HISTORY = {} 

# LLM & STT Configuration

# LLM Configuration (Using Gemini)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("FATAL: GEMINI_API_KEY not found in .env file or is empty.")

# Initialize Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
# Set the model (we will use a fast one for voice)
GEMINI_MODEL = "gemini-2.5-flash" 


# ... [Deepgram key and client] ...
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
if not DEEPGRAM_API_KEY:
    raise ValueError("FATAL: DEEPGRAM_API_KEY not found in .env file or is empty.")
deepgram_rest_client = DeepgramClient(DEEPGRAM_API_KEY) # Initialize Deepgram Client
# New Deepgram Client for streaming
deepgram_streaming_client = DeepgramClient(DEEPGRAM_API_KEY) # ✅ New client for streaming

# Global Constants loaded from .env (Use consistent UPPERCASE names)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
SHEET_ID = os.getenv("GOOGLE_SHEETS_FILE_ID")
CREDENTIALS_FILE = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE")

# REPLACE with your current Ngrok URL (e.g., https://redoubted-chu-jumpily.ngrok-free.dev/plivo_answer)
PLIVO_ANSWER_URL = "https://postcard-prize-polished-heath.trycloudflare.com/plivo_answer"  
# Inside /plivo_answer
#print(f"Streaming URL set to: {ws_url}")

app = FastAPI(title="AI Sales Call Agent MVP")

# --- Helper Functions ---

def get_pending_lead():
    """Reads Google Sheet, finds the first lead with 'Status' as 'Pending', and returns it."""
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        
        # Get all records with headers
        all_records = sheet.get_all_records()

        # Find the first lead where Status is 'Pending'
        # i starts from 0, so row_number must be i + 2 (Header row 1 + 1 for 0-index)
        for i, record in enumerate(all_records):
            if record.get('Status', '').lower() == 'pending':
                return record, i + 2 
        
        return None, None 

    except Exception as e:
        print(f"Google Sheets Error in get_pending_lead: {e}")
        return None, None
        
def set_lead_status(row_number: int, status: str, call_sid: str = None):
    """Updates the Status and CallSid columns for a specific row in the Google Sheet."""
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        
        # Update Status (Column 5) and CallSid (Column 6). Columns must be verified.
        sheet.update_cell(row_number, 5, status) 
        if call_sid:
            sheet.update_cell(row_number, 6, call_sid) 
            
        print(f"Lead status updated for row {row_number}: {status}")
    except Exception as e:
        print(f"Failed to update Google Sheet: {e}")

# --- API Endpoints ---

@app.get("/")
def home():
    """Root endpoint to verify the API is running."""
    return {"status": "ok", "message": "AI Sales Agent MVP is running!"}

@app.post("/call")
def initiate_call():
    """
    Finds the next pending lead and initiates an outbound call via Twilio.
    """
    lead_data, row_number = get_pending_lead()
    
    if not lead_data:
        return {"status": "complete", "message": "No pending leads found in the Google Sheet."}
        
    lead_name = lead_data.get('LeadName', 'Customer')
    lead_phone = lead_data.get('Phone') # This must be in E.164 format (+91XXXXXXXXXX)
    
    if not lead_phone:
        print(f"Skipping lead {lead_name}: No phone number found.")
        # Optional: set_lead_status(row_number, "Skipped - No Phone")
        return {"status": "skipped", "message": f"Lead {lead_name} skipped (No Phone)."}
        
    print(f"Attempting to call {lead_name} at {lead_phone}...")
    
    # --- TWILIO INTEGRATION (REAL CALL) ---
    try:
        # 1. Initialize Twilio Client
        client = RestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        # 2. Make the API call to start the call
        response = client.calls.create(
            to=lead_phone,
            from_=TWILIO_PHONE_NUMBER,
            url=PLIVO_ANSWER_URL, # Webhook URL for when the lead answers
            method='POST'
        )
        
        call_sid = response.sid 
        print(f"Call initiated successfully. Call SID: {call_sid}")
        
        # 3. Update Sheet Status (Only on successful API call)
        set_lead_status(row_number, "Calling", call_sid) 
        
        return {
            "status": "calling", 
            "message": f"Call initiated for {lead_name}.", 
            "call_sid": call_sid,
            "row_number": row_number
        }
    
    except Exception as e:
        # If API call fails (e.g., Bad Credentials, Invalid Phone Number, No Credit)
        print(f"--- TWILIO API FAILED ---")
        print(f"Error: {e}") 
        
        # Stop the flow and tell the user the API failed
        raise HTTPException(status_code=500, detail=f"Failed to initiate call via Twilio. Check keys/credits/phone format. Error: {e}")



# --- Conversation Handler (Day 10 Logic: Twilio <-> Deepgram) ---
async def handle_conversation(websocket: WebSocket):
    
    # 1. Deepgram Streaming Connection Shuru Karein
    # Deepgram ko live audio sunne ke liye set karein
    dg_connection = deepgram_streaming_client.listen.live.v("1").start_transcription({
        "punctuate": True,
        "language": "en-IN",
        "model": "phonecall", # Phone call ke liye optimized model
        "interim_results": False # Sirf final result chahiye
    })

    # 2. Deepgram se Events ko handle karne ke liye naya Task
    # Deepgram se results asynchronous tarike se receive karne ke liye
    async def deepgram_receiver(dg_connection):
        try:
            async for data in dg_connection:
                
                # Check for Final Transcript
                if data.event == LiveTranscriptionEvents.Transcript:
                    # Final transcript extract karein
                    transcript = data.response["channel"]["alternatives"][0]["transcript"]
                    is_final = data.response["is_final"]
                    
                    if is_final and transcript:
                        print(f"🎤 Final Transcript: {transcript}")
                        
                        # --- Day 11 Task Placeholder: Gemini LLM Call and Reply ---
                        llm_reply = f"Thank you for saying: {transcript}. I heard you clearly."
                        print(f"🤖 LLM Placeholder Reply: {llm_reply}")
                        
                        # Note: Twilio ko reply dene ki logic Day 11 mein aayegi
                        
        except Exception as e:
            print(f"Deepgram Receiver Error: {e}")
        finally:
            print("Deepgram receiver closed.")


    # 3. Main Task (Deepgram se data receive karna background mein chalega)
    dg_receiver_task = asyncio.create_task(deepgram_receiver(dg_connection))
    
    # 4. Twilio Media Stream Se Audio Receive Karna
    try:
        while True:
            # Twilio media messages JSON format mein aate hain
            data = await websocket.receive_text()
            
            # Twilio media message ko parse karna
            json_data = json.loads(data)
            
            if json_data["event"] == "media":
                # Media data base64 encoded hota hai
                audio_chunk = json_data["media"]["payload"]
                
                # Deepgram ko audio bhejte hain (Base64 decode karke)
                dg_connection.send(base64.b64decode(audio_chunk))
                
            elif json_data["event"] == "start":
                print(f"Twilio Call SID: {json_data['start']['callSid']} started stream.")
                
            elif json_data["event"] == "stop":
                print("Twilio stopped the media stream.")
                break
                
    except Exception as e:
        print(f"Twilio Receiver Error: {e}")
    finally:
        # Jab Twilio ka loop khatam ho jaye, toh Deepgram connection bhi band karo
        dg_connection.finish()
        dg_receiver_task.cancel()
        print("Conversation handler finished.")

# --- NEW WS CODE: Twilio Streaming Receiver ---

# src/main.py
# Twilio Media Stream के लिए एक अलग WebSocket EndPoint

@app.websocket("/media")
async def websocket_endpoint(websocket: WebSocket):
    """
    Handles the WebSocket connection from Twilio.
    This is the entry point for real-time audio streams.
    """
    # 🌟 CRITICAL FIX: Handshake MUST happen outside the try/except block.
    # This ensures the connection is accepted before any processing logic begins.
    try:
        # Twilio Media Protocol को तुरंत स्वीकार करें 
        await websocket.accept()
        await asyncio.sleep(0)

        print("WebSocket connection established with Twilio.")
        # CRITICAL: Twilio को पहला पैकेट भेजने के लिए जगह/समय दें
        
        
    except Exception as e:
        # अगर Handshake (accept) यहाँ विफल होता है, तो तुरंत लॉग करें और बाहर निकलें।
        print(f"WebSocket Handshake failed: {e}")
        return # कनेक्शन स्वीकार नहीं हुआ, तुरंत बाहर निकलें

    # Handshake सफल होने के बाद, अब हम संदेशों को process करते हैं
    try:
        # Twilio का पहला message 'start' event होता है
        start_message = await websocket.receive_text()
        print(f"Received Twilio Start Event.") 
        
        # अब conversation logic शुरू करें
        await handle_conversation(websocket)
            
    except Exception as e:
        # यह एरर अब conversation logic या Deepgram से संबंधित होगा
        print(f"WebSocket conversation handler failed: {e}")
        
    finally:
        print("WebSocket connection closed.")


# --- Webhook Endpoints ---
# src/main.py (Replace the existing /plivo_answer function entirely

@app.api_route("/plivo_answer", methods=["GET", "POST"]) 
async def twilio_answer(CallSid: str = Form(None)):
    
    GREETING = "Hi, I'm Rahul from [Your Compay Name]. I'm calling about a service that helps businesses like yours save time on sales calls. How are you today?"

    response = VoiceResponse()
    
    # URL को पहले की तरह सेट करें
    base_url = PLIVO_ANSWER_URL.split('//')[1].split('/plivo_answer')[0]
    ws_url = f"wss://{base_url}/media"
    
    print(f"Streaming URL set to: {ws_url}")
    
    # 1. <Start> वर्ब का उपयोग करें (Connect की जगह)
    start = Start()
    
    # 2. Stream को Start वर्ब के अंदर जोड़ें
    start.stream(
        url=ws_url,
        track='both' 
    )
    
    # 3. Start को VoiceResponse में जोड़ें
    response.append(start)
    
    # 4. ग्रीटिंग <Say> कमांड जोड़ें (यह स्ट्रीम शुरू होने के साथ बजना शुरू हो जाएगा)
    response.say(GREETING)
    
    # 5. CRITICAL: <Pause> जोड़ें ताकि WebSocket Handshake के लिए Twilio को समय मिले
    # यह सुनिश्चित करता है कि Twilio Handshake पूरा होने तक कॉल को सक्रिय रखता है।
    response.pause(length=60) # 60 सेकंड का पॉज़
    
    xml_response = str(response)
    print(f"Twilio Answer TwiML Sent (Streaming): {xml_response}")
    
    # सुनिश्चित करें कि आप XMLResponse वापस कर रहे हैं
    return XMLResponse(content=xml_response, media_type="application/xml")

# --- Execution ---

if __name__ == "__main__":
    # Reload=True is convenient for development
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

