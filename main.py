import os, json, re
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type"], methods=["GET", "POST", "OPTIONS"])

# ── API Keys ────────────────────────────────────────────────────────────
GROQ_KEY     = os.environ.get("GROQ_KEY", "")
TAVILY_KEY   = os.environ.get("TAVILY_KEY", "")
TWILIO_SID   = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM  = os.environ.get("TWILIO_FROM", "whatsapp:+14155238886")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

# ── LEVEL 2 FIX: Valid crop whitelist ──────────────────────────────────
VALID_CROPS = {
    "wheat", "rice", "onion", "tomato", "potato", "cotton", "sugarcane",
    "maize", "soybean", "mustard", "groundnut", "turmeric", "chilli",
    "banana", "mango", "garlic", "ginger", "cauliflower", "cabbage",
    "brinjal", "okra", "ladyfinger", "spinach", "carrot", "radish",
    "pea", "lentil", "chickpea", "moong", "urad", "arhar", "toor",
    "jowar", "bajra", "ragi", "sunflower", "sesame", "jute", "tobacco",
    "tea", "coffee", "rubber", "coconut", "arecanut", "cashew",
    "orange", "grapes", "pomegranate", "guava", "papaya", "watermelon",
    # Hindi/regional names also accepted
    "gehun", "chawal", "pyaz", "tamatar", "aalu", "makka", "sarson",
    "moongfali", "mirchi", "lahsun", "adrak", "gobhi", "baingan",
    "bhindi", "palak", "gajar", "matar", "chana", "masoor", "tur"
}

def is_valid_crop(crop_name):
    """Check if the input is a real crop name."""
    normalized = crop_name.lower().strip()
    # Direct match in whitelist
    if normalized in VALID_CROPS:
        return True
    # Partial match — e.g. "red onion", "basmati rice", "bt cotton"
    for valid in VALID_CROPS:
        if valid in normalized or normalized in valid:
            return True
    return False

# ── LEVEL 1 FUNCTION: search_prices ────────────────────────────────────
def search_prices(crop, district, state):
    try:
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_KEY,
            "query": f"{crop} mandi price today {district} {state} APMC",
            "search_depth": "basic",
            "max_results": 5
        }, timeout=15)
        results = r.json().get("results", [])
        return " ".join([x.get("content", "") for x in results])[:2000]
    except Exception as e:
        print(f"Tavily error: {e}")
        return f"No live data found. Use general knowledge for {crop} in {district}."

# ── CORE FUNCTION: call_groq ────────────────────────────────────────────
def call_groq(prompt, system):
    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 800,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt}
        ]
    }

    # Retry logic — tries up to 3 times on timeout
    for attempt in range(3):
        try:
            r = requests.post(GROQ_URL, headers=headers, json=body, timeout=30)
            break
        except requests.Timeout:
            if attempt == 2:
                raise Exception("Groq timed out after 3 attempts. Please try again.")
            print(f"Groq timeout attempt {attempt+1}, retrying...")

    raw = r.json()

    if "choices" not in raw:
        raise Exception(f"Groq error: {raw.get('error', {}).get('message', str(raw))}")

    text = raw["choices"][0]["message"]["content"].strip()

    # Strip markdown code fences
    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else parts[0]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    # Extract only the JSON part
    arr_start = text.find("[")
    obj_start = text.find("{")

    if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
        end = text.rfind("]") + 1
        text = text[arr_start:end]
    elif obj_start != -1:
        end = text.rfind("}") + 1
        text = text[obj_start:end]

    # Remove control characters
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"JSON parse failed: {e}")
        print(f"Problematic text: {text[:300]}")
        raise Exception("Could not parse AI response. Please try again.")

# ── WHATSAPP FUNCTION ───────────────────────────────────────────────────
def send_whatsapp(phone, crop, district, price_data, schemes):
    try:
        price_range = price_data.get('current_price_range', 'N/A')
        best_mandi  = price_data.get('best_mandi', 'N/A')
        sell_advice = price_data.get('sell_advice', 'N/A')
        trend       = price_data.get('price_trend', 'N/A')

        scheme_lines = ""
        for i, s in enumerate(schemes[:3], 1):
            scheme_lines += f"\n{i}. {s.get('scheme_name','')}\n   Benefit: {s.get('benefit_amount','')}\n"

        message = f"""FarmerMitr Report

Crop: {crop} | District: {district}

Today's Price
Best Mandi: {best_mandi}
Price Range: {price_range}
Trend: {trend}
Advice: {sell_advice}

Government Schemes{scheme_lines}
Visit your nearest CSC center for help applying.
Helpline: 1800-180-1551 (Kisan Call Centre)"""

        response = requests.post(
            f'https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json',
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={
                'From': TWILIO_FROM,
                'To':   f'whatsapp:+91{phone.strip()}',
                'Body': message
            },
            timeout=10
        )
        print(f"Twilio response: {response.status_code}")
        return True
    except Exception as e:
        print(f"WhatsApp error: {e}")
        return False

