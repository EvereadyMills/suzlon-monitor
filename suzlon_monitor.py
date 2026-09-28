import os
import sys
import json
import time
import traceback
from datetime import datetime

import pytz
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ---------------- SETTINGS ----------------
LOGIN_URL = "https://www.windpro.suzlon.com/s/login/"
DASHBOARD_URL = "https://www.windpro.suzlon.com/s/dashboard"
LOCATION_NAME = "Tirunelveli"

SUZLON_USER = os.environ.get("SUZLON_USER")
SUZLON_PASS = os.environ.get("SUZLON_PASS")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SUZLON_CHAT_ID = os.environ.get("SUZLON_CHAT_ID")      # NEW Suzlon group only

STATE_FILE = "suzlon_states.json"         # last known status of every WTG
REPORT_LOG_FILE = "suzlon_report_log.json"  # which 8 AM / 6 PM reports were sent
IST = pytz.timezone("Asia/Kolkata")
LOGIN_FAIL_ALERT_AFTER = 4                # alert after 4 failed runs in a row (~1 hour)


# ---------------- FILE HELPERS ----------------
def load_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"Could not read {path}, starting fresh:", e)
    return {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


# ---------------- TELEGRAM ----------------
def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not SUZLON_CHAT_ID:
        print("❌ TELEGRAM_BOT_TOKEN or SUZLON_CHAT_ID missing.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": SUZLON_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        res = requests.post(url, json=payload, timeout=30)
        print("Telegram API Response:", res.text)
        return res.status_code == 200
    except Exception as e:
        print("Telegram Error:", e)
        return False


def send_long_telegram(text):
    """Telegram limit is 4096 chars per message - split on blank lines if needed."""
    if len(text) <= 4000:
        return send_telegram(text)
    ok = True
    chunk = ""
    for block in text.split("\n\n"):
        if len(chunk) + len(block) + 2 > 4000:
            ok = send_telegram(chunk) and ok
            chunk = ""
        chunk += block + "\n\n"
    if chunk.strip():
        ok = send_telegram(chunk) and ok
    return ok


# ---------------- MESSAGE FORMAT ----------------
def wtg_lines(item):
    return [
        f" Loc.No        :  <b>{item['name']}</b>",
        f" Status         :  {item['status']}",
        f" w/s               :  {item['ws']}",
        f" Cur.prod     :  {item['cur_prod']}",
        f" Acc.prod     :  {item['acc_prod']}",
    ]


def change_message(item, old_status):
    lines = [
        "🌀 <b>Suzlon</b>",
        f"⚠️ Status Changed : {old_status} ➜ {item['status']}",
        "",
        f"📍 <b>Location : {LOCATION_NAME}</b>",
    ]
    lines += wtg_lines(item)
    return "\n".join(lines)


def full_report_message(data, title, now_ist):
    lines = [
        "🌀 <b>Suzlon</b>",
        f"📊 <b>{title}</b>",
        f"🕒 {now_ist.strftime('%d-%m-%Y %I:%M %p')} IST",
        "",
        f"📍 <b>Location : {LOCATION_NAME}</b>",
        "",
    ]
    for key in data:  # same order as the website table
        lines += wtg_lines(data[key])
        lines.append("")
    return "\n".join(lines).strip()


def clean_ws(value):
    # "4.44 M/S" -> "4.44"
    return value.replace("M/S", "").replace("m/s", "").strip() or "-"


# ---------------- SELENIUM ----------------
def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    return webdriver.Chrome(options=opts)


def login(driver):
    print("1. Opening Suzlon login page...")
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 40)

    # IDs like "154:0" change on every load, so use placeholder / class instead
    user_box = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[placeholder="Username"]')))
    user_box.clear()
    user_box.send_keys(SUZLON_USER)

    pass_box = driver.find_element(By.CSS_SELECTOR, 'input[placeholder="Password"]')
    pass_box.clear()
    pass_box.send_keys(SUZLON_PASS)

    driver.find_element(By.CSS_SELECTOR, "button.loginButton").click()
    print("2. Login clicked, waiting for home page...")

    try:
        WebDriverWait(driver, 45).until(lambda d: "/login" not in d.current_url)
    except Exception:
        page = driver.find_element(By.TAG_NAME, "body").text.lower()
        if "verif" in page or "code" in page:
            raise RuntimeError("Suzlon is asking for a VERIFICATION CODE (new device/IP check).")
        raise RuntimeError("Login did not complete - check SUZLON_USER / SUZLON_PASS.")

    if "verif" in driver.current_url.lower():
        raise RuntimeError("Suzlon is asking for a VERIFICATION CODE (new device/IP check).")

    # Salesforce goes through frontdoor.jsp -> /s/... ; wait until that finishes
    try:
        WebDriverWait(driver, 60).until(
            lambda d: "frontdoor" not in d.current_url
            and "/s/" in d.current_url
            and d.execute_script("return document.readyState") == "complete"
        )
    except Exception:
        pass
    # print only the page path (the full URL contains a session id - keep it out of public logs)
    print("   Logged in. Landing page:", driver.current_url.split("?")[0])


# Salesforce LWC hides components inside "shadow" layers that normal Selenium
# search cannot see. This JS walks into every shadow layer to find elements.
DEEP_JS = """
function deepAll(root, sel, out) {
    out = out || [];
    try { root.querySelectorAll(sel).forEach(function (e) { if (out.indexOf(e) < 0) out.push(e); }); } catch (err) {}
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
        if (all[i].shadowRoot) deepAll(all[i].shadowRoot, sel, out);
    }
    return out;
}
"""


def deep_find(driver, css):
    return driver.execute_script(DEEP_JS + "return deepAll(document, arguments[0]);", css) or []


def find_consolidated_button(driver):
    # 1st: by data-tab attribute, 2nd: any button/tab whose text is "Consolidated View"
    found = deep_find(driver, 'button[data-tab="consolidated"], button.kpi-tab--consolidated')
    if found:
        return found[0]
    for el in deep_find(driver, 'button, [role="tab"], a'):
        try:
            if "consolidated view" in (el.get_attribute("textContent") or "").strip().lower():
                return el
        except Exception:
            continue
    return None


def js_click(driver, el):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();", el)


def read_consolidated_table(driver):
    print("3. Finding Consolidated View...")
    # After login Suzlon sometimes opens the Dashboard, sometimes the Home page.
    #  - Dashboard  -> Consolidated View box is already there
    #  - Home page  -> click the "Machine Performance" tile (link to /s/dashboard)
    #  - Neither    -> open the dashboard URL directly
    tile_clicked = False
    direct_opened = False
    start = time.time()
    start_tile = start
    tab = None
    while time.time() - start < 150:
        tab = find_consolidated_button(driver)
        if tab is not None:
            break

        elapsed = time.time() - start
        if not tile_clicked and elapsed > 8 and "/s/dashboard" not in driver.current_url:
            tiles = deep_find(driver, 'a[href="/s/dashboard"], a[href$="/s/dashboard"]')
            if tiles:
                print("   Home page detected -> clicking Machine Performance tile")
                js_click(driver, tiles[0])
                tile_clicked = True
                start_tile = time.time()

        if not direct_opened and "/s/dashboard" not in driver.current_url and (
            elapsed > 45 or (tile_clicked and time.time() - start_tile > 30)
        ):
            print("   Opening dashboard URL directly")
            driver.get(DASHBOARD_URL)
            direct_opened = True

        time.sleep(3)

    if tab is None:
        raise RuntimeError("Consolidated View box not found (page: " + driver.current_url.split("?")[0] + ")")

    print("   Dashboard ready:", driver.current_url.split("?")[0])
    js_click(driver, tab)
    print("4. Consolidated View clicked, waiting for table...")

    # Wait for the table rows
    table_el = None
    for _ in range(30):
        tables = deep_find(driver, "table.consolidated-table")
        if tables:
            rows = driver.execute_script("var t=arguments[0]; return t.tBodies.length ? t.tBodies[0].rows.length : 0;", tables[0])
            if rows > 0:
                table_el = tables[0]
                break
        time.sleep(2)
    if table_el is None:
        raise RuntimeError("Consolidated table did not load.")

    # How many records the page says it has ("20 Records")
    expected = 0
    counts = deep_find(driver, "p.consolidated-count, .consolidated-count")
    if counts:
        digits = "".join(ch for ch in (counts[0].get_attribute("textContent") or "") if ch.isdigit())
        expected = int(digits) if digits else 0
    print(f"   Page says {expected or '?'} records")

    # Scroll the table box to the bottom step by step so every row is loaded
    wrappers = deep_find(driver, ".consolidated-table-wrapper")
    scroller = wrappers[0] if wrappers else table_el
    last = -1
    for _ in range(15):
        count = driver.execute_script("return arguments[0].tBodies[0].rows.length;", table_el)
        if (expected and count >= expected) or count == last:
            break
        last = count
        driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", scroller)
        time.sleep(1.5)
    driver.execute_script("arguments[0].scrollTop = 0;", scroller)
    time.sleep(2)  # let live values finish filling in

    # Read headers + all cells (native table API works inside shadow layers;
    # textContent gives the full text even when the screen shows "WTG Status n...")
    table = driver.execute_script("""
        var t = arguments[0];
        var heads = [];
        if (t.tHead && t.tHead.rows.length) {
            var hc = t.tHead.rows[0].cells;
            for (var i = 0; i < hc.length; i++) heads.push(hc[i].textContent.trim().toUpperCase());
        }
        var rows = [];
        var br = t.tBodies[0].rows;
        for (var r = 0; r < br.length; r++) {
            var cells = [];
            for (var c = 0; c < br[r].cells.length; c++) cells.push(br[r].cells[c].textContent.trim());
            rows.push(cells);
        }
        return {heads: heads, rows: rows};
    """, table_el)

    heads = table["heads"]

    def col(keyword, default):
        for i, h in enumerate(heads):
            if keyword in h:
                return i
        return default

    i_loc = col("LOCATION", 0)
    i_status = col("STATUS", 1)
    i_ws = col("WIND", 2)
    i_cur = col("CURRENT", 4)
    i_acc = col("ACCUMULATED", 5)

    data = {}
    for cells in table["rows"]:
        if len(cells) <= max(i_loc, i_status, i_ws, i_cur, i_acc):
            continue
        name = cells[i_loc]
        if not name:
            continue
        data[name] = {
            "name": name,
            "status": cells[i_status] or "-",
            "ws": clean_ws(cells[i_ws]),
            "cur_prod": cells[i_cur] or "-",
            "acc_prod": cells[i_acc] or "-",
        }

    print(f"   Total WTGs read: {len(data)}")
    if not data:
        raise RuntimeError("Consolidated table was empty.")
    if expected and len(data) < expected:
        print(f"   ⚠️ Only {len(data)} of {expected} rows read")
    return data


# ---------------- MAIN LOGIC ----------------
def process(current_data, now_ist):
    today = now_ist.strftime("%Y-%m-%d")
    previous = load_json(STATE_FILE)
    log = load_json(REPORT_LOG_FILE)
    new_state = dict(current_data)

    # 1. First ever run: no saved state -> one "started" message, no 20 change alerts
    if not previous:
        print("First run - saving state.")
        if send_long_telegram(full_report_message(current_data, "Suzlon Monitor Started ✅", now_ist)):
            # this full list also counts as the current 8 AM / 6 PM report (no duplicate)
            if now_ist.hour >= 18:
                log["evening_report"] = today
            elif now_ist.hour >= 8:
                log["morning_report"] = today
        else:
            new_state = {}  # try again next run
    else:
        # 2. Status change -> separate message for EACH changed WTG only
        for name, item in current_data.items():
            prev = previous.get(name)
            if not isinstance(prev, dict):
                continue  # new WTG appeared - just remember it
            old_status = prev.get("status", "-")
            if str(old_status).strip().lower() != str(item["status"]).strip().lower():
                print(f"Status change: {name}: {old_status} -> {item['status']}")
                if not send_telegram(change_message(item, old_status)):
                    new_state[name] = prev  # keep old, so next run re-sends this alert

    # 3. Scheduled full report - once per slot per day, even if GitHub starts late
    slot = None
    if now_ist.hour >= 18 and log.get("evening_report") != today:
        slot, title = "evening_report", "Evening Report (6 PM)"
    elif 8 <= now_ist.hour < 18 and log.get("morning_report") != today:
        slot, title = "morning_report", "Morning Report (8 AM)"

    if slot:
        print(f"Sending {title}...")
        if send_long_telegram(full_report_message(current_data, title, now_ist)):
            log[slot] = today

    log["login_fail_count"] = 0
    if new_state:
        save_json(STATE_FILE, new_state)
    save_json(REPORT_LOG_FILE, log)


def record_failure(reason):
    log = load_json(REPORT_LOG_FILE)
    count = log.get("login_fail_count", 0) + 1
    log["login_fail_count"] = count
    if count == LOGIN_FAIL_ALERT_AFTER:
        send_telegram(
            "🌀 <b>Suzlon</b>\n⚠️ <b>Monitor is not able to read the website</b>\n"
            f"Reason: {reason}\nFailed {count} runs in a row. Please check GitHub Actions log."
        )
    save_json(REPORT_LOG_FILE, log)


def main():
    if not SUZLON_USER or not SUZLON_PASS:
        print("❌ SUZLON_USER or SUZLON_PASS missing in GitHub Secrets.")
        sys.exit(1)

    driver = make_driver()
    try:
        login(driver)
        current_data = read_consolidated_table(driver)
        print("Detected data:", current_data)
        process(current_data, datetime.now(IST))
        print("✅ Suzlon monitor completed successfully.")
    except Exception as e:
        print("❌ Error during execution:")
        traceback.print_exc()
        try:
            print("   Page at error:", driver.current_url.split("?")[0])
            driver.save_screenshot("debug_screenshot.png")
        except Exception:
            pass
        record_failure(str(e)[:200])
        sys.exit(1)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
