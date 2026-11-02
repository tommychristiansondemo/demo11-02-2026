"""Cognito PreSignUp trigger: auto-confirm self-registered student IDs (no confirmation email or code).

Only the username is checked (letters, digits, . _ - ; 3-32 chars) so it is also a valid AgentCore Memory actor id.
The event never contains the password, and we never log the full event.
"""
import json
import re

USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")


def handler(event, _context):
    username = event.get("userName", "")
    if not USERNAME.match(username):
        raise Exception("Username must be 3-32 characters: letters, digits, '.', '_' or '-' (start with a letter or digit).")
    event["response"]["autoConfirmUser"] = True
    attrs = event["request"].get("userAttributes", {})
    if "email" in attrs:
        event["response"]["autoVerifyEmail"] = True
    if "phone_number" in attrs:
        event["response"]["autoVerifyPhone"] = True
    print(json.dumps({"level": "INFO", "service": "frcopilot-presignup", "msg": "signup.autoconfirmed",
                      "username": username, "client_id": event.get("callerContext", {}).get("clientId")}))
    return event
