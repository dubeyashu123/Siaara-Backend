# main.py

from google import genai
import aiohttp
import ssl
from fastapi import FastAPI, HTTPException, WebSocket, Form
import json
import base64
from dotenv import load_dotenv
from twilio.rest import Client as RestClient
from twilio.twiml.voice_response import VoiceResponse, Start
from starlette.responses import Response as XMLResponse
import os
import asyncio
import uvicorn
import gspread
import asyncio
from oauth2client.service_account import ServiceAccountCredentials

# --- Configuration ---
load_dotenv()

CONVERSATION_HISTORY = {}

# --- LLM Configuration ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("FATAL: GEMINI_API_KEY not found in .env file.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL = "gemini-2.5-flash"

# --- Deepgram Configuration ---
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
if not DEEPGRAM_API_KEY:
    raise ValueError("FATAL: DEEPGRAM_API_KEY not found in .env file.")

# --- Twilio & Sheets ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
SHEET_ID = os.getenv("GOOGLE_SHEETS_FILE_ID")
CREDENTIALS_FILE = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE")

PLIVO_ANSWER_URL = "https://siaara.clickites.com/plivo_answer"

app = FastAPI(title="AI Sales Call Agent MVP")


# --- Helper Functions ---
def get_pending_lead():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        all_records = sheet.get_all_records()
        for i, record in enumerate(all_records):
            if record.get('Status', '').lower() == 'pending':
                return record, i + 2
        return None, None
    except Exception as e:
        print(f"Google Sheets Error in get_pending_lead: {e}")
        return None, None


def set_lead_status(row_number: int, status: str, call_sid: str = None):
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        sheet.update_cell(row_number, 5, status)
        if call_sid:
            sheet.update_cell(row_number, 6, call_sid)
        print(f"Lead status updated for row {row_number}: {status}")
    except Exception as e:
        print(f"Failed to update Google Sheet: {e}")


@app.get("/")
def home():
    return {"status": "ok", "message": "AI Sales Agent MVP is running!"}


@app.post("/call")
def initiate_call():
    lead_data, row_number = get_pending_lead()
    if not lead_data:
        return {"status": "complete", "message": "No pending leads found in Google Sheet."}

    lead_name = lead_data.get('LeadName', 'Customer')
    lead_phone = lead_data.get('Phone')

    if not lead_phone:
        print(f"Skipping lead {lead_name}: No phone number found.")
        return {"status": "skipped", "message": f"Lead {lead_name} skipped (No Phone)."}

    print(f"Attempting to call {lead_name} at {lead_phone}...")

    try:
        client = RestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        response = client.calls.create(
            to=lead_phone,
            from_=TWILIO_PHONE_NUMBER,
            url=PLIVO_ANSWER_URL,
            method='POST'
        )
        call_sid = response.sid
        print(f"Call initiated successfully. Call SID: {call_sid}")
        set_lead_status(row_number, "Calling", call_sid)
        return {"status": "calling", "message": f"Call initiated for {lead_name}.", "call_sid": call_sid}
    except Exception as e:
        print(f"--- TWILIO API FAILED --- Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to initiate call via Twilio. Error: {e}")


# --- Conversation Handler ---

def mulaw_silence(duration_ms=200, sample_rate=8000):
    """
    Generate mu-law silence bytes for duration_ms.
    For mu-law 8kHz, 1 ms = 8 samples. Silence in mu-law is typically 0xFF.
    """
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    return bytes([0xFF]) * num_samples

