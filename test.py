import time
import requests

BASE_URL = "http://localhost:8000"

payload = {
    "recipients": [
        {
            "phone_number": "01153411597",
            "conversation_initiation_client_data": {
                "dynamic_variables": {
                    "is_guarantor": "false",
                    "cr_gender": "male",
                    "call_receiver": "أحمد محمد",
                    "contract_ref": "CTR-00123",
                    "gr_phone_number": "+201000000000",
                    "br_phone_number": "+201111111111",
                    "user_name": "أحمد محمد",
                    "user_name_full": "أحمد محمد علي",
                    "br_gender": "Male",
                    "guarantor_name": "",
                    "guarantor_name_full": "",
                    "gr_gender": "",
                    "payment_amount": "1500",
                    "due_date": "01/07/2026",
                    "outstanding_balance": "12000",
                    "Today_date": "14/07/2026",
                    "penalty": "150",
                    "remain_installments": "8",
                    "last_payment_date": "20/06/2026",
                    "last_paid_amount": "1500",
                    "total_number_installments": "24",
                    "total_loan_amount": "36000"
                }
            }
        },
    ]
}

print("Submitting call...")

response = requests.post(
    f"{BASE_URL}/calls",
    json=payload,
    timeout=10
)

response.raise_for_status()

result = response.json()
print(result)

call = result["scheduled"][0]
call_id = call["call_id"]

print(f"\nCall ID: {call_id}")
print("Polling status...\n")

while True:
    r = requests.get(f"{BASE_URL}/calls/{call_id}", timeout=10)
    r.raise_for_status()

    status = r.json()

    print(
        f"Status: {status['status']}"
        + (
            f" | Last latency: {status['last_turn_latency']}"
            if status.get("last_turn_latency")
            else ""
        )
    )

    if status["status"] in [
        "completed",
        "failed",
        "rejected",
        "cancelled",
    ]:
        print("\nFinal Result:")
        print(status)
        break

    time.sleep(2)