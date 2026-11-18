import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, quote_plus, urlparse

from .logic import classify_outcome, parse_amount_inr


LOGIN_TIMEOUT_MS = 30_000
MODAL_TIMEOUT_MS = 8_000
MONITOR_INTERVAL_S = 30
MONITOR_MAX_BACKOFF_S = 60
PAYOUT_READ_PATHS = ("/api/trade-api/payouts/new", "/api/trade-api/payouts/counts")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_proxy(proxy_str: str | None) -> dict | None:
    if not proxy_str or not str(proxy_str).strip():
        return None
    p = str(proxy_str).strip()
    parts = p.split(":")
    if len(parts) == 4 and not p.startswith(("http://", "https://", "socks5://", "socks4://")):
        ip, port, user, pwd = parts
        return {"server": f"http://{ip}:{port}", "username": user, "password": pwd}
    if not p.startswith(("http://", "https://", "socks5://", "socks4://")):
        p = "http://" + p
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(p)
        if not parsed.hostname:
            return None
        port = f":{parsed.port}" if parsed.port else ""
        proxy_cfg = {"server": f"{parsed.scheme}://{parsed.hostname}{port}"}
        if parsed.username:
            proxy_cfg["username"] = parsed.username
        if parsed.password:
            proxy_cfg["password"] = parsed.password
        return proxy_cfg
    except Exception:
        return None


def _redact(text: str, config: dict) -> str:
    safe = str(text or "")
    secrets = [config.get("email"), config.get("password")]
    proxy_val = config.get("proxy")
    if proxy_val:
        p_info = _parse_proxy(proxy_val)
        if p_info and p_info.get("password"):
            secrets.append(p_info["password"])
    for secret in secrets:
        if secret:
            variants = sorted({secret, quote(secret), quote_plus(secret)}, key=len, reverse=True)
            for variant in variants:
                safe = safe.replace(variant, "[redacted]")
    return safe[:500]


def _failure_screenshot(page, path: str, config: dict):
    page.evaluate("""({email, password}) => {
      window.__payoutTesterRedactions = {nodes: [], inputs: []};
      const secrets = [email, password].filter(Boolean);
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const original = node.nodeValue;
        let masked = original;
        for (const secret of secrets) masked = masked.split(secret).join('[redacted]');
        if (masked !== original) {
          window.__payoutTesterRedactions.nodes.push([node, original]);
          node.nodeValue = masked;
        }
      }
      for (const input of document.querySelectorAll('input, textarea')) {
        if (input.type === 'password' || secrets.includes(input.value)) {
          window.__payoutTesterRedactions.inputs.push([input, input.value]);
          input.value = '';
        }
      }
    }""", {"email": config.get("email", ""), "password": config.get("password", "")})
    try:
        page.screenshot(path=path, full_page=True)
    finally:
        page.evaluate("""() => {
          const saved = window.__payoutTesterRedactions;
          if (!saved) return;
          for (const [node, value] of saved.nodes) node.nodeValue = value;
          for (const [input, value] of saved.inputs) input.value = value;
          delete window.__payoutTesterRedactions;
        }""")


def _locator_text(locator) -> str:
    try:
        return locator.inner_text(timeout=1_000).strip()
    except Exception:
        return ""


def _dismiss_blocking_dialogs(page):
    """Close leftover dialogs (e.g. auto-reopened 'Processing of the request') that cover the table.
    Only closes via the X / Escape. Never presses 'I have transferred' or any action button."""
    for _ in range(4):
        try:
            dlg = page.locator('section[role="dialog"]:visible')
            if not dlg.count():
                return
            x = dlg.first.locator('[automation-id="tui-dialog__close"], .tui-dialog__close, button[class*="dialog__close"]')
            if x.count():
                x.first.click(timeout=2_000)
            else:
                page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass


def _close_chat_toast(page):
    try:
        toast = page.get_by_text("Chat without Telegram", exact=False).first
        if toast.count():
            for button in page.get_by_role("button").all():
                label = " ".join(filter(None, [button.get_attribute("aria-label"), button.get_attribute("title"), _locator_text(button)]))
                if re.search(r"close|dismiss|×|✕", label, re.IGNORECASE):
                    button.click(timeout=1_000)
                    break
    except Exception:
        pass


