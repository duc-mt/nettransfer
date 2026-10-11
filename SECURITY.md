# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private reporting instead:
**Security tab > Report a vulnerability** (<https://github.com/duc-mt/nettransfer/security/advisories/new>).

Include the version (`nettransfer-cli --version`), your OS, what you observed and, if you can, a minimal way to
reproduce it. You can expect an acknowledgement within about a week; this is a volunteer-maintained project, so
fix timelines depend on severity and complexity. Please allow time for a fix before disclosing publicly. Reporters
are credited in the release notes unless they ask not to be.

## Supported versions

Only the latest release receives security fixes.

| Version | Supported |
|---|---|
| latest `2.x` | yes |
| older | no |

## What this tool is - and is not

Network File Transfer is an **operator's utility** for moving files to and from network devices on a network you
control. Some of its protocols are unauthenticated or unencrypted *by design*, because the devices speak them:

* **TFTP, HTTP and FTP carry no encryption and TFTP/HTTP no authentication.** Anyone who can reach the server's port can
  fetch the selected file while it runs. Use the Safety options: *client allowlist*, *stop after the first transfer*,
  *idle auto-stop*; run it on a management VLAN, and only for as long as needed. Do not serve secrets.
* **SSH host keys are trusted on first use.** A *changed* key is refused until you tick *Accept changed host key*.
  The first connection to a new device is therefore not authenticated; verify the fingerprint out of band for
  high-value devices (`nettransfer-cli test scp <host>` prints it without logging in).
* **FTPS and HTTPS use certificates that are not verified** (devices use self-signed ones). The log says so.
* **Batch CSV files may hold plaintext passwords** (the app warns). Prefer SSH keys or the shared default credentials.
* Settings, `known_hosts`, logs and `crash.log` live in a per-user data folder (*Options > Open data folder*). They never
  contain passwords or key passphrases; `known_hosts` is created with mode `0600` on POSIX.

## In scope

Flaws in this code that let a remote party read files other than the one being served, write or overwrite files on the
machine running the server, bypass the allowlist, run code, or recover credentials from logs or settings. Also: unsafe
handling of host keys, or a CI/release-pipeline weakness that could let an unauthorised build be published.

## Out of scope

The inherent lack of encryption/authentication of TFTP/HTTP/FTP; a malicious *device* the operator chose to push to;
findings that need an attacker who already controls the operator's machine or account; vulnerabilities in third-party
libraries without a way they are reachable here (report those upstream; Dependabot and pip-audit watch them).

## Verifying a download

Each release lists `SHA256SUMS.txt` and carries GitHub build-provenance attestations:

```bash
sha256sum -c SHA256SUMS.txt --ignore-missing
gh attestation verify NetTransfer-<version>-linux-x86_64.tar.gz --repo duc-mt/nettransfer
```
