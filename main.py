import os, json, re
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type"], methods=["GET", "POST", "OPTIONS"])

GROQ_KEY     = os.environ.get("GROQ_KEY", "")
TAVILY_KEY   = os.environ.get("TAVILY_KEY", "")
TWILIO_SID   = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM  = os.environ.get("TWILIO_FROM", "whatsapp:+14155238886")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

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
            {"role": "user", "content": prompt}
        ]
    }

    # ── FIX 3: Retry logic — tries up to 3 times on timeout ───────────────
    for attempt in range(3):
        try:
            r = requests.post(GROQ_URL, headers=headers, json=body, timeout=30)
            break
        except requests.Timeout:
            if attempt == 2:
                raise Exception("Groq timed out after 3 attempts. Please try again.")
            print(f"Groq timeout attempt {attempt+1}, retrying...")
    # ─────────────────────────────────────────────────────────────────────

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

    # Extract only the JSON part — ignore any text before/after
    arr_start = text.find("[")
    obj_start = text.find("{")

    if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
        end = text.rfind("]") + 1
        text = text[arr_start:end]
    elif obj_start != -1:
        end = text.rfind("}") + 1
        text = text[obj_start:end]

    # Remove control characters that break JSON parsing in non-English languages
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"JSON parse failed: {e}")
        print(f"Problematic text: {text[:300]}")
        raise Exception("Could not parse AI response. Please try again.")

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
        print(f"Twilio response status: {response.status_code}")
        print(f"Twilio response body: {response.text}")
        return True
    except Exception as e:
        print(f"WhatsApp error: {e}")
        return False

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

@app.route("/farmer", methods=["POST"])
def farmer():
    try:
        d = request.get_json()

        # ── FIX 4: Validate required fields ──────────────────────────────
        if not d:
            return jsonify({"success": False, "error": "No data received. Please try again."}), 400
        if not d.get("crop", "").strip():
            return jsonify({"success": False, "error": "Crop name is required."}), 400
        if not d.get("state", "").strip():
            return jsonify({"success": False, "error": "State is required."}), 400
        if not d.get("district", "").strip():
            return jsonify({"success": False, "error": "District is required."}), 400
        # ─────────────────────────────────────────────────────────────────

        crop     = d.get("crop", "wheat").strip()
        district = d.get("district", "").strip()
        state    = d.get("state", "").strip()
        land     = d.get("land_acres", "1")
        bpl      = d.get("bpl_card", "no")
        phone    = d.get("phone", "")
        language = d.get("language", "English")

        print(f"Request: crop={crop}, district={district}, state={state}, language={language}")

        price_text = search_prices(crop, district, state)
        print(f"Price text length: {len(price_text)}")

        price_data = call_groq(
            f"Crop: {crop}, District: {district}, State: {state}, Land: {land} acres.\nMarket data: {price_text}\nReturn JSON only with keys: current_price_range, msp_2024, sell_advice, best_mandi, price_trend, action_urgency.\nRespond entirely in {language} language.",
            "You are FarmerMitr. Return valid JSON only. No markdown, no extra text."
        )

        schemes = call_groq(
            f"Farmer: crop={crop}, state={state}, land={land} acres, BPL={bpl}.\nReturn a JSON array of matching Indian government schemes. Each item: scheme_name, benefit_amount, eligibility_reason, how_to_apply, deadline_note. Respond entirely in {language} language.",
            "You are a government scheme advisor for Indian farmers. Return a JSON array only. No markdown, no extra text."
        )

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

@app.route("/")
def home():
    return f"FarmerMitr backend is running. GROQ_KEY set: {bool(GROQ_KEY)}, TAVILY_KEY set: {bool(TAVILY_KEY)}, TWILIO_SID set: {bool(TWILIO_SID)}"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
