# ── voc/config.py ─────────────────────────────────────────────────────────────
# Configuration for the SGP30 VOC air quality monitor.
#
# To override any setting without modifying this file, create:
#   ~/.config/voc/config.py
# with only the keys you want to change. That file is never committed to git.
#
# All keys are UPPER_CASE. Types must match (bool, int, or str).
#
# Example override file:
#   NTFY_URL = "https://ntfy.sh/myspace-air"
#   GSHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
#   GSHEET_CREDENTIALS = "/home/pi/.config/voc/credentials.json"
#   GSHEET_WRITE = True
# ─────────────────────────────────────────────────────────────────────────────

# ── Web server ────────────────────────────────────────────────────────────────
PORT = 8080

# ── ntfy push notifications ───────────────────────────────────────────────────
# Set to "" to disable alerts entirely.
NTFY_URL              = "https://ntfy.sh/slvmakerspace-voc"
# Minimum minutes between repeated alerts while air quality stays bad.
NTFY_COOLDOWN_MINUTES = 30

# ── Google Sheets — write mode ────────────────────────────────────────────────
# When True, every 5-minute reading is appended to a Google Sheet in addition
# to the local ~/.local/voc/voc.csv file.
#
# One-time setup:
#   1. Create a Google Cloud project and enable the Google Sheets API.
#   2. Create a service account; download its JSON key file.
#   3. Share your spreadsheet with the service account e-mail address.
#   4. Install the extra packages:
#        .venv/bin/pip install gspread google-auth
#      (or uncomment those lines in requirements.txt and re-run install.sh)
#
GSHEET_WRITE       = False
GSHEET_ID          = ""          # Spreadsheet ID from its URL
GSHEET_WORKSHEET   = "Sheet1"   # Tab name inside the spreadsheet
GSHEET_CREDENTIALS = ""         # Absolute path to the service account JSON key
                                 # e.g. "/home/pi/.config/voc/credentials.json"

# ── Google Sheets — dashboard (read) mode ─────────────────────────────────────
# When True, the 24-hour and 28-day charts read from the Google Sheet instead
# of the local CSV.  No SGP30 sensor is required — useful for a second Pi that
# acts as a display only.  In this mode the live 5-minute chart is hidden.
#
# Requires GSHEET_ID, GSHEET_WORKSHEET, and GSHEET_CREDENTIALS above.
GSHEET_READ = False
