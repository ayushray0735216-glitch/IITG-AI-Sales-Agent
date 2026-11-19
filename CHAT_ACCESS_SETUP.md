# Verified customer chat

The Developer Console overview shows the public chat link. Each lead profile with
an email address can create a private prospect link and review an invitation email.

Set these variables before using customer links outside the local computer:

- `CHAT_PUBLIC_BASE_URL`: public HTTPS address of this Streamlit app, without a trailing slash.
- `EMAIL_ADDRESS` and `EMAIL_PASSWORD`: sending mailbox and its app password. The same mailbox sends six-digit verification codes.
- `DATABASE_URL` on a hosted deployment, or a persistent `CRM_DATA_DIR` for a local deployment. The chat challenge store must survive app restarts and be shared by every app instance.

The public URL is `?chat=public`. Prospect URLs use `?chat=prospect&invite=...`.
Prospect links expire after 30 days. The first person to press Start chatting
through a new prospect link is associated with that prospect without an email
form. Later sessions must confirm the prospect's mailbox with a code. A
forwarded or stolen link opened first can be misattributed; one-click entry
cannot prevent this. Verification codes expire after 10 minutes, allow five
attempts, and cannot be reused. Sending a code does not grant contact consent.
Phone and WhatsApp details remain optional.

In Bulk email, put `[Chat with us]({chat_link})` in the message. Review each
recipient's preview before approving the batch. The visible words are the same
for everyone, while the URL is generated for that recipient's CRM lead.

Verification remains active for the current chat session. A new browser session
requires a new code. The app does not yet issue a persistent browser credential,
so returning after closing the browser also requires email verification.
