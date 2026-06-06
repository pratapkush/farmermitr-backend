import os, json, re, time, threading
import concurrent.futures
from collections import defaultdict
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

# ── Valid crop whitelist ─────────────────────────────────────────────────
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

# ── Valid Indian states and UTs ──────────────────────────────────────────
VALID_STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya",
    "mizoram", "nagaland", "odisha", "punjab", "rajasthan", "sikkim",
    "tamil nadu", "telangana", "tripura", "uttar pradesh", "uttarakhand",
    "west bengal", "andaman and nicobar islands", "chandigarh",
    "dadra and nagar haveli and daman and diu", "delhi", "jammu and kashmir",
    "ladakh", "lakshadweep", "puducherry"
}

# ── Official MSP 2024-25 (hardcoded for accuracy) ───────────────────────
MSP_2024_25 = {
    "wheat":      "Rs 2,275 per quintal",
    "gehun":      "Rs 2,275 per quintal",
    "rice":       "Rs 2,300 per quintal",
    "chawal":     "Rs 2,300 per quintal",
    "maize":      "Rs 2,090 per quintal",
    "makka":      "Rs 2,090 per quintal",
    "cotton":     "Rs 7,121 per quintal (medium staple)",
    "soybean":    "Rs 4,892 per quintal",
    "groundnut":  "Rs 6,783 per quintal",
    "moongfali":  "Rs 6,783 per quintal",
    "onion":      "No MSP — market determined price",
    "pyaz":       "No MSP — market determined price",
    "tomato":     "No MSP — market determined price",
    "tamatar":    "No MSP — market determined price",
    "potato":     "No MSP — market determined price",
    "aalu":       "No MSP — market determined price",
    "sugarcane":  "Rs 340 per quintal (FRP 2024-25)",
    "mustard":    "Rs 5,950 per quintal",
    "sarson":     "Rs 5,950 per quintal",
    "moong":      "Rs 8,682 per quintal",
    "matar":      "Rs 8,682 per quintal",
    "urad":       "Rs 7,400 per quintal",
    "arhar":      "Rs 7,550 per quintal",
    "toor":       "Rs 7,550 per quintal",
    "tur":        "Rs 7,550 per quintal",
    "bajra":      "Rs 2,625 per quintal",
    "jowar":      "Rs 3,371 per quintal",
    "ragi":       "Rs 4,290 per quintal",
    "sunflower":  "Rs 7,280 per quintal",
    "sesame":     "Rs 9,267 per quintal",
    "chickpea":   "Rs 5,440 per quintal",
    "chana":      "Rs 5,440 per quintal",
    "masoor":     "Rs 6,425 per quintal",
    "lentil":     "Rs 6,425 per quintal",
    "jute":       "Rs 5,335 per quintal",
    "copra":      "Rs 11,582 per quintal",
    "coconut":    "Rs 11,582 per quintal (copra)",
}

# ── Rate limiting ────────────────────────────────────────────────────────
request_counts = defaultdict(list)

def is_rate_limited(ip):
    now = time.time()
    request_counts[ip] = [t for t in request_counts[ip] if now - t < 60]
    if len(request_counts[ip]) >= 10:
        return True
    request_counts[ip].append(now)
    return False

# ── Prompt injection protection ──────────────────────────────────────────
INJECTION_PATTERNS = [
    "ignore", "forget", "pretend", "you are now", "new instructions",
    "system prompt", "jailbreak", "bypass", "override", "disregard",
    "act as", "roleplay", "simulate", "sudo", "admin"
]

def is_injection_attempt(text):
    text_lower = text.lower()
    return any(pattern in text_lower for pattern in INJECTION_PATTERNS)

# ── Crop validation ──────────────────────────────────────────────────────
def is_valid_crop(crop_name):
    normalized = crop_name.lower().strip()
    if normalized in VALID_CROPS:
        return True
    for valid in VALID_CROPS:
        if valid in normalized or normalized in valid:
            return True
    return False

