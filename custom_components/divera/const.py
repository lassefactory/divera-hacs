DOMAIN = "divera"

BASE_URL = "https://app.divera247.com/api/v2/pull/all"
JWT_URL = "https://app.divera247.com/api/v2/auth/jwt"
WS_URL = "wss://ws.divera247.com/ws"

CONF_ACCESS_KEY = "access_key"
CONF_UCR_ID = "ucr_id"
CONF_UCR_NAME = "ucr_name"

# Fallback-Polling-Intervall (Sekunden) falls WS-Verbindung unterbrochen ist
FALLBACK_POLL_INTERVAL = 300
# Wartezeit (Sekunden) vor erstem Verbindungsversuch nach Fehler
WS_RECONNECT_DELAY = 10
# Maximale Wartezeit (Sekunden) beim exponentiellen Backoff
WS_MAX_RECONNECT_DELAY = 300
