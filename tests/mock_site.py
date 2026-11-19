"""Local stand-in for the game site. Binds to 127.0.0.1 and serves fake data only.

Reproduces the evidence in plan.md: i18n literals in __NEXT_DATA__ on every page,
late hydration that wipes inputs, a "1 Remaining" badge and a Claim Now! button
that are visible logged out, images gated on load, a server-side claim ledger,
and pages on which neither `load` nor networkidle ever happens.
"""

import base64
import json
import select
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PASSWORD = "fake-Pw-7Qz!"
PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

I18N = json.dumps({"props": {"i18n": {
    "giftAvailable": "Available again at",
    "giftRemaining": "remaining",
    "claim": "Claim Now!",
    "dailyGiftFallbackMsg": "You have claimed the current offer. Check back soon for the next available offer!",
    "selectCharacter": "Select the Character",
}}})

BANNERS = {
    "": "",
    "onetrust": '<div id="onetrust-banner-sdk" style="position:fixed;inset:0;background:#fff;z-index:9">'
                '<button id="onetrust-accept-btn-handler" onclick="this.parentNode.remove()">I Agree</button></div>',
    "accept": '<div id="consent" style="position:fixed;inset:0;background:#fff;z-index:9">'
              '<button onclick="this.parentNode.remove()">Accept All</button></div>',
}

COMMON_JS = """
setInterval(() => fetch('/poll').catch(() => {}), 200);
for (let i = 0; i < 8; i++) {
  fetch('/api/tunnel/analytics/post-event', {method: 'POST', body: '{}'}).catch(() => {});
}
function gated(src, then) { const i = new Image(); i.onload = then; i.src = src; }
function spinAfter(ms) { setTimeout(() => { while (true) {} }, ms); }
async function signIn(o) {
  if (o.no_sign_in) return;
  if (o.sign_in_start_ms) await new Promise(done => setTimeout(done, o.sign_in_start_ms));
  await fetch('/api/appcharge/get-token', {method: 'POST', body: '{}'});
  await fetch('/api/auth/callback/credentials', {method: 'POST', body: '{}'});
}
function addCard(cards, o, c) {
  const b = document.createElement('button');
  if (c === o.selected) b.className = 'selected-profile';
  b.textContent = c;
  b.__reactProps$x = {};
  const avatar = '/a/chat_avatar_' + c + '.png';
  gated(avatar, () => {
    const i = document.createElement('img'); i.alt = ''; i.src = avatar; b.prepend(i);
  });
  gated('/a/icon_power.png', () => {
    const i = document.createElement('img'); i.alt = 'Total Power'; i.src = '/a/icon_power.png'; b.appendChild(i);
  });
  b.addEventListener('click', async () => {
    if ((o.select_never_answers || []).includes(c)) {
      fetch('/api/account/select-profile?locale=en-us',
            {method: 'POST', body: JSON.stringify({character: c})}).catch(() => {});
      document.getElementById('dlg').innerHTML = '';
      signIn(o);
      return;
    }
    let selected = true;
    if (!(c === o.selected && o.selected_no_post)) {
      const r = await fetch('/api/account/select-profile?locale=en-us',
                            {method: 'POST', body: JSON.stringify({character: c})});
      selected = r.ok;
    }
    if (!selected && o.select_failure_keeps_dialog) return;
    document.getElementById('dlg').innerHTML = '';
    if (selected) signIn(o);
  });
  cards.appendChild(b);
}
function showDialog(o) {
  const cookie = '<div role="dialog" aria-label="cookies"><p>We use cookies</p>'
    + '<button>Accept All</button><button>Cookie Settings</button></div>';
  // A kept dialog is modal, as the real one is: it sits over the header's avatar
  const modal = o.select_failure_keeps_dialog ? ' style="position:fixed;inset:0;background:#fff;z-index:5"' : '';
  const shell = '<div role="dialog" id="cd"' + modal + '><p>Signed in as: Fake</p>'
    + '<p>Select the Character you would like to use for store purchases below.</p>'
    + '<div id="cards"></div></div>';
  document.getElementById('dlg').innerHTML =
    (o.cookie === 'before' ? cookie : '') + shell + (o.cookie === 'after' ? cookie : '');
  if (o.freeze_dialog_ms !== null && o.freeze_dialog_ms !== undefined) spinAfter(o.freeze_dialog_ms);
  if (o.shell_ms < 0) return;
  o.characters.forEach((c, i) => setTimeout(() => {
    const cards = document.getElementById('cards');
    if (cards) addCard(cards, o, c);
  }, o.shell_ms + i * (o.card_gap_ms || 0)));
}
"""

