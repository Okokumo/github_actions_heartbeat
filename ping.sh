#!/usr/bin/env bash
# Sends one heartbeat ping. Called by action.yml; inputs arrive as env vars
# (never ${{ }}-interpolated into a script, so no input can inject shell).
#
# Exit codes: 0 = ping delivered, or not delivered but fail-on-error is false.
#             1 = bad configuration (unknown status / fail-on-error value,
#                 malformed URL), or delivery failed with fail-on-error: true.
set -u

ping_url="${INPUT_PING_URL:-}"
status="${INPUT_STATUS:-success}"
fail_on_error="${INPUT_FAIL_ON_ERROR:-false}"

# Per-attempt limits. Wall time is bounded by retry-max-time plus one attempt
# (30 + 10 = 40 s); a hung endpoint gets 3 attempts and gives up after ~34 s.
connect_timeout=5
max_time=10
retries=3
retry_delay=2
retry_max_time=30

out() {
  # $1 = key, $2 = value. GITHUB_OUTPUT is unset when run outside Actions.
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"
  fi
}

out sent false
out http-code ""

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# For echoing a user-supplied value inside a workflow command: keeps a value
# with a line break or '::' from smuggling a second command into the log.
safe() { printf '%s' "$1" | tr -cd 'a-z0-9_.-' | cut -c1-32; }

# Strip surrounding whitespace (a secret pasted with a trailing newline is the
# most common way to break a ping URL).
trim() { printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }

status="$(lower "$(trim "$status")")"
fail_on_error="$(lower "$(trim "$fail_on_error")")"
ping_url="$(trim "$ping_url")"

case "$fail_on_error" in
  true|false) ;;
  *)
    echo "::error title=heartbeat-action::fail-on-error must be true or false, got '$(safe "$fail_on_error")'"
    exit 1
    ;;
esac

# success/fail/start are the documented values. failure/cancelled are what
# ${{ job.status }} produces, accepted so `status: ${{ job.status }}` works.
case "$status" in
  success|"") suffix="" ;;
  start) suffix="/start" ;;
  fail|failure|cancelled) suffix="/fail" ;;
  *)
    echo "::error title=heartbeat-action::status must be success, fail or start (or a job.status value), got '$(safe "$status")'"
    exit 1
    ;;
esac

# Not delivering a ping is reported, and only fails the step on request.
give_up() {
  if [ "$fail_on_error" = "true" ]; then
    echo "::error title=heartbeat-action::$1"
    exit 1
  fi
  echo "::warning title=heartbeat-action::$1 (the job result is unchanged; set fail-on-error: true to fail the step instead)"
  exit 0
}

if [ -z "$ping_url" ]; then
  give_up "ping-url is empty, no ping sent. Is the secret set for this repository?"
fi

# Checked before masking: ::add-mask:: is line-based, so a value with an
# embedded newline would leave its second line unmasked. The URL itself is
# never printed.
case "$ping_url" in
  *[[:space:]]*)
    echo "::error title=heartbeat-action::ping-url contains whitespace or a line break"
    exit 1
    ;;
  # /start and /fail are appended to the path; after a query string or
  # fragment they would not reach the check.
  *[?#]*)
    echo "::error title=heartbeat-action::ping-url must not contain a query string (?) or fragment (#)"
    exit 1
    ;;
esac
# Match the whole "scheme://" prefix: a value without "://" (e.g. a bare
# "https") must be rejected, not parsed as a scheme. The character right after
# "://" must not be a slash, so an empty host ("https:///", "https:///x") is a
# config error too, not a delivery failure.
case "$ping_url" in
  [Hh][Tt][Tt][Pp][Ss]://[!/]*) scheme=https ;;
  [Hh][Tt][Tt][Pp]://[!/]*) scheme=http ;;
  *)
    echo "::error title=heartbeat-action::ping-url must start with https:// (or http://)"
    exit 1
    ;;
esac

# Mask the URL in case it was passed as plain text rather than a secret: the
# token in it is the only credential needed to send pings for the check.
echo "::add-mask::$ping_url"

if [ "$scheme" = "http" ]; then
  echo "::warning title=heartbeat-action::ping-url uses http://, so the token travels unencrypted, and a server that redirects to https:// will not receive the ping (redirects are not followed). Use the https:// URL."
fi

if ! command -v curl >/dev/null 2>&1; then
  give_up "curl is not installed on this runner, no ping sent. Install curl before this step."
fi

# Tolerate a trailing slash on the stored URL (and mask the stripped form too).
if [ "${ping_url%/}" != "$ping_url" ]; then
  while [ "${ping_url%/}" != "$ping_url" ]; do ping_url="${ping_url%/}"; done
  echo "::add-mask::$ping_url"
fi

target="$ping_url$suffix"
label="${status:-success}"

# -f: an HTTP error status (>= 400) counts as a failed attempt. curl retries
# only transient failures (timeouts, connection refused, 408/429/5xx; the exact
# list depends on the curl version), so a 404 for a wrong token fails fast.
# No -L: a redirect is never followed, so the token cannot be carried to
# another host. A 3xx is therefore not a delivery (checked below).
# --globoff: {a,b} and [1-3] in the URL are literal characters, not a curl
# glob, so curl does not expand one ping-url into several hosts or requests.
# -q (must stay the FIRST argument, curl ignores it anywhere else): no curl
# config file is read (~/.curlrc, $CURL_HOME, $XDG_CONFIG_HOME), so a runner's
# config cannot add -L ("location"), a second URL ("url = ...") or other
# options to this request. Proxy env vars (HTTPS_PROXY, NO_PROXY) still apply.
# The http-code checks below match exactly three digits; anything else is
# not a delivery.
code="$(curl -q --globoff -sS -f -o /dev/null -w '%{http_code}' \
  --connect-timeout "$connect_timeout" -m "$max_time" \
  --retry "$retries" --retry-delay "$retry_delay" --retry-max-time "$retry_max_time" \
  --retry-connrefused \
  -A "okokumo-heartbeat-action/1" \
  "$target")"
rc=$?

out http-code "$code"

# Only a 2xx answer means the check received the ping.
if [ "$rc" -eq 0 ]; then
  case "$code" in
    2[0-9][0-9])
      out sent true
      echo "heartbeat-action: '$label' ping delivered (HTTP $code)"
      exit 0
      ;;
    3[0-9][0-9])
      give_up "'$label' ping not delivered: the server answered HTTP $code (a redirect, which is not followed). Use the exact https:// ping URL shown on the check."
      ;;
    *)
      give_up "'$label' ping not delivered: unexpected HTTP ${code:-none}."
      ;;
  esac
fi

case "$code" in
  404) hint=" The ping URL is not recognised: check the secret." ;;
  000|"") hint=" The endpoint was unreachable." ;;
  *) hint="" ;;
esac
give_up "'$label' ping not delivered (curl exit $rc, HTTP ${code:-none}).$hint"
