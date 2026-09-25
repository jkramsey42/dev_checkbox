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

@app.route("/webhook-ces-multi", methods=["POST"])
def ces_multi_webhook():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    if str(data.get("SurveyId", "")) != "2369":
        return jsonify({"error": "Unexpected survey"}), 400

    numeric_id = ces_value(data, "NumericId")
    if not numeric_id:
        return jsonify({"error": "Missing NumericId"}), 400

    prefixes = {
        "CT": "CT_site_",
        "MR": "MR_site_",
    }
    groups_present = {
        group
        for group, prefix in prefixes.items()
        if any(key.startswith(prefix) for key in data)
    }
    if not groups_present:
        return jsonify({"status": "ignored", "reason": "No site fields"}), 200

    # Each selected alias gets its own row.
    selected = []
    for group, prefix in prefixes.items():
        for key, value in data.items():
            if key.startswith(prefix) and value is True:
                alias = key[len(prefix):]
                if alias:
                    selected.append((group, alias))

    worksheet = get_ces_worksheet()
    if worksheet.row_values(1)[:18] != CES_HEADERS:
        return jsonify({"error": "FY27C_single headers do not match"}), 500

    sheet_rows = worksheet.get_all_values()
    existing = {}
    previous_ps = ""

    for row_number, values in enumerate(sheet_rows[1:], start=2):
        row = (values + [""] * 18)[:18]
        if row[1] != numeric_id:
            continue

        # Selection rows have an alias in H or K.
        if row[7].startswith("CT"):
            identity = ("CT", row[7])
        elif row[10].startswith("MR"):
            identity = ("MR", row[10])
        else:
            continue

        if identity in existing:
            return jsonify({"error": "Duplicate selection row"}), 500

        existing[identity] = row_number
        previous_ps = previous_ps or row[2]

    ps = (
        ces_value(data, "ps")
        or request.args.get("access_code", "").strip()
        or previous_ps
    )
    timestamp = ces_value(data, "Timestamp")

    def make_row(group, alias):
        row = [""] * 18
        row[0] = timestamp
        row[1] = numeric_id
        row[2] = ps
        row[7 if group == "CT" else 10] = alias
        return row

    selected_set = set(selected)

    # Refresh rows for choices that are still selected.
    for identity in selected:
        if identity in existing:
            row_number = existing[identity]
            worksheet.update(
                range_name=f"A{row_number}:R{row_number}",
                values=[make_row(*identity)],
                value_input_option="RAW",
            )

    # If a respondent unchecks a choice, remove its old row.
    # Delete from the bottom so earlier row numbers do not move.
    stale_rows = [
        row_number
        for identity, row_number in existing.items()
        if identity[0] in groups_present and identity not in selected_set
    ]
    for row_number in sorted(stale_rows, reverse=True):
        worksheet.delete_rows(row_number)

    # Append newly selected choices after the existing data.
    new_rows = [
        make_row(*identity)
        for identity in selected
        if identity not in existing
    ]
    if new_rows:
        worksheet.append_rows(new_rows, value_input_option="RAW")

    return jsonify({
        "status": "success",
        "selected": len(selected),
        "added": len(new_rows),
        "removed": len(stale_rows),
    }), 200
    
FMS_HEADERS = [
    "timestamp", "user ID", "accesscode", "state",
    "AZ_loc", "CA_loc", "CT_loc", "FL_loc", "GA_loc",
    "MR_loc", "NJ_loc", "NY_loc", "PAA_loc", "PAC_loc",
    "PAPL_loc", "PAPO_loc", "PAS_loc", "PAT_loc", "ps",
]

FMS_SOURCES = [
    "Timestamp", "NumericId", "accesscode", "state_loc",
    "AZ_loc", "CA_loc", "CT_loc", "FL_loc", "GA_loc",
    "MR_loc", "NJ_loc", "NY_loc", "PAA_loc", "PAC_loc",
    "PAPL_loc", "PAPO_loc", "PAS_loc", "PAT_loc",
]

def get_fms_worksheet():
    credentials = Credentials.from_service_account_file(
        GOOGLE_APPLICATION_CREDENTIALS,
        scopes=SCOPES,
    )
    client = gspread.authorize(credentials)
    return client.open_by_key(
        os.environ["CES_SPREADSHEET_ID"]
    ).worksheet("FY27F")

def fms_has_answer(data):
    for key, value in data.items():
        if key in CES_METADATA or key in ("ps", "qcode"):
            continue
        if value is None or value is False:
            continue
        if str(value).strip():
            return True
    return False

@app.route("/webhook-fms", methods=["POST"])
def fms_webhook():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    if str(data.get("SurveyId", "")) != "2365":
        return jsonify({"error": "Unexpected survey"}), 400

    answered_keys = [
        key for key, value in data.items()
        if key not in CES_METADATA
        and value is not None
        and value is not False
        and str(value).strip()
    ]
    print(
        "FMS CHECK: "
        + json.dumps({
            "status": data.get("Status"),
            "answered_keys": answered_keys,
            "ps_in_webhook_url": bool(
                request.args.get("access_code", "").strip()
            ),
        }),
        flush=True,
    )

    numeric_id = ces_value(data, "NumericId")
    if not numeric_id:
        return jsonify({"error": "Missing NumericId"}), 400

    if not fms_has_answer(data):
        return jsonify({"status": "ignored", "reason": "No answers yet"}), 200

    worksheet = get_fms_worksheet()
    if worksheet.row_values(1)[:19] != FMS_HEADERS:
        return jsonify({"error": "FY27F headers do not match"}), 500

    ids = worksheet.col_values(2)
    matches = [
        row_number
        for row_number, value in enumerate(ids, start=1)
        if row_number > 1 and value == numeric_id
    ]
    if len(matches) > 1:
        return jsonify({"error": "Duplicate NumericId in FY27F"}), 500

    row_number = matches[0] if matches else None
    row = (
        (worksheet.row_values(row_number) + [""] * 19)[:19]
        if row_number else [""] * 19
    )

    # Keep earlier values when a partial payload omits their keys.
    for index, source in enumerate(FMS_SOURCES):
        if source in data:
            row[index] = ces_value(data, source)

    # Hidden ps may arrive through the webhook URL instead of JSON.
    ps_from_url = request.args.get("access_code", "").strip()
    if "ps" in data and ces_value(data, "ps"):
        row[18] = ces_value(data, "ps")
    elif ps_from_url:
        row[18] = ps_from_url

    if row_number:
        worksheet.update(
            range_name=f"A{row_number}:S{row_number}",
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