# ── CORS HEADERS ────────────────────────────────────────────────────────
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

# ── MAIN ENDPOINT ───────────────────────────────────────────────────────
@app.route("/farmer", methods=["POST"])
def farmer():
    try:
        d = request.get_json()

        # Basic presence validation
        if not d:
            return jsonify({"success": False, "error": "No data received. Please try again."}), 400
        if not d.get("crop", "").strip():
            return jsonify({"success": False, "error": "Crop name is required."}), 400
        if not d.get("state", "").strip():
            return jsonify({"success": False, "error": "State is required."}), 400
        if not d.get("district", "").strip():
            return jsonify({"success": False, "error": "District is required."}), 400

        crop     = d.get("crop", "").strip()
        district = d.get("district", "").strip()
        state    = d.get("state", "").strip()
        land     = d.get("land_acres", "1")
        bpl      = d.get("bpl_card", "no")
        phone    = d.get("phone", "")
        language = d.get("language", "English")

        # ── LEVEL 2 FIX: Whitelist check ──────────────────────────────
        if not is_valid_crop(crop):
            return jsonify({
                "success": False,
                "error": f"'{crop}' does not appear to be a valid crop name. Please enter a crop like wheat, rice, onion, tomato, cotton etc."
            }), 400
        # ──────────────────────────────────────────────────────────────

        print(f"Request: crop={crop}, district={district}, state={state}, language={language}")

        price_text = search_prices(crop, district, state)
        print(f"Price text length: {len(price_text)}")

        # ── LEVEL 3 FIX: AI prompt guard added ────────────────────────
        price_data = call_groq(
            f"""Crop: {crop}, District: {district}, State: {state}, Land: {land} acres.
Market data: {price_text}

IMPORTANT: If '{crop}' is not a real agricultural crop grown in India, return exactly:
{{"error": "invalid_crop", "message": "This does not appear to be a valid crop."}}

Otherwise return JSON only with keys: current_price_range, msp_2024, sell_advice, best_mandi, price_trend, action_urgency.
Respond entirely in {language} language.""",
            "You are FarmerMitr, an expert on Indian agriculture. Return valid JSON only. No markdown, no extra text."
        )
        # ──────────────────────────────────────────────────────────────

        # Check if AI itself flagged it as invalid crop
        if isinstance(price_data, dict) and price_data.get("error") == "invalid_crop":
            return jsonify({
                "success": False,
                "error": f"'{crop}' is not a valid agricultural crop. Please enter a crop name like wheat, onion, rice, cotton etc."
            }), 400

        schemes = call_groq(
            f"""Farmer: crop={crop}, state={state}, land={land} acres, BPL={bpl}.
Return a JSON array of matching Indian government schemes. Each item: scheme_name, benefit_amount, eligibility_reason, how_to_apply, deadline_note.
Respond entirely in {language} language.""",
            "You are a government scheme advisor for Indian farmers. Return a JSON array only. No markdown, no extra text."
        )

        # Send WhatsApp only if phone number provided
        if phone and len(phone.strip()) >= 10:
            send_whatsapp(phone, crop, district, price_data, schemes)

        return jsonify({
            "success": True,
            "crop": crop,
            "district": district,
            "price_data": price_data,
            "schemes": schemes
        })

    except Exception as e:
        print(f"Error in /farmer: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ── HEALTH CHECK ────────────────────────────────────────────────────────
@app.route("/")
def home():
    return (f"FarmerMitr backend is running. "
            f"GROQ_KEY set: {bool(GROQ_KEY)}, "
            f"TAVILY_KEY set: {bool(TAVILY_KEY)}, "
            f"TWILIO_SID set: {bool(TWILIO_SID)}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
