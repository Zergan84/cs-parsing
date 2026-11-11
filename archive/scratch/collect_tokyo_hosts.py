import sys
from scraper import CouchsurfingScraper

def main():
    trip_id = "tokyo-tokyo-japan-3lzoexgtitw9o2awbbnxoekirdptp-3LZOeXgTItw9o2AWbbnxoeKIrdP_tp"
    trip_url = f"https://www.couchsurfing.com/c/trips/{trip_id}"
    
    print(f"[*] Starting scraper for Tokyo Trip: {trip_id}", flush=True)
    scraper = CouchsurfingScraper(
        city=trip_url,
        gender="Any",
        status="Accepting Guests",
        headless=True
    )
    # Set explicit trip_id so hosts are associated directly with this trip
    scraper.trip_id = trip_id
    scraper.arrival_date = "2026-11-16"
    scraper.departure_date = "2026-11-19"
    scraper.max_scroll_pages = 4  # Enough to collect ~20-30 real hosts
    
    hosts = scraper.run()
    print(f"\n[+] Scraper finished. Total collected hosts: {len(hosts)}", flush=True)

if __name__ == "__main__":
    main()
