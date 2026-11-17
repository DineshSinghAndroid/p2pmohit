import re
from decimal import Decimal, InvalidOperation


_AMOUNT_RE = re.compile(r"(?:₹\s*([0-9][0-9\s\u00a0\u202f.,]*)(?:\s*INR)?|([0-9][0-9\s\u00a0\u202f.,]*?)\s*(?:INR|₹))", re.IGNORECASE)


def parse_amount_inr(value: str) -> Decimal | None:
    match = _AMOUNT_RE.search(value or "")
    if not match:
        return None
    amount = re.sub(r"[\s\u00a0\u202f]", "", match.group(1) or match.group(2))
    if "," in amount and "." in amount:
        if amount.rfind(",") > amount.rfind("."):
            amount = amount.replace(".", "").replace(",", ".")
        else:
            amount = amount.replace(",", "")
    elif "," in amount:
        amount = amount.replace(",", ".")
    try:
        return Decimal(amount)
    except InvalidOperation:
        return None


def classify_outcome(modal_text: str = "", *, otp_visible: bool = False, error_text: str = "") -> str:
    text = (modal_text or "").casefold()
    if error_text:
        return "FAIL"
    if otp_visible:
        return "OTP_REACHED"
    if "accepted by another trader" in text:
        return "TAKEN_BY_OTHER"
    if "accept the request for processing" in text:
        return "SUCCESS_POPUP"
    return "FAIL"


def calculate_stats(clicks: list[dict]) -> dict:
    outcomes = [click.get("outcome", "") for click in clicks]
    latencies = sorted(int(click.get("latency_ms") or 0) for click in clicks)
    total = len(clicks)
    valid = sum(outcome in {"OTP_REACHED", "TAKEN_BY_OTHER", "ACCEPTED", "SUCCESS_POPUP", "LIMIT_REACHED", "SKIPPED", "RATE_LIMITED"} for outcome in outcomes)
    p95_index = max(0, int((len(latencies) * 0.95) + 0.999999) - 1)
    reasons: dict[str, int] = {}
    for click in clicks:
        if click.get("outcome") == "FAIL":
            reason = click.get("error_text") or "Unspecified failure"
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "total_clicks": total,
        "popups_shown": sum(bool(click.get("popup_shown")) for click in clicks),
        "taken_by_other": outcomes.count("TAKEN_BY_OTHER"),
        "otp_reached": outcomes.count("OTP_REACHED"),
        "accepted": outcomes.count("ACCEPTED"),
        "limit_reached": outcomes.count("LIMIT_REACHED"),
        "rate_limited": outcomes.count("RATE_LIMITED"),
        "skipped": outcomes.count("SKIPPED") + outcomes.count("TAKEN_BY_OTHER"),
        "failures": outcomes.count("FAIL"),
        "success_rate": round(valid * 100 / total, 2) if total else 0,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0,
        "p95_latency_ms": latencies[p95_index] if latencies else 0,
        "failure_reasons": reasons,
    }