def _read_rows(page) -> list[dict]:
    rows = []
    for row in page.locator("tr").all():
        try:
            button = row.get_by_role("button", name=re.compile(r"Accept for processing", re.IGNORECASE))
            if not button.count() or not button.is_visible():
                continue
            cells = row.locator("th,td").all()
            values = [_locator_text(cell) for cell in cells]
            if not values:
                continue
            amount = next((parse_amount_inr(value) for value in values if parse_amount_inr(value) is not None), None)
            if amount is None:
                continue
            rows.append({"row_id": values[0], "amount": amount})
        except Exception:
            continue
    return rows


def _find_row(page, row_id: str):
    for row in page.locator("tr").all():
        try:
            cells = row.locator("th,td")
            if cells.count() and _locator_text(cells.nth(0)) == row_id:
                button = row.get_by_role("button", name=re.compile(r"Accept for processing", re.IGNORECASE))
                if button.count() and button.is_visible():
                    return button
        except Exception:
            continue
    return None


def _visible_dialog_text(page) -> str:
    try:
        dialogs = page.get_by_role("dialog").all()
        for dialog in dialogs:
            if dialog.is_visible():
                return _locator_text(dialog)
    except Exception:
        pass
    return ""


def _wait_for_modal(page, timeout_ms: int) -> str:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        text = _visible_dialog_text(page)
        full = text or _locator_text(page.locator("body"))
        if "accept the request for processing" in full.casefold() or "accepted by another trader" in full.casefold() or text:
            return full
        time.sleep(0.1)
    return ""


def _has_otp_step(page) -> bool:
    dialog = page.get_by_role("dialog")
    text = _locator_text(dialog.first) if dialog.count() else _locator_text(page.locator("body"))
    return bool(re.search(r"\b(otp|one.?time password|verification code)\b", text, re.IGNORECASE))


def _click_dialog_button(page, name: str) -> bool:
    ids = {"accept": "autotest_modal_button_accept", "close": "autotest_modal_button_close"}
    try:
        scope = page.locator('section[role="dialog"]:visible')
        if scope.count():
            scope = scope.last
            sel = ids.get(name.casefold())
            if sel:
                btn = scope.locator(f'[automation-id="{sel}"]')
                if btn.count() and btn.first.is_enabled():
                    btn.first.click(timeout=3_000)
                    return True
            button = scope.get_by_role("button", name=re.compile(rf"^{re.escape(name)}$", re.IGNORECASE))
            if button.count() and button.first.is_enabled():
                button.first.click(timeout=3_000)
                return True
    except Exception:
        pass
    return False


def _close_dialog(page):
    for label in ("Close", "×", "✕"):
        if _click_dialog_button(page, label):
            return
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _error_toast(page) -> str:
    for selector in ('[role="alert"]', '.toast', '.snackbar', '.notification', '[class*="toast"]'):
        try:
            for node in page.locator(selector).all():
                if node.is_visible():
                    text = _locator_text(node)
                    if text and re.search(r"error|failed|unable|try again", text, re.IGNORECASE):
                        return text[:400]
        except Exception:
            continue
    return ""


def _watch_counts(page, state):
    """Listen to the site's own periodic /payouts/counts call (~every 4s). No extra requests from us."""
    def on_resp(resp):
        try:
            if resp.request.method == "GET" and "/payouts/counts" in resp.url and resp.status == 200:
                state["counts"] = resp.text()
                state["stamp"] = time.monotonic()
        except Exception:
            pass
    page.on("response", on_resp)


def _refresh_list(page):
    try:
        refreshed = page.evaluate("""() => {
            const btns = [...document.querySelectorAll('button, a[role="button"]')];
            const ref = btns.find(b => /refresh[\\s]*list/i.test(b.innerText || b.textContent || '') || b.getAttribute('automation-id') === 'autotest_button_refresh');
            if (ref && !ref.disabled && ref.offsetParent !== null) {
                ref.click();
                return true;
            }
            return false;
        }""")
        if refreshed:
            return True
        page.reload(wait_until="domcontentloaded", timeout=10_000)
        return True
    except Exception:
        pass
    return False



