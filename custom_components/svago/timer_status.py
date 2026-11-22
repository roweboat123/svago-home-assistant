"""Read-only presentation of a Continuous choice and its renewal session."""

CONTINUOUS_STATES = [
    "Not selected",
    "Waiting for start",
    "Starting",
    "Running",
    "Waiting for massage",
    "Stopped",
]


def continuous_status(diagnostics: dict, *, ready: bool) -> tuple[str, str]:
    """Describe HA renewal, never imply that the heater or massage is off."""
    if not diagnostics["selected"]:
        return "Not selected", "Continuous is not selected."

    outcome = diagnostics.get("outcome", "idle")
    if outcome in {"start_failed", "renewal_failed"}:
        reason = diagnostics.get("error") or (
            "Continuous could not start."
            if outcome == "start_failed"
            else "The timer could not be renewed."
        )
        return "Stopped", f"{reason} Start the function again to resume Continuous."

    if diagnostics["active"]:
        if not ready:
            return "Stopped", "Chair status is unavailable; renewal cannot continue."
        if outcome == "deferred_massage":
            return (
                "Waiting for massage",
                "Renewal is waiting for finite or unmanaged massage to end.",
            )
        return "Running", "Home Assistant is managing timer renewal."

    if outcome == "starting" and ready:
        return "Starting", "Preparing Continuous renewal."

    if not ready:
        return (
            "Waiting for start",
            "Connect to the chair, then start the function to resume Continuous.",
        )
    if outcome in {"connection_changed", "shutdown", "preferences_restored"}:
        return (
            "Waiting for start",
            "The choice was retained; start the function to resume Continuous.",
        )
    if outcome in {"turned_off", "chair_stopped"}:
        return "Waiting for start", "The function was turned off; Continuous remains selected."
    return "Waiting for start", "Start the function to begin Continuous renewal."
