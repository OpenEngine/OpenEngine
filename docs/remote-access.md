# Phone Safari access over Tailscale

Open the full HTTPS hostname configured in `engine.toml`'s `public_url`,
with no development port appended. Connect the phone to the same tailnet
using Tailscale first. Being on the Mac's Wi-Fi alone does not provide
Tailscale routing or DNS.

## Verified diagnosis for the Mac mini

Host checks on September 11, 2026 (September 12 UTC) found:

| Check | Result |
| --- | --- |
| `tailscale serve status` | Port 443 is tailnet-only and proxies `/` to `http://localhost:5173`. |
| Public Funnel | Only port 8443, path `/api/slack/events`, is exposed. It does not publish the UI on port 443. |
| System DNS for `sheas-mac-mini.taileb7fdb.ts.net` | `100.74.102.122`, the tailnet address. |
| Public DNS (`1.1.1.1`) for the same name | `208.111.34.11` and `208.111.35.209`. These are observations, not addresses to hardcode. |
| HTTPS through system DNS | TLS 1.3, certificate verification successful, HTTP/2 200. |
| HTTPS forced to public address `208.111.34.11` on port 443 | TCP connected, but TLS failed with `SSL_ERROR_SYSCALL` before a certificate was received. |

The working endpoint presented a Let's Encrypt YE1 certificate whose subject
alternative name matched the full hostname, valid August 28–November 26, 2026.
There was no expired, self-signed, or mismatched certificate on that path.
Certificate dates and DNS answers must be checked again for future incidents.

This explains the desktop workaround: a Firefox DNS-over-HTTPS exception
restores the system resolver for the name, allowing Tailscale DNS to select
the private endpoint. It does not change certificate trust. Mozilla describes
this behavior in its [DNS-over-HTTPS settings guide](https://support.mozilla.org/en-US/kb/dns-over-https).
With Funnel enabled for another port, public DNS can resolve the hostname
without providing the private UI's service on port 443. The observed public
route reproduces the recalled handshake error.

The phone's settings and exact Safari error were not available for inspection.
DNS or routing bypass is the leading explanation, not a confirmed observation
of the phone. A disconnected Tailscale VPN can also prevent access.

## Fix on the phone

1. Connect Tailscale to the same tailnet as the Mac mini. Ensure the Mac is
   online, the phone accepts Tailscale DNS settings, and tailnet access policy
   permits the phone to reach the Mac on TCP 443.
2. Open `https://sheas-mac-mini.taileb7fdb.ts.net/` in Safari. Use the full
   hostname: an IP address or short machine name will not match its certificate.
3. If Safari still fails while Tailscale is connected, test Private Relay's
   per-site bypass: Safari's Page Menu → **Show IP Address**, if available.
   Otherwise temporarily turn off **Limit IP Address Tracking** for the active
   Wi-Fi or cellular network and reload. Apple's
   [Private Relay troubleshooting instructions](https://support.apple.com/en-us/102022)
   describe both controls. This changes privacy protection for the selected
   site or network; restore it if the test does not help. Tailscale lists
   Private Relay among [potential interoperability conflicts](https://tailscale.com/docs/reference/interoperability).
4. If needed, check other VPNs, encrypted-DNS profiles, or DNS-filtering apps
   for overrides of Tailscale's resolver. Have them use system/Tailscale DNS
   for this hostname or tailnet suffix, then reconnect Tailscale and retry.
   Do not replace the phone's DNS with a public resolver to fix this incident.

Success means Safari loads the UI without a certificate warning and can open
a conversation. Repeat on cellular with Tailscale connected to verify remote
access independently of local Wi-Fi. If it still fails, record the exact Safari
error, iOS/Tailscale versions, active network, and whether the Private Relay
test changed the result.

## Separate DNS, transport, and certificate failures

Run these read-only checks on a connected Mac. The system resolver matters:
`dig` alone does not exercise macOS's scoped DNS resolution.

```sh
tailscale serve status
dscacheutil -q host -a name sheas-mac-mini.taileb7fdb.ts.net
dig @1.1.1.1 sheas-mac-mini.taileb7fdb.ts.net A +noall +answer
curl -Iv --connect-timeout 5 --max-time 10 https://sheas-mac-mini.taileb7fdb.ts.net/
```

Compare routes without changing DNS or disabling certificate validation:

```sh
# Substitute the current Mac tailnet IPv4 address, then a current public answer.
curl -Iv --connect-timeout 5 --max-time 10 \
  --resolve sheas-mac-mini.taileb7fdb.ts.net:443:100.74.102.122 \
  https://sheas-mac-mini.taileb7fdb.ts.net/
```

`--resolve` preserves the URL hostname for TLS SNI and certificate checks.
A private-route success and public-route handshake failure isolate endpoint
selection. A timeout on the private route calls for VPN, access-policy, and
host availability checks. A certificate verification error after receiving a
certificate calls for checking its hostname, validity, chain, and device clock.
An HTTP 502 means TLS completed but the local upstream needs attention.

## Hosting configuration

`public_url` supplies notification links; it does not configure DNS, TLS, or
public exposure. The Python entrypoint (`apps/web/src/engine/apps/web/__main__.py`)
starts Uvicorn over HTTP. Development Vite already allows `.ts.net` hosts in
`apps/web/vite.config.ts`. Tailscale Serve terminates HTTPS before forwarding
to that HTTP service; see [Serve configuration](https://tailscale.com/docs/reference/tailscale-cli/serve).

The verified host configuration needs no change for phone access. For a new
host, enable MagicDNS and HTTPS certificates in the tailnet, start Engine, and
configure Serve to proxy to the actual listening HTTP port: normally 5173 for
`engine-dev`, or 8000 for `engine-web`. Inspect existing Serve/Funnel mappings
before changing them, especially webhook routes.

Do not install a custom root certificate, bypass TLS validation, or enable
Funnel for the whole UI to resolve this DNS issue. Tailscale Serve manages the
hostname's HTTPS certificate; manually exported certificates instead need
their own renewal handling, as described in
[Tailscale's HTTPS guide](https://tailscale.com/docs/how-to/set-up-https-certificates).
