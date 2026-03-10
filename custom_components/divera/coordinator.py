"""DIVERA 24/7 – WebSocket-getriebener DataUpdateCoordinator."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BASE_URL,
    CONF_ACCESS_KEY,
    CONF_UCR_ID,
    DOMAIN,
    FALLBACK_POLL_INTERVAL,
    JWT_URL,
    WS_RECONNECT_DELAY,
    WS_URL,
)

_LOGGER = logging.getLogger(__name__)


class DiveraCoordinator(DataUpdateCoordinator):
    """Koordiniert DIVERA-Daten per WebSocket (Push-to-Pull).

    Ablauf:
      1. Beim Setup: JWT holen, initiale Daten per REST laden.
      2. WebSocket-Schleife starten (läuft als HA-Background-Task).
      3. Bei 'cluster-pull'-Event: REST-Daten neu laden und Sensoren aktualisieren.
      4. Bei 'jwtExpired': neuen JWT holen und neu authentifizieren.
      5. Fallback: falls WS-Verbindung weg, greift der reguläre Poll-Intervall.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.access_key: str = entry.data[CONF_ACCESS_KEY]
        self.ucr_id: str | None = entry.data.get(CONF_UCR_ID)
        self._jwt: str | None = None
        self._ws_task: asyncio.Task | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # Fallback-Polling falls WS ausfällt
            update_interval=timedelta(seconds=FALLBACK_POLL_INTERVAL),
        )

    # ------------------------------------------------------------------
    # JWT
    # ------------------------------------------------------------------

    async def async_fetch_jwt(self) -> str:
        """JWT vom DIVERA-Server holen (ohne Bearer, nur mit Accesskey)."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    JWT_URL,
                    params={"accesskey": self.access_key},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 401:
                        raise ConfigEntryAuthFailed("Ungültiger Access Key")
                    if resp.status != 200:
                        raise UpdateFailed(f"JWT-Abruf fehlgeschlagen: HTTP {resp.status}")
                    payload = await resp.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Verbindungsfehler beim JWT-Abruf: {err}") from err

        data = payload.get("data", {})
        # Dokumentation zeigt "jwt", JS-Beispiel nutzt "jwt_ws" – beide abfangen
        jwt = data.get("jwt_ws") or data.get("jwt")
        if not jwt:
            raise UpdateFailed("Kein JWT in der Server-Antwort gefunden")
        self._jwt = jwt
        return jwt

    # ------------------------------------------------------------------
    # REST-Datenabruf (wird auch als Fallback-Poll genutzt)
    # ------------------------------------------------------------------

    async def _async_update_data(self):
        """Aktuelle Alarmdaten per REST laden."""
        params = {"accesskey": self.access_key}
        if self.ucr_id:
            params["ucr"] = self.ucr_id

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    BASE_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 401:
                        raise ConfigEntryAuthFailed("Ungültiger Access Key")
                    if resp.status != 200:
                        raise UpdateFailed(f"API-Fehler: HTTP {resp.status}")
                    payload = await resp.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Verbindungsfehler: {err}") from err

        return self._extract_alarm(payload)

    def _extract_alarm(self, payload: dict):
        """Neuesten aktiven Alarm aus der API-Antwort extrahieren."""
        items = payload.get("data", {}).get("alarm", {}).get("items", [])

        # items kann [] (kein Alarm) oder {id: alarm_obj} (Dict) sein
        if not items or not isinstance(items, dict):
            return None

        alarms = list(items.values())
        _LOGGER.debug("DIVERA: %d Alarm(e) gefunden", len(alarms))

        return max(alarms, key=lambda a: a.get("id", 0))

    # ------------------------------------------------------------------
    # WebSocket-Schleife
    # ------------------------------------------------------------------

    async def async_start_websocket(self) -> None:
        """WebSocket-Schleife als dauerhaften Background-Task starten."""
        self._ws_task = self.hass.async_create_background_task(
            self._ws_loop(),
            name="divera_websocket",
        )

    def async_stop_websocket(self) -> None:
        """WebSocket-Task beenden (beim Unload der Integration)."""
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            self._ws_task = None

    async def _ws_loop(self) -> None:
        """Endlosschleife: WebSocket verbinden, Events verarbeiten, bei Fehler neu verbinden."""
        while True:
            try:
                await self._ws_run_once()
            except asyncio.CancelledError:
                _LOGGER.debug("DIVERA WebSocket-Task wurde beendet")
                return
            except ConfigEntryAuthFailed:
                _LOGGER.error("DIVERA: Ungültiger Access Key – WebSocket wird nicht neu verbunden")
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "DIVERA WebSocket-Verbindung unterbrochen (%s). "
                    "Neuer Versuch in %s Sekunden.",
                    err,
                    WS_RECONNECT_DELAY,
                )
            await asyncio.sleep(WS_RECONNECT_DELAY)

    async def _ws_run_once(self) -> None:
        """Einmalige WebSocket-Sitzung: verbinden, auth, Events lesen."""
        jwt = await self.async_fetch_jwt()
        _LOGGER.debug("DIVERA: JWT erfolgreich geholt, verbinde WebSocket …")

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                WS_URL,
                heartbeat=25,  # Ping alle 25s gegen den 30s-Timeout
                timeout=aiohttp.ClientTimeout(total=None, connect=15),
            ) as ws:
                # Authentifizierung sofort nach Verbindungsaufbau senden
                auth_payload: dict = {"jwt": jwt}
                if self.ucr_id:
                    auth_payload["ucr"] = int(self.ucr_id)

                await ws.send_json({"type": "authenticate", "payload": auth_payload})
                _LOGGER.debug("DIVERA: authenticate gesendet")

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_ws_message(msg.data, ws)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise UpdateFailed(f"WebSocket-Fehler: {ws.exception()}")
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        _LOGGER.debug("DIVERA: WebSocket-Verbindung geschlossen")
                        return

    async def _handle_ws_message(self, raw: str, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Eingehende WebSocket-Nachricht verarbeiten."""
        try:
            data = __import__("json").loads(raw)
        except ValueError:
            _LOGGER.debug("DIVERA: Nicht-JSON-Nachricht empfangen: %s", raw)
            return

        msg_type = data.get("type", "")

        if msg_type == "init":
            _LOGGER.info("DIVERA: WebSocket erfolgreich authentifiziert (init empfangen)")

        elif msg_type == "jwtExpired":
            _LOGGER.info("DIVERA: JWT abgelaufen – hole neuen JWT und authentifiziere neu")
            try:
                new_jwt = await self.async_fetch_jwt()
                auth_payload: dict = {"jwt": new_jwt}
                if self.ucr_id:
                    auth_payload["ucr"] = int(self.ucr_id)
                await ws.send_json({"type": "authenticate", "payload": auth_payload})
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("DIVERA: JWT-Erneuerung fehlgeschlagen: %s", err)
                raise

        elif msg_type == "cluster-pull":
            _LOGGER.debug("DIVERA: cluster-pull empfangen – lade neue Alarmdaten")
            await self.async_refresh()

        elif msg_type == "cluster-vehicle":
            _LOGGER.debug("DIVERA: Fahrzeugstatus-Update: %s", data.get("payload"))

        elif msg_type == "user-status":
            _LOGGER.debug("DIVERA: Nutzerstatus-Update: %s", data.get("payload"))

        else:
            _LOGGER.debug("DIVERA: Unbekanntes WS-Event '%s': %s", msg_type, data)