LOGIN_PAGE = """<!doctype html><html><head><title>login</title>
<script async src="/never.js"></script>
<style>@font-face{font-family:Fake;src:url(/f/fake.woff2)} body{font-family:Fake,serif}</style>
</head><body>
<script id="__NEXT_DATA__" type="application/json">__I18N__</script>
__BANNER__
<input __EMAIL_ATTR__><input type="password" __PW_ATTR__><button id="login">Login</button>
<div id="dlg"></div>
<script>
__COMMON__
setTimeout(() => {
  const [email, password] = document.querySelectorAll('input');
  const login = document.getElementById('login');
  email.value = ''; password.value = '';
  for (const el of [email, password, login]) el.__reactProps$x = {};
  login.addEventListener('click', async () => {
    const r = await fetch('/api/account/login?locale=en-us', {method: 'POST',
      body: JSON.stringify({email: email.value, password: password.value})});
    if (!r.ok) return;
    const o = await r.json();
    if (o.poll_session_on_login) setInterval(() => fetch('/api/auth/session').catch(() => {}), 300);
    if (o.sign_in_before_dialog) signIn(o);
    if (o.characters.length) setTimeout(() => showDialog(o), o.dialog_ms);
    else if (!o.sign_in_before_dialog) signIn(o);
  });
}, __HYDRATE_MS__);
</script></body></html>"""

