import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./parking.db")
    mqtt_host: str = os.getenv("MQTT_HOST", "localhost")
    mqtt_port: int = int(os.getenv("MQTT_PORT", "1883"))
    mqtt_prefix: str = os.getenv("MQTT_PREFIX", "parking")
    num_slots: int = int(os.getenv("NUM_SLOTS", "4"))
    # Giai đoạn 4: luồng cổng
    capture_url: str = os.getenv("CAPTURE_URL", "http://host.docker.internal:8002/capture")
    alpr_url: str = os.getenv("ALPR_URL", "http://alpr:8001/recognize")
    gate_open_seconds: float = float(os.getenv("GATE_OPEN_SECONDS", "5"))
    gate_cooldown_seconds: float = float(os.getenv("GATE_COOLDOWN_SECONDS", "5"))
    http_timeout: float = float(os.getenv("HTTP_TIMEOUT", "10"))
    image_dir: str = os.getenv("IMAGE_DIR", "data/images")


settings = Settings()
