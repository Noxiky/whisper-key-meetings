from __future__ import annotations


class AudioRouteUnavailable(RuntimeError):
    """Raised when an explicit audio route is not currently exposed by Windows."""


def resolve_input_device(sounddevice_module, configured="default", preferred_name: str | None = None):
    """Resolve a PortAudio input while preserving explicit-route identity across reindexing.

    Windows/PortAudio numeric IDs can change after a USB or Bluetooth topology change. Once an
    exact name has been persisted, it is authoritative: the resolver waits for that microphone
    instead of accidentally opening whichever device inherited the old number.
    """

    if configured in {None, "", "default"}:
        return None, None

    normalized_name = str(preferred_name or "").strip()
    if normalized_name:
        expected = normalized_name.casefold()
        for index, candidate in enumerate(sounddevice_module.query_devices()):
            if not int(candidate.get("max_input_channels", 0) or 0):
                continue
            if str(candidate.get("name", "")).strip().casefold() == expected:
                return int(candidate.get("index", index)), normalized_name
        raise AudioRouteUnavailable(f"El micrófono configurado '{normalized_name}' no está conectado")

    try:
        configured = int(configured)
    except (TypeError, ValueError):
        pass
    try:
        info = sounddevice_module.query_devices(configured, kind="input")
    except Exception as exc:
        raise AudioRouteUnavailable(f"No se pudo abrir el micrófono configurado: {exc}") from exc
    if not int(info.get("max_input_channels", 0) or 0):
        raise AudioRouteUnavailable("El dispositivo configurado no ofrece una entrada de micrófono")
    detected_name = str(info.get("name", "")).strip() or None
    return configured, detected_name
