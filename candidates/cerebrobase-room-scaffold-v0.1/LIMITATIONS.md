# Known limitations / not claimed

## Out of scope by design

* No real login, real users, invitations, emails or credentials. The SYNTHETIC adapter serves two fixtures only.
* No production identity system. PROD is refused until a qualified EXTERNAL AuthProvider and deployment
  qualification exist.
* No private file storage, upload, download, quota enforcement, encryption, admin-unreadability or recovery. These are
  CB-P02; only reserved tables and interfaces exist.
* No Stambok/Dagbok mirror. That is CB-P03; only reserved tables and interfaces exist.
* No Postkasse integration: no Postkasse mutation and no use of its DB.
* No external tool is operative. `postkasse.lese`, `signalvev.status` and `drive.navigasjon` are UNQUALIFIED until
  the owner supplies an attested port.
* No live host mutation, SSH, DNS, ACL, Caddy or service install. EDGE install is UNRUN.

## Product limits

* The bundled `http.server` loopback server is a preview and staging server. It is not a publicly qualified
  production server; public exposure must go through the existing EDGE Caddy after qualification.
* The rate limiter and concurrency cap are in-process and per instance, not distributed.
* The audit log is in the room DB. There is no export or retention policy yet.

## Not verified in this run

* Windows run/stop commands are written but not executed; only Linux CPython 3.13.15 was run. Python 3.12 was not
  run.
* The visual check covers four pages at 320 px and 1280 px in Chromium (Playwright) only. No screen-reader audit was
  run.
