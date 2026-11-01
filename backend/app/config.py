import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./parking.db")
    mqtt_host: str = os.getenv("MQTT_HOST", "localhost")
    mqtt_port: int = int(os.getenv("MQTT_PORT", "1883"))
    mqtt_prefix: str = os.getenv("MQTT_PREFIX", "parking")
    num_slots: int = int(os.getenv("NUM_SLOTS", "4"))


settings = Settings()