def _next_page(page) -> bool:
    selectors = (
        '[aria-label*="next" i]', '[title*="next" i]',
        'button:has-text("›")', 'button:has-text("→")',
        'button:has-text("navigate_next")',
    )
    for selector in selectors:
        try:
            for candidate in page.locator(selector).all():
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click(timeout=3_000)
                    page.wait_for_timeout(500)
                    return True
        except Exception:
            continue
    try:
        button = page.get_by_role("button", name=re.compile(r"next", re.IGNORECASE))
        for candidate in button.all():
            if candidate.is_visible() and candidate.is_enabled():
                candidate.click(timeout=3_000)
                page.wait_for_timeout(500)
                return True
    except Exception:
        pass
    return False


CLICK_COOLDOWN_S = 2.0  # min gap between two Accept-for-processing clicks (avoids HTTP 429)
ACCEPT_SEL = '[automation-id="autotest_button_tradeAccept"]'
CONFIRM_SEL = '[automation-id="autotest_modal_button_accept"]'
MODAL_CLOSE_SEL = '[automation-id="autotest_modal_button_close"]'
X_SEL = '[automation-id="tui-dialog__close"], .tui-dialog__close, button[class*="dialog__close"]'


def _close_any_dialog(page) -> None:
    """Close whatever dialog is open using the top-right X only (never an action button)."""
    for _ in range(5):
        try:
            dlg = page.locator('section[role="dialog"]:visible')
            if not dlg.count():
                return
            x = dlg.last.locator(X_SEL)
            if x.count():
                x.first.click(timeout=2_000)
            else:
                m = dlg.last.locator(MODAL_CLOSE_SEL)
                if m.count():
                    m.first.click(timeout=2_000)  # plain "Close" of the Accept/other-trader dialog
                else:
                    page.keyboard.press("Escape")
            page.wait_for_timeout(350)
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass


def _visible_rows(page):
    """Rows on the current page that have an active Accept button, as (index, amount)."""
    out = []
    try:
        data = page.evaluate("""() => {
            function norm(s) {
                return (s || '').replace(/[\\s\\u00a0\\u202f\\n\\r\\t]+/g, ' ').trim().toLowerCase();
            }

            function isEligibleAcceptButton(b) {
                if (!b || b.disabled) return false;
                if (b.getAttribute('aria-disabled') === 'true') return false;
                if (b.classList && (b.classList.contains('tui-button_disabled') || b.classList.contains('_disabled'))) return false;
                if (b.offsetParent === null) return false;

                const t = norm(b.innerText || b.textContent || '');
                if (t.includes('another trader') || t.includes('accepted') || t.includes('too late') || t.includes('expired')) {
                    return false;
                }
                const autoId = b.getAttribute('automation-id') || '';
                if (autoId === 'autotest_button_tradeAccept') {
                    return t === '' || t.includes('accept');
                }
                return t === 'accept for processing' || t === 'accept' || t.startsWith('accept');
            }

            function parseAmount(el) {
                const cleanNorm = (el.innerText || el.textContent || '').replace(/[\\s\\u00a0\\u202f\\n\\r\\t]+/g, ' ').trim();
                const m = cleanNorm.match(/([\\d][\\d\\s\\u00a0\\u202f.,]*)\\s*INR/i) || cleanNorm.match(/₹\\s*([\\d][\\d\\s\\u00a0\\u202f.,]*)/i);
                if (!m) return null;
                let s = m[1].replace(/[\\s\\u00a0\\u202f]/g, '');
                if (s.includes(',') && s.includes('.')) {
                    if (s.lastIndexOf(',') > s.lastIndexOf('.')) s = s.replace(/\\./g, '').replace(',', '.');
                    else s = s.replace(/,/g, '');
                } else if (s.includes(',')) {
                    s = s.replace(',', '.');
                }
                const val = parseFloat(s);
                return isNaN(val) ? null : val;
            }

            const out = [];
            const rows = document.querySelectorAll('tr, [role="row"]');
            rows.forEach((tr, idx) => {
                const trText = norm(tr.innerText || tr.textContent || '');
                if (trText.includes('another trader') || trText.includes('too late') || trText.includes('expired')) return;

                const btns = tr.querySelectorAll('button, a[role="button"], [automation-id="autotest_button_tradeAccept"]');
                let found = null;
                for (const b of btns) {
                    if (isEligibleAcceptButton(b)) { found = b; break; }
                }
                if (!found) return;

                let val = null;
                const cells = tr.querySelectorAll('td, [role="cell"]');
                for (const cell of cells) {
                    val = parseAmount(cell);
                    if (val !== null) break;
                }
                if (val === null) val = parseAmount(tr);
                if (val !== null) {
                    out.push({ i: idx, amt_str: String(val) });
                }
            });
            return out;
        }""")
    except Exception:
        return out
    for d in data:
        amt = parse_amount_inr(d.get("amt_str", "") + " INR")
        if amt is not None:
            out.append((d["i"], amt))
    return out


