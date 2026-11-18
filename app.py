import os
import re
import base64
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func, or_
from cryptography.fernet import Fernet
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
# Vercel par sirf /tmp me likh sakte hain, is liye wahan DB /tmp me banegi
if os.getenv("VERCEL"):
    INSTANCE_PATH = "/tmp/instance"
    DB_PATH = "/tmp/crm.sqlite3"
else:
    INSTANCE_PATH = str(BASE_DIR / "instance")
    DB_PATH = str(Path(INSTANCE_PATH) / 'crm.sqlite3')

app = Flask(__name__, instance_path=INSTANCE_PATH)
Path(app.instance_path).mkdir(parents=True, exist_ok=True)
production = os.getenv("APP_ENV") == "production"
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "dev-only-change-me"),
    SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}").replace(
        "postgres://", "postgresql+psycopg://", 1
    ).replace("postgresql://", "postgresql+psycopg://", 1),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=production,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SECURE=production,
)
if production and (not os.getenv("SECRET_KEY") or len(os.getenv("SECRET_KEY", "")) < 32):
    raise RuntimeError("Set SECRET_KEY to a random value of at least 32 characters in production.")

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to access your workspace."
login_manager.login_message_category = "info"

class Workspace(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    users = db.relationship("User", backref="workspace", cascade="all, delete-orphan")
    contacts = db.relationship("Contact", backref="workspace", cascade="all, delete-orphan")

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(254), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Contact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(40), nullable=False)
    source = db.Column(db.String(60), nullable=False, default="Other")
    stage = db.Column(db.String(30), nullable=False, default="New")
    notes = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)

class WhatsAppConnection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    waba_id = db.Column(db.String(40), nullable=False)
    phone_number_id = db.Column(db.String(40), nullable=False, unique=True)
    display_phone = db.Column(db.String(40), nullable=False, default="")
    encrypted_token = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    workspace = db.relationship("Workspace", backref=db.backref("whatsapp_connection", uselist=False, cascade="all, delete-orphan"))

class WhatsAppMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False, index=True)
    phone_number_id = db.Column(db.String(40), nullable=False, index=True)
    wa_message_id = db.Column(db.String(160), nullable=False, unique=True)
    sender = db.Column(db.String(40), nullable=False)
    message_type = db.Column(db.String(30), nullable=False, default="text")
    body = db.Column(db.Text, nullable=False, default="")
    received_at = db.Column(db.DateTime, nullable=False)

STAGES = ("New", "Contacted", "Qualified", "Proposal", "Won", "Lost")
SOURCES = ("Instagram", "Facebook", "Website", "Referral", "Other")

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    return response

def workspace_contacts():
    # Every contact query is scoped to the signed-in user's workspace.
    return Contact.query.filter_by(workspace_id=current_user.workspace_id)

def token_cipher():
    key_material = hashlib.sha256((app.config["SECRET_KEY"] + "|orbit-whatsapp-token-v1").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key_material))

def graph_request(path, *, token=None, params=None, data=None, http_method=None):
    version = os.getenv("META_GRAPH_VERSION", "v27.0")
    url = f"https://graph.facebook.com/{version}/{path.lstrip('/')}"
    if params:
        url += "?" + urlencode(params)
    headers = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    req = Request(url, data=body, headers=headers, method=http_method or ("POST" if body is not None else "GET"))
    try:
        with urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        # Avoid showing request URLs, app secrets, or bearer tokens in user-facing errors.
        if isinstance(exc, HTTPError):
            try:
                details = json.loads(exc.read().decode()).get("error", {}).get("message", "Meta rejected the request.")
            except Exception:
                details = "Meta rejected the request. Check your app configuration."
            raise RuntimeError(details[:220]) from None
        raise RuntimeError("Could not reach Meta. Check the deployment and try again.") from None
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(str(payload["error"].get("message", "Meta rejected the request."))[:220])
    return payload

def validate_contact_form():
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    source = request.form.get("source", "Other")
    stage = request.form.get("stage", "New")
    notes = request.form.get("notes", "").strip()
    if not name or len(name) > 120:
        flash("Enter a name of up to 120 characters.", "error")
        return None
    if not phone or len(phone) > 40:
        flash("Enter a phone number of up to 40 characters.", "error")
        return None
    if source not in SOURCES or stage not in STAGES:
        flash("Choose a valid source and lead stage.", "error")
        return None
    if len(notes) > 5000:
        flash("Notes must be 5,000 characters or fewer.", "error")
        return None
    return {"name": name, "phone": phone, "source": source, "stage": stage, "notes": notes}

