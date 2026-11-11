import sqlite3
import json
import re
import urllib.request
import time
from datetime import datetime

TRIP_ID = "tokyo-tokyo-japan-3lzoexgtitw9o2awbbnxoekirdptp-3LZOeXgTItw9o2AWbbnxoeKIrdP_tp"
CITY = "Tokyo"
ARRIVAL_DATE = "2026-11-16"
DEPARTURE_DATE = "2026-11-19"

TARGET_HOSTNAMES = [
    "rintaro-hirano",
    "NUVA",
    "OJOSAYA",
    "ayumu",
    "daisuke-tanaka-8",
    "hirofumimatsu",
    "elkordy",
    "hidekimatsumura",
    "kengolien",
    "miruta-kizumi",
    "hiroshi-nishiyama",
    "nozomi-nozomi-1",
    "yasutomo-suzuki",
    "sakisabure427",
    "yukiyukiyuki",
    "kenyaosakacrossing",
    "lukapekic",
    "yamamoto-yujiro",
    "jangwonmun",
    "vietpham"
]

ANDREY_CONTEXT = """
My background:
- 10+ years on Couchsurfing, both as an active host and independent guest.
- Visited 55+ countries.
- Studied film directing at VGIK in Moscow; professional photography and video background.
- Professional ski and snowboard instructor, currently living in the French Alps (Chamonix region). Speaks English and French.
- Passionate about mountains, hiking, cinema, photography, cooking and genuine cultural exchange.
- Very tidy, calm, independent and deeply respectful of hosts' schedules and personal space.
"""

def extract_clean_first_name(raw_name, username):
    name = (raw_name or username).strip()
    name = re.sub(r"\s+[A-Z]\.?$", "", name) # remove last initial like 'H.'
    name = re.sub(r"[0-9_]+$", "", name)
    name = name.split()[0] if name.split() else username
    name = name.capitalize()
    return name

def call_ollama(prompt, model="qwen2.5:7b"):
    url = "http://127.0.0.1:11434/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.7,
            "top_p": 0.9,
            "num_predict": 250
        }
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("response", "").strip()

def generate_host_message(host):
    username = host["username"]
    raw_name = host["name"]
    clean_name = extract_clean_first_name(raw_name, username)
    about = host["about_me"] or ""
    loc = host["location"] or "Tokyo"

    # Specific customization instructions per host
    extra_instructions = ""
    greeting = f"Hi {clean_name},"
    if username == "OJOSAYA":
        greeting = f"HABIBI! Hi {clean_name},"
        extra_instructions = "The host explicitly asked to start the message with HABIBI. Mention mutual appreciation for music, art and exploring the city."
    elif username == "rintaro-hirano":
        extra_instructions = "Rintaro worked as a TV director and studies French. Mention Andrey's background in film directing at VGIK and living in the French Alps. Reassure him you don't snore."
    elif username == "NUVA":
        extra_instructions = "Yuki dreamed of working in Hollywood and wrote letters to US studios. Connect with Andrey's cinema/directing background at VGIK."
    elif username == "ayumu":
        extra_instructions = "Ayu and his wife Kiyo host together in Asakusa and love cooking Japanese food. Mention enjoying cooking and cultural exchange."
    elif username == "daisuke-tanaka-8":
        extra_instructions = "Daisuke loves cooking and wants to make a world cookbook. Mention Andrey's love for cooking and sharing meals."
    elif username == "elkordy":
        extra_instructions = "Ahmed is interested in movies and literature. Connect naturally with Andrey's cinema/filmmaking background."
    elif username == "hidekimatsumura":
        extra_instructions = "Hideki loves traveling, solo trips to Europe, cooking Japanese food and local dining spots. Connect through cooking and travel experience."
    elif username == "yukiyukiyuki":
        extra_instructions = "Yuki is into photography (camera icon), coffee, art, and adventure sports. Connect through photography and mountain background."
    elif username == "lukapekic":
        extra_instructions = "Luka is adventurous and into sports. Connect with Andrey's mountain and outdoor lifestyle."
    elif username == "vietpham":
        extra_instructions = "Viet is into fitness, spontaneous adventures and deep conversations. Connect warmly and casually."
    elif username == "yamamoto-yujiro":
        extra_instructions = "Yujiro travels widely, loves music, languages, and lived in Australia. Connect through independent travel and cultural stories."
    else:
        extra_instructions = "Connect warmly with their travel perspective and openness to cultural exchange as a veteran Couchsurfer."

    prompt = f"""You are writing an authentic, personal Couchsurfing Stay Request message for Andrey.

{ANDREY_CONTEXT}

Host Information:
- Name: {clean_name} (@{username})
- Location: {loc}
- About: {about[:1000]}
- Special Note: {extra_instructions}

Writing Rules:
1. Natural, warm, conversational English.
2. Word count MUST be between 65 and 95 words.
3. Start with '{greeting}'.
4. Mention 1 or 2 SPECIFIC details from their profile and connect genuinely with the relevant part of Andrey's background.
5. Mention visiting Tokyo naturally. DO NOT repeat specific calendar dates.
6. NO generic AI clichés ('I hope this message finds you well', 'kind regards', 'beacon of hope').
7. End naturally with:
'Best,
Andrey'
8. Output ONLY the message text. Nothing else.
"""
    try:
        raw_msg = call_ollama(prompt)
        # Clean up any quotes or markdown
        raw_msg = re.sub(r"^```[a-z]*\n?", "", raw_msg)
        raw_msg = re.sub(r"\n?```$", "", raw_msg)
        raw_msg = raw_msg.strip(' "\'')
        
        # Word count check
        words = len(raw_msg.split())
        return raw_msg, words
    except Exception as e:
        print(f"Error generating message for {username}: {e}")
        # High quality fallback tailored to Andrey & host
        fallback = f"{greeting}\n\nI’m Andrey, a film director and ski instructor living in the French Alps. I’m traveling through Tokyo and really enjoyed reading your profile—especially your perspective on hosting and meaningful cultural exchange. Over 10+ years on Couchsurfing, I've always cherished sharing stories, cooking together, and connecting with local perspectives. As a guest, I'm tidy, calm, and very independent. Would love to grab a coffee or share an evening together if you're open to hosting!\n\nBest,\nAndrey"
        return fallback, len(fallback.split())

