import os
import json
from datetime import datetime, timezone

from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials


app = Flask(__name__)


# --- Google Sheets setup ---

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "RawData")
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/etc/secrets/service_account.json"
)


def get_worksheet():
    credentials = Credentials.from_service_account_file(
        GOOGLE_APPLICATION_CREDENTIALS,
        scopes=SCOPES,
    )
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    return spreadsheet.worksheet(WORKSHEET_NAME)


# --- Helper functions ---

def get_nested(data, *keys, default=""):
    """
    Safely fetch nested values from dictionaries.

    Example:
    get_nested(data, "respondent", "email")
    """
    current = data

    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)

    if current is None:
        return default

    return current


def clean(value):
    """
    Normalize values before sending them to Google Sheets.
    """
    if value is None:
        return ""

    if isinstance(value, list):
        return "; ".join(str(item) for item in value)

    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)

    return str(value).strip()


def build_row(data):
    """
    Build one output row.

    The order here MUST match the headers in row 1 of your Google Sheet.
    """

    submitted_at = datetime.now(timezone.utc).isoformat()

    # Example direct fields.
    # Replace these with the actual Checkbox variable names.
    response_id = clean(data.get("response_id"))
    first_name = clean(data.get("first_name"))
    last_name = clean(data.get("last_name"))
    email = clean(data.get("email"))

    # Example survey variables.
    q1 = clean(data.get("q1"))
    q2 = clean(data.get("q2"))
    q3 = clean(data.get("q3"))

    row = [
        submitted_at,
        response_id,
        first_name,
        last_name,
        email,
        q1,
        q2,
        q3,
    ]

    return row


@app.route("/", methods=["GET"])
def home():
    return "Checkbox webhook is running."


@app.route("/webhook", methods=["POST"])
def checkbox_webhook():
    try:
        data = request.get_json(silent=True)

        if data is None:
            data = request.form.to_dict()

        print("Incoming Checkbox payload:")
        print(json.dumps(data, indent=2, ensure_ascii=False))

        row = build_row(data)

        print("Row being written:")
        print(row)

        worksheet = get_worksheet()

        worksheet.append_row(
            row,
            value_input_option="USER_ENTERED",
            table_range="A1:AQ1"
        )

        return jsonify({
            "status": "success",
            "columns_written": len(row)
        }), 200

    except Exception as e:
        print("Error processing webhook:")
        print(str(e))

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
