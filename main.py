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

# ── Valid crop whitelist ────────────────────────────────────────────────
VALID_CROPS = {
    "wheat", "rice", "onion", "tomato", "potato", "cotton", "sugarcane",
    "maize", "soybean", "mustard", "groundnut", "turmeric", "chilli",
    "banana", "mango", "garlic", "ginger", "cauliflower", "cabbage",
    "brinjal", "okra", "ladyfinger", "spinach", "carrot", "radish",
    "pea", "lentil", "chickpea", "moong", "urad", "arhar", "toor",
    "jowar", "bajra", "ragi", "sunflower", "sesame", "jute", "tobacco",
    "tea", "coffee", "rubber", "coconut", "arecanut", "cashew",
    "orange", "grapes", "pomegranate", "guava", "papaya", "watermelon",
    "gehun", "chawal", "pyaz", "tamatar", "aalu", "makka", "sarson",
    "moongfali", "mirchi", "lahsun", "adrak", "gobhi", "baingan",
    "bhindi", "palak", "gajar", "matar", "chana", "masoor", "tur"
}

def is_valid_crop(crop_name):
    normalized = crop_name.lower().strip()
    if normalized in VALID_CROPS:
        return True
    for valid in VALID_CROPS:
        if valid in normalized or normalized in valid:
            return True
    return False

# ── FIX: Robust JSON extractor ──────────────────────────────────────────
def extract_json(text):
    """
    Aggressively cleans and extracts valid JSON from messy AI output.
    Handles regional language responses, markdown fences, extra text.
    """
    # Step 1 — Strip markdown code fences
    if "```" in text:
        parts = text.split("```")
        # Take the largest chunk — most likely the JSON
        text = max(parts, key=len)
        if text.startswith("json"):
            text = text[4:]

    text = text.strip()

    # Step 2 — Remove ALL problematic Unicode control/formatting characters
    # This is broader than before — catches regional language punctuation issues
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    # Remove Unicode direction marks and zero-width characters
    text = re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]', '', text)
    # Replace curly/smart quotes with straight quotes (common in AI output)
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2018', "'").replace('\u2019', "'")

    # Step 3 — Extract only the JSON structure (array or object)
    arr_start = text.find("[")
    obj_start = text.find("{")

    if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
        end = text.rfind("]") + 1
        text = text[arr_start:end]
    elif obj_start != -1:
        end = text.rfind("}") + 1
        text = text[obj_start:end]
    else:
        raise Exception("No JSON structure found in AI response.")

    # Step 4 — Parse
    return json.loads(text)

# ── call_groq with language-safe prompting ──────────────────────────────
def call_groq(prompt, system, fallback_language=None):
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

    # Retry on timeout
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
    print(f"Raw Groq response (first 300 chars): {text[:300]}")

    # FIX 3 — Try parsing, if it fails retry in English
    try:
        return extract_json(text)
    except (json.JSONDecodeError, Exception) as e:
        print(f"JSON parse failed for language response: {e}")
        # If a fallback English prompt is provided, retry silently in English
        if fallback_language:
            print("Retrying in English as fallback...")
            english_prompt = prompt.replace(
                f"Respond entirely in {fallback_language} language.",
                "Respond in English."
            )
            body["messages"][1]["content"] = english_prompt
            r2 = requests.post(GROQ_URL, headers=headers, json=body, timeout=30)
            raw2 = r2.json()
            if "choices" not in raw2:
                raise Exception("Groq fallback also failed.")
            text2 = raw2["choices"][0]["message"]["content"].strip()
            return extract_json(text2)
        raise Exception("Could not parse AI response. Please try again.")

# ── search_prices ───────────────────────────────────────────────────────
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

# ── send_whatsapp ───────────────────────────────────────────────────────
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

# ── CORS ────────────────────────────────────────────────────────────────
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

# ── Main endpoint ───────────────────────────────────────────────────────
@app.route("/farmer", methods=["POST"])
def farmer():
    try:
        d = request.get_json()

        if not d:
            return jsonify({"success": False, "error": "No data received."}), 400
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

        # Whitelist check
        if not is_valid_crop(crop):
            return jsonify({
                "success": False,
                "error": f"'{crop}' does not appear to be a valid crop. Please enter a crop like wheat, rice, onion, tomato, cotton etc."
            }), 400

        print(f"Request: crop={crop}, district={district}, state={state}, language={language}")

        price_text = search_prices(crop, district, state)

        # ── KEY FIX: JSON keys always in English, values in chosen language
        price_data = call_groq(
            f"""Crop: {crop}, District: {district}, State: {state}, Land: {land} acres.
Market data: {price_text}

IMPORTANT RULES:
1. JSON keys must ALWAYS be in English: current_price_range, msp_2024, sell_advice, best_mandi, price_trend, action_urgency
2. JSON values should be in {language} language
3. If '{crop}' is not a real agricultural crop, return: {{"error": "invalid_crop", "message": "Not a valid crop."}}
4. Return JSON only — no markdown, no extra text before or after

Example format:
{{
  "current_price_range": "value in {language}",
  "msp_2024": "value in {language}",
  "sell_advice": "value in {language}",
  "best_mandi": "value in {language}",
  "price_trend": "value in {language}",
  "action_urgency": "value in {language}"
}}""",
            "You are FarmerMitr, an Indian agriculture expert. Return valid JSON only. Keys must be in English. No markdown.",
            fallback_language=language if language != "English" else None
        )

        # Check if AI flagged invalid crop
        if isinstance(price_data, dict) and price_data.get("error") == "invalid_crop":
            return jsonify({
                "success": False,
                "error": f"'{crop}' is not a valid agricultural crop. Please enter a crop like wheat, onion, rice, cotton etc."
            }), 400

        schemes = call_groq(
            f"""Farmer profile: crop={crop}, state={state}, land={land} acres, BPL card={bpl}.

IMPORTANT RULES:
1. JSON keys must ALWAYS be in English: scheme_name, benefit_amount, eligibility_reason, how_to_apply, deadline_note
2. JSON values should be in {language} language
3. Return a JSON array only — no markdown, no extra text

Example format:
[
  {{
    "scheme_name": "name in {language}",
    "benefit_amount": "amount in {language}",
    "eligibility_reason": "reason in {language}",
    "how_to_apply": "steps in {language}",
    "deadline_note": "deadline in {language}"
  }}
]""",
            "You are a government scheme advisor for Indian farmers. Return a JSON array only. Keys must be in English. No markdown.",
            fallback_language=language if language != "English" else None
        )

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

# ── Health check ────────────────────────────────────────────────────────
@app.route("/")
def home():
    return (f"FarmerMitr backend is running. "
            f"GROQ_KEY set: {bool(GROQ_KEY)}, "
            f"TAVILY_KEY set: {bool(TAVILY_KEY)}, "
            f"TWILIO_SID set: {bool(TWILIO_SID)}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
