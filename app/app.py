import os
import io
import csv
import json
import math
import shutil
import zipfile
import tempfile
from datetime import datetime, timezone
from xml.etree.ElementTree import parse as ET_parse, fromstring as ET_fromstring

from flask import Flask, render_template, request, jsonify, Response, send_file
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__, template_folder="../templates", static_folder="../static")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "uploads"), exist_ok=True)

app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DATA_DIR}/tracks.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

db = SQLAlchemy(app)


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
    track_id = db.Column(db.String(256), unique=True)
    coordinates = db.Column(db.Text)
    trackpoints = db.Column(db.Text)
    tags = db.Column(db.Text)        # comma-separated tags
    notes = db.Column(db.Text)       # free-text notes


with app.app_context():
    db.create_all()
    from sqlalchemy import inspect as sa_inspect
    inspector = sa_inspect(db.engine)
    cols = [c["name"] for c in inspector.get_columns("track")]
    for col_name in ("coordinates", "trackpoints", "tags", "notes"):
        if col_name not in cols:
            with db.engine.connect() as conn:
                conn.execute(db.text(f"ALTER TABLE track ADD COLUMN {col_name} TEXT"))
                conn.commit()


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

        if p1["speed"] is not None:
            speeds.append(p1["speed"])
        elif p0["time"] and p1["time"]:
            dt = (p1["time"] - p0["time"]).total_seconds()
            if dt > 0:
                speeds.append(d / dt)

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

    # Simplified coordinates for map overlay
    step = max(1, len(points) // 200)
    coord_list = [[round(p["lat"], 6), round(p["lon"], 6)] for p in points[::step]]

    metrics = {
        "date": date_str,
        "duration_s": duration_s,
        "distance_m": cum_dist,
        "avg_speed_ms": avg_speed,
        "max_speed_ms": max_speed,
        "elevation_gain_m": elevation_gain,
        "elevation_loss_m": elevation_loss,
        "avg_hr": sum(hrs) / len(hrs) if hrs else None,
        "max_hr": max(hrs) if hrs else None,
        "avg_cadence": sum(cads) / len(cads) if cads else None,
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
    activity_type = get_text(trk, "type") or "running"

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
                activity_type = val.text.strip()
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

def process_file(file_storage):
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
            existing = Track.query.filter_by(track_id=track_data["track_id"]).first()
            if existing:
                output.append((fname, False, "Duplicate track"))
                continue

        track = Track(**track_data)
        db.session.add(track)
        db.session.commit()

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


# --------------- Routes ---------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("files[]")
    if not files:
        return jsonify({"results": []}), 400

    all_results = []
    for f in files:
        results = process_file(f)
        all_results.extend(results)

    return jsonify({"results": [[r[0], r[1], r[2]] for r in all_results]})


@app.route("/api/track/manual", methods=["POST"])
def add_manual():
    d = request.get_json(force=True)
    date = d.get("date")
    name = d.get("name") or "Manual workout"
    activity_type = d.get("activity_type") or "running"
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

    track = Track(
        filename="manual",
        name=name,
        activity_type=activity_type,
        date=date,
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
    )
    db.session.add(track)
    db.session.commit()
    return jsonify({"ok": True, "id": track.id})


@app.route("/delete/<int:track_id>", methods=["POST"])
def delete_track(track_id):
    track = Track.query.get_or_404(track_id)
    db.session.delete(track)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/stats")
def api_stats():
    tracks = Track.query.order_by(Track.date.asc()).all()
    cumulative = 0.0
    result = []
    for t in tracks:
        dist_km = (t.distance_m or 0) / 1000
        cumulative += dist_km
        duration_min = (t.duration_s or 0) / 60
        avg_speed_kmh = (t.avg_speed_ms or 0) * 3.6
        max_speed_kmh = (t.max_speed_ms or 0) * 3.6
        pace = duration_min / dist_km if dist_km > 0 else None

        result.append({
            "id": t.id,
            "date": t.date,
            "name": t.name,
            "activity_type": t.activity_type or "running",
            "distance_km": round(dist_km, 2),
            "duration_min": round(duration_min, 2),
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
        })

    return jsonify(result)


@app.route("/api/geo")
def api_geo():
    tracks = Track.query.order_by(Track.date.asc()).all()
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
def api_track_detail(tid):
    t = Track.query.get_or_404(tid)
    tps = json.loads(t.trackpoints) if t.trackpoints else []
    split_m = request.args.get("split_m", 1000, type=int)
    split_m = max(100, min(split_m, 10000))  # clamp 100m-10km
    splits = compute_splits(tps, split_m) if tps else []

    dist_km = (t.distance_m or 0) / 1000
    duration_min = (t.duration_s or 0) / 60

    return jsonify({
        "id": t.id, "name": t.name, "date": t.date,
        "activity_type": t.activity_type or "running",
        "distance_km": round(dist_km, 2),
        "duration_min": round(duration_min, 2),
        "pace_min_km": round(duration_min / dist_km, 2) if dist_km > 0 else None,
        "elevation_gain": round(t.elevation_gain_m or 0, 1),
        "elevation_loss": round(t.elevation_loss_m or 0, 1),
        "avg_hr": round(t.avg_hr) if t.avg_hr else None,
        "max_hr": round(t.max_hr) if t.max_hr else None,
        "avg_cadence": round(t.avg_cadence) if t.avg_cadence else None,
        "tags": t.tags or "",
        "notes": t.notes or "",
        "trackpoints": tps,
        "splits": splits,
    })


@app.route("/api/records")
def api_records():
    tracks = Track.query.order_by(Track.date.asc()).all()
    distances = [1000, 5000, 10000]
    records = {}

    for dist in distances:
        key = f"{dist // 1000}km"
        records[key] = None

    # Best overall pace
    best_pace = None
    best_pace_track = None

    for t in tracks:
        if not t.trackpoints:
            continue
        tps = json.loads(t.trackpoints)

        # Best efforts
        for dist in distances:
            key = f"{dist // 1000}km"
            effort = compute_best_effort(tps, dist)
            if effort:
                if records[key] is None or effort["pace_min_km"] < records[key]["pace_min_km"]:
                    records[key] = {**effort, "track_id": t.id, "track_name": t.name, "date": t.date}

        # Best overall pace
        dk = (t.distance_m or 0) / 1000
        dm = (t.duration_s or 0) / 60
        if dk > 0:
            pace = dm / dk
            if best_pace is None or pace < best_pace:
                best_pace = pace
                best_pace_track = {"pace_min_km": round(pace, 2), "track_id": t.id,
                                   "track_name": t.name, "date": t.date}

    # Longest run
    longest = None
    for t in tracks:
        dk = (t.distance_m or 0) / 1000
        if longest is None or dk > longest["distance_km"]:
            longest = {"distance_km": round(dk, 2), "track_id": t.id,
                        "track_name": t.name, "date": t.date}

    return jsonify({
        "best_efforts": records,
        "best_pace": best_pace_track,
        "longest_run": longest,
    })


@app.route("/api/track/<int:tid>", methods=["PUT"])
def api_track_update(tid):
    t = Track.query.get_or_404(tid)
    data = request.get_json(force=True)
    if "tags" in data:
        t.tags = data["tags"]
    if "notes" in data:
        t.notes = data["notes"]
    if "activity_type" in data:
        t.activity_type = data["activity_type"]
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/export/csv")
def export_csv():
    tracks = Track.query.order_by(Track.date.asc()).all()
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
            t.date, t.name, t.activity_type or "running",
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
def export_json():
    tracks = Track.query.order_by(Track.date.asc()).all()
    result = []
    for t in tracks:
        dist_km = (t.distance_m or 0) / 1000
        duration_min = (t.duration_s or 0) / 60
        result.append({
            "date": t.date, "name": t.name,
            "activity_type": t.activity_type or "running",
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