def _ensure_logged_in(page, config) -> None:
    """If the session expired (redirected to /login), sign in again and return to /payouts."""
    try:
        if "/login" not in page.url:
            return
        parsed = urlparse(config["site_url"])
        root = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        email = page.get_by_role("textbox", name=re.compile("email", re.IGNORECASE))
        email.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
        email.fill(config["email"])
        page.locator('input[type="password"]').first.fill(config["password"])
        page.get_by_role("button", name=re.compile(r"^Sign in$", re.IGNORECASE)).click(timeout=10_000)
        page.wait_for_url(re.compile(r"/dashboard(?:$|[/?#])"), timeout=LOGIN_TIMEOUT_MS)
        page.goto(f"{root}/payouts", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1500)
    except Exception:
        pass


def _continuous_loop(page, config, stop_event, db, run_id, screenshot_dir, accept_resp, js_errors, server_errors, min_amount, max_amount):
    """Keep accepting orders inside [min,max] until the user presses STOP."""
    seq = 0
    last_click = [0.0]
    last_refresh = [time.monotonic()]
    consecutive_429 = [0]
    min_amt_f = float(min_amount) if min_amount is not None else None
    max_amt_f = float(max_amount) if max_amount is not None else None

    def record(amount, popup_shown, outcome, latency_ms, error_text=None, row_id=""):
        db.add_click({"run_id": run_id, "row_id": row_id, "amount": str(amount), "timestamp": utc_now(),
                      "popup_shown": int(popup_shown), "outcome": outcome, "latency_ms": latency_ms,
                      "error_text": error_text or None})

    while not stop_event.is_set():
        try:
            _ensure_logged_in(page, config)
            _close_any_dialog(page)
            _close_chat_toast(page)

            cooldown = max(0.01, (config.get("delay_ms") or 50) / 1000.0)
            gap = cooldown - (time.monotonic() - last_click[0])
            if gap > 0 and stop_event.wait(gap):
                break

            started = time.perf_counter()
            popup_shown = False
            amount = Decimal(0)
            row_id = ""
            try:
                accept_resp.clear()

                # ATOMIC SCAN, EXACT ROW AMOUNT VERIFICATION & 5ms CONFIRM CLICK
                turbo = page.evaluate("""([minAmt, maxAmt]) => {
                    function norm(s) {
                        return (s || '').replace(/[\\s\\u00a0\\u202f\\n\\r\\t]+/g, ' ').trim().toLowerCase();
                    }

                    function isEligibleAcceptButton(b) {
                        if (!b || b.disabled) return false;
                        if (b.getAttribute('aria-disabled') === 'true') return false;
                        if (b.classList && (b.classList.contains('tui-button_disabled') || b.classList.contains('_disabled'))) return false;
                        if (b.offsetParent === null) return false;

                        const t = norm(b.innerText || b.textContent || '');
                        if (t.includes('another trader') || t.includes('accepted') || t.includes('too late') || t.includes('expired')) {
                            return false;
                        }
                        const autoId = b.getAttribute('automation-id') || '';
                        if (autoId === 'autotest_button_tradeAccept') {
                            return t === '' || t.includes('accept');
                        }
                        return t === 'accept for processing' || t === 'accept' || t.startsWith('accept');
                    }

                    function parseAmountFromElement(el) {
                        if (!el) return null;
                        const cleanNorm = (el.innerText || el.textContent || '').replace(/[\\s\\u00a0\\u202f\\n\\r\\t]+/g, ' ').trim();
                        const m = cleanNorm.match(/([\\d][\\d\\s\\u00a0\\u202f.,]*)\\s*INR/i) || cleanNorm.match(/₹\\s*([\\d][\\d\\s\\u00a0\\u202f.,]*)/i);
                        if (!m) return null;
                        let s = m[1].replace(/[\\s\\u00a0\\u202f]/g, '');
                        if (s.includes(',') && s.includes('.')) {
                            if (s.lastIndexOf(',') > s.lastIndexOf('.')) s = s.replace(/\\./g, '').replace(',', '.');
                            else s = s.replace(/,/g, '');
                        } else if (s.includes(',')) {
                            s = s.replace(',', '.');
                        }
                        const val = parseFloat(s);
                        return isNaN(val) ? null : val;
                    }

                    function parseRowAmount(tr) {
                        const cells = tr.querySelectorAll('td, [role="cell"]');
                        for (const cell of cells) {
                            const val = parseAmountFromElement(cell);
                            if (val !== null) return val;
                        }
                        return parseAmountFromElement(tr);
                    }

                    function extractRowId(tr) {
                        const cells = tr.querySelectorAll('td, th, [role="cell"]');
                        for (const cell of cells) {
                            const t = (cell.innerText || cell.textContent || '').replace(/[\\s\\u00a0\\u202f\\n\\r\\t]+/g, ' ').trim();
                            if (t && !t.includes('INR') && !t.includes('IMPS') && !t.includes('UPI') && !t.includes(':') && t.length > 2) {
                                return t;
                            }
                        }
                        if (cells.length > 0) {
                            return (cells[0].innerText || cells[0].textContent || '').replace(/[\\s\\u00a0\\u202f\\n\\r\\t]+/g, ' ').trim();
                        }
                        return '';
                    }

                    // 1. Scan candidate buttons directly (top to bottom)
                    const candidateButtons = [...document.querySelectorAll('button, a[role="button"], [automation-id="autotest_button_tradeAccept"]')];
                    let target = null;

                    for (const btn of candidateButtons) {
                        if (!isEligibleAcceptButton(btn)) continue;

                        const tr = btn.closest('tr') || btn.closest('[role="row"]') || btn.parentElement;
                        if (!tr) continue;

                        const trText = norm(tr.innerText || tr.textContent || '');
                        if (trText.includes('another trader') || trText.includes('too late') || trText.includes('expired')) {
                            continue;
                        }

                        const amt = parseRowAmount(tr);
                        if (amt === null) continue;

                        // Min/Max filter check on the exact row amount
                        if (minAmt !== null && amt < minAmt) continue;
                        if (maxAmt !== null && amt > maxAmt) continue;

                        target = {
                            btn: btn,
                            amount: amt,
                            row_id: extractRowId(tr)
                        };
                        break;
                    }

                    if (!target) {
                        return Promise.resolve({ clicked: false, reason: 'no_matching_row' });
                    }

                    // 2. Click the matching button immediately
                    target.btn.click();
                    const start = performance.now();

                    // 3. High-frequency 5ms loop to confirm modal immediately and verify modal amount
                    return new Promise((resolve) => {
                        const timer = setInterval(() => {
                            const elapsed = performance.now() - start;

                            const confirmBtn = document.querySelector('[automation-id="autotest_modal_button_accept"]') ||
                                               [...document.querySelectorAll('section[role="dialog"] button')].find(b => {
                                                   const t = norm(b.innerText || b.textContent || '');
                                                   return t === 'accept' || t === 'confirm' || b.getAttribute('automation-id') === 'autotest_modal_button_accept';
                                               });
                            if (confirmBtn && !confirmBtn.disabled && confirmBtn.offsetParent !== null) {
                                clearInterval(timer);

                                // If modal dialog contains explicit amount, verify and adopt it
                                const dialog = document.querySelector('section[role="dialog"]:not(.hidden)');
                                let finalAmount = target.amount;
                                if (dialog) {
                                    const modalAmt = parseAmountFromElement(dialog);
                                    if (modalAmt !== null && modalAmt > 0) {
                                        finalAmount = modalAmt;
                                    }
                                }

                                confirmBtn.click();
                                resolve({
                                    clicked: true,
                                    amount: finalAmount,
                                    row_id: target.row_id,
                                    confirmed: true,
                                    popup_shown: true,
                                    elapsed_ms: Math.round(elapsed)
                                });
                                return;
                            }

                            const dialog = document.querySelector('section[role="dialog"]:not(.hidden)');
                            if (dialog) {
                                const dText = norm(dialog.innerText || dialog.textContent || '');
                                if (dText.includes('another trader') || dText.includes('too late') || dText.includes('expired')) {
                                    clearInterval(timer);
                                    resolve({
                                        clicked: true,
                                        amount: target.amount,
                                        row_id: target.row_id,
                                        confirmed: false,
                                        popup_shown: true,
                                        taken_by_other: true,
                                        elapsed_ms: Math.round(elapsed)
                                    });
                                    return;
                                }
                            }

                            if (elapsed > 3500) {
                                clearInterval(timer);
                                resolve({
                                    clicked: true,
                                    amount: target.amount,
                                    row_id: target.row_id,
                                    confirmed: false,
                                    popup_shown: !!dialog,
                                    timeout: true,
                                    elapsed_ms: Math.round(elapsed)
                                });
                            }
                        }, 50);
                    });
                }""", [min_amt_f, max_amt_f])

                if not turbo.get("clicked"):
                    # No matching eligible orders in table right now
                    if stop_event.wait(0.025):
                        break
                    # Click in-page "Refresh list" button every 1.0 second if no matching orders exist
                    if time.monotonic() - last_refresh[0] >= 1.0:
                        last_refresh[0] = time.monotonic()
                        _refresh_list(page)
                    continue

                seq += 1
                last_click[0] = time.monotonic()
                amount = Decimal(str(turbo["amount"]))
                row_id = turbo.get("row_id", "")

                if turbo.get("taken_by_other"):
                    lat = turbo.get("elapsed_ms", round((time.perf_counter() - started) * 1000))
                    record(amount, True, "TAKEN_BY_OTHER", lat, row_id=row_id)
                    _close_any_dialog(page)
                    _refresh_list(page)
                    last_refresh[0] = time.monotonic()
                    continue

                # Confirm was fired within 50ms — do not wait for browser/server confirmation
                lat = turbo.get("elapsed_ms", round((time.perf_counter() - started) * 1000))
                outcome = "ACCEPTED"
                err = None

                # Check if an immediate HTTP error occurred
                if accept_resp:
                    limited = next((item for item in accept_resp if item[0] == 429), None)
                    status, body, retry_after = limited or accept_resp[-1]
                    if status == 429:
                        outcome, err = "RATE_LIMITED", body
                    elif status == 424 and ("ALREADY_PROCESSED" in body or "already processed" in body.lower()):
                        outcome, err = "TAKEN_BY_OTHER", "Accepted by another trader (424)"
                    elif status == 424 and "MAX_SIMULTANEOUS" in body:
                        outcome, err = "LIMIT_REACHED", "Max simultaneous orders reached"

                record(amount, True, outcome, lat, err, row_id=row_id)

                if outcome == "RATE_LIMITED":
                    _close_any_dialog(page)
                    consecutive_429[0] += 1
                    server_wait = _retry_after_seconds(retry_after)
                    wait_s = max(2.0, server_wait)
                    db.update_run(run_id, "running", error_text=f"Rate limited (429 #{consecutive_429[0]}). Waiting {int(wait_s)}s before auto-retry...")
                    last_click[0] = time.monotonic() + wait_s
                    last_refresh[0] = time.monotonic() + wait_s
                    if stop_event.wait(wait_s):
                        break
                    db.update_run(run_id, "running", error_text=None)
                    _refresh_list(page)
                    continue

                consecutive_429[0] = 0
                _close_any_dialog(page)

                # Auto-refresh page immediately after every click to load new orders
                _refresh_list(page)
                last_refresh[0] = time.monotonic()

                # Micro 50ms pause before scanning for the next order
                if stop_event.wait(0.05):
                    break
            except Exception as exc:
                lat = round((time.perf_counter() - started) * 1000)
                record(amount, popup_shown, "FAIL", lat, _redact(str(exc), config).replace("\n", " ")[:600], row_id=row_id)
                try:
                    _failure_screenshot(page, str(Path(screenshot_dir) / f"{run_id}_{seq}.png"), config)
                except Exception:
                    pass
                _close_any_dialog(page)
        except Exception:
            if stop_event.wait(1.0):
                break


