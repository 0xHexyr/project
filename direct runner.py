import os
import re
import time
import random
from datetime import datetime
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

FORM_URL = "https://forms.gle/r8KWEhqs66fngHAD7"

# =====================================================================
# CONFIGURATION
# On GitHub Actions these come from the "Run workflow" form.
# When running locally, the defaults after the comma are used.
# =====================================================================
CSV_FILE = os.getenv("CSV_FILE", "").strip() or "antibiotic_survey_cleaned.csv"
START_INDEX = int(os.getenv("START_INDEX", "").strip() or "0")   # 0 = Row 1
_total = os.getenv("TOTAL_RESPONSES", "").strip()
TOTAL_RESPONSES = int(_total) if _total else None                # None = as many as time allows
MAX_RUNTIME_MINUTES = int(os.getenv("MAX_RUNTIME_MINUTES", "").strip() or "300")  # stops before GitHub's 6h limit
SUCCESS_TEXT = "Your response has been recorded"  # Change if you customised the form's confirmation message
SCREENSHOT_ON_FAILURE = True
MAX_PAGES = 10
# =====================================================================

QNUM_RE = re.compile(r"^\s*(?:Q\s*)?(\d+)\s*[.)]")


class SignInRequired(Exception):
    pass


def now():
    return datetime.now().strftime("%H:%M:%S")


def get_realistic_delay():
    roll = random.random()
    if roll < 0.70:
        return random.uniform(10, 120)
    elif roll < 0.90:
        return random.uniform(120, 600)
    else:
        return random.uniform(600, 1800)


def question_number(text):
    """Extracts '12' from '12. How often do you...' (also handles 'Q12.' and '12)')."""
    m = QNUM_RE.match(str(text))
    return m.group(1) if m else None


def clean_value(val):
    """pandas reads numeric answers as 3.0 — Google Forms expects '3'."""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val).strip()


def build_answers(row):
    """Maps question number -> answer string for one CSV row."""
    answers = {}
    for col_name, val in row.items():
        if pd.isna(val):
            continue
        qn = question_number(col_name)
        if qn is not None:
            answers[qn] = clean_value(val)
    return answers


def css_escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def click_option(container, role, label):
    """Clicks a radio/checkbox by its exact label and confirms Google registered it."""
    opt = container.get_by_role(role, name=label, exact=True)
    if opt.count() == 0:
        opt = container.locator(f'[role="{role}"][data-value="{css_escape(label)}"]')
    if opt.count() == 0:
        return False

    target = opt.first
    if target.get_attribute("aria-checked") != "true":
        target.scroll_into_view_if_needed()
        target.click()
        time.sleep(random.uniform(0.2, 0.6))
    return target.get_attribute("aria-checked") == "true"


def answer_question(container, value):
    """Returns a list of options that could not be selected (empty list = success)."""
    if container.get_by_role("checkbox").count() > 0:
        # Try the whole value first in case an option itself contains a comma
        if click_option(container, "checkbox", value):
            return []
        options = [o.strip() for o in value.split(",") if o.strip()]
        return [o for o in options if not click_option(container, "checkbox", o)]

    if click_option(container, "radio", value):
        return []
    return [value]


def fill_current_page(page, answers):
    containers = page.locator("div[role='listitem']")
    answered, problems = [], []

    for i in range(containers.count()):
        c = containers.nth(i)
        if not c.is_visible():
            continue
        qn = question_number(c.inner_text())
        if qn is None:
            continue
        if qn not in answers:
            problems.append(f"Q{qn}: no value in CSV")
            continue

        missing = answer_question(c, answers[qn])
        if missing:
            problems.append(f"Q{qn}: option(s) not found on form -> {missing}")
        else:
            answered.append(qn)

    return answered, problems


def visible_required_errors(page):
    errs = page.get_by_text("This is a required question")
    return sum(1 for i in range(errs.count()) if errs.nth(i).is_visible())


