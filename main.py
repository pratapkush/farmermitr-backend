import os, requests, json
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


GROQ_KEY = os.environ.get("GROQ_KEY", "")
TAVILY_KEY = os.environ.get("TAVILY_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

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
    r = requests.post(GROQ_URL, headers=headers, json=body, timeout=30)
    raw = r.json()
    print(f"Groq raw response: {raw}")

    if "choices" not in raw:
        raise Exception(f"Groq error: {raw.get('error', {}).get('message', str(raw))}")

    text = raw["choices"][0]["message"]["content"].strip()
    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else parts[0]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())

@app.route("/farmer", methods=["POST"])
def farmer():
    try:
        d = request.get_json()
        crop = d.get("crop", "wheat")
        district = d.get("district", "")
        state = d.get("state", "")
        land = d.get("land_acres", "1")
        bpl = d.get("bpl_card", "no")
        language = d.get("language", "English")

        print(f"Request: crop={crop}, district={district}, state={state}")
        print(f"GROQ_KEY present: {bool(GROQ_KEY)}, TAVILY_KEY present: {bool(TAVILY_KEY)}")

        price_text = search_prices(crop, district, state)
        print(f"Price text length: {len(price_text)}")

        price_data = call_groq(
            f"Crop: {crop}, District: {district}, State: {state}, Land: {land} acres.\nMarket data: {price_text}\nReturn JSON only with keys: current_price_range, msp_2024, sell_advice, best_mandi, price_trend, action_urgency.\nRespond entirely in {language} language.",
            "You are FarmerMitr. Return valid JSON only. No markdown, no extra text."
        )

        schemes = call_groq(
            f"Farmer: crop={crop}, state={state}, land={land} acres, BPL={bpl}.\nReturn a JSON array of matching Indian government schemes. Each item: scheme_name, benefit_amount, eligibility_reason, how_to_apply, deadline_note",
            "You are a government scheme advisor for Indian farmers. Return a JSON array only. No markdown, no extra text."
        )

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
    return f"FarmerMitr backend is running. GROQ_KEY set: {bool(GROQ_KEY)}, TAVILY_KEY set: {bool(TAVILY_KEY)}"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
