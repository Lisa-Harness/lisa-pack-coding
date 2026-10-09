# Terminal client

The same binary runs either the server or one client command.
Client global flags must appear before the command; server flags follow
`server`. The standard `flag` parser stops at the first positional argument.

## Configuration

| Setting | Environment | Default | Client flag |
| --- | --- | --- | --- |
| Server URL | `LEASEWARDEN_SERVER` | `http://127.0.0.1:8068` | `--server` |
| Output | `LEASEWARDEN_OUTPUT` | `table` | `--output` |
| Token | `LEASEWARDEN_TOKEN` | empty | none |
| Deadline | none | 5 seconds | `--timeout` |

Explicit flags override environment values. An empty environment value uses
the default. Tokens are not accepted on the command line, so process listings
do not expose them. The client never prints its Authorization header.

The URL must be absolute HTTP or HTTPS and may not include credentials,
a query string, or a fragment. The client refuses redirects rather than
forwarding the token to another endpoint. It does not retry any request.

## Read commands

```sh
leasewarden leases
leasewarden clients
leasewarden addresses AA-BB-CC-DD-EE-01
leasewarden devices 192.168.1.10
leasewarden reservations list
leasewarden reservations get aa:bb:cc:dd:ee:01
```

Lease queries show current file contents, including expired history.
`clients` groups unique IP strings by MAC. It takes the last nonempty hostname
in file order, not a DNS lookup result. Addresses in summaries sort as strings.

## Write commands

```sh
leasewarden reservations add aa:bb:cc:dd:ee:01 192.168.1.10 nas
leasewarden reservations update aa:bb:cc:dd:ee:01 192.168.1.11 backup
leasewarden reservations delete aa:bb:cc:dd:ee:01
```

Add requires MAC and IP; hostname is optional. Update requires MAC and IP and
replaces the record. Delete prints nothing in table mode. A mutation that
fails is printed to stderr and exits with a nonzero status. Inspect errors
before retrying, especially when the server reports a failed dnsmasq reload.

## Machine-readable output

```sh
leasewarden --output json leases
leasewarden --server http://lab-router:8068 --output json reservations list
leasewarden --timeout 2s devices 192.168.1.10
```

JSON is indented and preserves the response shape. A successful deletion is
`null` in JSON mode. Table output has stable preferred columns and alphabetic
extra columns. Empty arrays display `(empty)`; omitted cells are blank.
Control characters are replaced with spaces in table cells, not in JSON.

## Help and version

```sh
leasewarden --help
leasewarden server --help
leasewarden version
```

Help requires neither a token nor a running server. `version` reports the
build's version string; local builds report `development` by default.