# ── Robust JSON extractor ────────────────────────────────────────────────
def extract_json(text):
    # Strip markdown fences
    if "```" in text:
        parts = text.split("```")
        text = max(parts, key=len)
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    # Remove control characters
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    # Remove Unicode direction marks and zero-width characters
    text = re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]', '', text)
    # Replace smart quotes with straight quotes
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2018', "'").replace('\u2019', "'")

    # Extract only the JSON structure
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

    return json.loads(text)

# ── call_groq with fallback ──────────────────────────────────────────────
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
    print(f"Raw Groq response (first 300): {text[:300]}")

    try:
        return extract_json(text)
    except Exception as e:
        print(f"JSON parse failed: {e}")
        if fallback_language:
            print("Retrying in English as fallback...")
            english_prompt = prompt.replace(
                f"JSON values should be in {fallback_language} language",
                "JSON values should be in English"
            )
            body["messages"][1]["content"] = english_prompt
            r2 = requests.post(GROQ_URL, headers=headers, json=body, timeout=30)
            raw2 = r2.json()
            if "choices" not in raw2:
                raise Exception("Groq fallback also failed.")
            text2 = raw2["choices"][0]["message"]["content"].strip()
            return extract_json(text2)
        raise Exception("Could not parse AI response. Please try again.")

# ── search_prices ────────────────────────────────────────────────────────
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

# ── send_whatsapp ────────────────────────────────────────────────────────
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
Visit your nearest CSC center for help.
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

# ── Keep-alive ping (prevents Render free tier sleeping) ─────────────────
def keep_alive():
    while True:
        time.sleep(600)
        try:
            requests.get("https://farmermitr.onrender.com/", timeout=5)
            print("Keep-alive ping sent")
        except Exception as e:
            print(f"Keep-alive failed: {e}")

threading.Thread(target=keep_alive, daemon=True).start()

# ── CORS headers ─────────────────────────────────────────────────────────
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

# ── Health check endpoint ────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "groq_key":   bool(GROQ_KEY),
        "tavily_key": bool(TAVILY_KEY),
        "twilio_key": bool(TWILIO_SID),
        "version":    "3.0"
    })

