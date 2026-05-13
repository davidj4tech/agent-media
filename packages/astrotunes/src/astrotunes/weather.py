from __future__ import annotations

import httpx

MELBOURNE = (-37.8136, 144.9631)

WEATHER_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow",
    80: "light showers", 81: "showers", 82: "violent showers",
    95: "thunderstorm", 96: "thunderstorm w/ hail", 99: "severe thunderstorm",
}


def fetch_melbourne_weather(timeout: float = 5.0) -> dict:
    lat, lon = MELBOURNE
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,weather_code,wind_speed_10m,is_day",
        "timezone": "Australia/Melbourne",
    }
    r = httpx.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=timeout)
    r.raise_for_status()
    cur = r.json().get("current", {})
    return {
        "temperature_c": cur.get("temperature_2m"),
        "wind_kmh": cur.get("wind_speed_10m"),
        "is_day": bool(cur.get("is_day", 1)),
        "code": cur.get("weather_code"),
        "label": WEATHER_CODES.get(cur.get("weather_code"), "unknown"),
    }
