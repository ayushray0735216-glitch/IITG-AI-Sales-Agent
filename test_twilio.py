from urllib.parse import quote
from dotenv import load_dotenv
import os
from twilio.rest import Client

load_dotenv()

client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)
lead_name = input("Enter lead name: ").strip()
lead_param = quote(lead_name)

call = client.calls.create(
    url=f"https://iitg-ai-sales-agent.onrender.com/voice?lead={lead_param}",
    to=os.getenv("TWILIO_TEST_TO_NUMBER"),
    from_=os.getenv("TWILIO_PHONE_NUMBER")
)

print("Call initiated successfully!")
print("Call SID:", call.sid)