# ── Main endpoint ────────────────────────────────────────────────────────
@app.route("/farmer", methods=["POST"])
def farmer():
    try:
        # Rate limit check
        ip = request.remote_addr
        if is_rate_limited(ip):
            return jsonify({
                "success": False,
                "error": "Too many requests. Please wait a minute and try again."
            }), 429

        d = request.get_json()

        # ── Presence checks ──────────────────────────────────────────────
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
        phone    = d.get("phone", "").strip()
        language = d.get("language", "English")

        # ── Length limits ────────────────────────────────────────────────
        if len(crop) > 50:
            return jsonify({"success": False, "error": "Crop name is too long."}), 400
        if len(district) > 100:
            return jsonify({"success": False, "error": "District name is too long."}), 400
        if len(state) > 100:
            return jsonify({"success": False, "error": "State name is too long."}), 400

        # ── Prompt injection check ───────────────────────────────────────
        if is_injection_attempt(crop) or is_injection_attempt(district):
            return jsonify({"success": False, "error": "Invalid input detected."}), 400

        # ── Phone validation ─────────────────────────────────────────────
        if phone and not re.match(r'^[6-9]\d{9}$', phone):
            return jsonify({
                "success": False,
                "error": "Please enter a valid 10-digit Indian mobile number starting with 6, 7, 8, or 9."
            }), 400

        # ── Land size validation ─────────────────────────────────────────
        try:
            land_float = float(land)
            if land_float <= 0 or land_float > 10000:
                return jsonify({
                    "success": False,
                    "error": "Land size must be between 0.1 and 10000 acres."
                }), 400
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "Land size must be a number."}), 400

        # ── District format check ────────────────────────────────────────
        if not re.match(r'^[a-zA-Z\s\-\.]+$', district):
            return jsonify({
                "success": False,
                "error": "District name should contain letters only."
            }), 400

        # ── State whitelist check ────────────────────────────────────────
        if state.lower() not in VALID_STATES:
            return jsonify({
                "success": False,
                "error": f"'{state}' is not a recognised Indian state or UT. Please select a valid state."
            }), 400

        # ── Crop whitelist check ─────────────────────────────────────────
        if not is_valid_crop(crop):
            return jsonify({
                "success": False,
                "error": f"'{crop}' does not appear to be a valid crop. Please enter a crop like wheat, rice, onion, tomato, cotton etc."
            }), 400

        print(f"[REQUEST] crop={crop} | district={district} | state={state} | language={language} | phone={bool(phone)}")

        # ── Search prices ────────────────────────────────────────────────
        price_text = search_prices(crop, district, state)
        print(f"[TAVILY] Price text length: {len(price_text)}")

        # ── Build prompts ────────────────────────────────────────────────
        fallback = language if language != "English" else None

        price_prompt = f"""Crop: {crop}, District: {district}, State: {state}, Land: {land} acres.
Market data: {price_text}

IMPORTANT RULES:
1. JSON keys must ALWAYS be in English
2. JSON values should be in {language} language
3. If '{crop}' is not a real agricultural crop grown in India, return: {{"error": "invalid_crop", "message": "Not a valid crop."}}
4. Return JSON only — no markdown, no extra text

Required format exactly:
{{
  "current_price_range": "value in {language}",
  "msp_2024": "value in {language}",
  "sell_advice": "value in {language}",
  "best_mandi": "value in {language}",
  "price_trend": "value in {language}",
  "action_urgency": "value in {language}"
}}"""

        price_system = "You are FarmerMitr, an Indian agriculture expert. Return valid JSON only. Keys must be in English. No markdown."

        scheme_prompt = f"""Farmer profile: crop={crop}, state={state}, land={land} acres, BPL card={bpl}.

IMPORTANT RULES:
1. JSON keys must ALWAYS be in English
2. JSON values should be in {language} language
3. Return a JSON array only — no markdown, no extra text

Required format exactly:
[
  {{
    "scheme_name": "name in {language}",
    "benefit_amount": "amount in {language}",
    "eligibility_reason": "reason in {language}",
    "how_to_apply": "steps in {language}",
    "deadline_note": "deadline in {language}"
  }}
]"""

        scheme_system = "You are a government scheme advisor for Indian farmers. Return a JSON array only. Keys must be in English. No markdown."

        # ── Parallel Groq calls (cuts response time in half) ─────────────
        with concurrent.futures.ThreadPoolExecutor() as executor:
            price_future  = executor.submit(call_groq, price_prompt,  price_system,  fallback)
            scheme_future = executor.submit(call_groq, scheme_prompt, scheme_system, fallback)
            price_data = price_future.result()
            schemes    = scheme_future.result()

        # ── Invalid crop check from AI ───────────────────────────────────
        if isinstance(price_data, dict) and price_data.get("error") == "invalid_crop":
            return jsonify({
                "success": False,
                "error": f"'{crop}' is not a valid agricultural crop. Please enter a crop like wheat, onion, rice, cotton etc."
            }), 400

        # ── Override MSP with hardcoded accurate value ───────────────────
        crop_lower = crop.lower()
        if crop_lower in MSP_2024_25:
            price_data["msp_2024"] = MSP_2024_25[crop_lower]
            print(f"[MSP] Overridden with official value: {MSP_2024_25[crop_lower]}")

        # ── WhatsApp delivery ────────────────────────────────────────────
        if phone and len(phone) == 10:
            send_whatsapp(phone, crop, district, price_data, schemes)

        print(f"[SUCCESS] crop={crop} | district={district}")

        return jsonify({
            "success": True,
            "crop":       crop,
            "district":   district,
            "price_data": price_data,
            "schemes":    schemes
        })

    except Exception as e:
        print(f"[ERROR] /farmer: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ── Home / health check ──────────────────────────────────────────────────
@app.route("/")
def home():
    return (f"FarmerMitr backend is running. "
            f"GROQ_KEY set: {bool(GROQ_KEY)}, "
            f"TAVILY_KEY set: {bool(TAVILY_KEY)}, "
            f"TWILIO_SID set: {bool(TWILIO_SID)}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
