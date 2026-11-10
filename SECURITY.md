# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private
reporting: open the repository's **Security** tab and choose **Report a vulnerability**.
Include what you found, how to reproduce it and the version or commit you tested.

## Scope

Modera accepts untrusted text, image uploads and image URLs, and guards an API key.
These are in scope:

- bypassing authentication or the request, size and time limits
- fetching from a host that is not on the allowlist, or reaching a private network
  through URL input
- loading code or data other than the pinned model files
- leaking submitted content or credentials into logs, metrics or errors

Moderation accuracy is not a security issue. A model missing or wrongly flagging
content belongs in a regular issue.

## Supported versions

There are no releases yet. Fixes are made on `main`.
