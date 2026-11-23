# Okokumo Heartbeat Ping

A GitHub Action that pings an [okokumo](https://okokumo.com/?mtm_campaign=gh-action) heartbeat or cron check from your workflow.

It is a thin wrapper around one `curl` call to your check's ping URL. There is no Docker image, no Node runtime and no dependency besides `curl`, which every GitHub-hosted runner has. You could write the same `curl` line yourself. What the action adds is retries, a time limit, and a warning instead of a red job when a ping does not get through. It runs on Linux, macOS and Windows runners.

Why bother: a scheduled workflow can stop running without a failed run anywhere. GitHub disables schedules in public repositories after 60 days without activity, and it can delay or drop scheduled runs under load. A heartbeat check expects the ping on a schedule and alerts you when it does not arrive. The guide [GitHub Actions scheduled workflows fail silently](https://okokumo.com/blog/github-actions-scheduled-workflows-fail-silently?mtm_campaign=gh-action) covers these failure modes and how we tested the pattern.

## Setup

1. Create a `cron` check in okokumo with the same cron expression as your workflow (UTC, unless you set `timezone:` in the workflow) and a grace period. Copy its ping URL. [Free account](https://app.okokumo.com/signup?c=gh-action&mtm_campaign=gh-action), 5 checks included.
2. Add the URL as a repository secret, for example `OKOKUMO_PING_URL`. Anyone who has the URL can send pings for that check, so do not commit it.
3. Add one of the snippets below.

## Usage

### 1. Ping when a scheduled job succeeds

```yaml
name: nightly-backup
on:
  schedule:
    - cron: "17 3 * * *"   # off the hour: GitHub names the top of the hour as peak load
  workflow_dispatch:        # lets you run it by hand to test the ping

jobs:
  backup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - run: ./scripts/backup.sh
      - uses: Okokumo/github_actions_heartbeat@v1
        with:
          ping-url: ${{ secrets.OKOKUMO_PING_URL }}
```

The last step only runs when the job got that far. A failed job, a disabled schedule or a dropped run all mean no ping, and the check alerts you when the deadline passes.

### 2. Report a failure immediately

```yaml
    steps:
      - uses: Okokumo/github_actions_heartbeat@v1
        with:
          ping-url: ${{ secrets.OKOKUMO_PING_URL }}
          status: start          # optional: records when the run began
      - uses: actions/checkout@v7
      - run: ./scripts/backup.sh
      - uses: Okokumo/github_actions_heartbeat@v1
        with:
          ping-url: ${{ secrets.OKOKUMO_PING_URL }}
      - uses: Okokumo/github_actions_heartbeat@v1
        if: failure()
        with:
          ping-url: ${{ secrets.OKOKUMO_PING_URL }}
          status: fail
```

`/fail` marks the check down at once, so you hear about the failure now instead of at the deadline.

### 3. Catch a hung job with a timeout

`if: failure()` does not run when a job is cancelled, and a job that hits `timeout-minutes` is cancelled. To report timeouts as well, send the result from one `if: always()` step:

```yaml
jobs:
  backup:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: Okokumo/github_actions_heartbeat@v1
        with:
          ping-url: ${{ secrets.OKOKUMO_PING_URL }}
          status: start
      - uses: actions/checkout@v7
      - run: ./scripts/backup.sh
      - uses: Okokumo/github_actions_heartbeat@v1
        if: always()
        with:
          ping-url: ${{ secrets.OKOKUMO_PING_URL }}
          status: ${{ job.status }}   # success -> ping, failure/cancelled -> /fail
```

A run that never starts at all (disabled schedule, dropped run) cannot send anything. The check's deadline is what catches those.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `ping-url` | (required) | The check's ping URL, from a secret. |
| `status` | `success` | `success` pings the URL, `fail` pings `/fail`, `start` pings `/start`. The `job.status` values `failure` and `cancelled` send `/fail`. |
| `fail-on-error` | `false` | Fail the step if the ping cannot be delivered. |

## Outputs

| Output | Description |
| --- | --- |
| `sent` | `true` if the endpoint accepted the ping. |
| `http-code` | HTTP status of the last attempt. `000` if the request got no response (unreachable, timed out); empty if no request was made (empty `ping-url`, no `curl`). |

## Behaviour

- Each attempt is limited to 10 seconds. Timeouts, refused connections and HTTP 408, 429, 500, 502, 503 and 504 are retried, about 2 seconds apart (the exact list of retried codes depends on the runner's `curl` version). A hung endpoint gets 3 attempts and the step gives up after about 34 seconds (40 seconds at most).
- A 404 (unknown ping URL) is not retried.
- Only a 2xx answer counts as delivered. Redirects are not followed, so the token is never sent to another host; a 3xx answer is reported as not delivered. Use the exact `https://` ping URL shown on the check. An `http://` URL works but logs a warning: the token travels unencrypted, and a server that redirects to `https://` will not get the ping.
- One `ping-url` is exactly one request: `{}` and `[]` in the URL are sent literally (no curl URL globbing), and a curl config file on the runner (`~/.curlrc`) is ignored, so it cannot turn on redirects or add requests. The runner's proxy environment variables (`HTTPS_PROXY`, `NO_PROXY`) are still used.
- By default a ping that cannot be delivered logs a warning and leaves the job result unchanged. A network blip on the way to the monitor should not turn a good run into a red X. If the success ping is lost, the worst case is a false alert at the deadline, never a missed failure. Set `fail-on-error: true` if you prefer the step to fail.
- A configuration mistake (an unknown `status` or `fail-on-error` value, a URL that does not start with `https://` or `http://`, or a URL with a `?query` or `#fragment`, which `/start` and `/fail` cannot be appended to) always fails the step. An empty `ping-url`, for example a missing secret, or a runner without `curl`, only warns, unless `fail-on-error` is true.
- Pass the URL as a secret. GitHub masks secrets everywhere in the log. A URL passed as plain text is printed in the step's `with:` header before the action runs, so the action cannot hide it there. It only masks the URL in its own output.

The `/start` and `/fail` suffixes follow the same convention as Healthchecks.io, so the action may work with other services that use it. We have only tested it against okokumo.

## License

MIT
