"""Function URL behind the approval email: GET shows a confirm page, POST approves."""
import base64
import hashlib
import hmac
import html
import json
import os
import time

import boto3

KEY = os.environ["APPROVAL_KEY"].encode()
GROUP = os.environ.get("APPROVED_GROUP", "approved")

cognito = boto3.client("cognito-idp")


def read_token(token):
    """The token's data if the signature is right and it has not expired, else None."""
    try:
        payload, sig = token.rsplit(".", 1)
        good = hmac.new(KEY, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if data["exp"] > time.time() else None
    except Exception:
        return None


def page(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
        "body": '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>Dashboard approval</title><body style="font:16px system-ui,sans-serif;max-width:32rem;'
                'margin:15vh auto;padding:0 16px">' + body + "</body>",
    }


def handler(event, context):
    method = event["requestContext"]["http"]["method"]
    token = (event.get("queryStringParameters") or {}).get("t", "")
    data = read_token(token)
    if not data:
        return page(400, "<h1>Link not valid</h1><p>It is wrong or has expired (links last 7 days). "
                         "Approve the user from the CLI instead.</p>")
    who = html.escape(data["email"])
    if method == "POST":
        cognito.admin_add_user_to_group(UserPoolId=data["pool"], Username=data["user"], GroupName=GROUP)
        print("approved %s" % data["email"], flush=True)
        return page(200, "<h1>Approved</h1><p>%s can now use the dashboard after signing in again.</p>" % who)
    # GET only shows the page: mail scanners and link previews open links without anyone clicking
    return page(200, "<h1>Approve %s?</h1><p>This lets them see the printer dashboard.</p>"
                     '<form method="post" action="?t=%s"><button style="font-size:16px;padding:8px 20px">Approve</button></form>'
                % (who, html.escape(token, quote=True)))
