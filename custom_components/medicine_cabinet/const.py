"""Constants for Home Medicine Cabinet."""

DOMAIN = "medicine_cabinet"
NAME = "Домашняя аптечка"
VERSION = "0.4.4"

PANEL_URL_PATH = "medicine-cabinet"
PANEL_TITLE = "Домашняя аптечка"
PANEL_ICON = "mdi:pill-multiple"
PANEL_WEB_COMPONENT = "medicine-cabinet-panel-v044"
PANEL_STATIC_URL = "/medicine_cabinet_frontend"

STORAGE_VERSION = 1
STORAGE_KEY = "medicine_cabinet.data"
SIGNAL_UPDATE = "medicine_cabinet_update"
EVENT_ALERT = "medicine_cabinet_alert"

CATALOG_DIRECTORY = "medicine_cabinet"
CATALOG_FILENAME = "medicine_catalog.sqlite"

DEFAULT_SETTINGS = {
    "expiry_warning_days": 30,
    "notifications_enabled": True,
    "notification_hour": 9,
    "price_city": "Москва",
    "storage_locations": [
        "Основная аптечка",
        "Кухня",
        "Комод",
        "Шкаф Таня",
    ],
}
