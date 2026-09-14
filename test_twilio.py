from dotenv import load_dotenv
import os
from twilio.rest import Client

load_dotenv()

client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

call = client.calls.create(
    url="https://dust-relive-nappy.ngrok-free.dev/voice",
    to=os.getenv("TWILIO_TEST_TO_NUMBER"),
    from_=os.getenv("TWILIO_PHONE_NUMBER")
)

print("Call initiated successfully!")
print("Call SID:", call.sid)