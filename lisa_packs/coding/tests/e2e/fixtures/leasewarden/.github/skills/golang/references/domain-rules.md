# Lease and reservation domain rules

## Two sources, two owners

Dnsmasq owns the lease file.
Leasewarden treats it as read-only observed state.
Leasewarden owns MAC-named records in its dedicated hosts directory.
Dnsmasq consumes those records as configured reservations.
There is no SQLite file, schema, or migration layer.
Do not infer lease state from the reservation directory.
Do not rewrite lease records while changing a reservation.

## Lease record format

Each valid input line has exactly five whitespace-separated fields:

```text
<expiry-epoch> <mac> <ipv4> <hostname-or-star> <client-id-or-star>
```

Example records:

```text
4102444800 AA:BB:CC:DD:EE:01 192.168.1.20 Laptop 01:aa:bb
0 aa-bb-cc-dd-ee-02 192.168.1.21 * *
1000 aa:bb:cc:dd:ee:01 192.168.1.99 old *
```

Expiry is a nonnegative integer in Unix seconds.
Zero means the record never expires.
A positive expiry is active only when strictly later than the clock.
Hostname and client-ID stars become empty strings.
Client ID is opaque; it is not a second device identity.
Bad records are skipped without invalidating the whole snapshot.
Scanner I/O errors are returned, not turned into empty results.

## Snapshot semantics

Every read opens the configured lease path.
There is no background watcher or cached lease index.
The lease list preserves file order.
Queries include expired records for operator diagnosis.
`ByMAC` returns all matching records, not one preferred address.
`ByIP` returns all matching devices, not an arbitrarily selected owner.
A missing file is an empty snapshot after startup.
An unreadable file is an operational error.
Managed subnet filtering applies before records leave LeaseFile.

## MAC identity

Common colon, dash, and bare hexadecimal spellings are accepted.
Case and surrounding whitespace do not affect identity.
Mixed separators and dotted notation are rejected.
Exactly six octets are required.
Canonical identity is lowercase colon notation.
Use that canonical identity in JSON and record contents.
Use `macs.Filename` for file paths rather than user input.
The filename uses lowercase dash notation with `.conf`.
Neither IP nor hostname is a filename component.

## IPv4 and subnet

Use `net/netip` rather than hand-splitting octets.
Only actual IPv4 addresses are accepted.
IPv4-mapped IPv6 forms are still IPv6 and are rejected.
Unspecified, loopback, and multicast addresses are rejected.
A configured subnet limits the addresses managed by the service.
The server validates and masks its configured IPv4 prefix at startup.
Address validation happens on direct reservation input too.
The Store itself validates IPv4 syntax but has no subnet configuration.
The API applies the subnet rule before calling the Store.

## Hostnames

Normalize labels to lowercase.
Blank and star labels mean no hostname.
Accept one DNS label, at most 63 characters.
Allow letters, digits, and internal hyphens.
Do not accept leading or trailing hyphens.
Do not accept dots, commas, spaces, or embedded newlines.
A hostname must not inject additional dnsmasq fields.
The optional hostname is omitted from the hostsdir line when empty.
JSON still carries the empty string.

## Hostsdir records

A managed file holds one bare comma-separated record:

```text
aa:bb:cc:dd:ee:01,192.168.1.10,nas
```

This differs from a dnsmasq configuration file.
Do not prepend `dhcp-host=` to hostsdir records.
Do not write JSON into the directory.
Do not add comments, tags, or multiple records to a managed file.
End the record with one newline.
The record's MAC must match its filename.
Malformed managed records are visible Store errors.
Unrelated files and directories are ignored by List.

## Persistence and publication

Put validates the complete record.
Create rejects an existing MAC.
Replace requires an existing MAC.
Delete requires an existing managed filename.
Write staging and rename occur in the destination directory.
The staging filename is not a managed `.conf` filename.
The target mode is 0640.
The directory mode on creation is 0750.
A successful change invokes the configured Publisher afterward.
Filesystem atomicity does not imply atomicity across separate processes.
Do not share the directory between multiple writer daemons.