def _retry_after_seconds(value):
    if not value:
        return 0
    value = value.strip()
    try:
        if value.isascii() and value.isdecimal():
            return int(value)
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            return 0
        return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (OverflowError, TypeError, ValueError):
        return 0


def _wait_for_monitor(stop_event, seconds):
    while seconds > 60:
        if stop_event.wait(60):
            return True
        seconds -= 60
    return stop_event.wait(seconds)


def _monitor_payouts(client, url, headers, stop_event, db, run_id, retry_after):
    backoff = CLICK_COOLDOWN_S
    limited = True
    while not stop_event.is_set():
        delay = max(backoff, _retry_after_seconds(retry_after)) if limited else MONITOR_INTERVAL_S
        if limited:
            db.update_run(run_id, "running", error_text=(
                f"Site rate limit reached. Next read-only payout check in {math.ceil(delay)}s. "
                "Payout confirmations paused."
            ))
        if _wait_for_monitor(stop_event, delay) or stop_event.is_set():
            return None
        try:
            response = client.get(url, headers=headers, max_redirects=0, timeout=10_000)
            try:
                status = response.status
                retry_after = response.headers.get("retry-after")
                content_type = response.headers.get("content-type", "")
            finally:
                response.dispose()
        except Exception:
            db.update_run(run_id, "running", error_text=(
                "Read-only payout check failed. Next check in 30s. Payout confirmations paused."
            ))
            limited = False
            continue
        if status == 429:
            backoff = min(backoff * 2, MONITOR_MAX_BACKOFF_S) if limited else CLICK_COOLDOWN_S
            limited = True
            continue
        if status in (401, 403) or 300 <= status < 400:
            return f"Read-only payout check requires sign-in or access (HTTP {status})."
        if 200 <= status < 300 and "application/json" in content_type.casefold():
            backoff = CLICK_COOLDOWN_S
            message = "Read-only payout monitoring resumed. Payout confirmations paused."
        elif 200 <= status < 300:
            return "Read-only payout check did not return a payout API response."
        else:
            message = f"Read-only payout check returned HTTP {status}. Next check in 30s."
        limited = False
        db.update_run(run_id, "running", error_text=message)


