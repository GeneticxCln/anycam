# Security

## Reporting

Open a private security advisory on the GitHub repository (Security tab, "Report a vulnerability"), or email
the maintainer listed on the repository profile. Please include the version (`anycam --version`), what you did
and what you saw. There is no bounty; fixes are released as patch versions and credited in `CHANGELOG.md`.

## What AnyCam does with the network

- Nothing leaves the machine unless you ask. There is no telemetry, account or update check.
- The only internet request is the optional model download (Settings → AI effects), pinned to a SHA-256.
- The **phone link** is off until you add a phone. Then it serves HTTPS on your LAN address only:
  - pairing uses a single-use QR token (five minutes) carried in the URL fragment, so it is never logged;
  - each phone gets its own secret, stored hashed; every request is authenticated per device;
  - the certificate is self-signed and remade when the LAN address changes; the fingerprint is shown in the
    pairing dialog so you can compare it;
  - concurrent connections are capped.
- Streaming (RTMP) credentials live in the project file, which is written owner-only (`0600`); error texts
  and reports mask stream keys and camera passwords.

## Privileged actions

Only two, both through `pkexec` and the normal password prompt: loading `v4l2loopback` (virtual camera setup)
and, if you click **Allow phones**, one ufw or firewalld rule for TCP 8443.
