import os
import re
import threading
import tkinter as tk
from collections import OrderedDict
from datetime import datetime, timezone
from io import BytesIO

import requests
import tkintermapview
from PIL import Image, ImageTk

# ---------------- CONFIG ----------------
API_KEY = os.getenv("OPENWEATHER_API_KEY", "209e4eac04486e3a9e9d8cac97176b7e")
OWM = "https://api.openweathermap.org/data/2.5"
NOMINATIM = "https://nominatim.openstreetmap.org"
ICON_URL = "https://openweathermap.org/img/wn/{}@2x.png"
HEADERS = {"User-Agent": "WeatherMapApp/1.0 (student project)"}
TIMEOUT = 10
HOURLY_SLOTS = 6   # free API: 3-hour steps
DAILY_DAYS = 5

# Colors
BG = "#0f172a"
CARD = "#1e293b"
TEXT = "#e2e8f0"
MUTED = "#94a3b8"
ACCENT = "#38bdf8"
ERR = "#f87171"


class WeatherError(Exception):
    pass


# ---------------- API LAYER ----------------
def http_get(url, params=None, headers=None):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    except requests.exceptions.Timeout:
        raise WeatherError("Request timed out. Please try again.")
    except requests.exceptions.ConnectionError:
        raise WeatherError("No internet connection.")
    except requests.exceptions.RequestException as e:
        raise WeatherError(f"Network error: {e}")
    return r


def owm_get(endpoint, lat, lon):
    params = {"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"}
    r = http_get(f"{OWM}/{endpoint}", params)
    if r.status_code == 401:
        raise WeatherError("Invalid API key. (New keys can take up to 2 hours to activate.)")
    if r.status_code == 429:
        raise WeatherError("Too many requests. Wait a minute and retry.")
    if r.status_code != 200:
        raise WeatherError(f"Weather server error ({r.status_code}).")
    return r.json()


def span_from_bbox(bb):
    south, north, west, east = [float(x) for x in bb]
    return max(north - south, east - west)


def label_from_address(addr, fallback):
    name = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("county") or addr.get("state") or fallback)
    parts = [name]
    if addr.get("state") and addr.get("state") != name:
        parts.append(addr["state"])
    if addr.get("country"):
        parts.append(addr["country"])
    return ", ".join(parts)


def geocode(query):
    """City / state / PIN code / any place -> lat, lon, label, span."""
    if re.fullmatch(r"\d{5,6}", query):
        query += ", India"
    r = http_get(f"{NOMINATIM}/search",
                 {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1},
                 HEADERS)
    if r.status_code != 200:
        raise WeatherError("Location search failed. Try again.")
    data = r.json()
    if not data:
        raise WeatherError("Location not found. Try a different name.")
    d = data[0]
    return {
        "lat": float(d["lat"]),
        "lon": float(d["lon"]),
        "label": label_from_address(d.get("address", {}), d.get("name") or query),
        "span": span_from_bbox(d["boundingbox"]),
    }


def reverse_geocode(lat, lon):
    r = http_get(f"{NOMINATIM}/reverse",
                 {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10,
                  "addressdetails": 1}, HEADERS)
    label = f"{lat:.2f}, {lon:.2f}"
    if r.status_code == 200:
        d = r.json()
        if "address" in d:
            label = label_from_address(d["address"], label)
    return {"lat": lat, "lon": lon, "label": label, "span": 0.2}


def detect_location():
    r = http_get("https://ipinfo.io/json")
    if r.status_code != 200:
        raise WeatherError("Could not detect your location.")
    d = r.json()
    try:
        lat, lon = [float(x) for x in d["loc"].split(",")]
    except (KeyError, ValueError):
        raise WeatherError("Could not detect your location.")
    parts = [p for p in (d.get("city"), d.get("region"), d.get("country")) if p]
    return {"lat": lat, "lon": lon, "label": ", ".join(parts), "span": 0.3}


def fetch_icon(code, size):
    try:
        r = requests.get(ICON_URL.format(code), timeout=TIMEOUT)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGBA").resize(size)
    except Exception:
        return None


def zoom_for_span(span):
    if span > 10:
        return 5
    if span > 4:
        return 6
    if span > 1.5:
        return 8
    if span > 0.5:
        return 10
    if span > 0.15:
        return 11
    return 13


