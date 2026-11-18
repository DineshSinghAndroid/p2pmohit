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


def _do_page_refresh(page) -> bool:
    """25-second auto page reload: Refreshes the payouts list."""
    try:
        # Don't reload if the Accept confirmation modal is currently visible
        if page.locator('[automation-id="autotest_modal_button_accept"]:visible').count() > 0:
            return False
        # If any other stray dialog is open, close it first
        _close_any_dialog(page)
        page.reload(wait_until="domcontentloaded", timeout=15_000)
        page.wait_for_timeout(300)
        return True
    except Exception:
        return False


_tab_switch_refresh = _do_page_refresh
_refresh_list = _do_page_refresh



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
X_SEL = '[automation-id="tui-dialog__close"], .tui-dialog__close, button[class*="dialog__close"], button.t-wrapper, .t-wrapper, button:has-text("Close"), button:has-text("Cancel")'


def _close_any_dialog(page) -> None:
    """Close whatever useless dialog is open using the top-right X, .t-wrapper, or Escape."""
    for _ in range(3):
        try:
            dlg = page.locator('section[role="dialog"]:visible, [role="dialog"]:visible, .tui-dialog:visible')
            if not dlg.count():
                return
            x = dlg.last.locator(X_SEL)
            if x.count():
                x.first.click(timeout=1_500)
            else:
                m = dlg.last.locator(MODAL_CLOSE_SEL)
                if m.count():
                    m.first.click(timeout=1_500)
                else:
                    page.keyboard.press("Escape")
            page.wait_for_timeout(200)
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
        origin = f"{parsed.scheme}://{parsed.netloc}"
        email = page.get_by_role("textbox", name=re.compile("email", re.IGNORECASE))
        if not email.count():
            email = page.locator('input[type="email"], input[autocomplete="username"], input[type="text"]').first
        email.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
        email.fill(config["email"])
        password = page.locator('input[type="password"]').first
        password.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
        password.fill(config["password"])
        sign_btn = page.get_by_role("button", name=re.compile(r"sign in|log in|login", re.IGNORECASE))
        if sign_btn.count():
            sign_btn.first.click(timeout=10_000)
        else:
            page.locator('button[type="submit"], input[type="submit"]').first.click(timeout=10_000)
        try:
            page.wait_for_url(lambda u: "/login" not in u, timeout=LOGIN_TIMEOUT_MS)
        except Exception:
            pass
        page.goto(f"{origin}/payouts", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1500)
    except Exception:
        pass