def submit_row(page, answers):
    """Fills and submits the form once. Returns True only when Google confirms the response."""
    page.goto(FORM_URL, timeout=60000, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass

    if "accounts.google.com" in page.url:
        raise SignInRequired()

    for page_num in range(1, MAX_PAGES + 1):
        try:
            page.locator("div[role='listitem']").first.wait_for(timeout=15000)
        except PWTimeout:
            print(f"      ⚠️ No questions found on Page {page_num}.")

        answered, problems = fill_current_page(page, answers)
        print(f"   📝 [Page {page_num}] Answered {len(answered)} question(s): {', '.join('Q' + q for q in answered) or '-'}")
        for p in problems:
            print(f"      ⚠️ {p}")

        submit_btn = page.get_by_role("button", name="Submit", exact=True)
        next_btn = page.get_by_role("button", name="Next", exact=True)

        if submit_btn.count() > 0 and submit_btn.first.is_visible():
            print(f"   🚀 [Action] Clicking 'Submit'...")
            submit_btn.first.click()
            try:
                page.get_by_text(SUCCESS_TEXT).first.wait_for(timeout=20000)
                return True
            except PWTimeout:
                n = visible_required_errors(page)
                if n:
                    print(f"      ❌ Google rejected the submission: {n} required question(s) unanswered.")
                else:
                    print(f"      ❌ No confirmation page appeared (current URL: {page.url}).")
                return False

        if next_btn.count() > 0 and next_btn.first.is_visible():
            print(f"   ⏩ [Action] Clicking 'Next'...")
            next_btn.first.click()
            time.sleep(2)
            n = visible_required_errors(page)
            if n:
                print(f"      ❌ Blocked on Page {page_num}: {n} required question(s) unanswered.")
                return False
            continue

        print(f"      ❌ Neither 'Next' nor 'Submit' found on Page {page_num}.")
        return False

    print(f"      ❌ Exceeded {MAX_PAGES} pages without reaching 'Submit'.")
    return False


def write_summary(succeeded, failed, next_start, total_rows, stop_reason):
    lines = [
        f"CSV file: {CSV_FILE}",
        f"Confirmed submissions: {len(succeeded)}",
        f"Failed submissions: {len(failed)}",
        f"Failed CSV rows: {[i + 1 for i in failed] if failed else 'none'}",
        f"Stopped because: {stop_reason}",
    ]
    if next_start < total_rows:
        lines.append(f"NEXT RUN: use start_index = {next_start}")
    else:
        lines.append("All rows in this CSV have been processed.")
    with open("run_summary.txt", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def run_survey():
    run_started = time.time()

    print("=" * 65)
    print("🚀 SURVEY AUTOMATION AGENT (CONFIRMED SUBMISSION)")
    print("=" * 65)

    print(f"📂 [Step 1] Loading survey data from '{CSV_FILE}'...")
    try:
        df = pd.read_csv(CSV_FILE)
        print(f"   ✅ Loaded {len(df)} total rows from CSV.")
    except Exception as e:
        print(f"   ❌ Error loading CSV: {e}")
        return

    end = len(df) if TOTAL_RESPONSES is None else min(len(df), START_INDEX + TOTAL_RESPONSES)
    target_df = df.iloc[START_INDEX:end]

    if target_df.empty:
        print(f"   ⚠️ START_INDEX ({START_INDEX}) exceeds total rows in CSV ({len(df)}). Nothing to run.")
        return

    print(f"🎯 Target Batch: up to {len(target_df)} responses (CSV Rows {START_INDEX + 1} to {end}).")
    print(f"⏱️  Will stop starting new rows after {MAX_RUNTIME_MINUTES} minutes.\n")

    succeeded, failed = [], []
    next_start = START_INDEX
    stop_reason = "finished the requested rows"

    print("🌐 [Step 2] Launching Playwright browser engine...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        print("   ✅ Browser launched.\n")
        print("=" * 65)

        for count, (idx, row) in enumerate(target_df.iterrows(), 1):
            elapsed_min = (time.time() - run_started) / 60
            if elapsed_min >= MAX_RUNTIME_MINUTES:
                stop_reason = f"reached the {MAX_RUNTIME_MINUTES}-minute time limit"
                print(f"\n⏱️ {stop_reason}. Stopping before Row {idx + 1}.")
                break

            row_no = idx + 1
            print(f"\n▶️ [{count}/{len(target_df)}] {now()} | CSV Row {row_no} | {elapsed_min:.0f} min elapsed")
            print("   --------------------------------------------------------")

            answers = build_answers(row)
            context = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
            page = context.new_page()
            ok = False
            aborted = False

            try:
                ok = submit_row(page, answers)
            except SignInRequired:
                print("   ❌ The form redirected to a Google sign-in page.")
                print("      In the form's Settings > Responses, turn off 'Limit to 1 response'")
                print("      and any 'Restrict to users in your organisation' option, then rerun.")
                aborted = True
            except Exception as e:
                print(f"   ❌ Error during submission for Row {row_no}: {e}")

            if aborted:
                context.close()
                stop_reason = "form requires Google sign-in"
                break

            next_start = idx + 1
            if ok:
                succeeded.append(idx)
                print(f"   ✅ Confirmed recorded by Google for Row {row_no}.")
            else:
                failed.append(idx)
                if SCREENSHOT_ON_FAILURE:
                    try:
                        shot = f"failed_row_{row_no}.png"
                        page.screenshot(path=shot, full_page=True)
                        print(f"   📸 Saved screenshot: {shot}")
                    except Exception:
                        pass

            context.close()

            if count < len(target_df):
                if ok:
                    delay = get_realistic_delay()
                    print(f"   ⏳ [Pacing] Waiting {delay:.0f}s ({delay / 60:.1f} mins) before next submission...")
                    time.sleep(delay)
                else:
                    time.sleep(5)

        browser.close()

    print("\n" + "=" * 65)
    print("🏁 RUN FINISHED")
    if failed:
        df.loc[failed].to_csv("failed_rows.csv", index=False)
        print("   Failed rows saved to failed_rows.csv")
    write_summary(succeeded, failed, next_start, len(df), stop_reason)
    print("=" * 65)


if __name__ == "__main__":
    run_survey()
