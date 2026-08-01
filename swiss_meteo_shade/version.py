"""Single source of truth for the add-on version.

Deliberately dependency-free so every other module can import it without any
risk of a circular import.

Two copies of the version live OUTSIDE Python and must be bumped by hand
alongside VERSION here:

    config.yaml            version: "..."
    Dockerfile             LABEL io.hass.version="..."

Both are build manifests the Supervisor reads before any Python runs, so they
cannot import this. Everything else -- the MQTT `sw_version` shown in Home
Assistant's device info, and the User-Agent sent to MeteoSwiss/Open-Meteo --
derives from here, so a bump reaches them automatically.
"""

VERSION = "1.2.2"

# Sent on every outbound request so the data providers can identify the client.
USER_AGENT = f"swiss-meteo-shade/{VERSION} (Home Assistant add-on)"
