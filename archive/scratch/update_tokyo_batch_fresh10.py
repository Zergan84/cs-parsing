import sqlite3
import json
from pathlib import Path
from datetime import datetime

DB_PATH = Path("output/couchsurfing_platform.db")
TOKYO_TRIP_ID = "tokyo-tokyo-japan-3lzoexgtitw9o2awbbnxoekirdptp-3LZOeXgTItw9o2AWbbnxoeKIrdP_tp"

# 10 completely fresh, uncontacted candidates for Tokyo Draft batch:
FRESH_10 = [
    "daisuke-tanaka-8",
    "hirofumimatsu",
    "hidekimatsumura",
    "miruta-kizumi",
    "hiroshi-nishiyama",
    "nozomi-nozomi-1",
    "yamamoto-yujiro",
    "jangwonmun",
    "vietpham",
    "daichi-akiba"
]

EXCLUDED = [
    "rintaro-hirano",
    "NUVA",
    "OJOSAYA",
    "ayumu",
    "elkordy",
    "yasutomo-suzuki",
    "sakisabure427",
    "yukiyukiyuki",
    "kenyaosakacrossing",
    "lukapekic",
    "kengolien"  # Excluded because active dialogue exists in inbox_conversations
]

REVISED_MESSAGES = {
    "daisuke-tanaka-8": {
        "text": (
            "Hi Daisuke,\n\n"
            "I was intrigued to read about your transportation research in Bochum and your goal of creating a "
            "cookbook from your travels. I’m based in the French Alps and love cooking, so I would gladly prepare "
            "an Alpine meal for you and trade recipes if you’d like. I'm visiting Tokyo to explore the city, "
            "and I think it would be great to catch up about your time in Europe over dinner.\n\n"
            "Best,\n"
            "Andrey"
        ),
        "anchor": "Bochum mobility research & world cookbook project"
    },
    "hirofumimatsu": {
        "text": (
            "Hi Hirofumi,\n\n"
            "Your profile caught my eye, especially your offer to make a home-cooked Japanese dinner in exchange "
            "for snacks and beer from around the world. I live in the French Alps and would happily bring some "
            "local European treats for you to try. I’m spending a few days in Tokyo and would really enjoy "
            "hearing your perspective on the city over an evening meal. And thanks for the tip about picking up a Suica card!\n\n"
            "Best,\n"
            "Andrey"
        ),
        "anchor": "Beer/snacks exchange for home-cooked meal & Suica card advice"
    },
    "hidekimatsumura": {
        "text": (
            "Hi Hideki,\n\n"
            "Visiting all 47 prefectures in Japan is quite an achievement, and I appreciated your story about "
            "how your solo trip to Spain in 2012 helped you become more outgoing. As someone who has traveled "
            "widely and now lives in the French Alps, I really respect that mindset. I’m coming to Tokyo to "
            "explore the city, and staying in a quieter neighborhood like Meguro would be ideal. I’d love to "
            "hear your favorite hidden spots across Japan.\n\n"
            "Best,\n"
            "Andrey"
        ),
        "anchor": "47 prefectures traveled & 2012 solo trip to Spain"
    },
    "miruta-kizumi": {
        "text": (
            "Hi Taka,\n\n"
            "Your honest, self-deprecating introduction made me smile. I'm heading to Tokyo to wander through the "
            "city and take in the neighborhood atmosphere, and your relaxed approach to hosting sounds like a "
            "breath of fresh air. I live in the French Alps and spend most of my time outdoors or working on film "
            "projects, so unwinding over a cold Japanese beer and swapping travel stories after a day out sounds great.\n\n"
            "Best,\n"
            "Andrey"
        ),
        "anchor": "Self-deprecating humor & unwinding over Japanese beer"
    },
    "hiroshi-nishiyama": {
        "text": (
            "Hi Hirochan,\n\n"
            "Seeing your passion for Mexican gastronomy and your love for Mexico was a pleasant surprise on a "
            "Tokyo profile. I’ve traveled through Latin America myself and have a soft spot for Mexican cuisine, "
            "so hearing you cook it at home in Ota City immediately caught my attention. I’m visiting Tokyo to "
            "explore different neighborhoods, and it would be a pleasure to meet someone with such distinct "
            "interests and chat about travel.\n\n"
            "Best,\n"
            "Andrey"
        ),
        "anchor": "Mexican gastronomy, fluent Spanish & cooking in Ota City"
    },
    "nozomi-nozomi-1": {
        "text": (
            "Hi Nozomi,\n\n"
            "Preparing for an indefinite solo journey around the world starting this spring is a bold and "
            "exciting chapter. Having traveled through more than fifty countries over the years before basing "
            "myself in the French Alps, I know that mix of anticipation and curiosity well. I’m passing through "
            "Tokyo to explore the city, and I’d be delighted to meet up, share a casual drink, and talk about "
            "navigating life on the road.\n\n"
            "Best,\n"
            "Andrey"
        ),
        "anchor": "Indefinite solo world journey starting May 2025"
    },
    "yamamoto-yujiro": {
        "text": (
            "Hi Yamamoto,\n\n"
            "I liked reading that you split your time between Tokyo and Osaka, and that your place in Tokyo sits "
            "right next to a natural green area. Coming from the French Alps, having trees nearby while still having "
            "easy access to central Tokyo sounds wonderful. I work in film directing and photography, and I’m "
            "visiting Tokyo to take in both urban and quiet corners. It would be great to meet and hear about "
            "your dual-city lifestyle.\n\n"
            "Best,\n"
            "Andrey"
        ),
        "anchor": "Tokyo/Osaka split life & green natural area in Tokyo"
    },
    "jangwonmun": {
        "text": (
            "Hi Jangwon,\n\n"
            "I noticed that you enjoy both movies and hiking, which are two central parts of my own life. "
            "I studied film directing in Moscow and currently work as a ski coach in the French Alps, so your "
            "profile felt like a natural fit. I’m heading to Tokyo to explore the city and would appreciate a "
            "quiet stay in Arakawa, along with the chance to talk cinema and mountain trails in the evening.\n\n"
            "Best,\n"
            "Andrey"
        ),
        "anchor": "Shared cinema/movies & hiking passions in Arakawa"
    },
    "vietpham": {
        "text": (
            "Hi Viet,\n\n"
            "Your focus on fitness, entrepreneurship, and finding local spots off the beaten path sounded "
            "refreshingly grounded. I work as an alpine ski instructor in France and studied film, so staying "
            "active and having meaningful conversations are both very important to me. I’m visiting Tokyo to see "
            "the city beyond the usual tourist highlights, and I’d really enjoy meeting up, hearing about what "
            "you're working on, and exchanging stories over good food.\n\n"
            "Best,\n"
            "Andrey"
        ),
        "anchor": "Fitness, entrepreneurship & off-the-beaten-path exploration"
    },
    "daichi-akiba": {
        "text": (
            "Hi Daichi,\n\n"
            "I noticed that you lived and studied in Los Angeles and now work as an IT engineer in Tokyo, "
            "alongside playing guitar and piano. As someone who has spent years traveling through fifty-plus "
            "countries and is now based in the French Alps coaching skiing, I really value that open, creative "
            "perspective. I’m visiting Tokyo to see the city and would appreciate a quiet, respectful stay while "
            "sharing a few travel stories in the evening.\n\n"
            "Best,\n"
            "Andrey"
        ),
        "anchor": "Studied in LA, music & Tokyo IT engineering"
    },
    "kengolien": {
        "text": (
            "Hi Kengo,\n\n"
            "I noticed on your profile that you want to ski abroad and are planning a trip through Europe. "
            "I’m actually a ski and snowboard coach living in the French Alps, so if you are looking for "
            "honest recommendations for resorts in France or Switzerland, I would be very happy to share them. "
            "I'm visiting Tokyo for a few days and would love to grab a drink and talk winter sports and travel.\n\n"
            "Best,\n"
            "Andrey"
        ),
        "anchor": "Skiing abroad goal & European travel planning"
    }
}