VAULT_PAGE = """<!doctype html><html><head><title>vault</title>
<script async src="/never.js"></script>
</head><body>
<script id="__NEXT_DATA__" type="application/json">__I18N__</script>
<div id="hdr"></div>
<div id="menu" style="display:none"><button id="change">Change Account</button></div>
<div id="dlg"></div>
<div id="card"><span>1 Remaining</span></div>
<script>
__COMMON__
const S = __STATE__;
const card = document.getElementById('card');
function cooldown() {
  card.innerHTML = '<p class="gift-expire-message">Available again at 00:00 UTC</p>'
    + '<p class="gift-expire-message">11 hours remaining!</p>';
}
let sessionState = 'loading';
async function fetchSession() {
  try {
    const fetching = fetch('/api/auth/session');
    // Late enough that the script has seen the request, or there is nothing to lose
    if (S.replace_state_url) setTimeout(() => history.replaceState(null, '', S.replace_state_url), 150);
    const r = await fetching;
    const text = (await r.text()).trim();
    sessionState = ['', '{}', 'null'].includes(text) ? 'empty' : 'ready';
  } catch (e) {}
}
function loadSession() {
  if (S.session_start_ms >= 0) setTimeout(fetchSession, S.session_start_ms);
}
setTimeout(loadSession, S.button_ms + S.claim_hydrate_ms);
if (S.freeze_vault_ms !== null) spinAfter(S.button_ms + S.freeze_vault_ms);
if (S.poll_session) setInterval(fetchSession, 300);
function freezeIfAsked() { if (S.freeze_after_click) setTimeout(() => { while (true) {} }, 50); }
function fail(detail) {
  setTimeout(() => { console.error('Free purchase handling failed:', detail); freezeIfAsked(); }, S.fail_delay_ms);
}
function render() {
  if (S.mode === 'claim') {
    let b = document.createElement('button');
    b.textContent = 'Claim Now!';
    card.appendChild(b);
    setTimeout(() => {
      if (S.replace_on_hydrate) { const n = b.cloneNode(true); b.replaceWith(n); b = n; }
      b.__reactProps$x = {};
      b.addEventListener('click', async () => {
        for (const [level, text] of S.click_console) console[level](text);
        if (S.click_throw) setTimeout(() => { throw new Error(S.click_throw); }, 0);
        for (let i = 0; i < S.click_analytics_calls; i++) {
          fetch('/api/tunnel/analytics/post-event', {method: 'POST', body: '{}'}).catch(() => {});
        }
        for (let i = 0; i < S.click_api_calls; i++) fetch('/api/extra/' + i).catch(() => {});
        for (const url of S.click_urls) fetch(url).catch(() => {});
        // Matching the real page: a session still loading, or empty, reads as
        // logged out, so the click logs out and never reaches the claim
        if (sessionState !== 'ready') {
          await fetch('/api/account/logout', {method: 'POST', body: '{}'}).catch(() => {});
          await fetch('/api/auth/signout', {method: 'POST', body: '{}'}).catch(() => {});
          location.href = '/en-us/login';
          return;
        }
        if (S.claim_no_request) {
          console.error('Free purchase handling failed:', 'Missing ENCRYPTION_PUBLIC_KEY in environment variables');
          return freezeIfAsked();
        }
        if (S.cooldown_without_claim) { setTimeout(cooldown, 200); return; }
        // The app's own rule (plan finding 20): judged by truthiness, and a body
        // that isn't JSON throws into the same failure path
        try {
          const r = await fetch('/api/appcharge/collect-offer', {method: 'post', body: '{}'});
          const c = await r.json();
          if (!r.ok || c?.error || c?.errorCode) return fail(c?.error ?? c?.errorMessage ?? c);
          if (S.confirm_ms >= 0) setTimeout(cooldown, S.confirm_ms);
        } catch (e) { fail(e); }
      });
    }, S.claim_hydrate_ms);
  } else if (S.mode === 'cooldown') {
    cooldown();
  } else if (S.mode === 'claimedmsg') {
    card.innerHTML = '<p>You have claimed the current offer. Check back soon for the next available offer!</p>';
  }
}
setTimeout(render, S.button_ms);
setTimeout(() => {
  const src = '/a/chat_avatar_header.png';
  gated(src, () => {
    const b = document.createElement('button');
    b.className = 'chakra-menu__menu-button';
    b.innerHTML = '<img class="login-image-border" src="' + src + '">Fake';
    b.__reactProps$x = {};
    b.addEventListener('click', () => { document.getElementById('menu').style.display = 'block'; });
    document.getElementById('hdr').appendChild(b);
  });
}, S.button_ms + S.avatar_ms);
document.getElementById('change').addEventListener('click', async () => {
  document.getElementById('menu').style.display = 'none';
  const r = await fetch('/api/me');
  showDialog(await r.json());
});
</script></body></html>"""

GATED_PAGE = """<!doctype html><html><body>
<script id="__NEXT_DATA__" type="application/json">__I18N__</script>
<div id="hdr"></div><div id="dlg"></div>
<script>
__COMMON__
gated('/a/chat_avatar_header.png', () => {
  document.getElementById('hdr').innerHTML =
    '<button><img src="/a/chat_avatar_header.png">Fake</button>';
});
showDialog({characters: ['c1'], selected: 'c1', shell_ms: 0, cookie: 'none'});
</script></body></html>"""