async def handle_conversation(twilio_ws, sample_rate=8000):
    print("🧠 handle_conversation started")
    session = aiohttp.ClientSession()
    dg_ws = await session.ws_connect(
        DEEPGRAM_URL,
        headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
        autoping=True
    )
    print("✅ Connected to Deepgram realtime websocket.")

    configured = False

    async def configure_deepgram():
        nonlocal configured
        try:
            cfg = {
                "type": "Configure",
                "features": { 
                    "language": "en-IN",
                    "smart_format": True,
                    "interim_results": True, # Make it True for Live Transript
                    "encoding": "mulaw",
                    "sample_rate": 8000
                }
            }
            await dg_ws.send_str(json.dumps(cfg))
            configured = True
            print("🛠 Sent primary Configure to Deepgram.")

            # 🔸 Prime the stream with 200ms silence
            silence = mulaw_silence(200, sample_rate)
            await dg_ws.send_bytes(silence)
            print("🔊 Sent 200ms mu-law silence to Deepgram.")

        except Exception as e:
            print("⚠️ Primary Configure failed:", e)
            # fallback with processors config
            try:
                processors_cfg = {
                    "type": "Configure",
                    "processors": [
                        {
                            "type": "transcription",
                            "config": {
                                "language": "en-IN",
                                "model": "nova-2",
                                "smart_format": True,
                                "interim_results": False
                            }
                        }
                    ]
                }
                await dg_ws.send_str(json.dumps(processors_cfg))
                configured = True
                print("🛠 Sent fallback processors Configure to Deepgram.")
                # silence again after fallback
                silence = mulaw_silence(200, sample_rate)
                await dg_ws.send_bytes(silence)
                print("🔊 Sent 200ms mu-law silence after fallback.")
            except Exception as e2:
                print("🔴 processors Configure also failed:", e2)

    async def deepgram_listener():
        try:
            async for msg in dg_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    raw = msg.data
                    # print raw message snippet for debugging
                    print("📥 Deepgram raw:", raw[:500])
                    try:
                        data = json.loads(raw)
                    except Exception as e:
                        print("⚠️ Could not parse Deepgram JSON:", e, raw)
                        continue

                    t = data.get("type", "").lower()
                    if t in ("transcript", "transcripts"):
                        transcript = data.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "")
                        if transcript.strip():
                            print(f"🗣 Transcript: {transcript}")
                    elif t == "metadata":
                        print("ℹ️ Deepgram metadata:", data)
                    elif t == "error":
                        print("🔴 Deepgram ERROR event:", data)
                    else:
                        print("📄 Deepgram other event:", data)

                elif msg.type == aiohttp.WSMsgType.BINARY:
                    print("📦 Deepgram sent binary (len):", len(msg.data))

                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    print("🛑 Deepgram websocket closed.")
                    break

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    print("⚠️ Deepgram websocket error:", msg)
                    break

        except Exception as e:
            print("deepgram_listener exception:", e)

    async def twilio_listener():
        packet_count = 0
        try:
            while True:
                msg = await twilio_ws.receive()
                if msg["type"] == "websocket.disconnect":
                    print("📞 Twilio WebSocket disconnected.")
                    break
                if msg["type"] != "websocket.receive":
                    continue

                if "text" in msg:
                    data = json.loads(msg["text"])
                    event = data.get("event", "")

                    if event == "start":
                        print("Twilio start payload:", data)
                        await configure_deepgram()

                    elif event == "media" and configured:
                        try:
                            media_payload = data["media"]["payload"]
                            audio_data = base64.b64decode(media_payload)
                            await dg_ws.send_bytes(audio_data)
                            packet_count += 1
                            if packet_count % 50 == 0:
                                print(f"🎧 Forwarded {packet_count} audio packets to Deepgram.")
                        except Exception as e:
                            print("⚠️ Error decoding or sending media:", e)

                    elif event == "stop":
                        print("🛑 Twilio stop event received.")
                        break
        except Exception as e:
            print("twilio_listener exception:", e)
        finally:
            print(f"✅ Twilio listener done. Total packets: {packet_count}")

    listener_tasks = [
        asyncio.create_task(twilio_listener()),
        asyncio.create_task(deepgram_listener())
    ]

    await asyncio.wait(listener_tasks, return_when=asyncio.FIRST_COMPLETED)

    print("🧹 Cleaning up Twilio/Deepgram connections...")
    for t in listener_tasks:
        t.cancel()

    try:
        await dg_ws.close()
        await twilio_ws.close()
        await session.close()
    except Exception as e:
        print("⚠️ Cleanup exception:", e)

    print("✅ Conversation handler finished.")


@app.websocket("/media")
async def websocket_endpoint(websocket: WebSocket):
    try:
        await websocket.accept()
        print("WebSocket connection established with Twilio.")
    except Exception as e:
        print(f"WebSocket Handshake failed: {e}")
        return

    try:
        await handle_conversation(websocket)
    except Exception as e:
        print(f"WebSocket conversation handler failed: {e}")
    finally:
        print("WebSocket connection closed.")


@app.api_route("/plivo_answer", methods=["GET", "POST"])
async def twilio_answer(CallSid: str = Form(None)):
    GREETING = "Hi, I'm Rahul from [Your Company Name]. I'm calling about a service that helps businesses like yours save time on sales calls. How are you today?"

    response = VoiceResponse()
    ws_url = f"wss://siaara.clickites.com/media"
    print(f"Streaming URL set to: {ws_url}")

    start = Start()
    start.stream(url=ws_url, track='both')
    response.append(start)
    response.pause(length=1)
    response.say(GREETING)
    # NEW: Gather to keep the call alive and the stream open
    action_url = f"https://siaara.clickites.com/end_call"
    response.gather(
        timeout=30, # Keep the stream open for 30 seconds of silence/no input
        num_digits=1, 
        action='/end_call' 
    )
    xml_response = str(response)
    print(f"TwiML Sent: {xml_response}")
    return XMLResponse(content=xml_response, media_type="application/xml")


@app.post("/end_call")
def end_call_cleanup(CallSid: str = Form(None)):
    print(f"Call {CallSid} completed or timed out. Hanging up.")
    response = VoiceResponse()
    response.hangup()
    return XMLResponse(content=str(response), media_type="application/xml")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