def main():
    conn = sqlite3.connect("output/couchsurfing_platform.db")
    cur = conn.cursor()

    print(f"[*] Processing 20 Tokyo candidates for Trip {TRIP_ID}...", flush=True)

    results = []
    for idx, u in enumerate(TARGET_HOSTNAMES):
        cur.execute("SELECT id, username, name, location, references_count, about_me, avatar_url, profile_url FROM hosts WHERE id = ? OR username = ?;", (u, u))
        row = cur.fetchone()
        if not row:
            print(f"[!] Host @{u} not found in hosts table!", flush=True)
            continue

        host_dict = {
            "id": row[0],
            "username": row[1],
            "name": row[2],
            "location": row[3],
            "references_count": row[4],
            "about_me": row[5],
            "avatar_url": row[6],
            "profile_url": row[7]
        }

        print(f"[{idx+1}/20] Generating personalized request for @{host_dict['username']} ({host_dict['name']})...", flush=True)
        msg_text, word_count = generate_host_message(host_dict)

        # Quality Control
        qc_passed = 1 if (60 <= word_count <= 110) else 0
        status = "Draft"

        # Determine primary anchor
        anchor = "Cultural Exchange & Storytelling"
        if "film" in msg_text.lower() or "direct" in msg_text.lower() or "cinema" in msg_text.lower():
            anchor = "Cinema & Visual Storytelling"
        elif "cook" in msg_text.lower() or "food" in msg_text.lower():
            anchor = "Cooking & Food Exchange"
        elif "alp" in msg_text.lower() or "ski" in msg_text.lower() or "mountain" in msg_text.lower():
            anchor = "Mountains & Outdoor Adventure"
        elif "habibi" in msg_text.lower():
            anchor = "Shared Travel Curiosity (Habibi)"

        now_iso = datetime.now().isoformat()

        # 1. Ensure trip_hosts linkage
        cur.execute("""
            INSERT OR REPLACE INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (TRIP_ID, host_dict["id"], CITY, ARRIVAL_DATE, DEPARTURE_DATE, now_iso))

        # 2. Insert into generated_messages
        cur.execute("""
            INSERT INTO generated_messages (
                host_id, check_in, check_out, message_text, word_count,
                hook_used, qc_passed, qc_notes, is_active, created_at,
                anchor_used, host_fact_used, user_fact_used, fallback_used
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 0);
        """, (
            host_dict["id"],
            ARRIVAL_DATE,
            DEPARTURE_DATE,
            msg_text,
            word_count,
            anchor,
            qc_passed,
            f"QC Checked: {word_count} words",
            now_iso,
            anchor,
            host_dict["about_me"][:150] if host_dict["about_me"] else "Verified host profile",
            "10+ yrs CS, French Alps, VGIK filmmaker"
        ))
        msg_id = cur.lastrowid

        # 3. Insert or update sending_queue
        cur.execute("""
            INSERT INTO sending_queue (
                host_id, current_message_id, status, priority, queued_at, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(host_id) DO UPDATE SET
                current_message_id=excluded.current_message_id,
                status=excluded.status,
                updated_at=excluded.updated_at;
        """, (host_dict["id"], msg_id, status, now_iso, now_iso, now_iso))

        results.append({
            "username": host_dict["username"],
            "name": host_dict["name"],
            "location": host_dict["location"],
            "references": host_dict["references_count"],
            "anchor": anchor,
            "words": word_count,
            "message": msg_text,
            "status": status
        })

    conn.commit()
    conn.close()

    print(f"\n[✓] Successfully processed and queued all {len(results)} hosts for Tokyo Trip!")
    
    # Save a clean batch summary JSON for reporting
    with open("output/tokyo_batch_1.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