@app.get("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("register"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        business = request.form.get("business", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or len(name) > 100 or not business or len(business) > 100:
            flash("Enter your name and a business name (up to 100 characters each).", "error")
        elif not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
            flash("Enter a valid email address.", "error")
        elif len(password) < 12:
            flash("Use a password with at least 12 characters.", "error")
        elif User.query.filter_by(email=email).first():
            flash("An account with that email already exists. Please sign in.", "error")
        else:
            workspace = Workspace(name=business)
            db.session.add(workspace)
            db.session.flush()
            user = User(name=name, email=email, workspace_id=workspace.id)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash("Your private workspace is ready. Add your first contact below.", "success")
            return redirect(url_for("dashboard"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("Email or password is incorrect.", "error")
    return render_template("login.html")

@app.post("/logout")
@login_required
def logout():
    logout_user()
    flash("You have signed out.", "success")
    return redirect(url_for("login"))

@app.get("/dashboard")
@login_required
def dashboard():
    query = workspace_contacts()
    search = request.args.get("q", "").strip()[:100]
    if search:
        pattern = f"%{search}%"
        query = query.filter(or_(Contact.name.ilike(pattern), Contact.phone.ilike(pattern)))
    contacts = query.order_by(Contact.created_at.desc(), Contact.id.desc()).all()
    counts = {stage: workspace_contacts().filter_by(stage=stage).count() for stage in STAGES}
    return render_template("dashboard.html", contacts=contacts, counts=counts, stages=STAGES, sources=SOURCES, search=search)

@app.get("/settings")
@login_required
def settings():
    connection = WhatsAppConnection.query.filter_by(workspace_id=current_user.workspace_id).first()
    meta_ready = bool(os.getenv("META_APP_ID") and os.getenv("META_CONFIG_ID") and os.getenv("META_APP_SECRET") and os.getenv("META_WEBHOOK_VERIFY_TOKEN"))
    return render_template("settings.html", connection=connection, meta_ready=meta_ready,
                           meta_app_id=os.getenv("META_APP_ID", ""), meta_config_id=os.getenv("META_CONFIG_ID", ""),
                           graph_version=os.getenv("META_GRAPH_VERSION", "v27.0"))

@app.post("/whatsapp/connect")
@login_required
def whatsapp_connect():
    if not all(os.getenv(key) for key in ("META_APP_ID", "META_APP_SECRET", "META_CONFIG_ID")):
        return {"error": "Meta app credentials are not configured on the server yet."}, 503
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", ""))
    waba_id = str(payload.get("waba_id", ""))
    phone_number_id = str(payload.get("phone_number_id", ""))
    pin = str(payload.get("pin", ""))
    if not code or len(code) > 4096 or not re.fullmatch(r"\d{5,40}", waba_id) or not re.fullmatch(r"\d{5,40}", phone_number_id):
        return {"error": "Meta did not return a complete WhatsApp account and phone number."}, 400
    if not re.fullmatch(r"\d{6}", pin):
        return {"error": "Enter a six-digit WhatsApp two-step verification PIN to finish setup."}, 400
    try:
        exchange = graph_request("oauth/access_token", params={
            "client_id": os.environ["META_APP_ID"],
            "client_secret": os.environ["META_APP_SECRET"],
            "code": code,
        })
        access_token = exchange.get("access_token")
        if not access_token:
            raise RuntimeError("Meta did not return an access token.")
        existing_number = WhatsAppConnection.query.filter_by(phone_number_id=phone_number_id).first()
        if existing_number and existing_number.workspace_id != current_user.workspace_id:
            raise RuntimeError("This WhatsApp number is already connected to a different CRM workspace.")
        graph_request(f"{waba_id}/subscribed_apps", token=access_token, data={})
        graph_request(f"{phone_number_id}/register", token=access_token, data={"messaging_product": "whatsapp", "pin": pin})
        phone_info = graph_request(phone_number_id, token=access_token, params={"fields": "display_phone_number"})
        connection = WhatsAppConnection.query.filter_by(workspace_id=current_user.workspace_id).first()
        if not connection:
            connection = WhatsAppConnection(workspace_id=current_user.workspace_id, waba_id=waba_id, phone_number_id=phone_number_id, encrypted_token="")
            db.session.add(connection)
        connection.waba_id = waba_id
        connection.phone_number_id = phone_number_id
        connection.display_phone = str(phone_info.get("display_phone_number", ""))[:40]
        connection.encrypted_token = token_cipher().encrypt(access_token.encode()).decode()
        db.session.commit()
        return {"ok": True, "phone": connection.display_phone}
    except (RuntimeError, KeyError) as exc:
        db.session.rollback()
        return {"error": str(exc)}, 502

@app.post("/whatsapp/disconnect")
@login_required
def whatsapp_disconnect():
    connection = WhatsAppConnection.query.filter_by(workspace_id=current_user.workspace_id).first()
    if connection:
        try:
            access_token = token_cipher().decrypt(connection.encrypted_token.encode()).decode()
            graph_request(f"{connection.waba_id}/subscribed_apps", token=access_token, http_method="DELETE")
        except Exception:
            # Local disconnection should still work if Meta is temporarily unavailable.
            pass
        db.session.delete(connection)
        db.session.commit()
    flash("WhatsApp was disconnected from this CRM workspace.", "success")
    return redirect(url_for("settings"))

@app.get("/webhooks/whatsapp")
def whatsapp_webhook_verify():
    verify_token = os.getenv("META_WEBHOOK_VERIFY_TOKEN", "")
    if (request.args.get("hub.mode") == "subscribe" and verify_token
            and hmac.compare_digest(request.args.get("hub.verify_token", ""), verify_token)):
        return request.args.get("hub.challenge", ""), 200, {"Content-Type": "text/plain"}
    abort(403)

@app.post("/webhooks/whatsapp")
@csrf.exempt
def whatsapp_webhook_receive():
    app_secret = os.getenv("META_APP_SECRET", "")
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(app_secret.encode(), request.get_data(), hashlib.sha256).hexdigest() if app_secret else ""
    if not expected or not hmac.compare_digest(signature, expected):
        abort(403)
    payload = request.get_json(silent=True) or {}
    if payload.get("object") != "whatsapp_business_account":
        return {"ok": True}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            metadata = value.get("metadata", {})
            phone_number_id = str(metadata.get("phone_number_id", ""))
            connection = WhatsAppConnection.query.filter_by(phone_number_id=phone_number_id).first()
            if not connection:
                continue
            for message in value.get("messages", []):
                message_id = str(message.get("id", ""))
                if not message_id or WhatsAppMessage.query.filter_by(wa_message_id=message_id).first():
                    continue
                message_type = str(message.get("type", "unknown"))[:30]
                body = str(message.get("text", {}).get("body", ""))[:4096] if message_type == "text" else f"[{message_type} message]"
                try:
                    from datetime import datetime, timezone
                    received_at = datetime.fromtimestamp(int(message.get("timestamp", "0")), tz=timezone.utc).replace(tzinfo=None)
                except (ValueError, TypeError, OSError):
                    from datetime import datetime
                    received_at = datetime.utcnow()
                db.session.add(WhatsAppMessage(workspace_id=connection.workspace_id, phone_number_id=phone_number_id,
                    wa_message_id=message_id, sender=str(message.get("from", ""))[:40],
                    message_type=message_type, body=body, received_at=received_at))
    db.session.commit()
    return {"ok": True}

@app.get("/inbox")
@login_required
def inbox():
    messages = WhatsAppMessage.query.filter_by(workspace_id=current_user.workspace_id).order_by(WhatsAppMessage.received_at.desc()).limit(100).all()
    connection = WhatsAppConnection.query.filter_by(workspace_id=current_user.workspace_id).first()
    return render_template("inbox.html", messages=messages, connection=connection)

@app.post("/contacts")
@login_required
def create_contact():
    values = validate_contact_form()
    if values:
        db.session.add(Contact(workspace_id=current_user.workspace_id, **values))
        db.session.commit()
        flash("Contact saved.", "success")
    return redirect(url_for("dashboard"))

@app.post("/contacts/<int:contact_id>/edit")
@login_required
def edit_contact(contact_id):
    contact = workspace_contacts().filter_by(id=contact_id).first_or_404()
    values = validate_contact_form()
    if values:
        for key, value in values.items():
            setattr(contact, key, value)
        db.session.commit()
        flash("Contact updated.", "success")
    return redirect(url_for("dashboard"))

@app.post("/contacts/<int:contact_id>/delete")
@login_required
def delete_contact(contact_id):
    contact = workspace_contacts().filter_by(id=contact_id).first_or_404()
    db.session.delete(contact)
    db.session.commit()
    flash("Contact deleted.", "success")
    return redirect(url_for("dashboard"))

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8000")), debug=not production)
