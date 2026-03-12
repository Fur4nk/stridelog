import os
import io
import csv
import json
import math
import shutil
import secrets
import zipfile
import tempfile
import functools
import requests
from urllib.parse import urlsplit
from datetime import datetime, timezone, timedelta
from xml.etree.ElementTree import parse as ET_parse, fromstring as ET_fromstring

from flask import Flask, render_template, request, jsonify, Response, send_file, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import flask_login


def _load_dotenv():
    dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(dotenv_path):
        return
    with open(dotenv_path, "r", encoding="utf-8") as dotenv_file:
        for raw_line in dotenv_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            os.environ.setdefault(key, value)


_load_dotenv()

app = Flask(__name__, template_folder="../templates", static_folder="../static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "uploads"), exist_ok=True)

# --- SECRET_KEY (auto-generated, persisted) ---
secret_path = os.path.join(DATA_DIR, ".secret_key")
if os.path.exists(secret_path):
    with open(secret_path, "r") as _f:
        _secret = _f.read().strip()
else:
    _secret = secrets.token_hex(32)
    with open(secret_path, "w") as _f:
        _f.write(_secret)
    os.chmod(secret_path, 0o600)

app.config["SECRET_KEY"] = _secret
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DATA_DIR}/tracks.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"

db = SQLAlchemy(app)

