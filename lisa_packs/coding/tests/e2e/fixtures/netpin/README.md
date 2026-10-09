# netpin

Pin devices to addresses on a dnsmasq-managed home or lab network.

dnsmasq hands out DHCP leases and writes them to its lease file. netpin
reads that file to show what is on the network, and lets you *pin* a device —
reserve its IPv4 address (and hostname) by MAC — by publishing a
`dhcp-host=` line per reservation into a `--dhcp-hostsdir` directory that
dnsmasq watches. No dnsmasq restart is needed.

## Run

```sh
NETPIN_TOKEN=secret python3 -m netpin.cli \
  --db netpin.db \
  --leases /var/lib/misc/dnsmasq.leases \
  --hostdir /etc/dnsmasq.hosts.d \
  --subnet 192.168.1.0/24
```

## API

Every route except `GET /health` requires `Authorization: Bearer <token>`.

| Method | Path | Body | Success |
| --- | --- | --- | --- |
| GET | `/health` | — | 200 |
| GET | `/leases` | — | 200, list of leases |
| GET | `/leases/<mac>` | — | 200, leases for the MAC |
| GET | `/reservations` | — | 200, list of reservations |
| POST | `/reservations` | `{"mac", "ip", "hostname"?, "note"?}` | 201, the reservation |
| GET | `/reservations/<mac>` | — | 200, the reservation |
| PUT | `/reservations/<mac>` | `{"ip", "hostname"?, "note"?}` | 200, the reservation |
| DELETE | `/reservations/<mac>` | — | 204 |

Errors are `{"error": "<code>", "detail": "<text>"}`:

| Code | Status | When |
| --- | --- | --- |
| `bad_request` | 400 | body is not a JSON object |
| `unauthorized` | 401 | missing or wrong token |
| `not_found` | 404 | unknown route, MAC, or lease |
| `method_not_allowed` | 405 | known path, wrong method |
| `already_reserved` | 409 | POST for a MAC that already has a reservation |
| `invalid` | 422 | malformed MAC/IP/hostname, or IP outside `--subnet` |
| `too_large` | 413 | body over 16 KiB |

MACs are accepted in any case with `:`, `-`, or no separators and are always
stored and returned as lowercase `aa:bb:cc:dd:ee:ff`.

## Lease file

One lease per line: `<expiry-epoch> <mac> <ip> <hostname|*> <client-id|*>`.
An expiry of `0` means the lease never expires. The file is re-read on every
request.

## Layout

| Module | Owns |
| --- | --- |
| `netpin/macs.py` | MAC/IP/hostname validation and normalization |
| `netpin/leases.py` | lease file parsing |
| `netpin/store.py` | SQLite reservation store |
| `netpin/hostdir.py` | dhcp-hostsdir publishing |
| `netpin/service.py` | reservation workflows (validate → store → publish) |
| `netpin/api.py` | HTTP routing, auth, error mapping |
| `netpin/cli.py` | process wiring |

## Test

```sh
python3 -m unittest
```
