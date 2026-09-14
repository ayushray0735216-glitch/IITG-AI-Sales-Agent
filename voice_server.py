import os
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv
from google import genai

load_dotenv()

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

app = Flask(__name__)

def get_ai_response(customer_message):
    prompt = f"""
You are a professional B2B sales assistant for IITG.

The customer is speaking with you on a phone call.

Customer message:
{customer_message}

Respond naturally and conversationally.
Keep the response concise because this is a voice call.
Do not invent prices, features, discounts, guarantees, or other facts.
Ask a relevant follow-up question when appropriate.
Do not use markdown, bullet points, or headings.

Return only the sentence(s) that should be spoken to the customer.
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )

    return response.text.strip()

@app.route("/voice", methods=["GET", "POST"])
def voice():
    response = VoiceResponse()

    gather = Gather(
        input="speech",
        action="https://dust-relive-nappy.ngrok-free.dev/process",
        method="POST",
        speech_timeout="auto",
        language="en-IN"
    )

    gather.say(
        "Hello! This is the IITG AI Sales Agent. "
        "Your call has been connected successfully. "
        "How can I help you today?"
    )

    response.append(gather)

    response.say(
        "I didn't hear a response. Thank you for calling. Goodbye."
    )

    return str(response)


@app.route("/process", methods=["POST"])
def process():
    speech = request.values.get("SpeechResult", "")

    response = VoiceResponse()

    if speech:
        ai_reply = get_ai_response(speech)
        response.say(ai_reply)

    else:
        response.say(
            "I could not understand your response. Goodbye."
        )

    response.redirect("https://dust-relive-nappy.ngrok-free.dev/voice", method="POST")

    return str(response)


if __name__ == "__main__":
    app.run(port=5000, debug=True)