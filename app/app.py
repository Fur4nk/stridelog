import os
import math
import zipfile
import tempfile
from datetime import datetime, timezone
from xml.etree.ElementTree import parse as ET_parse, fromstring as ET_fromstring

from flask import Flask, render_template, request, jsonify
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


with app.app_context():
    db.create_all()


# --------------- GPX Parsing ---------------

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
    results = []
    for child in elem.iter():
        if strip_ns(child.tag) == local_name:
            results.append(child)
    return results


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


def parse_gpx_content(xml_bytes, filename="unknown"):
    root = ET_fromstring(xml_bytes)

    # Extract track_id from opentracks:trackid
    track_id = None
    for elem in root.iter():
        if strip_ns(elem.tag) == "trackid" and elem.text:
            track_id = elem.text.strip()
            break

    # Track name
    trk_elems = find_all_recursive(root, "trk")
    if not trk_elems:
        return None, "No track found in GPX"

    trk = trk_elems[0]
    name = get_text(trk, "name") or filename
    activity_type = get_text(trk, "type") or "running"

    # Collect trackpoints
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

        time_text = get_text(pt, "time")
        time = parse_timestamp(time_text)

        # Extensions (Garmin TrackPointExtension)
        hr = None
        cad = None
        speed = None
        for ext in find_all_recursive(pt, "hr"):
            if ext.text:
                hr = float(ext.text)
                break
        for ext in find_all_recursive(pt, "cad"):
            if ext.text:
                cad = float(ext.text)
                break
        if cad is None:
            for ext in find_all_recursive(pt, "RunCadence"):
                if ext.text:
                    cad = float(ext.text)
                    break
        for ext in find_all_recursive(pt, "speed"):
            if ext.text:
                speed = float(ext.text)
                break

        points.append({
            "lat": lat, "lon": lon, "ele": ele, "time": time,
            "hr": hr, "cad": cad, "speed": speed,
        })

    if len(points) < 2:
        return None, "Not enough trackpoints"

    # Calculate metrics
    total_distance = 0.0
    elevation_gain = 0.0
    elevation_loss = 0.0
    max_speed = 0.0
    hrs = []
    cads = []
    speeds = []

    for i in range(1, len(points)):
        p0, p1 = points[i - 1], points[i]
        d = haversine(p0["lat"], p0["lon"], p1["lat"], p1["lon"])
        total_distance += d

        if p0["ele"] is not None and p1["ele"] is not None:
            diff = p1["ele"] - p0["ele"]
            if diff > 0:
                elevation_gain += diff
            else:
                elevation_loss += abs(diff)

        # Speed from extensions or calculated
        if p1["speed"] is not None:
            speeds.append(p1["speed"])
        elif p0["time"] and p1["time"]:
            dt = (p1["time"] - p0["time"]).total_seconds()
            if dt > 0:
                speeds.append(d / dt)

    for p in points:
        if p["hr"] is not None:
            hrs.append(p["hr"])
        if p["cad"] is not None:
            cads.append(p["cad"])

    if speeds:
        max_speed = max(speeds)

    # Duration
    times = [p["time"] for p in points if p["time"] is not None]
    if len(times) >= 2:
        duration_s = (times[-1] - times[0]).total_seconds()
    else:
        duration_s = 0

    avg_speed = total_distance / duration_s if duration_s > 0 else 0
    date_str = times[0].strftime("%Y-%m-%d") if times else ""

    track_data = {
        "filename": filename,
        "name": name,
        "activity_type": activity_type,
        "date": date_str,
        "duration_s": duration_s,
        "distance_m": total_distance,
        "avg_speed_ms": avg_speed,
        "max_speed_ms": max_speed,
        "elevation_gain_m": elevation_gain,
        "elevation_loss_m": elevation_loss,
        "avg_hr": sum(hrs) / len(hrs) if hrs else None,
        "max_hr": max(hrs) if hrs else None,
        "avg_cadence": sum(cads) / len(cads) if cads else None,
        "calories": None,
        "track_id": track_id,
    }

    return track_data, None