# ---------------- GUI ----------------
class WeatherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Weather Map")
        self.geometry("1150x720")
        self.minsize(1000, 640)
        self.configure(bg=BG)

        self.unit = "C"
        self.data = None
        self.place = None
        self.marker = None
        self.anim_id = None
        self.icon_cache = {}
        self.photo_refs = []

        self.city_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.build_ui()

    # ----- layout -----
    def build_ui(self):
        left = tk.Frame(self, bg=BG, width=430)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        right = tk.Frame(self, bg=BG)
        right.pack(side="right", fill="both", expand=True)

        # search row
        search = tk.Frame(left, bg=BG)
        search.pack(fill="x", padx=14, pady=(14, 6))
        self.entry = tk.Entry(search, textvariable=self.city_var, font=("Segoe UI", 12),
                              bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
                              highlightthickness=1, highlightbackground="#334155",
                              highlightcolor=ACCENT)
        self.entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.entry.bind("<Return>", lambda e: self.search())
        self.entry.focus()
        self.btn_get = self.make_button(search, "Search", self.search, ACCENT, "#0f172a")
        self.btn_get.pack(side="left", padx=(8, 0))

        # second row
        row2 = tk.Frame(left, bg=BG)
        row2.pack(fill="x", padx=14)
        self.btn_loc = self.make_button(row2, "📍 My Location", self.use_my_location)
        self.btn_loc.pack(side="left")
        self.btn_unit = self.make_button(row2, "Switch to °F", self.toggle_unit)
        self.btn_unit.pack(side="right")

        # status (errors shown here, inside GUI)
        tk.Label(left, textvariable=self.status_var, fg=ERR, bg=BG, anchor="w",
                 wraplength=400, justify="left",
                 font=("Segoe UI", 10)).pack(fill="x", padx=14, pady=(8, 0))
        self.status_lbl = left.winfo_children()[-1]

        # panels
        self.current_frame = tk.Frame(left, bg=CARD)
        self.current_frame.pack(fill="x", padx=14, pady=(8, 6))
        self.hourly_frame = tk.Frame(left, bg=CARD)
        self.hourly_frame.pack(fill="x", padx=14, pady=6)
        self.daily_frame = tk.Frame(left, bg=CARD)
        self.daily_frame.pack(fill="x", padx=14, pady=6)

        tk.Label(self.current_frame, bg=CARD, fg=MUTED, font=("Segoe UI", 11),
                 text="Search any city, state, PIN code or place.\nYou can also click on the map.",
                 pady=30).pack()

        # map
        self.map = tkintermapview.TkinterMapView(right, corner_radius=0)
        self.map.pack(fill="both", expand=True)
        self.map.set_position(22.5, 79.0)
        self.map.set_zoom(5)
        self.map.add_left_click_map_command(self.on_map_click)

    def make_button(self, parent, text, cmd, bg=CARD, fg=TEXT):
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                         activebackground="#334155", activeforeground=TEXT,
                         relief="flat", bd=0, padx=12, pady=6,
                         font=("Segoe UI", 10, "bold"), cursor="hand2")

    # ----- helpers -----
    def show_status(self, msg, color=ERR):
        self.status_lbl.config(fg=color)
        self.status_var.set(msg)

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.btn_get.config(state=state)
        self.btn_loc.config(state=state)

    def temp(self, c):
        return c if self.unit == "C" else c * 9 / 5 + 32

    def fmt_temp(self, c):
        return f"{self.temp(c):.0f}°{self.unit}"

    def fmt_wind(self, ms):
        return f"{ms:.1f} m/s" if self.unit == "C" else f"{ms * 2.23694:.1f} mph"

    def photo(self, code, size):
        img = self.icon_cache.get((code, size))
        if img is None:
            return None
        ph = ImageTk.PhotoImage(img)
        self.photo_refs.append(ph)
        return ph

    def label(self, parent, text, size=10, bold=False, fg=TEXT):
        return tk.Label(parent, text=text, bg=CARD, fg=fg,
                        font=("Segoe UI", size, "bold" if bold else "normal"))

    # ----- actions -----
    def search(self):
        query = self.city_var.get().strip()
        if not query:
            self.show_status("Please enter a city, state or location.")
            return
        self.run_worker(lambda: geocode(query))

    def use_my_location(self):
        self.run_worker(detect_location)

    def on_map_click(self, coords):
        lat, lon = coords
        self.run_worker(lambda: reverse_geocode(lat, lon))

    def run_worker(self, resolver):
        self.show_status("Loading...", MUTED)
        self.set_busy(True)
        threading.Thread(target=self.worker, args=(resolver,), daemon=True).start()

    def worker(self, resolver):
        try:
            place = resolver()
            current = owm_get("weather", place["lat"], place["lon"])
            forecast = owm_get("forecast", place["lat"], place["lon"])
            codes = {current["weather"][0]["icon"]}
            codes.update(i["weather"][0]["icon"] for i in forecast["list"])
            for code in codes:
                for size in ((72, 72), (40, 40)):
                    if (code, size) not in self.icon_cache:
                        self.icon_cache[(code, size)] = fetch_icon(code, size)
            self.after(0, self.on_success, place, current, forecast)
        except WeatherError as e:
            self.after(0, self.on_error, str(e))
        except Exception as e:
            self.after(0, self.on_error, f"Unexpected error: {e}")

    def on_success(self, place, current, forecast):
        self.set_busy(False)
        self.show_status("")
        self.place = place
        self.data = (current, forecast)
        self.city_var.set(place["label"].split(",")[0])
        self.render()
        self.fly_to(place)

    def on_error(self, msg):
        self.set_busy(False)
        self.show_status(msg)

    def toggle_unit(self):
        self.unit = "F" if self.unit == "C" else "C"
        self.btn_unit.config(text="Switch to °C" if self.unit == "F" else "Switch to °F")
        if self.data:
            self.render()

    # ----- map animation -----
    def fly_to(self, place):
        if self.anim_id:
            self.after_cancel(self.anim_id)
        if self.marker:
            self.marker.delete()
            self.marker = None

        lat, lon = place["lat"], place["lon"]
        target_zoom = zoom_for_span(place["span"])
        start_lat, start_lon = self.map.get_position()
        self.map.set_zoom(min(int(self.map.zoom), 5))  # zoom out first
        steps = 20

        def pan(i=1):
            t = i / steps
            t = t * t * (3 - 2 * t)  # smooth ease
            self.map.set_position(start_lat + (lat - start_lat) * t,
                                  start_lon + (lon - start_lon) * t)
            if i < steps:
                self.anim_id = self.after(25, pan, i + 1)
            else:
                zoom_in()

        def zoom_in():
            z = int(self.map.zoom)
            if z < target_zoom:
                self.map.set_zoom(z + 1)
                self.map.set_position(lat, lon)
                self.anim_id = self.after(130, zoom_in)
            else:
                self.marker = self.map.set_marker(lat, lon, text=place["label"])

        pan()

    # ----- rendering -----
    def clear(self, frame):
        for w in frame.winfo_children():
            w.destroy()

    def render(self):
        current, forecast = self.data
        self.photo_refs = []
        tz = forecast["city"].get("timezone", 0)
        self.render_current(current)
        self.render_hourly(forecast, tz)
        self.render_daily(forecast, tz)

    def render_current(self, cur):
        f = self.current_frame
        self.clear(f)
        w = cur["weather"][0]
        main = cur["main"]

        ph = self.photo(w["icon"], (72, 72))
        if ph:
            tk.Label(f, image=ph, bg=CARD).grid(row=0, column=0, rowspan=3, padx=(12, 4), pady=10)

        self.label(f, self.place["label"], 12, True).grid(row=0, column=1, sticky="w", pady=(10, 0))
        self.label(f, self.fmt_temp(main["temp"]), 30, True, ACCENT).grid(row=1, column=1, sticky="w")
        self.label(f, w["description"].title(), 11, fg=MUTED).grid(row=2, column=1, sticky="w", pady=(0, 10))

        details = (f'Feels like  {self.fmt_temp(main["feels_like"])}\n'
                   f'Humidity   {main["humidity"]}%\n'
                   f'Wind         {self.fmt_wind(cur["wind"]["speed"])}')
        tk.Label(f, text=details, bg=CARD, fg=TEXT, justify="left",
                 font=("Segoe UI", 10)).grid(row=0, column=2, rowspan=3, padx=(10, 12))

    def render_hourly(self, forecast, tz):
        f = self.hourly_frame
        self.clear(f)
        self.label(f, "Next hours (3-hour steps)", 10, True, MUTED).grid(
            row=0, column=0, columnspan=HOURLY_SLOTS, sticky="w", padx=10, pady=(8, 2))
        for i, item in enumerate(forecast["list"][:HOURLY_SLOTS]):
            t = datetime.fromtimestamp(item["dt"] + tz, timezone.utc).strftime("%I %p").lstrip("0")
            self.label(f, t, 9, True).grid(row=1, column=i, padx=5)
            ph = self.photo(item["weather"][0]["icon"], (40, 40))
            if ph:
                tk.Label(f, image=ph, bg=CARD).grid(row=2, column=i)
            self.label(f, self.fmt_temp(item["main"]["temp"]), 10).grid(row=3, column=i, pady=(0, 10))
            f.columnconfigure(i, weight=1)

    def render_daily(self, forecast, tz):
        f = self.daily_frame
        self.clear(f)
        self.label(f, "Next 5 days", 10, True, MUTED).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(8, 2))

        days = OrderedDict()
        for item in forecast["list"]:
            local = datetime.fromtimestamp(item["dt"] + tz, timezone.utc)
            days.setdefault(local.date(), []).append((local, item))

        for i, (date, entries) in enumerate(list(days.items())[:DAILY_DAYS]):
            temps = [e["main"]["temp"] for _, e in entries]
            _, mid = min(entries, key=lambda x: abs(x[0].hour - 12))
            r = i + 1
            day = "Today" if i == 0 else date.strftime("%a, %d %b")
            self.label(f, day, 10, True).grid(row=r, column=0, sticky="w", padx=(12, 6))
            ph = self.photo(mid["weather"][0]["icon"], (40, 40))
            if ph:
                tk.Label(f, image=ph, bg=CARD).grid(row=r, column=1)
            self.label(f, mid["weather"][0]["main"], 9, fg=MUTED).grid(row=r, column=2, sticky="w", padx=6)
            self.label(f, f"{self.fmt_temp(max(temps))} / {self.fmt_temp(min(temps))}", 10).grid(
                row=r, column=3, sticky="e", padx=(6, 12))
        f.columnconfigure(2, weight=1)
        tk.Label(f, bg=CARD).grid(row=DAILY_DAYS + 1, column=0)


if __name__ == "__main__":
    if API_KEY == "YOUR_API_KEY_HERE":
        print("Set your API key: edit API_KEY in the file or set OPENWEATHER_API_KEY.")
    WeatherApp().mainloop()