def run_browser(run_id: str, config: dict, stop_event: threading.Event, db, screenshot_dir: str):
    from playwright.sync_api import sync_playwright

    Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
    failure = None
    status = "completed"
    page = None
    try:
        with sync_playwright() as playwright:
            headless = os.getenv("HEADLESS", "false").lower() in ("true", "1", "yes")
            channel = os.getenv("BROWSER_CHANNEL", "chrome")
            stealth = bool(config.get("stealth_mode", True))
            launch_args = ["--disable-blink-features=AutomationControlled", "--disable-infobars"] if stealth else []
            proxy_cfg = _parse_proxy(config.get("proxy"))
            launch_kwargs = {"headless": headless, "args": launch_args}
            if stealth:
                launch_kwargs["ignore_default_args"] = ["--enable-automation"]
            if channel:
                launch_kwargs["channel"] = channel
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg
            try:
                browser = playwright.chromium.launch(**launch_kwargs)
            except Exception:
                fallback_kwargs = {"headless": headless, "args": launch_args}
                if stealth:
                    fallback_kwargs["ignore_default_args"] = ["--enable-automation"]
                if proxy_cfg:
                    fallback_kwargs["proxy"] = proxy_cfg
                browser = playwright.chromium.launch(**fallback_kwargs)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            if stealth:
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    if (!window.chrome) { window.chrome = {}; }
                    window.chrome.runtime = window.chrome.runtime || {};
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                """)
            page = context.new_page()
            page.set_default_timeout(10_000)
            js_errors: list[str] = []
            server_errors: list[str] = []
            page.on("pageerror", lambda error: js_errors.append(_redact(str(error), config)))
            page.on("console", lambda message: js_errors.append(_redact(message.text, config)) if message.type == "error" and "status of 424" not in message.text else None)
            accept_resp: list[tuple[int, str, str | None]] = []
            read_request = None
            target = urlparse(config["site_url"])

            def _remember_payout_read(response):
                nonlocal read_request
                request = response.request
                url = urlparse(response.url)
                if (request.method != "GET" or not response.ok or request.redirected_from
                        or (url.scheme, url.netloc) != (target.scheme, target.netloc)
                        or url.path not in PAYOUT_READ_PATHS):
                    return
                if read_request and url.path == PAYOUT_READ_PATHS[1]:
                    return
                try:
                    headers = {
                        key: value for key, value in request.all_headers().items()
                        if key in ("accept", "authorization")
                    }
                except Exception:
                    return
                read_request = (response.url, headers)

            page.on("response", _remember_payout_read)
            def _on_accept(response):
                try:
                    if response.request.method in ("POST", "PUT", "PATCH") and "/api/" in response.url and "login" not in response.url:
                        if response.status == 429:
                            path = urlparse(response.url).path[:150]
                            details = [f"Site returned HTTP 429 on {response.request.method} {path}"]
                            for name in ("retry-after", "cf-ray", "x-request-id"):
                                value = response.headers.get(name)
                                if value:
                                    details.append(f"{name}: {value[:80]}")
                            accept_resp.append((429, _redact(" · ".join(details), config), response.headers.get("retry-after")))
                        else:
                            accept_resp.append((response.status, response.text()[:300], None))
                except Exception:
                    pass
            page.on("response", _on_accept)
            page.on("response", lambda response: server_errors.append(_redact(f"HTTP {response.status} {response.url[:160]}", config)) if response.status >= 500 else None)

            parsed = urlparse(config["site_url"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Site URL must be a valid http or https URL")
            root = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
            page.goto(f"{root}/login", wait_until="domcontentloaded", timeout=30_000)
            email = page.get_by_role("textbox", name=re.compile("email", re.IGNORECASE))
            if not email.count():
                email = page.locator('input[type="email"], input[autocomplete="username"], input[type="text"]').first
            password = page.locator('input[type="password"]').first
            email.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
            password.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
            email.fill(config["email"])
            password.fill(config["password"])
            page.get_by_role("button", name=re.compile(r"^Sign in$", re.IGNORECASE)).click(timeout=10_000)
            try:
                page.wait_for_url(re.compile(r"/dashboard(?:$|[/?#])"), timeout=LOGIN_TIMEOUT_MS)
            except Exception as exc:
                db.update_login_status(run_id, "failed")
                raise RuntimeError("Login failed: dashboard redirect was not observed") from exc
            db.update_login_status(run_id, "success")
            if stop_event.is_set():
                status = "stopped"
            else:
                page.goto(f"{root}/payouts", wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(500)
                _close_chat_toast(page)
                _dismiss_blocking_dialogs(page)
                min_amount = Decimal(config["min_amount"]) if config.get("min_amount") else None
                max_amount = Decimal(config["max_amount"]) if config.get("max_amount") else None
                rate_limit = _continuous_loop(page, config, stop_event, db, run_id, screenshot_dir, accept_resp, js_errors, server_errors, min_amount, max_amount)
                if rate_limit:
                    failure, retry_after = rate_limit
                    status = "stopped"
                    if read_request and not stop_event.is_set():
                        storage_state = context.storage_state()
                        context.close()
                        browser.close()
                        page = None
                        client = playwright.request.new_context(storage_state=storage_state)
                        try:
                            url, headers = read_request
                            failure = _monitor_payouts(client, url, headers, stop_event, db, run_id, retry_after)
                        finally:
                            client.dispose()
                        context = None
                    elif not stop_event.is_set():
                        failure += " · No verified read-only payout endpoint was available."
            if context is not None:
                context.close()
                browser.close()
    except Exception as exc:
        status = "failed"
        failure = _redact(str(exc), config)
        if page is not None:
            try:
                _failure_screenshot(page, str(Path(screenshot_dir) / f"{run_id}_run_failure.png"), config)
            except Exception:
                pass
    if stop_event.is_set() and status != "failed":
        status = "stopped"
    try:
        db.update_run(run_id, status, utc_now(), failure)
    finally:
        config["email"] = ""
        config["password"] = ""