def parse_kml_content(xml_bytes, filename="unknown"):
    root = ET_fromstring(xml_bytes)

    # Extract track_id from opentracks:trackid
    track_id = None
    for elem in root.iter():
        if strip_ns(elem.tag) == "trackid" and elem.text:
            track_id = elem.text.strip()
            break

    # Find Placemark with Track
    placemarks = find_all_recursive(root, "Placemark")
    if not placemarks:
        return None, "No Placemark found in KML"

    pm = placemarks[0]
    name_elem = find_child(pm, "name")
    name = name_elem.text.strip() if name_elem is not None and name_elem.text else filename

    # Activity type from ExtendedData
    activity_type = "running"
    for data_elem in find_all_recursive(pm, "Data"):
        if data_elem.get("name") == "activityType":
            val = find_child(data_elem, "value")
            if val is not None and val.text:
                activity_type = val.text.strip()
                break

    # Collect <when> and <coord> from Track elements
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

    # Parse SimpleArrayData for speed, heartrate, cadence
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

    # Build points
    points = []
    for i in range(len(whens)):
        time = parse_timestamp(whens[i])
        coord_parts = coords[i].split()
        if len(coord_parts) < 2:
            points.append({"lat": None, "lon": None, "ele": None, "time": time,
                           "hr": None, "cad": None, "speed": None})
            continue

        lon = float(coord_parts[0])
        lat = float(coord_parts[1])
        ele = float(coord_parts[2]) if len(coord_parts) >= 3 else None

        speed = None
        if i < len(speed_arr) and speed_arr[i]:
            try:
                speed = float(speed_arr[i])
            except ValueError:
                pass

        hr = None
        if i < len(hr_arr) and hr_arr[i]:
            try:
                hr = float(hr_arr[i])
            except ValueError:
                pass

        cad = None
        if i < len(cad_arr) and cad_arr[i]:
            try:
                cad = float(cad_arr[i])
            except ValueError:
                pass

        points.append({
            "lat": lat, "lon": lon, "ele": ele, "time": time,
            "hr": hr, "cad": cad, "speed": speed,
        })

    # Filter out points with no coordinates
    valid_points = [p for p in points if p["lat"] is not None]
    if len(valid_points) < 2:
        return None, "Not enough valid trackpoints"

    # Calculate metrics (same logic as GPX)
    total_distance = 0.0
    elevation_gain = 0.0
    elevation_loss = 0.0
    hrs = []
    cads = []
    speeds = []

    for i in range(1, len(valid_points)):
        p0, p1 = valid_points[i - 1], valid_points[i]
        d = haversine(p0["lat"], p0["lon"], p1["lat"], p1["lon"])
        total_distance += d

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

    for p in valid_points:
        if p["hr"] is not None:
            hrs.append(p["hr"])
        if p["cad"] is not None:
            cads.append(p["cad"])

    max_speed = max(speeds) if speeds else 0

    times = [p["time"] for p in valid_points if p["time"] is not None]
    if len(times) >= 2:
        duration_s = (times[-1] - times[0]).total_seconds()
    else:
        duration_s = 0

    avg_speed = total_distance / duration_s if duration_s > 0 else 0
    date_str = times[0].strftime("%Y-%m-%d") if times else ""

    track_data = {
        "filename": filename,
        "name": name,
        "activity_type": activity_type,
        "date": date_str,
        "duration_s": duration_s,
        "distance_m": total_distance,
        "avg_speed_ms": avg_speed,
        "max_speed_ms": max_speed,
        "elevation_gain_m": elevation_gain,
        "elevation_loss_m": elevation_loss,
        "avg_hr": sum(hrs) / len(hrs) if hrs else None,
        "max_hr": max(hrs) if hrs else None,
        "avg_cadence": sum(cads) / len(cads) if cads else None,
        "calories": None,
        "track_id": track_id,
    }

    return track_data, None


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

        # Dedup
        if track_data["track_id"]:
            existing = Track.query.filter_by(track_id=track_data["track_id"]).first()
            if existing:
                output.append((fname, False, "Duplicate track"))
                continue

        track = Track(**track_data)
        db.session.add(track)
        db.session.commit()

        # Save file
        upload_path = os.path.join(DATA_DIR, "uploads", fname)
        os.makedirs(os.path.dirname(upload_path), exist_ok=True)
        with open(upload_path, "wb") as f:
            f.write(xml_bytes)

        output.append((fname, True, "OK"))

    return output


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
            "distance_km": round(dist_km, 2),
            "duration_min": round(duration_min, 2),
            "pace_min_km": round(pace, 2) if pace else None,
            "avg_speed_kmh": round(avg_speed_kmh, 2),
            "max_speed_kmh": round(max_speed_kmh, 2),
            "elevation_gain": round(t.elevation_gain_m or 0, 1),
            "avg_hr": round(t.avg_hr) if t.avg_hr else None,
            "max_hr": round(t.max_hr) if t.max_hr else None,
            "avg_cadence": round(t.avg_cadence) if t.avg_cadence else None,
            "cumulative_km": round(cumulative, 2),
        })

    return jsonify(result)


@app.route("/api/summary")
def api_summary():
    tracks = Track.query.all()
    if not tracks:
        return jsonify({
            "total_tracks": 0, "total_km": 0, "total_hours": 0,
            "avg_pace": None, "best_pace": None, "longest_km": 0,
        })

    total_km = sum((t.distance_m or 0) / 1000 for t in tracks)
    total_s = sum(t.duration_s or 0 for t in tracks)
    total_hours = total_s / 3600

    paces = []
    longest = 0
    for t in tracks:
        dk = (t.distance_m or 0) / 1000
        dm = (t.duration_s or 0) / 60
        if dk > 0:
            paces.append(dm / dk)
        if dk > longest:
            longest = dk

    avg_pace = sum(paces) / len(paces) if paces else None
    best_pace = min(paces) if paces else None

    return jsonify({
        "total_tracks": len(tracks),
        "total_km": round(total_km, 2),
        "total_hours": round(total_hours, 1),
        "avg_pace": round(avg_pace, 2) if avg_pace else None,
        "best_pace": round(best_pace, 2) if best_pace else None,
        "longest_km": round(longest, 2),
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