# --- OIDC setup (optional) ---
OIDC_PROVIDER_URL = os.environ.get("OIDC_PROVIDER_URL", "").strip()
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "").strip()
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
password_login_enabled = os.environ.get("PASSWORD_LOGIN_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
oidc_enabled = bool(OIDC_PROVIDER_URL and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)
WEATHER_PROVIDER = os.environ.get("WEATHER_PROVIDER", "open-meteo").strip().lower()
weather_enabled = WEATHER_PROVIDER == "open-meteo"
WEATHER_HTTP_TIMEOUT = float(os.environ.get("WEATHER_HTTP_TIMEOUT", "8").strip() or "8")
WEATHER_CACHE_VERSION = 4

oauth = None
if oidc_enabled:
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    oauth.register(
        name="oidc",
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=f"{OIDC_PROVIDER_URL.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def _safe_next_url(target):
    if not target:
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return "/"
    if not target.startswith("/"):
        return "/"
    if target.startswith("//"):
        return "/"
    return target


# --------------- Models ---------------

class User(db.Model, flask_login.UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(256), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=True)
    oidc_provider = db.Column(db.String(256))
    oidc_sub = db.Column(db.String(256))
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.String(32), default=lambda: datetime.now(timezone.utc).isoformat())
    tracks = db.relationship("Track", backref="owner", lazy=True)


class Track(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(256))
    name = db.Column(db.String(256))
    activity_type = db.Column(db.String(64))
    date = db.Column(db.String(32))
    duration_s = db.Column(db.Float)
    distance_m = db.Column(db.Float)
    avg_speed_ms = db.Column(db.Float)
    max_speed_ms = db.Column(db.Float)
    elevation_gain_m = db.Column(db.Float)
    elevation_loss_m = db.Column(db.Float)
    avg_hr = db.Column(db.Float)
    max_hr = db.Column(db.Float)
    avg_cadence = db.Column(db.Float)
    calories = db.Column(db.Float)
    moving_time_s = db.Column(db.Float)
    track_id = db.Column(db.String(256))
    started_at = db.Column(db.String(64))
    ended_at = db.Column(db.String(64))
    start_lat = db.Column(db.Float)
    start_lon = db.Column(db.Float)
    coordinates = db.Column(db.Text)
    trackpoints = db.Column(db.Text)
    tags = db.Column(db.Text)
    notes = db.Column(db.Text)
    extra_data = db.Column(db.Text)
    weather_json = db.Column(db.Text)
    weather_provider = db.Column(db.String(64))
    weather_fetched_at = db.Column(db.String(64))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


# --- Flask-Login setup ---
login_manager = flask_login.LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = None


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def admin_required(f):
    @functools.wraps(f)
    @flask_login.login_required
    def decorated(*args, **kwargs):
        if not flask_login.current_user.is_admin:
            return jsonify({"error": "Admin required"}), 403
        return f(*args, **kwargs)
    return decorated


# --- Database migration ---
with app.app_context():
    from sqlalchemy import inspect as sa_inspect

    # Migrate old user table (had username/display_name columns) to new schema (email-based)
    inspector = sa_inspect(db.engine)
    if "user" in inspector.get_table_names():
        user_cols = [c["name"] for c in inspector.get_columns("user")]
        if "username" in user_cols and "email" not in user_cols:
            # Old schema — drop and recreate (only exists briefly during initial rollout)
            with db.engine.connect() as conn:
                conn.execute(db.text("DROP TABLE user"))
                conn.commit()
            # Also reset orphan tracks
            if "track" in inspector.get_table_names():
                track_cols = [c["name"] for c in inspector.get_columns("track")]
                if "user_id" in track_cols:
                    with db.engine.connect() as conn:
                        conn.execute(db.text("UPDATE track SET user_id = NULL"))
                        conn.commit()

    # Remove UNIQUE constraint on track.track_id (now dedup is per-user in Python)
    if "track" in inspector.get_table_names():
        indexes = inspector.get_indexes("track")
        unique_cols = inspector.get_unique_constraints("track")
        # Check if there's a unique index/constraint on track_id
        has_unique = any(
            c.get("column_names") == ["track_id"] for c in unique_cols
        ) or any(
            idx.get("column_names") == ["track_id"] and idx.get("unique") for idx in indexes
        )
        if has_unique:
            with db.engine.connect() as conn:
                conn.execute(db.text("""
                    CREATE TABLE track_new (
                        id INTEGER PRIMARY KEY,
                        filename VARCHAR(256),
                        name VARCHAR(256),
                        activity_type VARCHAR(64),
                        date VARCHAR(32),
                        duration_s FLOAT,
                        distance_m FLOAT,
                        avg_speed_ms FLOAT,
                        max_speed_ms FLOAT,
                        elevation_gain_m FLOAT,
                        elevation_loss_m FLOAT,
                        avg_hr FLOAT,
                        max_hr FLOAT,
                        avg_cadence FLOAT,
                        calories FLOAT,
                        moving_time_s FLOAT,
                        track_id VARCHAR(256),
                        started_at VARCHAR(64),
                        ended_at VARCHAR(64),
                        start_lat FLOAT,
                        start_lon FLOAT,
                        coordinates TEXT,
                        trackpoints TEXT,
                        tags TEXT,
                        notes TEXT,
                        extra_data TEXT,
                        weather_json TEXT,
                        weather_provider VARCHAR(64),
                        weather_fetched_at VARCHAR(64),
                        user_id INTEGER REFERENCES user(id)
                    )
                """))
                conn.execute(db.text("""
                    INSERT INTO track_new SELECT
                        id, filename, name, activity_type, date,
                        duration_s, distance_m, avg_speed_ms, max_speed_ms,
                        elevation_gain_m, elevation_loss_m, avg_hr, max_hr,
                        avg_cadence, calories, moving_time_s, track_id,
                        NULL, NULL, NULL, NULL,
                        coordinates, trackpoints, tags, notes, NULL,
                        NULL, NULL, NULL, user_id
                    FROM track
                """))
                conn.execute(db.text("DROP TABLE track"))
                conn.execute(db.text("ALTER TABLE track_new RENAME TO track"))
                conn.commit()
            inspector = sa_inspect(db.engine)

    db.create_all()
    inspector = sa_inspect(db.engine)

    # Track table migrations
    cols = [c["name"] for c in inspector.get_columns("track")]
    text_cols = ("started_at", "ended_at", "coordinates", "trackpoints", "tags", "notes", "extra_data", "weather_json", "weather_provider", "weather_fetched_at")
    float_cols = ("moving_time_s", "start_lat", "start_lon")
    int_cols = ("user_id",)
    for col_name in text_cols + float_cols + int_cols:
        if col_name not in cols:
            if col_name in float_cols:
                col_type = "FLOAT"
            elif col_name in int_cols:
                col_type = "INTEGER"
            else:
                col_type = "TEXT"
            with db.engine.connect() as conn:
                conn.execute(db.text(f"ALTER TABLE track ADD COLUMN {col_name} {col_type}"))
                conn.commit()


def _needs_setup():
    return User.query.count() == 0


# --- Query helper ---
def user_tracks():
    return Track.query.filter_by(user_id=flask_login.current_user.id)


def _normalized_activity_type(activity_type):
    value = (activity_type or "").strip().lower()
    if value in {"street running", "street_running", "road running", "road_running"}:
        return "running"
    if not value or value == "other":
        return "running"
    return value


COMMON_TRACK_METADATA = {
    "ground_state": {"dry", "wet"},
    "perceived_effort": {"low", "medium", "high", "extreme"},
}

TYPE_SPECIFIC_TRACK_METADATA = {
    "running": {
        "surface": {"asphalt", "track", "mixed"},
        "session_type": {"easy", "workout", "long_run", "race"},
    },
    "trail_running": {
        "surface": {"trail", "gravel", "mixed"},
        "technicality": {"easy", "medium", "hard"},
        "mud": {"none", "light", "heavy"},
    },
    "cycling": {
        "surface": {"road", "gravel", "mixed"},
        "wind_feeling": {"low", "medium", "strong"},
    },
    "walking": {
        "surface": {"asphalt", "trail", "mixed"},
        "technicality": {"easy", "medium", "hard"},
    },
    "hiking": {
        "surface": {"trail", "mixed"},
        "technicality": {"easy", "medium", "hard"},
    },
}


def _sanitize_track_metadata(activity_type, extra_data):
    if not isinstance(extra_data, dict):
        return {}

    activity_type = _normalized_activity_type(activity_type)
    sanitized = {}
    for key, allowed in COMMON_TRACK_METADATA.items():
        value = extra_data.get(key)
        if value in allowed:
            sanitized[key] = value

    for key, allowed in TYPE_SPECIFIC_TRACK_METADATA.get(activity_type or "other", {}).items():
        value = extra_data.get(key)
        if value in allowed:
            sanitized[key] = value

    return sanitized


def _load_track_metadata(track):
    if not track.extra_data:
        return {}
    try:
        data = json.loads(track.extra_data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


WEATHER_CODE_LABELS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _parse_stored_datetime(value):
    dt = parse_timestamp(value)
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _track_upload_path(track):
    if not track.filename:
        return None
    return os.path.join(DATA_DIR, "uploads", track.filename)


def _fallback_track_coordinates(track):
    if track.start_lat is not None and track.start_lon is not None:
        return track.start_lat, track.start_lon

    try:
        if track.trackpoints:
            points = json.loads(track.trackpoints)
            if points:
                return points[0].get("lat"), points[0].get("lon")
        if track.coordinates:
            coords = json.loads(track.coordinates)
            if coords:
                return coords[0][0], coords[0][1]
    except Exception:
        return None, None
    return None, None


def _fallback_track_started_at(track):
    started_at = _parse_stored_datetime(track.started_at)
    if started_at and (not track.date or started_at.date().isoformat() == track.date):
        return started_at, "exact"

    if not track.date:
        return None, None

    try:
        approx = datetime.fromisoformat(f"{track.date}T12:00:00+00:00")
        return approx, "approximate-date-only"
    except ValueError:
        return None, None


def _fallback_track_ended_at(track, started_at):
    ended_at = _parse_stored_datetime(track.ended_at)
    if ended_at and started_at and ended_at >= started_at and (
        not track.date or ended_at.date().isoformat() == track.date
    ):
        return ended_at

    if started_at and track.duration_s and track.duration_s > 0:
        return started_at + timedelta(seconds=track.duration_s)

    return started_at


def _load_track_source_metadata(track):
    path = _track_upload_path(track)
    if not path or not os.path.exists(path):
        return False

    try:
        with open(path, "rb") as uploaded_file:
            payload = uploaded_file.read()
        if track.filename.lower().endswith(".gpx"):
            parsed, err = parse_gpx_content(payload, track.filename)
        elif track.filename.lower().endswith(".kml"):
            parsed, err = parse_kml_content(payload, track.filename)
        else:
            return False
        if err or not parsed:
            return False
    except Exception:
        return False

    changed = False
    for field in ("started_at", "ended_at", "start_lat", "start_lon"):
        if getattr(track, field) in (None, "") and parsed.get(field) not in (None, ""):
            setattr(track, field, parsed[field])
            changed = True
    return changed


def _serialize_weather_snapshot(label, when_dt, hourly_times, hourly_values):
    if not hourly_times:
        return None

    target = when_dt.astimezone(timezone.utc)
    before_idx = 0
    after_idx = len(hourly_times) - 1
    for idx, hourly_dt in enumerate(hourly_times):
        if hourly_dt <= target:
            before_idx = idx
        if hourly_dt >= target:
            after_idx = idx
            break

    if hourly_times[before_idx] > target:
        before_idx = after_idx
    if hourly_times[after_idx] < target:
        after_idx = before_idx

    before_dt = hourly_times[before_idx]
    after_dt = hourly_times[after_idx]
    seconds_span = (after_dt - before_dt).total_seconds()
    ratio = 0.0 if seconds_span <= 0 else (target - before_dt).total_seconds() / seconds_span

    def _pick(name):
        values = hourly_values.get(name) or []
        if not values:
            return None
        before_val = values[before_idx]
        after_val = values[after_idx]
        if before_val is None:
            return after_val
        if after_val is None:
            return before_val
        if seconds_span <= 0:
            return before_val
        return before_val + (after_val - before_val) * ratio

    weather_codes = hourly_values.get("weather_code") or []
    weather_code = None
    if weather_codes:
        nearest_idx = before_idx if abs((target - before_dt).total_seconds()) <= abs((after_dt - target).total_seconds()) else after_idx
        weather_code = weather_codes[nearest_idx]

    return {
        "label": label,
        "time": when_dt.isoformat(),
        "temperature_c": round(_pick("temperature_2m"), 1) if _pick("temperature_2m") is not None else None,
        "apparent_temperature_c": round(_pick("apparent_temperature"), 1) if _pick("apparent_temperature") is not None else None,
        "relative_humidity_pct": round(_pick("relative_humidity_2m")) if _pick("relative_humidity_2m") is not None else None,
        "wind_speed_kmh": round(_pick("wind_speed_10m"), 1) if _pick("wind_speed_10m") is not None else None,
        "cloud_cover_pct": round(_pick("cloud_cover")) if _pick("cloud_cover") is not None else None,
        "precipitation_mm_per_hour": round(_pick("precipitation"), 2) if _pick("precipitation") is not None else None,
        "weather_code": weather_code,
        "weather_label": WEATHER_CODE_LABELS.get(weather_code, "Unknown") if weather_code is not None else None,
    }


def _estimate_precipitation_total_mm(start_dt, end_dt, hourly_times, precipitation_values):
    if not precipitation_values or not hourly_times or end_dt <= start_dt:
        return None

    total = 0.0
    start_utc = start_dt.astimezone(timezone.utc)
    end_utc = end_dt.astimezone(timezone.utc)
    for idx, bucket_end in enumerate(hourly_times):
        bucket_start = bucket_end - timedelta(hours=1)
        overlap_start = max(start_utc, bucket_start)
        overlap_end = min(end_utc, bucket_end)
        overlap_seconds = (overlap_end - overlap_start).total_seconds()
        if overlap_seconds <= 0:
            continue
        bucket_value = precipitation_values[idx]
        if bucket_value is None:
            continue
        total += bucket_value * (overlap_seconds / 3600.0)
    return round(total, 2)


def _fetch_open_meteo_weather(track):
    started_at, accuracy = _fallback_track_started_at(track)
    ended_at = _fallback_track_ended_at(track, started_at)
    start_lat, start_lon = _fallback_track_coordinates(track)
    if not started_at or start_lat is None or start_lon is None:
        raise ValueError("Weather requires a timestamp and coordinates from the source track.")

    fields = [
        "temperature_2m",
        "apparent_temperature",
        "relative_humidity_2m",
        "precipitation",
        "weather_code",
        "wind_speed_10m",
        "cloud_cover",
    ]
    response = requests.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": start_lat,
            "longitude": start_lon,
            "start_date": started_at.astimezone(timezone.utc).date().isoformat(),
            "end_date": ended_at.astimezone(timezone.utc).date().isoformat(),
            "hourly": ",".join(fields),
            "timezone": "GMT",
        },
        timeout=WEATHER_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    hourly = payload.get("hourly") or {}
    hourly_times = [
        datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        for ts in hourly.get("time", [])
    ]
    if not hourly_times:
        raise ValueError("Provider returned no hourly weather data.")

    midpoint = started_at + ((ended_at - started_at) / 2 if ended_at and ended_at > started_at else timedelta())
    snapshots = [
        _serialize_weather_snapshot("start", started_at, hourly_times, hourly),
        _serialize_weather_snapshot("mid", midpoint, hourly_times, hourly),
    ]
    if ended_at and ended_at > started_at:
        snapshots.append(_serialize_weather_snapshot("end", ended_at, hourly_times, hourly))

    return {
        "available": True,
        "provider": "open-meteo",
        "cache_version": WEATHER_CACHE_VERSION,
        "provider_label": "Open-Meteo Archive API",
        "resolution": "hourly historical weather",
        "interpolation": "linear interpolation for numeric fields; nearest hour for weather code",
        "time_accuracy": accuracy,
        "location": {
            "lat": round(start_lat, 6),
            "lon": round(start_lon, 6),
        },
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat() if ended_at else None,
        "precipitation_total_mm": _estimate_precipitation_total_mm(
            started_at,
            ended_at,
            hourly_times,
            hourly.get("precipitation") or [],
        ),
        "snapshots": [snapshot for snapshot in snapshots if snapshot],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _get_or_fetch_weather(track, refresh=False):
    if not weather_enabled:
        return {"available": False, "error": "Weather integration is disabled."}

    if not refresh and track.weather_json:
        cached = json.loads(track.weather_json)
        if cached.get("cache_version") == WEATHER_CACHE_VERSION:
            return cached

    weather_payload = _fetch_open_meteo_weather(track)
    track.weather_json = json.dumps(weather_payload)
    track.weather_provider = weather_payload["provider"]
    track.weather_fetched_at = weather_payload["fetched_at"]
    db.session.add(track)
    db.session.commit()
    return weather_payload


# --------------- Parsing helpers ---------------

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_timestamp(ts):
    if ts is None:
        return None
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def strip_ns(tag):
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def find_all_recursive(elem, local_name):
    return [c for c in elem.iter() if strip_ns(c.tag) == local_name]


def find_child(elem, local_name):
    for child in elem:
        if strip_ns(child.tag) == local_name:
            return child
    return None


def get_text(elem, local_name):
    child = find_child(elem, local_name)
    if child is not None and child.text:
        return child.text.strip()
    return None


def build_trackpoints_and_metrics(points):
    """Given raw points [{lat,lon,ele,time,hr,cad,speed}...], compute metrics and trackpoints JSON."""
    if len(points) < 2:
        return None, None, "Not enough trackpoints"

    t0 = points[0]["time"]
    cum_dist = 0.0
    elevation_gain = 0.0
    elevation_loss = 0.0
    moving_time = 0.0
    MOVING_THRESHOLD = 0.5  # m/s — below this is considered a pause
    hrs = []
    cads = []
    speeds = []
    tps = []  # trackpoints for storage

    # First point
    tps.append({
        "lat": round(points[0]["lat"], 6), "lon": round(points[0]["lon"], 6),
        "ele": round(points[0]["ele"], 1) if points[0]["ele"] is not None else None,
        "time_s": 0.0, "dist_m": 0.0,
        "hr": points[0]["hr"], "cad": points[0]["cad"], "speed": points[0]["speed"],
    })
    if points[0]["hr"] is not None:
        hrs.append(points[0]["hr"])
    if points[0]["cad"] is not None:
        cads.append(points[0]["cad"])

    for i in range(1, len(points)):
        p0, p1 = points[i - 1], points[i]
        d = haversine(p0["lat"], p0["lon"], p1["lat"], p1["lon"])
        cum_dist += d

        if p0["ele"] is not None and p1["ele"] is not None:
            diff = p1["ele"] - p0["ele"]
            if diff > 0:
                elevation_gain += diff
            else:
                elevation_loss += abs(diff)

        seg_speed = None
        if p1["speed"] is not None:
            seg_speed = p1["speed"]
            speeds.append(seg_speed)
        elif p0["time"] and p1["time"]:
            dt = (p1["time"] - p0["time"]).total_seconds()
            if dt > 0:
                seg_speed = d / dt
                speeds.append(seg_speed)

        # Accumulate moving time: count segment if speed above threshold
        if p0["time"] and p1["time"]:
            seg_dt = (p1["time"] - p0["time"]).total_seconds()
            if seg_dt > 0 and seg_speed is not None and seg_speed >= MOVING_THRESHOLD:
                moving_time += seg_dt

        if p1["hr"] is not None:
            hrs.append(p1["hr"])
        if p1["cad"] is not None:
            cads.append(p1["cad"])

        time_s = (p1["time"] - t0).total_seconds() if (p1["time"] and t0) else None
        tps.append({
            "lat": round(p1["lat"], 6), "lon": round(p1["lon"], 6),
            "ele": round(p1["ele"], 1) if p1["ele"] is not None else None,
            "time_s": round(time_s, 1) if time_s is not None else None,
            "dist_m": round(cum_dist, 1),
            "hr": p1["hr"], "cad": p1["cad"], "speed": p1["speed"],
        })

    max_speed = max(speeds) if speeds else 0
    times = [p["time"] for p in points if p["time"] is not None]
    duration_s = (times[-1] - times[0]).total_seconds() if len(times) >= 2 else 0
    avg_speed = cum_dist / duration_s if duration_s > 0 else 0
    date_str = times[0].strftime("%Y-%m-%d") if times else ""
    started_at = times[0].isoformat() if times else None
    ended_at = times[-1].isoformat() if times else None

    # Simplified coordinates for map overlay
    step = max(1, len(points) // 200)
    coord_list = [[round(p["lat"], 6), round(p["lon"], 6)] for p in points[::step]]

    metrics = {
        "date": date_str,
        "duration_s": duration_s,
        "moving_time_s": moving_time if moving_time > 0 else duration_s,
        "distance_m": cum_dist,
        "avg_speed_ms": avg_speed,
        "max_speed_ms": max_speed,
        "elevation_gain_m": elevation_gain,
        "elevation_loss_m": elevation_loss,
        "avg_hr": sum(hrs) / len(hrs) if hrs else None,
        "max_hr": max(hrs) if hrs else None,
        "avg_cadence": sum(cads) / len(cads) if cads else None,
        "started_at": started_at,
        "ended_at": ended_at,
        "start_lat": round(points[0]["lat"], 6),
        "start_lon": round(points[0]["lon"], 6),
        "coordinates": json.dumps(coord_list),
        "trackpoints": json.dumps(tps),
    }

    return metrics, tps, None


# --------------- GPX Parsing ---------------

def parse_gpx_content(xml_bytes, filename="unknown"):
    root = ET_fromstring(xml_bytes)

    track_id = None
    for elem in root.iter():
        if strip_ns(elem.tag) == "trackid" and elem.text:
            track_id = elem.text.strip()
            break

    trk_elems = find_all_recursive(root, "trk")
    if not trk_elems:
        return None, "No track found in GPX"

    trk = trk_elems[0]
    name = get_text(trk, "name") or filename
    activity_type = _normalized_activity_type(get_text(trk, "type"))

    trkpts = find_all_recursive(trk, "trkpt")
    if not trkpts:
        return None, "No trackpoints found"

    points = []
    for pt in trkpts:
        lat = pt.get("lat")
        lon = pt.get("lon")
        if lat is None or lon is None:
            continue
        lat, lon = float(lat), float(lon)
        ele_text = get_text(pt, "ele")
        ele = float(ele_text) if ele_text else None
        time = parse_timestamp(get_text(pt, "time"))

        hr = cad = speed = None
        for ext in find_all_recursive(pt, "hr"):
            if ext.text:
                hr = float(ext.text); break
        for ext in find_all_recursive(pt, "cad"):
            if ext.text:
                cad = float(ext.text); break
        if cad is None:
            for ext in find_all_recursive(pt, "RunCadence"):
                if ext.text:
                    cad = float(ext.text); break
        for ext in find_all_recursive(pt, "speed"):
            if ext.text:
                speed = float(ext.text); break

        points.append({"lat": lat, "lon": lon, "ele": ele, "time": time,
                        "hr": hr, "cad": cad, "speed": speed})

    metrics, tps, err = build_trackpoints_and_metrics(points)
    if err:
        return None, err

    track_data = {
        "filename": filename, "name": name, "activity_type": activity_type,
        "calories": None, "track_id": track_id, **metrics,
    }
    return track_data, None


# --------------- KML Parsing ---------------

def parse_kml_content(xml_bytes, filename="unknown"):
    root = ET_fromstring(xml_bytes)

    track_id = None
    for elem in root.iter():
        if strip_ns(elem.tag) == "trackid" and elem.text:
            track_id = elem.text.strip()
            break

    placemarks = find_all_recursive(root, "Placemark")
    if not placemarks:
        return None, "No Placemark found in KML"

    pm = placemarks[0]
    name_elem = find_child(pm, "name")
    name = name_elem.text.strip() if name_elem is not None and name_elem.text else filename

    activity_type = "running"
    for data_elem in find_all_recursive(pm, "Data"):
        if data_elem.get("name") == "activityType":
            val = find_child(data_elem, "value")
            if val is not None and val.text:
                activity_type = _normalized_activity_type(val.text)
                break

    whens = []
    coords = []
    for track_elem in find_all_recursive(pm, "Track"):
        for child in track_elem:
            tag = strip_ns(child.tag)
            if tag == "when" and child.text:
                whens.append(child.text.strip())
            elif tag == "coord":
                coords.append(child.text.strip() if child.text else "")

    if len(whens) != len(coords) or len(whens) < 2:
        return None, "Not enough trackpoints in KML"

    array_data = {}
    for sad in find_all_recursive(root, "SimpleArrayData"):
        arr_name = sad.get("name")
        if arr_name:
            values = []
            for val in find_all_recursive(sad, "value"):
                values.append(val.text.strip() if val is not None and val.text and val.text.strip() else None)
            array_data[arr_name] = values

    speed_arr = array_data.get("speed", [])
    hr_arr = array_data.get("heartrate", [])
    cad_arr = array_data.get("cadence", [])

    points = []
    for i in range(len(whens)):
        time = parse_timestamp(whens[i])
        coord_parts = coords[i].split()
        if len(coord_parts) < 2:
            continue

        lon, lat = float(coord_parts[0]), float(coord_parts[1])
        ele = float(coord_parts[2]) if len(coord_parts) >= 3 else None

        def safe_float(arr, idx):
            if idx < len(arr) and arr[idx]:
                try:
                    return float(arr[idx])
                except ValueError:
                    pass
            return None

        points.append({
            "lat": lat, "lon": lon, "ele": ele, "time": time,
            "hr": safe_float(hr_arr, i), "cad": safe_float(cad_arr, i),
            "speed": safe_float(speed_arr, i),
        })

    if len(points) < 2:
        return None, "Not enough valid trackpoints"

    metrics, tps, err = build_trackpoints_and_metrics(points)
    if err:
        return None, err

    track_data = {
        "filename": filename, "name": name, "activity_type": activity_type,
        "calories": None, "track_id": track_id, **metrics,
    }
    return track_data, None


# --------------- File processing ---------------

def process_file(file_storage, user_id, extra_data=None):
    filename = file_storage.filename or "unknown"
    data = file_storage.read()

    results = []

    if filename.lower().endswith(".kmz"):
        try:
            with tempfile.NamedTemporaryFile(suffix=".kmz", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            with zipfile.ZipFile(tmp_path, "r") as zf:
                for name in zf.namelist():
                    low = name.lower()
                    if low.endswith(".gpx") or low.endswith(".kml"):
                        file_data = zf.read(name)
                        fmt = "gpx" if low.endswith(".gpx") else "kml"
                        results.append((name, file_data, fmt))
            os.unlink(tmp_path)
        except zipfile.BadZipFile:
            return [(filename, False, "Invalid KMZ/ZIP file")]
    elif filename.lower().endswith(".gpx"):
        results.append((filename, data, "gpx"))
    elif filename.lower().endswith(".kml"):
        results.append((filename, data, "kml"))
    else:
        return [(filename, False, "Unsupported file type")]

    output = []
    for fname, xml_bytes, fmt in results:
        try:
            if fmt == "kml":
                track_data, err = parse_kml_content(xml_bytes, fname)
            else:
                track_data, err = parse_gpx_content(xml_bytes, fname)
        except Exception as e:
            output.append((fname, False, f"Parse error: {e}"))
            continue

        if err:
            output.append((fname, False, err))
            continue

        if track_data["track_id"]:
            existing = Track.query.filter_by(
                track_id=track_data["track_id"], user_id=user_id
            ).first()
            if existing:
                output.append((fname, False, "Duplicate track"))
                continue

        track_data["user_id"] = user_id
        metadata = _sanitize_track_metadata(track_data.get("activity_type"), extra_data or {})
        if metadata:
            track_data["extra_data"] = json.dumps(metadata)
        track = Track(**track_data)
        db.session.add(track)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            output.append((fname, False, "Duplicate track"))
            continue

        upload_path = os.path.join(DATA_DIR, "uploads", fname)
        os.makedirs(os.path.dirname(upload_path), exist_ok=True)
        with open(upload_path, "wb") as f:
            f.write(xml_bytes)

        output.append((fname, True, "OK"))

    return output


# --------------- Splits & Records helpers ---------------

def compute_splits(tps, split_m=1000):
    """Compute splits from trackpoints list at every split_m meters."""
    splits = []
    seg_idx = 1
    prev_dist = 0.0
    prev_time = 0.0
    prev_ele = tps[0].get("ele")
    seg_hrs = []
    split_km = split_m / 1000

    for tp in tps:
        dist = tp["dist_m"]
        time_s = tp["time_s"]
        if tp.get("hr") is not None:
            seg_hrs.append(tp["hr"])

        while dist >= seg_idx * split_m:
            target = seg_idx * split_m
            if dist > prev_dist:
                ratio = (target - prev_dist) / (dist - prev_dist)
            else:
                ratio = 0
            split_time = prev_time + ratio * ((time_s or 0) - prev_time)
            split_dur = split_time - (splits[-1]["cum_time_s"] if splits else 0)
            pace = (split_dur / 60) / split_km  # min/km

            cur_ele = tp.get("ele")
            if prev_ele is not None and cur_ele is not None:
                ele_at_boundary = prev_ele + ratio * (cur_ele - prev_ele)
            else:
                ele_at_boundary = cur_ele

            splits.append({
                "km": round(seg_idx * split_km, 2),
                "pace_min": round(pace, 2),
                "duration_s": round(split_dur, 1),
                "cum_time_s": round(split_time, 1),
                "avg_hr": round(sum(seg_hrs) / len(seg_hrs)) if seg_hrs else None,
                "ele": round(ele_at_boundary, 1) if ele_at_boundary is not None else None,
            })
            seg_hrs = []
            seg_idx += 1

        prev_dist = dist
        prev_time = time_s or 0
        prev_ele = tp.get("ele")

    # Final partial segment
    remaining_m = prev_dist - (seg_idx - 1) * split_m
    if remaining_m > split_m * 0.1:  # only if > 10% of split distance
        last_cum = splits[-1]["cum_time_s"] if splits else 0
        split_dur = prev_time - last_cum
        remaining_km = remaining_m / 1000
        pace = (split_dur / 60) / remaining_km if remaining_km > 0 else 0
        splits.append({
            "km": round((seg_idx - 1) * split_km + remaining_km, 2),
            "pace_min": round(pace, 2),
            "duration_s": round(split_dur, 1),
            "cum_time_s": round(prev_time, 1),
            "avg_hr": round(sum(seg_hrs) / len(seg_hrs)) if seg_hrs else None,
            "ele": round(prev_ele, 1) if prev_ele is not None else None,
        })

    return splits


def compute_best_effort(tps, target_m):
    """Find fastest pace over a continuous segment of target_m meters."""
    if not tps or tps[-1]["dist_m"] < target_m:
        return None

    best_time = None
    best_date_idx = 0
    j = 0
    for i in range(len(tps)):
        while j < len(tps) and (tps[j]["dist_m"] - tps[i]["dist_m"]) < target_m:
            j += 1
        if j >= len(tps):
            break
        # Interpolate exact endpoint
        d_needed = target_m - (tps[j - 1]["dist_m"] - tps[i]["dist_m"])
        d_seg = tps[j]["dist_m"] - tps[j - 1]["dist_m"]
        if d_seg > 0 and tps[j]["time_s"] is not None and tps[j - 1]["time_s"] is not None:
            ratio = d_needed / d_seg
            t_end = tps[j - 1]["time_s"] + ratio * (tps[j]["time_s"] - tps[j - 1]["time_s"])
        else:
            t_end = tps[j]["time_s"]

        if t_end is not None and tps[i]["time_s"] is not None:
            elapsed = t_end - tps[i]["time_s"]
            if elapsed > 0 and (best_time is None or elapsed < best_time):
                best_time = elapsed
                best_date_idx = i

    if best_time is None:
        return None

    pace = (best_time / 60) / (target_m / 1000)
    return {"time_s": round(best_time, 1), "pace_min_km": round(pace, 2)}


# --------------- Auth Routes ---------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if flask_login.current_user.is_authenticated:
        return redirect("/")

    setup_mode = _needs_setup()

    if request.method == "POST":
        if not password_login_enabled:
            flash("Password login is disabled.")
            return render_template(
                "login.html",
                setup_mode=setup_mode,
                oidc_enabled=oidc_enabled,
                password_login_enabled=password_login_enabled,
            )

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Email and password are required.")
            return render_template(
                "login.html",
                setup_mode=setup_mode,
                oidc_enabled=oidc_enabled,
                password_login_enabled=password_login_enabled,
            )

        if setup_mode:
            # Create first admin user
            user = User(
                email=email,
                password_hash=generate_password_hash(password),
                is_admin=True,
            )
            db.session.add(user)
            db.session.flush()
            # Assign orphan tracks to this first user
            Track.query.filter_by(user_id=None).update({"user_id": user.id})
            db.session.commit()
            flask_login.login_user(user, remember=True)
            return redirect("/")

        # Normal login
        user = User.query.filter_by(email=email).first()
        if user and user.password_hash and check_password_hash(user.password_hash, password):
            flask_login.login_user(user, remember=True)
            next_url = _safe_next_url(request.args.get("next"))
            return redirect(next_url)

        flash("Invalid email or password.")

    return render_template(
        "login.html",
        setup_mode=setup_mode,
        oidc_enabled=oidc_enabled,
        password_login_enabled=password_login_enabled,
    )


@app.route("/auth/oidc")
def auth_oidc():
    if not oidc_enabled:
        return redirect("/login")
    redirect_uri = url_for("auth_callback", _external=True)
    return oauth.oidc.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    if not oidc_enabled:
        return redirect("/login")

    try:
        token = oauth.oidc.authorize_access_token()
    except Exception:
        flash("OIDC authentication failed.")
        return redirect("/login")

    userinfo = token.get("userinfo") or {}
    sub = userinfo.get("sub")
    if not sub:
        flash("OIDC provider did not return a subject identifier.")
        return redirect("/login")

    email = userinfo.get("email", "")

    # Look up by OIDC sub
    user = User.query.filter_by(oidc_sub=sub, oidc_provider=OIDC_PROVIDER_URL).first()

    if not user and email:
        # Try to link to existing user by email (admin pre-created)
        user = User.query.filter_by(email=email).first()
        if user and not user.oidc_sub:
            user.oidc_sub = sub
            user.oidc_provider = OIDC_PROVIDER_URL
            db.session.commit()

    if not user and _needs_setup():
        if not email:
            flash("OIDC provider did not return an email address.")
            return redirect("/login")
        # First user via OIDC becomes admin
        user = User(
            email=email,
            oidc_sub=sub,
            oidc_provider=OIDC_PROVIDER_URL,
            is_admin=True,
        )
        db.session.add(user)
        db.session.flush()
        Track.query.filter_by(user_id=None).update({"user_id": user.id})
        db.session.commit()

    if not user:
        flash("No account found for this identity. Contact your administrator.")
        return redirect("/login")

    flask_login.login_user(user, remember=True)
    return redirect("/")


@app.route("/logout")
def logout():
    flask_login.logout_user()
    return redirect("/login")


# --------------- Admin Routes ---------------

@app.route("/admin")
@admin_required
def admin_panel():
    users = User.query.all()
    user_data = []
    for u in users:
        track_count = Track.query.filter_by(user_id=u.id).count()
        u.track_count = track_count
        user_data.append(u)
    orphan_count = Track.query.filter_by(user_id=None).count()
    return render_template("admin.html", users=user_data, orphan_count=orphan_count,
                           current_user=flask_login.current_user)


@app.route("/admin/users", methods=["POST"])
@admin_required
def admin_create_user():
    email = request.form.get("email", "").strip().lower()
    if not email:
        flash("Email is required.", "error")
        return redirect("/admin")

    if User.query.filter_by(email=email).first():
        flash(f"User '{email}' already exists.", "error")
        return redirect("/admin")

    password = request.form.get("password", "")
    user = User(
        email=email,
        password_hash=generate_password_hash(password) if password else None,
        is_admin=bool(request.form.get("is_admin")),
    )
    db.session.add(user)
    db.session.commit()
    flash(f"User '{email}' created.", "success")
    return redirect("/admin")


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_delete_user(uid):
    if uid == flask_login.current_user.id:
        flash("Cannot delete your own account.", "error")
        return redirect("/admin")
    user = db.session.get(User, uid)
    if not user:
        flash("User not found.", "error")
        return redirect("/admin")
    # Orphan their tracks (don't delete data)
    Track.query.filter_by(user_id=uid).update({"user_id": None})
    db.session.delete(user)
    db.session.commit()
    flash(f"User '{user.email}' deleted.", "success")
    return redirect("/admin")


@app.route("/admin/orphan-tracks", methods=["POST"])
@admin_required
def admin_assign_orphans():
    target_uid = request.form.get("user_id", type=int)
    if not target_uid or not db.session.get(User, target_uid):
        flash("Invalid user.", "error")
        return redirect("/admin")
    count = Track.query.filter_by(user_id=None).update({"user_id": target_uid})
    db.session.commit()
    flash(f"{count} tracks assigned.", "success")
    return redirect("/admin")


@app.route("/admin/orphan-tracks/delete", methods=["POST"])
@admin_required
def admin_delete_orphans():
    count = Track.query.filter_by(user_id=None).delete()
    db.session.commit()
    flash(f"{count} orphan tracks deleted.", "success")
    return redirect("/admin")


# --------------- App Routes ---------------

@app.route("/")
@flask_login.login_required
def index():
    return render_template("index.html",
                           user=flask_login.current_user,
                           is_admin=flask_login.current_user.is_admin)


@app.route("/upload", methods=["POST"])
@flask_login.login_required
def upload():
    files = request.files.getlist("files[]")
    if not files:
        return jsonify({"results": []}), 400
    try:
        extra_data = json.loads(request.form.get("track_metadata") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        extra_data = {}

    uid = flask_login.current_user.id
    all_results = []
    for f in files:
        results = process_file(f, uid, extra_data=extra_data)
        all_results.extend(results)

    return jsonify({"results": [[r[0], r[1], r[2]] for r in all_results]})


@app.route("/share", methods=["POST", "GET"])
@flask_login.login_required
def share_target():
    """Web Share Target: receive files shared from other apps (e.g. OpenTracks)."""
    files = request.files.getlist("files")
    if not files:
        return redirect("/")
    uid = flask_login.current_user.id
    for f in files:
        process_file(f, uid)
    return redirect("/")


@app.route("/api/track/manual", methods=["POST"])
@flask_login.login_required
def add_manual():
    d = request.get_json(force=True)
    date = d.get("date")
    started_at = d.get("started_at")
    name = d.get("name") or "Manual workout"
    activity_type = _normalized_activity_type(d.get("activity_type"))
    distance_km = float(d.get("distance_km") or 0)
    duration_min = float(d.get("duration_min") or 0)
    if distance_km <= 0 or duration_min <= 0:
        return jsonify({"error": "Distance and duration are required"}), 400
    if not date:
        return jsonify({"error": "Date is required"}), 400

    distance_m = distance_km * 1000
    duration_s = duration_min * 60
    avg_speed_ms = distance_m / duration_s if duration_s > 0 else 0
    pace = duration_min / distance_km if distance_km > 0 else 0
    metadata = _sanitize_track_metadata(activity_type, d.get("extra_data") or {})

    track = Track(
        filename="manual",
        name=name,
        activity_type=activity_type,
        date=date,
        started_at=started_at,
        duration_s=duration_s,
        distance_m=distance_m,
        avg_speed_ms=avg_speed_ms,
        max_speed_ms=avg_speed_ms,
        elevation_gain_m=float(d.get("elevation_gain") or 0),
        elevation_loss_m=float(d.get("elevation_loss") or 0),
        avg_hr=float(d.get("avg_hr")) if d.get("avg_hr") else None,
        max_hr=float(d.get("max_hr")) if d.get("max_hr") else None,
        avg_cadence=float(d.get("avg_cadence")) if d.get("avg_cadence") else None,
        calories=float(d.get("calories")) if d.get("calories") else None,
        tags=d.get("tags") or "",
        notes=d.get("notes") or "",
        extra_data=json.dumps(metadata) if metadata else None,
        user_id=flask_login.current_user.id,
    )
    db.session.add(track)
    db.session.commit()
    return jsonify({"ok": True, "id": track.id})


@app.route("/delete/<int:track_id>", methods=["POST"])
@flask_login.login_required
def delete_track(track_id):
    track = Track.query.filter_by(id=track_id, user_id=flask_login.current_user.id).first_or_404()
    db.session.delete(track)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/stats")
@flask_login.login_required
def api_stats():
    tracks = user_tracks().order_by(Track.date.asc()).all()
    cumulative = 0.0
    result = []
    for t in tracks:
        dist_km = (t.distance_m or 0) / 1000
        cumulative += dist_km
        duration_min = (t.duration_s or 0) / 60
        moving_min = (t.moving_time_s or t.duration_s or 0) / 60
        avg_speed_kmh = (t.avg_speed_ms or 0) * 3.6
        max_speed_kmh = (t.max_speed_ms or 0) * 3.6
        pace = moving_min / dist_km if dist_km > 0 else None
        weather = None
        if t.weather_json:
            try:
                weather_payload = json.loads(t.weather_json)
                snapshots = weather_payload.get("snapshots") or []
                chosen = next((s for s in snapshots if s.get("label") == "mid"), None) or (snapshots[0] if snapshots else None)
                if chosen:
                    weather = {
                        "temperature_c": chosen.get("temperature_c"),
                        "relative_humidity_pct": chosen.get("relative_humidity_pct"),
                        "precipitation_mm_per_hour": chosen.get("precipitation_mm_per_hour"),
                        "weather_code": chosen.get("weather_code"),
                        "weather_label": chosen.get("weather_label"),
                        "precipitation_total_mm": weather_payload.get("precipitation_total_mm"),
                        "time_accuracy": weather_payload.get("time_accuracy"),
                    }
            except (TypeError, ValueError, json.JSONDecodeError):
                weather = None

        result.append({
            "id": t.id,
            "date": t.date,
            "name": t.name,
            "activity_type": _normalized_activity_type(t.activity_type),
            "distance_km": round(dist_km, 2),
            "duration_min": round(duration_min, 2),
            "moving_min": round(moving_min, 2),
            "pace_min_km": round(pace, 2) if pace else None,
            "avg_speed_kmh": round(avg_speed_kmh, 2),
            "max_speed_kmh": round(max_speed_kmh, 2),
            "elevation_gain": round(t.elevation_gain_m or 0, 1),
            "elevation_loss": round(t.elevation_loss_m or 0, 1),
            "avg_hr": round(t.avg_hr) if t.avg_hr else None,
            "max_hr": round(t.max_hr) if t.max_hr else None,
            "avg_cadence": round(t.avg_cadence) if t.avg_cadence else None,
            "cumulative_km": round(cumulative, 2),
            "tags": t.tags or "",
            "notes": t.notes or "",
            "perceived_effort": _load_track_metadata(t).get("perceived_effort"),
            "weather": weather,
        })

    return jsonify(result)


@app.route("/api/geo")
@flask_login.login_required
def api_geo():
    tracks = user_tracks().order_by(Track.date.asc()).all()
    result = []
    for t in tracks:
        if not t.coordinates:
            continue
        result.append({
            "id": t.id, "name": t.name, "date": t.date,
            "distance_km": round((t.distance_m or 0) / 1000, 2),
            "coordinates": json.loads(t.coordinates),
        })
    return jsonify(result)


@app.route("/api/track/<int:tid>")
@flask_login.login_required
def api_track_detail(tid):
    t = Track.query.filter_by(id=tid, user_id=flask_login.current_user.id).first_or_404()
    tps = json.loads(t.trackpoints) if t.trackpoints else []
    split_m = request.args.get("split_m", 1000, type=int)
    split_m = max(100, min(split_m, 10000))  # clamp 100m-10km
    splits = compute_splits(tps, split_m) if tps else []

    dist_km = (t.distance_m or 0) / 1000
    duration_min = (t.duration_s or 0) / 60
    moving_min = (t.moving_time_s or t.duration_s or 0) / 60

    return jsonify({
        "id": t.id, "name": t.name, "date": t.date,
        "started_at": t.started_at,
        "ended_at": t.ended_at,
        "activity_type": _normalized_activity_type(t.activity_type),
        "distance_km": round(dist_km, 2),
        "duration_min": round(duration_min, 2),
        "moving_min": round(moving_min, 2),
        "pace_min_km": round(moving_min / dist_km, 2) if dist_km > 0 else None,
        "elevation_gain": round(t.elevation_gain_m or 0, 1),
        "elevation_loss": round(t.elevation_loss_m or 0, 1),
        "avg_hr": round(t.avg_hr) if t.avg_hr else None,
        "max_hr": round(t.max_hr) if t.max_hr else None,
        "avg_cadence": round(t.avg_cadence) if t.avg_cadence else None,
        "tags": t.tags or "",
        "notes": t.notes or "",
        "extra_data": _load_track_metadata(t),
        "trackpoints": tps,
        "splits": splits,
    })


@app.route("/api/track/<int:tid>/weather")
@flask_login.login_required
def api_track_weather(tid):
    t = Track.query.filter_by(id=tid, user_id=flask_login.current_user.id).first_or_404()
    refresh = bool(request.args.get("refresh", type=int))
    try:
        return jsonify(_get_or_fetch_weather(t, refresh=refresh))
    except ValueError as exc:
        return jsonify({"available": False, "error": str(exc)}), 200
    except requests.RequestException:
        return jsonify({"available": False, "error": "Weather provider request failed."}), 502


@app.route("/api/records")
@flask_login.login_required
def api_records():
    tracks = user_tracks().order_by(Track.date.asc()).all()

    effort_distances = {
        "running": [1000, 5000, 10000, 21097, 42195],
        "trail_running": [1000, 5000, 10000],
        "cycling": [5000, 10000, 20000, 50000, 100000],
        "walking": [1000, 5000, 10000],
        "hiking": [1000, 5000, 10000],
    }
    effort_labels = {
        1000: "1km", 5000: "5km", 10000: "10km",
        20000: "20km", 21097: "Half Marathon", 42195: "Marathon",
        50000: "50km", 100000: "100km",
    }

    # Group tracks by sport
    by_sport = {}
    for t in tracks:
        sport = _normalized_activity_type(t.activity_type)
        by_sport.setdefault(sport, []).append(t)

    all_sports = sorted(by_sport.keys())
    result = {}

    for sport, sport_tracks in by_sport.items():
        distances = effort_distances.get(sport, [1000, 5000, 10000])
        best_efforts = {}
        best_pace = None
        best_speed = None
        longest = None
        most_elevation = None
        longest_duration = None

        for t in sport_tracks:
            dk = (t.distance_m or 0) / 1000
            dm = (t.duration_s or 0) / 60
            mm = (t.moving_time_s or t.duration_s or 0) / 60  # moving minutes

            # Best efforts (only for tracks with trackpoints)
            if t.trackpoints:
                tps = json.loads(t.trackpoints)
                for dist in distances:
                    key = effort_labels.get(dist, f"{dist // 1000}km")
                    effort = compute_best_effort(tps, dist)
                    if effort:
                        if key not in best_efforts or effort["pace_min_km"] < best_efforts[key]["pace_min_km"]:
                            best_efforts[key] = {
                                **effort, "track_id": t.id,
                                "track_name": t.name, "date": t.date,
                            }

            # Best pace (based on moving time)
            if dk > 0 and mm > 0:
                pace = mm / dk
                if best_pace is None or pace < best_pace["pace_min_km"]:
                    best_pace = {"pace_min_km": round(pace, 2), "track_id": t.id,
                                 "track_name": t.name, "date": t.date}

            # Best avg speed (based on moving time)
            if dk > 0 and mm > 0:
                speed = dk / (mm / 60)
                if best_speed is None or speed > best_speed["speed_kmh"]:
                    best_speed = {"speed_kmh": round(speed, 1), "track_id": t.id,
                                  "track_name": t.name, "date": t.date}

            # Longest distance
            if longest is None or dk > longest["distance_km"]:
                longest = {"distance_km": round(dk, 2), "track_id": t.id,
                           "track_name": t.name, "date": t.date}

            # Most elevation gain
            eg = t.elevation_gain_m or 0
            if eg > 0 and (most_elevation is None or eg > most_elevation["elevation_m"]):
                most_elevation = {"elevation_m": round(eg, 1), "track_id": t.id,
                                  "track_name": t.name, "date": t.date}

            # Longest duration
            ds = t.duration_s or 0
            if ds > 0 and (longest_duration is None or ds > longest_duration["duration_s"]):
                longest_duration = {"duration_s": round(ds, 1), "track_id": t.id,
                                    "track_name": t.name, "date": t.date}

        result[sport] = {
            "best_efforts": best_efforts,
            "best_pace": best_pace,
            "best_speed": best_speed,
            "longest": longest,
            "most_elevation": most_elevation,
            "longest_duration": longest_duration,
        }

    return jsonify({"sports": all_sports, "records": result})


@app.route("/api/track/<int:tid>", methods=["PUT"])
@flask_login.login_required
def api_track_update(tid):
    t = Track.query.filter_by(id=tid, user_id=flask_login.current_user.id).first_or_404()
    data = request.get_json(force=True)
    activity_type = _normalized_activity_type(data.get("activity_type", t.activity_type))
    if "tags" in data:
        t.tags = data["tags"]
    if "notes" in data:
        t.notes = data["notes"]
    if "activity_type" in data:
        t.activity_type = activity_type
    if "extra_data" in data:
        metadata = _sanitize_track_metadata(activity_type, data.get("extra_data") or {})
        t.extra_data = json.dumps(metadata) if metadata else None
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/tracks/bulk-metadata", methods=["PUT"])
@flask_login.login_required
def api_tracks_bulk_metadata():
    data = request.get_json(force=True)
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "No tracks selected"}), 400

    track_ids = []
    for raw_id in ids:
        try:
            track_ids.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    if not track_ids:
        return jsonify({"ok": False, "error": "No valid track ids"}), 400

    payload = data.get("extra_data") or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Invalid metadata payload"}), 400

    tracks = Track.query.filter(
        Track.user_id == flask_login.current_user.id,
        Track.id.in_(track_ids),
    ).all()
    if not tracks:
        return jsonify({"ok": False, "error": "Tracks not found"}), 404

    updated = 0
    for track in tracks:
        merged = _load_track_metadata(track)
        merged.update(payload)
        metadata = _sanitize_track_metadata(track.activity_type, merged)
        track.extra_data = json.dumps(metadata) if metadata else None
        updated += 1

    db.session.commit()
    return jsonify({"ok": True, "updated": updated})


@app.route("/api/export/csv")
@flask_login.login_required
def export_csv():
    tracks = user_tracks().order_by(Track.date.asc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Name", "Activity Type", "Distance (km)", "Duration (min)",
                      "Pace (min/km)", "Avg Speed (km/h)", "Max Speed (km/h)",
                      "Elevation Gain (m)", "Elevation Loss (m)",
                      "Avg HR", "Max HR", "Avg Cadence", "Calories", "Tags", "Notes"])
    for t in tracks:
        dist_km = (t.distance_m or 0) / 1000
        duration_min = (t.duration_s or 0) / 60
        pace = round(duration_min / dist_km, 2) if dist_km > 0 else ""
        writer.writerow([
            t.date, t.name, _normalized_activity_type(t.activity_type),
            round(dist_km, 2), round(duration_min, 2), pace,
            round((t.avg_speed_ms or 0) * 3.6, 2),
            round((t.max_speed_ms or 0) * 3.6, 2),
            round(t.elevation_gain_m or 0, 1),
            round(t.elevation_loss_m or 0, 1),
            round(t.avg_hr) if t.avg_hr else "",
            round(t.max_hr) if t.max_hr else "",
            round(t.avg_cadence) if t.avg_cadence else "",
            round(t.calories) if t.calories else "",
            t.tags or "", t.notes or "",
        ])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=stridelog_export.csv"})


@app.route("/api/export/json")
@flask_login.login_required
def export_json():
    tracks = user_tracks().order_by(Track.date.asc()).all()
    result = []
    for t in tracks:
        dist_km = (t.distance_m or 0) / 1000
        duration_min = (t.duration_s or 0) / 60
        result.append({
            "date": t.date, "name": t.name,
            "activity_type": _normalized_activity_type(t.activity_type),
            "distance_km": round(dist_km, 2),
            "duration_min": round(duration_min, 2),
            "pace_min_km": round(duration_min / dist_km, 2) if dist_km > 0 else None,
            "avg_speed_kmh": round((t.avg_speed_ms or 0) * 3.6, 2),
            "max_speed_kmh": round((t.max_speed_ms or 0) * 3.6, 2),
            "elevation_gain_m": round(t.elevation_gain_m or 0, 1),
            "elevation_loss_m": round(t.elevation_loss_m or 0, 1),
            "avg_hr": round(t.avg_hr) if t.avg_hr else None,
            "max_hr": round(t.max_hr) if t.max_hr else None,
            "avg_cadence": round(t.avg_cadence) if t.avg_cadence else None,
            "calories": round(t.calories) if t.calories else None,
            "tags": t.tags or "", "notes": t.notes or "",
        })
    return Response(json.dumps(result, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": "attachment;filename=stridelog_export.json"})


@app.route("/api/backup")
@admin_required
def backup_db():
    db_path = os.path.join(DATA_DIR, "tracks.db")
    if not os.path.exists(db_path):
        return jsonify({"error": "Database not found"}), 404
    # Create a safe copy to avoid locking issues
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy2(db_path, tmp.name)
    return send_file(tmp.name, as_attachment=True, download_name="stridelog_backup.db",
                     mimetype="application/x-sqlite3")


@app.route("/api/restore", methods=["POST"])
@admin_required
def restore_db():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file provided"}), 400
    # Validate it's a real SQLite file
    header = f.read(16)
    if not header.startswith(b"SQLite format 3"):
        return jsonify({"error": "Not a valid SQLite database"}), 400
    f.seek(0)
    db_path = os.path.join(DATA_DIR, "tracks.db")
    # Save backup of current db
    if os.path.exists(db_path):
        shutil.copy2(db_path, db_path + ".bak")
    # Close current connections
    db.session.remove()
    db.engine.dispose()
    # Write new db
    f.save(db_path)
    return jsonify({"ok": True, "message": "Database restored. Please reload the page."})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