@dataclass
class Account:
    """Per-account behaviour. Everything the login page does before the POST is
    global to the mock, because the server doesn't know who is logging in yet."""
    password: str = PASSWORD
    characters: tuple = ()          # empty: single character, no dialog
    vault: str = "claim"            # claim | cooldown | claimedmsg | nothing | hang
    login_status: int = 200         # non-200 forces that status
    login_delay_ms: int = 0
    login_hang: bool = False
    dialog_ms: int = 200            # response -> dialog
    shell_ms: int = 300             # dialog shell -> first login-page card; -1 never
    card_gap_ms: int = 0            # login dialog: gap between one card and the next
    reopen_shell_ms: int = 0        # same, for the dialog reopened from the vault; -1 never
    reopen_card_gap_ms: int = 0     # reopened dialog: gap between one card and the next
    cookie: str = "none"            # none | before | after
    selected_no_post: bool = False  # the selected card doesn't POST
    select_ms: int = 100
    vault_delay_ms: int = 0
    button_ms: int = 200            # badge -> Claim Now!
    claim_hydrate_ms: int = 150     # Claim Now! visible -> clickable
    replace_on_hydrate: bool = False
    avatar_ms: int = 200            # Claim Now! -> header avatar
    confirm_ms: int = 300           # claim -> cooldown text; -1 never
    selected_character: str = ""    # the card marked selected-profile; default the first
    select_failures: tuple = ()     # characters whose select-profile fails
    select_never_answers: tuple = ()  # select-profile lands but never answers, and sign-in still runs
    select_failure_status: int = 500  # 0 never responds
    select_failure_keeps_dialog: bool = False
    freeze_on: str = ""             # dialog | vault | reopen: spin after it paints, before any click
    freeze_on_character: str = ""   # freeze only from this character onward
    freeze_delay_ms: int = None     # paint -> spin; default 1000 login dialog, 0 reopened, 50 vault
    token_ms: int = 50              # get-token delay
    callback_ms: int = 100          # auth callback delay; -1 never responds
    callback_status: int = 200
    no_sign_in: bool = False        # the page never starts sign-in
    sign_in_before_dialog: bool = False  # login signs in at once, and still shows any dialog after dialog_ms
    sign_in_start_ms: int = 0       # character choice or login -> get-token
    session_ms: int = 300           # session fetch delay; -1 never responds
    session_start_ms: int = 0       # hydration -> session fetch; -1 never fetches
    session_body: str = None        # None: derived from whether sign-in finished
    session_drop: bool = False      # the session fetch fails with no response
    poll_session_on_login: bool = False  # the login page keeps fetching the session
    poll_session_on_vault_load: int = 0  # this vault load keeps fetching the session
    replace_state_url: str = ""     # history.replaceState just after the session fetch starts
    click_analytics_calls: int = 0  # analytics POSTs during the click, ahead of the others
    click_api_calls: int = 0        # extra /api/ calls during the click
    click_urls: tuple = ()          # extra GETs during the click
    collect_status: int = 200       # collect-offer status
    collect_body: str = "{}"        # collect-offer body
    collect_ms: int = 0             # collect-offer delay; -1 never responds
    collect_stall_ms: int = 0       # headers sent, then the body this much later
    collect_fail_characters: tuple = ()  # these characters get status 500
    claim_no_request: bool = False  # the click logs an error and sends nothing
    cooldown_without_claim: bool = False  # the click sends nothing, cooldown text appears
    click_console: tuple = ()       # (level, text) pairs logged on the click
    click_throw: str = ""           # thrown on the click, reaching pageerror
    fail_delay_ms: int = 0          # failure -> the app's console.error
    freeze_after_click: bool = False  # the failure spins the renderer forever
    vault_abort_times: int = 0      # first N vault documents per session are 204
    vault_abort_at: tuple = ()      # these vault load numbers are 204
    login_failures: int = 0         # first N login POSTs fail, right password or not
    login_failure_status: int = 500
    cooldown_characters: tuple = () # characters already on cooldown before this run
    unknown_characters: tuple = ()  # characters whose vault shows neither button nor cooldown


def js_truthy(value):
    """Written apart from claim_common's, as this one is the test's oracle"""
    return bool(value) or isinstance(value, (dict, list))


def server_accepts(status, body):
    """Whether a claim lands: an ok status, a JSON body, no truthy error field"""
    try:
        parsed = json.loads(body)
    except ValueError:
        return False
    fields = parsed if isinstance(parsed, dict) else {}
    return 200 <= status < 300 and not js_truthy(fields.get("error")) and not js_truthy(fields.get("errorCode"))


@dataclass
class Claim:
    email: str
    character: str
    visitor: str


@dataclass
class Visitor:
    first: float
    last: float
    email: str = None


