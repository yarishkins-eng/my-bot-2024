def device_app_name(device: dict) -> str | None:
    words = (device.get('userAgent') or '').strip().split('/', 1)[0].split()
    return words[0] if words else None
