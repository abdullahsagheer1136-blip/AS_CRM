# Orbit CRM

An early multi-workspace CRM web app for a social media marketing business. It includes signup/login, workspace-scoped contacts, lead tracking, a Meta WhatsApp Embedded Signup v4 connect screen, encrypted WhatsApp access-token storage, a signed inbound webhook, and a basic inbox for received messages.

## Important: WhatsApp setup is not automatic

The code is ready for Meta configuration, but the **Connect with Facebook** flow only appears after you add your own Meta app values to the hosting environment. For a public multi-business CRM, Meta requires a Tech Provider or Solution Partner setup, Embedded Signup, the required app review/permissions, and a public HTTPS domain. Meta says the signup flow returns a temporary authorization code and account IDs; the server exchanges the code, subscribes the WABA to webhooks, and registers the phone number. The app also needs the `account_update` subscription for onboarding. Follow the current [Meta Embedded Signup implementation guide](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation/) and [Tech Provider onboarding guide](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-customers-as-a-tech-provider/).

The current CRM can store inbound messages after webhooks are configured. It does **not** send outbound messages yet. WhatsApp message pricing, opt-in rules, approved templates, and customer service windows still apply. Never use unofficial WhatsApp Web automation for this product.

## Prepare and upload to GitHub

This folder is the GitHub-ready project. `.gitignore` excludes local databases, virtual environments, and `.env` secrets. `.env.example` contains placeholders only.

1. Create an empty GitHub repository (do not add a README or license in GitHub if you will push this folder).
2. In VS Code, open this folder and use **Source Control → Initialize Repository**, then stage the project files and make the first commit.
3. Add your new GitHub repository as the remote and push the `main` branch. Or use GitHub Desktop to publish this folder.
4. Before pushing, confirm no `.env`, `instance/`, `*.sqlite3`, passwords, tokens, or real customer data are staged.

## Deploy the web app

Use a hosted PostgreSQL database and a Python web service. The service build command is `pip install -r requirements.txt`; the start command is `gunicorn app:app`.

Set these environment variables in the host dashboard (never in source code or GitHub):

| Variable | Value |
| --- | --- |
| `APP_ENV` | `production` |
| `SECRET_KEY` | A fresh random secret, at least 32 characters |
| `DATABASE_URL` | Hosted PostgreSQL connection URL |
| `META_APP_ID` | Your Meta developer app ID |
| `META_APP_SECRET` | Your Meta app secret |
| `META_CONFIG_ID` | Facebook Login for Business Embedded Signup configuration ID |
| `META_GRAPH_VERSION` | Latest supported Graph API version (currently `v27.0` in Meta's guide) |
| `META_WEBHOOK_VERIFY_TOKEN` | A separate random string you create |

Render free web services sleep after 15 minutes without traffic, and free database plans have limits and retention conditions. Check current provider terms before storing important data. A custom domain may cost money.

## Configure Meta

1. Create a Meta developer app, add WhatsApp, and complete the Tech Provider onboarding for serving other businesses.
2. Configure Facebook Login for Business and create a WhatsApp Embedded Signup v4 configuration. Add your deployed HTTPS hostname to the allowed domains and valid OAuth redirect URI settings.
3. Set the environment variables above and redeploy.
4. In the Meta app's Webhooks product, configure object **WhatsApp Business Account**, callback URL `https://YOUR-DOMAIN/webhooks/whatsapp`, and the same verify token as `META_WEBHOOK_VERIFY_TOKEN`. Subscribe to `messages` and `account_update`.
5. Log in to Orbit, open **WhatsApp**, enter the WhatsApp Business number's six-digit two-step verification PIN, and click **Continue with Facebook**.
6. Test with your own Meta app roles first. After Meta approval, eligible external businesses can use the flow. Confirm inbound messages reach **Inbox**.

The `/webhooks/whatsapp` POST handler validates Meta's `X-Hub-Signature-256` signature before storing messages. Each connected WABA and incoming message is linked to a workspace. Access tokens are encrypted in the database using a key derived from `SECRET_KEY`; keep that secret stable and protected. Rotating it will prevent decrypting previously saved WhatsApp tokens.

## Local development

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:8000>. Without `DATABASE_URL`, the app uses SQLite under the ignored `instance/` directory. Embedded Signup requires public HTTPS, so WhatsApp onboarding cannot be completed on plain localhost.

## Current limitations

This is a deployable starter, not a finished production SaaS. It has no team invitations, password reset/email verification, login throttling, automated migrations, outbound messaging, billing, or full compliance workflows. Add those and arrange a security review before accepting real customers or targeting 1,000+ users. Free hosting is useful for a test launch, but its sleep, database, and capacity limits may not support that scale reliably.