class Mock:
    def __init__(self, accounts=None, hydrate_ms=300, email_attr="", pw_attr="",
                 banner="", login_page_status=200, drop_paths=()):
        self.accounts = accounts or {}
        self.hydrate_ms = hydrate_ms
        self.email_attr = email_attr
        self.pw_attr = pw_attr
        self.banner = banner
        self.login_page_status = login_page_status
        self.drop_paths = drop_paths  # these close the connection with no response

        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.sessions = {}              # sid -> {email, selected, visitor}
        self.acct_cookies = {}          # email -> account-specific cookie name
        self.visitors = {}              # vid -> Visitor, one per browser context
        self.ledger = []                # Claim, in claim order
        self.duplicate_claims = []      # (email, character) claimed again
        self.logins = []                # (email, password) as POSTed
        self.login_hits = []            # monotonic time of each GET /login
        self.login_page_cookies = []    # raw Cookie header on each GET /login
        self.vault_hits = []            # email or None per GET vault
        self.cross_account = []         # evidence of one account's cookie on another's request
        self.anonymous_claims = 0
        self.claim_posts = 0
        self.token_requests = 0
        self.callback_requests = 0
        self.cancelled_callbacks = 0
        self.callback_responses = []    # (time, email, character)
        self.session_fetches = 0
        self.session_responses = []     # (time, email)
        self.vault_documents = []       # (time, email), real documents only
        self.collect_requests = []      # (time, email)
        self.logouts = 0
        self.signouts = 0
        self.cancelled_selects = 0
        self.image_hits = 0
        self.font_hits = 0
        self.analytics_hits = 0
        self.total_requests = 0
        self.head_requests = []         # {path, headers, body}, apart from the login-page hits
        self.head_status = 200
        self.head_ms = 0
        self.head_hang = False

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.mock = self
        self.server.handle_error = lambda *a: None
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/en-us"

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()

    def account(self, email):
        return self.accounts.get(email, Account())

    def ledger_pairs(self):
        with self.lock:
            return [(c.email, c.character) for c in self.ledger]

    def peak_concurrency(self):
        """Most browser contexts active at once, first request to last request"""
        with self.lock:
            events = []
            for v in self.visitors.values():
                events += [(v.first, 1), (v.last, -1)]
        current = peak = 0
        for _, delta in sorted(events):
            current += delta
            peak = max(peak, current)
        return peak


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    @property
    def mock(self):
        return self.server.mock

    def cookies(self):
        found = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key:
                found[key] = value
        return found

    def send(self, status, body="", ctype="text/html", cookies=(), stall_ms=0):
        body = body.encode() if isinstance(body, str) else body
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for cookie in cookies:
                self.send_header("Set-Cookie", f"{cookie}; Path=/")
            self.end_headers()
            if stall_ms:
                self.wfile.flush()
                self.mock.stop.wait(stall_ms / 1000)
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, value, status=200, cookies=()):
        self.send(status, json.dumps(value), "application/json", cookies)

    def touch(self, path):
        """Record the request and return its session, or None"""
        mock = self.mock
        cookies = self.cookies()
        now = time.monotonic()
        with mock.lock:
            mock.total_requests += 1
            visitor = mock.visitors.get(cookies.get("vid"))
            if visitor and path != "/never.js":
                visitor.last = now
            session = mock.sessions.get(cookies.get("sid"))
            if session:
                own = mock.acct_cookies[session["email"]]
                foreign = sorted(k for k in cookies if k.startswith("acct_") and k != own)
                if foreign:
                    mock.cross_account.append((session["email"], foreign))
        return session

    def client_stayed(self, ms):
        """Wait ms, returning False if the browser cancelled the request meanwhile"""
        deadline = time.monotonic() + ms / 1000
        while (left := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select([self.connection], [], [], min(left, 0.05))
            if readable:
                try:
                    if not self.connection.recv(1, socket.MSG_PEEK):
                        return False
                except OSError:
                    return False
        return True

    def do_GET(self):
        mock = self.mock
        path = urlsplit(self.path).path
        session = self.touch(path)

        if path in mock.drop_paths:
            return self.drop()
        if path == "/api/auth/session":
            return self.api_session(session)
        if path.startswith("/api/extra/"):
            return self.send_json({})
        if path == "/never.js":
            mock.stop.wait(300)
            return self.send(200, "", "text/javascript")
        if path == "/poll":
            return self.send_json({})
        if path.startswith("/a/") or path == "/favicon.ico":
            with mock.lock:
                mock.image_hits += 1
            return self.send(200, PIXEL, "image/gif")
        if path.startswith("/f/"):
            with mock.lock:
                mock.font_hits += 1
            return self.send(200, b"x", "font/woff2")
        if path == "/en-us/login":
            return self.login_page()
        if path == "/en-us/catalog/main":
            return self.vault_page(session)
        if path == "/en-us/gated":
            page = GATED_PAGE.replace("__COMMON__", COMMON_JS).replace("__I18N__", I18N)
            return self.send(200, page)
        if path == "/api/me":
            if not session:
                return self.send_json({}, 401)
            return self.send_json(self.profile(session, reopen=True))
        return self.send(404, "nope")

    def do_HEAD(self):
        """The dashboard's site check. Not a page load, so never a login-page hit"""
        mock = self.mock
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with mock.lock:
            mock.head_requests.append({
                "path": urlsplit(self.path).path,
                "query": urlsplit(self.path).query,
                "headers": dict(self.headers.items()),
                "body": body,
            })
        while mock.head_hang and not mock.stop.is_set():
            mock.stop.wait(0.05)
        if mock.head_ms:
            mock.stop.wait(mock.head_ms / 1000)
        try:
            self.send_response(mock.head_status)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        mock = self.mock
        path = urlsplit(self.path).path
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        session = self.touch(path)

        if path in mock.drop_paths:
            return self.drop()
        if path == "/api/appcharge/get-token":
            return self.api_token(session)
        if path == "/api/auth/callback/credentials":
            return self.api_callback(session)
        if path == "/api/account/logout":
            with mock.lock:
                mock.logouts += 1
            return self.send_json({})
        if path == "/api/auth/signout":
            with mock.lock:
                mock.signouts += 1
            return self.send_json({})
        if path.startswith("/api/tunnel/analytics/"):
            with mock.lock:
                mock.analytics_hits += 1
            return self.send_json({})
        if path == "/api/account/login":
            return self.api_login(json.loads(raw))
        if path == "/api/account/select-profile":
            if not session:
                return self.send_json({}, 401)
            account = mock.account(session["email"])
            character = json.loads(raw)["character"]
            if character in account.select_never_answers:
                session["selected"] = character
                self.client_stayed(300000)
                return None
            if character in account.select_failures:
                if account.select_failure_status:
                    return self.send_json({}, account.select_failure_status)
                self.client_stayed(300000)
                return None
            if self.client_stayed(account.select_ms):
                session["selected"] = character
                return self.send_json({})
            with mock.lock:
                mock.cancelled_selects += 1
            return None
        if path == "/api/appcharge/collect-offer":
            return self.api_claim(session)
        return self.send(404, "nope")

    def reached(self, account, character):
        """Whether character is at or past the one the account freezes from"""
        names = list(account.characters)
        start = account.freeze_on_character
        return not start or character not in names or names.index(character) >= names.index(start)

    def dialog_freeze(self, account, session, reopen):
        if account.freeze_on != ("reopen" if reopen else "dialog"):
            return None
        names = list(account.characters)
        if reopen:
            selected = session["selected"]
            following = names[names.index(selected) + 1:] if selected in names else []
            if not following or not self.reached(account, following[0]):
                return None
        if account.freeze_delay_ms is not None:
            return account.freeze_delay_ms
        return 0 if reopen else 1000    # a reopened dialog is clicked within milliseconds

    def vault_freeze(self, account, session):
        if account.freeze_on != "vault" or not session or not self.reached(account, session["selected"]):
            return None
        return 50 if account.freeze_delay_ms is None else account.freeze_delay_ms

    def profile(self, session, reopen=False):
        account = self.mock.account(session["email"])
        return {
            "characters": list(account.characters),
            "selected": session["selected"],
            "dialog_ms": account.dialog_ms,
            "shell_ms": account.reopen_shell_ms if reopen else account.shell_ms,
            "card_gap_ms": account.reopen_card_gap_ms if reopen else account.card_gap_ms,
            "cookie": account.cookie,
            "selected_no_post": account.selected_no_post,
            "select_failure_keeps_dialog": account.select_failure_keeps_dialog,
            "select_never_answers": list(account.select_never_answers),
            "freeze_dialog_ms": self.dialog_freeze(account, session, reopen),
            "no_sign_in": account.no_sign_in,
            "sign_in_start_ms": account.sign_in_start_ms,
            "sign_in_before_dialog": account.sign_in_before_dialog,
            "poll_session_on_login": account.poll_session_on_login,
        }

    def login_page(self):
        mock = self.mock
        raw_cookie = self.headers.get("Cookie")
        cookies = self.cookies()
        new_cookies = []
        now = time.monotonic()
        with mock.lock:
            mock.login_hits.append(now)
            mock.login_page_cookies.append(raw_cookie)
            if "vid" not in cookies:
                vid = uuid.uuid4().hex
                mock.visitors[vid] = Visitor(first=now, last=now)
                new_cookies.append(f"vid={vid}")

        if mock.login_page_status != 200:
            return self.send(mock.login_page_status, "unavailable", cookies=new_cookies)

        page = (LOGIN_PAGE
                .replace("__COMMON__", COMMON_JS)
                .replace("__I18N__", I18N)
                .replace("__BANNER__", BANNERS[mock.banner])
                .replace("__EMAIL_ATTR__", mock.email_attr)
                .replace("__PW_ATTR__", mock.pw_attr)
                .replace("__HYDRATE_MS__", str(mock.hydrate_ms)))
        return self.send(200, page, cookies=new_cookies)

    def vault_page(self, session):
        mock = self.mock
        email = session["email"] if session else None
        with mock.lock:
            mock.vault_hits.append(email)
        account = mock.account(email)

        if session and account.vault == "hang":
            mock.stop.wait(300)
            return None
        if session:
            with mock.lock:
                session["vault_loads"] = session.get("vault_loads", 0) + 1
                load = session["vault_loads"]
            if load <= account.vault_abort_times or load in account.vault_abort_at:
                return self.send(204)
        with mock.lock:
            mock.vault_documents.append((time.monotonic(), email))
        if account.vault_delay_ms:
            mock.stop.wait(account.vault_delay_ms / 1000)

        mode = account.vault if session else "claim"
        with mock.lock:
            claimed = any((c.email, c.character) == (email, (session or {}).get("selected"))
                          for c in mock.ledger)
        selected = (session or {}).get("selected")
        if claimed or selected in account.cooldown_characters:
            mode = "cooldown"
        elif selected in account.unknown_characters:
            mode = "nothing"

        state = {
            "mode": mode,
            "button_ms": account.button_ms,
            "claim_hydrate_ms": account.claim_hydrate_ms,
            "replace_on_hydrate": account.replace_on_hydrate,
            "avatar_ms": account.avatar_ms,
            "confirm_ms": account.confirm_ms,
            "claim_no_request": account.claim_no_request,
            "cooldown_without_claim": account.cooldown_without_claim,
            "click_console": list(account.click_console),
            "click_throw": account.click_throw,
            "fail_delay_ms": account.fail_delay_ms,
            "freeze_after_click": account.freeze_after_click,
            "session_start_ms": account.session_start_ms,
            "freeze_vault_ms": self.vault_freeze(account, session),
            "replace_state_url": account.replace_state_url,
            "poll_session": bool(session) and session.get("vault_loads") == account.poll_session_on_vault_load,
            "click_analytics_calls": account.click_analytics_calls,
            "click_api_calls": account.click_api_calls,
            "click_urls": list(account.click_urls),
        }
        page = (VAULT_PAGE
                .replace("__COMMON__", COMMON_JS)
                .replace("__I18N__", I18N)
                .replace("__STATE__", json.dumps(state)))
        return self.send(200, page)

    def drop(self):
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def api_token(self, session):
        mock = self.mock
        with mock.lock:
            mock.token_requests += 1
        if not session:
            return self.send_json({}, 401)
        session["signed_in"] = False
        mock.stop.wait(mock.account(session["email"]).token_ms / 1000)
        return self.send_json({"token": "fake"})

    def api_callback(self, session):
        mock = self.mock
        with mock.lock:
            mock.callback_requests += 1
        if not session:
            return self.send_json({}, 401)

        account = mock.account(session["email"])
        wait_ms = 300000 if account.callback_ms < 0 else account.callback_ms
        if not self.client_stayed(wait_ms):
            with mock.lock:
                mock.cancelled_callbacks += 1
            return None

        with mock.lock:
            session["signed_in"] = account.callback_status == 200
            mock.callback_responses.append((time.monotonic(), session["email"], session["selected"]))
        return self.send_json({}, account.callback_status)

    def api_session(self, session):
        mock = self.mock
        email = session["email"] if session else None
        account = mock.account(email)
        with mock.lock:
            mock.session_fetches += 1
        if account.session_ms < 0:
            mock.stop.wait(300)
            return None
        mock.stop.wait(account.session_ms / 1000)
        if account.session_drop:
            return self.drop()

        body = account.session_body
        if body is None:
            body = json.dumps({"user": {"email": email}}) if session and session.get("signed_in") else "{}"
        with mock.lock:
            mock.session_responses.append((time.monotonic(), email))
        return self.send(200, body, "application/json")

    def api_login(self, body):
        mock = self.mock
        email, password = body["email"], body["password"]
        with mock.lock:
            mock.logins.append((email, password))
            attempt = sum(1 for used, _ in mock.logins if used == email)

        account = mock.account(email)
        if account.login_hang:
            mock.stop.wait(300)
            return None
        if account.login_delay_ms:
            mock.stop.wait(account.login_delay_ms / 1000)
        if attempt <= account.login_failures:
            return self.send_json({}, account.login_failure_status)
        if account.login_status != 200:
            return self.send_json({}, account.login_status)
        if not email or password != account.password:
            return self.send_json({}, 401)

        vid = self.cookies().get("vid")
        sid = uuid.uuid4().hex
        with mock.lock:
            visitor = mock.visitors.get(vid)
            if visitor:
                if visitor.email not in (None, email):
                    mock.cross_account.append((email, [f"context already used by {visitor.email}"]))
                visitor.email = email
            acct_cookie = mock.acct_cookies.setdefault(email, f"acct_{len(mock.acct_cookies)}")
            session = {
                "email": email,
                "selected": account.selected_character or (account.characters[0] if account.characters else "only"),
                "visitor": vid,
            }
            mock.sessions[sid] = session
        return self.send_json(self.profile(session), cookies=[f"sid={sid}", f"{acct_cookie}=1"])

    def api_claim(self, session):
        mock = self.mock
        if not session:
            with mock.lock:
                mock.anonymous_claims += 1
            return self.send_json({}, 401)

        account = mock.account(session["email"])
        with mock.lock:
            mock.claim_posts += 1
            mock.collect_requests.append((time.monotonic(), session["email"]))
        if account.collect_ms < 0:
            mock.stop.wait(300)
            return None
        if account.collect_ms:
            mock.stop.wait(account.collect_ms / 1000)

        status = 500 if session["selected"] in account.collect_fail_characters else account.collect_status
        if server_accepts(status, account.collect_body):
            with mock.lock:
                pair = (session["email"], session["selected"])
                if any((c.email, c.character) == pair for c in mock.ledger):
                    mock.duplicate_claims.append(pair)
                else:
                    mock.ledger.append(Claim(*pair, session["visitor"]))
        return self.send(status, account.collect_body, "application/json", stall_ms=account.collect_stall_ms)
