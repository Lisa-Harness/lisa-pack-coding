---
name: golang
description: 'Use when writing, reviewing, testing, or debugging Go in Leasewarden: net/http handlers, dnsmasq lease parsing, hostsdir reservations, CLI commands, output, token auth, process lifecycle, or offline builds.'
---

# Go development in Leasewarden

## Start with ownership

Leasewarden deliberately uses the standard library throughout.
It has one entrypoint and several small packages, not a generated framework.
Read the architecture map in `.github/copilot-instructions.md` first.
The nearest implementation and its tests explain the established contracts.
Do not infer a database layer from the product's persistence requirements.

## Reference index

Load references for the work you are doing, not all references by default.
Each reference describes current behavior and a focused verification loop.

| Work | Reference |
| --- | --- |
| Naming, errors, synchronization, comments | [code-style](./references/code-style.md) |
| Routes, JSON, body validation, middleware | [HTTP handlers](./references/http-handlers.md) |
| Test helpers, files, fakes, failure assertions | [testing](./references/testing.md) |
| Lease snapshots, MACs, hostsdir, subnet scope | [domain rules](./references/domain-rules.md) |
| Commands, transport, flags, rendering | [CLI and output](./references/cli-and-output.md) |
| Startup, signals, build and operator checks | [operations](./references/operations.md) |

## Evidence-first workflow

1. Identify the route, package, or operator command involved.
2. Read its existing implementation before proposing a change.
3. Read the matching reference from the index.
4. Find the nearest test and preserve its fixture conventions.
5. Reproduce the requested behavior with a focused test.
6. Change the producer of the behavior, not downstream presentation.
7. Run the focused test while iterating.
8. Run all packages and vet before finishing.
9. Check formatting and the diff for unrelated changes.
10. Update README or CLI when a user-visible contract changes.

## Local verification

```sh
go test ./...
go vet ./...
gofmt -l .
```

A clean formatting check prints nothing.
The root handlers are exercised with `httptest`, without a running dnsmasq.
File tests create private directories and do not use host configuration.
The transport and command packages have their own HTTP test servers.

## Constraints worth preserving

- Runtime state belongs in injected dependencies, not package globals.
- `newRouter(routerDeps)` is the composition seam used by tests.
- `LeaseFile` opens a new snapshot on every read.
- `Store` persists records in dnsmasq-compatible files.
- MAC identity is always lowercase colon notation in JSON.
- Publish happens synchronously after successful file mutation.
- The bearer token is fixed and supplied through the environment.
- API errors must not reveal filesystem paths or secret values.
- Help must work without network access.
- No external module is needed for any current package.