def _continuous_loop(page, config, stop_event, db, run_id, screenshot_dir, accept_resp, js_errors, server_errors, min_amount, max_amount):
    """Keep accepting orders inside [min,max] until the user presses STOP."""
    seq = 0
    last_click = [0.0]
    last_refresh = [time.monotonic()]
    last_heartbeat = [time.monotonic()]
    seen_skipped_ids = set()
    consecutive_429 = [0]
    min_amt_f = float(min_amount) if min_amount is not None else None
    max_amt_f = float(max_amount) if max_amount is not None else None

    def log(msg: str, level: str = "INFO"):
        if hasattr(db, "add_log"):
            try:
                db.add_log(run_id, msg, level)
            except Exception:
                pass

    def record(amount, popup_shown, outcome, latency_ms, error_text=None, row_id=""):
        db.add_click({"run_id": run_id, "row_id": row_id, "amount": str(amount), "timestamp": utc_now(),
                      "popup_shown": int(popup_shown), "outcome": outcome, "latency_ms": latency_ms,
                      "error_text": error_text or None})

    log(f"0ms DOM MutationObserver scanner activated. Filter: ₹{int(min_amt_f):,}" if min_amt_f else "0ms DOM MutationObserver scanner activated. Filter: ₹0" + (f" - ₹{int(max_amt_f):,}" if max_amt_f else " - ∞"))

    while not stop_event.is_set():
        try:
            started = time.perf_counter()
            popup_shown = False
            amount = Decimal(0)
            row_id = ""
            try:
                accept_resp.clear()

                # IN-BROWSER 0ms SYNCHRONOUS MUTATION OBSERVER & FULL EVENT CLICKER
                turbo = page.evaluate("""([minAmt, maxAmt]) => {
                    return new Promise((resolve) => {
                        let finished = false;
                        let timeoutTimer = null;
                        let scanInterval = null;
                        let modalTimer = null;
                        let observer = null;
                        let orderListener = null;
                        let takenListener = null;

                        function fireFullClick(el) {
                            if (!el) return;
                            try { el.scrollIntoView({ block: 'nearest' }); } catch(_) {}
                            try { el.focus(); } catch(_) {}
                            const opts = { bubbles: true, cancelable: true, composed: true, view: window };
                            const mouseOpts = { ...opts, buttons: 1, button: 0 };
                            try { el.dispatchEvent(new PointerEvent('pointerover', opts)); } catch(_) {}
                            try { el.dispatchEvent(new PointerEvent('pointerenter', opts)); } catch(_) {}
                            try { el.dispatchEvent(new PointerEvent('pointerdown', mouseOpts)); } catch(_) {}
                            try { el.dispatchEvent(new MouseEvent('mousedown', mouseOpts)); } catch(_) {}
                            try { el.dispatchEvent(new PointerEvent('pointerup', mouseOpts)); } catch(_) {}
                            try { el.dispatchEvent(new MouseEvent('mouseup', mouseOpts)); } catch(_) {}
                            try { el.dispatchEvent(new MouseEvent('click', mouseOpts)); } catch(_) {}
                            try { el.click(); } catch(_) {}
                        }

                        function norm(s) {
                            return (s || '').replace(/[\\s\\u00a0\\u202f\\n\\r\\t]+/g, ' ').trim().toLowerCase();
                        }

                        function parseAmount(text) {
                            if (!text) return null;
                            const clean = text.replace(/[\\s\\u00a0\\u202f\\n\\r\\t]+/g, ' ').trim();
                            const m = clean.match(/([\\d][\\d\\s.,]*)\\s*INR/i) || clean.match(/₹\\s*([\\d][\\d\\s.,]*)/i);
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

                        function extractRowId(row) {
                            if (row.id) return row.id;
                            const autoId = row.getAttribute('automation-id');
                            if (autoId) return autoId;
                            const link = row.querySelector('a[href*="/payouts/"], a[href*="/order/"]');
                            if (link && link.getAttribute('href')) {
                                const parts = link.getAttribute('href').split('/');
                                return parts[parts.length - 1];
                            }
                            const cells = row.querySelectorAll('td, th, [role="cell"]');
                            for (const cell of cells) {
                                const t = norm(cell.innerText || cell.textContent || '');
                                if (t && !t.includes('inr') && !t.includes('imps') && !t.includes('upi') && !t.includes(':') && t.length > 2) {
                                    return t;
                                }
                            }
                            return '';
                        }

                        const skippedItems = [];
                        let outOfRangeCount = 0;

                        function checkPage() {
                            if (finished) return;

                            // 1. Process all open popups/dialogs
                            const allDialogs = document.querySelectorAll('section[role="dialog"]:not(.hidden), [role="dialog"]:not(.hidden), .tui-dialog, tui-dialog');
                            for (const dlg of allDialogs) {
                                const dText = norm(dlg.innerText || dlg.textContent || '');

                                // Check if this is the "Accept for processing" confirmation modal
                                const confirmBtn = dlg.querySelector('[automation-id="autotest_modal_button_accept"]') ||
                                                   [...dlg.querySelectorAll('button')].find(b => {
                                                       const t = norm(b.innerText || b.textContent || '');
                                                       const autoId = (b.getAttribute('automation-id') || '').toLowerCase();
                                                       return autoId === 'autotest_modal_button_accept' || t === 'accept' || t === 'confirm' || t === 'accept for processing';
                                                   });

                                const isAcceptModal = confirmBtn && !confirmBtn.disabled &&
                                                      !dText.includes('another trader') &&
                                                      !dText.includes('too late') &&
                                                      !dText.includes('already processed');

                                if (isAcceptModal) {
                                    finished = true;
                                    cleanup();
                                    fireFullClick(confirmBtn);
                                    resolve({
                                        clicked: true,
                                        confirmed: true,
                                        popup_shown: true,
                                        amount: 0,
                                        row_id: 'modal_confirmed',
                                        elapsed_ms: 10
                                    });
                                    return;
                                }

                                // Otherwise, this is a useless/blocking popup (e.g. "Processing of the request", "Accepted by another trader", error alert, etc.)
                                // Close it immediately using close button / .t-wrapper / Escape!
                                const closeBtn = dlg.querySelector('[automation-id="tui-dialog__close"]') ||
                                                 dlg.querySelector('[automation-id="autotest_modal_button_close"]') ||
                                                 dlg.querySelector('button.t-wrapper, .t-wrapper button, button[class*="dialog__close"]') ||
                                                 dlg.querySelector('.t-wrapper') ||
                                                 [...dlg.querySelectorAll('button, [role="button"]')].find(b => {
                                                     const t = norm(b.innerText || b.textContent || '');
                                                     return t === 'close' || t === 'cancel' || t === 'dismiss' || t === 'ok';
                                                 });

                                if (closeBtn) {
                                    fireFullClick(closeBtn);
                                } else {
                                    try {
                                        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, which: 27, bubbles: true }));
                                    } catch (_) {}
                                }
                            }

                            // 2. LASER TARGET: ONLY ROW 1 (THE TOP / NEWEST ORDER IN TABLE)
                            const allRows = document.querySelectorAll('tbody tr, [role="row"], tr');
                            let topRow = null;
                            for (const tr of allRows) {
                                if (tr.querySelector('th')) continue;
                                const text = norm(tr.innerText || tr.textContent || '');
                                if (text.includes('application received') || text.includes('waiting time') || (text.includes('payment method') && !text.includes('inr'))) {
                                    continue;
                                }
                                topRow = tr;
                                break;
                            }

                            if (!topRow) return;

                            const btn = topRow.querySelector('[automation-id="autotest_button_tradeAccept"], button, [role="button"], a');
                            if (!btn) return;

                            const btnText = norm(btn.innerText || btn.textContent || '');
                            const autoId = (btn.getAttribute('automation-id') || '').toLowerCase();

                            // If Row 1 is already taken by another trader, DO NOT check lower rows!
                            if (btnText.includes('another trader') || btnText.includes('too late') || btnText.includes('expired') || btnText.includes('already')) {
                                return;
                            }

                            const isAccept = autoId === 'autotest_button_tradeaccept' ||
                                             btnText === 'accept for processing' ||
                                             btnText === 'accept' ||
                                             btnText.startsWith('accept') ||
                                             btnText.includes('processing');

                            if (!isAccept || btn.disabled || btn.getAttribute('aria-disabled') === 'true') {
                                return;
                            }

                            const rowText = topRow.innerText || topRow.textContent || '';
                            const amt = parseAmount(rowText) || parseAmount(btn.innerText || '');
                            const rowId = extractRowId(topRow);

                            if (amt !== null) {
                                if (minAmt !== null && amt < minAmt) {
                                    outOfRangeCount++;
                                    skippedItems.push({ row_id: rowId, amount: amt, outcome: 'SKIPPED', reason: `Amount ₹${amt} < Min ₹${minAmt}` });
                                    return; // Top order out of range; wait for next top order
                                }
                                if (maxAmt !== null && amt > maxAmt) {
                                    outOfRangeCount++;
                                    skippedItems.push({ row_id: rowId, amount: amt, outcome: 'SKIPPED', reason: `Amount ₹${amt} > Max ₹${maxAmt}` });
                                    return; // Top order out of range; wait for next top order
                                }
                            }

                            // 0ms LASER HIT ON TOP ORDER!
                            finished = true;
                            cleanup();
                            fireFullClick(btn);
                            const startTime = performance.now();

                                modalTimer = setInterval(() => {
                                    const elapsed = performance.now() - startTime;

                                    const confirmBtn = document.querySelector('[automation-id="autotest_modal_button_accept"]') ||
                                                       [...document.querySelectorAll('section[role="dialog"] button, [role="dialog"] button')].find(b => {
                                                           const t = norm(b.innerText || b.textContent || '');
                                                           return t === 'accept' || t === 'confirm' || b.getAttribute('automation-id') === 'autotest_modal_button_accept';
                                                       });
                                    if (confirmBtn && !confirmBtn.disabled) {
                                        clearInterval(modalTimer);
                                        fireFullClick(confirmBtn);
                                        resolve({
                                            clicked: true,
                                            confirmed: true,
                                            popup_shown: true,
                                            amount: amt || 0,
                                            row_id: rowId,
                                            elapsed_ms: Math.round(elapsed)
                                        });
                                        return;
                                    }

                                    const dialog = document.querySelector('section[role="dialog"]:not(.hidden), [role="dialog"]:not(.hidden)');
                                    if (dialog) {
                                        const dText = norm(dialog.innerText || dialog.textContent || '');
                                        if (dText.includes('processing of the request') || dText.includes('make and confirm the transfer') || dText.includes('payment method')) {
                                            clearInterval(modalTimer);
                                            const modalAmt = parseAmount(dialog.innerText || dialog.textContent || '');
                                            resolve({
                                                clicked: true,
                                                confirmed: true,
                                                popup_shown: true,
                                                processing_opened: true,
                                                amount: modalAmt || amt || 0,
                                                row_id: rowId,
                                                elapsed_ms: Math.round(elapsed)
                                            });
                                            return;
                                        }
                                        if (dText.includes('another trader') || dText.includes('too late') || dText.includes('expired')) {
                                            clearInterval(modalTimer);
                                            resolve({
                                                clicked: true,
                                                confirmed: false,
                                                popup_shown: true,
                                                taken_by_other: true,
                                                amount: amt || 0,
                                                row_id: rowId,
                                                elapsed_ms: Math.round(elapsed)
                                            });
                                            return;
                                        }
                                    }

                                    if (elapsed > 2000) {
                                        clearInterval(modalTimer);
                                        resolve({
                                            clicked: true,
                                            confirmed: true,
                                            popup_shown: !!dialog,
                                            amount: amt || 0,
                                            row_id: rowId,
                                            elapsed_ms: Math.round(elapsed)
                                        });
                                    }
                                }, 10);

                                return;
                        }

                        try {
                            observer = new MutationObserver(() => checkPage());
                            observer.observe(document.body, { childList: true, subtree: true, characterData: true });
                        } catch (_) {}

                        function cleanup() {
                            if (observer) {
                                try { observer.disconnect(); } catch(_) {}
                                observer = null;
                            }
                            if (scanInterval) {
                                clearInterval(scanInterval);
                                scanInterval = null;
                            }
                            if (timeoutTimer) {
                                clearTimeout(timeoutTimer);
                                timeoutTimer = null;
                            }
                            if (modalTimer) {
                                clearInterval(modalTimer);
                                modalTimer = null;
                            }
                            if (orderListener) {
                                try { window.removeEventListener('p2p:order:new', orderListener); } catch(_) {}
                                orderListener = null;
                            }
                            if (takenListener) {
                                try { window.removeEventListener('p2p:order:taken', takenListener); } catch(_) {}
                                takenListener = null;
                            }
                        }

                        orderListener = () => { checkPage(); };
                        takenListener = () => { checkPage(); };
                        window.addEventListener('p2p:order:new', orderListener);
                        window.addEventListener('p2p:order:taken', takenListener);

                        scanInterval = setInterval(checkPage, 20);
                        checkPage();

                        timeoutTimer = setTimeout(() => {
                            if (!finished) {
                                finished = true;
                                cleanup();

                                const allRows = document.querySelectorAll('tbody tr, [role="row"], tr');
                                const allBtns = document.querySelectorAll('button, [role="button"]');
                                let taken = 0;
                                allBtns.forEach(b => {
                                    const t = norm(b.innerText || b.textContent || '');
                                    if (t.includes('another trader')) taken++;
                                });

                                let banner = '';
                                const warningEl = document.querySelector('.tui-notification, [role="alert"]');
                                if (warningEl) {
                                    const wt = (warningEl.innerText || '').trim();
                                    if (wt.includes('500 USDT') || wt.includes('suspended')) {
                                        banner = wt;
                                    }
                                }

                                resolve({
                                    clicked: false,
                                    total_rows: allRows.length,
                                    taken_count: taken,
                                    out_of_range_count: outOfRangeCount,
                                    skipped_items: skippedItems.slice(0, 5),
                                    warning: banner
                                });
                            }
                        }, 2500);
                    });
                }""", [min_amt_f, max_amt_f])

                if not turbo.get("clicked"):
                    if turbo.get("warning"):
                        log(f"⚠️ TARGET SITE ALERT: {turbo['warning']}", level="WARN")

                    for item in turbo.get("skipped_items", []):
                        r_id = item.get("row_id") or f"row_{item.get('amount')}"
                        if r_id not in seen_skipped_ids:
                            seen_skipped_ids.add(r_id)
                            if len(seen_skipped_ids) > 1000:
                                seen_skipped_ids.clear()
                            amt_val = Decimal(str(item.get("amount") or 0))
                            record(
                                amount=amt_val,
                                popup_shown=False,
                                outcome=item.get("outcome", "SKIPPED"),
                                latency_ms=0,
                                error_text=item.get("reason"),
                                row_id=r_id
                            )
                            log(f"Skipped order {r_id} (₹{amt_val}): {item.get('reason')}")

                    if time.monotonic() - last_heartbeat[0] >= 3.0:
                        last_heartbeat[0] = time.monotonic()
                        t_rows = turbo.get("total_rows", 0)
                        taken = turbo.get("taken_count", 0)
                        oor = turbo.get("out_of_range_count", 0)
                        range_str = f"₹{int(min_amt_f):,}" if min_amt_f else "₹0"
                        range_str += f" - ₹{int(max_amt_f):,}" if max_amt_f else " - ∞"
                        log(f"Active MutationObserver: {t_rows} orders visible ({taken} taken, {oor} outside [{range_str}]). 25s auto-refresh active.")

                    if time.monotonic() - last_refresh[0] >= 25.0:
                        last_refresh[0] = time.monotonic()
                        log("🔄 25s auto-refresh: Reloading payouts page...")
                        _do_page_refresh(page)
                        continue

                    if stop_event.wait(0.01):
                        break
                    continue

                seq += 1
                last_click[0] = time.monotonic()
                amount = Decimal(str(turbo["amount"]))
                row_id = turbo.get("row_id", "")
                lat = turbo.get("elapsed_ms", round((time.perf_counter() - started) * 1000))

                if turbo.get("taken_by_other"):
                    record(amount, True, "TAKEN_BY_OTHER", lat, row_id=row_id)
                    log(f"Order #{row_id} (₹{amount}) was taken by another trader in {lat}ms.")
                    _close_any_dialog(page)
                    continue

                outcome = "ACCEPTED"
                err = None

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

                if outcome == "ACCEPTED":
                    log(f"⚡ ORDER ACCEPTED! ₹{amount} (Order #{row_id}) secured in {lat}ms! Transfer details ready.")
                    if config.get("max_clicks") and seq >= config["max_clicks"]:
                        log(f"Reached configured limit of {config['max_clicks']} orders. Stopping run.")
                        break
                    # Wait 5s cooldown after an accept before taking next order
                    if stop_event.wait(5.0):
                        break
                    continue

                if outcome == "RATE_LIMITED":
                    _close_any_dialog(page)
                    consecutive_429[0] += 1
                    server_wait = _retry_after_seconds(retry_after)
                    wait_s = max(2.0, server_wait)
                    db.update_run(run_id, "running", error_text=f"Rate limited (429 #{consecutive_429[0]}). Waiting {int(wait_s)}s before auto-retry...")
                    last_click[0] = time.monotonic() + wait_s
                    if stop_event.wait(wait_s):
                        break
                    db.update_run(run_id, "running", error_text=None)
                    continue

                consecutive_429[0] = 0
                _close_any_dialog(page)
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
            if stop_event.wait(0.5):
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
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="Asia/Kolkata",
            )
            if stealth:
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    if (!window.chrome) { window.chrome = {}; }
                    window.chrome.runtime = window.chrome.runtime || {};
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                """)
            context.add_init_script("""
                (function() {
                    const OrigWebSocket = window.WebSocket;
                    if (!OrigWebSocket) return;
                    window._p2pSockets = window._p2pSockets || [];
                    window._lastWsPayout = null;

                    class P2PWebSocket extends OrigWebSocket {
                        constructor(url, protocols) {
                            super(url, protocols);
                            try {
                                window._p2pSockets.push(this);
                                window._activeP2pSocket = this;
                                const safeUrl = typeof url === 'string' ? url.split('?')[0] : 'ws';
                                console.log('[P2P WS Hook] Connected to:', safeUrl);
                            } catch (e) {}

                            this.addEventListener('message', (event) => {
                                try {
                                    const message = JSON.parse(event.data);
                                    const payload = typeof message.data === 'string' ? JSON.parse(message.data) : message.data;

                                    if (message.type === 'PAYOUT') {
                                        const order = payload?.payout;
                                        const orderId = payload?.payoutId ?? order?.id;
                                        const amount = Number(order?.fiatAmount || 0);
                                        window._lastWsPayout = {
                                            id: orderId,
                                            amount: amount,
                                            status: payload?.status ?? order?.status,
                                            created: order?.created,
                                            time: Date.now()
                                        };
                                        console.log('[P2P WS Hook] PAYOUT:', orderId, 'Amount: ₹' + amount);
                                        window.dispatchEvent(new CustomEvent('p2p:order:new', { detail: window._lastWsPayout }));
                                    } else if (message.type === 'PAYOUT_ACCEPTED_BY_ANOTHER_TRADER') {
                                        const orderId = payload?.payoutId;
                                        window.dispatchEvent(new CustomEvent('p2p:order:taken', { detail: { id: orderId } }));
                                    }
                                } catch (err) {}
                            });
                        }
                    }

                    window.WebSocket = P2PWebSocket;
                })();
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

            def log(msg: str, level: str = "INFO"):
                if hasattr(db, "add_log"):
                    try:
                        db.add_log(run_id, msg, level)
                    except Exception:
                        pass

            page.on("response", _remember_payout_read)
            def _on_accept(response):
                try:
                    req = response.request
                    if "/api/" in response.url and "login" not in response.url:
                        u_path = urlparse(response.url).path
                        if req.method in ("POST", "PUT", "PATCH") or "payout" in u_path:
                            snippet = ""
                            try:
                                snippet = response.text()[:200].replace("\n", " ")
                            except Exception:
                                pass
                            log(f"🌐 API {req.method} {u_path} -> HTTP {response.status} | {snippet}")

                        if req.method in ("POST", "PUT", "PATCH"):
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

            def _on_ws(ws):
                safe_url = ws.url.split("?")[0]
                log(f"🔌 WebSocket connected: {safe_url}")
                def _on_frame(payload):
                    try:
                        text = payload if isinstance(payload, str) else payload.decode("utf-8", "ignore")
                        if "PAYOUT" in text:
                            log(f"⚡ WS Event received: {text[:160]}")
                    except Exception:
                        pass
                ws.on("framereceived", _on_frame)
                ws.on("close", lambda: log(f"🔌 WebSocket closed: {safe_url}", level="DEBUG"))
            page.on("websocket", _on_ws)

            parsed = urlparse(config["site_url"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Site URL must be a valid http or https URL")

            log(f"Worker started. Headless: {headless}, Stealth: {stealth}, Proxy: {'Yes' if proxy_cfg else 'Direct'}")
            origin = f"{parsed.scheme}://{parsed.netloc}"

            db.update_login_status(run_id, "logging_in")
            log(f"Navigating to login page ({origin}/login)...")
            page.goto(f"{origin}/login", wait_until="domcontentloaded", timeout=30_000)

            auth_fail_msg = [None]
            def _check_auth_resp(response):
                if any(x in response.url for x in ("/api/login", "/api/auth", "/api/user")):
                    if response.status >= 400:
                        try:
                            body = response.json()
                            auth_fail_msg[0] = body.get("message") or body.get("error") or body.get("detail") or f"HTTP {response.status}"
                        except Exception:
                            auth_fail_msg[0] = f"HTTP {response.status}"

            page.on("response", _check_auth_resp)

            if "/login" in page.url or page.locator('input[type="password"]').count():
                log("Login page loaded. Waiting for form fields...")
                try:
                    page.wait_for_selector('input[type="password"], input[type="email"], input[name="email"]', timeout=10_000)
                except Exception:
                    pass
                email = page.locator('input[type="email"], input[name="email"], input[autocomplete="username"], input[type="text"]').first
                password = page.locator('input[type="password"]').first
                email.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
                password.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
                log(f"Filling account credentials ({config.get('email', '')[:4]}***)...")
                email.fill(config["email"])
                password.fill(config["password"])
                log("Clicking Sign In button...")
                sign_btn = page.get_by_role("button", name=re.compile(r"sign in|log in|login", re.IGNORECASE))
                if sign_btn.count():
                    sign_btn.first.click(timeout=10_000)
                else:
                    page.locator('button[type="submit"], input[type="submit"]').first.click(timeout=10_000)

                log("Credentials submitted. Verifying session...")
                start_login_wait = time.monotonic()
                while time.monotonic() - start_login_wait < 15.0:
                    if "/login" not in page.url:
                        break
                    if auth_fail_msg[0]:
                        db.update_login_status(run_id, "failed")
                        err_text = f"Target site error: {auth_fail_msg[0]}"
                        log(err_text, level="ERROR")
                        raise RuntimeError(err_text)
                    err_msg = ""
                    try:
                        app_text = page.locator('app-root').inner_text(timeout=300)
                        if "incorrect email or password" in app_text.lower():
                            err_msg = "Incorrect email or password"
                        elif "invalid" in app_text.lower():
                            for l in app_text.splitlines():
                                if any(w in l.lower() for w in ("invalid", "incorrect", "failed", "blocked", "suspended")):
                                    err_msg = l.strip()
                                    break
                    except Exception:
                        pass
                    if err_msg:
                        db.update_login_status(run_id, "failed")
                        log(f"Login failed: {err_msg}", level="ERROR")
                        raise RuntimeError(f"Target site error: {err_msg}")
                    page.wait_for_timeout(300)
                    if stop_event.is_set():
                        return

                if "/login" in page.url:
                    db.update_login_status(run_id, "failed")
                    log("Login timed out: did not leave login page", level="ERROR")
                    raise RuntimeError("Login failed: page did not leave /login after 15 seconds")

            log("Login verified! Navigating to /payouts...")
            db.update_login_status(run_id, "navigating_payouts")
            if stop_event.is_set():
                status = "stopped"
            else:
                page.goto(f"{origin}/payouts", wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(500)
                _close_chat_toast(page)
                _dismiss_blocking_dialogs(page)
                db.update_login_status(run_id, "success")
                log("On payouts page! Active order monitoring started.")
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
