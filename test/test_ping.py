#!/usr/bin/env python3
"""Local tests for ping.sh against a throwaway HTTP server.

Run: python3 test/test_ping.py   (needs bash + curl; no network access)

Each case runs ping.sh with only the env vars action.yml passes it, plus a
GITHUB_OUTPUT file, and asserts on what the server actually received - not
only on the exit code.
"""
import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "ping.sh")
TOKEN = "0123456789abcdef0123456789abcdef"
BASH = shutil.which("bash") or "bash"

hits = []  # (method, path, user-agent)
flaky_left = {}
lock = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        with lock:
            hits.append(("GET", self.path, self.headers.get("User-Agent", "")))
        parts = self.path.strip("/").split("/")
        mode = parts[0]
        if mode == "ok":
            code = 200
        elif mode == "404":
            code = 404
        elif mode == "503":
            code = 503
        elif mode == "flaky":
            key = parts[1]
            with lock:
                n = flaky_left.setdefault(key, int(parts[2]))
                flaky_left[key] = n - 1
            code = 503 if n > 0 else 200
        elif mode in ("301", "302", "307", "308"):
            # Redirect to a path that would answer 200: the action must not
            # follow it (the token would travel to wherever Location points).
            code = int(mode)
            body = b"moved"
            try:
                self.send_response(code)
                self.send_header("Location", "/ok/" + "/".join(parts[1:]))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        elif mode == "204":
            code = 204
        elif mode == "slow":
            time.sleep(60)
            code = 200
        else:
            code = 500
        body = b"" if code == 204 else (b"OK" if code == 200 else b"no")
        try:
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


srv = Server(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run(url, status=None, foe=None, path=None, extra_env=None):
    with lock:
        hits.clear()
    fd, outfile = tempfile.mkstemp()
    os.close(fd)
    env = {"PATH": path or os.environ["PATH"], "GITHUB_OUTPUT": outfile, "INPUT_PING_URL": url}
    env.update(extra_env or {})
    if status is not None:
        env["INPUT_STATUS"] = status
    if foe is not None:
        env["INPUT_FAIL_ON_ERROR"] = foe
    t0 = time.monotonic()
    p = subprocess.run([BASH, SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    elapsed = time.monotonic() - t0
    outputs = {}
    with open(outfile) as f:
        for line in f:
            k, _, v = line.rstrip("\n").partition("=")
            outputs[k] = v  # last write wins, as in Actions
    os.unlink(outfile)
    with lock:
        got = list(hits)
    return p.returncode, p.stdout + p.stderr, outputs, got, elapsed


failures = []


def check(name, cond, detail: object = ""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        failures.append(name)


def warned(log, needle=""):
    # A give-up warning, not the advisory one every http:// URL triggers
    # (the local test server is plain http).
    return any(l.startswith("::warning") and "uses http://" not in l and needle in l
               for l in log.splitlines())


def paths(got):
    return [h[1] for h in got]


# --- signals -------------------------------------------------------------
for status, suffix in [
    (None, ""), ("success", ""), ("start", "/start"), ("fail", "/fail"),
    ("failure", "/fail"), ("cancelled", "/fail"), ("  Success \n", ""), ("FAIL", "/fail"),
]:
    rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}", status)
    check(f"status={status!r} -> /ok/TOKEN{suffix}",
          rc == 0 and paths(got) == [f"/ok/{TOKEN}{suffix}"] and out.get("sent") == "true"
          and out.get("http-code") == "200",
          (rc, paths(got), out, log))

rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}/", "start")
check("trailing slash on URL is tolerated", paths(got) == [f"/ok/{TOKEN}/start"], paths(got))
check("stripped URL is masked too", f"::add-mask::{BASE}/ok/{TOKEN}" in log.splitlines(), log)
rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}//", "fail")
check("several trailing slashes: all stripped, stripped URL masked",
      paths(got) == [f"/ok/{TOKEN}/fail"] and f"::add-mask::{BASE}/ok/{TOKEN}" in log.splitlines(),
      (paths(got), log))

rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}")
check("user agent identifies the action", got and got[0][2] == "okokumo-heartbeat-action/1", got)

# --- secret hygiene ------------------------------------------------------
rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}")
lines = log.splitlines()
check("URL is masked before anything else is printed",
      lines and lines[0] == f"::add-mask::{BASE}/ok/{TOKEN}", lines[:2])
check("token appears in the log only inside the add-mask command",
      sum(TOKEN in l for l in lines) == 1, log)

rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}\nhttps://evil.example/x")
check("URL with a line break is rejected, nothing sent, never echoed",
      rc == 1 and got == [] and TOKEN not in log and "::add-mask::" not in log, (rc, got, log))

rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}", "x\n::add-mask::injected")
check("bad status cannot inject a workflow command",
      rc == 1 and got == [] and not any(l.startswith("::add-mask::") for l in log.splitlines()),
      log)

# --- configuration errors fail the step regardless of fail-on-error ------
rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}", "done")
check("unknown status fails the step, no ping sent", rc == 1 and got == [] and "::error" in log, (rc, log))
rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}", None, "yes")
check("fail-on-error must be true/false", rc == 1 and got == [] and "::error" in log, (rc, log))
rc, log, out, got, _ = run(f"ftp://127.0.0.1/{TOKEN}")
check("non-http URL rejected", rc == 1 and got == [], (rc, log))
# A bare scheme with no "://" is a config error, not a delivery failure.
for u in ("https", "http", "HTTPS", "HTTP", "https:", "https:/x", "https://",
          f"https//127.0.0.1/{TOKEN}", "https:///", "https:///x", "http:///",
          f"https:///127.0.0.1/{TOKEN}"):
    rc, log, out, got, _ = run(u, None, "false")
    check(f"malformed URL {u!r} fails the step even without fail-on-error",
          rc == 1 and got == [] and any(l.startswith("::error") and "must start with https://" in l
                                        for l in log.splitlines()), (rc, got, log))

# --- empty URL: warn by default, fail on request -------------------------
rc, log, out, got, _ = run("")
check("empty URL warns, job result unchanged", rc == 0 and warned(log) and out.get("sent") == "false", (rc, log))
rc, log, out, got, _ = run("", None, "true")
check("empty URL + fail-on-error fails", rc == 1 and "::error" in log, (rc, log))

# --- retries -------------------------------------------------------------
rc, log, out, got, _ = run(f"{BASE}/flaky/a/2/{TOKEN}")
check("2x 503 then 200: delivered on the 3rd attempt",
      rc == 0 and len(got) == 3 and out.get("sent") == "true", (rc, len(got), out, log))

rc, log, out, got, el = run(f"{BASE}/503/{TOKEN}")
check("persistent 503: 1 try + 3 retries, then warning, exit 0",
      rc == 0 and len(got) == 4 and warned(log) and out.get("sent") == "false"
      and out.get("http-code") == "503", (rc, len(got), out, log))

rc, log, out, got, _ = run(f"{BASE}/404/{TOKEN}")
check("404 is not retried, warns with a hint, exit 0",
      rc == 0 and len(got) == 1 and "not recognised" in log and out.get("http-code") == "404",
      (rc, len(got), log))
rc, log, out, got, _ = run(f"{BASE}/404/{TOKEN}", None, "true")
check("404 + fail-on-error fails the step", rc == 1 and "::error" in log, (rc, log))

# --- only 2xx counts as delivered; redirects are never followed ---------
for code in ("301", "302", "307", "308"):
    rc, log, out, got, _ = run(f"{BASE}/{code}/{TOKEN}")
    check(f"HTTP {code}: not delivered, not followed, warning, exit 0",
          rc == 0 and paths(got) == [f"/{code}/{TOKEN}"] and out.get("sent") == "false"
          and out.get("http-code") == code and warned(log, f"HTTP {code} (a redirect"),
          (rc, paths(got), out, log))
rc, log, out, got, _ = run(f"{BASE}/301/{TOKEN}", "fail", "true")
check("HTTP 301 + fail-on-error fails the step (coordinator repro)",
      rc == 1 and "::error" in log and out.get("sent") == "false"
      and paths(got) == [f"/301/{TOKEN}/fail"], (rc, paths(got), out, log))

rc, log, out, got, _ = run(f"{BASE}/204/{TOKEN}")
check("HTTP 204 counts as delivered",
      rc == 0 and out.get("sent") == "true" and out.get("http-code") == "204", (rc, out, log))

rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}")
check("http:// URL warns that the token travels unencrypted, still delivered",
      rc == 0 and out.get("sent") == "true"
      and any(l.startswith("::warning") and "http://" in l for l in log.splitlines()), (rc, out, log))
rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}".replace("http://", "HTTP://"))
check("uppercase HTTP:// scheme also warns",
      any(l.startswith("::warning") and "http://" in l for l in log.splitlines()), log)
rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}".replace("http://", "hTtP://"))
check("mixed-case hTtP:// is accepted and delivered",
      rc == 0 and out.get("sent") == "true" and paths(got) == [f"/ok/{TOKEN}"], (rc, out, log))

# --- URL with a query or fragment: /fail would land after it -------------
for u in (f"{BASE}/ok/{TOKEN}?x=1", f"{BASE}/ok/{TOKEN}#frag"):
    rc, log, out, got, _ = run(u, "fail")
    check(f"URL with ?/# rejected, nothing sent ({u[-6:]})",
          rc == 1 and got == [] and "::error" in log and TOKEN not in log, (rc, got, log))

# --- curl URL globbing is off: {a,b} / [1-3] never fan a ping out --------
# Without -g, "https://{a,b}.example/TOKEN" would send the token to every
# host in the set, and "[1-3]" would send several pings.
for u, label in ((f"{BASE}/ok/{{a,b}}/{TOKEN}", "{a,b} in path"),
                 (f"{BASE}/ok/[1-3]/{TOKEN}", "[1-3] in path")):
    rc, log, out, got, _ = run(u)
    check(f"glob {label}: one literal request, not a fan-out",
          len(got) == 1 and TOKEN in got[0][1], (rc, paths(got), log))
rc, log, out, got, _ = run(f"http://{{127.0.0.1,localhost}}:{PORT}/ok/{TOKEN}")
check("glob {a,b} in host: token is not sent to either host",
      got == [] and out.get("sent") == "false", (rc, paths(got), out, log))

# --- a curl config file on the runner is ignored (curl -q) -------------
# A ~/.curlrc (or a $CURL_HOME / $XDG_CONFIG_HOME one) holding "location"
# would follow the redirect and carry the token to the Location target;
# "url = " would add a second request. Self-hosted runners can have either.
rcdir = tempfile.mkdtemp()
os.makedirs(os.path.join(rcdir, "curl"))
curlrc = f'location\nurl = "{BASE}/leak"\n'
for rcfile in (os.path.join(rcdir, ".curlrc"), os.path.join(rcdir, "curl", "curlrc")):
    with open(rcfile, "w") as f:
        f.write(curlrc)
rc, log, out, got, _ = run(f"{BASE}/302/{TOKEN}",
                           extra_env={"HOME": rcdir, "CURL_HOME": rcdir, "XDG_CONFIG_HOME": rcdir})
check(".curlrc ignored: 302 not followed, no extra URL, not delivered",
      paths(got) == [f"/302/{TOKEN}"] and out.get("sent") == "false"
      and out.get("http-code") == "302",
      (rc, paths(got), out, log))
shutil.rmtree(rcdir)

# --- curl missing ---------------------------------------------------------
nocurl = tempfile.mkdtemp()
for tool in ("tr", "sed", "cut"):
    os.symlink(shutil.which(tool), os.path.join(nocurl, tool))
rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}", path=nocurl)
check("no curl on PATH: clear warning, exit 0",
      rc == 0 and warned(log, "curl is not installed") and out.get("sent") == "false", (rc, log))
rc, log, out, got, _ = run(f"{BASE}/ok/{TOKEN}", None, "true", path=nocurl)
check("no curl on PATH + fail-on-error: exit 1",
      rc == 1 and any(l.startswith("::error") and "curl is not installed" in l for l in log.splitlines()), (rc, log))
shutil.rmtree(nocurl)

# --- unreachable / slow endpoints ---------------------------------------
dead = f"http://127.0.0.1:{closed_port()}/ping/{TOKEN}"
rc, log, out, got, el = run(dead)
check("connection refused: warning, exit 0", rc == 0 and "unreachable" in log and out.get("http-code") == "000", (rc, out, log))
rc, log, out, got, el = run(dead, "fail", "true")
check("connection refused + fail-on-error: exit 1", rc == 1 and "::error" in log, (rc, log))
for scheme in ("HTTPS", "hTtPs"):
    rc, log, out, got, el = run(f"{scheme}://127.0.0.1:{closed_port()}/ping/{TOKEN}")
    check(f"{scheme}:// is a valid scheme (delivery failure, not config error)",
          rc == 0 and "unreachable" in log and "must start with" not in log, (rc, out, log))

if os.environ.get("SKIP_SLOW") != "1":
    rc, log, out, got, el = run(f"{BASE}/slow/{TOKEN}")
    check(f"hung endpoint: 3 attempts, gives up in ~34 s (took {el:.1f}s), exit 0",
          rc == 0 and el < 40 and len(got) == 3 and warned(log), (rc, el, len(got), log))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all passed")
