"""Receipt marker semantics shared by writers and business conditions."""

FIELDS = ("photo", "paper", "delivered", "supplement")


def active(marker):
    return marker is True or (isinstance(marker, dict) and bool(marker) and not marker.get("off"))


def transition(marker, desired, name, at):
    previous = marker if isinstance(marker, dict) else {}
    if desired:
        result = {"by": name, "at": at}
        history = previous.get("offHistory")
        history = list(history) if isinstance(history, list) else []
        if previous.get("off"):
            history.append({"by": previous.get("offBy"), "at": previous.get("offAt")})
        if history:
            result["offHistory"] = history
        return result
    return {**previous, "off": True, "offBy": name, "offAt": at}