def run_update():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    now_iso = datetime.now().isoformat()

    # 1. Update generated_messages and sending_queue for FRESH_10 (and kengolien)
    updated_items = []
    all_to_process = FRESH_10 + ["kengolien"]
    for username in all_to_process:
        info = REVISED_MESSAGES[username]
        msg_text = info["text"]
        words = len(msg_text.split())
        anchor = info["anchor"]

        # Check existing message ID in sending_queue
        c.execute("SELECT current_message_id FROM sending_queue WHERE host_id = ?", (username,))
        row = c.fetchone()
        msg_id = row["current_message_id"] if row else None

        if not msg_id:
            msg_id = f"msg_{username}_revised"
            c.execute("""
                INSERT INTO generated_messages (
                    id, host_id, check_in, check_out, message_text, word_count,
                    hook_used, anchor_used, qc_passed, qc_notes, is_active, created_at
                ) VALUES (?, ?, '2026-11-16', '2026-11-19', ?, ?, ?, ?, 1, 'Revised natural personalized text', 1, ?)
            """, (msg_id, username, msg_text, words, anchor, anchor, now_iso))
        else:
            c.execute("""
                UPDATE generated_messages
                SET message_text = ?, word_count = ?, anchor_used = ?, hook_used = ?, qc_passed = 1,
                    qc_notes = 'Revised natural personalized text', is_active = 1
                WHERE id = ? OR (host_id = ? AND id IS NULL)
            """, (msg_text, words, anchor, anchor, msg_id, username))

        if username in FRESH_10:
            c.execute("""
                INSERT OR REPLACE INTO sending_queue (
                    host_id, current_message_id, status, priority, created_at, updated_at
                ) VALUES (?, ?, 'Draft', 1, ?, ?)
            """, (username, msg_id, now_iso, now_iso))

            # Get host metadata
            c.execute("SELECT name, location, references_count FROM hosts WHERE id = ? OR username = ?", (username, username))
            h_row = c.fetchone()

            updated_items.append({
                "username": username,
                "name": h_row["name"] if h_row else username,
                "location": h_row["location"] if h_row else "Tokyo",
                "references": h_row["references_count"] if h_row else 0,
                "anchor": anchor,
                "words": words,
                "message": msg_text,
                "status": "Draft",
                "message_id": msg_id
            })

    # 2. In trip_hosts, link exactly FRESH_10 to Tokyo trip
    c.execute("""
        DELETE FROM trip_hosts
        WHERE trip_id = ? AND host_id NOT IN ({})
    """.format(",".join("?" * len(FRESH_10))), (TOKYO_TRIP_ID, *FRESH_10))

    for username in FRESH_10:
        c.execute("""
            INSERT OR IGNORE INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at)
            VALUES (?, ?, 'Tokyo', '2026-11-16', '2026-11-19', ?)
        """, (TOKYO_TRIP_ID, username, now_iso))

    # 3. For the EXCLUDED candidates, remove them from sending_queue
    c.execute("""
        DELETE FROM sending_queue
        WHERE host_id IN ({})
    """.format(",".join("?" * len(EXCLUDED))), EXCLUDED)

    conn.commit()

    # 4. Save JSON export
    export_path = Path("output/tokyo_batch_1.json")
    with open(export_path, "w", encoding="utf-8") as f:
        json.dump(updated_items, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(updated_items)} items to {export_path}")

    # 5. Verify queue using RequestQueueManager
    from request_manager import RequestQueueManager
    rm = RequestQueueManager(str(DB_PATH))
    queue = rm.load_queue(exclude_sent=True, trip_id=TOKYO_TRIP_ID)
    print(f"\nVerification: Total loaded in Tokyo queue: {len(queue)}")
    for item in queue:
        print(f"  * {item.get('username')} ({item.get('name')}) | Status: {item.get('status')} | Msg ID: {item.get('current_message_id', item.get('id'))} | Words: {item.get('word_count')}")
        print(f"    Preview: {item.get('generated_message')[:60]}...")

if __name__ == "__main__":
    run_update()
