# Uploading to YouTube

`python -m renderfarm upload` (`renderfarm/youtube.py`) uploads a video with the YouTube Data API: standard library
only, resumable (64 MB pieces, retried), Private unless `--privacy` says otherwise, and the same file never twice (it
leaves `<file>.youtube.txt` with the link). It never prints a secret. On a pod, the job runs it after the checks when
the launcher was given `--upload`; the master goes from the pod straight to YouTube, which is faster than any home
connection (6.66 GB in 84 seconds) and keeps it off your disk.

## Credentials, once

1. In a Google Cloud project, enable the **YouTube Data API v3** and create an **OAuth client** of type *Desktop app*:
   its client id and client secret.
2. Get a **refresh token** for the channel, with the scope `https://www.googleapis.com/auth/youtube.upload`. One way:
   Google's OAuth 2.0 Playground (developers.google.com/oauthplayground), gear icon → "Use your own OAuth credentials"
   (add `https://developers.google.com/oauthplayground` to the client's redirect URIs if it asks), authorise the scope
   while signed in as the channel's owner, then "Exchange authorization code for tokens".
3. Keep the three values somewhere safe: as environment variables `YT_CLIENT_ID`, `YT_CLIENT_SECRET`,
   `YT_REFRESH_TOKEN` (or a file of `NAME=value` lines given with `--env-file`, kept out of any repository), and for
   cloud renders as RunPod secrets `yt_client_id`, `yt_client_secret`, `yt_refresh_token`.

`python -m renderfarm upload --check` signs in and names the channel, without uploading. `--profile NAME` reads
`YT_NAME_CLIENT_ID`, ... instead, for several channels side by side.

## Things Google decides

- While the OAuth consent screen is in *Testing*, refresh tokens expire after seven days. Publish the app (an internal
  tool can stay unverified for its owner) or expect to renew the token.
- Videos uploaded through an API project that has not passed Google's audit (projects created after 28 July 2020) are
  restricted to private viewing. Uploading as Private and publishing from YouTube Studio is this tool's default either
  way; check your project's status before you count on anything else.
- Each upload costs 1,600 units of the project's daily API quota (10,000 by default).
