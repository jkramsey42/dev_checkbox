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

def find_selected_program(data):
    """
    Finds a true field like:
    'In which program do you participate?_Community Services_Column2_1': true

    Returns:
    'Community Services'
    """
    prefix = "In which program do you participate?_"
    suffix = "_Column2_1"

    for key, value in data.items():
        if key.startswith(prefix) and key.endswith(suffix) and value is True:
            program = key[len(prefix):-len(suffix)]
            return clean(program)

    return ""

def find_where_in_state(data):
    """
    Finds a key like:
    'Where in Arizona do you receive services?'
    'Where in Texas do you receive services?'
    'Where in Florida do you receive services?'
    """
    for key, value in data.items():
        if key.startswith("Where in ") and key.endswith(" do you receive services?"):
            return clean(value)

    return ""

def build_row(data, access_code=""):
    """
    Build one output row.

    The order here MUST match the headers in row 1 of your Google Sheet.
    """

    submitted_at = datetime.now(timezone.utc).isoformat()
    
    numeric_id = clean(data.get("NumericId", ""))
    state = clean(data.get("Where do you receive services?", ""))
    location_in_state = find_where_in_state(data)
    selected_program = find_selected_program(data)

    foster_care_type = clean(data.get(
        "Do you participate in adult or youth foster care?",
        ""
    ))

    row = [
        submitted_at,
        numeric_id,
        access_code,
        state,
        location_in_state,
        selected_program,
        foster_care_type
        
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

        access_code = (
            clean(request.args.get("access_code", ""))
            or clean(data.get(
                "Please enter your access code. This should be a string of 6 - 8 letters.",
                ""
            ))
        )

        print("Access code from URL:", access_code)
        
        row = build_row(data, access_code)

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

# --- New code 9/24 --- 
CES_HEADERS = [
    "timestamp", "user ID", "ps", "pscode_manual", "state_name",
    "loc_AZ", "loc_CA", "loc_CT", "loc_FL", "loc_GA", "loc_MR",
    "loc_NJ", "loc_NY", "loc_PA", "loc_PC", "loc_PP", "loc_TC", "loc_PS",
]

# These describe the response, rather than answers to survey items.
CES_METADATA = {
    "WebhookPayloadId", "Timestamp", "CurrentPageId",
    "TotalTimeInSeconds", "Score", "ProgressCurrentPageNumber",
    "ProgressTotalPageCount", "Id", "NumericId", "SurveyId",
    "Status", "Language", "Started", "LastEdit", "Ended",
    "IpAddress", "IsTest", "ContactId", "AnonymousRespondentId",
    "Invitee", "IsAnonymized", "ImportBatchId",
}

def ces_value(data, key):
    value = data.get(key, "")
    return "" if value is None else str(value).strip()

def ces_has_answer(data):
    return any(
        key not in CES_METADATA
        and key != "ps"  # Hidden field; don't count it as an answer.
        and value is not None
        and str(value).strip() != ""
        for key, value in data.items()
    )

def get_ces_worksheet():
    credentials = Credentials.from_service_account_file(
        GOOGLE_APPLICATION_CREDENTIALS,
        scopes=SCOPES,
    )
    client = gspread.authorize(credentials)
    return client.open_by_key(
        os.environ["CES_SPREADSHEET_ID"]
    ).worksheet("FY27C_single")

@app.route("/webhook-ces", methods=["POST"])
def ces_webhook():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    if str(data.get("SurveyId", "")) != "2366":
        return jsonify({"error": "Unexpected survey"}), 400

    numeric_id = ces_value(data, "NumericId")
    if not numeric_id:
        return jsonify({"error": "Missing NumericId"}), 400

    if not ces_has_answer(data):
        return jsonify({"status": "ignored", "reason": "No answers yet"}), 200

    worksheet = get_ces_worksheet()
    if worksheet.row_values(1)[:18] != CES_HEADERS:
        return jsonify({"error": "FY27C_single headers do not match"}), 500

    # Find this response's existing row, regardless of its status.
    ids = worksheet.col_values(2)
    matches = [
        row_number
        for row_number, value in enumerate(ids, start=1)
        if row_number > 1 and value == numeric_id
    ]
    if len(matches) > 1:
        return jsonify({"error": "Duplicate NumericId in sheet"}), 500

    row_number = matches[0] if matches else None
    row = (
        (worksheet.row_values(row_number) + [""] * 18)[:18]
        if row_number else [""] * 18
    )

    # Update fields present in this payload; preserve earlier answers
    # when a later partial payload omits those fields.
    sources = [
        "Timestamp", "NumericId", "ps", "pscode_manual", "state_name",
        "loc_AZ", "loc_CA", "loc_CT", "loc_FL_OLD", "loc_GA",
        "loc_MR", "loc_NJ", "loc_NY", "loc_PA", "loc_PC",
        "loc_PP", "loc_TC", "loc_PS",
    ]
    for index, source in enumerate(sources):
        if source in data:
            row[index] = ces_value(data, source)

    # Checkbox may pass the hidden ps value in the webhook URL
    # rather than in the JSON payload.
    url_access_code = request.args.get("access_code", "").strip()
    if not row[2] and url_access_code:
        row[2] = url_access_code

    if row_number:
        worksheet.update(
            range_name=f"A{row_number}:R{row_number}",
            values=[row],
            value_input_option="RAW",
        )
        action = "updated"
    else:
        worksheet.append_row(row, value_input_option="RAW")
        action = "added"

    return jsonify({"status": action, "numeric_id": numeric_id}), 200
# --- end new code ---

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
