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
        os.environ["CURRENT_CALL_SID"] = call_sid
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
    print("handle_conversation started")

    # Setup Deepgram session
    session = aiohttp.ClientSession()
    dg_ws = await session.ws_connect(
        f"{DEEPGRAM_URL}?encoding=mulaw&sample_rate=8000&channels=1&model=phonecall&language=en",
        headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
        autoping=True
    )
    print("✅ Connected to Deepgram realtime websocket.")

    configured = False
    ai_response_queue = asyncio.Queue()

    # --- Configure Deepgram ---
    async def configure_deepgram():
        nonlocal configured
        try:
            cfg = {
                "type": "configure",
                "encoding": "mulaw",      # ✅ Correct encoding for Twilio audio
                "sample_rate": 8000,
                "channels": 1,
                "model": "phonecall",
                "language": "en",
                "interim_results": False,
                "smart_format": True
            }
            await dg_ws.send_str(json.dumps(cfg))
            configured = True
            print("🛠 Sent Deepgram configuration.")

            # Prime with silence
            silence = mulaw_silence(200, sample_rate)
            await dg_ws.send_bytes(silence)
            print("Sent 200ms mu-law silence to Deepgram.")
        except Exception as e:
            print("Deepgram configure failed:", e)

    # --- Deepgram Listener ---
    async def deepgram_listener():
        try:
            async for msg in dg_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except:
                        continue

                    event_type = data.get("type", "")

                    # ✅ Handle the newer Deepgram "Results" event format
                    if event_type in ("Results", "transcript", "transcripts"):
                        channel = data.get("channel", data.get("metadata", {}))
                        alt = channel.get("alternatives", [{}])[0]
                        transcript = alt.get("transcript", "").strip()

                        if transcript:
                            print(f"🗣 Customer said: {transcript}")
                            await ai_response_queue.put(transcript)
                    else:
                        print(f"🧩 Deepgram event: {event_type}")
        except Exception as e:
            print("deepgram_listener exception:", e)
        finally:
            print("✅ Deepgram listener finished.")


    # --- Twilio Listener ---
    async def twilio_listener():
        packet_count = 0
        try:
            while True:
                msg = await twilio_ws.receive()

                if msg["type"] == "websocket.disconnect":
                    print("Twilio WebSocket disconnected.")
                    break
                if msg["type"] != "websocket.receive":
                    continue

                if "text" in msg:
                    data = json.loads(msg["text"])
                    event = data.get("event", "")

                    if event == "start":
                        print("Twilio start payload received. Deepgram already pre-configured via URL.")
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
                            print("Error sending media to Deepgram:", e)

                    elif event == "stop":
                        print("Twilio stop event received.")
                        break

        except Exception as e:
            print("twilio_listener exception:", e)
        finally:
            print(f"✅ Twilio listener done. Total packets: {packet_count}")

    # --- AI Responder ---
    async def ai_responder():
        from twilio.rest import Client as TwilioRestClient
        twilio_client = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        while True:
            user_text = await ai_response_queue.get()
            if not user_text:
                continue

            print(f"Queued transcript received by AI: {user_text}")
            print(f"Gemini thinking about: {user_text}")
            try:
                response = gemini_client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=f"The user said: '{user_text}'. Respond naturally..."   
                )
                ai_text = getattr(response, "text", "").strip() or "Okay."
                print(f"Gemini replied: {ai_text}")

                # --- Play AI's response on call ---
                # You already have Call SID saved in your sheet — or can pass it via context.
                # For simplicity, let's assume you use a global or env var for last call sid.

                CALL_SID = os.getenv("CURRENT_CALL_SID")  # or store dynamically when you call initiate_call

                if CALL_SID:
                    from twilio.twiml.voice_response import VoiceResponse
                    resp = VoiceResponse()
                    resp.say(ai_text, voice="Polly.Matthew")
                    twilio_client.calls(CALL_SID).update(twiml=str(resp))
                    print(f"📞 Sent AI reply to Twilio for call {CALL_SID}")
                else:
                    print("⚠️ No CALL_SID available to send reply to Twilio.")

            except Exception as e:
                print("⚠️ AI responder error:", e)

    # --- Run All Tasks ---
    tasks = [
        asyncio.create_task(twilio_listener()),
        asyncio.create_task(deepgram_listener()),
        asyncio.create_task(ai_responder())
    ]

    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    print(" Cleaning up connections...")
    for t in tasks:
        t.cancel()

    try:
        await dg_ws.close()
        await twilio_ws.close()
        await session.close()
    except Exception as e:
        print(" Cleanup exception:", e)

    print("✅ Conversation handler finished.")


@app.websocket("/media")
async def websocket_endpoint(websocket: WebSocket):
    call_sid = websocket.query_params.get("call_sid")
    print(f"🔗 Incoming WebSocket from Twilio for Call SID: {call_sid}")
    if call_sid:
        os.environ["CURRENT_CALL_SID"] = call_sid  # Now it’s available for AI responder
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
    """
    TwiML logic for outbound call to customer — two-way streaming.
    """
    GREETING = (
        "Hi, I'm Rahul from Siaara. "
        "We help automate your business calls and save you time. "
        "Is this a good time to talk?"
    )

    # WebSocket URL
    ws_url = f"wss://siaara.clickites.com/media?call_sid={CallSid}"
    print(f"Streaming URL set to: {ws_url}")

    # --- Build TwiML for Twilio ---
    response = VoiceResponse()

    # Start real-time stream
    start = Start()
    start.stream(url=ws_url, track="inbound")
    response.append(start)

    # Small pause before speaking
    response.pause(length=1)

    # Greeting message
    response.say(GREETING, voice="Polly.Matthew")

    # Keep the stream alive for 60s even if no DTMF input
    response.pause(length=60)

    # End call cleanup
    response.hangup()

    # Log + return
    xml_response = str(response)
    print(f"TwiML Sent: {xml_response}")
    return XMLResponse(content=xml_response, media_type="application/xml")



@app.post("/twiml_reply")
async def twiml_reply(text: str = Form(...)):
    """
    Twilio will fetch this TwiML when we want to play AI's reply.
    """
    from twilio.twiml.voice_response import VoiceResponse

    response = VoiceResponse()
    response.say(text, voice="Polly.Matthew")
    response.pause(length=1)
    response.redirect("https://siaara.clickites.com/plivo_answer")  # optional
    print(f"🎙️ TwiML Reply Sent: {text}")
    return XMLResponse(content=str(response), media_type="application/xml")


@app.post("/end_call")
def end_call_cleanup(CallSid: str = Form(None)):
    print(f"Call {CallSid} completed or timed out. Hanging up.")
    response = VoiceResponse()
    response.hangup()
    return XMLResponse(content=str(response), media_type="application/xml")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
