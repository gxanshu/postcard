# Security Policy

## Supported versions

Only the **latest release** receives security fixes.
Postcard is a young project with no LTS, so older releases are not patched.
The Flatpak updates itself `flatpak update` (or GNOME Software) is all it takes to be current.

## Reporting a vulnerability

Security issues go through the same channel as every other bug:
[the issue tracker](https://github.com/gxanshu/postcard/issues).

To get the fastest response, include what a good bug report always includes:

- Postcard version (`flatpak info in.gxanshu.postcard`) and your distro
- Mail provider and account type, if relevant
- Steps to reproduce, and logs if you have them (`POSTCARD_LOG=debug`)

**Never include passwords, OAuth tokens, or the contents of real messages** in a report there's no need, and the tracker is public.

Postcard is maintained by one person, so there's no SLA. you'll get an acknowledgement within a few days, and fixes land in the next release. Reports that turn out to be non-issues are still welcome the app is young, and a clearly-worded "is this safe?" beats a silently-exploitable edge case.
